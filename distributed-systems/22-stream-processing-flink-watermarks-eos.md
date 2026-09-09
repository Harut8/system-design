# Chapter 22: Stream Processing -- Flink, Watermarks, and Exactly-Once Semantics

A production-grade deep dive into stream processing for senior engineers who need to design real-time ML feature pipelines, recommendation feedback loops, and event-driven architectures in system design interviews. Covers the theory of event-time processing, Apache Flink's internals (checkpointing, state management, windowing), watermark mechanics, exactly-once end-to-end guarantees, and the concrete patterns that appear in AI/ML system designs -- real-time feature computation, sessionization, dual-write to online/offline stores, and complex event processing.

Prerequisites: Kafka fundamentals from `07-kafka-and-event-streaming.md`, caching patterns from `08-caching-strategies-and-patterns.md`, and failure detection from `29-failure-detection-phi-accrual.md`. This chapter provides the stream processing foundation referenced by recommendation systems, feature stores, and AI search engine designs throughout the curriculum.

---

## Table of Contents

1. [Stream Processing Fundamentals](#1-stream-processing-fundamentals)
2. [Apache Flink Architecture](#2-apache-flink-architecture)
3. [Windowing Deep Dive](#3-windowing-deep-dive)
4. [Watermarks -- The Key to Event-Time Processing](#4-watermarks--the-key-to-event-time-processing)
5. [State Management](#5-state-management)
6. [Checkpointing and Exactly-Once Semantics](#6-checkpointing-and-exactly-once-semantics)
7. [Flink + Kafka Integration](#7-flink--kafka-integration)
8. [Real-Time Feature Computation Patterns](#8-real-time-feature-computation-patterns)
9. [Backpressure](#9-backpressure)
10. [Failure Recovery and Restarts](#10-failure-recovery-and-restarts)
11. [Scaling and Resource Management](#11-scaling-and-resource-management)
12. [Alternatives and Comparison](#12-alternatives-and-comparison)
13. [Capacity Planning for Streaming Pipelines](#13-capacity-planning-for-streaming-pipelines)
14. [Failure Walkthroughs](#14-failure-walkthroughs)
15. [Interview Patterns](#15-interview-patterns)

---

## 1. Stream Processing Fundamentals

### 1.1 Bounded vs Unbounded Data

All data processing ultimately deals with datasets that are either bounded or unbounded. The distinction is not about size but about completeness.

**Bounded data** has a defined beginning and end. A CSV file, a database snapshot, yesterday's click logs partitioned by date -- these are all bounded. You can load the entire dataset, process it, and produce a complete result. Batch systems (MapReduce, Spark batch) are designed for bounded data.

**Unbounded data** has a beginning but no defined end. A Kafka topic receiving user clicks, a sensor stream from IoT devices, a firehose of ad impressions -- these never stop. You cannot wait for "all the data" because it will never arrive. Stream processing systems (Flink, Kafka Streams) are designed for unbounded data.

The key insight for interviews: **batch processing is a special case of stream processing**. Any batch job can be expressed as a streaming job over a bounded source that terminates when the source is exhausted. The reverse is not true -- you cannot naturally express unbounded processing as a batch job without artificial boundaries (hourly partitions, daily runs), which introduce latency and completeness problems.

```
BOUNDED vs UNBOUNDED DATA:

Bounded (Batch):
  ┌─────────────────────────────────┐
  │ event1 event2 event3 ... eventN │  <-- complete, finite dataset
  └─────────────────────────────────┘
  Process once, produce final result.

Unbounded (Stream):
  event1 event2 event3 event4 event5 event6 ... ──────>  (never ends)
  │      │      │      │
  ▼      ▼      ▼      ▼
  Process continuously, produce incremental results.
```

### 1.2 Stream Processing vs Batch Processing vs Micro-Batch

Three processing models, each with different latency and throughput characteristics:

| Property | Batch (Spark, MapReduce) | Micro-Batch (Spark Structured Streaming) | True Streaming (Flink) |
|:---|:---|:---|:---|
| Processing unit | Entire dataset | Small time-based batches | Individual events |
| Latency | Minutes to hours | 100ms to seconds | Milliseconds to low seconds |
| Throughput | Very high (optimized for bulk) | High | High (but per-event overhead) |
| State management | Implicit (re-read from source) | Managed per micro-batch | Continuous, incremental |
| Fault tolerance | Rerun the batch | Rerun the micro-batch | Checkpoint-based recovery |
| Windowing | Natural (data is already bounded) | Approximate (batch boundaries) | Native, event-time precise |
| Exactly-once | Natural (idempotent rerun) | Within micro-batch boundary | Checkpoint + 2PC sinks |

**Spark Structured Streaming** deserves special mention because it is the most common alternative to Flink in the ML ecosystem. It processes data in micro-batches -- small, scheduled batch jobs that run every 100ms to several seconds. This is simpler to reason about (each micro-batch is a small DataFrame operation) and integrates naturally with Spark's batch APIs. The tradeoff: you cannot achieve sub-100ms latency, and windowing semantics are less precise because events are grouped by processing-time micro-batch boundaries rather than true event time. For many ML feature computation workloads, this tradeoff is acceptable. For real-time fraud detection or session-level recommendation, it is not.

### 1.3 The Streaming Duality: Tables and Streams

One of the most powerful mental models in stream processing is the **table-stream duality**, first articulated clearly in the Kafka ecosystem.

**A stream is a changelog of a table.** Every INSERT, UPDATE, and DELETE to a database table can be captured as a stream of change events (CDC). Given the stream from the beginning of time, you can reconstruct the current table state by replaying all events.

**A table is a materialized view of a stream.** If you accumulate a stream of events into a key-value store (e.g., using Kafka's log compaction), you get the latest value for each key -- which is a table.

```
STREAM-TABLE DUALITY:

Stream (changelog):                    Table (materialized state):
  t=1: INSERT user_123, name="Alice"     ┌───────────┬──────────┐
  t=2: INSERT user_456, name="Bob"       │ user_123  │ "Alice"  │  (current state
  t=3: UPDATE user_123, name="Alicia"    │ user_456  │ "Bob"    │   at t=4)
  t=4: DELETE user_456                   └───────────┴──────────┘
                │                                  ▲
                │    replay all events             │
                └──────────────────────────────────┘
                ▲                                  │
                │    capture every change           │
                └──────────────────────────────────┘
```

This duality matters for ML system design because feature stores exploit it directly:
- The **stream** is the raw event feed (user clicks, purchases, page views).
- The **table** is the materialized feature vector (user_click_count_last_1h, user_avg_purchase_amount).
- Flink sits in between: it reads the stream, computes windowed aggregations, and materializes the result into the feature table (Redis for online serving, S3/Hive for offline training).

### 1.4 Event Time vs Processing Time vs Ingestion Time

This is the single most important concept in stream processing and the source of most complexity.

**Event time** is when the event actually occurred in the real world. A user clicked a button at 14:03:27.412 UTC. This timestamp is embedded in the event payload by the producer.

**Ingestion time** is when the event entered the stream processing system. It was received by Kafka at 14:03:27.891 UTC (479ms after the event). This timestamp is assigned by the messaging system.

**Processing time** is when the stream processing operator handles the event. Flink processed it at 14:03:28.203 UTC (791ms after the event, 312ms after ingestion).

```
EVENT TIMELINE:

  14:03:27.412   14:03:27.891   14:03:28.203
       │              │              │
       ▼              ▼              ▼
   ┌────────┐    ┌─────────┐    ┌──────────┐
   │ Event  │    │Ingestion│    │Processing│
   │  Time  │    │  Time   │    │   Time   │
   └────────┘    └─────────┘    └──────────┘
   (user's       (Kafka         (Flink
    device)       broker)        operator)
```

**Why the distinction matters**: Consider computing "clicks per user in the last 5 minutes." If you use processing time, a batch of events delayed by 2 minutes (mobile device going through a tunnel) will be counted in the wrong window. If you use event time, those events are assigned to the correct historical window regardless of when they arrive at the processor.

For ML features, **event time is almost always correct**. A feature like `user_purchases_last_1h` must reflect the actual purchase times, not when Flink happened to process the records. Using processing time would cause training-serving skew: the offline training pipeline (batch, reprocessing historical data) would assign events to different windows than the online pipeline, producing different feature values for the same input.

### 1.5 Out-of-Order Events: Why They Happen

In a distributed system, events arrive out of order for multiple reasons:

1. **Network delays**: Different paths through the network have different latencies. Event A (t=100) routed through a congested switch arrives after event B (t=102) routed through an uncongested path.

2. **Mobile/IoT offline buffering**: A mobile device loses connectivity and buffers events locally. When it reconnects, it flushes a batch of events with timestamps from the offline period -- potentially minutes or hours old.

3. **Multi-partition joins**: Events from two Kafka partitions have independent ordering. When a Flink operator joins streams from partition 0 and partition 1, the interleaving is non-deterministic.

4. **Producer retries**: A Kafka producer retries a failed send. The retry succeeds, but the original send also eventually succeeds (it was delayed, not lost). Now the same event appears twice, and the retry may arrive before other events that were produced after the original send.

5. **Clock skew**: Different producers have slightly different system clocks. Producer A's clock is 3 seconds ahead of producer B's. Events from A appear to be "from the future" relative to B's events.

The magnitude of out-of-orderness varies dramatically by use case:

| Source | Typical Out-of-Order Delay |
|:---|:---|
| Server-side events (same datacenter) | < 1 second |
| Cross-datacenter replication | 1-10 seconds |
| Mobile app events (good connectivity) | 1-30 seconds |
| Mobile app events (spotty connectivity) | Minutes to hours |
| IoT sensors with batch upload | Hours to days |
| Late-arriving billing/settlement events | Days to weeks |

This variance is why watermarks exist: they give the system a principled way to decide "I have waited long enough for stragglers -- time to produce results."

---

## 2. Apache Flink Architecture

### 2.1 JobManager and TaskManagers

Flink uses a leader-worker architecture. The **JobManager** (JM) is the coordinator; **TaskManagers** (TMs) are the workers that execute the actual dataflow operators.

```
FLINK CLUSTER ARCHITECTURE:

  ┌─────────────────────────────────────────────────────┐
  │                    JobManager                        │
  │  ┌──────────────┐ ┌──────────────┐ ┌─────────────┐ │
  │  │  Dispatcher  │ │ResourceManager│ │ JobMaster   │ │
  │  │  (REST API,  │ │ (allocates   │ │ (per-job    │ │
  │  │   job submit)│ │  TM slots)   │ │  coordinator│ │
  │  └──────────────┘ └──────────────┘ └─────────────┘ │
  └───────────────────────┬─────────────────────────────┘
                          │ coordinate
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
  │ TaskManager 0│ │ TaskManager 1│ │ TaskManager 2│
  │ ┌──────────┐ │ │ ┌──────────┐ │ │ ┌──────────┐ │
  │ │ Slot 0   │ │ │ │ Slot 0   │ │ │ │ Slot 0   │ │
  │ │ [src→map]│ │ │ │ [src→map]│ │ │ │ [src→map]│ │
  │ ├──────────┤ │ │ ├──────────┤ │ │ ├──────────┤ │
  │ │ Slot 1   │ │ │ │ Slot 1   │ │ │ │ Slot 1   │ │
  │ │ [agg→snk]│ │ │ │ [agg→snk]│ │ │ │ [agg→snk]│ │
  │ └──────────┘ │ │ └──────────┘ │ │ └──────────┘ │
  └──────────────┘ └──────────────┘ └──────────────┘
```

The JobManager has three subcomponents:
- **Dispatcher**: Receives job submissions via REST API, starts a JobMaster per job.
- **ResourceManager**: Manages TaskManager slots. Integrates with YARN, Kubernetes, or standalone deployment for elastic resource allocation.
- **JobMaster**: One per running job. Coordinates checkpointing, tracks task status, handles failover.

TaskManagers are JVM processes. Each TM has a fixed number of **task slots** that determine how many parallel operator chains it can execute. A TM with 4 slots can run 4 parallel subtasks. The total parallelism of a Flink job is bounded by the total number of available slots across all TMs.

### 2.2 Task Slots, Parallelism, and Operator Chaining

**Parallelism** in Flink means the number of parallel instances of each operator. If you set parallelism to 6, each operator in the DAG runs as 6 subtasks distributed across available slots.

**Operator chaining** is Flink's key optimization: consecutive operators that have the same parallelism and are connected by a forward (non-shuffle) edge are fused into a single task. This eliminates serialization/deserialization and network transfer between chained operators.

```
OPERATOR CHAINING EXAMPLE:

Logical plan:
  Source(p=3) ──> Map(p=3) ──> KeyBy ──> Window+Aggregate(p=3) ──> Sink(p=3)

Physical plan (with chaining):
  [Source → Map](p=3)  ──shuffle──>  [Window+Aggregate → Sink](p=3)
       chain 1                              chain 2

  Chain 1 runs in 3 slots, chain 2 in 3 slots = 6 slots total.
  Within each chain, data passes as Java objects (no serialization).
  Between chains, data is serialized, shuffled by key, and deserialized.
```

Rules for chaining: operators must have the same parallelism, be connected by a forward partition strategy (not keyBy, rebalance, or broadcast), and neither operator may have disabled chaining explicitly.

### 2.3 Flink's Layered API

Flink provides three API layers, from high-level to low-level:

```
API LAYERS (highest abstraction to lowest):

  ┌─────────────────────────────────────────┐
  │         Table API / SQL                  │  Declarative, relational
  │   SELECT user_id, COUNT(*) FROM clicks   │  Auto-optimized query plans
  │   GROUP BY user_id, TUMBLE(event_time,   │  Schema-aware
  │           INTERVAL '1' HOUR)             │
  ├─────────────────────────────────────────┤
  │         DataStream API                   │  Functional, streaming-native
  │   stream.keyBy(e -> e.userId)            │  Explicit windowing, state
  │         .window(TumblingEventTimeWindows  │  Type-safe (Java/Scala)
  │                 .of(Time.hours(1)))      │
  │         .aggregate(new ClickCounter())   │
  ├─────────────────────────────────────────┤
  │         ProcessFunction                  │  Full control
  │   Access to: timers, raw state,          │  Register event-time timers
  │   side outputs, event-time service       │  Manual watermark handling
  │                                          │  Custom windowing logic
  └─────────────────────────────────────────┘
```

**Table API / SQL**: Best for ad-hoc analytics and simple aggregations. Flink's SQL planner (based on Apache Calcite) optimizes the query plan. In practice, many production ML feature pipelines use SQL for readability and maintainability.

**DataStream API**: The workhorse for production streaming jobs. Provides explicit control over windowing, state, and exactly-once semantics. Most Kafka-to-Flink-to-Redis feature pipelines are written at this level.

**ProcessFunction**: The escape hatch for when the DataStream API is not flexible enough. Gives raw access to timers (both event-time and processing-time), per-key state, and side outputs. Used for complex sessionization logic, custom watermark strategies, and CEP-like pattern matching without the CEP library.

### 2.4 Flink Program Structure

Every Flink program follows the same structure: configure the execution environment, define sources, apply transformations, define sinks, and execute.

```
FLINK JOB STRUCTURE:

  ┌────────────┐     ┌────────────────────────────┐     ┌───────────┐
  │   Source    │────>│      Transformations        │────>│   Sink    │
  │            │     │                              │     │           │
  │ - Kafka    │     │ - map(), flatMap(), filter() │     │ - Kafka   │
  │ - Files    │     │ - keyBy() + window()         │     │ - Redis   │
  │ - Custom   │     │ - connect() + coProcess()    │     │ - JDBC    │
  │ - Kinesis  │     │ - ProcessFunction            │     │ - S3      │
  └────────────┘     └────────────────────────────┘     └───────────┘

  StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
  env.setParallelism(12);
  env.enableCheckpointing(60_000);  // checkpoint every 60 seconds

  DataStream<ClickEvent> clicks = env.fromSource(kafkaSource, watermarkStrategy, "clicks");
  DataStream<FeatureVector> features = clicks
      .keyBy(event -> event.getUserId())
      .window(TumblingEventTimeWindows.of(Time.hours(1)))
      .aggregate(new ClickAggregator());
  features.sinkTo(redisSink);
  features.sinkTo(kafkaSink);  // dual-write

  env.execute("Click Feature Pipeline");
```

### 2.5 Deployment Modes

Flink supports three deployment modes, each with different lifecycle and isolation characteristics:

| Mode | Description | Use Case |
|:---|:---|:---|
| **Session cluster** | Long-running cluster, multiple jobs share TMs | Development, small jobs, cost optimization |
| **Per-job cluster** | One cluster per job, dedicated resources | Production, resource isolation between jobs |
| **Application mode** | main() runs on the cluster, not the client | Production on K8s, avoids client-side bottleneck |

In Kubernetes deployments (the dominant production pattern as of 2025), **application mode** is standard. The Flink job's JAR is baked into a Docker image, the main() method runs inside the cluster, and the Flink Kubernetes Operator manages lifecycle (deploy, upgrade, savepoint, rollback).

### 2.6 State Backends

The state backend determines where Flink stores operator state during processing:

**HashMapStateBackend** (formerly MemoryStateBackend): State lives as Java objects on the JVM heap. Fast (no serialization during access) but limited by available heap memory. Suitable for jobs with small state (< 5-10 GB per TM). Checkpoints serialize the entire heap state to the checkpoint storage.

**EmbeddedRocksDBStateBackend** (formerly RocksDBStateBackend): State is stored in an embedded RocksDB instance on local disk. State can grow far beyond JVM heap (limited only by disk). State access involves serialization/deserialization per read/write, adding ~10-50us overhead per access compared to heap. Supports **incremental checkpointing**: only the RocksDB SST files that changed since the last checkpoint are uploaded, dramatically reducing checkpoint size for large state.

```
STATE BACKEND DECISION:

  State size < 5 GB per TaskManager?
    ├── Yes ──> HashMapStateBackend (faster access, simpler)
    └── No  ──> EmbeddedRocksDBStateBackend (scales to TBs)
                 └── Enable incremental checkpoints
                 └── Tune: block_cache_size, write_buffer_count
                 └── Use SSD for local state directory
```

For ML feature pipelines processing 100M+ users with windowed aggregations, RocksDB is almost always the right choice. The state often reaches hundreds of GB to TB scale.

---

## 3. Windowing Deep Dive

### 3.1 Tumbling Windows

Fixed-size, non-overlapping windows that partition the stream into consecutive, equal-duration buckets.

```
TUMBLING WINDOWS (5-minute):

Event time:  00:00    00:05    00:10    00:15    00:20
              │        │        │        │        │
              ▼        ▼        ▼        ▼        ▼
  ┌──────────┐┌──────────┐┌──────────┐┌──────────┐
  │ Window 1 ││ Window 2 ││ Window 3 ││ Window 4 │
  │ [00:00,  ││ [00:05,  ││ [00:10,  ││ [00:15,  │
  │  00:05)  ││  00:10)  ││  00:15)  ││  00:20)  │
  │ *** *    ││  **  *** ││ *     *  ││ ****     │
  └──────────┘└──────────┘└──────────┘└──────────┘
  Each * is an event. Each event belongs to exactly one window.
```

**Properties**: Every event belongs to exactly one window. Window boundaries are aligned to the epoch (not to the first event). A 1-hour tumbling window always starts at :00, not at whenever the first event arrives.

**ML feature use case**: `user_clicks_last_1h` as a tumbling 1-hour window. Produces one aggregate per user per hour. Simple, no overlap, minimal state (one accumulator per key per window).

**State cost**: O(keys x 1) -- one accumulator per key because there is only one active window at a time.

### 3.2 Sliding Windows

Fixed-size windows that overlap. Defined by two parameters: window size and slide interval.

```
SLIDING WINDOWS (size=1h, slide=15min):

  00:00       00:15       00:30       00:45       01:00       01:15
    │           │           │           │           │           │
    ├───────────────────────────────────┤                        Window [00:00, 01:00)
    │           ├───────────────────────────────────┤            Window [00:15, 01:15)
    │           │           ├───────────────────────────────────┤Window [00:30, 01:30)
    │           │           │           ├───────────────────────────────────┤ ...
    │           │           │           │           │           │

  Each event belongs to (size / slide) = 4 windows simultaneously.
```

**Properties**: Each event is assigned to `ceil(size / slide)` windows. For a 1-hour window sliding every 15 minutes, each event belongs to 4 windows. This means 4x the state and computation compared to tumbling.

**ML feature use case**: `user_avg_purchase_amount_last_7d` sliding daily. The feature needs to be updated every day, but the window covers 7 days. A sliding window with size=7d, slide=1d produces a daily rolling average.

**State cost**: O(keys x (size/slide)). For size=30d, slide=1h, that is 720 concurrent windows per key. This gets expensive fast. In practice, use incremental aggregation (ReduceFunction or AggregateFunction) so that each window stores only the accumulator, not all events.

**Warning**: Flink internally creates `(size / slide)` window panes. A 24-hour window sliding every 1 minute creates 1,440 panes per key. This can exhaust memory. For long windows with fine-grained slides, consider using a ProcessFunction with custom state instead.

### 3.3 Session Windows

Gap-based windows that group events per key until a period of inactivity (the gap) is detected. Unlike tumbling and sliding windows, session windows have no fixed size -- they are defined solely by the maximum gap between consecutive events.

```
SESSION WINDOWS (gap=30min):

  User A's events over time:

  10:00  10:05  10:12  10:18          10:55  11:02  11:08          11:45
    *      *      *      *              *      *      *              *
    ├──────────────────────┤            ├──────────────────┤        ├──┤
         Session 1                          Session 2           Session 3
    (10:00-10:48)                      (10:55-11:38)           (11:45-12:15)
    duration: 18min                     duration: 13min         single event
    events: 4                           events: 3               events: 1

  Note: window end = last_event_time + gap (30min)
```

**Properties**: Per-key, dynamic-size windows. A session window fires when no new event arrives for the key within the gap duration. Session windows require **merging**: when a new event arrives, Flink checks whether it falls within the gap of an existing session window. If it extends an existing session, the windows merge. If it falls between two sessions and bridges the gap, those two sessions merge into one.

**ML feature use case**: Recommendation systems need session-level features: `items_viewed_this_session`, `session_duration`, `categories_explored_this_session`. Session windows compute these naturally without requiring the application to define session boundaries.

**State cost**: O(keys x active_sessions). Since sessions merge, the number of active session windows is bounded by the number of keys with recent activity. However, the merge operation requires storing all events (or a mergeable accumulator) for each session until it closes.

### 3.4 Global Windows

A single window that contains all events for a key. Global windows never fire on their own -- they require a **custom trigger** to determine when to emit results.

Use cases: event-count-based windows ("emit after every 100 events"), custom business logic triggers ("emit when the user has viewed 5 different categories"), or when you want to handle all windowing logic in a ProcessFunction but still use the window API.

### 3.5 Window Assigners, Triggers, and Evictors

Every window operation in Flink is composed of three components:

**WindowAssigner**: Determines which window(s) an event belongs to. The four built-in types above are all WindowAssigners. Custom assigners are possible but rarely needed.

**Trigger**: Determines when a window's contents should be emitted (fired). The default trigger fires when the watermark passes the window's end time. Custom triggers can fire on event count, processing-time timers, or any combination. Triggers can also fire incrementally (FIRE) or fire and purge the window's contents (FIRE_AND_PURGE).

**Evictor**: Optionally removes elements from the window before or after the trigger fires. Rarely used in production because it prevents optimized pre-aggregation (Flink must buffer all elements if an evictor is present).

### 3.6 Late Data Handling

When a watermark passes a window's end time, the window fires. But what about events that arrive after the watermark -- late events?

Flink provides three mechanisms:

1. **Allowed lateness**: After a window fires, keep it open for an additional duration. Late events that arrive within the allowed lateness period trigger a re-fire of the window with updated results. After the allowed lateness expires, the window state is purged and any subsequent events are dropped.

2. **Side outputs**: Events that arrive after the allowed lateness period can be redirected to a side output stream for separate processing (logging, reconciliation, a secondary slower pipeline).

3. **Watermark delay**: The simplest approach -- set a larger watermark delay to wait longer for late events before firing windows. This increases end-to-end latency but reduces the number of late events.

```
LATE DATA HANDLING TIMELINE:

  Watermark delay: 10 sec
  Window: [12:00:00, 12:05:00)
  Allowed lateness: 1 minute

  12:05:10  Watermark reaches 12:05:00 → window FIRES (first result)
  12:05:30  Late event with t=12:04:55 arrives → window RE-FIRES (updated result)
  12:06:10  Allowed lateness expires → window state PURGED
  12:06:30  Very late event with t=12:04:50 arrives → sent to SIDE OUTPUT
```

### 3.7 Practical: Which Window Type for Which ML Feature

| Feature | Window Type | Configuration | Notes |
|:---|:---|:---|:---|
| `user_clicks_last_1h` | Tumbling | size=1h | Simple hourly count |
| `user_clicks_last_5m` (real-time) | Tumbling | size=5m | Low-latency feature |
| `avg_purchase_amount_last_7d` | Sliding | size=7d, slide=1d | Rolling average |
| `items_viewed_this_session` | Session | gap=30min | Session-level feature |
| `trending_items_last_15m` | Sliding | size=15m, slide=1m | Rapid refresh |
| `user_active_days_last_30d` | Sliding | size=30d, slide=1d | Count-distinct per day |
| `peak_hourly_requests` | Tumbling | size=1h | Global max tracking |

---

## 4. Watermarks -- The Key to Event-Time Processing

### 4.1 What a Watermark Is

A watermark is a **monotonically increasing timestamp** that flows through the processing DAG as a special element in the stream. A watermark with timestamp `W` is an assertion: **the system believes it has received all events with event time `t <= W`**. When an operator receives a watermark `W`, it can confidently close all windows that end at or before `W` and emit their results.

Watermarks are not guarantees -- they are heuristics. An event with `t < W` can still arrive after the watermark (a late event). The system handles late events through the mechanisms in Section 3.6.

```
WATERMARK IN THE STREAM:

  Stream of events with event times:

  ... [e, t=10:03] [e, t=10:01] [W=10:00] [e, t=9:58] [e, t=9:55] [e, t=9:52] ...
                                    │
                                    │ "All events with t <= 10:00
                                    │  should have arrived by now"
                                    │
                                    ▼
  Any window ending at or before 10:00 can now fire.
```

Key properties:
- **Monotonically increasing**: A watermark never goes backward. If you see W=100, you will never see W=99 later.
- **Per-stream**: Each source and each operator maintains its own watermark.
- **Not per-event**: Watermarks are separate elements that flow between data events. They are generated periodically or on punctuation markers.

### 4.2 Watermark Generation Strategies

**Periodic watermarks** (most common): Flink calls a watermark generator function every `autoWatermarkInterval` milliseconds (default 200ms). The generator inspects the events it has seen and computes the watermark as `max_observed_event_time - bounded_out_of_orderness`.

```java
WatermarkStrategy
    .<Event>forBoundedOutOfOrderness(Duration.ofSeconds(10))
    .withTimestampAssigner((event, timestamp) -> event.getEventTime());
```

This says: "Events can arrive up to 10 seconds late. The watermark trails the maximum observed event time by 10 seconds." If the highest event time seen so far is 14:03:30, the watermark is 14:03:20.

**Punctuated watermarks**: Watermarks are emitted in response to specific events in the stream -- special marker events that indicate progress. Used when the event source provides explicit progress markers (e.g., "end of batch" markers from a mobile device flush).

**Source-specific: per-partition watermarks in Kafka**: When reading from Kafka, Flink tracks watermarks **per partition**. Each partition independently advances its watermark based on the events it contains. The source operator's effective watermark is the **minimum across all partitions**.

```
PER-PARTITION WATERMARK (Kafka Source):

  Partition 0: events up to t=10:03:25  → local watermark = 10:03:15
  Partition 1: events up to t=10:03:30  → local watermark = 10:03:20
  Partition 2: events up to t=10:03:28  → local watermark = 10:03:18

  Source effective watermark = min(10:03:15, 10:03:20, 10:03:18) = 10:03:15
                                    ^
                                    Partition 0 is the bottleneck
```

### 4.3 Watermark Propagation Through the DAG

Watermarks flow through the operator DAG like data events. Each operator receives watermarks from its upstream operators. An operator's output watermark is the **minimum of the latest watermarks from all its input channels**.

```
WATERMARK PROPAGATION:

                     Source 0               Source 1
                     (W=10:03:15)           (W=10:03:22)
                         │                      │
                         ▼                      ▼
                    ┌──────────────────────────────┐
                    │        KeyBy + Shuffle        │
                    │                               │
                    │  Subtask A         Subtask B  │
                    │  input 0: W=15     input 0: W=15
                    │  input 1: W=22     input 1: W=22
                    │  output: min=15    output: min=15
                    └──────────┬───────────────────┘
                               │
                               ▼
                    ┌──────────────────┐
                    │    Sink (W=15)    │
                    └──────────────────┘

  The slowest source (W=10:03:15) determines the watermark for the
  entire downstream pipeline. One stalled source stalls ALL windows.
```

This has a critical implication: a single slow source can hold back the entire pipeline's watermark progress. This is the idle source problem.

### 4.4 The Bounded-Out-of-Orderness Model

The most common watermark strategy is the bounded-out-of-orderness model:

```
watermark(t) = max_event_time_seen_so_far - max_allowed_delay
```

Where `max_allowed_delay` is a configuration parameter that represents the maximum expected delay between event creation and arrival.

**How to choose `max_allowed_delay`**:

| Source Type | Typical Setting | Reasoning |
|:---|:---|:---|
| Server-side events | 1-5 seconds | Low network variance within a datacenter |
| Cross-datacenter events | 5-30 seconds | Inter-DC replication lag |
| Web/mobile (connected) | 10-60 seconds | CDN and client-side batching |
| Mobile (intermittent) | 5-30 minutes | Offline buffering + reconnection |
| IoT batch upload | 1-24 hours | Devices upload on schedule |

**The tradeoff**:

```
WATERMARK DELAY TRADEOFF:

  max_allowed_delay = 5 seconds:
    + Low latency (windows fire 5 sec after window end)
    - High late event rate (many events arrive > 5 sec late)
    - Data loss if no allowed-lateness configured

  max_allowed_delay = 5 minutes:
    + Very low late event rate
    - High latency (windows fire 5 min after window end)
    - 5 min delay before features are available in the store

  FORMULA for end-to-end feature freshness:
    feature_available_at = window_end + max_allowed_delay + processing_time + sink_write_time
    Example: window_end=12:05:00, delay=10s, processing=2s, sink=1s
           → feature available at ~12:05:13
```

### 4.5 Idle Sources

When a Kafka partition stops producing events (e.g., a low-traffic partition, or a partition whose producer is temporarily down), its watermark stops advancing. Because the source's effective watermark is the minimum across all partitions, one idle partition stalls the entire pipeline.

```
IDLE SOURCE PROBLEM:

  Partition 0: active, events flowing  → watermark advancing (10:03:30)
  Partition 1: active, events flowing  → watermark advancing (10:03:28)
  Partition 2: IDLE (no events)        → watermark stuck at (10:01:00)

  Source watermark = min(10:03:30, 10:03:28, 10:01:00) = 10:01:00
                                                              ^
                                              Pipeline stuck 2.5 min behind!
                                              No windows fire. Features stale.
```

**Solution 1: withIdleness()** (Flink 1.11+)

```java
WatermarkStrategy
    .<Event>forBoundedOutOfOrderness(Duration.ofSeconds(10))
    .withIdleness(Duration.ofMinutes(1));
```

If no events arrive on a partition for 1 minute, Flink marks it as idle and excludes it from the minimum watermark computation. The partition is automatically re-included when events resume.

**Solution 2: Source-level heartbeat watermarks**: The producer periodically writes a "heartbeat" record with the current timestamp to every partition, even those with no business events. This keeps the watermark advancing on all partitions.

**Solution 3: Partitioning strategy**: If some partitions are inherently low-traffic, consider reducing the number of partitions or using a hash key that distributes events more evenly.

---

## 5. State Management

### 5.1 Keyed State

Keyed state is state that is partitioned by key. After a `keyBy()` operation, each key has its own isolated state namespace. Flink guarantees that all events for the same key are processed by the same operator subtask, and that subtask accesses only that key's state.

Flink provides five keyed state primitives:

| State Type | Java Type | Description | Use Case |
|:---|:---|:---|:---|
| `ValueState<T>` | Single value | One value per key | Running count, latest value |
| `ListState<T>` | `List<T>` | Append-only list per key | Buffering events for a pattern |
| `MapState<K, V>` | `Map<K, V>` | Key-value map per key | Per-user per-item interactions |
| `ReducingState<T>` | Single value | Combines via ReduceFunction | Incremental sum/max/min |
| `AggregatingState<IN, OUT>` | Single value | Combines via AggregateFunction | Incremental average (sum + count) |

**Important for RocksDB**: `MapState<K, V>` stores each map entry as a separate RocksDB key-value pair, making individual entry access O(1). In contrast, `ValueState<Map<K, V>>` serializes the entire map on every read/write, which is O(n) in the map size. Always prefer `MapState` over `ValueState<Map>` when using RocksDB.

### 5.2 Operator State

Operator state is non-keyed state that belongs to an operator subtask, not to a key. It is used by source and sink connectors and by operators that need to maintain state independently of the key space.

- **ListState**: The most common operator state. On checkpoint, each subtask snapshots its list. On recovery with a different parallelism, lists are redistributed (union or even-split redistribution).
- **BroadcastState**: Used with broadcast streams. A small, slowly-changing dataset (e.g., ML model parameters, feature configuration) is broadcast to all subtasks. Each subtask maintains an identical copy of the broadcast state.

```
BROADCAST STATE PATTERN (common for ML):

  Model update stream     Click event stream
  (low volume, broadcast) (high volume, keyed)
         │                       │
         ▼                       ▼
  ┌─────────────────────────────────────────┐
  │      BroadcastProcessFunction            │
  │                                          │
  │  processBroadcastElement():              │
  │    broadcastState.put("model", newModel) │
  │                                          │
  │  processElement():                       │
  │    model = broadcastState.get("model")   │
  │    score = model.predict(click)          │
  │    emit(click, score)                    │
  └─────────────────────────────────────────┘

  Use case: real-time scoring with a model that updates every few minutes.
  The model is broadcast; click events are keyed by user_id.
```

### 5.3 State TTL

State TTL (Time-To-Live) automatically cleans up stale state entries. This is critical for long-running streaming jobs where state would otherwise grow unboundedly.

```java
StateTtlConfig ttlConfig = StateTtlConfig
    .newBuilder(Duration.ofDays(7))
    .setUpdateType(StateTtlConfig.UpdateType.OnCreateAndWrite)
    .setStateVisibility(StateTtlConfig.StateVisibility.NeverReturnExpired)
    .cleanupInRocksdbCompactFilter(1000)  // clean during RocksDB compaction
    .build();

ValueStateDescriptor<UserFeatures> descriptor = new ValueStateDescriptor<>("user-features", UserFeatures.class);
descriptor.enableTimeToLive(ttlConfig);
```

TTL cleanup strategies:
- **Lazy cleanup**: Check TTL on access. Expired entries are deleted when read. Does not reclaim space for entries never read again.
- **Full snapshot cleanup**: During checkpointing, filter out expired entries. Adds checkpoint overhead.
- **RocksDB compaction filter**: Clean expired entries during RocksDB's background compaction. Most efficient for RocksDB state backend. Configure `cleanupInRocksdbCompactFilter` with a batch size to limit overhead per compaction cycle.

### 5.4 State Serialization and Schema Evolution

Flink serializes all state for checkpoints and network transfer. The default serialization uses Flink's **TypeSerializer** framework, which generates efficient serializers for POJOs, Tuples, and primitives. For types Flink cannot serialize natively, it falls back to **Kryo**, which is slower and produces larger serialized output.

**Schema evolution** matters because streaming jobs run for months or years. When you add a field to a state POJO, you need the new code to read state checkpointed by the old code. Flink supports schema evolution for POJO state (add/remove fields) and Avro state (full Avro evolution rules). Kryo-serialized state does not support evolution -- this is a common production trap.

**Best practice**: Use POJOs or Avro for state types. Register all Kryo serializers explicitly. Test savepoint compatibility as part of your CI/CD pipeline.

### 5.5 State Size Management

When state grows large (tens of GB to TBs), these strategies keep the pipeline operational:

**RocksDB + incremental checkpointing**: The first line of defense. RocksDB spills to disk, and incremental checkpoints upload only changed SST files. A 500 GB state with 1% churn per checkpoint interval produces ~5 GB checkpoint uploads.

**Timers for TTL-based cleanup**: For patterns where state should expire after a time window, register a processing-time or event-time timer to clean up the entry. This is more precise than state TTL for complex expiration logic.

**Approximate data structures**: When exact computation is too expensive:

| Structure | Use Case | Space | Error |
|:---|:---|:---|:---|
| HyperLogLog (HLL) | Count-distinct (unique users) | ~12 KB fixed | ~0.8% standard error |
| Count-Min Sketch | Frequency estimation (item counts) | ~100 KB | Overcount only, bounded |
| Bloom filter | Set membership (seen this event?) | ~10 bits/element | False positives only |
| T-Digest | Quantile estimation (p99 latency) | ~5-10 KB | ~1% at extreme quantiles |

**Example**: Computing `unique_visitors_last_24h` over 500M users with exact count would require ~4 GB of state (HashSet of user IDs). With HyperLogLog, it requires 12 KB with 0.8% error. For feature computation in ML, this accuracy is usually more than sufficient.

---

## 6. Checkpointing and Exactly-Once Semantics

### 6.1 The Chandy-Lamport Algorithm (Adapted for Flink)

Flink's checkpointing is based on the Chandy-Lamport distributed snapshot algorithm, adapted for the streaming dataflow model. The key insight: by injecting special markers (checkpoint barriers) into the data stream, each operator can take a consistent snapshot of its state without stopping processing.

The algorithm proceeds in four phases:

**Phase 1: Barrier injection.** The JobManager triggers a checkpoint by sending a checkpoint barrier (with checkpoint ID `n`) to all source operators. Each source snapshots its current position (e.g., Kafka offsets) and injects the barrier into its output stream.

**Phase 2: Barrier propagation.** Barriers flow through the DAG as regular stream elements. They are not overtaken by data events and do not overtake data events. This ordering guarantee is what makes the snapshot consistent.

**Phase 3: Barrier alignment.** When an operator with multiple input channels receives barrier `n` from one input but not yet from another, it must align:
- It buffers events from the input channel whose barrier has arrived.
- It continues processing events from input channels whose barrier has not yet arrived.
- When barrier `n` arrives from ALL input channels, the operator snapshots its state.

**Phase 4: State snapshot.** Once aligned, the operator asynchronously writes its state to durable storage (S3, HDFS). When all operators have acknowledged their snapshot, the JobManager marks checkpoint `n` as complete.

```
CHECKPOINT BARRIER PROPAGATION:

  Source A         Source B
  offset=142       offset=287
     │                │
     │ data  data     │ data  data
     │  ║ barrier n   │  ║ barrier n
     │ data  data     │ data  data
     ▼                ▼
  ┌──────────────────────────────────┐
  │         Keyed Aggregator          │
  │                                   │
  │  1. Receive barrier n from A      │
  │     → buffer A's subsequent data  │
  │     → continue processing B's     │
  │                                   │
  │  2. Receive barrier n from B      │
  │     → SNAPSHOT state to S3        │
  │     → stop buffering              │
  │     → inject barrier n downstream │
  └──────────────┬───────────────────┘
                 │
                 ▼
  ┌──────────────────────────────────┐
  │              Sink                 │
  │  Receive barrier n               │
  │  → snapshot (pre-commit txn)     │
  │  → acknowledge to JobManager     │
  └──────────────────────────────────┘

  JobManager: all operators acknowledged checkpoint n → COMPLETE
              → notify sinks to COMMIT transactions
              → record checkpoint n as latest completed
```

### 6.2 Unaligned Checkpoints (Flink 1.11+)

Barrier alignment has a problem: when there is backpressure, the barrier from a fast input channel may be blocked behind buffered data from a slow channel. The operator buffers data from the fast channel while waiting, which exacerbates backpressure and increases checkpoint duration. In extreme cases, checkpoints time out.

**Unaligned checkpoints** solve this by NOT waiting for barrier alignment. Instead:
1. When barrier `n` arrives from ANY input channel, the operator immediately snapshots its state.
2. All in-flight data (events in network buffers and internal queues) between the arrived barrier and the not-yet-arrived barriers are included in the snapshot.
3. On recovery, these in-flight records are replayed, restoring the exact pre-checkpoint state.

```
ALIGNED vs UNALIGNED CHECKPOINTS:

Aligned (default):
  Input A: [data] [data] [barrier] [data] [data]
  Input B: [data] [data] [data] [data] [barrier] [data]
                                   ^
                          Waiting for B's barrier.
                          A's subsequent data is BUFFERED.
                          Processing stalls on A's side.

Unaligned:
  Input A: [data] [data] [barrier] [data] [data]
  Input B: [data] [data] [data] [data] [barrier] [data]
                          ^
                Barrier from A arrives first.
                Snapshot state IMMEDIATELY.
                In-flight data from B (between where B's
                barrier will arrive) included in snapshot.
                NO buffering. NO stall.
```

**Tradeoff**: Unaligned checkpoints produce larger snapshots (they include in-flight data) but complete faster under backpressure. Use them when checkpoint timeouts are a recurring problem.

### 6.3 Exactly-Once End-to-End

**The critical distinction**: Flink's checkpointing provides exactly-once **within the Flink pipeline** -- each event is processed exactly once, and state reflects this. But end-to-end exactly-once (from source to sink) requires cooperation from the external systems.

```
END-TO-END EXACTLY-ONCE:

  ┌─────────┐     ┌───────────────┐     ┌──────────┐
  │  Kafka   │────>│     Flink     │────>│  Kafka   │
  │ (Source) │     │  (Processor)  │     │  (Sink)  │
  └─────────┘     └───────────────┘     └──────────┘
  Exactly-once     Exactly-once          Exactly-once
  SOURCE:          INTERNAL:             SINK:
  Replay from      Chandy-Lamport       Two-phase commit
  checkpointed     checkpointing        (Kafka transactions)
  offsets                                OR idempotent writes
```

**Source exactly-once**: The source must be replayable. On recovery, Flink rolls back to the last checkpoint's source offsets and replays from there. Kafka is naturally replayable (seek to offset). File sources can seek to byte position. Non-replayable sources (TCP sockets) cannot provide exactly-once.

**Sink exactly-once via Two-Phase Commit (2PC)**:

The `TwoPhaseCommitSinkFunction` (or the newer `SinkV2` with `TwoPhaseCommittingSink`) implements:

1. **Pre-commit**: During normal processing, the sink writes output to the external system inside a transaction (e.g., a Kafka transaction, a database transaction). It does NOT commit.

2. **Checkpoint**: When the sink receives a checkpoint barrier, it flushes its current transaction and starts a new one. The old transaction is "pre-committed" -- all data is written but not visible to consumers.

3. **Commit**: When the JobManager notifies the sink that the checkpoint completed successfully, the sink commits the pre-committed transaction. Data becomes visible to downstream consumers.

4. **Abort**: If the checkpoint fails or the job restarts, the sink aborts the pre-committed transaction. Data is discarded.

```
TWO-PHASE COMMIT TIMELINE:

  Checkpoint n-1          Checkpoint n            Checkpoint n+1
  complete                triggered               complete
     │                       │                       │
     ▼                       ▼                       ▼
  ┌──────────────────────┬──────────────────────┬────────────┐
  │   Transaction T1     │   Transaction T2     │  Txn T3    │
  │   (write events)     │   (write events)     │  (write...)│
  │                      │                      │            │
  │   COMMIT T0          │   PRE-COMMIT T1      │  COMMIT T2 │
  │   (previous txn)     │   (flush, start T2)  │            │
  └──────────────────────┴──────────────────────┴────────────┘

  If checkpoint n fails: ABORT T1, replay events from checkpoint n-1.
  Events in T1 were never committed, so no duplicates.
```

**Sink exactly-once via Idempotent Writes**:

An alternative to 2PC: make writes idempotent. If the same event is written twice (due to replay after failure), the second write is a no-op.

For Redis: `SET user:123:click_count 42` is idempotent. `INCR user:123:click_count` is NOT.

For databases: `INSERT ... ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value WHERE version < EXCLUDED.version` is idempotent when the version is derived from the checkpoint epoch.

**Idempotent writes are simpler than 2PC but require careful key/version design.** For feature store dual-writes, the idempotent pattern is preferred for Redis (online store) and 2PC for Kafka (offline store).

### 6.4 Checkpoint Storage

Checkpoints are stored on durable filesystem storage:
- **S3** (most common in AWS deployments): High durability, moderate latency. Checkpoint writes are parallel across operators.
- **HDFS**: Common in on-premise Hadoop clusters. Lower latency than S3 for small checkpoints.
- **GCS/Azure Blob Storage**: Cloud-specific equivalents of S3.

Checkpoint configuration:

```
# Checkpoint every 60 seconds
execution.checkpointing.interval: 60000

# Minimum pause between checkpoints (prevents overlapping)
execution.checkpointing.min-pause: 30000

# Checkpoint timeout (abort if not complete in time)
execution.checkpointing.timeout: 600000

# Number of checkpoints to retain
state.checkpoints.num-retained: 3

# Incremental checkpoints for RocksDB
state.backend.incremental: true
```

**Sizing guideline**: Checkpoint duration ~ (state_size / upload_bandwidth) for full checkpoints, or (changed_state_size / upload_bandwidth) for incremental. A 100 GB state with 1% churn and 500 MB/s upload bandwidth: incremental checkpoint takes ~2 seconds. A full checkpoint would take ~200 seconds.

### 6.5 Savepoints vs Checkpoints

| Property | Checkpoint | Savepoint |
|:---|:---|:---|
| Trigger | Automatic (periodic) | Manual (CLI, REST API) |
| Purpose | Failure recovery | Planned operations |
| Format | Optimized (can be incremental) | Canonical, portable |
| Retained | Last N, auto-cleaned | Kept until manually deleted |
| Use case | Crash recovery | Job upgrade, A/B test, migration |
| State compatibility | Same job only | Can change parallelism, add operators |

**Savepoint workflow for job upgrades**:
1. Trigger savepoint: `flink savepoint <job-id> s3://savepoints/`
2. Stop the job: `flink cancel <job-id>`
3. Deploy new code version
4. Resume from savepoint: `flink run -s s3://savepoints/<savepoint-path> new-job.jar`

Savepoints enable zero-downtime upgrades of streaming pipelines -- a requirement for production ML feature stores that cannot tolerate hours-long state rebuilds.

---

## 7. Flink + Kafka Integration

### 7.1 KafkaSource

The Kafka source connector is the most battle-tested Flink source. Key behaviors:

**Per-partition offset tracking**: Flink tracks the current read offset for each Kafka partition in its operator state. On checkpoint, these offsets are snapshotted. On recovery, Flink seeks each partition to the checkpointed offset and replays.

**Watermark strategy per partition**: Each Kafka partition generates its own watermark. The source's output watermark is the minimum across all partitions (see Section 4.2).

```java
KafkaSource<Event> source = KafkaSource.<Event>builder()
    .setBootstrapServers("kafka:9092")
    .setTopics("user-clicks")
    .setGroupId("flink-feature-pipeline")   // for offset tracking only
    .setStartingOffsets(OffsetsInitializer.committedOffsets(OffsetResetStrategy.EARLIEST))
    .setDeserializer(new EventDeserializer())
    .build();

WatermarkStrategy<Event> watermarkStrategy = WatermarkStrategy
    .<Event>forBoundedOutOfOrderness(Duration.ofSeconds(10))
    .withTimestampAssigner((event, ts) -> event.getTimestamp())
    .withIdleness(Duration.ofMinutes(2));

DataStream<Event> stream = env.fromSource(source, watermarkStrategy, "kafka-source");
```

### 7.2 KafkaSink

**Exactly-once via Kafka transactions**: The Kafka sink uses Kafka's transactional API for exactly-once delivery. Each sink subtask uses a unique `transactional.id` (derived from the job ID and subtask index). On checkpoint, the sink pre-commits its Kafka transaction; on checkpoint completion, it commits.

```java
KafkaSink<FeatureUpdate> sink = KafkaSink.<FeatureUpdate>builder()
    .setBootstrapServers("kafka:9092")
    .setRecordSerializer(
        KafkaRecordSerializationSchema.builder()
            .setTopic("feature-updates")
            .setValueSerializationSchema(new FeatureUpdateSerializer())
            .build())
    .setDeliveryGuarantee(DeliveryGuarantee.EXACTLY_ONCE)
    .setTransactionalIdPrefix("feature-pipeline")
    .setProperty(ProducerConfig.TRANSACTION_TIMEOUT_CONFIG, "900000")  // 15 min
    .build();
```

**Critical detail**: Kafka's `transaction.timeout.ms` on the broker (default 15 minutes) must be greater than the checkpoint interval plus the maximum checkpoint duration. If a Kafka transaction times out before the checkpoint completes, the transaction is aborted and data is lost.

### 7.3 Consumer Group Semantics

Flink does NOT use Kafka consumer groups in the traditional sense. The `group.id` is set for compatibility but Flink manages offsets internally:
- Offsets are stored in Flink's state (checkpoints), not committed to Kafka's `__consumer_offsets`.
- Flink can optionally commit offsets to Kafka for monitoring purposes (lag monitoring via Burrow or Kafka's consumer group describe) but these committed offsets are not used for recovery.
- Partition assignment is handled by Flink's source split assigner, not Kafka's consumer group protocol.

### 7.4 Partition Discovery

When new partitions are added to a Kafka topic, Flink can discover them dynamically:

```java
KafkaSource.builder()
    // ...
    .setProperty("partition.discovery.interval.ms", "30000")  // check every 30 sec
    .build();
```

New partitions are assigned to source subtasks, and reading begins from the configured starting offset (usually earliest or latest).

### 7.5 Kafka Topic to Flink Parallelism Mapping

The Kafka source's parallelism should match or be a multiple of the number of Kafka partitions. If Flink parallelism > partitions, some source subtasks will be idle (waste resources). If Flink parallelism < partitions, each subtask reads from multiple partitions (fine, but increases per-subtask load).

**Recommended**: Set the Kafka source parallelism equal to the number of partitions. Downstream operators can have different parallelism based on their computational needs (with a keyBy shuffle in between).

---

## 8. Real-Time Feature Computation Patterns

### 8.1 Sliding Window Aggregation for Feature Store

The most common ML streaming pattern: compute a sliding window aggregate over a keyed stream and write the result to a feature store.

```
SLIDING WINDOW FEATURE COMPUTATION:

  Kafka (purchases)          Flink                    Feature Store
  ┌─────────────────┐   ┌──────────────────┐   ┌─────────────────────┐
  │ user_id: 123    │   │ keyBy(user_id)   │   │ Redis (online)      │
  │ amount: $42.50  │──>│ .window(sliding   │──>│ user:123:avg_30d    │
  │ ts: 2025-01-15  │   │   30d, slide 1h) │   │  = $38.72           │
  │                 │   │ .aggregate(       │   ├─────────────────────┤
  │ user_id: 123    │   │   avgAmount)     │   │ S3/Kafka (offline)  │
  │ amount: $18.99  │   │                  │   │ user:123:avg_30d    │
  │ ts: 2025-01-15  │   │                  │   │  = $38.72           │
  └─────────────────┘   └──────────────────┘   └─────────────────────┘
```

**Key detail**: For a 30-day window sliding every hour, use `AggregateFunction` (not `ProcessWindowFunction`). The aggregate function maintains only the accumulator (sum + count for average), not all events. This reduces state from O(events_in_window) to O(1) per key per window.

However, a 30-day window sliding every hour creates 720 window panes per key. With 100M users, that is 72 billion active panes. Even with 16 bytes per accumulator, this is ~1.1 TB of state. Strategies:
- Use `ReduceFunction` or `AggregateFunction` to minimize per-pane state.
- Use RocksDB state backend with incremental checkpoints.
- Consider a `ProcessFunction` with custom sliding aggregation using a circular buffer (O(slide_count) per key instead of O(panes)).

### 8.2 Sessionization for Recommendation

Session windows compute session-level features for recommendation systems:

```java
DataStream<SessionFeatures> sessions = clickStream
    .keyBy(event -> event.getUserId())
    .window(EventTimeSessionWindows.withGap(Time.minutes(30)))
    .aggregate(new SessionAggregator(), new SessionCollector());
```

The `SessionAggregator` incrementally computes:
- `items_viewed`: count of distinct item IDs
- `categories_explored`: count of distinct category IDs
- `session_duration_sec`: max(event_time) - min(event_time)
- `click_rate`: clicks / session_duration
- `add_to_cart_count`: count of cart events

These features are emitted when the session closes (30 minutes of inactivity for that user) and written to the feature store for use in the next recommendation request.

### 8.3 Dual-Write Pattern for Feature Store

The dual-write pattern writes computed features to both an online store (Redis, for real-time serving) and an offline store (S3/Kafka, for training data generation).

```
DUAL-WRITE PATTERN:

                    ┌──────────────────────────────────────┐
                    │            Flink Pipeline              │
                    │                                        │
  Kafka ──────────>│  Source → KeyBy → Window → Aggregate   │
  (raw events)     │              │                          │
                    │              ▼                          │
                    │    ┌──────────────────┐                │
                    │    │ Computed Feature  │                │
                    │    │ user:123          │                │
                    │    │ avg_purchase: $39 │                │
                    │    │ version: ckpt_42  │                │
                    │    └───────┬───────┬──┘                │
                    │            │       │                    │
                    │            ▼       ▼                    │
                    │    ┌───────────┐ ┌──────────────────┐  │
                    │    │ Redis     │ │ Kafka topic       │  │
                    │    │ Sink      │ │ "feature-updates" │  │
                    │    │ (online)  │ │ (offline)         │  │
                    │    └───────────┘ └──────────────────┘  │
                    └──────────────────────────────────────┘
                                         │
                                         ▼
                                    ┌────────────┐
                                    │ S3 (via    │
                                    │  Kafka     │
                                    │  Connect)  │
                                    └────────────┘

  Exactly-once to Redis:  Idempotent SET with version/timestamp
    SET user:123:avg_purchase $39 NX_OR_GT_VERSION 42

  Exactly-once to Kafka:  Kafka transactions (2PC with checkpoint)
```

**Why dual-write instead of CDC from Redis?** Redis does not have a reliable change capture mechanism. Writing to both stores from Flink ensures consistency: both stores receive the same computed feature value from the same checkpoint epoch. The version/checkpoint epoch embedded in each write enables idempotent deduplication.

### 8.4 Real-Time Feedback Loop for Recommendation

The latency-critical path in recommendation systems: a user action should update their real-time features and influence the next recommendation within seconds.

```
REAL-TIME FEEDBACK LOOP:

  User clicks       Kafka           Flink               Redis        Recommendation
  on item      (user-actions)    (feature pipeline)    (features)     Service
     │               │                │                    │              │
     │──(1) click──>│                │                    │              │
     │               │──(2) event──>│                    │              │
     │               │               │──(3) update──────>│              │
     │               │               │  user:123          │              │
     │               │               │  recent_clicks=[A] │              │
     │               │               │                    │              │
     │──────────────────(4) request recommendation────────────────────>│
     │               │               │                    │◄─(5) read──│
     │               │               │                    │    features │
     │◄─────────────────────────(6) recommendations (use updated features)─│
     │               │               │                    │              │

  Latency budget:
    (1) Client → Kafka:    ~50ms  (producer batching + network)
    (2) Kafka → Flink:     ~10ms  (Flink polling interval)
    (3) Flink → Redis:     ~5ms   (pipeline processing + write)
    ──────────────────────────────
    Total event-to-feature: ~65ms (well under 5-second target)
    
    (4-6) serve recommendation:  ~100ms (model inference + feature fetch)
```

The key constraint: the feature pipeline must be fast enough that the updated features are available in Redis before the user's next interaction triggers a recommendation request. For most UIs, this is 1-5 seconds.

### 8.5 Complex Event Processing (CEP)

Flink's CEP library enables temporal pattern detection over event streams.

**Example pattern**: "User viewed the same item 3+ times in 10 minutes without purchasing it" -- trigger a discount offer.

```java
Pattern<ClickEvent, ?> pattern = Pattern.<ClickEvent>begin("views")
    .where(new SimpleCondition<ClickEvent>() {
        public boolean filter(ClickEvent event) {
            return event.getType().equals("VIEW");
        }
    })
    .timesOrMore(3)
    .within(Time.minutes(10))
    .notFollowedBy("purchase")
    .where(new SimpleCondition<ClickEvent>() {
        public boolean filter(ClickEvent event) {
            return event.getType().equals("PURCHASE");
        }
    });
```

CEP is useful for:
- Fraud detection: "3 purchases from different countries within 5 minutes"
- User engagement: "User opened app 5 days in a row" (daily session trigger)
- Anomaly detection: "Sensor reading exceeds threshold 3 times in 1 minute"
- Real-time alerting: "Service error rate exceeds 5% for 2 consecutive windows"

**State cost warning**: CEP stores all partially matched patterns in state. For high-cardinality keys with complex patterns, state can grow rapidly. Use `within()` to bound the time window and limit state growth.

---

## 9. Backpressure

### 9.1 What Backpressure Is

Backpressure occurs when a downstream operator cannot process events as fast as the upstream operator produces them. Network buffers fill up, and the pressure propagates backward through the pipeline until it reaches the source, which slows its read rate.

```
BACKPRESSURE PROPAGATION:

  Source        Map          Aggregate       Slow Sink
  (fast)       (fast)        (fast)         (bottleneck)
    │            │              │               │
    │ ──data──> │ ──data──>   │ ──data──>    │ ██████████
    │            │              │               │ ██FULL██
    │            │              │ ◄──WAIT──     │ ██████████
    │            │ ◄──WAIT──   │               │
    │ ◄──WAIT── │              │               │
    │            │              │               │
  Source slows  Buffers fill   Buffers fill    Sink cannot
  its read     upstream        upstream        keep up
  from Kafka

  Result: Kafka consumer lag increases, but NO data is lost.
  Backpressure is a healthy safety mechanism.
```

### 9.2 Flink's Credit-Based Flow Control

Flink uses a **credit-based flow control** mechanism between operators:

1. The downstream operator announces how many **credits** (buffer slots) it has available.
2. The upstream operator sends data only when it has credits.
3. When the downstream operator processes a buffer and frees a slot, it sends a credit back upstream.

This is more efficient than TCP-level flow control because it operates at the Flink buffer level (32 KB default) rather than the TCP window level, and credits are piggybacked on data messages to reduce overhead.

### 9.3 Detecting Backpressure

**Metrics**:
- `outPoolUsage` (output buffer pool usage): High on the operator BEFORE the bottleneck. If operator A's outPoolUsage is high and operator B's is low, the bottleneck is between A and B (network or B's processing).
- `inPoolUsage` (input buffer pool usage): High on the bottleneck operator itself.
- `busyTimeMsPerSecond`: Time the operator is busy processing (in ms per second). If 1000, the operator is fully saturated.

**Flink Web UI**: The backpressure tab shows per-operator backpressure status (OK, LOW, HIGH) based on thread stack sampling.

### 9.4 Common Causes and Solutions

| Cause | Diagnosis | Solution |
|:---|:---|:---|
| Slow external sink (Redis, DB) | Sink operator has high busyTime | Async I/O (`AsyncFunction`), batch writes |
| Unbalanced parallelism | One subtask has higher load | Increase parallelism of bottleneck operator |
| Data skew | One key receives disproportionate traffic | Pre-aggregation, key salting, two-phase aggregation |
| Large state access | RocksDB reads slow under heavy load | Tune RocksDB (block_cache, bloom filters), SSD disks |
| Serialization overhead | CPU-bound on ser/de | Use Flink's native serializers, avoid Kryo |
| Window materializing | ProcessWindowFunction loads all elements | Use ReduceFunction/AggregateFunction for incremental |

**Async I/O pattern** for sink writes:

```java
// Instead of synchronous Redis write in a MapFunction:
AsyncDataStream.unorderedWait(
    featureStream,
    new AsyncRedisWriter(redisClient),
    5000,    // timeout ms
    TimeUnit.MILLISECONDS,
    100      // max concurrent requests
);
```

This lets the operator have up to 100 concurrent Redis writes in flight, dramatically improving throughput when the bottleneck is network latency to Redis.

---

## 10. Failure Recovery and Restarts

### 10.1 Checkpoint-Based Recovery

When a failure occurs (TaskManager crash, operator exception, network partition), Flink restores the pipeline to the last completed checkpoint:

1. **Cancel all running tasks** in the affected pipeline region.
2. **Roll back state** of all operators to their checkpointed state.
3. **Reset source offsets** to the checkpointed positions (e.g., Kafka offsets).
4. **Restart operators** from the checkpointed state and resume processing.

Events between the checkpoint and the failure are **replayed** from the source. Because the state was also rolled back, processing these events again produces the same output (deterministic processing) or is deduplicated by the exactly-once sink mechanism (2PC abort + recommit, or idempotent writes).

```
CHECKPOINT-BASED RECOVERY:

  Time ─────────────────────────────────────────────>
  
  ckpt 5          ckpt 6              CRASH!
  complete        complete              │
     │               │                  │
     ▼               ▼                  ▼
  ┌──────────────┬──────────────────┬───┤
  │  processed   │    processed     │ X │  <-- events after ckpt 6
  │  (in ckpt 5) │    (in ckpt 6)  │   │      are lost from state
  └──────────────┴──────────────────┴───┘
                                        │
                                    RECOVERY:
                                        │
  ┌──────────────┬──────────────────┐   │
  │  state from  │  REPLAY from     │   │
  │  ckpt 6      │  Kafka offsets   │   │
  │  restored    │  in ckpt 6       │   │
  └──────────────┴──────────────────┘

  Events between ckpt 6 and crash are re-read from Kafka
  and reprocessed with the ckpt 6 state as starting point.
```

### 10.2 Restart Strategies

| Strategy | Behavior | Configuration | Use Case |
|:---|:---|:---|:---|
| **Fixed-delay** | Restart after fixed delay, up to N attempts | delay=10s, attempts=3 | Simple jobs |
| **Failure-rate** | Allow N failures within a time interval | failures=5, interval=10min, delay=10s | Production |
| **Exponential-backoff** | Increasing delay between restarts | initial=1s, max=60s, multiplier=2, reset=1h | Transient failures |
| **No restart** | Job fails permanently on first error | N/A | Development/testing |

**Production recommendation**: Use failure-rate restart with generous parameters:

```yaml
restart-strategy: failure-rate
restart-strategy.failure-rate.max-failures-per-interval: 10
restart-strategy.failure-rate.failure-rate-interval: 600s   # 10 min window
restart-strategy.failure-rate.delay: 15s
```

This tolerates up to 10 failures in any 10-minute window, with 15 seconds between restarts. If failures exceed the rate, the job is stopped and alerts fire.

### 10.3 Regional Restart (Flink 1.9+)

In a complex pipeline with multiple independent branches, a failure in one branch does not need to restart the entire job. Regional restart identifies the **failover region** (the set of operators connected by pipelined data exchanges) and restarts only that region.

```
REGIONAL RESTART:

  Source A → Map → KeyBy → Agg → Sink A     (Region 1)
                     │
                     └───> Filter → Sink B   (Region 2)

  If Sink B fails, only Region 2 is restarted.
  Region 1 continues processing uninterrupted.
  State for Region 2 is restored from checkpoint.
```

This significantly reduces the blast radius of failures and recovery time for large, multi-branch pipelines.

### 10.4 Recovery Time

Recovery time = time_to_restore_state + time_to_replay_lag

- **State restoration**: Download checkpoint from S3 + rebuild state (RocksDB SST file download). For a 100 GB state on S3 with 500 MB/s bandwidth: ~200 seconds. With incremental checkpoints, typically the last few SST files: ~10-30 seconds.
- **Replay lag**: Reprocess events from the checkpoint to the current position. If checkpoint interval is 60 seconds and throughput is 100K events/sec: replay 6M events. At 50K events/sec processing speed: ~120 seconds.

**Total**: Typical production recovery in 30 seconds to 5 minutes, depending on state size and lag.

---

## 11. Scaling and Resource Management

### 11.1 Dynamic Scaling

Changing a Flink job's parallelism requires a stop-and-restart cycle:

1. Trigger a savepoint: `flink savepoint <job-id>`
2. Cancel the job
3. Restart with new parallelism: `flink run -s <savepoint> -p <new-parallelism> job.jar`

This is operationally expensive (minutes of downtime) but necessary because keyed state must be redistributed across the new number of subtasks.

### 11.2 Reactive Scaling (Flink 1.13+)

In reactive mode, Flink automatically adjusts the job's parallelism to match the number of available TaskManager slots. When new TMs are added (e.g., Kubernetes HPA scales up the TM deployment), Flink takes a savepoint, restarts with the new parallelism, and resumes.

This enables elastic scaling: scale up during traffic spikes, scale down during quiet periods. However, each rescale involves a savepoint + restart, so it is not instant (typically 30-60 seconds of processing pause).

### 11.3 Key Group Assignment

Flink uses **key groups** to enable state redistribution when parallelism changes.

```
KEY GROUP REDISTRIBUTION:

  max_parallelism = 128 (fixed at job creation)
  Key groups: [0, 1, 2, ..., 127]

  Parallelism = 4:
    Subtask 0: key groups [0-31]
    Subtask 1: key groups [32-63]
    Subtask 2: key groups [64-95]
    Subtask 3: key groups [96-127]

  Parallelism changes to 8:
    Subtask 0: key groups [0-15]
    Subtask 1: key groups [16-31]
    Subtask 2: key groups [32-47]
    Subtask 3: key groups [48-63]
    Subtask 4: key groups [64-79]
    Subtask 5: key groups [80-95]
    Subtask 6: key groups [96-111]
    Subtask 7: key groups [112-127]

  Each key maps to a key group via: keyGroup = hash(key) % max_parallelism
  Key groups are the unit of state redistribution, not individual keys.
```

**Important**: `max_parallelism` is set at job creation and cannot be changed without discarding state. Set it to a reasonable upper bound (default 128, increase for large jobs). It must be a multiple of the expected parallelism values.

### 11.4 Resource Configuration

Flink's TaskManager memory model:

```
TASKMANAGER MEMORY MODEL:

  Total Process Memory (e.g., 8 GB)
  ├── Flink Memory (7.2 GB)
  │   ├── Framework Heap (128 MB)         -- Flink runtime overhead
  │   ├── Task Heap (3 GB)                -- user code objects
  │   ├── Managed Memory (2.8 GB)         -- RocksDB, sorting, caching
  │   │   └── RocksDB uses this for:
  │   │       - Block cache (read cache)
  │   │       - Write buffers
  │   │       - Index/filter blocks
  │   ├── Network Memory (1 GB)           -- shuffle buffers
  │   │   └── Min 64MB, Max 1GB, fraction: 0.1
  │   └── Framework Off-Heap (128 MB)     -- Flink internal off-heap
  └── JVM Overhead (800 MB)               -- metaspace, stack, GC overhead
      └── Fraction: 0.1 of total
```

**Tuning for ML feature pipelines** (large state, moderate computation):
- Increase managed memory for RocksDB (40-50% of Flink memory).
- Monitor RocksDB block cache hit ratio. If < 90%, increase managed memory.
- Ensure network buffers are sufficient for high-parallelism shuffles.

---

## 12. Alternatives and Comparison

### 12.1 Apache Spark Structured Streaming

**Architecture**: Micro-batch processing using Spark's existing batch engine. Each micro-batch is a small Spark job. Continuous processing mode (experimental) attempts true record-at-a-time but lacks production readiness.

**Strengths**: Unified batch and streaming API (same DataFrame/SQL), excellent for teams already using Spark, large ecosystem (MLlib, GraphX), simpler operational model.

**Weaknesses**: Minimum latency ~100ms (micro-batch boundary), no true event-time session windows (approximated), state management less mature than Flink's, no native CEP.

**When to choose**: Your team already uses Spark for batch, latency requirements are > 1 second, and you value API unification over streaming features.

### 12.2 Apache Kafka Streams

**Architecture**: A library (not a cluster). Runs embedded within your application's JVM. Consumes from and produces to Kafka only. State is stored locally in RocksDB and backed up to Kafka changelog topics.

**Strengths**: No cluster to operate (deploy as regular app instances), exactly-once via Kafka transactions, tight Kafka integration, simple deployment (scale by adding instances).

**Weaknesses**: Limited to Kafka as source/sink, single JVM processing (no distributed shuffle), state size limited by local disk, harder to manage for complex topologies.

**When to choose**: Simple transformations close to Kafka, microservice teams that do not want to operate a Flink cluster, state fits on a single node per partition.

### 12.3 Apache Beam

**Architecture**: A portable API that compiles to multiple runners (Flink, Spark, Google Cloud Dataflow). Write once, run anywhere.

**Strengths**: Portability, clean windowing and trigger abstractions, backed by Google's Dataflow model, avoids vendor lock-in.

**Weaknesses**: Abstraction adds complexity and overhead, debugging is harder (which layer is the problem?), runner-specific features cannot be used, lag behind native runner features.

**When to choose**: Multi-cloud strategy, or targeting Google Cloud Dataflow specifically.

### 12.4 Google Cloud Dataflow

**Architecture**: Managed Beam runner. Serverless, auto-scaling, no cluster management.

**Strengths**: Zero operational overhead, automatic scaling, built-in monitoring, liquid sharding for hot keys.

**Weaknesses**: GCP only, expensive at high scale, less control over tuning, closed-source runner.

### 12.5 Comparison Table

| Feature | Flink | Spark SS | Kafka Streams | Beam/Dataflow |
|:---|:---|:---|:---|:---|
| **Latency** | ~ms | ~100ms+ | ~ms | ~ms (Dataflow) |
| **Exactly-once** | Yes (2PC) | Yes (micro-batch) | Yes (Kafka txn) | Yes (runner) |
| **Max state size** | TBs (RocksDB) | 10s GB (heap) | 100s GB (local) | TBs (managed) |
| **Session windows** | Native | Approximate | Via DSL | Native |
| **Operational complexity** | High | Medium | Low | None (managed) |
| **Batch+Stream unified** | Yes (1.12+) | Excellent | No | Yes |
| **CEP** | Native library | No | No | No |
| **Source/Sink flexibility** | Any | Any | Kafka only | Any |
| **Auto-scaling** | Reactive (1.13+) | Yes (cloud) | Manual | Native |

---

## 13. Capacity Planning for Streaming Pipelines

### 13.1 Throughput Estimation

Per-subtask throughput depends heavily on the operation:

| Operation | Throughput per Subtask | Notes |
|:---|:---|:---|
| Simple map/filter | 100K-500K events/sec | CPU-bound, no state |
| Keyed aggregation (incremental) | 10K-50K events/sec | State reads/writes per event |
| Windowed aggregation | 10K-50K events/sec | Similar to keyed, plus timer overhead |
| Async I/O (Redis write) | 5K-20K events/sec | Network-bound, depends on batch size |
| CEP pattern matching | 5K-20K events/sec | Pattern state overhead |
| Session window (merge) | 5K-15K events/sec | Merge is expensive |

**Example**: A feature pipeline processing 1M events/sec with keyed windowed aggregation at 25K events/sec per subtask needs 1,000,000 / 25,000 = 40 subtasks (parallelism 40). With 4 slots per TaskManager, that is 10 TaskManagers.

### 13.2 State Size Estimation

Formula:

```
total_state = num_keys * state_per_key * concurrent_windows + overhead

Example: User click feature store
  num_keys = 500M users (active in the last 30 days)
  state_per_key = 200 bytes (3 features: count, sum, list of 10 categories)
  concurrent_windows = 4 (tumbling 1h, sliding 24h by 1h: 24 panes, etc.)
  
  Naive: 500M * 200B * 4 = 400 GB
  
  With incremental aggregation: 500M * 64B * 4 = 128 GB
  (Only store accumulator, not raw events)
  
  With state TTL (7-day active users only): 50M * 64B * 4 = 12.8 GB
  (90% of users inactive in any given week)
```

### 13.3 Checkpoint Sizing and Interval

Checkpoint interval affects both recovery time and overhead:

```
CHECKPOINT SIZING:

  State size: 128 GB (RocksDB, incremental)
  Churn rate: 2% per checkpoint interval
  Incremental checkpoint size: 128 GB * 0.02 = 2.56 GB
  S3 upload bandwidth: 500 MB/s (parallel, multiple TMs)
  Checkpoint duration: 2.56 GB / 500 MB/s = ~5 seconds
  
  Checkpoint interval: 60 seconds
  Overhead: 5s / 60s = 8.3% of time spent checkpointing
  
  Recovery time:
    State restore: ~5s (download last incremental)
    Replay lag: 60s * 1M events/sec = 60M events
    At 500K events/sec replay speed: 120 seconds
    Total: ~125 seconds
  
  If checkpoint interval = 30 seconds:
    Overhead: 5s / 30s = 16.7% (higher)
    Recovery replay: 30M events = 60 seconds
    Total recovery: ~65 seconds (faster recovery, higher overhead)
```

**Rule of thumb**: Set checkpoint interval to 1-5 minutes for production. Shorter intervals mean faster recovery but higher checkpoint overhead. Longer intervals mean less overhead but longer recovery.

### 13.4 Kafka Lag Monitoring

Consumer lag = latest_offset - committed_offset (or checkpointed offset).

```
KAFKA LAG MONITORING:

  Healthy: lag < 1 * checkpoint_interval * throughput
    Example: lag < 60s * 100K/sec = 6M records
    
  Warning: lag > 5 * checkpoint_interval * throughput
    Pipeline is falling behind. Scale up or investigate bottleneck.
    
  Critical: lag growing linearly
    Pipeline throughput < input rate. Must scale up or reduce input.
    
  Monitoring:
    - Flink metrics: KafkaConsumer.records-lag-max
    - External: Burrow, LinkedIn's Kafka consumer lag checker
    - Cloud: AWS CloudWatch for MSK, Confluent Cloud metrics
```

### 13.5 Example Capacity Plan: Feature Store Pipeline

**Requirements**: Compute 5 features per user from a click stream of 500K events/sec. Features: click_count_1h, click_count_24h, avg_session_duration, top_5_categories_7d, purchase_conversion_rate_30d.

```
CAPACITY PLAN:

  Input: 500K events/sec, avg event size 500 bytes = 250 MB/s
  
  Kafka Source:
    Partitions: 48 (allows parallelism up to 48)
    Source parallelism: 48
    Per-subtask: 500K / 48 = ~10.4K events/sec (comfortable)
  
  Feature Computation (keyed, windowed):
    Parallelism: 48 (match source)
    Per-subtask: 10.4K events/sec (within 10-50K range for windowed agg)
    
  State:
    Active users: ~50M (7-day active)
    Per-user state: 5 features * 64 bytes accumulator = 320 bytes
    Windows: tumbling 1h (1) + sliding 24h/1h (24) + session (avg 2 active)
             + sliding 7d/1d (7) + sliding 30d/1d (30) = 64 concurrent accumulators
    Total: 50M * 320B * 64 / 5 features ... simplified:
           50M users * ~4 KB per user (all features, all windows) = 200 GB
    Backend: RocksDB with incremental checkpoints
    
  TaskManagers:
    Slots per TM: 4
    TMs needed: 48 / 4 = 12 TaskManagers
    Memory per TM: 16 GB (4 GB task heap + 6 GB managed/RocksDB + 2 GB network + 4 GB overhead)
    Disk per TM: 500 GB SSD (RocksDB state + spill)
    
  Checkpoints:
    Interval: 60 seconds
    Incremental size: ~2% * 200 GB = 4 GB
    Duration: 4 GB / (12 TMs * 100 MB/s each) = ~3.3 seconds
    Storage: S3, retain last 3 checkpoints = 12 GB
    
  Sinks:
    Redis: 48 parallel writers, async I/O, batch SET (pipeline 100 commands)
    Kafka: 48 parallel producers, exactly-once with transactions
    
  Total cluster:
    12 TaskManagers * 16 GB = 192 GB RAM
    12 TaskManagers * 500 GB SSD = 6 TB disk
    1 JobManager * 8 GB = 8 GB RAM
    Plus: Kafka cluster, Redis cluster, S3 for checkpoints
```

---

## 14. Failure Walkthroughs

### 14.1 TaskManager Crash Mid-Checkpoint

**Scenario**: TaskManager 3 crashes (OOM, hardware failure) while checkpoint 42 is in progress.

**What happens**:
1. JobManager detects the TM heartbeat timeout (default 50 seconds, configurable).
2. Checkpoint 42 is aborted (incomplete -- TM 3 never acknowledged).
3. Regional restart initiates for the operators that ran on TM 3.
4. ResourceManager requests a new TM (or existing TM has spare slots).
5. Operators are redeployed and state is restored from checkpoint 41 (last completed).
6. Kafka source seeks to offsets from checkpoint 41.
7. Processing resumes, events between checkpoint 41 and the crash are replayed.

**Data loss**: None. Checkpoint 41 is consistent. Replayed events are deduplicated by exactly-once mechanisms.

**Duration**: TM heartbeat timeout (50s) + new TM provisioning (30-120s on K8s) + state restore (5-30s) + replay (seconds to minutes) = typically 2-5 minutes.

### 14.2 Kafka Broker Outage

**Scenario**: One of three Kafka brokers goes down. Some partitions lose their leader.

**What happens**:
1. Kafka elects new leaders for affected partitions (seconds, depends on `unclean.leader.election.enable`).
2. Flink's Kafka source experiences temporary read failures on those partitions.
3. Source retries with backoff. During retry, no events flow from those partitions.
4. Backpressure propagates from the source: downstream operators slow down.
5. Watermark stalls on affected partitions (if `withIdleness()` is set, watermark advances from other partitions after the idle timeout).
6. Once Kafka elects new leaders, Flink resumes reading from the last committed offset.

**Data loss**: None. Kafka's replication ensures no data loss (assuming RF >= 2 and min.insync.replicas >= 2). Flink replays from the last good offset.

**Duration**: Kafka leader election (1-30 seconds) + Flink reconnect (seconds) = typically < 1 minute.

### 14.3 State Backend (S3) Unavailable

**Scenario**: S3 experiences a regional outage. Flink cannot write checkpoints.

**What happens**:
1. Checkpoint attempts fail (S3 upload timeout).
2. The Flink job continues processing normally -- checkpoints are async and do not block processing.
3. No new checkpoints complete. The last completed checkpoint ages.
4. If the job crashes during the S3 outage, recovery rolls back to the last completed checkpoint (potentially far behind). The replay lag could be very large.
5. When S3 recovers, the next checkpoint completes successfully.

**Risk**: This is the most dangerous failure mode. A long S3 outage followed by a job crash means replaying hours of data, which could take hours itself and produce a feature staleness incident.

**Mitigation**: Monitor checkpoint age. Alert if the latest checkpoint is older than 3x the checkpoint interval. Consider dual checkpoint storage (S3 + HDFS) for critical pipelines.

### 14.4 Data Skew Causing OOM

**Scenario**: A popular item (e.g., a viral product) generates 10x the events of any other key. The subtask handling that key's key group runs out of memory.

**What happens**:
1. One subtask processes 10x the events of its peers.
2. Its state grows faster (more events in windows), its processing lags, it consumes more heap.
3. The TM hits an OOM error and crashes.
4. Flink restarts the subtask (or the entire TM). The same skew persists.
5. The subtask OOMs again. The restart strategy eventually exhausts its budget and the job fails.

**Solutions**:

1. **Pre-aggregation (two-phase aggregation)**: Add a random salt to the key, aggregate per salted key, then aggregate across salts.

```
WITHOUT salting:                WITH salting (salt = 0-9):
  item_123 → subtask 7           item_123_0 → subtask 2  ─┐
  (ALL events go to 7)           item_123_1 → subtask 5   │  2nd keyBy
                                  item_123_2 → subtask 9   ├─> (item_123)
                                  ...                      │    aggregate
                                  item_123_9 → subtask 1  ─┘    partial results
```

2. **Split hot keys**: Route events for the hot key to a separate pipeline with higher parallelism.

3. **Approximate aggregation**: Use HyperLogLog for count-distinct, Count-Min Sketch for frequency. These have fixed-size state regardless of event volume.

---

## 15. Interview Patterns

### 15.1 "Design Real-Time Feature Computation"

This is the most common stream processing question in ML system design interviews. The expected answer:

**Architecture**: Kafka (event source) -> Flink (computation) -> dual-write to Redis (online store) + Kafka/S3 (offline store).

**Key points to hit**:
1. Event-time processing with watermarks for correct feature values.
2. Incremental aggregation (not full-window materialization) for memory efficiency.
3. Exactly-once: Chandy-Lamport checkpoints internally, idempotent writes to Redis, Kafka transactions to the offline topic.
4. State backend: RocksDB with incremental checkpoints for large state.
5. Latency budget: event -> feature available in Redis in < 5 seconds.
6. Monitoring: Kafka consumer lag, checkpoint duration, feature freshness (time since last update).

```
INTERVIEW ANSWER STRUCTURE:

  "For real-time feature computation, I would use Kafka as the event
   backbone and Apache Flink for stateful stream processing.

   Events flow from producers to a Kafka topic, partitioned by user_id.
   Flink reads from Kafka using event-time semantics with bounded
   out-of-orderness watermarks (10-second delay for server-side events).

   The Flink job keys by user_id and applies tumbling or sliding windows
   depending on the feature. For click_count_last_1h, a tumbling 1-hour
   window with an incremental ReduceFunction. For avg_purchase_7d, a
   sliding 7-day window with daily slide using an AggregateFunction
   that maintains sum and count.

   Output is dual-written: to Redis for online serving (idempotent
   SET with a checkpoint-epoch version) and to a Kafka topic for
   offline consumption by the training pipeline.

   State is managed with RocksDB and incremental checkpoints to S3
   every 60 seconds. Recovery replays from the last checkpoint's
   Kafka offsets.

   For a system with 500K events/sec and 50M active users, I'd
   estimate 200 GB of state, 12 TaskManagers with 16 GB each,
   and checkpoint sizes around 4 GB (incremental, 2% churn)."
```

### 15.2 "How Do You Handle Late-Arriving Data?"

**Expected answer**: A three-layer defense:

1. **Watermark delay**: Set the watermark to trail max event time by the expected out-of-orderness (e.g., 10 seconds for server events, 5 minutes for mobile). This handles the majority of late events transparently.

2. **Allowed lateness**: After the window fires, keep it open for an additional period (e.g., 1 minute). Late events within this period trigger a window re-fire with updated results. The downstream system must handle updates (idempotent writes).

3. **Side outputs**: Events arriving after the allowed lateness period are routed to a side output for separate handling -- log them for monitoring, feed them into a reconciliation batch job, or drop them with a metric.

**Quantify**: "In our pipeline, 99.5% of events arrive within the watermark delay. The allowed lateness catches another 0.4%. The remaining 0.1% goes to side outputs and is reconciled in a daily batch job. This gives us sub-minute latency for 99.9% of events while still maintaining data completeness."

### 15.3 "How Do You Guarantee Exactly-Once?"

**Expected answer**: Exactly-once has three components that must all be in place:

1. **Replayable source**: Kafka stores events durably. On recovery, Flink seeks back to the checkpointed offset and replays. Events are re-read but state was also rolled back, so they are reprocessed from the same starting state.

2. **Internal exactly-once** (Chandy-Lamport): Flink injects checkpoint barriers into the stream. Each operator snapshots its state when it receives barriers from all inputs. On failure, all operators roll back to the last completed checkpoint. The combination of state rollback + source replay produces the same processing as if the failure never happened.

3. **Sink exactly-once**: Two options:
   - **Two-phase commit (2PC)**: The sink writes within a transaction. On checkpoint, the transaction is pre-committed. On checkpoint completion, it is committed. On failure, it is aborted. Used for Kafka sinks (Kafka transactions).
   - **Idempotent writes**: The sink writes with a deduplication key (e.g., user_id + checkpoint_epoch). Replayed writes overwrite with the same value or are rejected by a version check. Used for Redis, database sinks.

**Common mistake to avoid**: Saying "Flink provides exactly-once" without distinguishing internal vs end-to-end. The interviewer will probe: "What if the Redis write succeeds but Flink crashes before the checkpoint completes?" The answer: On recovery, Flink replays the event and writes to Redis again. If the write is idempotent (same key, same value), this is safe. If it is not (e.g., INCR), you get duplicates.

### 15.4 Common Interview Mistakes

| Mistake | Correction |
|:---|:---|
| "We use processing time for features" | Almost always wrong for ML. Event time prevents training-serving skew. |
| "Flink is exactly-once" (without qualification) | Specify: internal vs end-to-end, and which sink mechanism (2PC or idempotent). |
| "We use a ProcessWindowFunction" | For large windows, this loads all events into memory. Use AggregateFunction for incremental aggregation. |
| "Watermark delay of 0" | No tolerance for out-of-order events. Many events will be late. |
| "Watermark delay of 1 hour" | Unnecessary latency. Use minutes for mobile, seconds for server events. |
| Ignoring state size | Always estimate state: keys x state_per_key x windows. 500M users with large state needs RocksDB. |
| "Just increase parallelism" | Requires savepoint + restart. State redistribution takes time. Also increases checkpoint size. |
| Forgetting idle sources | One idle Kafka partition stalls all watermarks. Always configure withIdleness(). |

### 15.5 Quick Reference: Connecting to Other Chapters

| Design Problem | Stream Processing Role | Key Concepts from This Chapter |
|:---|:---|:---|
| Recommendation system (real-time) | Compute user features, feedback loop | Section 8.4 (feedback loop), 8.2 (session windows) |
| Feature store | Streaming feature computation, dual-write | Section 8.1 (sliding agg), 8.3 (dual-write), 6.3 (EOS) |
| AI search engine | Real-time index updates | Kafka -> Flink -> index writer, Section 9 (backpressure) |
| Fraud detection | CEP pattern matching | Section 8.5 (CEP), 4 (watermarks for event-time) |
| Ad click attribution | Session windows, late data | Section 3.3 (sessions), 3.6 (late data) |
| Real-time dashboard | Tumbling window aggregations | Section 3.1 (tumbling), 13 (capacity planning) |
| ML model serving monitoring | Streaming metrics computation | Section 8.1 (sliding windows for latency percentiles) |

---

## Summary of Key Numbers

| Parameter | Typical Production Value |
|:---|:---|
| Checkpoint interval | 60-300 seconds |
| Watermark delay (server events) | 5-30 seconds |
| Watermark delay (mobile events) | 1-10 minutes |
| Idle source timeout | 1-5 minutes |
| Allowed lateness | 1-10 minutes |
| Throughput per subtask (windowed agg) | 10-50K events/sec |
| RocksDB block cache hit ratio target | > 90% |
| Kafka transaction timeout | 15 minutes (must exceed ckpt interval + duration) |
| Recovery time (typical) | 30 seconds - 5 minutes |
| Feature freshness target (event to store) | < 5 seconds (server events) |
| Max state before RocksDB required | ~5-10 GB per TaskManager |
| Checkpoint storage retained | 2-3 checkpoints |
| TaskManager memory (production) | 8-32 GB |

---

*Next: Chapter 23 covers batch processing with Spark and the Lambda/Kappa architecture decision, building on the streaming foundation from this chapter. Chapter 24 addresses distributed storage engines and their role as sinks for the streaming pipelines described here.*
