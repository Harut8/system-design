## System Design Task: ML Feature Store Platform

### Problem Statement

Design an **enterprise ML feature store** — the centralized platform that
computes, stores, serves, and governs the **features** (input signals) that
every ML model in the company consumes for both training and real-time inference.

Today, feature engineering is the most duplicated and error-prone work in ML.
The fraud team computes "user's average transaction amount in the last 30 days"
in a PySpark job; the recommendations team computes nearly the same thing in a
different Flink pipeline with slightly different semantics; the risk team copies
the fraud team's SQL but doesn't know it was updated last month. When a data
scientist trains a model on features computed in batch SQL, then deploys it
against features computed in a real-time Java service, the subtle differences
(a different time window, a different null-handling strategy, a rounding
difference) silently degrade model quality — the infamous
**training-serving skew**.

The feature store fixes this by establishing a **single source of truth** for
feature definitions, computation, and serving. A feature is defined once,
computed by a managed pipeline, served consistently for both training (batch
lookups against historical state) and inference (low-latency online lookups
against the freshest state), and governed with lineage, access control, and
quality monitoring.

This is platform infrastructure used by 100+ ML teams. Every deployed model's
prediction quality depends on it, so correctness (especially point-in-time
correctness for training) and serving reliability are existential requirements.

---

### Functional Requirements

1. **Feature Definition and Registry**

   * A **declarative feature definition** (YAML or DSL) specifying:
     * Feature name, entity (user, item, transaction, session), value type
       (int, float, string, embedding vector, list).
     * Computation logic: either a **transformation expression** (SQL/PySpark/
       Flink SQL) or a reference to a pre-computed data source.
     * Aggregation windows: for time-windowed features (last 1h, 7d, 30d, 90d),
       specify the window, aggregation function (sum, avg, count, min, max,
       percentile, count_distinct), and slide interval.
     * Data source(s): Kafka topic, database table, S3 path, API endpoint.
     * Freshness SLA: how stale the feature is allowed to be at serving time
       (real-time ≤ 5s, near-real-time ≤ 5min, batch ≤ 24h).
     * Schema and validation: expected range, nullability, and data quality
       checks.
   * A **feature registry** (catalog) where all features are discoverable:
     search by name, entity, team, use case, data source. Every feature has
     an owner, description, lineage (what data produces it, what models
     consume it), and quality metrics.
   * **Feature groups**: logically related features bundled together (e.g.,
     "user_engagement_features" containing last_7d_watch_time,
     last_7d_like_count, avg_session_duration), versioned as a unit.

2. **Feature Computation (Offline / Batch)**

   * Run batch feature computation on a schedule (hourly, daily) or triggered
     by upstream data arrival.
   * Compute engines: **Spark, Flink (batch mode), SQL (BigQuery/Redshift/
     Snowflake)** — the platform orchestrates; users write transformation
     logic, not infrastructure.
   * **Backfill**: recompute historical feature values when a feature definition
     changes (new logic, bug fix), filling in the historical feature table
     without disrupting current serving.
   * **Incremental computation**: for additive aggregations, maintain running
     state and process only new data, not the full window on each run.
   * Output: feature values materialized to both the **offline store** (for
     training) and the **online store** (for serving).

3. **Feature Computation (Online / Streaming)**

   * For features requiring real-time freshness (≤ seconds), compute features
     from streaming data sources (Kafka, Kinesis) using **Flink, Spark
     Structured Streaming, or a custom streaming processor**.
   * Streaming aggregations: sliding and tumbling windows with exactly-once
     semantics.
   * **Dual-write**: streaming features are written to both the online store
     (for immediate serving) and logged to the offline store (for future
     training data).
   * Late-arriving data handling: define a watermark / allowed lateness policy
     per feature; late events either update the aggregate or are dropped with
     a metric.
   * Streaming pipeline must be fault-tolerant: Flink checkpointing, exactly-
     once Kafka consumer offsets, and automatic restart on failure with
     state recovery.

4. **Online Feature Serving**

   * A low-latency API that, given an entity key (e.g., user_id) and a list
     of feature names, returns the latest feature values.
   * **Latency**: P99 ≤ **10 ms** for a single entity's feature vector (up to
     200 features).
   * **Throughput**: 500,000 lookups/sec sustained across all consumers.
   * **Batch lookup**: fetch features for multiple entities in a single call
     (e.g., score 500 candidate items — fetch item features for all 500 at
     once).
   * **Consistency**: for a single entity, all features in a response must be
     from the same point in time (or as close as the freshness SLAs allow) —
     not a mix of stale and fresh values from different pipeline runs.
   * Online store backed by **Redis, DynamoDB, or Bigtable** — key-value
     access pattern, tuned for read-heavy workloads.

5. **Offline Feature Serving (for Training)**

   * **Point-in-time correct joins**: the critical feature store capability.
     Given a set of training examples with timestamps (e.g., "user X at time
     T clicked item Y"), look up what the feature values for user X and
     item Y were **at time T** — not the latest values, but the values that
     would have been available to the model at prediction time.
   * This prevents **data leakage**: training on features that include
     information from the future relative to the prediction time.
   * Offline store backed by a **columnar data lake** (Parquet on S3, Delta
     Lake, Iceberg) partitioned by entity and time.
   * **Training dataset generation**: given an entity set, a feature list,
     a time range, and a label source, produce a training dataset with
     point-in-time correct features joined to labels — as a Spark DataFrame,
     a Parquet file, or a TFRecord file.
   * **Incremental training dataset generation**: when only the label data
     has changed (not the features), avoid recomputing feature joins.

6. **Feature Transformation at Serving Time**

   * Some features require **on-demand computation** at serving time that
     cannot be pre-materialized:
     * Cross-features (user-item dot product of embeddings).
     * Request-context features (time since last login, current device type).
     * Features depending on the specific request (distance between user and
       item location).
   * The platform must support a **lightweight transformation layer** at
     serving time that combines pre-materialized features from the online
     store with on-demand computed features, without exceeding the latency
     budget.

7. **Feature Quality and Monitoring**

   * **Data quality checks** on every feature write:
     * Schema validation (type, range, nullability).
     * Statistical checks: null rate, value distribution (mean, stddev, min,
       max, percentile), cardinality — compared against historical baselines.
     * Freshness monitoring: alert when a feature's latest value is older than
       its freshness SLA.
   * **Feature drift detection**: compare current feature distributions against
     a reference window (e.g., training-time distribution) to detect data
     pipeline issues or upstream changes.
   * **Feature lineage**: for any feature value, trace back to the source
     data, computation pipeline, and code version that produced it.
   * **Impact analysis**: given a data source change, identify all downstream
     features and models affected.

8. **Governance and Access Control**

   * **RBAC**: control who can create, modify, read, and serve features.
   * **PII handling**: mark features containing PII; enforce that PII features
     are not used in unauthorized models, are encrypted at rest, and are
     excluded from training datasets unless explicitly approved.
   * **Feature deprecation**: mark features as deprecated with a sunset date;
     alert consuming models before removal.
   * **Audit log**: who accessed which features, when, for what purpose
     (training dataset generation, online serving).

---

### Non-Functional Requirements

1. **Scale**

   * **50,000+ feature definitions** across 100+ teams.
   * **1 billion entities** (users, items, transactions) with feature vectors
     in the online store.
   * **500,000 online lookups/sec** sustained, peak 1,000,000/sec.
   * Offline store: **50 TB** of historical feature data (3 years of history).
   * **100+ streaming feature pipelines** running concurrently.

2. **Latency**

   * Online single-entity lookup: P99 ≤ **10 ms**.
   * Online batch lookup (500 entities): P99 ≤ **25 ms**.
   * Feature write propagation (streaming): event to online store ≤ **5
     seconds** P99.
   * Batch feature materialization: complete within **2 hours** of schedule
     trigger for the largest feature groups.

3. **Correctness**

   * **Point-in-time correctness is a hard invariant.** Training datasets
     must never contain feature values from after the label timestamp.
   * **Online-offline consistency**: the feature value served online at time T
     must match the value that a point-in-time offline query for time T would
     return (within the freshness SLA delta).
   * **Exactly-once materialization**: a pipeline crash and restart must not
     produce duplicate or missing feature values.

4. **Availability**

   * Online feature serving: **99.99%** — it's on the critical path of every
     ML prediction in production.
   * Batch computation pipelines: **99.9%** — delayed features are tolerable
     if they self-heal within the freshness SLA.

---

### Constraints and Assumptions

* The company has an existing data lake (S3 + Parquet/Delta Lake), a streaming
  platform (Kafka), and a data warehouse (BigQuery or Snowflake). The feature
  store integrates with, not replaces, these.
* ML models are served by a separate ML inference platform; the feature store's
  online API is called by that platform's preprocessing layer.
* Assume standard internal identity (service accounts + user identity via IdP).
* The feature store must support both batch-trained models (which need point-in-
  time correct offline features) and real-time models (which need millisecond-
  fresh online features).
* Not in scope: model training or serving — only feature computation, storage,
  and serving.

---

### What You Should Deliver

1. Requirement clarification and explicit assumptions.
2. High-level architecture: offline plane (batch compute + offline store),
   online plane (streaming compute + online store + serving API), and the
   registry/governance layer.
3. Feature registry and definition language design.
4. Batch computation pipeline: orchestration, incremental computation, and
   backfill.
5. Streaming computation pipeline: Flink topology, exactly-once semantics,
   dual-write to online + offline stores.
6. Online store design: data model, storage engine choice, and how 10ms P99
   is achieved at 500K QPS.
7. Offline store design: data layout, partitioning, and how point-in-time
   correct joins are implemented efficiently.
8. Point-in-time join algorithm: the exact mechanism, with examples showing
   how data leakage is prevented.
9. Feature transformation at serving time: the lightweight on-demand compute
   layer.
10. Feature quality monitoring: what you check, how you detect drift, and
    alerting.
11. Training-serving consistency: how you guarantee the same feature value
    online and offline.
12. Capacity estimates with arithmetic: online store size, streaming pipeline
    throughput, batch compute resource requirements.
13. Failure walkthroughs: streaming pipeline lag, online store partial
    outage, batch computation producing wrong values, and a PII feature
    accidentally served to an unauthorized model.
14. Trade-offs: freshness vs. cost, pre-materialization vs. on-demand
    computation, point-in-time precision vs. storage cost, and Redis vs.
    DynamoDB vs. Bigtable for the online store.

---

### Expectations

* **Point-in-time correctness is the #1 differentiator.** If you can explain
  the join algorithm — with timestamps, asof semantics, and why naive SQL
  joins leak data — you demonstrate senior-level feature store knowledge.
* **Training-serving skew must be structurally impossible, not just unlikely.**
  Show the mechanism that guarantees the same feature definition produces the
  same value in both paths.
* **Do the arithmetic.** Online store memory for 1B entities × 200 features,
  streaming throughput for 100 pipelines, and batch compute cost.
* **Name concrete mechanisms** — Flink event-time processing, Redis Cluster
  hash slots, Spark broadcast join for point-in-time lookups, Delta Lake
  time travel — and say what each costs.
* Prefer a design that a platform team of 4-6 engineers can operate.

---
