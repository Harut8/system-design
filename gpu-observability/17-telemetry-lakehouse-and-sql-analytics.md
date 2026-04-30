# 17 — Telemetry Lakehouse & SQL Analytics

> Prometheus answers "is this GPU on fire *right now*?" The lakehouse answers "what was the average GR_ENGINE_ACTIVE per host per day for team X last quarter, and what did it cost?" Same DCGM source, two sinks, two timescales, two query languages. This chapter is the bridge between business questions, raw GPU samples, and the SQL that turns one into the other.

If you read docs 08 (cardinality), 12 (capacity), and 13 (multi-tenant) and walked away thinking "I cannot answer FinOps questions in PromQL," you were right. PromQL is shaped for time-windowed math over short retention; FinOps and capacity reviews are shaped for SQL joins over a year of high-cardinality samples. You need both.

---

## 1. Why Two Paths Exist

The realtime path (DCGM → exporter → Prometheus → Thanos → Grafana, doc 01) and the analytics path (DCGM → exporter → Kafka → Hive/Iceberg → SQL) share the same source and disagree about everything else.

| Concern | Realtime path (Prometheus) | Analytics path (Lakehouse) |
|---------|----------------------------|----------------------------|
| Query latency | Sub-second | Seconds to minutes |
| Retention | 15s raw → 5m → 1h, weeks | Raw days, rollups years |
| Cardinality budget | Tight (doc 08) — millions of series | Loose — billions of rows OK |
| Joins | Label-based, single-source | SQL joins to org/finance/registry |
| Late data | Drop / ignore | First-class, bounded by watermark |
| Query language | PromQL | SQL (Presto/Trino, Spark, Hive) |
| Primary user | SRE on-call, alert rules | FinOps, capacity, DS, leadership |
| Cost shape | RAM (active series) | Storage + scan |

> **Mental model:** Prometheus is a *circular buffer with math*. The lakehouse is a *ledger with joins*. Don't ask the buffer to do accounting; don't ask the ledger to page you.

The two paths are not redundant. Trying to answer "$/useful-GPU-hour by team YoY" in PromQL forces you to either (a) keep `pod`, `namespace`, and `team` as scrape labels (cardinality bomb, doc 08 §3) or (b) pre-compute every team rollup as a recording rule and lose the ability to ask new questions ad-hoc. The lakehouse exists exactly so you don't have to make that trade.

---

## 2. The Kafka Contract

Kafka is the *seam* between realtime and analytics. Both Prometheus (via `remote_write`) and the lake (via Kafka consumer) can fan out from one publish path, or you can run a separate "analytics" emit alongside Prometheus scrape. Most large platforms do the second — the realtime scrape stays untouched, and a sidecar pushes a richer event stream to Kafka.

### 2.1 Topic layout

Two viable shapes. Pick one early; switching later means a backfill.

| Shape | Topics | Pro | Con |
|-------|--------|-----|-----|
| **One wide topic** | `gpu_telemetry_raw` with a `metric_name` field | Simple producer, simple schema evolution | Consumers filter; partitioning is coarser |
| **Per-family topics** | `gpu_telemetry_dcgm_compute`, `…_memory`, `…_health`, `…_throttle` | Cheap consumers, family-level retention tuning | More producer config, more Kafka admin |

A reasonable production split:

```
gpu_telemetry_raw          # all DCGM fields, per-device, every scrape
gpu_telemetry_rollup_1m    # pre-aggregated rollups (see §3)
gpu_workload_events        # pod/job lifecycle joined to GPU UUID
gpu_hardware_events        # XID, ECC retirements, fabric errors (low-rate)
```

`gpu_hardware_events` is its own topic because it is low-rate and high-value — you do not want it stuck behind a backlog of compute samples.

### 2.2 Schema

Avro or Protobuf with a registry. JSON is a mistake at this volume — you'll pay for it in storage and in every consumer's CPU. A minimal schema for `gpu_telemetry_raw`:

```protobuf
message GpuSample {
  // event-time, milliseconds since epoch — set by exporter, not by Kafka
  int64 ts_ms = 1;

  // identity
  string cluster      = 10;  // "prod-iad-1"
  string region       = 11;  // "us-east-1"
  string hostname     = 12;  // "gpu-h100-0173.iad1.internal"
  string device_uuid  = 13;  // GPU UUID — stable across reboots
  int32  gpu_index    = 14;  // 0..7 on an HGX node
  string gpu_product  = 15;  // "H100-SXM5-80GB"

  // MIG (optional, empty for full-GPU)
  string gi_id = 20;  // GPU Instance ID
  string ci_id = 21;  // Compute Instance ID

  // workload binding (joined upstream by exporter sidecar; may be empty)
  string namespace   = 30;
  string pod         = 31;
  string container   = 32;
  string workload_id = 33;  // training job ID, deployment uid, etc.

  // metric payload
  string metric_name = 50;  // "DCGM_FI_PROF_GR_ENGINE_ACTIVE"
  double value       = 51;
  string unit        = 52;  // "ratio", "watts", "celsius", "bytes"
}
```

Three rules that keep this schema sane:

1. **`device_uuid` is the join key, not `hostname` + `gpu_index`.** Hosts get reimaged and GPU indices shift; UUIDs survive (doc 02 §4). Keep both, but joins go through UUID.
2. **`workload_id` is opaque to the exporter.** The sidecar reads it from a pod label or env var; the lake doesn't care what it means as long as it is stable.
3. **`ts_ms` is event-time.** Late events arrive — see §8. Never use Kafka ingest-time for the analytical record.

### 2.3 Partitioning

Partition key drives ordering and consumer parallelism. The default that works:

```
partition_key = hash(cluster + ":" + hostname)
```

This keeps all samples for a given host in one partition (so a rollup job sees a consistent ordering for `host`-grain aggregation) without making `device_uuid` the key (which would multiply partitions by 8 with no benefit). Use 64–256 partitions per topic for a 1k-node cluster; size up if a host's per-second sample count exceeds ~5MB/s (rare).

> **Pitfall:** keying by `device_uuid` looks tempting because the lake joins on it. Don't — you lose host-level locality, you make per-host rollups (`WORKLOAD_UTILIZATION_BY_HOST`, §3) far more expensive in the streaming job, and you 8× your partition count for no real ordering gain.

### 2.4 Exporter-side shim

`dcgm-exporter` natively speaks Prometheus. To get to Kafka, the typical pattern is a small sidecar (or a Vector / OpenTelemetry Collector pipeline) that:

1. Scrapes `dcgm-exporter` on `:9400/metrics` every 10–15s (matching Prometheus scrape).
2. Joins each sample with pod/namespace/workload metadata from the kubelet API or a local cache fed by `kube-state-metrics`.
3. Encodes into `GpuSample` and produces to Kafka with `ts_ms` set to scrape time.

This is the *one* place where the analytics path can diverge from Prometheus — it can carry labels (`pod`, `workload_id`) that the Prometheus scrape drops to stay under cardinality budget.

---

## 3. Pre-Aggregation in the Stream

The user's example — `WORKLOAD_UTILIZATION_BY_DEVICE`, `WORKLOAD_UTILIZATION_BY_HOST`, `WORKLOAD_UTILIZATION_BY_NAMESPACE` — is the canonical pattern: same DCGM `GR_ENGINE_ACTIVE` source, multiple grains emitted into separate topics by a streaming job.

### 3.1 Why pre-aggregate at all

Raw scrape volume:

```
1000 nodes × 8 GPUs × ~30 metrics × 4 samples/min ≈ 1M events/min ≈ 17k/s
```

That is fine for Kafka and fine for Hive raw, but a query that asks "average host utilization per day for the last 90 days" scans 90 × 1.5B rows. A 1-minute host-grain rollup reduces that to 90 × 1k hosts × 1440 min ≈ 130M rows — 11× cheaper to scan, and the answer is bit-identical for the question being asked.

### 3.2 The grains worth emitting

| Topic / table | Grain | Source | Typical query |
|---------------|-------|--------|---------------|
| `gpu_metrics_raw` | (device, ts) | exporter | forensic, per-second drill |
| `workload_util_by_device_1m` | (device, namespace, workload_id, minute) | stream agg | per-job efficiency |
| `workload_util_by_host_1m` | (hostname, minute) | stream agg | host saturation |
| `workload_util_by_namespace_1m` | (cluster, namespace, minute) | stream agg | team-level utilization |
| `workload_util_by_cluster_5m` | (cluster, 5min) | stream agg | fleet rollup, capacity |

Each is a Flink (or Spark Structured Streaming) job consuming `gpu_telemetry_raw` and emitting on event-time tumbling windows. The aggregation is mean (occasionally max for saturation metrics, p95/p99 for latencies). For `GR_ENGINE_ACTIVE` you almost always want mean — it is already a ratio.

A Flink-style pseudocode for `workload_util_by_host_1m`:

```sql
INSERT INTO workload_util_by_host_1m
SELECT
  cluster,
  hostname,
  TUMBLE_START(ts, INTERVAL '1' MINUTE) AS bucket_ts,
  AVG(value) FILTER (WHERE metric_name = 'DCGM_FI_PROF_GR_ENGINE_ACTIVE') AS gr_engine_active_avg,
  AVG(value) FILTER (WHERE metric_name = 'DCGM_FI_PROF_SM_ACTIVE')        AS sm_active_avg,
  AVG(value) FILTER (WHERE metric_name = 'DCGM_FI_DEV_POWER_USAGE')       AS power_w_avg,
  COUNT(DISTINCT device_uuid)                                              AS gpus_seen
FROM gpu_telemetry_raw
GROUP BY cluster, hostname, TUMBLE(ts, INTERVAL '1' MINUTE);
```

`gpus_seen` is a sentinel — when it drops below 8 on an HGX node, an exporter or device dropped out (doc 07 §dead-GPU detection). Carrying it in the rollup means an SRE can spot the dropout in the lake without going back to raw.

### 3.3 Trade-off: pre-agg vs raw-and-aggregate-in-SQL

> **Mental model:** every pre-aggregation is a *cached query*. It is faster and cheaper, but it is also *lossy* — you cannot recover a finer grain from a coarser one. Decide by half-life: if the question is asked weekly for years, pre-agg. If it is asked once for an incident, hit raw.

| Strategy | Pro | Con |
|----------|-----|-----|
| **Pre-agg in stream (Flink/Spark)** | Cheap reads, low Hive scan cost | Lossy; new grains require backfill or wait |
| **Raw to Hive, aggregate in SQL** | Fully re-derivable, ad-hoc-friendly | Expensive scans on long ranges |
| **Both** | Fast common queries, raw available for forensics | More moving parts, two retention tiers |

Most large platforms do **both**: raw kept for 30–90 days for forensic and ad-hoc, rollups kept for years for FinOps and capacity. Section 7 covers retention.

---

## 4. Hive / Iceberg Table Design

The "where does it land" question. Hive metastore + Parquet on S3/HDFS is the floor; Iceberg on the same is the modern default (snapshot isolation, schema evolution, partition evolution, hidden partitioning). Delta and Hudi solve the same problems with different trade-offs. The tables below assume Iceberg semantics.

### 4.1 The fact tables

#### `raw_gpu_metrics`

```sql
CREATE TABLE raw_gpu_metrics (
  ts             TIMESTAMP,
  cluster        STRING,
  region         STRING,
  hostname       STRING,
  device_uuid    STRING,
  gpu_index      INT,
  gpu_product    STRING,
  gi_id          STRING,
  ci_id          STRING,
  namespace      STRING,
  pod            STRING,
  container      STRING,
  workload_id    STRING,
  metric_name    STRING,
  value          DOUBLE,
  unit           STRING
)
PARTITIONED BY (dt DATE, cluster STRING)
STORED AS PARQUET
TBLPROPERTIES (
  'format-version'='2',
  'write.target-file-size-bytes'='536870912'  -- 512 MiB
);
```

Partition by `(dt, cluster)`. **Not** by `metric_name` — pruning by metric name is rare for most queries, and per-metric partitions explode the small-files count. Sort within a partition by `(hostname, device_uuid, ts)` for locality on the most common scan pattern.

> **Pitfall:** the long-format `(metric_name, value)` schema is flexible but pays a 30–50% storage tax over wide-format. If 90% of your queries hit a known set of ~20 fields, materialize a wide-format rollup with one column per metric (next subsection) and let raw stay long-format for the long tail.

#### `workload_util_by_device_1m`, `…_by_host_1m`, etc.

```sql
CREATE TABLE workload_util_by_host_1m (
  bucket_ts            TIMESTAMP,
  cluster              STRING,
  hostname             STRING,
  gr_engine_active_avg DOUBLE,
  sm_active_avg        DOUBLE,
  tensor_active_avg    DOUBLE,
  fb_used_bytes_avg    DOUBLE,
  power_w_avg          DOUBLE,
  power_w_max          DOUBLE,
  temp_c_max           DOUBLE,
  gpus_seen            INT
)
PARTITIONED BY (dt DATE, cluster STRING)
STORED AS PARQUET;
```

Wide format, one row per (host, minute). For a 1k-node fleet, that is 1k × 1440 = 1.44M rows per day — trivially scannable. Roll up further into `_5m`, `_1h`, `_1d` tiers.

### 4.2 The dimension tables

The lakehouse's job is the join. Carry these as slowly-changing dimension (SCD-2) tables refreshed daily from the system of record:

| Table | Source of truth | Grain | What it enables |
|-------|-----------------|-------|-----------------|
| `dim_gpu_device` | DCGM inventory + RMA system | per `device_uuid` | SKU, install date, RMA history |
| `dim_host` | CMDB / cluster API | per `hostname` | rack, row, PSU, datacenter |
| `dim_workload` | k8s API / training launcher | per `workload_id` | team, project, model, run name |
| `dim_team` | HR / org system | per `team_id` | cost center, VP, headcount |
| `dim_gpu_sku_cost` | finance | per (`gpu_product`, month) | $/hour amortized, depreciation, power cost |
| `dim_model_registry` | MLflow / model registry | per `model_id` | parameter count, family, owning team |

`dim_gpu_sku_cost` is the table that turns utilization into dollars. It typically holds:

```
gpu_product       month       hourly_capex_usd  hourly_power_usd  hourly_total_usd
H100-SXM5-80GB    2026-01     2.85              0.32              3.17
H100-SXM5-80GB    2026-02     2.83              0.31              3.14
A100-SXM4-80GB    2026-01     1.42              0.21              1.63
```

Built from amortized capex (procurement memo, doc 12 §12) over the SKU's lifetime, plus regional power cost. Refreshed monthly. **This is the table finance audits.**

### 4.3 The "metric model"

Wide rollup tables are easy to query but rigid. A pattern that survives change: keep the rollups *narrow on dimensions, wide on metrics*. New metrics → add a column (Iceberg schema evolution handles it). New dimensions → join through `device_uuid` or `workload_id` to a dimension table; do not denormalize team / cost / org into the fact table.

> **Pitfall:** denormalizing `team` into `workload_util_by_device_1m` looks tempting ("queries are simpler!"). It rots the moment a workload changes ownership, and it forces a backfill every time the org chart moves. Keep the fact table to immutable physical identity; resolve ownership at query time through `dim_workload`.

---

## 5. SQL Recipes for Business Questions

The point of all this. Each recipe maps a phrase a VP says to a query.

### 5.1 "What was average device utilization per host last week?"

The user's literal example.

```sql
SELECT
  d.dt,
  r.hostname,
  AVG(r.gr_engine_active_avg) AS host_util_daily_avg
FROM workload_util_by_host_1m r
JOIN (
  SELECT date_trunc('day', bucket_ts) AS dt FROM workload_util_by_host_1m
  WHERE bucket_ts >= current_date - INTERVAL '7' DAY
  GROUP BY 1
) d ON date_trunc('day', r.bucket_ts) = d.dt
WHERE r.bucket_ts >= current_date - INTERVAL '7' DAY
GROUP BY d.dt, r.hostname
ORDER BY d.dt, host_util_daily_avg DESC;
```

The host-grain rollup makes this scan ~10M rows for a 1k-node fleet over 7 days. Run on raw, it would scan ~1B.

### 5.2 "Average GR_ENGINE_ACTIVE per host per day for team X last quarter"

```sql
SELECT
  date_trunc('day', r.bucket_ts) AS dt,
  r.hostname,
  AVG(r.gr_engine_active_avg) AS host_util
FROM workload_util_by_host_1m r
JOIN dim_host h ON h.hostname = r.hostname
JOIN dim_workload w ON w.dominant_host = r.hostname  -- or join through device-grain
JOIN dim_team t ON t.team_id = w.team_id
WHERE r.bucket_ts >= date_trunc('quarter', current_date - INTERVAL '1' DAY)
  AND r.bucket_ts <  date_trunc('quarter', current_date)
  AND t.name = 'forecasting-platform'
GROUP BY 1, 2
ORDER BY 1, 2;
```

For team-level questions, prefer `workload_util_by_namespace_1m` (cleaner ownership) over inferring team from host. The host-grain rollup is for *infrastructure* questions ("which racks are hot"), namespace-grain for *team* questions ("is forecasting using their quota").

### 5.3 "$/useful-GPU-hour by team last month"

The FinOps headline number.

```sql
WITH device_hours AS (
  SELECT
    w.team_id,
    d.gpu_product,
    SUM(r.gr_engine_active_avg) / 60.0          AS useful_gpu_hours,  -- 1m buckets
    COUNT(*) / 60.0                             AS allocated_gpu_hours
  FROM workload_util_by_device_1m r
  JOIN dim_gpu_device d ON d.device_uuid = r.device_uuid
  JOIN dim_workload   w ON w.workload_id = r.workload_id
  WHERE r.bucket_ts >= date_trunc('month', current_date - INTERVAL '1' DAY)
    AND r.bucket_ts <  date_trunc('month', current_date)
  GROUP BY w.team_id, d.gpu_product
),
costed AS (
  SELECT
    dh.team_id,
    dh.gpu_product,
    dh.useful_gpu_hours,
    dh.allocated_gpu_hours,
    dh.allocated_gpu_hours * c.hourly_total_usd AS allocated_cost_usd
  FROM device_hours dh
  JOIN dim_gpu_sku_cost c
    ON c.gpu_product = dh.gpu_product
   AND c.month = date_trunc('month', current_date - INTERVAL '1' DAY)
)
SELECT
  t.name AS team,
  SUM(allocated_cost_usd)                                    AS spend_usd,
  SUM(useful_gpu_hours)                                      AS useful_hours,
  SUM(allocated_cost_usd) / NULLIF(SUM(useful_gpu_hours), 0) AS cost_per_useful_hour,
  SUM(useful_gpu_hours)   / NULLIF(SUM(allocated_gpu_hours), 0) AS efficiency
FROM costed c
JOIN dim_team t ON t.team_id = c.team_id
GROUP BY t.name
ORDER BY spend_usd DESC;
```

Two columns the CFO actually reads: `cost_per_useful_hour` (lower is better) and `efficiency` (utilization × allocation, higher is better). A team paying $3.17/h for an H100 but only achieving 30% efficiency has an effective cost of $10.57/useful-h — and that is the number that drives the conversation.

### 5.4 "Top 50 expensive idle devices last 7 days"

```sql
SELECT
  r.device_uuid,
  d.hostname,
  d.gpu_product,
  AVG(r.gr_engine_active_avg) AS avg_util,
  SUM(CASE WHEN r.gr_engine_active_avg < 0.05 THEN 1 ELSE 0 END) / 60.0 AS idle_hours,
  SUM(CASE WHEN r.gr_engine_active_avg < 0.05 THEN 1 ELSE 0 END) / 60.0
    * c.hourly_total_usd AS wasted_usd
FROM workload_util_by_device_1m r
JOIN dim_gpu_device  d ON d.device_uuid = r.device_uuid
JOIN dim_gpu_sku_cost c ON c.gpu_product = d.gpu_product
                       AND c.month = date_trunc('month', current_date)
WHERE r.bucket_ts >= current_date - INTERVAL '7' DAY
GROUP BY r.device_uuid, d.hostname, d.gpu_product, c.hourly_total_usd
HAVING AVG(r.gr_engine_active_avg) < 0.20
ORDER BY wasted_usd DESC
LIMIT 50;
```

This is the query that turns into a weekly "GPU waste report" auto-emailed to engineering managers. Doc 05 covers the dashboard form; the lake form is what survives a year of trend.

### 5.5 "Model-family efficiency ranking"

```sql
SELECT
  m.family,                             -- llama, mixtral, internal_rec, etc.
  COUNT(DISTINCT w.workload_id)         AS runs,
  SUM(r.gr_engine_active_avg) / 60.0    AS useful_hours,
  AVG(r.tensor_active_avg)              AS avg_tensor_active,
  AVG(r.gr_engine_active_avg)           AS avg_gr_active
FROM workload_util_by_device_1m r
JOIN dim_workload       w ON w.workload_id = r.workload_id
JOIN dim_model_registry m ON m.model_id    = w.model_id
WHERE r.bucket_ts >= current_date - INTERVAL '30' DAY
GROUP BY m.family
ORDER BY avg_tensor_active DESC;
```

The answer to "which model family is leaving tensor cores on the table" — typically a kernel or framework problem (doc 11), not a hardware one.

### 5.6 "Year-over-year fleet utilization"

```sql
SELECT
  date_trunc('month', bucket_ts) AS month,
  AVG(gr_engine_active_avg)      AS fleet_util,
  COUNT(DISTINCT hostname)       AS active_hosts
FROM workload_util_by_host_1m
WHERE bucket_ts >= current_date - INTERVAL '24' MONTH
GROUP BY 1
ORDER BY 1;
```

The chart that goes in the procurement memo (doc 12 §12). If the line is flat-or-down while spend is up, capacity is being added faster than utilization improves — a planning red flag.

---

## 6. The Reconciliation Problem

Prometheus and Hive will disagree about the same metric. When finance asks why, you need a defensible answer.

### 6.1 Sources of divergence

| Source | Direction | Magnitude | Defensible answer |
|--------|-----------|-----------|-------------------|
| Different scrape interval (Prom 15s vs Kafka 10s) | Either | <1% | Document both intervals; report Hive at minute grain only |
| Late-arriving Kafka events | Hive higher after watermark | ~0.1–1% | Quote Hive numbers only after watermark close (§8.1) |
| Prometheus drops samples on remote_write backlog | Prom lower | bursty | Monitor `prometheus_remote_storage_samples_dropped_total` |
| Kafka producer retries duplicate | Hive higher | ~0.01–0.1% | Idempotent producer + dedup on `(device_uuid, ts_ms, metric_name)` |
| Recording-rule rounding in Prom | Prom slightly off | <0.5% | Always cite Hive for accounting |
| Different aggregation function (mean-of-means) | Either | up to 5% | Aggregate to same grain on both sides before comparing |

> **Mental model:** Prometheus is the *source of truth for alerts*. The lake is the *source of truth for ledgers*. When they disagree on a number that finance cares about, the lake wins, and the difference is documented as part of close.

### 6.2 The "mean-of-means" trap

A 1-minute average of 4 samples, then averaged across 1440 minutes for a daily mean, is **not** the same as the daily mean of all raw samples — *unless* every minute had the same number of samples. Exporter dropouts break this assumption.

The defensible pattern:

```sql
-- WRONG: mean of 1m means weights every minute equally
SELECT AVG(gr_engine_active_avg) FROM workload_util_by_host_1m WHERE …;

-- RIGHT: weight each 1m bucket by its sample count
SELECT SUM(gr_engine_active_avg * sample_count) / SUM(sample_count)
FROM workload_util_by_host_1m WHERE …;
```

Carry `sample_count` in every rollup table for this reason. The 5% errors people argue about in finance reviews almost always come from this.

---

## 7. Cost & Retention

Each tier has a different economic shape.

| Tier | Where | Retention | Why |
|------|-------|-----------|-----|
| Kafka raw topic | Kafka brokers | 3–7 days | Replay buffer for downstream jobs; not a query store |
| `raw_gpu_metrics` | Object store (S3/GCS) | 30–90 days | Forensic + ad-hoc backfill of new rollups |
| `*_1m` rollups | Object store | 12–24 months | Most analytics & dashboards |
| `*_1h` rollups | Object store | 3–5 years | Long-horizon trend, capacity planning |
| `*_1d` rollups | Object store | indefinite | YoY charts, board decks |

Storage cost example for a 1k-node H100 fleet:

```
raw, 90 days  ≈ 1k × 8 × 30 metrics × 4 samples/min × 60 × 24 × 90
              ≈ 1.5T rows × ~100 B/row Parquet ≈ ~150 TB
              @ $0.023/GB-mo (S3 standard) ≈ $3.5k/mo
1m rollups, 24 months ≈ 1k × 60 × 24 × 720 × ~200 B ≈ ~21 GB
              ≈ trivial cost
```

Raw is the expensive tier. Aggressive raw tiering (S3 IA after 7d, Glacier after 30d) cuts that 60–80% if you accept slower forensic queries.

> **Pitfall:** small files. A naïve Kafka → Hive sink writes a file per partition per micro-batch and you end up with a 10M-file table that can't be queried. Use a compaction job (Iceberg's `rewrite_data_files`, or a daily Spark job) to merge into 256MB–1GB files. Without this, scan cost is 10× and metastore CPU climbs steadily until it fails.

---

## 8. Operational Concerns

### 8.1 Late events and watermarks

Exporters disconnect, hosts reboot, Kafka consumers lag. A sample with `ts_ms = 14:32` may arrive in Hive at `15:10`. Streaming jobs must define a watermark — typically 5–15 minutes — and either (a) hold rollup commits until watermark passes or (b) emit and update.

The reconciliation contract that usually works:

- 1-minute rollups are *provisional* until `now() - 30m`, *final* after.
- Dashboards label the last 30 minutes as "live, may revise".
- FinOps reports always run with `bucket_ts <= date_trunc('day', now()) - INTERVAL '1' DAY` to avoid the unstable edge.

### 8.2 Schema evolution

DCGM adds fields. Exporters add labels. Kafka schemas grow. The discipline:

1. **Producers are forward-compatible** — new fields are optional, default-valued.
2. **Consumers are backward-compatible** — they ignore unknown fields.
3. **Iceberg handles wide-table evolution** — add columns; never reorder; never repurpose.
4. **Long-format raw absorbs new metrics for free** — `metric_name` is just another value. Wide rollups need a column add and a backfill, which is why long-format raw exists.

### 8.3 Backfill

The painful operation. You introduced `workload_util_by_namespace_5m` last month and need 12 months of history. Two approaches:

| Approach | When | Cost |
|----------|------|------|
| Replay from raw via Spark batch | Raw retention covers the window | One-time scan of `raw_gpu_metrics` |
| Re-derive from a finer rollup | Finer rollup covers the window | Cheap, but only if grains nest |

Backfills are why raw retention is 90 days, not 7. The first time you skip raw retention to save money, the second new dimension ask costs you more than a year of raw storage.

### 8.4 PII and tenancy

Workload labels can carry user identifiers (notebook owner, training run author). Treat the lake as PII-bearing: row-level ACLs in the metastore, column masking for `pod` / `workload_id` outside the platform team, and a tenancy boundary that matches doc 13's RBAC model. Cost reports go to managers; per-user breakdowns go nowhere outside the platform team without a documented use case.

---

## 9. Putting It Together: The Full Path

```
                 ┌──────────────────┐
                 │  GPU + DCGM      │  per node (doc 02)
                 └────────┬─────────┘
                          │ NVML / DCP fields
                          ▼
                 ┌──────────────────┐
                 │  dcgm-exporter   │  :9400/metrics
                 └────────┬─────────┘
                          │
              ┌───────────┴────────────┐
              │                        │
              ▼                        ▼
   ┌──────────────────┐      ┌────────────────────┐
   │ Prometheus scrape│      │ Kafka shim sidecar │  joins pod/workload meta
   │ (15s, doc 01)    │      │ (10s)              │
   └────────┬─────────┘      └─────────┬──────────┘
            │                          │ Avro/Proto
            ▼                          ▼
   ┌──────────────────┐      ┌────────────────────┐
   │ Thanos / Mimir   │      │  Kafka topic       │
   │ alerting, Grafana│      │  gpu_telemetry_raw │
   └──────────────────┘      └─────────┬──────────┘
                                       │
                       ┌───────────────┴───────────────┐
                       │                               │
                       ▼                               ▼
              ┌─────────────────┐           ┌─────────────────────┐
              │ Flink/Spark     │           │ Kafka → Hive sink   │
              │ rollup jobs     │           │ (compacted)         │
              └────────┬────────┘           └─────────┬───────────┘
                       │                              │
                       ▼                              ▼
            ┌──────────────────────┐      ┌──────────────────────┐
            │ workload_util_*_1m   │      │ raw_gpu_metrics      │
            │ (Iceberg, Parquet)   │      │ (Iceberg, Parquet)   │
            └──────────┬───────────┘      └──────────┬───────────┘
                       │                             │
                       └─────────────┬───────────────┘
                                     ▼
                       ┌──────────────────────────────┐
                       │ Trino / Spark / Hive SQL     │
                       │ + dim_team / dim_gpu_sku_cost│
                       │ + dim_workload / dim_model   │
                       └──────────────┬───────────────┘
                                      ▼
                       ┌──────────────────────────────┐
                       │  Reports, dashboards, memos  │
                       │  FinOps, capacity, exec      │
                       └──────────────────────────────┘
```

The realtime path (top-left branch) and the analytics path (top-right branch) share *only* the exporter. Everything downstream is independent — and that is the point. A Kafka outage does not blind alerting; a Prometheus outage does not break next month's FinOps report.

---

## 10. Cross-References

- **doc 02** — DCGM fields and what each metric means at the source
- **doc 05** — utilization vs allocation framing (the same numerator/denominator the lake computes)
- **doc 08** — why pod/workload labels do not belong in Prometheus scrape and *do* belong in the Kafka stream
- **doc 12** — capacity & cost; this chapter is the data layer that backs §12.3 (right-sizing) and §12.12 (procurement memo)
- **doc 13** — multi-tenant RBAC model that the lake's row/column ACLs mirror
- **Appendix B** — DCGM field IDs that map to the `metric_name` column in `raw_gpu_metrics`

---

## 11. Acceptance Checklist

- [ ] Kafka topic schema (Avro/Proto) registered, with `device_uuid`, event-time `ts_ms`, and `workload_id`
- [ ] Exporter sidecar joins pod/workload metadata before publish
- [ ] At least three rollup grains: by-device, by-host, by-namespace, at 1m
- [ ] `raw_gpu_metrics` Iceberg table partitioned by `(dt, cluster)`, compacted daily
- [ ] `dim_gpu_device`, `dim_workload`, `dim_team`, `dim_gpu_sku_cost` refreshed daily/monthly
- [ ] `sample_count` carried in every rollup; mean-of-means queries weight by it
- [ ] Watermark policy documented; FinOps queries exclude the unstable edge
- [ ] Raw retention ≥ 30 days to allow new-rollup backfill
- [ ] Compaction job running; mean file size on raw partition ≥ 256 MB
- [ ] Reconciliation procedure documented for Prom-vs-Hive disagreements
- [ ] Row/column ACLs match the doc-13 tenancy model
- [ ] At least one production query backs a recurring exec/finance report — the proof that the lake is *used*, not just *built*

---
