# 23 — Database Observability

> Most outages bottom out in a database. The slow trace you're staring at? Its longest span is a SQL query. The cascading failure across services? A replica lag. The 4 AM page? A connection pool exhaustion. Database observability is the discipline of making these stories *visible* — query-by-query, plan-by-plan, replica-by-replica — not just "the database is slow."

This chapter assumes the storage internals from `doc 06` and `doc 07`, and especially leans on the database fundamentals in the sister `databases/` folder. The focus here is on *observing the database from the SRE / observability platform perspective*, not on operating the database itself.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [The four database signals](#2-four-signals)
3. [The query log: the highest-leverage source](#3-query-log)
4. [pg_stat_statements and equivalents](#4-pg-stat-statements)
5. [Plan capture: when latency comes from a bad plan](#5-plan-capture)
6. [Connection pool observability](#6-connection-pool)
7. [Replica lag and consistency signals](#7-replica-lag)
8. [Lock and wait events](#8-locks)
9. [Buffer pool / cache hit rate](#9-buffer-pool)
10. [Index and bloat metrics](#10-bloat)
11. [Per-query SLIs](#11-per-query-sli)
12. [Database tracing: the OTel database semantic conventions](#12-db-tracing)
13. [Database SLOs and golden signals](#13-db-slos)
14. [Per-database-engine specifics](#14-per-engine)
15. [Anti-patterns](#15-anti-patterns)
16. [Worked example: Postgres observability stack](#16-worked-example)
17. [Pitfalls](#17-pitfalls)
18. [Mental models](#18-mental-models)

---

## 1. Thesis

Three claims:

1. **Database SLOs are about queries, not "uptime."** A DB at 100% uptime serving 5-second p99 queries is broken. The right SLI is "fraction of queries below latency target."
2. **The database is the most expensive observability surface.** Each query is rich; per-query metrics multiply cardinality fast. Sample, aggregate, and bound.
3. **Plan capture is the secret weapon.** When a query gets slow, the answer is *almost always* a plan change. Without capturing plans, you have to recreate by archaeology — slow, error-prone, often impossible.

If your team treats the database as a "black box that's up or down," you're missing the largest performance signal source in your stack. This chapter is the discipline.

---

## 2. The Four Database Signals

| Signal | What it tells you | Volume |
|---|---|---|
| **Connection / session metrics** | Is the DB reachable? Are connections healthy? | Low; per-connection-pool |
| **Query log / statement stats** | What queries are being run? How fast? | High; per-query |
| **Plan / execution profile** | How is the DB executing this query? | Per-slow-query |
| **Resource / engine metrics** | CPU, mem, IO, buffer pool, replication | Low; per-instance |

Each is necessary. Most teams have engine metrics ("CPU 60%") but skip query-level observability — the most useful layer.

---

## 3. The Query Log: The Highest-Leverage Source

The single highest-value DB observability signal.

### 3.1 What it captures

Per query: SQL text, normalized form, execution time, rows returned, user, source IP, transaction context. With pg_stat_statements (Postgres) and equivalents, *aggregated* statistics per normalized query.

### 3.2 The "normalized" trick

```
Raw:        SELECT * FROM users WHERE id = 12345;
Raw:        SELECT * FROM users WHERE id = 67890;
Normalized: SELECT * FROM users WHERE id = $1;
```

Without normalization, cardinality is per literal value (millions). With normalization, cardinality is per query *shape* (hundreds). Normalization is the cardinality discipline of database observability.

### 3.3 The slow-query log

Most engines have a "log queries slower than X" feature. Set X to ~100ms; log every query above. This captures the *long tail*.

```postgres
log_min_duration_statement = 100ms
log_statement = 'all'           # not in production usually; expensive
```

Slow-query log is a cheap, high-signal log stream. Pipe it to the observability stack via your standard logging pipeline.

### 3.4 The auto-explain pattern

Postgres `auto_explain` extension: for any query above a threshold, automatically capture the EXPLAIN plan. Saves an enormous amount of post-facto work.

```
auto_explain.log_min_duration = '500ms'
auto_explain.log_analyze = on
auto_explain.log_buffers = on
```

Now slow queries arrive with their plans attached. Triage time drops by an order of magnitude.

---

## 4. pg_stat_statements (and Equivalents)

The aggregated query stats table.

### 4.1 What it gives

```
queryid │ query                              │ calls │ mean_exec_ms │ rows
1234    │ SELECT * FROM orders WHERE id = $1 │ 50000 │ 1.2          │ 1
5678    │ SELECT ... GROUP BY ...            │ 100   │ 850          │ 12000
```

Per normalized query: call count, total time, mean / std-dev / max time, rows returned, IO time, buffer hit rate.

### 4.2 The exporters

Convert pg_stat_statements to Prometheus metrics:

- **postgres_exporter** (the default).
- **pgwatch2** — heavier, more features.
- **Datadog Agent** with `postgres` integration.
- **Custom queries via the exporter's `queries.yaml`**.

The exporter periodically queries the stats view and emits:

```
postgres_query_runtime_seconds_total{queryid, ...}
postgres_query_calls_total{queryid, ...}
postgres_query_rows_total{queryid, ...}
```

### 4.3 The cardinality trap

`queryid` can have hundreds to thousands of values. Per-queryid metrics are reasonable. **But** if you add `database`, `user`, `client_addr`, etc., cardinality multiplies fast.

The right bounds:
- `queryid` (or query fingerprint): keep.
- `database` (the logical DB): keep (usually <10).
- `user`: keep if you have a few service accounts; drop if it's per-customer.
- `client_addr`: drop. (Use logs/traces for IP-level debugging.)

### 4.4 The "top-N" approach

Most engines have thousands of distinct query shapes. Most are negligible. Default approach:
- Track the top-N (e.g., top-100) by execution time.
- Drop the long tail.
- Alert when "new query in top-N" appears (could be a regression).

### 4.5 Equivalents in other engines

| Engine | Stats source |
|---|---|
| Postgres | pg_stat_statements |
| MySQL / MariaDB | performance_schema, sys schema |
| SQL Server | sys.dm_exec_query_stats |
| Oracle | V$SQL, V$SQLAREA |
| MongoDB | currentOp, profile |
| Cassandra | system_traces, JMX metrics |
| Redis | SLOWLOG, INFO commandstats |

The shape of the data is similar; the names differ.

---

## 5. Plan Capture

Why it matters. When a query gets slow, the cause is almost always a **plan change** — the optimizer chose a different execution strategy due to:
- Statistics drift.
- Data growth past a threshold.
- Index missing/added.
- Histogram skew.

Without plan capture, "this query slowed down" is undebuggable.

### 5.1 The mechanisms

| Engine | Plan capture |
|---|---|
| Postgres | `auto_explain` extension; `EXPLAIN (ANALYZE, BUFFERS)` ad-hoc |
| MySQL | `EXPLAIN ... FORMAT=JSON`; `optimizer_trace` |
| SQL Server | Query Store (built-in plan history) |
| Oracle | DBMS_XPLAN, AWR snapshots |
| MongoDB | `explain()` |

### 5.2 The Query Store pattern

SQL Server's Query Store keeps a persistent history of plans per query. When a query slows down, you can see the plan history and identify the regression point. Postgres lacks this; Aurora and pg_stat_kcache + pg_query_state get close.

### 5.3 The plan-as-attribute pattern

A trace span for a slow DB call can include the EXPLAIN plan as a span attribute. Engineers debugging see the plan inline:

```
db.statement: "SELECT ..."
db.plan:       "Seq Scan on orders ... cost=10000.00..50000.00"
db.rows:       12000
```

Trade-off: span size grows. Cap plan capture at slow queries only.

### 5.4 The plan-stability story

For latency-sensitive queries, plan stability is a feature. Some engines support plan hints / pinned plans. Capture is the prerequisite — you can't pin what you didn't measure.

---

## 6. Connection Pool Observability

The most common DB-related outage source.

### 6.1 The metrics

Per service's connection pool:
- Active connections (in use).
- Idle connections.
- Pool size.
- Wait time for a connection.
- Connection acquisition rate.
- Connection failures.

```
db_connections_active{service, pool}
db_connections_idle{service, pool}
db_pool_max_size{service, pool}
db_pool_wait_seconds{service, pool}     # histogram
db_pool_acquire_failures_total{service, pool}
```

### 6.2 The saturation calculation

```
saturation = active / max
wait time > 0 → already saturated
```

When pool saturation hits 1, requests queue. Wait-time histogram tells you how long. As soon as p99 wait > 0, latency is rising.

### 6.3 The "pool too small" diagnostic flow

```
1. Service latency spikes.
2. Trace shows DB call latency dominant.
3. DB itself reports normal latency.
4. Pool wait time elevated.
→ Pool too small / saturated. Increase pool or fix slow callers.
```

This flow is so common that connection-pool metrics deserve their own dashboard panel on every service-RED dashboard.

### 6.4 The other side: too-large pools

A pool of 1000 connections per service × 100 services × 10 replicas = 1M connections expected. Most DBs cannot handle that. Pool sizing is a *coordination* problem; the DB has a connection limit, and pools must respect it.

The pattern: PgBouncer (or equivalent) in front of Postgres for connection multiplexing. Then per-service pool sizing matters less.

---

## 7. Replica Lag and Consistency Signals

When replicas matter (read-heavy workloads, hot standby, async analytics).

### 7.1 The metrics

```
postgres_replication_lag_seconds{replica="..."}
postgres_replication_lag_bytes{replica="..."}
postgres_replication_active{replica="..."}    # binary; replication healthy?
```

### 7.2 The freshness SLI

A read replica with 30 seconds of lag returns 30-second-stale data. For some workloads this is fine; for others it's a correctness bug.

```yaml
sli:
  name: replica_freshness
  metric: replication_lag_seconds < 5
  target: 0.999  # 99.9% of the time, lag < 5s
```

This is a freshness SLI (`doc 13 §2.1`).

### 7.3 The fail-over signal

When the primary dies and a replica is promoted: the lag at promotion = data lost. Track this; it's the RPO (`doc 36`).

### 7.4 The "stale-read" anti-pattern

Reads accidentally hitting a replica that's lagging cause customer bugs ("I just placed an order; why doesn't my order list show it?"). Defenses:
- Pin recent-write reads to the primary for N seconds.
- Track stale-read incidents in error metrics.
- Use logical timestamps (CRDB, Spanner) to guarantee read-after-write.

---

## 8. Locks and Wait Events

The other class of DB latency.

### 8.1 What to look for

```
postgres_locks_total{mode}
postgres_wait_events_seconds{event}    # which wait events accumulate?
```

Postgres `pg_stat_activity` exposes per-session wait events. Aggregate; expose top wait events.

### 8.2 The lock contention story

Two transactions try to update the same row. One waits. If the wait exceeds a timeout, query times out. The pattern:
- Locks growing → contention.
- Wait events on `Lock` → blocked.
- Long-running blocking transaction → look for an open tx not committing.

Tools: `pg_blocking_pids`, query store for long-running queries, alerts on max-tx-age.

### 8.3 The deadlock metric

```
postgres_deadlocks_total
```

A deadlock indicates broken transaction ordering or schema design. Rare deadlocks fine; recurring deadlocks need investigation.

---

## 9. Buffer Pool / Cache Hit Rate

The "is the database memory-bound?" signal.

### 9.1 The metric

```
postgres_blocks_hit_total / (postgres_blocks_hit_total + postgres_blocks_read_total)
```

A "hit" means the page was in shared_buffers. A "read" means it had to come from disk. Hit rate < 99% is a sign of memory pressure or working-set growth past memory.

### 9.2 The diagnostic

- Hit rate 99.9%: comfortable.
- Hit rate 95-99%: monitor; data working set near memory limit.
- Hit rate < 95%: capacity event; either grow memory, prune working set, or accept higher latency.

For MySQL InnoDB: `innodb_buffer_pool_read_requests` vs `innodb_buffer_pool_reads`.

### 9.3 The cache-warm pattern

On DB restart, hit rate is 0% — the cache is cold. Latency spikes for minutes to hours. Monitor cache warm-up; prefer rolling restarts that prewarm one replica before flipping.

---

## 10. Index and Bloat Metrics

The "the table is rotting" signals.

### 10.1 Bloat (Postgres)

Postgres MVCC keeps old row versions; vacuuming reclaims. If vacuum lags, tables and indexes bloat.

```
postgres_table_bloat_ratio{table}
postgres_index_bloat_ratio{index}
```

Bloat ratio > 50% is a sign vacuum is unable to keep up. Causes: long-running transactions blocking VACUUM, too few autovacuum workers, schema patterns that break vacuum.

### 10.2 Index usage

```
postgres_index_scans_total{index}     # how often is this index used?
postgres_index_size_bytes{index}      # how much storage does it occupy?
```

Indexes that are never scanned cost storage and slow writes. Quarterly review: drop unused indexes.

### 10.3 Missing-index detection

EXPLAIN plans showing sequential scans on large tables → missing index. Some tools (pganalyze, Datadog) auto-suggest indexes from plan history. Use them.

---

## 11. Per-Query SLIs

The shift from DB-instance SLOs to per-query SLOs.

### 11.1 Why per-query

A DB-level SLO ("p99 < 100ms") averages all queries; one slow query type hides in the noise. Per-query SLOs surface the actual broken patterns.

### 11.2 The pattern

Identify the top-N queries by traffic. Set SLOs:

```yaml
sli:
  name: orders_by_id_lookup
  metric: pg_stat_statements_mean_time_seconds_bucket{queryid="...", le="0.01"}
  target: 0.999

sli:
  name: customer_search
  metric: pg_stat_statements_mean_time_seconds_bucket{queryid="...", le="0.1"}
  target: 0.99
```

Each top query has its own SLO. Regressions in any one are individually visible.

### 11.3 The "new top-N query" alert

Alert when a new query enters the top 10 by execution time. This often signals:
- A new feature deployed with an unoptimized query.
- A data growth threshold breached, changing query plans.
- A bug introducing N+1 or full-scan patterns.

---

## 12. Database Tracing: The OTel Database Semantic Conventions

OTel standardizes how to instrument DB calls.

### 12.1 The attributes

```
db.system        = "postgresql"
db.name          = "checkout"
db.statement     = "SELECT * FROM orders WHERE id = $1"
db.operation     = "SELECT"
db.sql.table     = "orders"
db.user          = "checkout_app"
db.connection_string  (without password)
peer.service     = "primary-db"
net.peer.name    = "db.checkout.svc"
net.peer.port    = 5432
```

Each DB call becomes a span; the span has these attributes. Now traces include DB-level detail, queryable.

### 12.2 The auto-instrumentation

OTel SDKs auto-instrument popular drivers:
- `psycopg2`, `asyncpg` (Python)
- `pgx`, `database/sql` (Go)
- `pg`, `mysql2` (Node)
- JDBC (Java)

Drop in OTel; DB spans appear automatically in traces.

### 12.3 The PII hazard

`db.statement` can contain PII if literals aren't normalized. Many SDKs default to *normalize* the statement (replace literals with placeholders). Verify; PII in span attributes is the same compliance issue as in logs.

### 12.4 The cardinality hazard

`db.statement` is high-cardinality. Don't use it as a metric label. Use the *queryid* / fingerprint for metric labels; keep statement in span attributes only.

---

## 13. Database SLOs and Golden Signals

The four golden signals (`doc 00 §3`) for databases.

| Signal | DB version |
|---|---|
| **Latency** | P99 query time, by query class |
| **Traffic** | Queries per second, by query class |
| **Errors** | Failed queries; deadlocks; timeouts |
| **Saturation** | Connection pool utilization; CPU; IO wait; lock waits |

### 13.1 The golden DB SLOs

```yaml
- name: db_query_latency
  threshold: 100ms (p99)
  target: 0.999
- name: db_error_rate
  metric: db_errors / db_queries
  target: 0.9999
- name: db_replication_freshness
  metric: replication_lag_seconds < 5
  target: 0.999
```

Three SLOs, journey-aligned. Most DB outages are caught by one of these.

### 13.2 The "DB is fine, app is slow" failure mode

Common: DB metrics green; app latency spiking. Causes:
- App-side connection pool saturated.
- Network between app and DB slow (mesh, NAT).
- App ORM doing N+1.

Pattern: include DB call latency *measured at the app side* (via OTel) alongside DB-side metrics. Discrepancies localize the issue.

---

## 14. Per-Database-Engine Specifics

Each engine has its idioms.

### 14.1 Postgres

- pg_stat_statements (must be enabled at install).
- pg_stat_activity (current sessions).
- pg_stat_replication (replica lag).
- auto_explain extension (plan capture).
- Vacuum and bloat metrics.
- pgBouncer in front for connection multiplexing.

Exporter: postgres_exporter (default) or pgwatch2 (more features).

### 14.2 MySQL

- performance_schema and sys schema.
- slow query log.
- Replica lag via SHOW SLAVE STATUS / Performance Schema.
- InnoDB buffer pool metrics.

Exporter: mysqld_exporter.

### 14.3 MongoDB

- currentOp / db.profile.
- replSetGetStatus for replica lag.
- WiredTiger cache stats.

Exporter: mongodb_exporter.

### 14.4 Cassandra

- nodetool tpstats, cfstats.
- JMX metrics.
- Read/write latency histograms per CF.

Exporter: cassandra_exporter (JMX-based).

### 14.5 Redis

- INFO command (latency, memory, replication).
- SLOWLOG.
- Latency monitor (Redis 2.8+).

Exporter: redis_exporter.

### 14.6 Cloud-managed (Aurora, RDS, Cloud SQL, Cosmos DB)

Native CloudWatch / Stackdriver / Azure Monitor metrics; rich but cloud-locked. Often the best baseline; augment with engine-native exporters where deeper insight needed.

---

## 15. Anti-Patterns

1. **DB observed only as "up/down."** Misses 90% of real failures.
2. **No query log capture.** Slow queries un-debuggable.
3. **No pg_stat_statements / equivalent.** No aggregate query insights.
4. **No plan capture.** Plan regressions invisible.
5. **No connection pool metrics.** Pool exhaustion looks like DB outage.
6. **`db.statement` as metric label.** Cardinality bomb.
7. **Per-query metrics with PII literals.** Privacy + cardinality.
8. **No replica lag SLO.** Stale-read bugs un-attributed.
9. **No bloat monitoring.** Tables degrade silently.
10. **No vacuum / autovacuum monitoring.** Slow vacuum cascades.
11. **No buffer-pool hit rate.** Memory-pressure invisible.
12. **No auto_explain.** Slow queries arrive without plans.
13. **No DB tracing.** Service traces incomplete.
14. **Cloud-only metrics, no native.** CloudWatch alone misses query-level detail.
15. **No DB SLOs at the journey level.** DB invisible in user-experience SLOs.

---

## 16. Worked Example: Postgres Observability Stack

Concrete and complete.

### 16.1 The setup

- Postgres 15 (primary + 2 read replicas) on AWS.
- 200 GB working set, 6.5M queries/day.
- Aurora Postgres for some services, self-managed for others.

### 16.2 Components

- **postgres_exporter** scraped by Prometheus (per-instance).
- **pg_stat_statements** enabled; per-query metrics via `queries.yaml`.
- **auto_explain** with `log_min_duration = 500ms`.
- **OTel auto-instrumentation** in app SDKs (Go pgx, Python asyncpg).
- **Slow-query log** shipped to Loki via Fluent Bit.
- **PgBouncer** in front for connection multiplexing.

### 16.3 Dashboards

- Per-DB RED panel (latency, traffic, errors).
- Top 20 queries by total time.
- Top 20 queries by frequency.
- Connection pool utilization.
- Replica lag.
- Cache hit rate.
- Bloat per table.
- Vacuum statistics.

### 16.4 Alerts

- DB error rate burn > 14.4× normal: page.
- Replication lag > 30s: page.
- Connection pool wait p99 > 100ms: page.
- Cache hit rate < 95%: ticket.
- Bloat ratio > 50% on critical tables: ticket.
- New query in top-10 by total time: ticket.

### 16.5 Per-query SLOs

```yaml
- query: "SELECT * FROM orders WHERE customer_id = $1"
  threshold: 50ms
  target: 0.999
- query: "INSERT INTO orders ..."
  threshold: 30ms
  target: 0.9995
```

### 16.6 Outcomes

- Mean DB-related incident resolution time: dropped from 90 min to 18 min after plan capture and per-query metrics.
- 1 prevented outage: a deploy introduced an N+1 query; alert "new query in top-10" caught it within 5 minutes; rollback before SLO burn.

---

## 17. Pitfalls

1. **No query log.** Most DB performance problems un-debuggable.
2. **No statement-level stats.** Aggregate insight missing.
3. **Plan history not captured.** Regressions un-localizable.
4. **Cardinality on per-query metrics.** Memory bomb.
5. **Connection pool unobserved.** Pool exhaustion mistaken for DB outage.
6. **Replica lag unmonitored.** Stale reads, RPO loss.
7. **Bloat / vacuum invisible.** Slow degradation.
8. **Buffer pool unmonitored.** Memory pressure invisible.
9. **No DB tracing.** Service traces incomplete.
10. **DB SLOs not journey-aligned.** User pain hidden.
11. **PII in db.statement.** Compliance and cardinality.
12. **Cloud-only metrics.** Query-level insights miss.
13. **No top-N query review.** Drift unmonitored.
14. **No connection-pool sizing coordination.** Pools exceed DB connection limit.
15. **No "new top-N" alert.** Regressions ship undetected.

---

## 18. Mental Models

> **DB SLOs are query-level, not instance-level.**

> **The query log is the highest-leverage source. Capture it; normalize it; aggregate it.**

> **Plan capture is the secret weapon. auto_explain in Postgres; Query Store in SQL Server.**

> **Connection pool metrics are mandatory.** Pool exhaustion looks like DB latency from the app side.

> **Replica lag is a freshness SLI.** Stale reads are real bugs.

> **Per-query metrics use queryid (fingerprint), not statement text.**

> **OTel auto-instruments DB drivers. Use it.**

> **The "new top-N" alert catches regressions before SLO burn.**

> **Bloat, vacuum, cache hit — the slow-rot signals. Quarterly review.**

> **The DB is the most common bottom of an incident chain. Observe it accordingly.**

Now go to `doc 24` (network observability) — the layer below DB calls, where retransmits and packet loss live.
