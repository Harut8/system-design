# SQL Performance: A Deep Dive

A staff-engineer-level reference on SQL database performance -- where query time actually goes, how to measure it, how to fix it, and how to prevent regressions. This document covers the full lifecycle: from understanding latency components at the microsecond level, through profiling and diagnosis, to production monitoring, alerting, and capacity planning. It is focused on practical performance engineering, not theory.

Prerequisites: familiarity with query engine internals (`04-query-engine-internals.md`), indexing (`06-indexing-internals.md`), and OLTP database internals (`07-oltp-databases.md`).

---

## Table of Contents

1. [Anatomy of Query Latency](#1-anatomy-of-query-latency)
2. [Measuring Performance: Tools and Methodology](#2-measuring-performance-tools-and-methodology)
3. [Statistics and the Optimizer](#3-statistics-and-the-optimizer)
4. [Performance Anti-Patterns](#4-performance-anti-patterns)
5. [Advanced EXPLAIN Analysis](#5-advanced-explain-analysis)
6. [Memory, Caching, and Spill Behavior](#6-memory-caching-and-spill-behavior)
7. [Lock Contention and Concurrency](#7-lock-contention-and-concurrency)
8. [VACUUM, Bloat, and Maintenance](#8-vacuum-bloat-and-maintenance)
9. [Monitoring and Alerting](#9-monitoring-and-alerting)
10. [Capacity Planning and Benchmarking](#10-capacity-planning-and-benchmarking)
11. [Performance Debugging Workflows](#11-performance-debugging-workflows)
12. [System-Specific Tuning Reference](#12-system-specific-tuning-reference)

---

## 1. Anatomy of Query Latency

### Where Time Goes

Every SQL query passes through a pipeline of stages. Understanding where time is spent is the first step to improving performance. Most engineers blame the wrong stage.

```
QUERY LATENCY BREAKDOWN:

  Client                            Database Server
  ──────                            ───────────────
  ┌──────────┐                      ┌──────────────────────────────────┐
  │ App code │ ── network ──────► │ 1. Connection handling (0.01ms)  │
  │          │    round-trip       │ 2. Parse SQL (0.01-0.1ms)       │
  │          │    (0.1-100ms)      │ 3. Analyze/Rewrite (0.01ms)     │
  │          │                     │ 4. Plan/Optimize (0.1-50ms)     │
  │          │                     │ 5. Execute                       │
  │          │                     │    a. Lock acquisition (0-∞ms)  │
  │          │                     │    b. Buffer pool lookups        │
  │          │                     │    c. Disk I/O (if cache miss)  │
  │          │                     │    d. CPU computation            │
  │          │                     │    e. WAL writes (on mutation)  │
  │          │                     │ 6. Send results                  │
  │          │ ◄── network ──────│    (serialization + network)     │
  └──────────┘    round-trip       └──────────────────────────────────┘

  Typical OLTP query (index lookup, 1 row):
  ┌─────────────────────────┬──────────────┬────────────────────────┐
  │ Stage                    │ Time         │ Notes                   │
  ├─────────────────────────┼──────────────┼────────────────────────┤
  │ Network (client → DB)   │ 0.1-1 ms     │ Same datacenter         │
  │ Parse                   │ 0.01 ms      │ Cached with prepared    │
  │ Plan                    │ 0.05-0.2 ms  │ Simple plans are fast   │
  │ Execute (buffer hit)    │ 0.01-0.05 ms │ Page in shared_buffers  │
  │ Execute (buffer miss)   │ 0.05-0.2 ms  │ Page in OS cache        │
  │ Execute (disk read)     │ 0.05-5 ms    │ NVMe SSD vs HDD         │
  │ Network (DB → client)   │ 0.1-1 ms     │ Small result set        │
  ├─────────────────────────┼──────────────┼────────────────────────┤
  │ TOTAL                   │ 0.3-8 ms     │ Point lookup range      │
  └─────────────────────────┴──────────────┴────────────────────────┘

  Typical OLTP query (sequential scan, 100K rows):
  ┌─────────────────────────┬──────────────┬────────────────────────┐
  │ Stage                    │ Time         │ Notes                   │
  ├─────────────────────────┼──────────────┼────────────────────────┤
  │ Parse + Plan            │ 0.1-0.5 ms   │                         │
  │ Execute (seq scan)      │ 50-500 ms    │ Depends on table size   │
  │ Network (100K rows)     │ 10-100 ms    │ Result serialization    │
  ├─────────────────────────┼──────────────┼────────────────────────┤
  │ TOTAL                   │ 60-600 ms    │ Dominated by scan + net │
  └─────────────────────────┴──────────────┴────────────────────────┘
```

### The Network Tax

```
NETWORK LATENCY — THE HIDDEN DOMINATOR:

  For fast OLTP queries, network round-trips often exceed execution time.
  A query that executes in 0.1ms can take 2ms end-to-end due to network.

  Same machine (Unix socket):     0.02-0.05 ms
  Same rack (1Gbps):              0.1-0.3 ms
  Same datacenter (10Gbps):       0.2-0.5 ms
  Cross-AZ (same region):         0.5-2 ms
  Cross-region:                   20-200 ms

  Impact on N+1 queries:
    Fetching 100 related records one-by-one:
      100 × 0.5ms network = 50ms just in network
      100 × 0.1ms execution = 10ms in actual work

    Fetching 100 records in one query:
      1 × 0.5ms network = 0.5ms
      1 × 1ms execution = 1ms

    N+1 is 33x slower — and network is the main reason.

  Mitigations:
    - Batched queries (WHERE id IN (...))
    - Connection pooling (avoid connection setup overhead)
    - Prepared statements (skip re-parsing)
    - Server-side cursors for large result sets
    - Co-locate app servers with database (same AZ)
    - Use Unix sockets when app and DB are on the same host
```

### Parse and Plan Overhead

```
PARSE + PLAN: USUALLY NEGLIGIBLE, SOMETIMES NOT

  Parse time:
    Simple query:     0.01-0.05 ms
    Complex query:    0.1-0.5 ms (many joins, subqueries, CTEs)

    PostgreSQL caching:
      Prepared statement: parse once, reuse plan
      PG 12+: automatic plan caching after 5 executions
              (custom plan vs generic plan decision)

    MySQL caching:
      Prepared statements: parse once, optimize once (per session)
      Query cache: removed in MySQL 8.0 (too many invalidation issues)

  Plan time:
    Simple (single table, obvious index):    0.05-0.1 ms
    Medium (2-3 joins):                      0.1-1 ms
    Complex (5+ joins):                      1-50 ms
    Pathological (10+ joins, no constraints): 100ms-seconds
      Join ordering is NP-hard. PostgreSQL uses GEQO (Genetic Query
      Optimizer) when join count exceeds geqo_threshold (default 12).

  When planning becomes the bottleneck:
    - High-frequency queries (>10K/sec) with complex plans
    - Repeated identical queries without prepared statements
    - Excessive use of dynamic SQL preventing plan caching

    Fix: use prepared statements.
      PostgreSQL: PREPARE stmt AS SELECT ... ; EXECUTE stmt(param);
      Application: use parameterized queries in your ORM/driver.
      Connection poolers: PgBouncer in transaction mode breaks
        prepared statements — use pg_prepared_statements workaround
        or switch to session mode for heavy prepared statement usage.
```

### I/O Latency Hierarchy

```
I/O LATENCY: THE PERFORMANCE CLIFF

  Every database operation ultimately reads or writes pages.
  The latency depends entirely on WHERE the page lives:

  ┌────────────────────────────────┬──────────────────┬──────────────┐
  │ Location                        │ Read latency      │ Throughput    │
  ├────────────────────────────────┼──────────────────┼──────────────┤
  │ L1 CPU cache                   │ 1 ns              │ ~1 TB/s       │
  │ L2 CPU cache                   │ 4 ns              │ ~500 GB/s     │
  │ L3 CPU cache                   │ 12 ns             │ ~200 GB/s     │
  │ RAM (buffer pool / OS cache)   │ 80-100 ns         │ ~50 GB/s      │
  │ NVMe SSD (random 4K read)     │ 10-20 μs          │ ~3-7 GB/s     │
  │ SATA SSD (random 4K read)     │ 50-100 μs         │ ~500 MB/s     │
  │ HDD (random 4K read)          │ 2,000-10,000 μs   │ ~1-2 MB/s     │
  │ Network storage (EBS gp3)     │ 200-500 μs        │ ~125-1000 MB/s│
  │ Network storage (EBS io2)     │ 100-200 μs        │ ~256-4000 MB/s│
  └────────────────────────────────┴──────────────────┴──────────────┘

  The cliff between RAM and disk:
    Buffer pool hit:  0.0001 ms (100 ns)
    NVMe miss:        0.02 ms   (20 μs)    → 200x slower
    HDD miss:         5 ms      (5000 μs)  → 50,000x slower

  This is why buffer pool hit ratio matters so much.
  A query touching 1000 pages:
    100% cache hit:  0.1 ms
    90% hit, NVMe:   0.1 + 100 × 0.02 = 2.1 ms    (21x slower)
    90% hit, HDD:    0.1 + 100 × 5 = 500 ms        (5000x slower)
    50% hit, HDD:    0.1 + 500 × 5 = 2500 ms       (25000x slower)

  Buffer pool hit ratio query (PostgreSQL):
    SELECT
      sum(blks_hit) AS hits,
      sum(blks_read) AS misses,
      round(sum(blks_hit)::numeric /
            nullif(sum(blks_hit) + sum(blks_read), 0) * 100, 2) AS hit_ratio
    FROM pg_stat_database;

    Target: > 99% for OLTP workloads.
    < 95%: you are I/O bound. Increase shared_buffers or add RAM.
```

---

## 2. Measuring Performance: Tools and Methodology

### The Measurement Stack

```
PERFORMANCE MEASUREMENT LAYERS:

  Layer 1: Application-side timing
  ─────────────────────────────────
    Measure end-to-end query duration from your application.
    Includes network, parse, plan, execute, fetch.
    Tools: APM (Datadog, New Relic), custom timing, ORM logging

  Layer 2: Database-side query logging
  ─────────────────────────────────────
    Log slow queries with execution time, rows examined, plan info.
    Excludes network latency.
    Tools: pg_stat_statements, slow query log, performance_schema

  Layer 3: Execution plan analysis
  ─────────────────────────────────
    EXPLAIN ANALYZE: per-operator timing and row counts.
    Shows exactly where time is spent within the query.

  Layer 4: System-level profiling
  ────────────────────────────────
    OS-level I/O, CPU, memory, and lock profiling.
    Tools: perf, bpftrace, eBPF, iostat, vmstat

  Layer 5: Hardware counters
  ──────────────────────────
    CPU cache misses, TLB misses, branch mispredictions.
    Tools: perf stat, Intel VTune
    Rarely needed outside kernel/engine development.

  CRITICAL RULE: Always start from Layer 1 and drill down.
  Don't jump to perf before checking EXPLAIN ANALYZE.
  Don't check EXPLAIN before looking at pg_stat_statements.
```

### pg_stat_statements (PostgreSQL)

```
pg_stat_statements — THE MOST IMPORTANT PERFORMANCE VIEW

  Tracks execution statistics for every distinct query (normalized).
  Must be enabled: shared_preload_libraries = 'pg_stat_statements'

  Key columns:
  ┌──────────────────────┬──────────────────────────────────────────────┐
  │ Column                │ What it tells you                            │
  ├──────────────────────┼──────────────────────────────────────────────┤
  │ query                │ Normalized query text ($1, $2 for params)    │
  │ calls                │ Number of times executed                      │
  │ total_exec_time      │ Total execution time (ms) across all calls  │
  │ mean_exec_time       │ Average execution time per call              │
  │ min_exec_time        │ Fastest execution                            │
  │ max_exec_time        │ Slowest execution (outlier indicator)        │
  │ stddev_exec_time     │ Standard deviation (consistency indicator)   │
  │ rows                 │ Total rows returned                          │
  │ shared_blks_hit      │ Buffer pool hits (pages found in cache)      │
  │ shared_blks_read     │ Buffer pool misses (pages read from disk)   │
  │ shared_blks_dirtied  │ Pages dirtied by this query                  │
  │ shared_blks_written  │ Pages written by this query                  │
  │ local_blks_*         │ Same, for temporary tables                   │
  │ temp_blks_read/write │ Temp file I/O (spills to disk)              │
  │ blk_read_time        │ Time spent reading blocks (if track_io_     │
  │ blk_write_time       │ timing=on). THE most useful timing metric.  │
  │ wal_records          │ WAL records generated (PG 13+)              │
  │ wal_bytes            │ WAL bytes generated (PG 13+)                │
  │ plans                │ Number of times planned (PG 13+)            │
  │ total_plan_time      │ Total planning time (PG 13+)                │
  └──────────────────────┴──────────────────────────────────────────────┘

  Essential queries:

  -- Top 10 queries by total time (where to focus optimization):
  SELECT query,
         calls,
         round(total_exec_time::numeric, 2) AS total_ms,
         round(mean_exec_time::numeric, 2) AS avg_ms,
         round(stddev_exec_time::numeric, 2) AS stddev_ms,
         rows
  FROM pg_stat_statements
  ORDER BY total_exec_time DESC
  LIMIT 10;

  -- Top 10 queries by I/O (cache misses):
  SELECT query,
         calls,
         shared_blks_read AS cache_misses,
         shared_blks_hit AS cache_hits,
         round(shared_blks_read::numeric /
               nullif(shared_blks_hit + shared_blks_read, 0) * 100, 2)
               AS miss_pct,
         round(blk_read_time::numeric, 2) AS io_time_ms
  FROM pg_stat_statements
  WHERE shared_blks_read > 0
  ORDER BY shared_blks_read DESC
  LIMIT 10;

  -- Queries with high variance (intermittent slowness):
  SELECT query,
         calls,
         round(mean_exec_time::numeric, 2) AS avg_ms,
         round(max_exec_time::numeric, 2) AS max_ms,
         round(stddev_exec_time::numeric, 2) AS stddev_ms,
         round(stddev_exec_time / nullif(mean_exec_time, 0), 2)
               AS cv  -- coefficient of variation
  FROM pg_stat_statements
  WHERE calls > 100
  ORDER BY stddev_exec_time DESC
  LIMIT 10;

  -- Queries generating the most WAL (write amplification):
  SELECT query,
         calls,
         wal_records,
         pg_size_pretty(wal_bytes) AS wal_volume,
         round(wal_bytes::numeric / nullif(calls, 0), 0) AS wal_per_call
  FROM pg_stat_statements
  WHERE wal_bytes > 0
  ORDER BY wal_bytes DESC
  LIMIT 10;

  IMPORTANT: enable track_io_timing = on (default: off).
    This adds ~2% overhead but gives you blk_read_time and blk_write_time,
    which are essential for distinguishing CPU-bound from I/O-bound queries.
    Without this, you're flying blind.
```

### MySQL Performance Schema

```
MYSQL PERFORMANCE MEASUREMENT:

  1. Performance Schema (events_statements_summary_by_digest):
     MySQL's equivalent of pg_stat_statements.

     SELECT DIGEST_TEXT,
            COUNT_STAR AS calls,
            ROUND(SUM_TIMER_WAIT / 1e12, 2) AS total_sec,
            ROUND(AVG_TIMER_WAIT / 1e12, 4) AS avg_sec,
            SUM_ROWS_EXAMINED AS rows_examined,
            SUM_ROWS_SENT AS rows_sent,
            SUM_NO_INDEX_USED AS full_scans
     FROM performance_schema.events_statements_summary_by_digest
     ORDER BY SUM_TIMER_WAIT DESC
     LIMIT 10;

  2. Slow query log:
     SET GLOBAL slow_query_log = 1;
     SET GLOBAL long_query_time = 0.1;  -- 100ms threshold
     SET GLOBAL log_queries_not_using_indexes = 1;

     Use pt-query-digest (Percona Toolkit) to aggregate slow query logs:
     $ pt-query-digest /var/log/mysql/slow.log

  3. EXPLAIN ANALYZE (MySQL 8.0.18+):
     Same syntax as PostgreSQL. Shows actual row counts and timing.
     Critical addition: "actual time" in the tree-format output.

  4. sys schema (MySQL 5.7+):
     Pre-built views over performance_schema:
     SELECT * FROM sys.statements_with_full_table_scans;
     SELECT * FROM sys.statements_with_sorting;
     SELECT * FROM sys.schema_unused_indexes;
     SELECT * FROM sys.schema_redundant_indexes;
```

### Benchmarking Methodology

```
BENCHMARKING: HOW TO NOT LIE TO YOURSELF

  Rule 1: Measure steady-state, not cold start.
    First run after restart: cold buffer pool, no plan cache.
    Always run warmup iterations before measuring.

  Rule 2: Use percentiles, not averages.
    Average latency hides tail latency.
    p50 (median): "typical" user experience
    p95: 1 in 20 requests is this slow or worse
    p99: 1 in 100 requests
    p99.9: 1 in 1000 — often 10-100x the median

    Example:
      Average: 5ms    ← looks great
      p50: 2ms        ← most requests are fast
      p99: 150ms      ← 1% of users wait 150ms
      p99.9: 2000ms   ← 0.1% of users wait 2 seconds

  Rule 3: Measure at the client, not just the server.
    Server-side timing excludes network and connection overhead.
    Client-side timing is what users actually experience.

  Rule 4: Control for concurrency.
    A query that takes 1ms with 1 connection may take 50ms with
    100 connections due to lock contention, buffer pool churn,
    and CPU scheduling.

  Rule 5: Run long enough to capture variance.
    Short benchmarks miss periodic events: autovacuum, checkpoint
    I/O spikes, log rotation, background compaction.
    Minimum: run for 2× your checkpoint interval.

  Rule 6: Use realistic data distributions.
    Uniform random keys don't represent real access patterns.
    Real workloads are Zipfian: a small fraction of keys get
    most of the traffic. This affects buffer pool hit rates.

  Tools:
    pgbench (PostgreSQL):
      pgbench -c 32 -j 4 -T 300 mydb    # 32 clients, 4 threads, 5 min
      pgbench -c 32 -j 4 -T 300 -f custom.sql mydb  # custom workload

    sysbench (MySQL/PostgreSQL):
      sysbench oltp_read_write --db-driver=pgsql --tables=10
              --table-size=1000000 --threads=32 --time=300 run

    HammerDB (TPC-C):
      Full OLTP benchmark with realistic transaction mix.

    k6 / wrk / ab:
      For API-level benchmarking (includes app + network overhead).
```

---

## 3. Statistics and the Optimizer

### How Statistics Drive Plan Choices

The optimizer's plan choices are only as good as its cardinality estimates, which are only as good as the table statistics. Stale or missing statistics are the #1 cause of bad query plans.

```
STATISTICS → CARDINALITY → COST → PLAN:

  ANALYZE gathers:
  ┌──────────────────────┬──────────────────────────────────────────────┐
  │ Statistic             │ What it captures                             │
  ├──────────────────────┼──────────────────────────────────────────────┤
  │ n_distinct            │ Number of distinct values in the column      │
  │ null_frac             │ Fraction of NULL values                      │
  │ avg_width             │ Average byte width of column values          │
  │ most_common_vals      │ The N most frequent values (MCVs)            │
  │ most_common_freqs     │ Frequency of each MCV                        │
  │ histogram_bounds      │ Equal-frequency histogram bucket boundaries  │
  │ correlation           │ Physical-to-logical ordering correlation     │
  │                       │ (1.0 = perfectly ordered, 0 = random)       │
  └──────────────────────┴──────────────────────────────────────────────┘

  How the optimizer uses them:

  Query: SELECT * FROM orders WHERE status = 'shipped'

  1. Check MCVs: is 'shipped' in most_common_vals?
     YES → use most_common_freqs[i] as selectivity
     NO  → use histogram for range estimation
           or assume uniform distribution across non-MCV values

  2. Estimated rows = selectivity × n_rows
     If selectivity = 0.4 and n_rows = 1M → estimated 400K rows

  3. Compare costs:
     Index scan cost = estimated_rows × random_page_cost × ...
     Seq scan cost = total_pages × seq_page_cost × ...

     If estimated 400K rows out of 1M: seq scan wins (too many rows for index)
     If estimated 100 rows out of 1M: index scan wins

  4. Wrong statistics → wrong estimate → wrong plan:
     Actual rows = 100, but estimated = 400K → seq scan chosen → slow!
```

### When Statistics Go Wrong

```
STATISTICS FAILURE MODES:

  1. Stale statistics (most common):
     Table was bulk-loaded or heavily modified since last ANALYZE.
     Optimizer thinks table has 1K rows, actually has 10M.

     Fix: run ANALYZE.
     Prevention: tune autovacuum_analyze_threshold and
       autovacuum_analyze_scale_factor.

     Default trigger: 10% of rows changed since last ANALYZE.
     For a 100M row table: must change 10M rows before auto-ANALYZE.
     This is often too late. Set per-table:
       ALTER TABLE big_table SET (autovacuum_analyze_scale_factor = 0.01);

  2. Correlated columns:
     The optimizer assumes columns are independent.
     WHERE city = 'San Francisco' AND state = 'CA'
     Estimated selectivity: P(city=SF) × P(state=CA) = 0.01 × 0.02 = 0.0002
     Actual selectivity: P(city=SF AND state=CA) = 0.01
     Off by 50x. The optimizer underestimates the result size.

     Fix (PG 10+): CREATE STATISTICS city_state_corr (dependencies)
       ON city, state FROM addresses;
     Fix (PG 12+): extended statistics with mcv and ndistinct types.
     Fix (PG 14+): extended statistics used automatically for more cases.

  3. Non-uniform distributions:
     default_statistics_target = 100 (100 histogram buckets).
     For highly skewed data (e.g., status column with 95% = 'active'),
     100 buckets may not capture the tail well.

     Fix: ALTER TABLE orders ALTER COLUMN status SET STATISTICS 1000;
     Then run ANALYZE.

  4. Expression statistics missing:
     WHERE lower(email) = 'alice@example.com'
     No statistics on lower(email) — optimizer uses default selectivity (0.5%).

     Fix (PG 14+): CREATE STATISTICS ON lower(email) FROM users;
     Fix (all PG versions): create a functional index:
       CREATE INDEX idx_email_lower ON users (lower(email));
       PostgreSQL will then collect stats on the index expression.

  5. CTEs as optimization fences (PG < 12):
     Pre-PG 12: CTEs are materialized, not inlined.
     WITH cte AS (SELECT * FROM orders WHERE ...) -- always materialized
     SELECT * FROM cte WHERE ...  -- filter cannot be pushed down

     PG 12+: CTEs are inlined by default (unless MATERIALIZED keyword used).

  6. Parameterized query plan mismatch:
     Prepared statements: PG decides after 5 executions whether to use
     a generic plan (same for all parameter values) or custom plan
     (re-planned per execution).

     Problem: generic plan is optimal for common values but terrible
     for rare values (or vice versa).

     Fix: plan_cache_mode = force_custom_plan (always re-plan)
     Trade-off: increases planning overhead.
```

### Forcing Hints and Overriding the Optimizer

```
WHEN THE OPTIMIZER IS WRONG — ESCAPE HATCHES:

  PostgreSQL has NO traditional query hints (by design).
  Instead, use indirect methods:

  1. Disable specific plan types:
     SET enable_seqscan = off;       -- force index usage
     SET enable_nestloop = off;      -- force hash/merge joins
     SET enable_hashjoin = off;      -- force nested loop/merge
     SET enable_mergejoin = off;
     SET enable_indexscan = off;
     SET enable_bitmapscan = off;

     CAUTION: these are session-level. Use SET LOCAL in a transaction
     to limit scope:
       BEGIN;
       SET LOCAL enable_seqscan = off;
       SELECT ...;
       COMMIT;  -- setting reverts

  2. Adjust cost parameters:
     SET random_page_cost = 1.1;    -- default 4.0, lower for SSD
     SET seq_page_cost = 1.0;       -- default 1.0
     SET effective_cache_size = 32GB; -- hint how much OS cache exists
     SET work_mem = '256MB';        -- allow larger in-memory sorts/hashes

  3. pg_hint_plan extension (third-party):
     /*+ IndexScan(orders orders_pkey) */ SELECT * FROM orders WHERE ...
     /*+ HashJoin(a b) */ SELECT * FROM a JOIN b ON ...
     Widely used in production. Not in core PostgreSQL.

  MySQL hints (native):
     SELECT /*+ INDEX(orders idx_status) */ * FROM orders WHERE ...
     SELECT /*+ NO_INDEX(orders idx_status) */ * FROM orders WHERE ...
     SELECT /*+ HASH_JOIN(a, b) */ * FROM a JOIN b ON ...
     SELECT /*+ SET_VAR(optimizer_switch='mrr=off') */ * FROM ...
     USE INDEX (idx_name), FORCE INDEX (idx_name), IGNORE INDEX (idx_name)
```

---

## 4. Performance Anti-Patterns

### The Deadly Dozen

```
ANTI-PATTERN 1: SELECT *

  Problem: fetches all columns, even ones you don't need.
  Impact:
    - More data read from disk (wider rows = more pages)
    - More data shipped over network
    - Prevents index-only scans (covering index can't cover SELECT *)
    - More memory for sort/hash operations

  SELECT * FROM orders WHERE user_id = 42;
    → reads all 20 columns, maybe 500 bytes per row
    → cannot use Index Only Scan even if index covers user_id

  SELECT order_id, status FROM orders WHERE user_id = 42;
    → reads 2 columns, maybe 30 bytes per row
    → can use Index Only Scan with (user_id, order_id, status) covering index

  Exception: acceptable for single-row lookups by PK when you need all columns.
```

```
ANTI-PATTERN 2: IMPLICIT TYPE CONVERSION

  Problem: mismatched types prevent index usage.

  -- Column "phone" is VARCHAR, but parameter is integer:
  SELECT * FROM users WHERE phone = 5551234;
    PostgreSQL: casts phone to integer for EACH row → seq scan
    MySQL: casts column to double for EACH row → no index

  -- Column "id" is BIGINT, but parameter is text:
  SELECT * FROM orders WHERE id = '42';
    PostgreSQL: casts '42' to bigint → index works (cast on constant)
    MySQL: depends on context, may disable index

  -- UUID as text:
  WHERE uuid_col = '550e8400-e29b-41d4-a716-446655440000'
    Works if uuid_col is UUID type and PostgreSQL casts the literal.
    Breaks if uuid_col is TEXT and you pass UUID type.

  Rule: ALWAYS match types exactly. Check with EXPLAIN if unsure.
  Red flag in EXPLAIN: "Filter:" instead of "Index Cond:" means
  the predicate was not pushed into the index.
```

```
ANTI-PATTERN 3: N+1 QUERY PATTERN

  Problem: fetch parent, then loop to fetch children one-by-one.

  -- The N+1 pattern (100 round trips):
  users = SELECT * FROM users LIMIT 100;
  for user in users:
      orders = SELECT * FROM orders WHERE user_id = user.id;

  -- Fixed with JOIN (1 round trip):
  SELECT u.*, o.*
  FROM users u
  JOIN orders o ON o.user_id = u.id
  LIMIT 100;

  -- Fixed with IN (2 round trips):
  users = SELECT * FROM users LIMIT 100;
  orders = SELECT * FROM orders WHERE user_id IN (1, 2, 3, ..., 100);

  -- ORM-level fix (eager loading):
  User.objects.prefetch_related('orders')[:100]  # Django
  User.includes(:orders).limit(100)              # Rails

  Performance difference at 100 rows, same-DC network:
    N+1: 100 × (0.5ms network + 0.1ms execute) = 60ms
    JOIN: 1 × (0.5ms network + 2ms execute) = 2.5ms
    → 24x faster
```

```
ANTI-PATTERN 4: FUNCTIONS ON INDEXED COLUMNS

  Problem: applying a function to a column prevents index usage.

  -- Index on created_at is USELESS here:
  SELECT * FROM orders WHERE YEAR(created_at) = 2024;
  SELECT * FROM orders WHERE DATE(created_at) = '2024-01-15';
  SELECT * FROM orders WHERE LOWER(email) = 'alice@example.com';
  SELECT * FROM orders WHERE amount + tax > 100;

  -- Fixed: apply function to the constant, not the column:
  SELECT * FROM orders
  WHERE created_at >= '2024-01-01' AND created_at < '2025-01-01';

  SELECT * FROM orders
  WHERE created_at >= '2024-01-15' AND created_at < '2024-01-16';

  -- If you MUST apply a function, use a functional index:
  CREATE INDEX idx_email_lower ON users (LOWER(email));
  -- Now this uses the index:
  SELECT * FROM users WHERE LOWER(email) = 'alice@example.com';
```

```
ANTI-PATTERN 5: OFFSET PAGINATION

  Problem: OFFSET N reads and discards N rows before returning results.

  SELECT * FROM orders ORDER BY created_at DESC LIMIT 20 OFFSET 10000;
    → DB must sort and skip 10,000 rows to return 20.
    → Page 500 of results requires reading 10,000 rows.
    → Gets progressively slower as OFFSET grows.

  ┌──────────────┬─────────────┐
  │ OFFSET        │ Time         │
  ├──────────────┼─────────────┤
  │ 0            │ 1 ms         │
  │ 1,000        │ 5 ms         │
  │ 10,000       │ 50 ms        │
  │ 100,000      │ 500 ms       │
  │ 1,000,000    │ 5,000 ms     │
  └──────────────┴─────────────┘

  Fix: keyset pagination (cursor-based):
  -- First page:
  SELECT * FROM orders ORDER BY created_at DESC, id DESC LIMIT 20;

  -- Next page (use last row's values as cursor):
  SELECT * FROM orders
  WHERE (created_at, id) < ('2024-03-15 10:30:00', 12345)
  ORDER BY created_at DESC, id DESC
  LIMIT 20;

  With an index on (created_at DESC, id DESC):
    → Every page costs the same: ~1ms regardless of position.
    → No rows skipped; index seek goes directly to the cursor.

  Trade-off: cannot jump to arbitrary page N. Only next/previous.
  For most UIs (infinite scroll, "Load More"), this is fine.
```

```
ANTI-PATTERN 6: LARGE IN LISTS

  Problem: WHERE id IN (1, 2, 3, ..., 50000) generates a huge plan
  and can overwhelm the optimizer.

  Symptoms:
    - Planning time > execution time
    - Memory spike during parsing
    - PG may choose seq scan because it estimates many rows

  Fix for PostgreSQL: use ANY with an array:
    SELECT * FROM orders WHERE id = ANY($1::int[]);
    -- Pass a single parameter as array, much faster to parse/plan

  Fix for any DB: use a temporary table or VALUES list:
    CREATE TEMP TABLE lookup_ids (id int);
    COPY lookup_ids FROM STDIN;  -- bulk insert the IDs
    SELECT o.* FROM orders o JOIN lookup_ids l ON o.id = l.id;

  Fix for MySQL:
    -- Break into chunks of ~1000 IDs
    -- Or use a temporary table with JOIN
```

```
ANTI-PATTERN 7: MISSING LIMIT ON UNBOUNDED QUERIES

  Problem: queries that can return millions of rows when data grows.

  -- Looks innocent in development (100 rows):
  SELECT * FROM events WHERE user_id = 42;

  -- In production (user has 5M events): returns 5M rows,
  -- saturates network, OOMs the app server.

  Fix: ALWAYS add LIMIT for user-facing queries.
  SELECT * FROM events WHERE user_id = 42
  ORDER BY created_at DESC LIMIT 100;

  For batch processing: use cursor-based iteration:
    DECLARE cur CURSOR FOR SELECT * FROM events WHERE user_id = 42;
    FETCH 1000 FROM cur;  -- process in chunks
```

```
ANTI-PATTERN 8: UNNECESSARY DISTINCT / ORDER BY

  -- DISTINCT when JOIN already produces unique rows:
  SELECT DISTINCT u.id, u.name
  FROM users u
  JOIN orders o ON o.user_id = u.id
  WHERE o.status = 'shipped';
  -- If you only want user info, use EXISTS instead:
  SELECT u.id, u.name FROM users u
  WHERE EXISTS (SELECT 1 FROM orders o
                WHERE o.user_id = u.id AND o.status = 'shipped');

  -- ORDER BY on a column that's already ordered by the index:
  -- If using Index Scan on (created_at), the output is already ordered.
  -- Adding ORDER BY created_at is harmless (optimizer knows), but
  -- ORDER BY created_at, id may force a re-sort if the index is only
  -- on (created_at).

  Cost of unnecessary DISTINCT:
    Usually requires a sort or hash of the entire result set.
    On 1M rows: ~200ms for the sort alone.
```

```
ANTI-PATTERN 9: NOT EXISTS vs. NOT IN WITH NULLs

  NOT IN with NULLs produces unexpected results:

  SELECT * FROM orders WHERE user_id NOT IN (SELECT id FROM deleted_users);

  If deleted_users.id has ANY NULL value:
    NOT IN returns NO ROWS. (SQL three-valued logic: x NOT IN (..., NULL)
    is UNKNOWN for every x, which is treated as FALSE.)

  Fix: use NOT EXISTS:
  SELECT * FROM orders o
  WHERE NOT EXISTS (
    SELECT 1 FROM deleted_users d WHERE d.id = o.user_id
  );
  -- Correct behavior AND usually same or better performance.
  -- NOT EXISTS can short-circuit; NOT IN must materialize the subquery.
```

```
ANTI-PATTERN 10: LOCKING FULL TABLES FOR SCHEMA CHANGES

  Problem: ALTER TABLE acquires AccessExclusiveLock in PostgreSQL,
  blocking ALL reads and writes.

  ALTER TABLE orders ADD COLUMN shipped_at TIMESTAMP;
  -- Blocks every query on 'orders' until ALTER completes.
  -- On a 500M row table, this can take minutes.

  Fix: use non-blocking DDL patterns:
  -- PG: ADD COLUMN with DEFAULT is instant since PG 11
  ALTER TABLE orders ADD COLUMN shipped_at TIMESTAMP DEFAULT NULL;
  -- Instant! Default stored in catalog, not backfilled.

  -- For other alterations, use CREATE INDEX CONCURRENTLY:
  CREATE INDEX CONCURRENTLY idx_orders_shipped ON orders (shipped_at);
  -- Does not block reads or writes (takes longer, uses more resources).

  -- For column type changes, use a multi-step migration:
  1. Add new column (instant)
  2. Backfill new column in batches (UPDATE ... WHERE id BETWEEN x AND y)
  3. Deploy app to read/write both columns
  4. Rename columns (instant, brief lock)
  5. Drop old column (instant)

  MySQL: ALTER TABLE is usually blocking in InnoDB.
    Use pt-online-schema-change (Percona) or gh-ost (GitHub)
    for non-blocking schema changes.
    MySQL 8.0: some ALGORITHM=INSTANT operations (add column at end).
```

```
ANTI-PATTERN 11: EXCESSIVE INDEXING

  Problem: too many indexes slow down writes and waste space.

  Every index:
    - Must be updated on every INSERT (log2(N) B-tree traversal + split)
    - Must be updated on every UPDATE to the indexed column
    - Must be updated on every DELETE (mark as dead, vacuum later)
    - Consumes disk space (often 2-5x the data size with many indexes)
    - Increases WAL volume (each index change generates WAL)
    - Slows vacuum (must clean each index)

  Symptoms of over-indexing:
    - INSERT latency growing as table grows
    - VACUUM taking hours
    - WAL generation rate much higher than expected
    - Disk usage dominated by indexes, not data

  Detection:
  -- Find unused indexes (PostgreSQL):
  SELECT schemaname, tablename, indexname,
         idx_scan AS times_used,
         pg_size_pretty(pg_relation_size(indexrelid)) AS size
  FROM pg_stat_user_indexes
  WHERE idx_scan = 0
    AND indexrelid NOT IN (
      SELECT conindid FROM pg_constraint
      WHERE contype IN ('p', 'u')  -- exclude PK and unique constraints
    )
  ORDER BY pg_relation_size(indexrelid) DESC;

  -- Find redundant indexes (index A is prefix of index B):
  -- Use pg_catalog queries or tools like pgstatindex, DBA-style scripts.
```

```
ANTI-PATTERN 12: WRONG TRANSACTION SCOPE

  Problem: holding transactions open too long.

  BEGIN;
  SELECT * FROM orders WHERE id = 42;   -- acquires snapshot
  -- ... app does HTTP call to payment service (2-30 seconds) ...
  UPDATE orders SET status = 'paid' WHERE id = 42;
  COMMIT;

  Consequences:
    - Row locks held for entire duration → blocks other writers
    - MVCC snapshot held → prevents dead tuple cleanup by VACUUM
    - Connection held → reduces pool capacity
    - In extreme cases: table bloat grows unbounded

  Fix: minimize transaction scope.
  -- Do the external call OUTSIDE the transaction:
  order = SELECT * FROM orders WHERE id = 42;  -- auto-commit
  payment_result = call_payment_api(order);     -- no DB transaction
  BEGIN;
  UPDATE orders SET status = 'paid' WHERE id = 42;
  INSERT INTO payments (order_id, ...) VALUES (42, ...);
  COMMIT;

  Fix: set statement_timeout and idle_in_transaction_session_timeout:
    SET idle_in_transaction_session_timeout = '30s';
    -- Kills any transaction idle for more than 30 seconds.
    SET statement_timeout = '30s';
    -- Kills any single statement running more than 30 seconds.
```

---

## 5. Advanced EXPLAIN Analysis

EXPLAIN basics are covered in `04-query-engine-internals.md` §9. This section covers advanced analysis patterns.

### Reading Execution Time Distribution

```
EXPLAIN ANALYZE TIME BREAKDOWN:

  Each node reports:
    actual time=X..Y  (X = time to first row, Y = time to last row)
    rows=N
    loops=L

  Total wall-clock time for a node = Y × L

  IMPORTANT: times are INCLUSIVE of child nodes.
  To get the time spent IN a specific node (exclusive):
    exclusive_time = node_time - sum(child_times)

  Example:
  Hash Join  (actual time=0.5..150.0 rows=10000 loops=1)
    -> Seq Scan on orders  (actual time=0.1..80.0 rows=100000 loops=1)
    -> Hash  (actual time=0.3..0.3 rows=500 loops=1)
       -> Seq Scan on users  (actual time=0.1..0.2 rows=500 loops=1)

  Time breakdown:
    Seq Scan on orders:    80.0ms (exclusive)
    Seq Scan on users:     0.2ms (exclusive)
    Hash build:            0.3 - 0.2 = 0.1ms (exclusive)
    Hash Join:             150.0 - 80.0 - 0.3 = 69.7ms (exclusive)
    TOTAL:                 150.0ms

  The Join itself took 69.7ms (probing hash for 100K rows).
  The orders scan took 80ms (dominant — consider index).
```

### Detecting Cardinality Estimation Errors

```
CARDINALITY ESTIMATION ERRORS:

  Compare estimated rows with actual rows in EXPLAIN ANALYZE.

  Index Scan on orders  (cost=0.43..8.45 rows=1 width=64)
                        (actual time=0.05..15.3 rows=15000 loops=1)
                                              ^^^^^^^^^^^
                        Estimated: 1 row. Actual: 15,000 rows.

  This is a 15,000x estimation error. The optimizer chose Index Scan
  thinking it would find 1 row. If it knew there were 15K rows,
  it might have chosen Seq Scan or Bitmap Index Scan instead.

  Impact of estimation errors:
  ┌──────────────────────┬──────────────────────────────────────────┐
  │ Error direction       │ Consequence                               │
  ├──────────────────────┼──────────────────────────────────────────┤
  │ Underestimate rows   │ Chooses nested loop when hash join is     │
  │ (estimate < actual)  │ better. Chooses index scan when seq scan  │
  │                      │ is better. Under-allocates work_mem.      │
  ├──────────────────────┼──────────────────────────────────────────┤
  │ Overestimate rows    │ Chooses seq scan when index scan is       │
  │ (estimate > actual)  │ better. Over-allocates sort memory.       │
  │                      │ May add unnecessary materializations.     │
  └──────────────────────┴──────────────────────────────────────────┘

  Fix estimation errors:
    1. ANALYZE the table (refresh statistics)
    2. Increase default_statistics_target for skewed columns
    3. Create extended statistics (multi-column correlations)
    4. Check for implicit type conversions in predicates
    5. Consider expression statistics (PG 14+)
```

### Spotting Spills to Disk

```
SPILL DETECTION IN EXPLAIN ANALYZE:

  When sort or hash operations exceed work_mem, they spill to temp files.
  Spills are 10-100x slower than in-memory operations.

  Sort (actual time=50.0..350.0 rows=1000000)
    Sort Key: created_at
    Sort Method: external merge  Disk: 128000kB
                 ^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^
                 SPILL!          128MB on disk

  Compare with:
  Sort (actual time=5.0..15.0 rows=1000000)
    Sort Key: created_at
    Sort Method: quicksort  Memory: 71452kB
                 ^^^^^^^^^  ^^^^^^^^^^^^^^^^
                 In-memory   71MB in RAM

  Hash Join with spill:
  Hash (actual time=200.0..200.0 rows=5000000)
    Buckets: 65536  Batches: 32  Memory Usage: 4096kB
                              ^^^^^^^^^^
                    Batches > 1 means SPILL to disk!
                    Ideally Batches = 1 (all in memory).

  Fix: increase work_mem.
    SET work_mem = '256MB';  -- per-operation, not global!

    CAUTION: work_mem is per-sort/hash OPERATION, not per-query.
    A query with 10 sort nodes each gets work_mem.
    With 100 concurrent connections: 100 × 10 × 256MB = 256GB
    Set it per-session for heavy queries, not globally:
      SET LOCAL work_mem = '256MB';  -- within a transaction only

  Global recommendation:
    work_mem = available_RAM / max_connections / 4
    Example: 64GB RAM, 100 connections: work_mem = 64GB / 100 / 4 ≈ 160MB
    Adjust downward if your queries are simple (fewer operations per query).
```

### JIT Compilation Analysis

```
JIT COMPILATION (PostgreSQL 11+):

  PostgreSQL can JIT-compile expressions, tuple deforming, and
  other CPU-intensive operations using LLVM.

  Enabled by default when query cost exceeds jit_above_cost (100000).

  EXPLAIN ANALYZE output:
  JIT:
    Functions: 12
    Options: Inlining true, Optimization true, Expressions true, Deforming true
    Timing: Generation 2.5ms, Inlining 15.0ms, Optimization 45.0ms,
            Emission 30.0ms, Total 92.5ms

  When JIT helps:
    - Queries processing millions of rows with complex expressions
    - Aggregate queries with many columns
    - Queries spending >50% time in expression evaluation

  When JIT hurts:
    - Short OLTP queries: 92ms JIT compilation for a 5ms query → 20x slower!
    - Queries that are I/O bound (JIT can't help with disk waits)

  Tuning:
    jit_above_cost = 100000       # cost threshold to enable JIT
    jit_inline_above_cost = 500000  # cost threshold for inlining
    jit_optimize_above_cost = 500000  # cost threshold for LLVM optimization

  If you see JIT timing > execution timing in your EXPLAIN ANALYZE:
    SET jit = off;  -- disable for this session
    Or raise jit_above_cost to exclude short queries.
```

---

## 6. Memory, Caching, and Spill Behavior

### Buffer Pool Sizing

```
BUFFER POOL SIZING:

  PostgreSQL: shared_buffers
  MySQL/InnoDB: innodb_buffer_pool_size

  The buffer pool is a page cache managed by the database engine.
  It sits BETWEEN the query engine and disk I/O.

  PostgreSQL shared_buffers sizing:
  ┌─────────────────────────┬───────────────────────────────────────┐
  │ Total RAM                │ Recommended shared_buffers             │
  ├─────────────────────────┼───────────────────────────────────────┤
  │ ≤ 1 GB                  │ 25% of RAM (256MB)                     │
  │ 2-32 GB                 │ 25% of RAM                             │
  │ 32-64 GB                │ 8-16 GB (diminishing returns beyond)  │
  │ > 64 GB                 │ 16-32 GB (rest used by OS page cache) │
  └─────────────────────────┴───────────────────────────────────────┘

  Why not give ALL RAM to shared_buffers?
    PostgreSQL relies on the OS page cache as a second-level cache.
    Data path: shared_buffers → OS page cache → disk.
    If shared_buffers consumes all RAM, the OS cache shrinks to nothing,
    and EVERY buffer miss goes to disk instead of OS cache.

  InnoDB innodb_buffer_pool_size:
    Set to 70-80% of total RAM (InnoDB does NOT rely on OS cache).
    128GB RAM → innodb_buffer_pool_size = 96-104GB.
    InnoDB manages its own cache entirely, bypassing OS cache for data.
    (O_DIRECT is used by default on Linux.)

  Monitoring buffer pool effectiveness:
  -- PostgreSQL:
  SELECT relname,
         heap_blks_read AS disk_reads,
         heap_blks_hit AS buffer_hits,
         round(heap_blks_hit::numeric /
               nullif(heap_blks_hit + heap_blks_read, 0) * 100, 2) AS hit_pct
  FROM pg_statio_user_tables
  ORDER BY heap_blks_read DESC
  LIMIT 20;
  -- Tables with low hit_pct are thrashing the cache.

  -- MySQL:
  SHOW ENGINE INNODB STATUS\G
  -- Look for "Buffer pool hit rate: XXXX / 1000"
  -- Target: > 999/1000 (99.9% hit rate)
```

### Working Set Analysis

```
WORKING SET: THE KEY TO CACHE PERFORMANCE

  The working set = the set of pages actively accessed by your workload.
  If working set fits in RAM → cache hit ratio ≈ 100% → fast.
  If working set exceeds RAM → cache thrashing → slow.

  ┌────────────────────────────────────────────────────────────┐
  │                                                             │
  │  Response     │                                             │
  │  time         │                           ╱                 │
  │               │                         ╱                   │
  │               │                       ╱  ← working set     │
  │               │                     ╱      exceeds RAM      │
  │               │                   ╱                         │
  │               │──────────────────╱                          │
  │               │  ← working set fits in RAM                  │
  │               └────────────────────────────── Data size     │
  │                                 ▲                           │
  │                            RAM size                         │
  └────────────────────────────────────────────────────────────┘

  Estimating working set size (PostgreSQL):

  -- Which tables are "hot" (most accessed)?
  SELECT relname,
         pg_size_pretty(pg_relation_size(relid)) AS table_size,
         seq_scan + idx_scan AS total_scans,
         n_tup_ins + n_tup_upd + n_tup_del AS total_writes
  FROM pg_stat_user_tables
  ORDER BY (seq_scan + idx_scan) DESC
  LIMIT 20;

  -- How much of each table is cached?
  -- Use pg_buffercache extension:
  CREATE EXTENSION pg_buffercache;
  SELECT c.relname,
         count(*) AS buffers,
         pg_size_pretty(count(*) * 8192) AS cached_size,
         pg_size_pretty(pg_relation_size(c.oid)) AS total_size,
         round(count(*)::numeric * 8192 /
               pg_relation_size(c.oid) * 100, 1) AS cached_pct
  FROM pg_buffercache b
  JOIN pg_class c ON b.relfilenode = pg_relation_filenode(c.oid)
  WHERE c.relkind = 'r'
  GROUP BY c.relname, c.oid
  ORDER BY count(*) DESC
  LIMIT 20;

  If your hottest table is 50GB but only 5GB is cached (10%):
    You are I/O bound on that table.
    Options:
      1. Add more RAM
      2. Partition the table (hot partition fits in RAM)
      3. Archive cold data to a separate table/store
      4. Add covering indexes to reduce pages accessed
```

### OS Page Cache Interaction

```
DOUBLE CACHING: DATABASE BUFFER POOL + OS PAGE CACHE

  PostgreSQL (buffered I/O):
  ┌──────────────────────────────────────────────────────────┐
  │ Query → shared_buffers → OS page cache → disk            │
  │                                                           │
  │ Read: check shared_buffers first.                         │
  │   Hit → return page from shared_buffers (fastest).        │
  │   Miss → read from kernel. Kernel checks OS page cache.   │
  │     OS hit → copy from OS cache to shared_buffers.        │
  │     OS miss → read from disk to OS cache to buffer pool.  │
  │                                                           │
  │ The same data may exist in BOTH caches simultaneously.    │
  │ This is intentional — PG's eviction policy (clock-sweep)  │
  │ is optimized for DB workloads, while the OS LRU is not.   │
  └──────────────────────────────────────────────────────────┘

  InnoDB (direct I/O via O_DIRECT):
  ┌──────────────────────────────────────────────────────────┐
  │ Query → innodb_buffer_pool → disk (bypasses OS cache)    │
  │                                                           │
  │ O_DIRECT tells the kernel: don't cache this file.         │
  │ All caching is done by InnoDB's own buffer pool.          │
  │ No double-caching waste.                                  │
  │                                                           │
  │ This is why InnoDB can safely use 70-80% of RAM for its   │
  │ buffer pool — there's no OS cache competing for memory.   │
  └──────────────────────────────────────────────────────────┘

  When to use huge pages:
    Both PostgreSQL and MySQL benefit from huge pages (2MB vs 4KB).
    Reduces TLB misses when buffer pool is large (>8GB).

    PostgreSQL:
      huge_pages = try  (postgresql.conf)
      vm.nr_hugepages = shared_buffers / 2MB + overhead (sysctl)

    InnoDB:
      innodb_use_native_aio = 1
      Huge pages via OS: vm.nr_hugepages in sysctl
```

---

## 7. Lock Contention and Concurrency

### Diagnosing Lock Waits

```
LOCK CONTENTION DIAGNOSIS (PostgreSQL):

  Step 1: Find blocked queries:
  SELECT
    blocked.pid AS blocked_pid,
    blocked.query AS blocked_query,
    blocked.wait_event_type,
    blocked.wait_event,
    age(now(), blocked.query_start) AS blocked_duration,
    blocking.pid AS blocking_pid,
    blocking.query AS blocking_query,
    age(now(), blocking.query_start) AS blocking_duration
  FROM pg_stat_activity blocked
  JOIN pg_locks bl ON bl.pid = blocked.pid AND NOT bl.granted
  JOIN pg_locks gl ON gl.database = bl.database
       AND gl.relation = bl.relation AND gl.granted
  JOIN pg_stat_activity blocking ON blocking.pid = gl.pid
  WHERE blocked.pid != blocking.pid;

  Step 2: Find the lock type:
  ┌──────────────────┬───────────┬────────────────────────────────────┐
  │ Lock Type         │ Conflicts │ Caused by                          │
  ├──────────────────┼───────────┼────────────────────────────────────┤
  │ AccessShare      │ AExcl     │ SELECT                              │
  │ RowShare         │ Excl,AExcl│ SELECT FOR UPDATE/SHARE             │
  │ RowExclusive     │ Share,    │ INSERT, UPDATE, DELETE               │
  │                  │ Excl,AExcl│                                     │
  │ ShareUpdateExcl  │ Share,    │ VACUUM, CREATE INDEX CONCURRENTLY   │
  │                  │ Excl,AExcl│ ALTER TABLE (some)                  │
  │ Share            │ RowExcl,  │ CREATE INDEX (non-concurrent)       │
  │                  │ Excl,AExcl│                                     │
  │ Exclusive        │ All but   │ REFRESH MATVIEW CONCURRENTLY        │
  │                  │ AccessShr │                                     │
  │ AccessExclusive  │ ALL       │ ALTER TABLE, DROP TABLE, VACUUM FULL│
  │                  │           │ LOCK TABLE, REINDEX                 │
  └──────────────────┴───────────┴────────────────────────────────────┘

  Step 3: Common patterns and fixes:

  Pattern: UPDATE hotspot (many txns updating same row)
    → Row-level lock contention
    Fix: redesign to reduce contention
      - Use advisory locks for application-level serialization
      - Batch updates (UPDATE ... WHERE id IN (...) instead of one-by-one)
      - Use SELECT FOR UPDATE SKIP LOCKED for queue patterns

  Pattern: DDL blocking all queries
    → AccessExclusiveLock from ALTER TABLE
    Fix: see Anti-Pattern 10 (non-blocking DDL)
      - Set lock_timeout before DDL:
        SET lock_timeout = '5s';
        ALTER TABLE ...;  -- fails fast instead of blocking forever

  Pattern: Long-running SELECT blocking VACUUM
    → MVCC snapshot prevents dead tuple cleanup
    Fix: set statement_timeout, cancel long queries
      - old_snapshot_threshold (PG 9.6+): auto-invalidate old snapshots
```

### Row-Level Lock Strategies

```
ROW-LEVEL LOCKING PATTERNS:

  1. SELECT FOR UPDATE (exclusive row lock):
     Prevents other transactions from updating or locking the row.
     Use for: update-after-read patterns (check-then-act).

     SELECT * FROM accounts WHERE id = 42 FOR UPDATE;
     -- check balance ...
     UPDATE accounts SET balance = balance - 100 WHERE id = 42;

  2. SELECT FOR SHARE (shared row lock):
     Allows other readers but blocks writers.
     Use for: ensuring referenced row doesn't change.

     SELECT * FROM users WHERE id = 42 FOR SHARE;
     -- user exists and won't be deleted during this transaction
     INSERT INTO orders (user_id, ...) VALUES (42, ...);

  3. SELECT FOR UPDATE SKIP LOCKED (queue pattern):
     Skip rows locked by other transactions.
     Use for: work queues, job processing.

     SELECT * FROM jobs
     WHERE status = 'pending'
     ORDER BY created_at
     LIMIT 1
     FOR UPDATE SKIP LOCKED;
     -- Returns next unlocked job. Multiple workers don't block.

  4. SELECT FOR UPDATE NOWAIT:
     Fail immediately if row is locked.
     Use for: try-lock patterns, avoiding deadlocks.

     SELECT * FROM accounts WHERE id = 42 FOR UPDATE NOWAIT;
     -- ERROR: could not obtain lock on row (if locked)

  5. Advisory locks (application-level):
     Named locks not tied to any row or table.
     Use for: external resource coordination, rate limiting.

     SELECT pg_advisory_lock(hashtext('process-payments'));
     -- do work ...
     SELECT pg_advisory_unlock(hashtext('process-payments'));

     Or try-lock variant:
     SELECT pg_try_advisory_lock(hashtext('process-payments'));
     -- returns true/false without blocking
```

### Deadlock Detection

```
DEADLOCK SCENARIOS AND PREVENTION:

  Classic deadlock:
    T1: UPDATE accounts SET ... WHERE id = 1;  (locks row 1)
    T2: UPDATE accounts SET ... WHERE id = 2;  (locks row 2)
    T1: UPDATE accounts SET ... WHERE id = 2;  (waits for T2)
    T2: UPDATE accounts SET ... WHERE id = 1;  (waits for T1)
    → DEADLOCK!

  PostgreSQL: detects deadlocks every deadlock_timeout (default 1s).
    Aborts one transaction (the one that's easier to roll back).
    Logs: "ERROR: deadlock detected"

  MySQL/InnoDB: detects immediately (waits-for graph check on every lock wait).
    Aborts the transaction with the least undo work.

  Prevention strategies:

  1. Lock ordering:
     Always acquire locks in a consistent order (e.g., by primary key).
     T1: lock row 1, then row 2
     T2: lock row 1, then row 2  (same order → no deadlock)

  2. Lock timeout:
     SET lock_timeout = '5s';
     If a lock can't be acquired in 5s, the statement fails.
     The app retries with backoff.

  3. Reduce transaction scope:
     Shorter transactions hold locks for less time.
     Less overlap between transactions → fewer deadlock opportunities.

  4. Use SKIP LOCKED for queue patterns:
     Workers pick different rows → no contention.

  Monitoring:
    -- PostgreSQL deadlock count:
    SELECT deadlocks FROM pg_stat_database WHERE datname = current_database();
    -- Alert if deadlocks/min > threshold (e.g., > 5/min is concerning).
```

---

## 8. VACUUM, Bloat, and Maintenance

### Why VACUUM Matters for Performance

```
VACUUM AND TABLE BLOAT:

  PostgreSQL's MVCC creates dead tuples (old row versions) on every
  UPDATE and DELETE. These dead tuples consume space and slow down scans.

  UPDATE users SET name = 'Bob' WHERE id = 42;

  Before:  Page [... (id=42, name='Alice', xmax=∞) ...]
  After:   Page [... (id=42, name='Alice', xmax=100)    ← dead tuple
                     (id=42, name='Bob', xmin=100) ...]  ← live tuple

  The dead tuple stays on the page until VACUUM removes it.

  Impact of bloat:
  ┌──────────────────────┬──────────────────────────────────────────┐
  │ Bloat level           │ Impact                                    │
  ├──────────────────────┼──────────────────────────────────────────┤
  │ < 10% dead tuples    │ Negligible. Normal operation.              │
  │ 10-30% dead tuples   │ Seq scans read 10-30% more pages.         │
  │                      │ Index scans may hit dead-tuple pages.      │
  │ 30-50% dead tuples   │ Significant I/O waste. Index bloat        │
  │                      │ causes additional random I/O.              │
  │ > 50% dead tuples    │ Table is 2x+ its actual size.             │
  │                      │ Performance severely degraded.              │
  │                      │ May need VACUUM FULL (rewrites table).     │
  └──────────────────────┴──────────────────────────────────────────┘

  Monitoring bloat:
  -- Dead tuple count:
  SELECT relname,
         n_live_tup,
         n_dead_tup,
         round(n_dead_tup::numeric / nullif(n_live_tup + n_dead_tup, 0) * 100, 2)
           AS dead_pct,
         last_vacuum,
         last_autovacuum
  FROM pg_stat_user_tables
  ORDER BY n_dead_tup DESC
  LIMIT 20;

  -- Estimated bloat (using pgstattuple extension):
  CREATE EXTENSION pgstattuple;
  SELECT * FROM pgstattuple('tablename');
  -- dead_tuple_percent: percentage of space occupied by dead tuples
```

### Autovacuum Tuning

```
AUTOVACUUM TUNING — THE MOST UNDER-TUNED SETTING:

  Autovacuum triggers when:
    dead_tuples > autovacuum_vacuum_threshold +
                  autovacuum_vacuum_scale_factor × n_live_tup

  Default: 50 + 0.2 × n_live_tup
    For 100M rows: triggers at 20,000,050 dead tuples (20% of table!)
    That's far too late for a large table.

  Recommended per-table settings for large/hot tables:
    ALTER TABLE hot_table SET (
      autovacuum_vacuum_threshold = 1000,
      autovacuum_vacuum_scale_factor = 0.01,      -- 1% instead of 20%
      autovacuum_analyze_threshold = 1000,
      autovacuum_analyze_scale_factor = 0.005,     -- 0.5% for stats refresh
      autovacuum_vacuum_cost_delay = 2,            -- ms, default 2 (PG 12+)
      autovacuum_vacuum_cost_limit = 1000          -- higher = faster vacuum
    );

  Global autovacuum settings:
  ┌──────────────────────────────────┬────────────────────────────────┐
  │ Parameter                         │ Recommendation                  │
  ├──────────────────────────────────┼────────────────────────────────┤
  │ autovacuum_max_workers           │ 3-5 (default 3). More workers  │
  │                                  │ = more tables vacuumed in      │
  │                                  │ parallel. Each uses I/O budget.│
  ├──────────────────────────────────┼────────────────────────────────┤
  │ autovacuum_vacuum_cost_delay     │ 2ms (PG 12+ default).          │
  │                                  │ Lower = faster vacuum.         │
  ├──────────────────────────────────┼────────────────────────────────┤
  │ autovacuum_vacuum_cost_limit     │ 200 (default). Increase to     │
  │                                  │ 1000-2000 on NVMe storage.     │
  │                                  │ Shared across all workers.     │
  ├──────────────────────────────────┼────────────────────────────────┤
  │ vacuum_cost_page_hit             │ 1 (default). Cost of vacuuming │
  │                                  │ a page in buffer cache.        │
  ├──────────────────────────────────┼────────────────────────────────┤
  │ vacuum_cost_page_miss            │ 2 (default). Cost of reading   │
  │                                  │ page from disk for vacuum.     │
  │                                  │ Lower to 2 on SSD (default).   │
  ├──────────────────────────────────┼────────────────────────────────┤
  │ vacuum_cost_page_dirty           │ 20 (default). Cost of dirtying │
  │                                  │ a page during vacuum.          │
  └──────────────────────────────────┴────────────────────────────────┘

  Autovacuum is NOT optional. Disabling it causes:
    1. Table bloat → degraded read performance
    2. Index bloat → degraded index performance
    3. Transaction ID wraparound → database STOPS accepting writes
       (the XID wraparound emergency — see below)
```

### Transaction ID Wraparound Prevention

```
XID WRAPAROUND — THE SILENT KILLER:

  PostgreSQL uses 32-bit transaction IDs (XIDs). With ~4 billion XIDs,
  a busy database can exhaust them. When XIDs wrap around, old
  transactions become "in the future" and their data becomes invisible.

  To prevent this, PostgreSQL must VACUUM every table at least once
  every 2 billion transactions to "freeze" old XIDs.

  If autovacuum can't keep up:
    At 200M transactions remaining: WARNING in logs
    At 40M transactions remaining: autovacuum runs in "anti-wraparound"
      mode (ignores cost limits, runs full speed)
    At 1M transactions remaining: DATABASE SHUTS DOWN
      "ERROR: database is not accepting commands to avoid wraparound"
      Can only be fixed by single-user mode vacuum.

  Monitoring:
  SELECT datname,
         age(datfrozenxid) AS xid_age,
         2147483647 - age(datfrozenxid) AS remaining
  FROM pg_database
  ORDER BY age(datfrozenxid) DESC;

  -- Alert when remaining < 500,000,000 (500M)

  Per-table:
  SELECT relname,
         age(relfrozenxid) AS xid_age,
         pg_size_pretty(pg_relation_size(oid)) AS size
  FROM pg_class
  WHERE relkind = 'r'
  ORDER BY age(relfrozenxid) DESC
  LIMIT 20;

  Prevention:
    - Ensure autovacuum is running and keeping up
    - Monitor XID age in your alerting system
    - vacuum_freeze_min_age (default 50M): rows older than this get frozen
    - vacuum_freeze_table_age (default 150M): trigger aggressive freeze
    - autovacuum_freeze_max_age (default 200M): force vacuum for XID safety
```

---

## 9. Monitoring and Alerting

### The Essential Metrics

```
DATABASE MONITORING — THE FOUR GOLDEN SIGNALS:

  1. LATENCY: query execution time distribution
     ┌─────────────────────────┬─────────────────────────────────────┐
     │ Metric                   │ How to collect                       │
     ├─────────────────────────┼─────────────────────────────────────┤
     │ p50/p95/p99 query time  │ pg_stat_statements, APM, slow log   │
     │ Planning time            │ pg_stat_statements (PG 13+)         │
     │ I/O wait time            │ blk_read_time (track_io_timing=on) │
     │ Lock wait time           │ pg_stat_activity wait_event         │
     └─────────────────────────┴─────────────────────────────────────┘

  2. THROUGHPUT: queries per second, rows processed per second
     ┌─────────────────────────┬─────────────────────────────────────┐
     │ Metric                   │ How to collect                       │
     ├─────────────────────────┼─────────────────────────────────────┤
     │ QPS (queries/sec)       │ pg_stat_database.xact_commit + rollback│
     │ TPS (transactions/sec)  │ pg_stat_database.xact_commit        │
     │ Rows returned/sec       │ pg_stat_database.tup_returned       │
     │ Rows inserted/sec       │ pg_stat_database.tup_inserted       │
     │ Rows updated/sec        │ pg_stat_database.tup_updated        │
     │ Rows deleted/sec        │ pg_stat_database.tup_deleted        │
     └─────────────────────────┴─────────────────────────────────────┘

  3. ERRORS: failed queries, connection refused, deadlocks
     ┌─────────────────────────┬─────────────────────────────────────┐
     │ Metric                   │ How to collect                       │
     ├─────────────────────────┼─────────────────────────────────────┤
     │ Deadlocks               │ pg_stat_database.deadlocks           │
     │ Conflicts               │ pg_stat_database.conflicts           │
     │ Rollbacks               │ pg_stat_database.xact_rollback       │
     │ Cancelled queries       │ pg_stat_activity.state = 'idle'     │
     │ Connection errors       │ Connection pooler logs               │
     └─────────────────────────┴─────────────────────────────────────┘

  4. SATURATION: resource utilization approaching limits
     ┌─────────────────────────┬─────────────────────────────────────┐
     │ Metric                   │ How to collect                       │
     ├─────────────────────────┼─────────────────────────────────────┤
     │ Connection usage %      │ numbackends / max_connections        │
     │ Buffer pool hit ratio   │ blks_hit / (blks_hit + blks_read)  │
     │ Disk I/O utilization    │ iostat, node_exporter                │
     │ CPU utilization         │ top, node_exporter                   │
     │ WAL generation rate     │ pg_stat_wal.wal_bytes                │
     │ Replication lag         │ pg_stat_replication.replay_lag       │
     │ XID age                 │ age(datfrozenxid)                    │
     │ Table bloat             │ n_dead_tup / n_live_tup              │
     └─────────────────────────┴─────────────────────────────────────┘
```

### Alert Thresholds

```
RECOMMENDED ALERT THRESHOLDS:

  ┌─────────────────────────────┬──────────┬──────────┬───────────────┐
  │ Metric                       │ Warning  │ Critical │ Notes          │
  ├─────────────────────────────┼──────────┼──────────┼───────────────┤
  │ Query p99 latency           │ > 500ms  │ > 2s     │ Per-endpoint   │
  │ Active connections %        │ > 70%    │ > 90%    │ Of max_conn    │
  │ Buffer pool hit ratio       │ < 99%    │ < 95%    │ OLTP workload  │
  │ Replication lag (seconds)   │ > 5s     │ > 30s    │ Streaming repl │
  │ Replication lag (bytes)     │ > 100MB  │ > 1GB    │ For archiving  │
  │ Deadlocks per minute        │ > 1      │ > 10     │ Investigate >1 │
  │ Long-running queries        │ > 5 min  │ > 30 min │ Active queries │
  │ Idle-in-transaction         │ > 1 min  │ > 5 min  │ Snapshot hold  │
  │ XID age                     │ > 500M   │ > 1B     │ Wraparound risk│
  │ Dead tuples (large tables)  │ > 10%    │ > 30%    │ Per-table      │
  │ Disk space free %           │ < 20%    │ < 10%    │ Data + WAL dirs│
  │ WAL disk space free         │ < 30%    │ < 15%    │ Separate WAL   │
  │ Checkpoints requested       │ > timed  │ >> timed │ max_wal_size   │
  │ Temp file usage (per query) │ > 100MB  │ > 1GB    │ Spill to disk  │
  │ Table sequential scans      │ trending │ dominant │ Missing index? │
  │ Locks waiting > 5s          │ any      │ > 10     │ Contention     │
  └─────────────────────────────┴──────────┴──────────┴───────────────┘
```

### Monitoring Stack Architecture

```
PRODUCTION MONITORING STACK:

  ┌──────────────────────────────────────────────────────────┐
  │                    Grafana Dashboard                       │
  │  ┌──────────┐  ┌───────────────┐  ┌──────────────────┐  │
  │  │ QPS /    │  │ Latency p50/  │  │ Connection pool  │  │
  │  │ TPS      │  │ p95/p99       │  │ usage            │  │
  │  └──────────┘  └───────────────┘  └──────────────────┘  │
  │  ┌──────────┐  ┌───────────────┐  ┌──────────────────┐  │
  │  │ Cache    │  │ Replication   │  │ Disk I/O         │  │
  │  │ hit %    │  │ lag           │  │ utilization      │  │
  │  └──────────┘  └───────────────┘  └──────────────────┘  │
  └────────────────────────┬─────────────────────────────────┘
                           │
                    ┌──────┴───────┐
                    │  Prometheus  │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────────┐
              │            │                │
    ┌─────────┴──────┐ ┌──┴──────────┐ ┌───┴──────────────┐
    │ postgres_       │ │ node_        │ │ pgbouncer_        │
    │ exporter        │ │ exporter     │ │ exporter          │
    │                 │ │              │ │                    │
    │ pg_stat_*       │ │ CPU, RAM,    │ │ Pool stats,       │
    │ views           │ │ disk, net    │ │ wait times        │
    └─────────────────┘ └──────────────┘ └──────────────────┘
           │                   │                  │
           ▼                   ▼                  ▼
    ┌──────────────┐  ┌───────────────┐  ┌──────────────────┐
    │  PostgreSQL  │  │    Linux OS   │  │   PgBouncer      │
    └──────────────┘  └───────────────┘  └──────────────────┘

  Essential exporters:
    postgres_exporter: pg_stat_* metrics → Prometheus
    node_exporter: OS-level metrics (CPU, RAM, disk, network)
    pgbouncer_exporter: connection pool metrics

  Key Grafana dashboards:
    - PostgreSQL Overview (QPS, latency, connections, cache)
    - PostgreSQL Table Statistics (per-table scans, bloat, vacuum)
    - PostgreSQL Replication (lag, WAL generation, slot status)
    - PostgreSQL Locks (waiting queries, lock types, deadlocks)
    - Query Performance (top queries by time, I/O, calls)

  For MySQL:
    mysqld_exporter: performance_schema + sys schema metrics
    PMM (Percona Monitoring and Management): all-in-one solution
```

### Useful Diagnostic Queries

```
DIAGNOSTIC QUERY COOKBOOK:

  -- Current activity (what's running right now):
  SELECT pid, age(now(), query_start) AS duration,
         state, wait_event_type, wait_event,
         left(query, 100) AS query
  FROM pg_stat_activity
  WHERE state != 'idle'
    AND pid != pg_backend_pid()
  ORDER BY query_start;

  -- Long-running queries (potential problems):
  SELECT pid, age(now(), query_start) AS duration,
         usename, client_addr, left(query, 200)
  FROM pg_stat_activity
  WHERE state = 'active'
    AND query_start < now() - interval '5 minutes'
  ORDER BY query_start;

  -- Idle-in-transaction (snapshot holders, block vacuum):
  SELECT pid, age(now(), state_change) AS idle_duration,
         usename, left(query, 200)
  FROM pg_stat_activity
  WHERE state = 'idle in transaction'
    AND state_change < now() - interval '1 minute';

  -- Table I/O statistics (which tables cause most I/O):
  SELECT relname,
         seq_scan, seq_tup_read,
         idx_scan, idx_tup_fetch,
         n_tup_ins, n_tup_upd, n_tup_del,
         n_live_tup, n_dead_tup,
         pg_size_pretty(pg_total_relation_size(relid)) AS total_size
  FROM pg_stat_user_tables
  ORDER BY (seq_tup_read + idx_tup_fetch) DESC
  LIMIT 20;

  -- Index usage (are all indexes being used?):
  SELECT relname AS table,
         indexrelname AS index,
         idx_scan AS scans,
         pg_size_pretty(pg_relation_size(indexrelid)) AS index_size,
         CASE WHEN idx_scan = 0 THEN 'UNUSED' ELSE 'used' END AS status
  FROM pg_stat_user_indexes
  ORDER BY idx_scan ASC, pg_relation_size(indexrelid) DESC;

  -- Seq scans on large tables (missing index?):
  SELECT relname,
         seq_scan,
         seq_tup_read,
         pg_size_pretty(pg_relation_size(relid)) AS size,
         seq_tup_read / nullif(seq_scan, 0) AS avg_rows_per_scan
  FROM pg_stat_user_tables
  WHERE seq_scan > 0
    AND pg_relation_size(relid) > 10 * 1024 * 1024  -- > 10MB
  ORDER BY seq_tup_read DESC
  LIMIT 20;

  -- Checkpoint frequency (are checkpoints too frequent?):
  SELECT checkpoints_timed,
         checkpoints_req,
         round(checkpoints_req::numeric /
               nullif(checkpoints_timed + checkpoints_req, 0) * 100, 1)
           AS forced_pct,
         pg_size_pretty(buffers_checkpoint * 8192) AS data_written
  FROM pg_stat_bgwriter;
  -- If forced_pct > 20%: increase max_wal_size

  -- Replication status:
  SELECT client_addr,
         state,
         sent_lsn, write_lsn, flush_lsn, replay_lsn,
         pg_wal_lsn_diff(sent_lsn, replay_lsn) AS replay_lag_bytes,
         write_lag, flush_lag, replay_lag
  FROM pg_stat_replication;
```

---

## 10. Capacity Planning and Benchmarking

### Sizing for Throughput

```
THROUGHPUT CAPACITY MODEL:

  Max QPS depends on:
    CPU cores × queries_per_core_per_second

  For pure buffer-hit OLTP (index lookup, 1 row):
    ~5,000-20,000 QPS per CPU core (depending on query complexity)
    8-core server: ~40K-160K QPS ceiling

  For queries with disk I/O:
    Limited by IOPS:
    NVMe SSD: 500K-1M random IOPS
    SATA SSD: 50K-100K random IOPS
    HDD: 100-200 random IOPS

    If query needs 5 random I/Os on average:
    NVMe: 500K / 5 = 100K QPS
    SATA: 50K / 5 = 10K QPS
    HDD: 200 / 5 = 40 QPS (this is why HDDs can't do OLTP)

  For write-heavy workloads:
    Limited by WAL fsync latency:
    Without group commit: 1 / fsync_latency
      NVMe: ~30K TPS, HDD: ~200 TPS
    With group commit (10 txns/group):
      NVMe: ~300K TPS, HDD: ~2K TPS

  Connection count:
    max_connections should NOT be set to thousands.
    Rule of thumb: max_connections = 2-4 × CPU cores + disk_concurrency
    Use connection pooling (PgBouncer, ProxySQL) for thousands of app connections.

    Example: 16-core server, NVMe:
      max_connections = 100-200
      PgBouncer pool_size = 50-100
      PgBouncer max_client_conn = 10000
```

### Sizing for Storage

```
STORAGE CAPACITY PLANNING:

  Data growth estimation:
    Measure current table sizes:
    SELECT relname,
           pg_size_pretty(pg_total_relation_size(oid)) AS total,
           pg_size_pretty(pg_relation_size(oid)) AS data,
           pg_size_pretty(pg_indexes_size(oid)) AS indexes,
           pg_size_pretty(pg_total_relation_size(oid) -
                          pg_relation_size(oid) -
                          pg_indexes_size(oid)) AS toast
    FROM pg_class
    WHERE relkind = 'r'
    ORDER BY pg_total_relation_size(oid) DESC
    LIMIT 20;

  Storage overhead factors:
  ┌──────────────────┬──────────────────────────────────────────────┐
  │ Component         │ Overhead                                      │
  ├──────────────────┼──────────────────────────────────────────────┤
  │ Row overhead     │ 23 bytes/row header (PostgreSQL HeapTuple)   │
  │ Page overhead    │ 24 bytes/page header + item pointers          │
  │ Alignment padding│ Rows padded to MAXALIGN (8 bytes typically)  │
  │ Fill factor      │ Default 100% for read-heavy, 70-90% for     │
  │                  │ update-heavy (leaves room for HOT updates)    │
  │ Dead tuples      │ 5-20% in steady state with good autovacuum   │
  │ Indexes          │ Often 1-3x the data size                      │
  │ TOAST            │ Large values stored out-of-line               │
  │ WAL              │ 2-10x the data change volume                  │
  │ Temp files       │ work_mem spills, sort/hash operations         │
  └──────────────────┴──────────────────────────────────────────────┘

  Rule of thumb for PostgreSQL:
    Disk needed = data_size × 2.5 (for indexes, bloat, WAL, temp)
    + WAL retention: wal_generation_rate × retention_period
    + Backup space: at least 1 full backup + WAL archives

  Growth projection:
    If adding 1M rows/month, each row = 200 bytes average:
    Data growth = 200MB/month
    With indexes (2x): 600MB/month
    With overhead: ~800MB/month
    Plan for 2 years: ~20GB
    Plus WAL: depends on update/delete rate
```

### Load Testing Methodology

```
LOAD TESTING CHECKLIST:

  1. DEFINE WORKLOAD MIX:
     Real OLTP workloads are a MIX of query types:
     ┌──────────────────┬───────────┐
     │ Query type        │ % of mix  │
     ├──────────────────┼───────────┤
     │ Point lookup (PK)│ 40%       │
     │ Range scan       │ 20%       │
     │ INSERT           │ 15%       │
     │ UPDATE           │ 15%       │
     │ Complex JOIN     │ 8%        │
     │ Aggregation      │ 2%        │
     └──────────────────┴───────────┘
     Use pgbench custom scripts or sysbench Lua scripts.

  2. DATA DISTRIBUTION:
     Use Zipfian distribution for key access (not uniform).
     Real workloads: 10% of rows get 90% of accesses.
     pgbench: use --random-seed and custom probability distributions.

  3. WARMUP:
     Run for 5-10 minutes before measuring.
     Buffer pool and OS cache need to reach steady state.

  4. RAMP-UP:
     Gradually increase concurrency:
     1 → 4 → 16 → 32 → 64 → 128 → 256 connections
     At each level, measure latency AND throughput.
     Find the "knee" where latency starts climbing.

     Typical pattern:
     ┌────────────────────────────────────────────────────┐
     │                                                     │
     │  Latency  │                              ╱          │
     │           │                            ╱            │
     │           │                          ╱  ← knee      │
     │           │                        ╱                │
     │           │────────────────────────╱                 │
     │           │  ← linear region                        │
     │           └──────────────────────────── Concurrency │
     │                                    ▲                │
     │                           Optimal connection count  │
     └────────────────────────────────────────────────────┘

  5. STEADY STATE:
     Run at target concurrency for at least:
     - 2× checkpoint interval (to capture checkpoint I/O spikes)
     - Long enough for autovacuum to run
     - Long enough for JIT compilation to amortize

  6. RECORD EVERYTHING:
     - Client-side latency percentiles (p50, p95, p99, p99.9)
     - Server-side QPS and TPS
     - CPU, RAM, disk I/O (IOPS + throughput + latency)
     - WAL generation rate
     - Buffer pool hit ratio
     - Lock wait events
     - Error count (deadlocks, timeouts, connection refused)
```

---

## 11. Performance Debugging Workflows

### The Systematic Approach

```
PERFORMANCE DEBUGGING FLOWCHART:

  "Query X is slow"
         │
         ▼
  ┌──────────────────┐
  │ Is it ALWAYS slow │──── NO ──► Intermittent slowness
  │ or SOMETIMES?    │            (see Intermittent section)
  └──────┬───────────┘
         │ ALWAYS
         ▼
  ┌──────────────────┐
  │ Run EXPLAIN      │
  │ ANALYZE          │
  └──────┬───────────┘
         │
         ▼
  ┌──────────────────┐
  │ Compare estimated │──── Large gap ──► Statistics problem
  │ vs. actual rows  │                    Run ANALYZE, check §3
  └──────┬───────────┘
         │ Estimates OK
         ▼
  ┌──────────────────┐
  │ Is it I/O bound? │──── YES ──► Check buffer pool hit ratio
  │ (blk_read_time   │            Add index, increase shared_buffers,
  │  dominates?)     │            add RAM, or optimize access pattern
  └──────┬───────────┘
         │ NO (CPU bound)
         ▼
  ┌──────────────────┐
  │ Scan too many    │──── YES ──► Add WHERE clause, better index,
  │ rows?            │            covering index, or LIMIT
  └──────┬───────────┘
         │ NO
         ▼
  ┌──────────────────┐
  │ Sort/Hash spill? │──── YES ──► Increase work_mem for this query
  │ (Disk: NNNkB)    │
  └──────┬───────────┘
         │ NO
         ▼
  ┌──────────────────┐
  │ Wrong join type? │──── YES ──► Investigate join order,
  │ (NL when Hash    │            check cardinality estimates,
  │  would be better)│            consider hints or enable_*
  └──────┬───────────┘
         │ NO
         ▼
  ┌──────────────────┐
  │ Lock waits?      │──── YES ──► See Lock Contention §7
  │ (wait_event =    │
  │  Lock, LWLock)   │
  └──────┬───────────┘
         │ NO
         ▼
  ┌──────────────────┐
  │ Network overhead?│──── YES ──► Reduce result set size,
  │ (large result,   │            use pagination, compress
  │  many round-trips)│
  └──────────────────┘
```

### Intermittent Slowness

```
DIAGNOSING INTERMITTENT PERFORMANCE PROBLEMS:

  Intermittent slowness is harder than consistent slowness.
  The query is fast 95% of the time but slow 5% of the time.

  Common causes:

  1. CHECKPOINT I/O SPIKE:
     Symptom: periodic slowness every checkpoint_timeout.
     Check: pg_stat_bgwriter.checkpoints_req increasing.
     Verify: correlate slow queries with checkpoint times in logs.
     Fix: increase checkpoint_completion_target (spread I/O),
          increase max_wal_size (less frequent checkpoints),
          use NVMe (lower I/O latency).

  2. AUTOVACUUM CONTENTION:
     Symptom: slowness on specific tables during vacuum.
     Check: pg_stat_progress_vacuum shows active vacuum.
     Verify: pg_stat_activity shows vacuum process running.
     Fix: tune autovacuum_vacuum_cost_delay and cost_limit,
          schedule manual vacuum during low-traffic windows.

  3. BUFFER POOL CHURN:
     Symptom: a batch job or report evicts hot pages from cache.
     Check: blks_read spikes after the batch job.
     Verify: buffer pool hit ratio drops during the batch.
     Fix: use a separate connection with small effective_cache_size,
          or run batch on a read replica.

  4. LOCK ESCALATION:
     Symptom: queries wait intermittently, then proceed.
     Check: pg_stat_activity.wait_event = 'Lock' or 'transactionid'.
     Fix: reduce transaction scope, use SKIP LOCKED, add lock_timeout.

  5. OS-LEVEL EVENTS:
     Symptom: all queries slow simultaneously.
     Check: dmesg, syslog for OOM killer, disk errors, NIC errors.
     Verify: vmstat shows swapping (si/so > 0), iostat shows high await.
     Fix: add RAM (stop swapping), fix disk issues, tune vm.swappiness=1.

  6. PLAN FLIP:
     Symptom: query suddenly gets a different (worse) plan.
     Check: auto_explain logs (if enabled) show different plans.
     Verify: ANALYZE was run, statistics changed, plan flipped.
     Fix: CREATE STATISTICS for correlated columns,
          pg_hint_plan to stabilize the plan,
          or SET plan_cache_mode = force_custom_plan.

  7. REPLICATION CONFLICT (on replicas):
     Symptom: queries cancelled on replica with "conflict with recovery".
     Check: pg_stat_database_conflicts.
     Fix: increase max_standby_streaming_delay,
          set hot_standby_feedback = on (but may bloat primary).
```

### The Performance Regression Checklist

```
AFTER A DEPLOYMENT: PERFORMANCE REGRESSION CHECKLIST

  □ Compare pg_stat_statements before/after deployment:
    - Are any queries significantly slower (mean_exec_time)?
    - Are any queries called much more often (calls)?
    - Are there new queries not seen before?

  □ Check EXPLAIN ANALYZE for affected queries:
    - Has the plan changed?
    - Are row estimates still accurate?

  □ Check for missing indexes:
    - New columns added without indexes?
    - New query patterns not covered by existing indexes?

  □ Check for table bloat:
    - Migration caused mass UPDATE → dead tuples → bloat?
    - Run VACUUM ANALYZE on affected tables.

  □ Check connection usage:
    - New feature opening more connections?
    - Connection pool exhaustion?

  □ Check for N+1 queries:
    - New ORM code with lazy loading?
    - Check APM for increased query count per request.

  □ Check lock contention:
    - New schema changes blocking queries?
    - New write patterns causing row lock hotspots?

  Baseline capture (do this BEFORE every deployment):
    pg_stat_statements_reset();  -- reset counters
    -- deploy --
    -- wait for traffic --
    SELECT * FROM pg_stat_statements ORDER BY total_exec_time DESC;
    -- compare with pre-deployment snapshot
```

---

## 12. System-Specific Tuning Reference

### PostgreSQL Quick Reference

```
POSTGRESQL PERFORMANCE PARAMETERS (Quick Reference):

  MEMORY:
  ┌──────────────────────────┬────────────────────────────────────────┐
  │ Parameter                 │ Setting                                 │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ shared_buffers           │ 25% of RAM (max ~16GB, rest for OS)    │
  │ effective_cache_size     │ 75% of RAM (hint for planner only)     │
  │ work_mem                 │ RAM / max_connections / 4               │
  │ maintenance_work_mem     │ 1-2 GB (for VACUUM, CREATE INDEX)      │
  │ huge_pages               │ try (reduces TLB misses)               │
  └──────────────────────────┴────────────────────────────────────────┘

  WAL / DURABILITY:
  ┌──────────────────────────┬────────────────────────────────────────┐
  │ wal_buffers              │ -1 (auto) or 64MB                      │
  │ wal_compression          │ lz4 (PG 15+)                           │
  │ max_wal_size             │ 4-16 GB                                 │
  │ checkpoint_timeout       │ 10-30 min                               │
  │ checkpoint_completion    │ 0.9                                     │
  │ synchronous_commit       │ on (safe) or off (fast, <200ms risk)   │
  └──────────────────────────┴────────────────────────────────────────┘

  QUERY PLANNER:
  ┌──────────────────────────┬────────────────────────────────────────┐
  │ random_page_cost         │ 1.1 (SSD) or 4.0 (HDD, default)       │
  │ effective_io_concurrency │ 200 (SSD) or 2 (HDD)                   │
  │ default_statistics_target│ 100-500 (higher for skewed data)       │
  │ jit                      │ on (default) or off for OLTP            │
  └──────────────────────────┴────────────────────────────────────────┘

  CONNECTIONS:
  ┌──────────────────────────┬────────────────────────────────────────┐
  │ max_connections          │ 100-300 (use pooler for more)           │
  │ idle_in_transaction_     │ 30s-60s (kill idle transactions)       │
  │   session_timeout        │                                         │
  │ statement_timeout        │ 30s-120s (prevent runaway queries)      │
  │ lock_timeout             │ 5s-30s (fail fast on lock waits)        │
  └──────────────────────────┴────────────────────────────────────────┘

  AUTOVACUUM:
  ┌──────────────────────────┬────────────────────────────────────────┐
  │ autovacuum               │ on (NEVER disable)                      │
  │ autovacuum_max_workers   │ 3-5                                     │
  │ autovacuum_vacuum_cost   │ 2ms                                     │
  │   _delay                 │                                         │
  │ autovacuum_vacuum_cost   │ 1000-2000 (NVMe)                       │
  │   _limit                 │                                         │
  └──────────────────────────┴────────────────────────────────────────┘

  LOGGING / DIAGNOSTICS:
  ┌──────────────────────────┬────────────────────────────────────────┐
  │ track_io_timing          │ on (essential for I/O diagnosis)        │
  │ track_functions          │ all (track function call stats)         │
  │ log_min_duration         │ 100ms-1s (log slow queries)             │
  │   _statement             │                                         │
  │ log_lock_waits           │ on (log lock waits > deadlock_timeout)  │
  │ log_temp_files           │ 0 (log all temp file usage)             │
  │ log_checkpoints          │ on                                      │
  │ log_autovacuum_min       │ 0 (log all autovacuum runs)             │
  │   _duration              │                                         │
  │ auto_explain.log_min     │ 100ms (auto-explain slow queries)       │
  │   _duration              │ (requires: shared_preload_libraries     │
  │                          │  = 'auto_explain')                      │
  └──────────────────────────┴────────────────────────────────────────┘
```

### MySQL/InnoDB Quick Reference

```
MYSQL/INNODB PERFORMANCE PARAMETERS:

  MEMORY:
  ┌──────────────────────────────┬────────────────────────────────────┐
  │ innodb_buffer_pool_size      │ 70-80% of RAM                      │
  │ innodb_buffer_pool_instances │ 8 (for buffer_pool > 1GB)          │
  │ innodb_log_buffer_size       │ 64-256 MB                          │
  │ sort_buffer_size             │ 256KB-2MB (per-connection)          │
  │ join_buffer_size             │ 256KB-1MB (per-join, per-conn)     │
  │ tmp_table_size               │ 64-256MB                           │
  │ max_heap_table_size          │ Same as tmp_table_size              │
  └──────────────────────────────┴────────────────────────────────────┘

  WAL (REDO LOG):
  ┌──────────────────────────────┬────────────────────────────────────┐
  │ innodb_redo_log_capacity     │ 1-4 GB (8.0.30+)                   │
  │ innodb_flush_log_at_trx_     │ 1 (safe) or 2 (fast, OS crash risk)│
  │   commit                     │                                     │
  │ innodb_flush_method          │ O_DIRECT (default on Linux)         │
  │ sync_binlog                  │ 1 (safe) or 0 (fast)               │
  └──────────────────────────────┴────────────────────────────────────┘

  CONCURRENCY:
  ┌──────────────────────────────┬────────────────────────────────────┐
  │ innodb_thread_concurrency    │ 0 (auto) or 2 × CPU cores          │
  │ max_connections              │ 151 (default), use pooler for more │
  │ innodb_lock_wait_timeout     │ 50s (default), lower for OLTP      │
  └──────────────────────────────┴────────────────────────────────────┘

  QUERY:
  ┌──────────────────────────────┬────────────────────────────────────┐
  │ optimizer_switch              │ Tune specific optimizer features   │
  │ long_query_time              │ 0.1 (100ms, log slow queries)      │
  │ slow_query_log               │ ON                                  │
  │ log_queries_not_using_indexes│ ON                                  │
  └──────────────────────────────┴────────────────────────────────────┘
```

---

## Summary: Performance Engineering Principles

```
THE TEN COMMANDMENTS OF DATABASE PERFORMANCE:

  1. MEASURE BEFORE OPTIMIZING.
     Never guess. Use EXPLAIN ANALYZE, pg_stat_statements,
     and application-side timing. Optimize the bottleneck, not
     what you think the bottleneck is.

  2. KEEP THE WORKING SET IN RAM.
     The biggest performance jump is RAM vs. disk.
     If your hot data fits in memory, everything is fast.

  3. USE INDEXES WISELY.
     Not too few (slow reads), not too many (slow writes).
     Every index is a trade-off. Monitor unused indexes.

  4. BATCH, DON'T LOOP.
     N+1 queries are the #1 application-level performance killer.
     Fetch related data in bulk.

  5. KEEP TRANSACTIONS SHORT.
     Long transactions hold locks, block vacuum, hold snapshots,
     and waste connections.

  6. MONITOR CONTINUOUSLY.
     Performance regressions happen gradually. Catch them with
     dashboards and alerts, not user complaints.

  7. TUNE AUTOVACUUM.
     Default autovacuum settings are too conservative for large tables.
     Bloat kills performance. XID wraparound kills availability.

  8. USE PREPARED STATEMENTS.
     Skip re-parsing and re-planning for repeated queries.
     Reduces CPU overhead by 10-30% for simple OLTP.

  9. SIZE YOUR CONNECTION POOL.
     connections = CPU_cores × 2-4. Not hundreds. Not thousands.
     Use PgBouncer/ProxySQL for connection multiplexing.

  10. PLAN FOR GROWTH.
      What works at 1M rows may break at 100M rows.
      Test with production-scale data. Partition before it's urgent.
```