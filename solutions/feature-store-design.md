# ML Feature Store Platform: Design Document

> Solution to [`tasks/feature-store.md`](../tasks/feature-store.md).

---

## Table of Contents

1. [Requirements Clarification](#1-requirements-clarification)
2. [Capacity Estimates](#2-capacity-estimates)
3. [High-Level Architecture](#3-high-level-architecture)
4. [Feature Registry and Definition Language](#4-feature-registry-and-definition-language)
5. [Batch Computation Pipeline](#5-batch-computation-pipeline)
6. [Streaming Computation Pipeline](#6-streaming-computation-pipeline)
7. [Online Store Design](#7-online-store-design)
8. [Offline Store Design](#8-offline-store-design)
9. [Point-in-Time Join Algorithm](#9-point-in-time-join-algorithm)
10. [Feature Transformation at Serving Time](#10-feature-transformation-at-serving-time)
11. [Training-Serving Consistency](#11-training-serving-consistency)
12. [Feature Quality and Monitoring](#12-feature-quality-and-monitoring)
13. [Data Models](#13-data-models)
14. [API Design](#14-api-design)
15. [Sequence Flows](#15-sequence-flows)
16. [Failure Walkthroughs](#16-failure-walkthroughs)
17. [Trade-offs](#17-trade-offs)
18. [Evolution Path](#18-evolution-path)

---

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|---|---|---|
| Scope | Does the platform own model training or serving? | No. The platform's contract ends at delivering feature vectors -- to the online serving API for inference, and to generated training datasets for offline training. Model lifecycle management is a separate platform's concern. |
| Entity scale | What is the entity cardinality distribution? | Heavily skewed: ~1B user entities, ~50M item entities, ~10M merchant/session entities. Most feature lookups are by user_id (80% of online traffic). |
| Feature shape | What is the typical value size? | Scalar features (int/float/string): 8-64 bytes. Embedding vectors (128-512 dim float32): 512-2048 bytes. Lists/maps: up to 4 KB. Weighted average across all features: ~50 bytes. |
| Freshness tiers | Is every feature real-time? | No -- three tiers: real-time (streaming, <=5s stale), near-real-time (micro-batch, <=5min), batch (daily/hourly, <=24h). Roughly 20% of features are real-time, 30% near-real-time, 50% batch. |
| Consistency | When a streaming feature updates, must the online and offline stores be atomically consistent? | No -- eventual consistency with bounded lag. The online store reflects streaming writes within seconds; the offline store receives the same writes via a dual-write log, but may lag by minutes. The hard invariant is that the offline store's historical record, once written, is immutable and point-in-time queryable. |
| Computation | Can teams bring arbitrary compute (custom Python, Java)? | Teams write transformation logic (SQL, PySpark, Flink SQL); the platform owns orchestration, resource management, exactly-once guarantees, and materialization. Custom UDFs are supported but must be registered and versioned. |
| PII | How is PII handled? | PII features are tagged at definition time. They are encrypted at rest in both stores, excluded from training datasets by default (opt-in with approval), and access-logged. The platform does not decide what is PII -- the feature owner declares it. |
| Multi-tenancy | How many teams share the platform? | 100+ ML teams, each owning 50-500 features, sharing a single platform instance. Feature definitions are namespaced by team but features can be shared cross-team via explicit grants. |
| Offline store | What format is the existing data lake? | S3 + Delta Lake (Parquet-backed). The feature store's offline store integrates into this existing lake, not replaces it. |
| Backfill | How far back can backfills go? | Up to 3 years of historical data, limited by source data retention. A backfill recomputes feature values from raw source data using the new feature definition, filling the offline store's historical table. |
| Team size | How many engineers operate the platform? | 4-6 platform engineers. This shapes every decision toward managed/automated operations over manual intervention. |

### Key Assumptions

1. **The existing stack is Kafka + S3/Delta Lake + Spark/Flink + a data warehouse (BigQuery or Snowflake).** The feature store integrates with all of these, not replaces any.
2. **Feature definitions change infrequently (days/weeks), feature values change constantly (seconds/hours).** The registry is low-write, the stores are high-write.
3. **Point-in-time correctness is a hard invariant, not a best-effort goal.** A training dataset that leaks future information is worse than useless -- it produces a model that appears to perform well offline but degrades in production, and the degradation is silent and difficult to diagnose.
4. **Training-serving skew must be structurally impossible.** The same feature definition, executed by the same computation engine, must produce identical values for both training and serving paths. "We test for skew" is not acceptable; "skew cannot occur by construction" is.
5. **The online store is on the critical path of every ML prediction.** Its availability requirement (99.99%) is higher than typical infrastructure because a feature store outage degrades every model simultaneously, not just one.

### What We Are Explicitly *Not* Promising

- Not sub-millisecond feature serving -- the 10ms P99 budget is generous enough for a network hop to a key-value store, but not for in-process serving (that would require co-located caches, a v2 optimization).
- Not cross-entity transactional consistency -- features for user_123 and item_456 may be at slightly different pipeline timestamps within the freshness SLA. The consistency guarantee is per-entity: all features for a single entity in one response are from the same materialization epoch.
- Not real-time backfill -- backfilling 3 years of history for a new feature takes hours to days, not seconds. The platform optimizes for correctness and incremental progress, not speed.
- Not arbitrary aggregation windows at serving time -- time-windowed aggregations are pre-computed at defined intervals (1h, 7d, 30d, 90d). An ad-hoc "last 13 days" window requires a new feature definition, not a query parameter.

---

## 2. Capacity Estimates

### Online Store Sizing

The single largest infrastructure decision. Do the arithmetic first, then choose the technology.

```
Entities:              1,000,000,000 (1B)
Features per entity:   200 (average across all feature groups)
Avg value size:        50 bytes (weighted: 70% scalars @ 16B, 20% small
                       lists @ 100B, 10% embeddings @ 800B)

Raw feature data per entity:  200 * 50 B = 10,000 B = 10 KB
Total raw feature data:       1B * 10 KB = 10 TB

With metadata overhead per key (entity_id + feature_name + timestamp
+ TTL + encoding overhead): ~40% overhead
Effective per entity:         10 KB * 1.4 = 14 KB
Total with metadata:          1B * 14 KB = 14 TB

With replication (3x for Redis Cluster, or built-in for DynamoDB):
  Redis Cluster: 14 TB * 3 = 42 TB of RAM across the cluster
  DynamoDB:      14 TB stored (replication is managed, not user-visible)
  Bigtable:      14 TB stored (replication managed per zone)
```

**42 TB of RAM for Redis Cluster is feasible but expensive.** At ~$5/GB-month for memory-optimized instances (e.g., r6g.16xlarge with 512 GB RAM), that's 84 nodes * $5 * 512 = ~$215,000/month for Redis. DynamoDB at ~$0.25/GB-month for on-demand storage is ~$3,500/month for storage alone, but read costs dominate at 500K QPS (computed below). Bigtable is similar in shape. These numbers recur in the trade-off analysis (section 17).

### Online Serving Throughput

```
Sustained QPS:         500,000 lookups/sec
Peak QPS:              1,000,000 lookups/sec

Per lookup:            1 entity * 200 features = 200 key-value reads
                       (or 1 wide-row read if stored as a single row)

If stored as individual keys (Redis hash fields):
  500K lookups * 1 HGETALL per lookup = 500K commands/sec
  Each returning ~10 KB payload (200 features * 50B avg)
  Bandwidth: 500K * 10 KB = 5 GB/sec sustained

If stored as wide rows (DynamoDB single-item / Bigtable single-row):
  500K point-reads/sec, each returning ~10 KB
  Same 5 GB/sec bandwidth

Batch lookups (500 entities):
  500 * 10 KB = 5 MB per batch request
  At P99 <= 25ms, need parallel fan-out across shards
```

### Streaming Pipeline Throughput

```
Streaming feature pipelines:  100 concurrent
Events per pipeline (avg):    10,000 events/sec
Total ingest rate:             1,000,000 events/sec across all pipelines

Per event processing:
  Deserialize + validate:     ~0.1 ms
  Windowed aggregation:       ~0.5 ms (amortized, state lookup + update)
  Dual-write (online + log):  ~2 ms (async, batched)

Flink cluster sizing:
  At 10K events/sec/TaskManager (conservative, depends on state size):
  100 pipelines * 10K events/sec / 10K events/sec/TM = 100 TaskManagers
  With 2x headroom for rebalancing and checkpointing: ~200 TaskManagers
  Each with 4 CPU cores + 16 GB memory = 800 cores, 3.2 TB total memory
```

### Batch Computation

```
Largest feature group:  covers 1B entities, daily materialization
Source data per run:    ~500 GB (one day of transaction data)
Spark cluster:          50 executors * 8 cores * 32 GB = 400 cores, 1.6 TB

Processing time target: <= 2 hours
Throughput needed:      500 GB / 2h = ~70 MB/sec sustained read + compute

Output per run:         1B entities * 200 bytes (subset of features) = 200 GB
Written to:
  - Offline store (Delta Lake):  200 GB Parquet, partitioned
  - Online store (bulk load):    200 GB pushed via bulk-write API

Daily compute cost:
  50 * m5.4xlarge-equivalent * 2h * $0.77/hr = ~$77/day = ~$2,300/month
  (for the single largest feature group; total across all groups: ~$10K/month)
```

### Offline Store Sizing

```
Historical feature data:  3 years retention
Daily materialization:    ~200 GB/day (all feature groups combined)
Streaming log:            ~50 GB/day (dual-write from streaming pipelines)

Total over 3 years:       (200 + 50) GB/day * 365 * 3 = ~274 TB
With Delta Lake overhead (versioning, transaction log): ~300 TB

Compressed (Parquet, typical 3-5x compression): ~60-100 TB on S3
At $0.023/GB-month: ~$1,500-2,300/month for storage
```

---

## 3. High-Level Architecture

```
                             ┌────────────────────────────────┐
                             │       Control Plane             │
                             │  Feature Registry / Metadata    │
                             │  Definition DSL / Schema / ACL  │
                             │  Lineage Graph / Deprecation    │
                             └──────────────┬─────────────────┘
                                            │ definitions, configs
        ┌───────────────────────────────────┼──────────────────────────────────┐
        │                    OFFLINE PLANE (Batch)                             │
        │                                                                      │
        │  ┌─────────────┐   ┌──────────────────┐   ┌───────────────────────┐  │
        │  │  Scheduler   │──▶│  Spark / SQL      │──▶│   Offline Store       │  │
        │  │ (Airflow)    │   │  Batch Compute    │   │  (Delta Lake on S3)   │  │
        │  │              │   │  Engine            │   │  Partitioned by       │  │
        │  │  - cron      │   │                    │   │  entity + date        │  │
        │  │  - trigger   │   │  - full recompute  │   │                       │  │
        │  │  - backfill  │   │  - incremental     │   │  Point-in-time        │  │
        │  └──────────────┘   │  - backfill        │   │  queryable            │  │
        │                      └────────┬──────────┘   └───────────┬───────────┘  │
        │                               │ bulk write                │              │
        │                               ▼                           │              │
        │                      ┌────────────────┐                  │              │
        │                      │  Online Store   │◀─────────────────┘              │
        │                      │  (bulk load     │  (training dataset              │
        │                      │   from batch)   │   generation reads              │
        │                      └────────┬────────┘   from offline store)           │
        └───────────────────────────────┼──────────────────────────────────────────┘
                                        │
        ┌───────────────────────────────┼──────────────────────────────────────────┐
        │                    ONLINE PLANE (Streaming + Serving)                     │
        │                                                                           │
        │  ┌────────────┐   ┌──────────────────┐   ┌──────────────────────────┐    │
        │  │  Kafka      │──▶│  Flink Streaming  │──▶│   Online Store           │    │
        │  │  (source    │   │  Compute Engine   │   │  (Redis Cluster)         │    │
        │  │   events)   │   │                    │   │                          │    │
        │  │             │   │  - windowed aggs   │   │  Entity-keyed, latest    │    │
        │  │             │   │  - exactly-once    │   │  feature values          │    │
        │  │             │   │  - watermarks      │   │                          │    │
        │  └─────────────┘   └───────┬──────────┘   └────────────┬─────────────┘    │
        │                            │                            │                  │
        │                            │ dual-write log             │ read             │
        │                            ▼                            ▼                  │
        │                   ┌────────────────┐         ┌──────────────────────┐      │
        │                   │  Offline Store  │         │  Feature Serving API  │      │
        │                   │  (streaming     │         │  (gRPC / REST)       │      │
        │                   │   feature log)  │         │                      │      │
        │                   └────────────────┘         │  - single entity     │      │
        │                                              │  - batch entity      │      │
        │                                              │  - on-demand xform   │      │
        │                                              └──────────────────────┘      │
        └───────────────────────────────────────────────────────────────────────────┘

        ┌───────────────────────────────────────────────────────────────────────────┐
        │                     QUALITY & GOVERNANCE PLANE                             │
        │  Feature Drift Monitor ── Schema Validator ── Freshness Tracker            │
        │  PII Enforcer ── Lineage Service ── Audit Logger                           │
        │  Training-Serving Skew Detector                                            │
        └───────────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Scaling Axis |
|---|---|---|
| Feature Registry | Feature CRUD, schema management, lineage, discovery, deprecation | Low QPS, strong consistency -- Postgres-backed |
| Batch Compute Engine | Spark/SQL jobs for scheduled feature materialization, backfill | Horizontal Spark executors; cluster-per-job or shared pool |
| Scheduler | Airflow DAGs: cron, data-arrival triggers, backfill orchestration | Low QPS, Airflow workers scale per concurrent DAG count |
| Streaming Compute Engine | Flink jobs for real-time feature computation from Kafka | Horizontal TaskManagers per pipeline; state on RocksDB |
| Online Store | Low-latency feature serving; latest feature values per entity | Sharded by entity key; Redis Cluster or DynamoDB |
| Offline Store | Historical feature values; point-in-time queryable; training data | S3/Delta Lake; scales with storage, not compute |
| Feature Serving API | gRPC/REST endpoint for online lookups + on-demand transforms | Stateless, horizontal; colocated with online store |
| Quality & Governance | Drift detection, schema validation, PII enforcement, audit | Off critical path; batch + streaming monitors |

### Why This Shape

- **Offline and online planes are architecturally separate** -- they share feature definitions (from the registry) and data flow (batch writes to online store, dual-write from streaming), but are independently deployable and scalable. A batch pipeline failure does not affect online serving; a streaming lag does not corrupt offline training data.
- **The Feature Registry is the single source of truth for "what is a feature."** Every computation (batch or streaming) is derived from a registry definition. This is the structural mechanism that prevents training-serving skew -- not testing, not monitoring, but the fact that both paths read from the same definition.
- **The dual-write from streaming to offline is a log, not a direct table write.** Streaming features are appended to a Kafka topic that is then compacted into the offline store's Delta Lake tables by a separate consumer. This decouples streaming latency from offline write durability and allows the offline store to receive both batch and streaming data through a uniform append-only pattern.

---

## 4. Feature Registry and Definition Language

### Feature Definition DSL

Every feature in the platform is defined by a YAML document. This is the load-bearing contract between feature authors and the platform -- it specifies what to compute, from what data, how fresh it must be, and what invariants it must satisfy.

```yaml
# Feature group: user engagement signals
# Owner: recommendations-team
# Version: 3

feature_group:
  name: user_engagement_features
  entity: user
  entity_key: user_id
  owner: recommendations-team
  description: "User engagement signals for recommendation models"
  tags: ["engagement", "recommendations", "core"]

features:
  - name: txn_count_7d
    description: "Number of transactions in the last 7 days"
    value_type: int64
    default_value: 0

    computation:
      mode: streaming           # streaming | batch | on_demand
      source:
        type: kafka
        topic: transactions
        schema_registry: "sr://transactions-v2"
      expression: |
        SELECT
          user_id,
          COUNT(*) AS txn_count_7d
        FROM transactions
        WHERE event_time >= NOW() - INTERVAL '7' DAY
        GROUP BY user_id
      window:
        type: sliding
        size: 7d
        slide: 1h
      aggregation: count

    freshness_sla: 5s           # real-time tier
    serving:
      online: true
      offline: true

    quality:
      nullable: false
      range: [0, 100000]
      alert_on_null_rate_above: 0.01
      alert_on_value_drift: true

  - name: avg_txn_amount_30d
    description: "Average transaction amount in the last 30 days"
    value_type: float64
    default_value: 0.0

    computation:
      mode: batch
      source:
        type: delta_lake
        table: "warehouse.transactions"
      expression: |
        SELECT
          user_id,
          AVG(amount) AS avg_txn_amount_30d
        FROM warehouse.transactions
        WHERE txn_date >= DATE_SUB(CURRENT_DATE(), 30)
        GROUP BY user_id
      schedule: "0 6 * * *"     # daily at 6 AM UTC
      incremental:
        enabled: true
        watermark_column: txn_date
        merge_strategy: running_average  # platform-managed incremental

    freshness_sla: 24h          # batch tier
    serving:
      online: true
      offline: true

    quality:
      nullable: false
      range: [0.0, 1000000.0]
      alert_on_null_rate_above: 0.05

  - name: user_item_affinity
    description: "Dot product of user and item embedding vectors"
    value_type: float64

    computation:
      mode: on_demand            # computed at serving time
      expression: |
        DOT_PRODUCT(
          FEATURE('user_embedding_128d', request.user_id),
          FEATURE('item_embedding_128d', request.item_id)
        )
      dependencies:
        - user_embedding_128d
        - item_embedding_128d

    freshness_sla: null          # computed on every request
    serving:
      online: true
      offline: false             # not pre-materialized

  - name: user_embedding_128d
    description: "User preference embedding from the collaborative filtering model"
    value_type: embedding
    embedding_dim: 128

    computation:
      mode: batch
      source:
        type: model_output
        pipeline: "user-embedding-pipeline-v3"
        output_path: "s3://ml-artifacts/user-embeddings/latest/"
      schedule: "0 4 * * *"     # daily at 4 AM UTC

    freshness_sla: 24h
    serving:
      online: true
      offline: true

    quality:
      embedding_norm_range: [0.8, 1.2]   # L2 norm should be near 1.0

governance:
  pii: false
  access:
    read: ["recommendations-team", "fraud-team", "risk-team"]
    write: ["recommendations-team"]
  deprecation: null              # not deprecated
```

### Why YAML and Not Code

The definition is declarative, not imperative. A Flink job or Spark SQL query is derived *from* this definition by the platform's code generators -- the user never writes or manages infrastructure code directly. This is the mechanism that makes training-serving consistency structural: the same YAML produces the same SQL for both the batch path (offline training data) and the streaming path (online serving), with the platform responsible for the translation, not the user.

The `expression` field contains SQL intentionally -- it's the one language that both Spark (batch) and Flink (streaming) can execute natively. Features requiring non-SQL logic (custom Python UDFs, complex ML preprocessing) register the UDF in the platform's UDF registry and reference it by name in the SQL expression; the UDF jar/wheel is then deployed to both Spark and Flink runtimes identically.

### Feature Registry: Catalog and Discovery

The registry is a metadata store (Postgres-backed) exposing:

```
Search by:     name, entity type, owner team, tag, data source,
               freshness tier, value type, consumption (which models use it)
List:          all features for entity "user", all features owned by "fraud-team"
Lineage:       upstream (what data sources) and downstream (what models)
Impact:        "if transactions topic schema changes, which features are affected?"
Deprecation:   sunset date, replacement feature, consuming model alerts
```

```python
class FeatureRegistry:
    def register(self, definition: FeatureGroupDef) -> FeatureGroupVersion:
        """Validate, version, and store a feature group definition.
        Triggers: schema compatibility check against the previous version,
        impact analysis against consuming models, and (if computation
        changed) a backfill eligibility check."""
        self._validate_schema(definition)
        self._check_backward_compatibility(definition)
        version = self._store_versioned(definition)
        self._notify_downstream_consumers(version)
        return version

    def get_feature_definition(self, name: str, version: int = None) -> FeatureDef:
        """Retrieve a specific version (or latest) of a feature definition.
        Both batch and streaming compute engines call this at job startup
        to get the canonical definition they should execute."""
        ...

    def search(self, query: str, filters: dict) -> list[FeatureSummary]:
        """Full-text + structured search across all registered features."""
        ...

    def lineage(self, feature_name: str) -> LineageGraph:
        """Return the full upstream (sources) and downstream (models, feature
        groups) dependency graph for a feature."""
        ...
```

### Feature Group Versioning

Feature groups are versioned as a unit. A version bump is required when:
- Any feature's computation logic changes (new SQL expression)
- A feature is added or removed from the group
- Schema changes (value type, nullability)

Schema evolution rules:
- **Backward compatible** (safe): adding a new feature to a group, widening a numeric type (int32 -> int64), relaxing nullability (non-null -> nullable).
- **Breaking** (requires backfill): changing computation logic, narrowing a type, changing entity key. Breaking changes trigger a mandatory backfill of historical data before the new version activates for training, but the old version continues serving online until the backfill completes and is validated.

---

## 5. Batch Computation Pipeline

### Orchestration

Batch feature computation is orchestrated by Airflow, with one DAG per feature group. The DAG structure:

```
┌─────────────────┐     ┌───────────────────┐     ┌────────────────────┐
│  Sensor:         │────▶│  Compute:          │────▶│  Validate:          │
│  data arrival    │     │  Spark/SQL job     │     │  schema + quality   │
│  or cron trigger │     │  (full or          │     │  checks on output   │
│                  │     │   incremental)     │     │                    │
└─────────────────┘     └────────┬───────────┘     └─────────┬──────────┘
                                 │                            │
                                 │                   pass     │ fail
                                 │                   ┌────────┴───────┐
                                 │                   ▼                ▼
                        ┌────────┴──────────┐  ┌──────────┐   ┌────────────┐
                        │  Materialize:      │  │ Write to │   │ Alert +    │
                        │  write to offline  │  │ online   │   │ rollback   │
                        │  store (Delta)     │  │ store    │   │ to previous│
                        └───────────────────┘  └──────────┘   │ version    │
                                                               └────────────┘
```

**Data-arrival triggers** use Airflow sensors that poll for new partitions in the source Delta Lake table (e.g., `warehouse.transactions/txn_date=2024-08-12/` appears). This is preferred over pure cron for features derived from warehouse tables, because a cron-triggered job that runs before its source data lands produces stale or empty results silently -- the sensor makes the dependency explicit and blocks until the upstream is ready.

### Full Recompute vs. Incremental

Two modes, selected per feature definition:

**Full recompute** -- the default for non-additive aggregations (percentiles, count_distinct, median) and for features with complex join logic:

```python
def batch_compute_full(feature_def: FeatureDef, execution_date: date) -> DataFrame:
    """Recompute the feature from scratch over the full window."""
    spark = get_spark_session()

    # The SQL expression from the YAML definition, with date substitution
    sql = feature_def.computation.expression.replace(
        "CURRENT_DATE()", f"DATE('{execution_date}')"
    )
    result = spark.sql(sql)

    # Attach metadata: which definition version, execution timestamp
    result = result.withColumn("_feature_timestamp", lit(datetime.utcnow()))
    result = result.withColumn("_feature_version", lit(feature_def.version))

    return result
```

**Incremental computation** -- for additive aggregations (sum, count, avg via sum+count) where maintaining running state avoids reprocessing the full window:

```python
def batch_compute_incremental(
    feature_def: FeatureDef,
    execution_date: date,
    previous_state: DataFrame
) -> DataFrame:
    """Process only new data since the last watermark, merge with running state."""
    spark = get_spark_session()

    # Read only new source data since last watermark
    watermark_col = feature_def.computation.incremental.watermark_column
    last_watermark = get_last_watermark(feature_def.name)

    new_data = spark.read.format("delta").table(
        feature_def.computation.source.table
    ).filter(
        col(watermark_col) > last_watermark
    ).filter(
        col(watermark_col) <= execution_date
    )

    if new_data.count() == 0:
        return previous_state  # nothing new, carry forward

    # Compute partial aggregates on new data only
    partial = new_data.groupBy(feature_def.entity_key).agg(
        F.sum("amount").alias("_partial_sum"),
        F.count("*").alias("_partial_count")
    )

    # Merge with running state
    merged = previous_state.alias("old").join(
        partial.alias("new"),
        on=feature_def.entity_key,
        how="full_outer"
    ).select(
        coalesce(col("old." + feature_def.entity_key),
                 col("new." + feature_def.entity_key)).alias(feature_def.entity_key),
        (coalesce(col("old._running_sum"), lit(0)) +
         coalesce(col("new._partial_sum"), lit(0))).alias("_running_sum"),
        (coalesce(col("old._running_count"), lit(0)) +
         coalesce(col("new._partial_count"), lit(0))).alias("_running_count"),
    )

    # Derive the final feature value from running state
    result = merged.withColumn(
        feature_def.features[0].name,
        col("_running_sum") / col("_running_count")  # avg = sum/count
    )

    # Window expiry: subtract contributions from data that aged out
    # of the 30-day window. This is the hard part of incremental --
    # for sliding windows, we also need to read the data that just
    # EXITED the window and subtract its partial aggregates.
    expired_data = spark.read.format("delta").table(
        feature_def.computation.source.table
    ).filter(
        (col(watermark_col) >= (execution_date - timedelta(days=31))) &
        (col(watermark_col) < (execution_date - timedelta(days=30)))
    )

    if expired_data.count() > 0:
        expired_partial = expired_data.groupBy(feature_def.entity_key).agg(
            F.sum("amount").alias("_expired_sum"),
            F.count("*").alias("_expired_count")
        )
        result = result.join(expired_partial, on=feature_def.entity_key, how="left")
        result = result.withColumn(
            "_running_sum", col("_running_sum") - coalesce(col("_expired_sum"), lit(0))
        ).withColumn(
            "_running_count", col("_running_count") - coalesce(col("_expired_count"), lit(0))
        )

    update_watermark(feature_def.name, execution_date)
    return result
```

The **window expiry subtraction** is the hard part of incremental computation for sliding windows. For tumbling windows (non-overlapping), expiry is simpler: discard the old window's state entirely. For sliding windows, we must subtract the exiting data's contribution. This only works for decomposable aggregations (sum, count, avg); non-decomposable aggregations (percentile, count_distinct) cannot be incrementally maintained this way and must use full recompute.

### Backfill

When a feature definition changes (new computation logic, bug fix), historical feature values need recomputation. Backfill runs the new definition against historical source data, filling the offline store without disrupting current serving:

```python
def backfill(
    feature_def: FeatureDef,
    start_date: date,
    end_date: date,
    parallelism: int = 10
) -> BackfillResult:
    """Recompute historical feature values for a date range.

    Key invariants:
    1. Backfill writes to a STAGING partition, not directly to the
       production offline store. Only after validation does the staging
       data get promoted to production.
    2. Online store is NOT updated by backfill -- it always serves the
       latest value. Backfill is purely an offline-store operation.
    3. Backfill can be interrupted and resumed: each date partition is
       an independent unit of work, and completed partitions are
       checkpointed.
    """
    completed = get_backfill_checkpoint(feature_def.name, start_date, end_date)
    remaining_dates = [
        d for d in date_range(start_date, end_date)
        if d not in completed
    ]

    for batch in chunk(remaining_dates, parallelism):
        # Process multiple dates in parallel
        futures = []
        for d in batch:
            futures.append(submit_spark_job(
                feature_def=feature_def,
                execution_date=d,
                output_path=staging_path(feature_def, d),
            ))

        # Wait for batch, validate each
        for future, d in zip(futures, batch):
            result = future.result()
            validation = validate_feature_output(result, feature_def)
            if validation.passed:
                promote_staging_to_production(feature_def, d)
                checkpoint_backfill(feature_def, d)
            else:
                log_backfill_failure(feature_def, d, validation.errors)
                # Continue with next dates -- one failed date doesn't
                # block the rest

    return BackfillResult(
        total_dates=len(date_range(start_date, end_date)),
        completed=len(completed) + count_newly_completed,
        failed=count_failures,
    )
```

Backfill jobs run at **lower priority** than production batch jobs (Spark dynamic resource allocation, or separate job queues) so a large backfill cannot delay today's daily feature computation.

### Materialization to Both Stores

After batch computation completes and passes validation:

1. **Offline store**: Spark writes Parquet files directly to the Delta Lake table, partitioned by `(entity_type, date)`. This is an atomic `MERGE INTO` or `INSERT OVERWRITE` on the date partition, using Delta Lake's ACID transactions.

2. **Online store**: A separate materialization job reads the batch output and bulk-writes to the online store. For Redis Cluster, this uses pipelined `HSET` commands; for DynamoDB, `BatchWriteItem`. The bulk write is rate-limited to avoid overwhelming the online store during the daily materialization window.

```python
def materialize_to_online_store(
    batch_output: DataFrame,
    feature_group: str,
    online_store: OnlineStoreClient
):
    """Bulk-write batch results to the online store.

    Uses micro-batching to control write throughput and avoid
    overwhelming the online store during the daily materialization.
    """
    BATCH_SIZE = 1000
    RATE_LIMIT = 50000  # writes/sec to online store

    for micro_batch in batch_output.toLocalIterator(prefetchPartitions=True):
        entities = collect_batch(micro_batch, BATCH_SIZE)
        online_store.bulk_write(
            feature_group=feature_group,
            entities=entities,
            # Timestamp from batch computation, not from write time --
            # this is what makes consistency checking possible (section 11)
            timestamp=batch_output.metadata.execution_timestamp,
        )
        rate_limiter.acquire(len(entities))
```

---

## 6. Streaming Computation Pipeline

### Flink Topology

Each streaming feature pipeline is a Flink job derived from the feature definition YAML. The platform generates the Flink SQL or DataStream API code from the definition; the user never writes Flink code directly.

```
Kafka Source ──▶ Deserialize ──▶ Validate ──▶ Windowed ──▶ Dual-Write
(events)         + Schema         + Filter     Aggregation   Sink
                 Check                         (with         ├──▶ Online Store (Redis)
                                               watermarks)   └──▶ Offline Log (Kafka)
                                                                    └──▶ Delta Lake
                                                                        (compaction job)
```

### Flink Streaming Job: Concrete Implementation

```java
public class FeaturePipelineJob {

    public static void main(String[] args) throws Exception {
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();

        // Exactly-once semantics: Flink checkpointing + Kafka transactions
        env.enableCheckpointing(30_000, CheckpointingMode.EXACTLY_ONCE);
        env.getCheckpointConfig().setMinPauseBetweenCheckpoints(10_000);
        env.getCheckpointConfig().setCheckpointTimeout(120_000);
        env.getCheckpointConfig().setMaxConcurrentCheckpoints(1);

        // State backend: RocksDB for large windowed state
        env.setStateBackend(new EmbeddedRocksDBStateBackend());
        env.getCheckpointConfig().setCheckpointStorage(
            "s3://flink-checkpoints/user-engagement-features/"
        );

        // Kafka source with event-time semantics
        KafkaSource<Transaction> source = KafkaSource.<Transaction>builder()
            .setBootstrapServers("kafka:9092")
            .setTopics("transactions")
            .setGroupId("feature-pipeline-user-engagement-v3")
            .setStartingOffsets(OffsetsInitializer.committedOffsets(
                OffsetResetStrategy.LATEST))
            .setDeserializer(new TransactionDeserializer())
            .build();

        DataStream<Transaction> events = env.fromSource(
            source,
            WatermarkStrategy
                .<Transaction>forBoundedOutOfOrderness(Duration.ofSeconds(10))
                .withTimestampAssigner((txn, ts) -> txn.getEventTime())
                .withIdleness(Duration.ofMinutes(1)),
            "transactions-source"
        );

        // Validate incoming events (schema check, null handling)
        DataStream<Transaction> validated = events
            .filter(new SchemaValidator())
            .name("schema-validation");

        // Sliding window aggregation: 7-day window, 1-hour slide
        DataStream<FeatureValue> aggregated = validated
            .keyBy(Transaction::getUserId)
            .window(SlidingEventTimeWindows.of(
                Time.days(7),
                Time.hours(1)
            ))
            .allowedLateness(Time.minutes(5))
            .sideOutputLateData(lateOutputTag)
            .aggregate(
                new TransactionCountAggregate(),
                new FeatureWindowFunction("txn_count_7d")
            )
            .name("txn-count-7d-aggregation");

        // Handle late-arriving data
        DataStream<Transaction> lateData = aggregated
            .getSideOutput(lateOutputTag);
        lateData.addSink(new LateDataMetricsSink())
            .name("late-data-metrics");

        // Dual-write: online store + offline log
        // CRITICAL: both writes are part of the same Flink checkpoint
        // boundary, ensuring exactly-once delivery to both sinks

        // Sink 1: Online store (Redis)
        aggregated.addSink(new RedisSink<>(
            new FlinkJedisClusterConfig.Builder()
                .setNodes(redisNodes)
                .setMaxTotal(128)
                .build(),
            new FeatureValueRedisMapper()
        )).name("redis-online-store-sink");

        // Sink 2: Offline log (Kafka topic, later compacted to Delta Lake)
        aggregated.sinkTo(
            KafkaSink.<FeatureValue>builder()
                .setBootstrapServers("kafka:9092")
                .setRecordSerializer(new FeatureValueKafkaSerializer(
                    "feature-offline-log"))
                .setDeliveryGuarantee(DeliveryGuarantee.EXACTLY_ONCE)
                .setTransactionalIdPrefix("feature-pipeline-user-engagement")
                .build()
        ).name("kafka-offline-log-sink");

        env.execute("user-engagement-features-v3");
    }
}
```

### Exactly-Once Semantics: How It Actually Works

The exactly-once guarantee spans three systems: Kafka (source), Flink (processing), and the dual-write sinks (Redis + Kafka). Here is how each boundary is protected:

```
Kafka Source ──── Flink Processing ──── Kafka Sink (offline log)
    │                    │                      │
    │  consumer offsets   │  checkpointed state  │  Kafka transactions
    │  committed at       │  in RocksDB,         │  committed at
    │  checkpoint         │  snapshotted to S3   │  checkpoint
    │  barrier            │  at barrier           │  barrier
    │                    │                      │
    └────────────────────┴──────────────────────┘
    All three committed atomically at each checkpoint

Redis Sink (online store):
    │
    │  NOT transactional with Kafka -- Redis does not participate
    │  in Flink's two-phase commit protocol.
    │
    │  Instead: idempotent writes. Each feature value is written with
    │  a monotonic (event_time, window_end) version. The Redis write
    │  is a conditional SET that only succeeds if the incoming version
    │  is >= the stored version. A replay after checkpoint recovery
    │  re-writes the same value with the same version -- idempotent,
    │  no duplicate or lost update.
    │
    └── This is the standard pattern for exactly-once with a non-
        transactional external sink: make the write idempotent by
        including a deterministic version derived from the event
        time, not the processing time.
```

The **idempotent Redis write** implementation:

```python
class FeatureValueRedisMapper(RedisMapper):
    def get_command(self, feature_value: FeatureValue) -> RedisCommand:
        key = f"features:{feature_value.entity_type}:{feature_value.entity_id}"
        field = feature_value.feature_name
        # Value includes version for idempotency check
        value = encode(feature_value.value, feature_value.version)

        # Lua script: write only if incoming version >= stored version
        lua = """
        local current = redis.call('HGET', KEYS[1], ARGV[1])
        if current == false then
            redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
            return 1
        end
        local current_version = decode_version(current)
        local incoming_version = decode_version(ARGV[2])
        if incoming_version >= current_version then
            redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
            return 1
        end
        return 0
        """
        return RedisCommand.eval(lua, key, field, value)
```

### Watermark and Late Data Handling

**Watermarks** are Flink's mechanism for tracking event-time progress. The watermark `W` at time `t` asserts: "no events with timestamp < W will arrive after this point." Late events (timestamp < current watermark) are handled per the feature definition's policy:

| Policy | Behavior | When Used |
|---|---|---|
| `drop` | Late events are discarded; a `late_events_dropped` counter is incremented | Features where freshness of the aggregate matters more than perfect accuracy (e.g., real-time CTR) |
| `update` | Late events trigger a re-emission of the affected window's aggregate, which overwrites the previous value in both stores | Features where accuracy of the aggregate matters more than stable serving values (e.g., fraud signals) |
| `side_output` | Late events are routed to a dead-letter topic for offline reconciliation | Features with strict audit requirements |

The **10-second bounded out-of-orderness** watermark from the code above means: the platform tolerates events arriving up to 10 seconds after their event timestamp before considering them "late." The `allowedLateness(5 minutes)` window means: even after the watermark has passed a window's end, events arriving within 5 more minutes still update the window result. Beyond 5 minutes, the `lateOutputTag` side output captures them.

### Dual-Write Architecture

The dual-write ensures every streaming feature value reaches both the online store (for serving) and the offline store (for future training data). The architecture:

```
Flink Aggregation Output
         │
         ├──▶ Redis (online store): immediate write, idempotent
         │    Latency: ~2ms per write, batched
         │
         └──▶ Kafka topic "feature-offline-log": exactly-once via
              Flink's Kafka transaction protocol
              │
              └──▶ Delta Lake Compaction Job (separate process):
                   Reads from Kafka topic, compacts into Delta Lake
                   Parquet files every 15 minutes.
                   │
                   └── Offline store now contains the same feature
                       values that were served online, with the same
                       event-time timestamps -- this is what makes
                       point-in-time joins (section 9) work for
                       streaming features.
```

The **compaction job** that drains the Kafka offline log into Delta Lake:

```python
def compact_feature_log_to_delta(
    kafka_topic: str = "feature-offline-log",
    delta_table: str = "feature_store.streaming_features",
    interval_minutes: int = 15,
):
    """Micro-batch consumer: reads accumulated feature values from
    the Kafka log and appends them to the Delta Lake offline store.

    Runs as a Spark Structured Streaming job with a trigger interval,
    not a continuous Flink job -- this intentionally decouples the
    offline store's write cadence from the streaming pipeline's
    event-by-event processing."""

    spark = get_spark_session()

    stream = spark.readStream \
        .format("kafka") \
        .option("kafka.bootstrap.servers", "kafka:9092") \
        .option("subscribe", kafka_topic) \
        .option("startingOffsets", "earliest") \
        .option("failOnDataLoss", "false") \
        .load()

    parsed = stream.select(
        from_json(col("value").cast("string"), feature_value_schema).alias("fv")
    ).select(
        col("fv.entity_type"),
        col("fv.entity_id"),
        col("fv.feature_name"),
        col("fv.value"),
        col("fv.event_time").alias("feature_timestamp"),
        col("fv.version"),
    )

    # Write to Delta Lake, partitioned by entity_type and date
    parsed.writeStream \
        .format("delta") \
        .outputMode("append") \
        .option("checkpointLocation", f"s3://checkpoints/{kafka_topic}/") \
        .partitionBy("entity_type", "date") \
        .trigger(processingTime=f"{interval_minutes} minutes") \
        .start(delta_table)
```

---

## 7. Online Store Design

### Storage Engine Comparison

The online store must serve P99 <= 10ms at 500K QPS for single-entity lookups (200 features per entity). Three candidates:

| Criterion | Redis Cluster | DynamoDB | Bigtable |
|---|---|---|---|
| **Latency (P99)** | 1-3 ms (in-memory) | 5-10 ms (SSD, single-digit) | 5-15 ms (SSD) |
| **Throughput** | ~100K ops/sec/node (pipelined) | Virtually unlimited (auto-scales) | ~10K rows/sec/node (scales with nodes) |
| **Storage model** | Hash: entity_id -> {feature: value} | Item: (entity_id, feature_group) -> {features} | Row: entity_id -> column_family:features |
| **Cost (14 TB data, 500K QPS)** | ~$215K/month (84 nodes x 512GB RAM, 3x replication) | ~$90K/month ($3.5K storage + ~$85K read capacity at provisioned pricing) | ~$45K/month (~300 nodes at SSD tier) |
| **Operational burden** | Medium: cluster management, resharding, failover | Low: fully managed | Low-Medium: managed, but capacity planning needed |
| **Hot key handling** | Manual: hash tags, read replicas | Adaptive capacity, auto-split | Auto-splits hot tablets |
| **Batch write** | Pipeline HSET, ~500K writes/sec sustained | BatchWriteItem, 25 items/call, auto-throttle | Batch mutations, ~100K rows/sec |

### Decision: Redis Cluster (Primary) with DynamoDB (Fallback Tier)

**Redis Cluster** is the primary online store because:

1. **Latency margin**: P99 of 1-3ms leaves 7-9ms of budget for network hop, serialization, on-demand transforms, and application overhead. DynamoDB's 5-10ms P99 leaves almost no margin.
2. **Batch lookup efficiency**: `HGETALL` on a Redis hash returns all 200 features in a single round-trip. DynamoDB would require a `GetItem` returning a ~10 KB item, which is fine, but Redis's in-memory speed advantage compounds when doing 500-entity batch lookups in parallel.
3. **The cost is high but justified** -- this store is on the critical path of every ML prediction in the company. A 10ms P99 at 99.99% availability is worth $215K/month when the alternative is degrading every model's serving latency.

**DynamoDB as fallback tier** for:
- Entities with infrequently accessed features (cold entities not looked up in the last 7 days) -- automatically tiered by a TTL-based eviction from Redis + lazy-load from DynamoDB on miss.
- Disaster recovery: DynamoDB serves as a warm standby if the Redis cluster has a multi-node failure.

### Redis Cluster Data Model

```
Key:    features:{entity_type}:{entity_id}
Type:   Hash
Fields: {feature_name} -> {encoded_value}

Example:
  HSET features:user:u_12345 txn_count_7d "\x03\x00\x00\x00\x2a\x00..."
  HSET features:user:u_12345 avg_txn_amount_30d "\x04\x40\x59\x00\x00..."
  HSET features:user:u_12345 user_embedding_128d "\x05\x80\x02\x00..."

  HGETALL features:user:u_12345
  → returns all 200 features in one round-trip
```

The value encoding includes:
- 1 byte: value type tag (int64=3, float64=4, embedding=5, ...)
- 8 bytes: feature_timestamp (millis since epoch) -- when the value was computed
- 4 bytes: feature_version (definition version that produced it)
- N bytes: the actual value (8 bytes for scalars, 512+ bytes for embeddings)

The timestamp and version in the value are essential for:
1. **Consistency checking**: the serving API can verify all features for an entity were produced by the same (or compatible) pipeline run.
2. **Idempotent writes**: streaming writes use the timestamp as a version guard (section 6).
3. **Staleness detection**: the serving API can check if a feature is older than its freshness SLA and log/alert accordingly.

### Cluster Topology

```
42 TB RAM / 512 GB per node = 84 nodes minimum (3x replication included)
Redis Cluster: 16,384 hash slots distributed across 28 primary shards
  (28 primaries x 2 replicas = 84 nodes total)

Each primary: ~500 GB data (14 TB / 28 shards)
Each primary handles: 500K QPS / 28 shards ≈ 18K QPS per shard
  (well within Redis's ~100K ops/sec/node for HGETALL on 10KB hashes)

Entity-to-shard mapping: CRC16(entity_id) mod 16384 → hash slot → primary
  This ensures all features for one entity live on the same shard,
  so HGETALL is always a local operation -- no cross-shard coordination.
```

### Batch Lookup Implementation

For a batch lookup of 500 entities (e.g., scoring 500 candidate items):

```python
async def batch_get_features(
    entity_type: str,
    entity_ids: list[str],
    feature_names: list[str] = None,  # None = all features
    max_staleness: timedelta = None,
) -> dict[str, dict[str, FeatureValue]]:
    """Fetch features for multiple entities in a single call.

    Implementation: partition entity_ids by their Redis shard,
    issue one PIPELINE per shard (not one HGETALL per entity),
    and collect results. This minimizes round-trips to ceil(N_shards)
    regardless of entity count."""

    # Partition entities by shard
    shard_groups = defaultdict(list)
    for eid in entity_ids:
        slot = crc16(f"features:{entity_type}:{eid}") % 16384
        shard = slot_to_shard[slot]
        shard_groups[shard].append(eid)

    # Issue pipelined commands to each shard concurrently
    async def fetch_shard(shard, eids):
        pipe = redis_cluster.pipeline(shard)
        for eid in eids:
            key = f"features:{entity_type}:{eid}"
            if feature_names:
                pipe.hmget(key, *feature_names)
            else:
                pipe.hgetall(key)
        return await pipe.execute()

    shard_results = await asyncio.gather(*[
        fetch_shard(shard, eids)
        for shard, eids in shard_groups.items()
    ])

    # Reassemble results, check staleness
    results = {}
    for shard_eids, shard_vals in zip(shard_groups.values(), shard_results):
        for eid, vals in zip(shard_eids, shard_vals):
            decoded = decode_feature_values(vals)
            if max_staleness:
                check_staleness(decoded, max_staleness)
            results[eid] = decoded

    return results
```

**Latency math for 500-entity batch lookup:**
- 500 entities across 28 shards = ~18 entities per shard on average
- One pipeline per shard: 28 pipelines issued concurrently
- Per pipeline: 18 HGETALL commands, pipelined = 1 round-trip
- Network round-trip to Redis: ~1ms within the same AZ
- Redis processing: ~0.5ms for 18 HGETALL on 10KB hashes
- Total: ~2-3ms for all 28 shards in parallel, well within 25ms P99 budget

---

## 8. Offline Store Design

### Data Layout

The offline store is a Delta Lake table on S3, partitioned for efficient point-in-time queries:

```
s3://feature-store/offline/
  ├── batch_features/                      # From batch computation
  │   ├── entity_type=user/
  │   │   ├── date=2024-08-01/
  │   │   │   ├── feature_group=user_engagement_features/
  │   │   │   │   ├── part-00000.parquet
  │   │   │   │   ├── part-00001.parquet
  │   │   │   │   └── ...
  │   │   │   └── feature_group=user_profile_features/
  │   │   │       └── ...
  │   │   ├── date=2024-08-02/
  │   │   │   └── ...
  │   │   └── ...
  │   └── entity_type=item/
  │       └── ...
  │
  └── streaming_features/                  # From dual-write log
      ├── entity_type=user/
      │   ├── date=2024-08-12/
      │   │   ├── hour=14/
      │   │   │   ├── part-00000.parquet   # 15-min micro-batch compactions
      │   │   │   └── ...
      │   │   └── hour=15/
      │   │       └── ...
      │   └── ...
      └── ...
```

### Parquet Schema

```
batch_features table:
  entity_id:          STRING          (partition pruning via Z-ORDER)
  feature_name:       STRING
  feature_value:      BINARY          (type-tagged encoded value)
  feature_timestamp:  TIMESTAMP       (when this value was computed)
  feature_version:    INT             (definition version)
  created_at:         TIMESTAMP       (when this row was written)

streaming_features table:
  entity_id:          STRING
  feature_name:       STRING
  feature_value:      BINARY
  event_time:         TIMESTAMP       (original event time from source)
  feature_timestamp:  TIMESTAMP       (when Flink emitted this value)
  window_start:       TIMESTAMP       (aggregation window start)
  window_end:         TIMESTAMP       (aggregation window end)
  feature_version:    INT
  created_at:         TIMESTAMP
```

### Partitioning Strategy

The partitioning serves two conflicting access patterns:

1. **Batch materialization** writes: whole-entity-type, whole-date partitions. Needs write-efficient partitioning (one write per date partition).
2. **Point-in-time join** reads: specific entities at specific timestamps. Needs read-efficient partitioning (minimize data scanned for a single entity's history).

**Resolution**: partition by `(entity_type, date)` for write efficiency, then apply **Z-ORDER clustering** within each partition on `(entity_id, feature_name)` for read efficiency. Delta Lake's Z-ORDER indexing co-locates rows for the same entity_id physically on disk, so a point-in-time query for one entity reads a small fraction of the partition's files rather than scanning all of them.

```sql
-- After writing a new date partition, optimize it with Z-ORDER
OPTIMIZE feature_store.batch_features
  WHERE entity_type = 'user' AND date = '2024-08-12'
  ZORDER BY (entity_id, feature_name);
```

**Why not partition by entity_id?** With 1B entities, partitioning by entity_id would create billions of tiny partitions (each a few KB), which is catastrophically slow for both S3 listing and Spark planning. The Z-ORDER approach gives us the read-efficiency benefit of entity-level locality without the metadata explosion of entity-level partitioning.

### Delta Lake Time Travel for Point-in-Time Queries

Delta Lake's time travel feature (`VERSION AS OF` / `TIMESTAMP AS OF`) operates on the table's *transaction log*, not on the feature values' timestamps. This is a common confusion: Delta time travel answers "what did the table look like at write time T" not "what was the feature value at event time T." The point-in-time join (section 9) needs the latter, which requires an explicit temporal query on the `feature_timestamp` column, not Delta time travel.

However, Delta time travel is useful for **debugging and rollback**: if a batch pipeline writes incorrect values for date 2024-08-12, we can `RESTORE TABLE feature_store.batch_features TO VERSION AS OF <previous_version>` to undo the bad write atomically, then rerun the batch job.

### Retention and Compaction

- **Batch features**: retained for 3 years (the backfill window). Old partitions are compacted quarterly (merge small files, optimize Z-ORDER).
- **Streaming features**: retained for 1 year at full granularity (15-min compaction intervals), then downsampled to hourly snapshots for years 2-3. The hourly downsampling loses some point-in-time precision for very old data, which is acceptable because models rarely train on sub-hourly feature precision from 2+ years ago.
- **Delta Lake vacuum**: `VACUUM` runs weekly with a 7-day retention, removing old file versions from S3 to control storage cost. This is set conservatively (7 days, not the default 0) to allow time for rollback if a bad batch write is discovered late.

---

## 9. Point-in-Time Join Algorithm

**This is the single most important section of the document.** A feature store that gets this wrong produces training data that leaks future information, resulting in models that appear excellent in offline evaluation and silently fail in production. The failure is insidious: it is not a crash or an error, but a subtle overfitting to information the model could not have had at prediction time.

### The Problem: Data Leakage from Naive Joins

Consider a training dataset where each example is "user X clicked item Y at time T." We need the feature values for user X **as they would have been at time T** -- not the latest values, not tomorrow's values, not last month's values, but the values that the online serving API would have returned if queried at time T.

**Naive join (WRONG):**

```sql
-- THIS PRODUCES DATA LEAKAGE
SELECT
  labels.user_id,
  labels.item_id,
  labels.event_time,
  labels.clicked,
  features.txn_count_7d,
  features.avg_txn_amount_30d
FROM training_labels labels
JOIN batch_features features
  ON labels.user_id = features.entity_id
  AND features.date = DATE(labels.event_time)  -- Same day? Close enough? NO.
```

Why this is wrong: the batch feature for 2024-08-12 is computed at 6 AM UTC using all data through end-of-day 2024-08-11. But a label event at 2024-08-12 02:00 AM would have been served the feature from the 2024-08-11 batch run (computed at 6 AM on 2024-08-11 using data through 2024-08-10). The naive join uses the 2024-08-12 features, which include a full extra day of data the model would not have had at 2 AM.

**Diagram: Naive Join vs. Point-in-Time Correct Join**

```
Timeline:
  Aug 10      Aug 11      Aug 12      Aug 13
  ──┬──────────┬──────────┬──────────┬──────
    │          │          │          │
    │  Batch   │  Batch   │  Batch   │
    │  run     │  run     │  run     │
    │  6AM     │  6AM     │  6AM     │
    │  ▼       │  ▼       │  ▼       │
    │  F(8/10) │  F(8/11) │  F(8/12) │
    │          │          │          │
    │          │    ★ Label event at Aug 12 02:00 AM
    │          │          │
    │          │          │

  What was available at prediction time (Aug 12 02:00 AM)?
    → F(8/11), computed from data through Aug 10.
    The Aug 12 batch hasn't run yet (runs at 6 AM).

  NAIVE JOIN uses: F(8/12) ← WRONG, includes data from Aug 11
    that wasn't available at 02:00 AM on Aug 12.

  CORRECT JOIN uses: F(8/11) ← the MOST RECENT feature value
    whose feature_timestamp < label event_time.

  For streaming features, the same logic applies but at finer
  granularity: use the most recent feature_timestamp that is
  strictly BEFORE the label event_time.
```

### The Correct Algorithm: As-Of Join

The point-in-time correct join is an **as-of join** (also called a temporal join or time-travel join): for each label event at time T, find the most recent feature value whose `feature_timestamp` is strictly less than T.

```python
def point_in_time_join(
    labels: DataFrame,        # (entity_id, event_time, label, ...)
    features: DataFrame,      # (entity_id, feature_name, value, feature_timestamp)
    feature_names: list[str],
    entity_key: str = "entity_id",
    event_time_col: str = "event_time",
    feature_time_col: str = "feature_timestamp",
) -> DataFrame:
    """Point-in-time correct join: for each label row, find the
    most recent feature value BEFORE the label's event time.

    This is the load-bearing correctness guarantee of the feature store.
    Every training dataset generated by the platform uses this function.

    Algorithm:
    1. Union labels and features into a single timeline per entity.
    2. Sort by timestamp.
    3. For each label row, carry forward the most recent feature value.

    This is equivalent to a SQL ASOF JOIN or a pandas merge_asof,
    but implemented in Spark for scale.
    """

    # Step 1: Pivot features to wide format per (entity, timestamp)
    # so each row is one entity at one timestamp with all feature values
    features_wide = features.groupBy(entity_key, feature_time_col).pivot(
        "feature_name", feature_names
    ).agg(F.first("feature_value"))

    # Step 2: Union labels and features into a single timeline
    # Mark each row as either a "label" or a "feature" event
    labels_marked = labels.select(
        col(entity_key),
        col(event_time_col).alias("_ts"),
        lit("label").alias("_type"),
        *[col(c) for c in labels.columns if c not in [entity_key, event_time_col]]
    )

    features_marked = features_wide.select(
        col(entity_key),
        col(feature_time_col).alias("_ts"),
        lit("feature").alias("_type"),
        *[col(f) for f in feature_names]
    )

    # Step 3: Window-based as-of join
    # For each entity, order by timestamp, then for each label row,
    # take the most recent preceding feature row's values.

    unioned = labels_marked.unionByName(features_marked, allowMissingColumns=True)

    window = Window.partitionBy(entity_key).orderBy(
        col("_ts"),
        # CRITICAL: feature events must sort BEFORE label events at
        # the same timestamp. If a feature was computed at exactly
        # the same millisecond as a label event, it WAS available
        # at prediction time (it was computed and served before the
        # prediction request). Using "feature" < "label" in sort
        # order ensures this.
        col("_type")
    )

    # Carry forward: for each feature column, use last non-null value
    # in the timestamp-ordered window up to and including the current row
    for fname in feature_names:
        unioned = unioned.withColumn(
            fname,
            F.last(col(fname), ignorenulls=True).over(
                window.rowsBetween(Window.unboundedPreceding, Window.currentRow)
            )
        )

    # Step 4: Filter to only label rows (features were just used for carry-forward)
    result = unioned.filter(col("_type") == "label").drop("_type", "_ts")

    return result
```

### The Sort-Order Subtlety

The `col("_type")` in the sort order is the single most critical detail. When a feature and a label have the **exact same timestamp** (same millisecond), the question is: was the feature available at the time of the label event?

The answer depends on the platform's serving semantics: a feature value that has been written to the online store at time T is available for serving at time T. Therefore, a feature with `feature_timestamp = T` WAS available at the moment of a label event at `event_time = T`, and should be included. The sort order `("feature" < "label")` at equal timestamps ensures the feature's value is carried forward into the label row.

If the opposite convention is needed (strict less-than, feature must be strictly before the label), change the sort order to place features after labels at equal timestamps.

### Why SQL Joins Don't Work

A standard SQL `JOIN ... ON` with inequality conditions (`features.feature_timestamp < labels.event_time`) would produce a cross product of all feature rows before each label, then require a `MAX(feature_timestamp)` to select the most recent one. For 1B entities with 3 years of daily features (1,095 feature rows per entity) and millions of label events, this cross product explodes:

```
Naive SQL approach:
  SELECT l.*, f.*
  FROM labels l
  JOIN features f
    ON l.entity_id = f.entity_id
    AND f.feature_timestamp < l.event_time
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY l.entity_id, l.event_time
    ORDER BY f.feature_timestamp DESC
  ) = 1

  For 10M label events * ~500 matching feature rows each
  (average features before the label time):
  Intermediate cross product: 5 BILLION rows before the ROW_NUMBER filter.
  This is computationally expensive and memory-intensive.
```

The **window-based carry-forward approach** from the algorithm above avoids this cross product entirely: it processes each entity's timeline in a single sorted pass (O(N log N) for the sort, O(N) for the carry-forward), regardless of how many features are before each label. This is why the point-in-time join is implemented as a window function, not as a standard join.

### Handling Streaming Features in Point-in-Time Joins

Streaming features have finer-grained timestamps (per-window emission) than batch features (daily). The same as-of join algorithm works for both -- the only difference is the `feature_timestamp` granularity:

```
Batch feature:   feature_timestamp = 2024-08-12 06:00:00 (daily batch time)
Streaming feature: feature_timestamp = 2024-08-12 14:30:00 (window end time)

For a label event at 2024-08-12 14:45:00:
  Batch:     use the 06:00 value (most recent before 14:45)
  Streaming: use the 14:30 value (most recent window end before 14:45)

For a label event at 2024-08-12 14:15:00:
  Batch:     use the 06:00 value (same as above)
  Streaming: use the 13:30 value (the PREVIOUS window end before 14:15,
             since the 14:30 window hasn't emitted yet at 14:15)
```

The streaming feature's `feature_timestamp` is set to the **window end time**, not the event time of the last event in the window. This is because the window result is not "available" to the online serving API until the window closes and emits -- using the window end time as the feature timestamp correctly models when the value became available for serving.

### Training Dataset Generation API

```python
def generate_training_dataset(
    label_source: str,          # Delta table with (entity_id, event_time, label)
    feature_groups: list[str],  # ["user_engagement_features", "user_profile_features"]
    entity_key: str,
    time_range: tuple[datetime, datetime],
    output_format: str = "parquet",  # parquet | tfrecord | csv
    output_path: str = None,
) -> TrainingDatasetMetadata:
    """Generate a point-in-time correct training dataset.

    Steps:
    1. Read labels from the label source within the time range.
    2. For each feature group, read the offline store's feature history.
    3. Perform point-in-time join for each feature group.
    4. Validate: no feature_timestamp > label event_time (invariant check).
    5. Write the joined result to the output path.
    """
    spark = get_spark_session()

    # Read labels
    labels = spark.read.format("delta").table(label_source).filter(
        (col("event_time") >= time_range[0]) &
        (col("event_time") <= time_range[1])
    )

    # For each feature group, perform point-in-time join
    result = labels
    for fg_name in feature_groups:
        fg_def = registry.get_feature_group(fg_name)
        feature_names = [f.name for f in fg_def.features if f.serving.offline]

        # Read feature history (both batch and streaming sources)
        batch_features = read_offline_features(
            "batch_features", fg_def, time_range, entity_key
        )
        streaming_features = read_offline_features(
            "streaming_features", fg_def, time_range, entity_key
        )
        all_features = batch_features.unionByName(
            streaming_features, allowMissingColumns=True
        )

        result = point_in_time_join(
            labels=result,
            features=all_features,
            feature_names=feature_names,
            entity_key=entity_key,
        )

    # INVARIANT CHECK: no feature timestamp is after the label event time
    # This is the final safety net. If this assertion ever fires, there
    # is a bug in the point-in-time join implementation.
    violation_count = result.filter(
        col("_max_feature_timestamp") > col("event_time")
    ).count()
    assert violation_count == 0, (
        f"POINT-IN-TIME VIOLATION: {violation_count} rows have feature "
        f"timestamps after their label event time. This is a data leakage bug."
    )

    # Write output
    if output_format == "parquet":
        result.write.parquet(output_path)
    elif output_format == "tfrecord":
        result.write.format("tfrecord").save(output_path)

    return TrainingDatasetMetadata(
        row_count=result.count(),
        feature_groups=feature_groups,
        time_range=time_range,
        output_path=output_path,
        pit_join_verified=True,
    )
```

---

## 10. Feature Transformation at Serving Time

### On-Demand Computation Layer

Some features cannot be pre-materialized:
- **Cross-entity features**: user-item affinity (dot product of user and item embeddings) depends on which specific item is being scored, which is not known until request time.
- **Request-context features**: time since last login, current device type, geo-distance between user and store -- these depend on the request itself.
- **Freshest-possible features**: "seconds since last event" must be computed at the exact moment of the request.

The serving API includes a lightweight transformation layer that combines pre-materialized features (from the online store) with on-demand computed features, within the 10ms latency budget:

```python
async def serve_features(
    entity_type: str,
    entity_id: str,
    feature_names: list[str],
    request_context: dict = None,    # for on_demand features
) -> FeatureVector:
    """Serve a feature vector, mixing pre-materialized and on-demand features.

    Latency budget breakdown:
      Redis lookup:         ~2ms
      On-demand transforms: ~1-3ms (CPU-bound, no I/O)
      Serialization:        ~0.5ms
      Network overhead:     ~2ms
      Total:                ~5-8ms, well within 10ms P99
    """

    # Partition features into pre-materialized and on-demand
    premat_features = []
    ondemand_features = []
    for name in feature_names:
        fdef = registry.get_feature_def(name)
        if fdef.computation.mode == "on_demand":
            ondemand_features.append(fdef)
        else:
            premat_features.append(name)

    # Fetch pre-materialized features from online store (single round-trip)
    premat_values = {}
    if premat_features:
        premat_values = await online_store.get_features(
            entity_type, entity_id, premat_features
        )

    # Compute on-demand features
    ondemand_values = {}
    for fdef in ondemand_features:
        # On-demand features can depend on other features (pre-materialized
        # or other on-demand). Resolve dependencies in topological order.
        deps = resolve_dependencies(fdef, premat_values, ondemand_values)
        value = evaluate_expression(fdef.computation.expression, deps, request_context)
        ondemand_values[fdef.name] = value

    # Merge and return
    all_values = {**premat_values, **ondemand_values}
    return FeatureVector(
        entity_id=entity_id,
        features=all_values,
        metadata=FeatureVectorMetadata(
            timestamps={name: premat_values[name].timestamp for name in premat_features},
            on_demand_computed=list(ondemand_values.keys()),
        ),
    )


def evaluate_expression(expression: str, dependencies: dict, context: dict) -> Any:
    """Evaluate an on-demand feature expression.

    Supported built-in functions:
      DOT_PRODUCT(vec1, vec2)     - dot product of two embedding vectors
      COSINE_SIM(vec1, vec2)      - cosine similarity
      TIME_SINCE(timestamp)       - seconds since a timestamp (from context or feature)
      GEO_DISTANCE(lat1,lon1,lat2,lon2) - haversine distance in km
      FEATURE(name, entity_id)    - lookup a pre-materialized feature

    The expression is pre-compiled to a Python AST at feature registration
    time (not at serving time) and executed against a sandboxed namespace.
    """
    compiled = get_compiled_expression(expression)  # cached
    namespace = {**dependencies, **context, **BUILTIN_FUNCTIONS}
    return compiled.evaluate(namespace)
```

**Latency constraint**: on-demand features are pure CPU computation (arithmetic, vector ops) with no I/O beyond the initial feature store lookup that already happened. If an on-demand feature requires an additional online store lookup (e.g., `FEATURE('item_embedding_128d', request.item_id)` for a different entity than the primary lookup), that lookup is batched with the primary entity's lookup in the same round-trip where possible, or issued as a second parallel round-trip. The serving API rejects on-demand feature definitions that would require more than 2 round-trips to the online store, as they would exceed the latency budget.

---

## 11. Training-Serving Consistency

### The Problem: Training-Serving Skew

Training-serving skew occurs when the feature values used during model training differ from the values served during inference, even for the same entity at the same logical time. Sources of skew:

| Skew Source | Example | Consequence |
|---|---|---|
| **Different computation logic** | Training SQL uses `AVG(amount)`, serving code uses `SUM(amount)/COUNT(amount)` with different null handling | Model learns a relationship to a feature that doesn't exist in production |
| **Different data sources** | Training reads from the warehouse (yesterday's data), serving reads from a stream (today's data) | Feature value ranges differ between training and serving |
| **Different time windows** | Training computes "last 30 days" from the label timestamp, serving computes "last 30 days" from now | The 30-day windows don't align, values differ |
| **Different preprocessing** | Training applies log-transform in the feature pipeline, serving applies it in the model code (or doesn't) | Feature distribution at serving time doesn't match what the model was trained on |

### Structural Prevention: Single Definition, Dual Execution

The feature store prevents skew structurally, not by testing or monitoring (though both are also done). The mechanism:

```
Feature Definition (YAML, section 4)
         │
         │ Single source of truth
         │
    ┌────┴────┐
    │         │
    ▼         ▼
  Batch     Streaming
  Compute   Compute
  (Spark)   (Flink)
    │         │
    │         │  SAME SQL expression, compiled to each engine's dialect
    │         │  by the platform's code generator -- the user never
    │         │  writes Spark SQL and Flink SQL separately.
    │         │
    ▼         ▼
  Offline   Online
  Store     Store
    │         │
    │         │  SAME encoded value (same type, same precision,
    │         │  same null handling) in both stores.
    │         │
    ▼         ▼
  Training  Serving
  Dataset   API
  (PIT join)
```

**Why this works**: the SQL expression in the YAML definition is the canonical computation. The platform's code generator translates it to:
- Spark SQL for batch computation (same SQL, executed on the Spark engine)
- Flink SQL for streaming computation (same SQL, executed on the Flink engine)

Because both engines execute the same SQL, and because SQL semantics are standardized for the operations the platform supports (aggregations, filters, arithmetic, UDFs), the values are identical. The user never writes two separate implementations.

**Where this can still break** (and what the platform does about it):

1. **Floating-point precision differences between Spark and Flink**: mitigated by standardizing on double precision (float64) for all numeric features and rounding to 6 decimal places in the value encoder. Validated by the skew detector (below).
2. **Null handling differences**: mitigated by explicit `COALESCE(x, default_value)` in every feature expression, enforced by the YAML validator at definition time.
3. **Time semantics differences**: the `feature_timestamp` in both the online and offline stores is set by the computation engine (not by the clock at write time), and the point-in-time join uses this timestamp. This means the offline store's view of "what was available at time T" matches the online store's actual state at time T.

### Training-Serving Skew Detector

Even with structural prevention, the platform runs a continuous validation:

```python
def detect_training_serving_skew(
    feature_group: str,
    sample_size: int = 10000,
    tolerance: float = 0.001,  # 0.1% relative difference
) -> SkewReport:
    """Compare online store values against offline store values for
    the same entity at the same timestamp.

    This is the empirical verification that the structural prevention
    (single SQL definition) is working correctly. It should NEVER
    find skew in steady state; if it does, there is a bug in the
    platform's code generation or value encoding.

    Runs hourly, samples recent entities.
    """

    # Sample entities from the online store
    sampled_entities = online_store.sample_entities(
        feature_group, sample_size
    )

    skew_violations = []
    for entity_id, online_values in sampled_entities:
        for feature_name, online_val in online_values.items():
            online_ts = online_val.timestamp

            # Look up the SAME entity at the SAME timestamp in the offline store
            offline_val = offline_store.get_feature_at_time(
                entity_id, feature_name, online_ts
            )

            if offline_val is None:
                # Offline store hasn't received this value yet (replication lag)
                # -- not a skew violation, just lag. Alert if lag > SLA.
                continue

            # Compare values
            if not values_match(online_val.value, offline_val.value, tolerance):
                skew_violations.append(SkewViolation(
                    entity_id=entity_id,
                    feature_name=feature_name,
                    online_value=online_val.value,
                    offline_value=offline_val.value,
                    timestamp=online_ts,
                    relative_diff=abs(online_val.value - offline_val.value) /
                                  max(abs(online_val.value), 1e-10),
                ))

    if skew_violations:
        # This is a P0 alert. Any skew detection means the structural
        # prevention mechanism has a bug.
        alert_p0("training-serving-skew-detected", skew_violations)

    return SkewReport(
        sampled=sample_size,
        violations=len(skew_violations),
        max_relative_diff=max(v.relative_diff for v in skew_violations) if skew_violations else 0,
    )
```

---

## 12. Feature Quality and Monitoring

### Schema Validation

Every feature value, whether from batch or streaming, passes through schema validation before being written to either store:

```python
def validate_feature_value(
    value: Any,
    feature_def: FeatureDef,
) -> ValidationResult:
    """Validate a single feature value against its definition's quality spec.

    Called in-line on the write path for streaming features (within the
    Flink pipeline, before the dual-write sink), and as a batch validation
    step for batch features (after Spark job, before materialization).
    """
    errors = []

    # Type check
    if not isinstance(value, EXPECTED_TYPES[feature_def.value_type]):
        errors.append(f"Type mismatch: expected {feature_def.value_type}, got {type(value)}")

    # Null check
    if value is None and not feature_def.quality.nullable:
        errors.append("Null value for non-nullable feature")

    # Range check
    if feature_def.quality.range and value is not None:
        lo, hi = feature_def.quality.range
        if value < lo or value > hi:
            errors.append(f"Value {value} outside range [{lo}, {hi}]")

    # Embedding dimension check
    if feature_def.value_type == "embedding" and value is not None:
        if len(value) != feature_def.embedding_dim:
            errors.append(f"Embedding dim {len(value)} != expected {feature_def.embedding_dim}")

    # Embedding norm check
    if feature_def.quality.get("embedding_norm_range") and value is not None:
        norm = np.linalg.norm(value)
        lo, hi = feature_def.quality.embedding_norm_range
        if norm < lo or norm > hi:
            errors.append(f"Embedding L2 norm {norm} outside [{lo}, {hi}]")

    return ValidationResult(passed=len(errors) == 0, errors=errors)
```

### Feature Drift Detection

Feature drift occurs when the distribution of a feature's values shifts over time, often indicating an upstream data pipeline issue or a genuine change in the underlying data that may require model retraining.

```python
def detect_feature_drift(
    feature_name: str,
    reference_window: tuple[datetime, datetime],  # e.g., training time distribution
    current_window: tuple[datetime, datetime],      # e.g., last 24 hours
    method: str = "psi",                            # psi | ks | wasserstein
) -> DriftReport:
    """Compare current feature distribution against a reference distribution.

    Statistical tests:
      PSI (Population Stability Index): binned comparison, good for
        detecting distribution shape changes. PSI > 0.1 = moderate shift,
        PSI > 0.25 = significant shift.
      KS (Kolmogorov-Smirnov): non-parametric, tests whether two samples
        come from the same distribution. P-value < 0.05 = significant.
      Wasserstein (Earth Mover's Distance): measures the "work" needed
        to transform one distribution into another. Good for continuous
        features, sensitive to location shifts.
    """

    reference_values = offline_store.read_feature_values(
        feature_name, reference_window
    ).sample(100000)  # sample for efficiency

    current_values = offline_store.read_feature_values(
        feature_name, current_window
    ).sample(100000)

    if method == "psi":
        # Population Stability Index
        ref_hist, bin_edges = np.histogram(reference_values, bins=20)
        cur_hist, _ = np.histogram(current_values, bins=bin_edges)
        ref_pct = ref_hist / ref_hist.sum() + 1e-10  # avoid log(0)
        cur_pct = cur_hist / cur_hist.sum() + 1e-10
        psi = np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct))
        drifted = psi > 0.25

    elif method == "ks":
        # Kolmogorov-Smirnov test
        ks_stat, p_value = scipy.stats.ks_2samp(reference_values, current_values)
        drifted = p_value < 0.05

    elif method == "wasserstein":
        # Wasserstein (Earth Mover's) Distance
        distance = scipy.stats.wasserstein_distance(reference_values, current_values)
        # Normalize by reference std to get a relative measure
        relative_distance = distance / (np.std(reference_values) + 1e-10)
        drifted = relative_distance > 0.5  # threshold tunable per feature

    return DriftReport(
        feature_name=feature_name,
        method=method,
        score=psi if method == "psi" else ks_stat if method == "ks" else distance,
        drifted=drifted,
        reference_stats=compute_stats(reference_values),
        current_stats=compute_stats(current_values),
    )
```

### Freshness Monitoring

```python
def check_feature_freshness(feature_name: str) -> FreshnessReport:
    """Check if a feature's latest value is within its freshness SLA.

    This runs continuously (every 60 seconds for real-time features,
    every 5 minutes for batch features) and alerts when a feature
    falls behind its SLA.
    """
    fdef = registry.get_feature_def(feature_name)
    sla = parse_duration(fdef.freshness_sla)

    # Sample latest timestamps from the online store
    sample_entities = online_store.sample_timestamps(feature_name, n=1000)

    ages = [(datetime.utcnow() - ts) for ts in sample_entities]
    p50_age = np.percentile(ages, 50)
    p99_age = np.percentile(ages, 99)

    sla_violation = p99_age > sla
    if sla_violation:
        alert(
            severity="high" if fdef.freshness_sla.endswith("s") else "medium",
            message=f"Feature {feature_name} P99 age {p99_age} exceeds SLA {sla}",
            details={
                "p50_age": p50_age,
                "p99_age": p99_age,
                "sla": sla,
                "likely_cause": diagnose_freshness_issue(feature_name),
            }
        )

    return FreshnessReport(
        feature_name=feature_name,
        p50_age=p50_age,
        p99_age=p99_age,
        sla=sla,
        violation=sla_violation,
    )
```

### Monitoring Dashboard Summary

| Metric | What It Measures | Alert Threshold |
|---|---|---|
| **Null rate** | Fraction of null values per feature | > configured threshold (typically 1-5%) |
| **Value distribution** (mean, stddev, min, max, percentiles) | Statistical profile per feature | > 3-sigma shift from baseline |
| **Feature drift** (PSI) | Distribution shift from training baseline | PSI > 0.25 |
| **Freshness P99** | Staleness of feature values in online store | > freshness SLA per feature |
| **Write throughput** | Features/sec written to each store | Drop > 50% from baseline |
| **Serving latency** (P50, P99) | Online feature lookup latency | P99 > 10ms |
| **Skew detector violations** | Online vs offline value mismatch | Any violation > 0 is P0 |
| **Batch job SLA** | Time to complete daily materialization | > 2 hours |
| **Streaming pipeline lag** | Consumer lag on source Kafka topic | > 30 seconds sustained |
| **Validation failure rate** | Fraction of values failing schema/quality checks | > 0.1% |

---

## 13. Data Models

### Entity-Relationship Model

```
FeatureGroup (1) ──── (N) FeatureDefinition
      │                        │
      │ version                │ has
      │                        ▼
      │                  ComputationConfig
      │                  (mode, source, expression, schedule, window)
      │                        │
      │                        │ produces
      │                        ▼
      │                  FeatureValue (N per entity per timestamp)
      │                        │
      │                        ├──▶ OnlineStore (latest per entity)
      │                        └──▶ OfflineStore (full history, partitioned)
      │
      └──── (N) ConsumingModel
                  (downstream lineage: which models read which features)

QualityConfig ──── DriftBaseline ──── AlertRule
```

### Representative Payloads

```json
// FeatureGroup (in registry)
{
  "name": "user_engagement_features",
  "entity": "user",
  "entity_key": "user_id",
  "owner": "recommendations-team",
  "version": 3,
  "created_at": "2024-01-15T00:00:00Z",
  "features": ["txn_count_7d", "avg_txn_amount_30d", "user_embedding_128d"],
  "governance": {
    "pii": false,
    "access": {
      "read": ["recommendations-team", "fraud-team"],
      "write": ["recommendations-team"]
    }
  },
  "consuming_models": [
    "rec-model-v4", "fraud-model-v2", "risk-scoring-v1"
  ]
}

// FeatureValue (in online store, encoded)
{
  "key": "features:user:u_12345",
  "fields": {
    "txn_count_7d": {
      "value": 42,
      "value_type": "int64",
      "feature_timestamp": "2024-08-12T14:30:00Z",
      "feature_version": 3
    },
    "avg_txn_amount_30d": {
      "value": 156.78,
      "value_type": "float64",
      "feature_timestamp": "2024-08-12T06:00:00Z",
      "feature_version": 3
    }
  }
}

// FeatureValue (in offline store, Parquet row)
{
  "entity_id": "u_12345",
  "feature_name": "txn_count_7d",
  "feature_value": 42,
  "feature_timestamp": "2024-08-12T14:30:00Z",
  "feature_version": 3,
  "source": "streaming",
  "window_start": "2024-08-05T14:00:00Z",
  "window_end": "2024-08-12T14:00:00Z",
  "created_at": "2024-08-12T14:30:05Z"
}

// TrainingDataset metadata
{
  "dataset_id": "td_rec-model-v5-2024-08-12",
  "label_source": "warehouse.click_events",
  "feature_groups": ["user_engagement_features", "user_profile_features"],
  "time_range": ["2024-01-01", "2024-08-01"],
  "row_count": 15234567,
  "pit_join_verified": true,
  "feature_versions": {
    "user_engagement_features": 3,
    "user_profile_features": 2
  },
  "generated_at": "2024-08-12T08:15:00Z",
  "generated_by": "recommendations-team"
}
```

---

## 14. API Design

### Feature Serving (gRPC)

```protobuf
service FeatureServing {
  // Single entity lookup
  rpc GetFeatures(GetFeaturesRequest) returns (GetFeaturesResponse);

  // Batch entity lookup (up to 500 entities)
  rpc GetFeaturesBatch(GetFeaturesBatchRequest) returns (GetFeaturesBatchResponse);
}

message GetFeaturesRequest {
  string entity_type = 1;
  string entity_id = 2;
  repeated string feature_names = 3;    // empty = all features for entity
  map<string, string> request_context = 4;  // for on-demand features
  Duration max_staleness = 5;           // optional: reject stale values
}

message GetFeaturesResponse {
  string entity_id = 1;
  map<string, FeatureValue> features = 2;
  ResponseMetadata metadata = 3;
}

message FeatureValue {
  oneof value {
    int64 int_value = 1;
    double float_value = 2;
    string string_value = 3;
    bytes embedding_value = 4;
    RepeatedDouble list_value = 5;
  }
  google.protobuf.Timestamp feature_timestamp = 10;
  int32 feature_version = 11;
  FeatureStatus status = 12;   // FRESH, STALE, DEFAULT (fallback used)
}
```

### Feature Management (REST)

```
POST   /v1/feature-groups                     # Register a new feature group
GET    /v1/feature-groups/{name}              # Get current definition
PUT    /v1/feature-groups/{name}              # Update definition (triggers version bump)
DELETE /v1/feature-groups/{name}              # Deprecate (soft delete)

GET    /v1/feature-groups/{name}/versions     # List all versions
GET    /v1/feature-groups/{name}/lineage      # Upstream/downstream dependencies

POST   /v1/feature-groups/{name}/backfill     # Trigger a backfill job
GET    /v1/feature-groups/{name}/backfill/{id} # Backfill status

POST   /v1/training-datasets                  # Generate a training dataset
GET    /v1/training-datasets/{id}             # Dataset status and metadata

GET    /v1/features/search?q={query}&entity={type}&owner={team}
GET    /v1/features/{name}/quality            # Quality metrics and drift reports
GET    /v1/features/{name}/freshness          # Freshness SLA compliance
```

### Feature Retrieval for Training (Python SDK)

```python
from feature_store import FeatureStoreClient

client = FeatureStoreClient()

# Generate a training dataset with point-in-time correct joins
dataset = client.generate_training_dataset(
    label_source="warehouse.click_events",
    feature_groups=["user_engagement_features", "user_profile_features"],
    entity_key="user_id",
    time_range=("2024-01-01", "2024-08-01"),
    output_format="parquet",
    output_path="s3://ml-training/rec-model-v5/",
)

# Use in training
import pandas as pd
df = pd.read_parquet(dataset.output_path)
# Every feature value in df is point-in-time correct relative to the label event_time

# Online serving (from model inference code)
features = client.get_features(
    entity_type="user",
    entity_id="u_12345",
    feature_names=["txn_count_7d", "avg_txn_amount_30d", "user_item_affinity"],
    request_context={"item_id": "i_789"},  # for on-demand features
)
```

---

## 15. Sequence Flows

### 15.1 End-to-End: A Transaction Event Becomes a Serving Feature

```
User makes a purchase ──▶ Transaction event published to Kafka
                                     │
                                     ▼
Flink streaming pipeline (txn_count_7d feature):
  1. Deserialize + validate schema                              ~0.1ms
  2. Extract (user_id, amount, event_time)
  3. Assign watermark (bounded out-of-orderness, 10s)
  4. Route to keyed window: user_id -> sliding(7d, 1h)
  5. Update window state (increment count in RocksDB)          ~0.5ms
  6. Window fires (every 1 hour, or on watermark advance):
     emit (user_id, txn_count_7d=42, window_end=14:00:00)
                                     │
                        ┌────────────┴────────────┐
                        ▼                          ▼
Dual-write sink 1:                  Dual-write sink 2:
  Redis idempotent write            Kafka "feature-offline-log"
  key: features:user:u_12345        (exactly-once via Flink
  field: txn_count_7d               Kafka transactions)
  value: {42, ts=14:00:00, v=3}           │
  (Lua: write if version >= stored)       ▼
         │                          Compaction job (every 15min):
         │                          Spark reads Kafka, writes to
         │                          Delta Lake offline store
         ▼                          s3://feature-store/offline/
  Online store updated.             streaming_features/
  Feature available for             entity_type=user/date=2024-08-12/
  serving within ~2ms               hour=14/part-00042.parquet
  of window emission.
         │
         ▼
  ML inference service calls GetFeatures(user_id=u_12345)
  → gets txn_count_7d=42, computed 0-5 seconds ago.

  End-to-end latency: event → servable feature:
    Event ingest to Flink: ~100ms (Kafka + deserialization)
    Window processing:     ~500ms (aggregation state update)
    Window emission:       up to 1 hour (window slide interval)
    Redis write:           ~2ms
    Total: dominated by the window slide interval.
    For a 1-hour slide: feature updates every hour.
    For tighter freshness, use a tumbling window with smaller size.
```

### 15.2 End-to-End: Training Dataset Generation

```
Data scientist requests training dataset via Python SDK
         │
         ▼
Feature Store API receives request:
  label_source: warehouse.click_events
  feature_groups: [user_engagement, user_profile]
  time_range: [2024-01-01, 2024-08-01]
         │
         ▼
Step 1: Read labels
  Spark reads warehouse.click_events, filters to time range
  → 15M label events: (user_id, item_id, event_time, clicked)
         │
         ▼
Step 2: Read feature history from offline store
  For each feature group:
    Read batch_features + streaming_features from Delta Lake
    Filter to relevant entity_ids and time range
    Union into single features DataFrame
         │
         ▼
Step 3: Point-in-time join (section 9 algorithm)
  For each feature group:
    Union labels + features into single timeline per entity
    Sort by (entity_id, timestamp, type)
    Carry forward: last non-null feature value before each label
         │
         ▼
Step 4: Invariant check
  Assert: no feature_timestamp > label event_time
  (If this ever fires, section 9's algorithm has a bug.)
         │
         ▼
Step 5: Write output
  Write joined DataFrame as Parquet to s3://ml-training/...
  Record metadata: row count, feature versions, PIT verification
         │
         ▼
Data scientist receives path to training dataset.
Every feature value in the dataset was the value that would have
been served online at the moment of each label event -- no more,
no less.
```

---

## 16. Failure Walkthroughs

### Failure 1: Streaming Pipeline Lag (Flink Consumer Falls Behind)

**Scenario**: The Flink pipeline computing `txn_count_7d` falls behind Kafka. Consumer lag grows from the normal ~100ms to 30 minutes.

**Detection** (multiple independent signals):
1. Flink metrics: `records-lag-max` exceeds threshold (>10,000 records).
2. Freshness monitor (section 12): P99 feature age in online store exceeds the 5-second SLA.
3. Flink checkpoint metrics: checkpoint duration increases (more state to snapshot when behind).

**Timeline**:
```
T+0min:   Source topic throughput spikes (Black Friday traffic).
          Flink pipeline processes at max capacity but falls behind.

T+2min:   records-lag-max crosses 10K threshold → PagerDuty alert fires.
          Online store still serving features from the last window emission
          (at most 1 hour old for a 1-hour slide window -- stale but not wrong).

T+5min:   Freshness monitor detects P99 age > 5s SLA for real-time features.
          Dashboard shows: lag = 15 minutes, growing at ~1 minute/minute.

T+10min:  Automated response: Flink job manager triggers reactive scaling
          (increase parallelism from 10 to 20 TaskManagers for this pipeline).
          Flink redistributes state (rebalances keyed partitions).

T+15min:  Rebalancing complete. New TaskManagers start processing.
          Catch-up rate: processing 2x the incoming rate.

T+45min:  Lag fully recovered to <1s. Freshness SLA restored.
          During the lag window, the online store served STALE but CORRECT
          values -- the features were from an older window, not from a
          different computation. No serving error, no wrong values,
          just reduced freshness.
```

**What the online store serves during the lag**: stale values. The feature `txn_count_7d` in the online store reflects the last completed window. It does not reflect transactions from the last 30 minutes. This is degraded freshness, not incorrect values. The serving API's `FeatureStatus` field reports `STALE` (timestamp > SLA), so consuming models can decide how to handle it (use the stale value, fall back to a default, or use a simpler heuristic feature).

### Failure 2: Online Store Partial Outage (Redis Shard Failure)

**Scenario**: 2 of 28 Redis primary shards fail simultaneously (AZ-correlated event).

**Detection**: Redis Cluster sentinel detects primary failure within ~5 seconds, promotes replicas.

**Timeline**:
```
T+0s:     Two primaries fail. Their replicas detect heartbeat loss.

T+5s:     Redis Cluster failover: replicas promoted to primary.
          Cluster is self-healing. During the ~5s failover window,
          requests routed to the failed shards return errors.

T+5-10s:  Serving API circuit breaker trips for the affected shards.
          Requests for entities on those shards get fallback responses:
            Option A: return cached values from a local L1 cache (if warm)
            Option B: return default values with status=DEFAULT
            Option C: route to DynamoDB fallback tier (if configured)

T+10s:    Replicas fully promoted. Redis Cluster slot map updated.
          Serving API detects healthy shards, circuit breaker resets.

T+15s:    Normal serving resumes. The ~10-second window of degraded
          serving affected only entities hashed to 2/28 shards (~7%
          of all entities). The remaining 93% were unaffected.

Post-incident: the failed nodes are replaced, new replicas sync
from the promoted primaries. Full 3x replication restored within
minutes.
```

**Impact**: ~7% of feature lookups returned degraded responses (defaults or cached values) for ~10 seconds. No data loss (replicas had the data). No incorrect values served (defaults are clearly marked as `DEFAULT` status, not confused with real values).

### Failure 3: Batch Pipeline Produces Wrong Values

**Scenario**: A data engineer updates the `avg_txn_amount_30d` feature definition, introducing a bug (divides by 1000 instead of 100 for currency conversion). The daily batch job runs and materializes incorrect values.

**Detection** (layered):
1. **Schema validation**: passes (values are valid floats in the expected range).
2. **Statistical validation** (the layer that catches this): mean of the new batch is 10x lower than the baseline. The validation step (section 5, Airflow DAG) compares the batch output's statistical profile against the previous day's profile and flags the anomaly.

```
Validation check output:
  Feature: avg_txn_amount_30d
  Previous day mean: $156.78
  Current batch mean: $15.68
  Relative change: -90%
  Threshold: 50% relative change
  RESULT: FAIL — batch output rejected, not materialized.
```

**Timeline**:
```
T+0h:     Batch job completes. Validation step fires.
          Mean has dropped 90% — exceeds the 50% relative-change threshold.
          Batch output written to STAGING, not to production offline store.
          Online store is NOT updated (materialization gated by validation).

T+0h5m:   Alert fires: "Batch validation failed for avg_txn_amount_30d."
          Previous day's values remain in both online and offline stores.
          No model sees the incorrect values.

T+1h:     Data engineer identifies the bug, fixes the definition,
          re-triggers the batch job. New output passes validation.
          Materialization proceeds normally.
```

**Why the validation gate matters**: without it, the incorrect values would have been written to both the online store (degrading every model using this feature in production) and the offline store (corrupting future training datasets). The validation gate is the platform's last line of defense against computation bugs, and it catches this class of error (systematic shift in distribution) reliably.

**What it does NOT catch**: a subtle bug that shifts values by 1-2%, within the normal variance threshold. This is where the training-serving skew detector (section 11) and feature drift monitor (section 12) provide secondary coverage over longer time horizons.

### Failure 4: PII Feature Accidentally Served to Unauthorized Model

**Scenario**: A feature `user_email_domain` is tagged as PII. A model team that does not have PII access attempts to include it in their feature request.

**Prevention** (at multiple layers):

```
Layer 1: Feature Definition (YAML)
  governance:
    pii: true
    access:
      read: ["fraud-team"]      # only fraud-team has PII access

Layer 2: Training Dataset Generation
  When the model team requests a training dataset including
  user_email_domain, the API checks the requesting team's
  access grants against the feature's governance.access.read list.
  → DENIED: "Feature user_email_domain requires PII access.
    Your team 'recommendations-team' is not in the read ACL."
  No dataset is generated. The request is logged to the audit trail.

Layer 3: Online Serving API
  The serving API checks the caller's service account identity
  against the feature's access grants at request time.
  → DENIED: service account 'rec-model-serving' is not authorized
    for PII features. The feature is excluded from the response
    (not the entire response -- only the unauthorized feature).
  Response includes: {"user_email_domain": {"status": "ACCESS_DENIED"}}

Layer 4: Audit Trail
  Both denial events are logged:
  {
    "event": "feature_access_denied",
    "feature": "user_email_domain",
    "reason": "pii_access_required",
    "requester": "recommendations-team",
    "timestamp": "2024-08-12T14:00:00Z"
  }

  A dashboard shows all PII access denials and grants, reviewable
  by the security team.
```

The key property: PII enforcement happens at the platform layer, not in the consuming model's code. A team cannot accidentally bypass PII controls by writing their own feature retrieval code because the online store's API is the only interface for reading features, and the API enforces governance rules on every request.

---

## 17. Trade-offs

### 17.1 Freshness vs. Cost

| Choice | Freshness | Cost | When to choose |
|---|---|---|---|
| **Streaming (Flink)** | <=5s | High: dedicated Flink cluster, Kafka topics, dual-write infrastructure | Features where staleness directly degrades model quality (fraud signals, real-time CTR) |
| **Micro-batch (Spark Structured Streaming, 5-15min)** | <=15min | Medium: shared Spark cluster, simpler than Flink | Features where sub-minute freshness isn't needed but daily is too stale (user activity counts) |
| **Batch (Spark, daily)** | <=24h | Low: scheduled Spark jobs, no streaming infrastructure | Features that change slowly (user demographics, 30-day aggregates) or where freshness doesn't affect model quality |

**The platform's default recommendation**: start with batch, move to streaming only when offline evaluation demonstrates that fresher features measurably improve model quality. Most teams overestimate how much real-time freshness their models actually need -- a 30-day average transaction amount does not benefit from sub-second updates, and paying for streaming infrastructure to compute it is pure waste.

### 17.2 Pre-Materialization vs. On-Demand Computation

| | Pre-materialized | On-demand |
|---|---|---|
| **Serving latency** | 1-3ms (online store lookup) | 3-10ms (lookup + compute) |
| **Storage cost** | Full: every entity * every feature stored | None: computed per request |
| **Applicable to** | Any feature computable in advance | Cross-entity, request-dependent, or combinatorial features |
| **Freshness** | Bounded by pipeline latency (seconds to hours) | Always live (computed from current values) |

**Decision**: pre-materialize everything that can be pre-materialized (all features with known entity keys at write time). Use on-demand only for features that inherently depend on the request (cross-entity interactions, request context). The 10ms P99 budget makes on-demand feasible for simple transformations (dot products, arithmetic) but not for anything requiring a database query or model inference.

### 17.3 Point-in-Time Precision vs. Storage Cost

The offline store's granularity determines how precise point-in-time joins can be:

| Granularity | Storage (1B entities, 3 years) | PIT precision |
|---|---|---|
| **Per-event** (every streaming emission logged) | ~300 TB (section 2) | Exact: join at the precise feature timestamp |
| **Hourly snapshots** | ~25 TB | <=1 hour: feature value was correct within the last hour |
| **Daily snapshots** | ~1 TB | <=24 hours: feature value was correct within the last day |

**Decision**: per-event logging for streaming features (enabled by the dual-write log), daily snapshots for batch features (enabled by the daily batch materialization). This gives exact PIT precision for streaming features and <=24h precision for batch features. The batch precision is acceptable because batch features change daily by definition -- a batch feature computed at 6 AM on Aug 12 has the same value all day, so daily granularity is lossless.

For teams that need sub-daily precision on batch features (rare), the platform supports hourly batch scheduling at higher compute cost.

### 17.4 Redis Cluster vs. DynamoDB vs. Bigtable for Online Store

This is a three-way trade-off between latency, cost, and operational complexity:

```
                   Latency
                   (lower = better)
                     ▲
                     │
        Redis ●      │
        (1-3ms)      │
                     │
                     │
        DynamoDB ●   │      ● Bigtable
        (5-10ms)     │      (5-15ms)
                     │
                     └────────────────────▶ Cost
                                          (lower = better)
                     Redis: $215K/mo
                     DynamoDB: $90K/mo
                     Bigtable: $45K/mo

Operational burden:
  Redis:    Medium (cluster management, resharding, memory monitoring)
  DynamoDB: Low (fully managed, auto-scales)
  Bigtable: Low-Medium (managed, but capacity planning needed)
```

**Decision**: Redis Cluster (section 7), because:

1. The 10ms P99 budget is non-negotiable (it's a stated requirement), and Redis is the only option with comfortable margin (1-3ms P99 leaves 7-9ms for everything else in the serving path).
2. DynamoDB can meet 10ms P99 under ideal conditions, but at 500K QPS with 10KB items, tail latency spikes under partition-level throttling become a real risk. Redis's in-memory model eliminates this class of tail latency entirely.
3. The $215K/month cost is high but proportionate -- this store serves every ML prediction in the company. If the company has 100+ ML teams, the per-team cost is ~$2K/month, well within any ML team's infrastructure budget.

**When DynamoDB is the right choice instead**: if the entity count is smaller (100M, not 1B), the feature vector is smaller (50 features, not 200), or the latency requirement is relaxed to 20ms P99. At smaller scale, DynamoDB's managed operations and lower cost per GB dominate over Redis's latency advantage.

**When Bigtable is the right choice**: if the workload is read-heavy with wide rows and the organization already operates on GCP. Bigtable's column-family model maps naturally to feature groups, and its auto-splitting handles hot keys well. But its tail latency (5-15ms P99) is the least comfortable for the stated 10ms budget.

### 17.5 Exactly-Once vs. At-Least-Once for Streaming

| | Exactly-once | At-least-once |
|---|---|---|
| **Correctness** | Feature values are correct: no duplicates, no missing | Feature values may be inflated (duplicated events counted twice) |
| **Complexity** | High: Flink checkpointing, Kafka transactions, idempotent sinks | Low: standard Kafka consumer + fire-and-forget writes |
| **Throughput** | ~30% lower than at-least-once (checkpoint overhead) | Higher |
| **Recovery time** | Checkpoint restore: 1-5 minutes | Consumer offset reset: seconds |

**Decision**: exactly-once (section 6). The correctness cost of at-least-once is too high for a feature store. A duplicated event in a `COUNT` aggregation produces a wrong feature value, which produces a wrong model prediction, which is the exact class of bug the feature store exists to prevent. The 30% throughput reduction from checkpointing is absorbed by sizing the Flink cluster accordingly (section 2: 200 TaskManagers with 2x headroom includes this overhead).

---

## 18. Evolution Path

### v1 -- Batch Feature Store (Months 1-3)

- Feature registry with YAML definitions, Postgres-backed.
- Batch computation with Spark, daily schedule, full recompute only.
- Offline store on Delta Lake, basic point-in-time join.
- Online store on Redis (single shard, small scale for pilot teams).
- REST/gRPC serving API, single-entity lookups only.
- No streaming, no on-demand transforms, no drift detection.
- **Target**: 5 teams, 500 features, 10M entities.

### v2 -- Streaming + Scale (Months 4-8)

- Streaming computation with Flink, dual-write architecture.
- Incremental batch computation for additive aggregations.
- Redis Cluster scaled to 1B entities.
- Batch entity lookups (500 entities/call).
- Schema validation on write path.
- Freshness monitoring.
- Backfill support.
- **Target**: 30 teams, 10K features, 1B entities.

### v3 -- Production Grade (Months 9-14, this document's full design)

- On-demand feature transformation at serving time.
- Full point-in-time join with streaming feature support.
- Feature drift detection with statistical tests.
- Training-serving skew detector.
- PII governance and access control.
- Feature lineage and impact analysis.
- Automated quality gates on batch pipelines.
- DynamoDB fallback tier for cold entities.
- **Target**: 100+ teams, 50K features, 1B entities, 500K QPS.

### v4 -- Advanced (Months 15+)

- Feature embeddings: learn vector representations of feature interactions for automatic feature discovery.
- Near-line compute: a Spark Structured Streaming tier between batch and streaming (5-15 minute freshness at lower cost than Flink).
- Feature versioning with automatic A/B testing: serve different feature versions to different model variants, measure impact.
- Cross-entity feature joins at serving time: "average rating of items purchased by users similar to this user" -- requires a graph query, not just a key-value lookup.
- Self-serve feature creation via a web UI with SQL editor, schema preview, and cost estimate.

---
