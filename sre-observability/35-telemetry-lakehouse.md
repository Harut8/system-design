# 35 — Telemetry Lakehouse

> The hot observability stack (Mimir/Loki/Tempo) is optimized for "the last 30 days" — fast queries, real-time alerting, dashboard rendering. The lakehouse is optimized for the *other* questions: "year-over-year trends," "what was the user's behavior in March?", "join my telemetry to my analytics warehouse." A telemetry lakehouse converges the observability stack with the data platform, enabling SQL on telemetry at scale.

This chapter is about the lakehouse architecture for telemetry: OTel → Kafka → Iceberg/Delta → BigQuery/Snowflake/ClickHouse, with SQL as the universal query layer.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [Why hot observability is not enough](#2-why-not-enough)
3. [The lakehouse architecture](#3-architecture)
4. [The "tee" pattern from hot to cold](#4-tee-pattern)
5. [Storage formats: Iceberg, Delta, Hudi, Parquet](#5-formats)
6. [Query engines: BigQuery, Snowflake, ClickHouse, Spark, Trino](#6-engines)
7. [Schema-on-write vs schema-on-read](#7-schema)
8. [Per-signal lakehouse design](#8-per-signal)
9. [Joining telemetry to warehouse data](#9-joining)
10. [The cost calculus](#10-cost)
11. [Use cases the lakehouse enables](#11-use-cases)
12. [Anti-patterns](#12-anti-patterns)
13. [Worked example: a unified ClickHouse lakehouse](#13-worked-example)
14. [Pitfalls](#14-pitfalls)
15. [Mental models](#15-mental-models)

---

## 1. Thesis

Three claims:

1. **Hot observability and lakehouse telemetry serve different questions.** Hot for "right now"; lakehouse for "trends, joins, complex SQL." Both are needed at scale.
2. **The lakehouse pattern is data engineering, not observability.** OTel → Kafka → Iceberg/Parquet → SQL engine. Familiar tools; observability is the new data domain.
3. **Cost dynamics differ.** Lakehouse storage is dramatically cheaper (S3/GCS at pennies per GB); compute scales with query, not retention. Right architecture for years-long retention; wrong for sub-second alerting.

If your team has 200 services, six dashboards per service, and zero ability to ask "what was our metric distribution two years ago?", the hot stack alone is insufficient. The lakehouse fills the gap.

---

## 2. Why Hot Observability Is Not Enough

The questions hot stacks struggle with.

### 2.1 The "long history" question

"What did our error rate look like during last year's holiday season?"

Hot stacks default to 30-90 days retention. Beyond that, downsampled (lossy). For trend analysis, the downsampling is fine; for forensic ("which exact errors fired?"), it's not.

### 2.2 The "complex SQL" question

"Find all sessions where user clicked add-to-cart in browser, then 5 minutes later got an order-failed in their iOS app."

Hot stacks (PromQL, LogQL, TraceQL) are domain-specific languages. Cross-signal join across multiple time windows isn't their strength.

### 2.3 The "join with warehouse" question

"For users who churned in Q3, what were their last 100 sessions' Web Vitals?"

The user's churn data lives in the warehouse (BigQuery, Snowflake). Joining to telemetry requires the telemetry to be queryable from the warehouse.

### 2.4 The "ad-hoc analytical" question

Data scientists, product managers, growth analysts ask telemetry questions. They use SQL. Hot stacks don't.

### 2.5 The "compliance / forensic" question

"What was the exact data flow for customer X on date Y, three years ago?"

Compliance demands long retention with full fidelity. Hot stacks don't fit.

The lakehouse is the answer for all five.

---

## 3. The Lakehouse Architecture

The shape.

```
Services
  │   OTel SDK / native exporters
  ▼
OTel Collector (gateway)
  │
  ├──→ Hot stack (Mimir/Loki/Tempo) — short retention, low latency
  │
  └──→ Kafka topic per signal type
            │
            ▼
       Stream-processing job (Flink / Spark / Beam)
            │   transform / enrich / partition
            ▼
       Object store (S3 / GCS / ADLS)
            │   format: Parquet / ORC, organized by Iceberg / Delta / Hudi
            ▼
       Query engines:
            BigQuery (external table)
            Snowflake (external table)
            ClickHouse (S3 engine)
            Trino / Presto
            Spark
```

### 3.1 The components

- **Producers:** every service emitting OTel.
- **Bus:** Kafka (or Kinesis / Pub/Sub) as the durable buffer.
- **Compactor / writer:** stream job that batches into columnar files.
- **Format:** Iceberg / Delta / Hudi (table format on top of Parquet).
- **Catalog:** Glue / Hive / Iceberg-native, mapping table names to files.
- **Engine:** SQL engine over the object store.

### 3.2 The "lake" vs "warehouse" vs "lakehouse"

- **Lake:** raw files in object store; query via Spark / Trino. Cheap, slow, flexible.
- **Warehouse:** managed columnar store (BigQuery, Snowflake, Redshift). Fast, structured, expensive.
- **Lakehouse:** lake's storage + warehouse's metadata + ACID transactions. The 2020s convergence.

Iceberg / Delta / Hudi are the format layer that makes a lake behave like a warehouse.

### 3.3 The 2026 default

For new builds: Iceberg (Apache, broad vendor support) on S3, queried by BigQuery / Snowflake / ClickHouse. Mature; vendor-neutral.

For existing data-platform shops: probably Delta Lake (Databricks-led) or whatever the data team uses.

---

## 4. The "Tee" Pattern from Hot to Cold

The dual-write architecture.

### 4.1 The principle

The collector / Kafka *tees* every signal:
- One copy to the hot stack (low-latency, short-retention).
- One copy to the lakehouse (high-latency-tolerant, long-retention).

```
collector → Mimir (hot)
         → Kafka → Iceberg (cold)
```

### 4.2 Why both

- The hot path serves alerts and dashboards.
- The cold path serves analytical queries.
- Each is optimized for its access pattern.

### 4.3 The cost split

Hot stack: ~$5 per million metrics points. Cold lake: ~$0.05. 100× difference.

For long retention, cold dominates the bill. Send everything to cold; keep only recent in hot.

### 4.4 The replay capability

The lakehouse retains forever (or per policy). If hot stack data is lost, replay from the lakehouse — re-ingest into Mimir from the cold copy. Disaster-recovery-friendly.

---

## 5. Storage Formats: Iceberg, Delta, Hudi, Parquet

The format layer.

### 5.1 Parquet (the file format)

Columnar; compressed; widely supported. The lowest-level container. Single Parquet file = single immutable record set.

### 5.2 The table formats

Built on Parquet (or ORC), adding metadata + ACID:

| Format | Origin | Strength |
|---|---|---|
| **Iceberg** | Netflix; Apache | Vendor-neutral; broad query-engine support |
| **Delta Lake** | Databricks; Linux Foundation | Best Spark integration; Databricks-native |
| **Hudi** | Uber; Apache | Strong upsert / streaming support |

For telemetry: Iceberg is the safest 2026 bet — most engines (BigQuery, Snowflake, ClickHouse, Trino, Spark) read it natively.

### 5.3 The partitioning scheme

Time-based: partition by hour/day/month.

```
s3://telemetry-lake/metrics/year=2026/month=05/day=06/hour=14/
                                                              part-0001.parquet
                                                              part-0002.parquet
```

Queries with time predicates (most queries) only read relevant partitions. 100×+ scan reduction.

### 5.4 The "small files" problem

Streaming writes create many small files. Query performance suffers.

Compaction: a periodic job merges small files into larger ones (target ~512 MB - 1 GB per file).

Iceberg / Delta have built-in compaction; trigger periodically.

### 5.5 The schema evolution

Iceberg supports schema evolution: add columns, rename, change type (limited). Backward-compatible.

For telemetry where attributes evolve constantly: schema flexibility is non-negotiable.

---

## 6. Query Engines: BigQuery, Snowflake, ClickHouse, Spark, Trino

The compute layer.

### 6.1 The choices

| Engine | Strength | When |
|---|---|---|
| **BigQuery** | Serverless; auto-scaling; GCP-native | Already on GCP; all-in on BQ |
| **Snowflake** | Multi-cloud; mature; strong UI | Already on Snowflake |
| **ClickHouse** | Column-store; very fast; can be self-hosted | Cost-conscious; high-volume |
| **Trino / Presto** | Query federation across stores | Multiple sources to query as one |
| **Spark** | Heaviest analytics; ML | Already on Databricks; ML pipelines |
| **DuckDB** | In-process; fast for single-node | Ad-hoc; smaller scales |

### 6.2 The federation pattern

Trino can query Iceberg in S3, plus Postgres, plus Snowflake. One SQL query joins across.

For telemetry: query telemetry (Iceberg) joined with users (Postgres) and product analytics (Snowflake). Powerful for cross-domain analysis.

### 6.3 The cost model

| Engine | Cost model |
|---|---|
| BigQuery | $/TB scanned (or slot-based) |
| Snowflake | $/credit-hour |
| ClickHouse self-hosted | infra + ops |
| Trino self-hosted | infra + ops |

For exploratory analytics: BigQuery's per-query is great. For high-volume queries: ClickHouse's flat rate wins.

### 6.4 The right tool

Most orgs end up with multiple. Hot stack = Mimir + Loki + Tempo. Cold lakehouse = BigQuery or Snowflake for ad-hoc; ClickHouse for high-volume specific use cases (e.g., trace search).

Don't fight the hybrid. Embrace it.

---

## 7. Schema-on-Write vs Schema-on-Read

The fundamental design choice.

### 7.1 Schema-on-write

Define the table schema upfront. Every row conforms. Like a SQL database.

Pros: fast queries; small storage.
Cons: schema changes are migrations; rigid.

### 7.2 Schema-on-read

Store data in flexible JSON/Map; parse at query time.

Pros: flexible; no migration.
Cons: slower queries; more storage; complex SQL.

### 7.3 The hybrid (recommended)

Use schema-on-write for known fields (the OTel semantic conventions, top-N high-traffic attributes). Use a JSON column for the rest.

```sql
CREATE TABLE metrics (
  timestamp TIMESTAMP,
  service_name STRING,
  metric_name STRING,
  value DOUBLE,
  attributes MAP<STRING, STRING>    -- everything else
)
PARTITIONED BY (date(timestamp), service_name);
```

Queries on `service_name` are fast (column). Queries on `attributes['custom_field']` work but slower.

### 7.4 The "promote to column" pattern

When a JSON field becomes commonly queried, promote it to a column. Migration happens at the lakehouse layer (Iceberg supports column adds).

---

## 8. Per-Signal Lakehouse Design

Each signal needs its own table design.

### 8.1 Metrics

```sql
CREATE TABLE metrics (
  ts TIMESTAMP,
  service_name STRING,
  metric_name STRING,
  value DOUBLE,
  labels MAP<STRING, STRING>,
  exemplar_trace_id STRING,
  region STRING,
  tenant STRING
)
PARTITIONED BY (date(ts), service_name);
```

Metrics tables get *huge*. Aggressive partitioning, columnar compression. Some teams pre-aggregate (downsample) before lakehouse: per-minute or per-hour aggregates only.

### 8.2 Logs

```sql
CREATE TABLE logs (
  ts TIMESTAMP,
  service_name STRING,
  level STRING,
  message STRING,
  trace_id STRING,
  span_id STRING,
  attributes MAP<STRING, STRING>,
  region STRING,
  tenant STRING
)
PARTITIONED BY (date(ts), service_name);
```

Full-text search on `message` is the hard part. ClickHouse has `tokenbf` indexes; BigQuery has search indexes (newer feature). For dedicated log search, may keep an Elasticsearch / Loki cluster alongside.

### 8.3 Traces

```sql
CREATE TABLE spans (
  ts TIMESTAMP,
  trace_id STRING,
  span_id STRING,
  parent_span_id STRING,
  service_name STRING,
  span_name STRING,
  duration_ns BIGINT,
  status STRING,
  attributes MAP<STRING, STRING>,
  resource_attributes MAP<STRING, STRING>
)
PARTITIONED BY (date(ts), service_name)
CLUSTER BY (trace_id);
```

Clustering by `trace_id` so all spans of a trace are co-located: efficient trace assembly.

### 8.4 Profiles

```sql
CREATE TABLE profiles (
  ts TIMESTAMP,
  service_name STRING,
  profile_type STRING,    -- cpu, heap, etc.
  duration_ns BIGINT,
  pprof_bytes BINARY,
  attributes MAP<STRING, STRING>
)
PARTITIONED BY (date(ts), service_name);
```

Profiles are blobs (pprof format). Storage cost low; queries decode at read time.

### 8.5 RUM events

```sql
CREATE TABLE rum_events (
  ts TIMESTAMP,
  user_id_hash STRING,
  session_id STRING,
  page STRING,
  event_type STRING,    -- pageview, click, etc.
  web_vitals MAP<STRING, DOUBLE>,
  attributes MAP<STRING, STRING>
)
PARTITIONED BY (date(ts), page);
```

The cross-domain table: joins to product analytics; shapes the user-experience analysis.

---

## 9. Joining Telemetry to Warehouse Data

The killer use case.

### 9.1 The setup

- Customer table in Snowflake.
- Telemetry tables in lakehouse.
- One SQL engine queries both.

### 9.2 Example queries

```sql
-- Average checkout latency by customer tier
SELECT
  c.tier,
  PERCENTILE(s.duration_ns / 1e6, 0.99) AS p99_ms
FROM spans s
JOIN customers c ON c.id = s.attributes['customer_id_hash']
WHERE s.ts > CURRENT_TIMESTAMP - INTERVAL '7 days'
  AND s.span_name = 'POST /checkout'
GROUP BY c.tier;
```

```sql
-- Errors per release for customers who churned
SELECT
  l.release,
  COUNT(*) AS errors
FROM logs l
JOIN customers c ON c.id_hash = l.attributes['user_id_hash']
WHERE c.churned_date BETWEEN '2026-04-01' AND '2026-04-30'
  AND l.level = 'error'
GROUP BY l.release
ORDER BY errors DESC;
```

These queries are *impossible* in the hot stack alone. They're trivial in SQL.

### 9.3 The PII consideration

Joining telemetry (with user IDs) to customer data: legal review needed. Pseudonymize where possible; access-control the joined views.

### 9.4 The reverse query

Joining customer events (warehouse) to telemetry (lake) — same shape, different direction. Both work.

---

## 10. The Cost Calculus

The economics.

### 10.1 The lakehouse cost model

```
Storage: ~$0.023/GB/month (S3 standard)
         ~$0.0125/GB/month (S3 IA)
         ~$0.001/GB/month (Glacier)

Query:   $5/TB scanned (BigQuery)
         ~$2/TB (Snowflake X-Small credit)
         ~$0.50/TB (ClickHouse self-hosted, amortized)

Compaction: small periodic job; ~$/month
```

### 10.2 The 1-year retention math

100 GB/day × 365 days = ~36 TB. Compressed (zstd): ~12 TB.

S3 cost: ~$280/year storage.
Query cost: depends. ~$1k-$10k/year for moderate ad-hoc usage.

Total: ~$10k/year for a long-retention lakehouse handling 100 GB/day.

For comparison: Datadog at the same volume would be ~$300k/year. 30× difference.

### 10.3 The break-even

Lakehouse becomes worth the engineering investment around 50-100 GB/day, or 30+ days of retention beyond the hot stack. Below that, hot-stack-only is fine.

### 10.4 The data-team partnership

The lakehouse benefits from data-team expertise (Spark, dbt, schema management). Partnering with the data team reduces the platform team's load.

---

## 11. Use Cases the Lakehouse Enables

The "what's it good for" inventory.

### 11.1 Long-term capacity planning

Year-over-year traffic, growth modeling, seasonality. The lakehouse retains years of metrics.

### 11.2 Compliance / forensic

"What did we do with customer X's data three years ago?" Lakehouse retains; queries answer.

### 11.3 SLO compliance reports

Quarterly / annual SLO reports require long-window calculations. Lakehouse easier than hot.

### 11.4 Cost analysis

Per-team / per-tenant / per-feature cost analysis over months. Lakehouse is the source.

### 11.5 Cross-team, cross-signal analytics

The product team asks: "Do users who experience > 3s page-load Web Vitals churn more?" SQL query joining RUM + customers.

### 11.6 ML model training data

Anomaly detection, predictive autoscaling models trained on historical telemetry. Lakehouse provides the training data.

### 11.7 Drift detection (LLM, ML)

Model performance over months; drift detection requires historical signal access.

### 11.8 Audit / compliance

Audit logs in the lakehouse with multi-year retention; queryable; exportable to auditors.

### 11.9 Custom dashboards for product analytics

Product / growth / business teams query telemetry directly. Lakehouse is their interface.

### 11.10 Data science exploration

Hypothesis testing, ad-hoc analysis, retrospective studies. SQL is the native language.

---

## 12. Anti-Patterns

1. **Lakehouse-only (no hot stack).** Alert latency unacceptable.
2. **Hot-stack-only (no lakehouse) at scale.** Long-window queries impossible.
3. **Schema-on-read everywhere.** Slow; expensive.
4. **Schema-on-write everywhere.** Brittle.
5. **No partitioning.** Full-scan queries.
6. **No compaction.** Many small files; slow queries.
7. **Same table for all signals.** Loses optimization opportunity.
8. **No cost monitoring on queries.** Surprise bills.
9. **No PII handling.** Compliance violation.
10. **Lakehouse without warehouse-team partnership.** Reinvents data engineering.
11. **No schema evolution plan.** Breaking changes hurt.
12. **No snapshot / time-travel.** Lose historical state.
13. **Vendor lock-in via proprietary format.** Migration trap.
14. **No retention policy on lakehouse.** Bills grow unbounded.
15. **No lifecycle (hot/warm/cold within lakehouse).** Storage cost suboptimal.

---

## 13. Worked Example: A Unified ClickHouse Lakehouse

Concrete and complete.

### 13.1 The org

- 200 services.
- 100 GB/day metrics, 500 GB/day logs, 200 GB/day traces.
- 1-year retention required (compliance).
- Hot stack: Mimir + Loki + Tempo with 30-day retention.

### 13.2 The architecture

```
Services → OTel Collector → Kafka
                                 ├─→ Mimir / Loki / Tempo (hot, 30d)
                                 └─→ ClickHouse cluster (cold, 1y)
                                         (with Iceberg-on-S3 for archival)
```

ClickHouse here serves dual roles: query engine and warm-cold storage. Hot data on local SSD; cold tiered to S3 via ClickHouse's S3 storage engine.

### 13.3 The schema

Per-signal tables:
- `metrics` — partitioned by date + service
- `logs` — partitioned by date + service
- `spans` — partitioned by date + service, clustered by trace_id
- `profiles` — partitioned by date + service

ClickHouse `MergeTree` engines with proper partitioning and ordering.

### 13.4 The ingest

Stream-processing job (Apache Flink) reads from Kafka, batches into 5-minute Parquet chunks, writes to ClickHouse. Compacted into hourly chunks after 24 hours.

### 13.5 The query

Engineers use ClickHouse SQL:
- For ad-hoc: ClickHouse's Web UI / JDBC.
- For dashboards: ClickHouse as a Grafana datasource.
- For ML / analytics: Trino federates ClickHouse + Snowflake.

### 13.6 Examples

```sql
-- Year-over-year holiday traffic
SELECT date_trunc('day', ts), sum(value)
FROM metrics
WHERE metric_name = 'http_requests_total'
  AND ts BETWEEN '2025-11-01' AND '2025-12-31'
  OR ts BETWEEN '2026-11-01' AND '2026-12-31'
GROUP BY date_trunc('day', ts);
```

```sql
-- Trace search by attribute (impossible in Tempo)
SELECT trace_id, service_name, span_name, duration_ns
FROM spans
WHERE attributes['feature_flag'] = 'pricing_v2'
  AND ts BETWEEN '2026-05-01' AND '2026-05-06'
  AND duration_ns > 1e9   -- > 1 second
ORDER BY duration_ns DESC
LIMIT 100;
```

### 13.7 The cost

- ClickHouse cluster: ~$80k/year.
- S3 storage: ~$5k/year for 1-year of compacted data.
- Total: ~$85k/year.

For comparison, vendor SaaS with same retention would be ~$1M+. ClickHouse lakehouse pays for itself in months.

### 13.8 Operational cost

1 engineer maintaining the ClickHouse cluster: ~30% time. Negligible at the cluster's scale; manageable.

---

## 14. Pitfalls

1. **No lakehouse at scale.** Long-window questions unanswerable.
2. **Lakehouse alone (no hot stack).** Alert latency unacceptable.
3. **No partitioning.** Full-scans.
4. **No compaction.** Small-file proliferation.
5. **No schema evolution plan.** Breakage.
6. **PII in lakehouse without controls.** Compliance gap.
7. **Vendor lock-in via proprietary format.** Migration trap.
8. **No cost monitoring.** Bill explodes.
9. **No tiered storage within lakehouse.** Cost suboptimal.
10. **Lakehouse separate from data team.** Reinvented effort.
11. **No retention policy.** Storage grows unbounded.
12. **Schema-on-read for high-traffic queries.** Slow.
13. **Schema-on-write for everything.** Rigid.
14. **No documentation of tables.** Engineers can't query.
15. **No backup of lakehouse metadata.** Catalog corruption = data loss.

---

## 15. Mental Models

> **Hot stack and lakehouse serve different questions. Both are needed at scale.**

> **The lakehouse is data engineering applied to telemetry. Same tools as the warehouse.**

> **Tee from collector to both hot and cold. Each path optimized differently.**

> **Iceberg is the 2026 default format. Vendor-neutral; broad engine support.**

> **Schema-on-write for known fields; schema-on-read for the rest. Hybrid wins.**

> **Partition by date + service. Clustering by trace_id for spans.**

> **Compaction is mandatory. Otherwise small-file death.**

> **The lakehouse enables joins across telemetry and warehouse. The killer use case.**

> **Cost is dramatically lower than hot. ~30× difference at scale.**

> **Partner with the data team. Reuse expertise.**

Now go to `doc 36` (DR for the observability stack).
