# 08 — Trace Storage Internals

> A trace is a DAG of spans, joined by `trace_id`. The store has to ingest billions of spans per day, lose almost none, find any one trace by ID in tens of milliseconds, support attribute-shaped queries that nobody anticipated, and stay cheap. This chapter is about how the four production architectures — **trace-id-keyed object stores (Tempo, Jaeger Badger)**, **attribute-indexed span stores (Jaeger ES, Honeycomb, Lightstep)**, **columnar SQL on spans (ClickHouse, BigQuery)**, and the **vendor APMs (Datadog, New Relic, Dynatrace)** — actually implement that. By the end you should know which one to pick from your fleet's RPS, query patterns, cardinality budget, and tolerance for ops.

This chapter assumes you've read [03 — Instrumentation](./03-instrumentation.md) (where spans come from), [04 — Collection & Edge](./04-collection-and-edge.md) (the OTel Collector and tail sampling), [05 — Transport & Buffering](./05-transport-and-buffering.md) (Kafka in front of long-tail backends), and [06 — Metrics Storage](./06-metrics-storage.md) and [07 — Logs Storage](./07-logs-storage.md) (their architectural lessons recur). Cardinality framing is in [ROADMAP §5.1](./ROADMAP.md#51-cardinality--how-many-unique-time-series-exist); the query layer is [chapter 10](./10-query-layer.md); cost discipline is [chapter 18](./18-cardinality-and-cost.md).

---

## Table of Contents

1. [What a Trace Actually Is](#1-what-a-trace-actually-is)
2. [The Fundamental Choice: Index by trace_id or by Attributes?](#2-the-fundamental-choice-index-by-trace_id-or-by-attributes)
3. [Tempo Internals](#3-tempo-internals)
4. [Jaeger Internals](#4-jaeger-internals)
5. [Honeycomb & Lightstep Architecture](#5-honeycomb--lightstep-architecture)
6. [ClickHouse-on-Traces Internals](#6-clickhouse-on-traces-internals)
7. [Service Graph Derivation](#7-service-graph-derivation)
8. [Sampling Deep Dive](#8-sampling-deep-dive)
9. [Cardinality in Tracing](#9-cardinality-in-tracing)
10. [Compression and Storage Layout](#10-compression-and-storage-layout)
11. [Query Patterns and Engines](#11-query-patterns-and-engines)
12. [Live Tail, Exemplars, Correlation](#12-live-tail-exemplars-correlation)
13. [The Long-Tail Problem and Buffer Sizing](#13-the-long-tail-problem-and-buffer-sizing)
14. [Operational Pitfalls](#14-operational-pitfalls)
15. [Decision Tree](#15-decision-tree)
16. [End-to-End: Life of One Trace](#16-end-to-end-life-of-one-trace)

---

## 1. What a Trace Actually Is

A **trace** is the directed acyclic graph of operations that one logical request — or one logical job — performed across all the processes it touched. A **span** is one node of that graph: a unit of work bounded by a start and end time, named, attributed, and linked to a parent.

```
trace                                      ← one trace_id (16 bytes)
├── span A (root)        kind=SERVER
│   service=gateway, op="POST /checkout"
│   start=T0, end=T0+1215ms
│   ├── span B           kind=CLIENT, parent=A
│   │   service=gateway, op="auth.Validate"
│   │   start=T0+2,  end=T0+8
│   │   └── span C       kind=SERVER, parent=B
│   │       service=auth, op="POST /validate"
│   │       start=T0+3, end=T0+7
│   ├── span D           kind=CLIENT, parent=A
│   │   service=gateway, op="cart.Get"
│   │   ...
│   └── span E           kind=PRODUCER, parent=A
│       service=gateway, op="kafka.publish order.created"
│       link: trace_id=X, span_id=Y           ← async link
└──   (consumer in another service starts a new span with parent_span_id=E)
```

### 1.1 The W3C trace-context header

The two header fields propagated at every process boundary:

```
traceparent: 00-{trace_id_hex}-{parent_span_id_hex}-{flags}
              ↑version  ↑16 bytes ↑8 bytes        ↑01 = sampled

tracestate:  vendor1=value1,vendor2=value2,    ← per-vendor extensions
```

Both are passed via HTTP headers, gRPC metadata, Kafka headers, AWS Lambda environment, queue messages, etc. **A single missing propagation breaks the trace.** This is the #1 cause of "tracing doesn't work" in real fleets.

### 1.2 Spans, kinds, links, baggage

```
SpanKind        meaning                                 implication
──────────      ──────────────────────────────────      ──────────────
SERVER          this process received a request         root or child of CLIENT
CLIENT          this process called another             paired with a SERVER on the next hop
INTERNAL        in-process work, no network             cheap; lots of these are normal
PRODUCER        sent a message asynchronously           may produce a CONSUMER span much later
CONSUMER        consumed a message                      links to PRODUCER span via span link
```

A **span link** records "this span was caused by that span, but not as a tree parent" — used for batch jobs (consumer of N messages each links its respective producer), for follows-from semantics, and for joins of traces. **Baggage** is propagated key/value context (e.g., `userId`, `tenantId`) added to one span and visible everywhere downstream. Baggage is *not* span attributes; it's a separate header (`baggage`) you must consciously set.

### 1.3 Resource vs span attributes

Same split as logs:

```
resource attributes      = "what process emitted this span"  (immutable per process)
   service.name = checkout
   service.version = 2.6.13
   k8s.pod.uid = 7c5b...
   host.name = ip-10-0-1-29
   cloud.region = eu-west-1

span attributes          = "what happened in this operation"  (per span)
   http.method = POST
   http.route = /checkout/{id}
   http.status_code = 500
   db.statement = SELECT ... WHERE user_id = $1
   peer.service = pricing
```

Stores deduplicate resource attributes ruthlessly — the same `service.name` is one column entry per batch, not per span. Span attributes scale per-span and are the cardinality monster.

### 1.4 What spans look like in OTLP

OTLP is the wire format. A `ResourceSpans` envelope groups spans by resource, then by `InstrumentationScope` (the SDK or auto-instrumentation that produced them):

```
ResourceSpans {
  resource: { attributes: {service.name="checkout", host.name="..."} }
  scope_spans: [
    {
      scope: { name="opentelemetry.contrib.instrumentations.requests", version="..." }
      spans: [
        Span {
          trace_id    : 16 bytes
          span_id     : 8 bytes
          parent_span_id: 8 bytes (zero = root)
          name        : "POST /checkout"
          kind        : SERVER
          start_time_unix_nano : ...
          end_time_unix_nano   : ...
          attributes  : {http.method=POST, http.status=500, ...}
          status      : { code=ERROR, message="..." }
          events      : [...]
          links       : [...]
        },
        ...
      ]
    }
  ]
}
```

Stores ingest these batches; some flatten to per-span rows (ClickHouse, ES), others keep batches roughly intact (Tempo blocks).

> **Mental model.** A trace store is two stores wearing the same logo: a **trace_id-keyed blob store** (cheap, dumb, always present) plus an **inverted/columnar index over span attributes** (rich, expensive, optional). Every architectural choice below is whether and how to build the second store.

---

## 2. The Fundamental Choice: Index by trace_id or by Attributes?

```
WHAT IS YOUR DOMINANT QUERY?

A) "I have a trace_id from a log/exemplar/page; show me the trace."
   → trace_id is the lookup. Object store + bloom filter is sufficient.

B) "Find me traces where http.status=500 and service=auth in the last hour."
   → attribute-shaped query. Need an inverted/columnar index.

C) "Compute p99 latency by route, grouped by region, over the last day."
   → aggregation. Need a columnar engine (or pre-aggregated metrics).
```

The architecture is determined by which mix of A/B/C you serve.

```
                  WRITE COST                        READ COST
                  ─────────                         ─────────
                                                      A    B    C
A) trace_id-keyed object store     (Tempo)         100  10   1
B) attribute-indexed span store    (Jaeger ES,
                                    Honeycomb,
                                    Lightstep)      30   90   30
C) columnar SQL on spans           (ClickHouse,
                                    BigQuery)       50   60   100
D) vendor APM (Datadog, NR, DT)                     varies; usually 50/70/70
```

### 2.1 Trace-id-keyed object stores (Tempo, Jaeger Badger)

**Promise**: store every span. Pay $/GB on object storage. Look up by trace_id in O(blocks-with-bloom-filter-hit).

**Constraint**: anything that isn't a trace_id lookup is hard. Tempo bolts on a metrics-generator and a "search" path that uses *derived* indexes (spanmetrics, attribute summaries) — but the architectural primitive is still "object store keyed by trace_id."

### 2.2 Attribute-indexed span stores (Jaeger ES, Honeycomb, Lightstep)

**Promise**: every span attribute is queryable. Find "all spans with http.status_code=500 and tenant_id=X" in milliseconds.

**Constraint**: every queryable attribute is an index column. Cost scales with cardinality of indexed dimensions. Honeycomb and Lightstep solve this with *columnar* engines tuned for high-cardinality grouping; Jaeger-on-ES is a more naive port of the log-archetype and falls over above ~50k events/sec without careful tuning.

### 2.3 Columnar SQL (ClickHouse, BigQuery, Snowflake)

**Promise**: arbitrary SQL aggregations across all spans. Same engine as your logs lakehouse.

**Constraint**: storage layout (sort key, partitioning) has to balance both "find by trace_id" and "aggregate by attribute" — tricky but solvable with secondary projections.

### 2.4 Vendor APMs

**Promise**: integrated metrics + logs + traces + RUM + APM with a UI that doesn't suck and a service-map you didn't have to build. Tail sampling baked in.

**Constraint**: pricing is per host or per indexed-span. At 500-service scale this is the bill that bankrupts a startup; at 5000-service scale it is genuinely competitive with a self-hosted stack once you count engineers.

---

## 3. Tempo Internals

Grafana Tempo is the canonical "trace_id-keyed object store" implementation: an open-source, S3/GCS-backed, Cortex-style distributed tracing backend. Single most-deployed self-hosted trace store today.

### 3.1 Component layout

```
                  ┌─────────────────┐
   spans ──→      │ DISTRIBUTOR     │ hash(trace_id) → ingester replica set
                  └────────┬────────┘
                           │ gRPC
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
      ┌────────┐      ┌────────┐      ┌────────┐
      │INGESTER│      │INGESTER│      │INGESTER│   3-replica quorum
      │  WAL   │      │  WAL   │      │  WAL   │
      │ traces │      │ traces │      │ traces │   (in-memory traces by trace_id)
      └───┬────┘      └───┬────┘      └───┬────┘
          │               │               │
          └───── flush ───┴───── flush ───┘
                          │
                 ┌────────▼────────┐
                 │  OBJECT STORAGE │   blocks/<tenant>/<block_id>/...
                 │   (S3/GCS)      │
                 └────────┬────────┘
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
     ┌────────┐      ┌────────┐      ┌─────────────────────┐
     │QUERIER │      │QUERY  │       │ METRICS-GENERATOR   │
     │  fetch │      │FRONTEND│      │ span_metrics +       │
     │ blocks │      │ split,│       │ service_graph metrics│
     └────────┘      │ merge │       │  → Prometheus        │
                     └───────┘       └─────────────────────┘
```

- **Distributors** are stateless. They hash `trace_id` and forward to a quorum of ingester replicas.
- **Ingesters** assemble spans by trace_id in memory (a "live trace" can collect spans for up to `max_idle_time` before being flushed). They write a WAL on local disk for crash recovery. When a trace becomes idle (no new spans for ~10s) or the block fills, the ingester flushes a **block** to object storage.
- **Object storage** is the sole source of truth.
- **Queriers** fetch blocks (and live in-memory traces from ingesters) on demand.
- **Query frontend** splits a query by time, routes shards to queriers, merges.
- **Metrics-generator** consumes the same span stream and emits *derived* RED metrics and service-graph metrics back into the Prometheus-compatible TSDB.

### 3.2 The block format (vParquet3)

Tempo evolved through formats: v2 → vParquet (1, 2, 3). vParquet3 is the current default.

```
block/                                    (per-tenant, per-period bucket)
├── meta.json                             (block ID, time range, total objects)
├── data.parquet                          (Parquet file holding spans)
├── bloom-0.bloom, bloom-1.bloom, ...     (sharded bloom filter on trace_id)
└── index.parquet                         (for range search by attribute)
```

The `data.parquet` file is column-oriented. Span batches are flattened into rows; columns include `TraceID`, `SpanID`, `ParentSpanID`, `Name`, `StartTimeUnixNano`, `EndTimeUnixNano`, `StatusCode`, plus a list of `Resource.attributes` and `Span.attributes`.

The bloom is *the* primary access path: "is trace_id X in this block?" Yes/no, with ~1% false positive at ~10 bits per trace_id. With 100M trace_ids per block at 1% FP, you need ~125 MB of bloom — Tempo shards the bloom across multiple files (`bloom-0`, `bloom-1`...) and only loads the needed shard.

### 3.3 Trace lookup by ID

```
GET /api/traces/{trace_id}

1. compute time range from trace_id (encoded in upper bits via "shard_query_by")
2. list blocks intersecting that range
3. for each candidate block:
     load bloom filter shard for trace_id
     if bloom hit → fetch the block's parquet file (or relevant row groups)
     scan column TraceID for matches → fetch full row data
4. assemble spans into a trace structure, return
```

A typical trace fetch reads bloom + ~1 row group per matching block. With sensible block sizes and S3 latencies, ~10–80 ms is the steady-state.

### 3.4 TraceQL search

TraceQL is Tempo's domain-specific query language for *attribute-shaped* search:

```
{ resource.service.name = "auth"
  && span.http.status_code = 500 }
| select(span.user_id, span.http.route)
```

Resolution:

1. The query frontend uses **`spanmetrics`** (recording rules over the metrics-generator's `traces_spanmetrics_*` series) to identify candidate `(service, route)` combinations matching the predicate.
2. For attributes not in spanmetrics, the frontend fetches the per-block `index.parquet` (a per-attribute mini-index) and prunes block lists.
3. The querier loads matching row groups from `data.parquet` and applies the predicate row-by-row.
4. Resulting trace_ids are resolved by ID (§3.3) for full-trace fetch.

It is *not* as fast as Honeycomb or Lightstep on attribute-shaped queries — Tempo's design choice is "make the trace_id path cheap and the attribute path acceptable."

### 3.5 Compactor

Background process that merges small blocks into larger ones, drops blocks past retention, and recomputes summary indices. Critical for cost: without compaction, a 30-day fleet has hundreds of thousands of blocks and slow query fan-out.

### 3.6 Metrics-generator: the killer feature

The metrics-generator is a stateless Tempo component that consumes the **span stream live** (not the stored blocks) and emits derived RED metrics + service-graph metrics into a Prometheus-compatible target.

```
For each completed span:
  service_label = resource.service.name
  span_metrics = {
    traces_spanmetrics_calls_total{service, span_name, span_kind, status_code}
    traces_spanmetrics_duration_seconds_bucket{service, span_name, ...}  # histogram
  }

For pairs (caller, callee) inferred via parent_span_id matching:
  service_graph_request_total{client, server}
  service_graph_request_failed_total{client, server}
  service_graph_request_server_seconds_bucket{client, server}
```

**Why this is critical**: span metrics are computed *before* tail sampling discards traces. If you tail-sample 1 % of traces, you keep 100 % of the *metrics*. The service graph is statistically valid even when only 1 % of traces are stored.

### 3.7 Tempo retention and tiering

```
storage:
  trace:
    backend: s3
    s3:
      bucket: tempo-blocks
    blocklist_poll: 5m
    pool:
      max_workers: 50

compactor:
  compaction:
    block_retention: 720h            # 30d delete
    compacted_block_retention: 1h
```

There is no warm/cold split — everything lives on object storage. Cost is essentially storage + S3 GETs; queriers cache hot blocks in memory and on local NVMe.

> **Mental model.** Tempo = "S3 keyed by trace_id, with a Prometheus-shaped derivative for everything that isn't a direct ID lookup." The architectural elegance is that you can store every span you collect (no indexed-span pricing) and pay S3-shaped bills.

---

## 4. Jaeger Internals

Jaeger predates Tempo and serves the same role with a different storage philosophy. It supports Cassandra, Elasticsearch / OpenSearch, Badger (embedded), Kafka (as a buffer), and (recently) ClickHouse and gRPC plugin storage.

### 4.1 Component layout

```
   sdk → agent → collector → [storage backend]      → query service → UI
                                                      (REST/gRPC)
                  │
                  └─ optionally → Kafka → ingester → backend
                                  (decouple, replay)
```

Jaeger v2 (2024+) re-architected on top of OpenTelemetry Collector — the "collector" is now an OTel pipeline. Older Jaeger v1 is still common in production.

### 4.2 Cassandra backend

The original Jaeger storage. Schema (simplified):

```
CREATE TABLE traces (
  trace_id        blob,
  span_id         bigint,
  span            blob,         -- protobuf-serialized span
  ts              timestamp,
  PRIMARY KEY ((trace_id), span_id)
);

CREATE TABLE service_names (
  service_name text PRIMARY KEY
);

CREATE TABLE operation_names_v2 (
  service_name text,
  span_kind    text,
  operation_name text,
  PRIMARY KEY ((service_name), span_kind, operation_name)
);

CREATE TABLE service_operation_index (
  service_name   text,
  span_name      text,
  bucket         int,        -- time bucket
  start_time     timestamp,
  trace_id       blob,
  PRIMARY KEY ((service_name, span_name, bucket), start_time, trace_id)
);

-- and similar tables: tag_index, duration_index
```

Cassandra is the canonical "trace_id-keyed key-value" store. The secondary indexes are wide rows that map `(service, op, time_bucket) → list of trace_ids`. Search by attribute (`tag_index`) is supported but expensive — Cassandra is bad at "find me trace_ids where some_tag = some_value".

### 4.3 Elasticsearch backend

Per-day indices: `jaeger-span-2026-05-03`, `jaeger-service-2026-05-03`. Each span is a document with all its attributes flattened. Standard Lucene archetype, with all the wins (rich attribute queries) and pains (mapping explosion, hot shard) from chapter 7.

```
{
  "traceID": "6f...",
  "spanID":  "a1...",
  "parentSpanID": "...",
  "operationName": "POST /checkout",
  "startTime": 1714720000000000,
  "duration": 1215000,
  "tags": [
    {"key": "http.method", "value": "POST"},
    {"key": "http.status_code", "value": 500},
    {"key": "user_id", "value": "42"}
  ],
  "process": {
    "serviceName": "checkout",
    "tags": [...]
  }
}
```

The flat tags array (vs nested objects) is to keep field count bounded; otherwise every distinct tag key becomes a field and mapping explodes.

### 4.4 Badger (embedded)

Single-node, RocksDB-style local store for development and small deployments. Not for production at scale. Useful as a reference for the trace_id-keyed approach without the operational overhead of Cassandra.

### 4.5 Kafka as a buffer

```
collector → Kafka topic (jaeger-spans) → ingester → backend
```

Decouples ingest from storage; if Cassandra/ES is down, spans buffer in Kafka and replay when storage recovers. Same pattern as the metrics/logs side and discussed at length in [chapter 5](./05-transport-and-buffering.md).

### 4.6 Adaptive sampling

Jaeger ships an **adaptive sampler** that observes per-operation rates and adjusts sampling probabilities to maintain a target rate. Implementation:

```
agent reports per-(service, operation) trace counts to collector
collector computes per-op probabilities to maintain target_qps
collector pushes probabilities back to agents (gRPC poll)
agents apply per-op probabilistic sampling at root span
```

This is the original "head sampling done right" feedback loop — predates OTel's Collector tail sampling and is still useful when tail sampling cost is too high.

---

## 5. Honeycomb & Lightstep Architecture

Honeycomb (now part of CrowdStrike) and Lightstep (acquired by ServiceNow) are the canonical **wide-events** vendors. The architectural philosophy is different enough to warrant its own section.

### 5.1 The wide-events philosophy

> A span is just an event with a parent. An event with no parent is just a structured log line. Treat them all the same.

Honeycomb does not store traces *as graphs*. It stores **events** — span-shaped records — in a high-cardinality columnar engine (Retriever) optimized for grouping and filtering on arbitrary attributes. The "trace view" is reconstructed at query time by joining events on `trace_id` and `parent_span_id`.

The implication: there is no "indexed attribute" vs "non-indexed attribute" — *every* attribute is queryable, with the same performance, all the way to a million distinct values. This is the closest production analog to "I just want SQL on every span."

### 5.2 Retriever: the columnar engine

Public details (Charity Majors and Liz Fong-Jones have written about this):

- Column-oriented, not row-oriented.
- Events are ingested into per-segment columnar files, sharded by tenant.
- Each column is dictionary-encoded for low-cardinality fields and run-length-encoded for sequential repeats.
- Query is parallelized across shards, with **adaptive sampling** of the result if the query is too expensive.
- A custom storage engine optimized for "GROUP BY any column" — the BubbleUp UI is the public face of this primitive.

Honeycomb's claim is "you can group-by `customer_id` (a million values) in seconds" — which on a Lucene-archetype store is a multi-minute query and on a Tempo is impossible.

### 5.3 BubbleUp & heatmaps

BubbleUp is the killer interaction: pick a region of a heatmap (e.g., the slow tail), and Honeycomb tells you which attribute values are over-represented in that region versus the baseline.

```
Heatmap: x=time, y=duration, color=density.
User selects: top tail (>p99 latency) over the last 30 min.

BubbleUp computes: for every attribute, frequency in the selection vs frequency
in the baseline. Sort by relative frequency.

Output:
  region=eu-west-1     selection 78%, baseline 12%      ← anomaly
  customer_id=123      selection 31%, baseline 0.04%    ← anomaly
  http.route=/legacy   selection 60%, baseline 18%
```

The infrastructure underneath is **a frequency-counting columnar scan over the selection and baseline**. This requires the ability to group by every attribute cheaply; it cannot be done on Lucene at any tolerable cost.

### 5.4 Lightstep: similar philosophy, different terminology

Lightstep coined "satellite" for the ingest tier (a high-volume rolling buffer that holds N seconds of all spans before tail-sampling decisions are made) and emphasizes correlation with metrics. Architecturally similar columnar storage. The acquisition by ServiceNow merged it into "Cloud Observability."

### 5.5 What this archetype is bad at

- **Cost at scale.** Wide-events vendors price by indexed event volume; "store every span" workloads get expensive.
- **Self-host.** Neither Honeycomb's nor Lightstep's storage engine is OSS; you can't replicate it locally.
- **Trace-as-graph operations.** Reconstructing a deep trace's parent chain requires N round trips; the architecture is event-oriented, not graph-oriented.

---

## 6. ClickHouse-on-Traces Internals

The rising default for self-hosted lakehouse traces. The OTel Collector ships an officially supported `clickhouseexporter` (in opentelemetry-collector-contrib) that produces a sane schema you can deploy in a day.

### 6.1 The schema

```sql
CREATE TABLE otel_traces (
  Timestamp           DateTime64(9, 'UTC') CODEC(Delta, ZSTD(1)),
  TraceId             String CODEC(ZSTD(1)),
  SpanId              String CODEC(ZSTD(1)),
  ParentSpanId        String CODEC(ZSTD(1)),
  TraceState          String CODEC(ZSTD(1)),
  SpanName            LowCardinality(String) CODEC(ZSTD(1)),
  SpanKind            LowCardinality(String) CODEC(ZSTD(1)),
  ServiceName         LowCardinality(String) CODEC(ZSTD(1)),
  ResourceAttributes  Map(LowCardinality(String), String) CODEC(ZSTD(1)),
  ScopeName           String CODEC(ZSTD(1)),
  ScopeVersion        String CODEC(ZSTD(1)),
  SpanAttributes      Map(LowCardinality(String), String) CODEC(ZSTD(1)),
  Duration            Int64 CODEC(ZSTD(1)),
  StatusCode          LowCardinality(String) CODEC(ZSTD(1)),
  StatusMessage       String CODEC(ZSTD(1)),
  Events Nested(
    Timestamp  DateTime64(9),
    Name       LowCardinality(String),
    Attributes Map(LowCardinality(String), String)
  ) CODEC(ZSTD(1)),
  Links Nested(
    TraceId    String,
    SpanId     String,
    TraceState String,
    Attributes Map(LowCardinality(String), String)
  ) CODEC(ZSTD(1)),
  INDEX idx_trace_id   TraceId            TYPE bloom_filter(0.001) GRANULARITY 1,
  INDEX idx_res_attr_keys mapKeys(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1,
  INDEX idx_span_attr_keys mapKeys(SpanAttributes)    TYPE bloom_filter(0.01) GRANULARITY 1,
  INDEX idx_duration   Duration           TYPE minmax GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(Timestamp)
ORDER BY (ServiceName, SpanName, toUnixTimestamp(Timestamp))
TTL toDateTime(Timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;
```

Two key choices:

- **`ORDER BY (ServiceName, SpanName, ts)`** balances "find traces by service" with time-range pruning. A different shop with `customer_id`-heavy queries would lead with `customer_id`.
- **`bloom_filter(0.001)` on `TraceId`** is the all-important index. Lookup by `TraceId = '6f...'` is ~milliseconds via this index.

### 6.2 Two query archetypes, one engine

```
-- Trace-by-id (the easy case)
SELECT * FROM otel_traces
WHERE TraceId = unhex('6f...')
  AND Timestamp > now() - INTERVAL 2 HOUR;

-- Attribute-shaped search
SELECT TraceId, ServiceName, SpanName, Duration
FROM otel_traces
WHERE ServiceName = 'auth'
  AND StatusCode = 'STATUS_CODE_ERROR'
  AND Timestamp > now() - INTERVAL 1 HOUR
ORDER BY Duration DESC
LIMIT 50;

-- Aggregation
SELECT
  ServiceName, SpanName,
  quantile(0.99)(Duration) AS p99_ns,
  count() AS calls,
  countIf(StatusCode='STATUS_CODE_ERROR') AS errors
FROM otel_traces
WHERE Timestamp > now() - INTERVAL 1 HOUR
GROUP BY ServiceName, SpanName
ORDER BY p99_ns DESC;
```

Same SQL engine, three workload classes. ClickHouse's columnar scan throughput (>1 GB/s/node) makes the third class — historically impossible in Tempo, painful in Jaeger, expensive in Honeycomb — fast and cheap.

### 6.3 Materialized views for service graph and span metrics

Replace Tempo's metrics-generator with a materialized view:

```
CREATE MATERIALIZED VIEW otel_traces_spanmetrics
ENGINE = SummingMergeTree
ORDER BY (ServiceName, SpanName, StatusCode, ts1m)
AS SELECT
  ServiceName,
  SpanName,
  StatusCode,
  toStartOfMinute(Timestamp) AS ts1m,
  count() AS calls,
  sumState(Duration) AS sum_dur_state,
  quantilesState(0.5, 0.95, 0.99)(Duration) AS dur_quantiles_state
FROM otel_traces
GROUP BY ServiceName, SpanName, StatusCode, ts1m;
```

This view is updated transactionally on insert into `otel_traces`. A dashboard on top runs in <10 ms regardless of underlying volume because the heavy lifting is per-insert, not per-query.

### 6.4 Projections for `TraceId` lookups

Default sort order leads with `ServiceName`; lookup by `TraceId` is bloom-assisted but still scans within hits. For high-frequency by-id workloads, add a projection:

```
ALTER TABLE otel_traces ADD PROJECTION p_by_trace_id (
  SELECT * ORDER BY TraceId
);
ALTER TABLE otel_traces MATERIALIZE PROJECTION p_by_trace_id;
```

The optimizer rewrites `WHERE TraceId = ?` to use the projection, dropping by-id queries to ~granule-scan speed. Cost: doubled storage for that data.

### 6.5 The lakehouse value

Same engine for logs, traces, and metric exemplars. Cross-signal SQL joins (e.g., "traces of failed checkouts whose log line says vendor.timeout") become trivial. This is why ClickHouse is winning the self-hosted lakehouse mindshare — even Datadog and Cloudflare have public talks about replacing internal ES with ClickHouse for tracing.

---

## 7. Service Graph Derivation

A service graph is the directed graph of "who-calls-whom" with RED metrics on the edges:

```
     gateway ──(2.4k calls/min, 0.2% errors, p99=23ms)──► auth
        │
        └────────(1.9k calls/min, 0.1% errors, p99=15ms)──► cart
                          │
                          ├──(1.2k, 0.3%, p99=120ms)──► pricing
                          │                                │
                          │                                └──(900, 4.2%, p99=950ms)──► vendor.foo
                          └──(1.7k, 0.0%, p99=8ms)──► redis
```

This is the most operationally useful tracing artifact for SREs. Every backend derives it differently.

### 7.1 Tempo: metrics-generator (real-time)

Stream-process span pairs by matching `parent_span_id`:

```
For each span s:
  if s.kind = SERVER and parent exists in window:
    parent = lookup(s.parent_span_id)
    edge = (parent.service, s.service)
    increment service_graph_request_total{client, server}
    if s.status = ERROR: increment ..._failed_total
    record duration in histogram
```

The matcher uses a bounded in-memory map keyed by `span_id`, with TTL eviction. Spans whose parent wasn't seen within the window are dropped (you lose stats for unusually deep traces). Tunable via `metrics_generator.processor.service_graphs.wait` and `max_items`.

### 7.2 Jaeger: Spark or Flink batch jobs

Original Jaeger had a Spark job (`spark-dependencies`) running periodically over the storage backend to compute service dependencies. Slow, batch-ish, but cost-effective at huge scale.

### 7.3 ClickHouse: materialized view

```
CREATE MATERIALIZED VIEW service_graph_edges
ENGINE = SummingMergeTree
ORDER BY (ts1m, client_service, server_service)
AS SELECT
  toStartOfMinute(child.Timestamp) AS ts1m,
  parent.ServiceName AS client_service,
  child.ServiceName AS server_service,
  count() AS calls,
  countIf(child.StatusCode='STATUS_CODE_ERROR') AS errors,
  quantilesState(0.5, 0.95, 0.99)(child.Duration) AS dur
FROM otel_traces AS child
JOIN otel_traces AS parent ON parent.SpanId = child.ParentSpanId
WHERE child.SpanKind = 'SPAN_KIND_SERVER'
GROUP BY ts1m, client_service, server_service;
```

A self-join on `ParentSpanId` is heavy on the streaming insert path; alternatives: do this offline via `INSERT INTO service_graph_edges SELECT ... FROM otel_traces WHERE ts > now() - 5m` on a 5-minute schedule.

### 7.4 Honeycomb: live-derived from events

Honeycomb maintains the service graph as a live view over the wide-events store; no separate derivation pipeline.

> **Mental model.** Service graphs are a *secondary product*, not a primary store. Treat them as a recording-rule output of the trace stream and surface them in metrics dashboards alongside latency.

---

## 8. Sampling Deep Dive

The single biggest cost lever in tracing. Three different problems live in the same word.

### 8.1 Head sampling

Decision made *at the SDK*, before context propagation, before anything is shipped.

```
trace_id (16 bytes) is generated at the root span.
sampler.shouldSample(trace_id, ...) returns SAMPLED or NOT_SAMPLED.
The sampling decision is encoded in trace_flags (bit 0 = sampled).

For deterministic head sampling at rate p:
  hash = trace_id[8:16]   # last 8 bytes treated as uniform random
  sample = (hash mod 1_000_000) < (p * 1_000_000)
```

Properties:

- **Cheap.** No buffer, no wait — instant decision.
- **Loses error visibility** if not selected. A 1 % rate misses 99 % of rare errors.
- **Must be consistent across services.** Encode the decision in `trace_flags` (the W3C bit) or in `tracestate`. If service A samples-in and service B independently samples-out, you get partial traces.

Common heuristic: 1 % head sampling for non-error traffic; 100 % for known-error endpoints (via `parentbased(traceidratio(1%))` plus per-route always-on rules).

### 8.2 Tail sampling

Decision made *at the collector*, after the full trace has assembled — or at least after enough of it has arrived.

```
Spans arrive at the OTel Collector tail-sampler:
  bucket spans into a per-trace_id buffer
  start a timer (e.g., 30s) when the first span arrives
  on timer expiry OR on receipt of root-span-with-end-time:
    apply policy:
      - any span has status=ERROR     → keep
      - root span duration > P99       → keep
      - any span attribute matches (e.g., debug=true) → keep
      - any span service.name = "rare" → keep
      - else                            → keep with probability p
  if keep: forward all buffered spans to the exporter
  if drop: discard all spans for this trace_id
```

Properties:

- **Captures rare events** that head sampling misses.
- **Costs RAM**: must buffer all spans of all in-flight traces for the wait window.
- **Adds latency**: spans don't ship until the tail decision is made.
- **Loses real-time export** of partial traces.

The collector must hold *all* spans of a trace until the decision is made — meaning all spans must hit the *same* collector instance. This requires either a singleton tail-sampler (bottleneck) or a **load balancer that routes spans by trace_id** (the recommended pattern). The OTel Collector's `loadbalancing exporter` does exactly this:

```
agents → loadbalancing-exporter → tailsampling-collector-1
                                  → tailsampling-collector-2  (sticky by trace_id)
                                  → tailsampling-collector-3
```

### 8.3 Adaptive sampling

Increase sampling for rare classes; decrease for hot paths. Jaeger's per-operation adaptive sampler (§4.6) is one form. OTel SDK's `JaegerRemoteSampler` is another.

### 8.4 Spanmetrics: the metric escape hatch

The trick that makes 1 % tail-sampling tolerable:

```
spans → metrics-generator (Tempo) OR span_metrics processor (OTel Collector)
        ↓
        emit per-span metrics BEFORE tail sampling:
          calls{service, span_name, status_code} += 1
          duration{...} histogram observe(latency)
        ↓
        tail sampler may drop the trace, but the metrics are kept.
```

This is what makes the "drop 99 % of traces, keep 100 % of metrics" strategy statistically sound. **Configure it. Always.**

> **Pitfall.** A spanmetrics pipeline that runs *after* tail sampling looks fine on day one and is silently wrong forever — your metrics are 1 % of reality. Always place spanmetrics in the pipeline before the tailsampling processor.

### 8.5 Buffer sizing for tail sampling

```
buffer_RAM = expected_traces_in_flight × avg_spans_per_trace × avg_span_size

Example:
  10 k traces/sec ingest
  30 s wait window      → 300 k traces in flight
  20 spans/trace        → 6 M spans buffered
  500 B/span (protobuf) → 3 GB RAM per collector

Add 50% headroom for bursts → 4.5 GB.
```

For a 4-replica tail-sampling collector pool, you need ~18 GB of RAM dedicated to the buffer, plus headroom for the rest of the pipeline. Spans-per-trace is the most variable factor; a crawler that hits 200 internal services produces 5,000-span traces and bloats the buffer.

### 8.6 Probabilistic + always-on

The standard production pattern:

```
sampler:
  probabilistic: 1%       # baseline
  always_on:
    - status_code == ERROR
    - http.status_code >= 500
    - duration > 1s
    - resource.service.name == "checkout"  # critical service
    - tracestate has 'sampled=true'        # debug flag
```

Captures the long tail, controls cost, and keeps the data store sized to a fraction of "100 %".

---

## 9. Cardinality in Tracing

Spans tolerate higher cardinality than metrics, but lower than logs. The blow-up vectors:

### 9.1 The usual suspects

```
http.url with path parameters
  /users/123/orders/456    → distinct attribute value
  /users/124/orders/789    → distinct attribute value
  ...
  500k requests = 500k distinct http.url values

  Fix: use http.route (the templated form) /users/{user_id}/orders/{order_id}
       Drop http.url to logs/baggage if needed at all.

db.statement with literal values
  SELECT * FROM users WHERE id = 42
  SELECT * FROM users WHERE id = 43
  Fix: parameterize → "SELECT * FROM users WHERE id = $1"
       Most language SDKs do this if the DB driver is wrapped.

customer_id, user_id, request_id, session_id
  Fix: keep in span attributes (cardinality is OK), NOT as resource attribute.
       (Resource attributes are deduped per-batch; span attributes are per-span.)
```

### 9.2 Stores' tolerance

| Store | Cost driver | Tolerance |
|---|---|---|
| Tempo | block index size | high — bloom filters scale OK to 100k attribute keys |
| Tempo (TraceQL search via spanmetrics) | spanmetrics series count | LOW — same as Prometheus; explodes on high-cardinality labels |
| Jaeger ES | mapping field count | LOW — explodes on >5k tag keys |
| Honeycomb | columnar scan time | HIGH — designed for it |
| ClickHouse | sort key + index | MEDIUM — hot keys must be in sort key |

The asymmetry between "store the attribute" and "filter on the attribute fast" is real. Tempo will happily store `user_id` per span; querying by it requires it to be in spanmetrics.

### 9.3 Normalization at the SDK / collector

OTel SDKs and the Collector's `transform` processor normalize before storage:

```
processors:
  transform:
    trace_statements:
      - context: span
        statements:
          - set(attributes["http.url"], URL(attributes["http.url"]).Path)
          - replace_pattern(attributes["http.route"], "[0-9]+", "{id}")
          - delete_key(attributes, "http.request.body")
```

Apply normalization at the collector, not the SDK, so the rule changes don't require redeploying every service.

> **Mental model.** Tracing cardinality cost is "what % of your storage / index is repeated copies of unique strings." Push high-entropy strings either out (drop them) or up (resource attribute, deduplicated) or down (event-scoped log line carrying the same trace_id).

---

## 10. Compression and Storage Layout

### 10.1 What compresses well in spans

- **trace_id, span_id**: 16/8 random bytes. Don't compress at all. Store as-is.
- **parent_span_id**: also random, but per-trace it correlates with span_id values nearby. Hard to compress meaningfully.
- **service.name, span.name, span.kind**: low cardinality. Dictionary-encoded gives 30–100×.
- **timestamps**: monotonic per service per shard. Delta + ZSTD gives 20×.
- **attributes**: medium cardinality keys, mixed-cardinality values. ZSTD gives 5–8×.
- **stack traces (event.exception.stacktrace)**: long but repetitive. ZSTD 8–12×.

### 10.2 Layout choices

```
A) Span as a row (ClickHouse, Jaeger ES)
   ✓ rich aggregations, projections, joins
   ✗ duplicates resource attributes per row
   ✗ harder to reconstruct a trace (multiple rows)

B) Trace as a blob (Tempo, Cassandra)
   ✓ compact: resource attributes deduped, structure intact
   ✓ fast trace fetch (single object read)
   ✗ aggregation by attribute requires re-reading every blob

C) Trace as a Parquet block (Tempo vParquet3, hybrid)
   ✓ columnar within the block, batch within the file
   ✓ row groups compress well
   ✗ block-level aggregation only; cross-block requires materialized index
```

Tempo vParquet3 is C: each block is a Parquet file with per-column compression, and the cross-block layer is per-attribute index files plus the bloom on trace_id.

### 10.3 Realistic numbers

Production ratios for a typical microservices fleet:

| Metric | Per-span raw | Compressed |
|---|---|---|
| Average span size in OTLP | 500–800 B | — |
| Span size in Tempo vParquet3 | — | 150–250 B |
| Span size in ClickHouse (typed columns) | — | 80–150 B |
| Span size in Jaeger ES (with index) | — | 600–1200 B (! — index doubles size) |
| Span size in Honeycomb (proprietary columnar) | — | ~100–200 B |

ClickHouse and the Tempo Parquet path are the most efficient by a wide margin. Jaeger-on-ES is the most expensive — index inflates storage 2–4× over the raw span.

---

## 11. Query Patterns and Engines

The five archetypal queries every trace store must answer.

### 11.1 By trace_id

```
SHOW trace 6f...        ← from a log, exemplar, or paged alert.
```

| Store | Cost | Latency |
|---|---|---|
| Tempo | bloom + 1 row group fetch per matching block | 20–80 ms |
| Jaeger Cassandra | 1 partition read | 5–20 ms |
| Jaeger ES | 1 term lookup | 10–50 ms |
| ClickHouse | bloom + granule scan | 5–20 ms |
| Honeycomb | filter on trace_id column | 50–200 ms |

### 11.2 Find traces matching attribute filter

```
{ resource.service.name = "auth" && span.http.status = 500 }
```

| Store | Approach |
|---|---|
| Tempo | spanmetrics + index.parquet to prune blocks; row-group filter |
| Jaeger Cassandra | secondary index on `tag_index` table |
| Jaeger ES | term query on tags array |
| ClickHouse | sort-key prefix + skipping index |
| Honeycomb | columnar scan + dictionary lookup |

### 11.3 Top slow operations for service X

```
SELECT span_name, p99(duration), count() FROM spans
WHERE service='auth' GROUP BY span_name ORDER BY p99 DESC LIMIT 20
```

Easy in ClickHouse and Honeycomb. Hard in Tempo (does it via spanmetrics recording rules in Prometheus). Painful in Jaeger.

### 11.4 Service graph from→to

(See §7.) Aggregation over `(parent.service, child.service)`.

### 11.5 Compare trace shape A vs B

```
Trace A took 150ms, Trace B took 1.2s. What's different?

Compute span-set difference, attribute diff, structure diff.
```

A Honeycomb specialty (BubbleUp) and a recent ClickHouse SQL pattern (with `arrayMap` and `arrayDifference`). Tempo doesn't do this natively; you fetch both traces and diff client-side.

### 11.6 TraceQL grammar

Tempo's query language. Two halves: a trace-shape predicate, then per-span filters.

```
{ resource.service.name = "auth"                     # span filter
  && span.http.status_code >= 500
  && span.duration > 100ms
  && root.duration > 1s                              # whole-trace constraint
} | by(span.http.route)                              # group result
  | count() > 5                                      # filter groups
```

Compiles to a fetch plan: spanmetrics-derived candidate filter → block index lookup → per-block row-group scan → trace assembly.

---

## 12. Live Tail, Exemplars, Correlation

### 12.1 Exemplars: the metric→trace bridge

Prometheus histograms support **exemplars** — a single trace_id stitched into one bucket of one observation:

```
http_request_duration_seconds_bucket{le="1.0",route="/checkout"} 1234 # {trace_id="6f..."} 0.951
```

The trailing `# {trace_id="..."} value` is OpenMetrics syntax for an exemplar. Grafana's panel UI renders the bucket and lets the user click the exemplar — which jumps directly into the trace store via a configured datasource link.

End-to-end:

```
SDK observes a slow request (>P99) → OTel Histogram record exemplar(trace_id)
                                  → metric exporter forwards the exemplar
                                  → Mimir/Prometheus stores it in the chunk
                                  → Grafana renders → user clicks
                                  → datasource link to Tempo /api/traces/{id}
                                  → trace renders
```

This is the operationally most important integration in the whole observability stack: it converts "I see a spike" into "show me the request" in two clicks.

### 12.2 Live tail

Most stores expose a streaming endpoint that emits spans as they are ingested, filterable by service / span-name / attribute. Useful during deploys.

| Store | Mechanism |
|---|---|
| Tempo | gRPC stream from ingesters' in-memory live traces |
| Jaeger | not native; ES has scroll/PIT patterns |
| ClickHouse | LIVE VIEW (experimental) or polling on inserts |
| Honeycomb | a "stream" mode in the UI |
| Datadog APM | live trace explorer |

### 12.3 Cross-signal correlation

The full triangle:

```
metric → exemplar → trace → span attribute → log query (by trace_id)
                       ↑
                       └── from a profile sample carrying span_id
```

Every signal carries `trace_id` so the join is 1:1. The store choices for logs/traces/profiles should respect this — making `trace_id` a first-class lookup column everywhere.

---

## 13. The Long-Tail Problem and Buffer Sizing

The traces you most want to keep are the *rare slow ones* — exactly the traces head sampling never sees and tail sampling exists to catch.

### 13.1 The math

```
At 1 % head sampling and a 1-in-1,000,000 error class:
  Expected stored errors per million events = 0.01 × 1 = 0.01
  → in 1B events, you keep ~10 errors of the rare class.

With tail sampling that always keeps errors:
  → all 1,000 errors are kept regardless.
```

The cost difference between "head 1 %" and "head 1 % + tail-keep-errors" is small (you're already storing 1 % of normal traffic, and errors are by definition rare). The diagnostic value difference is enormous.

### 13.2 Biased sampling math

Three classes:

```
class 1: normal, 99.5% of traffic    head_rate = 0.5%
class 2: error,  0.4% of traffic     keep_rate = 100%
class 3: slow,   0.1% of traffic     keep_rate = 100%
```

Storage proportion:

```
class 1: 0.995 × 0.005 = 0.00498
class 2: 0.004 × 1.0  = 0.004
class 3: 0.001 × 1.0  = 0.001

total stored / total in = 0.0100 ≈ 1%
```

You're storing roughly 1 % of total volume but capturing 100 % of errors and slow traces. This is the right point on the curve.

### 13.3 Buffer-size formula

(Repeating §8.5 because it's the most common ops question.)

```
buffer_RAM = expected_traces_in_flight × avg_spans_per_trace × avg_span_size

  expected_traces_in_flight = traces_per_sec × wait_window_sec
  default wait window:        30 s
  default avg span size:      ~500 B in OTLP protobuf
```

Memory-bound operations: this is the constraint that makes tail sampling expensive. Under an unexpected burst, the buffer doubles, the collector OOM-kills, and you lose every in-flight trace.

### 13.4 Gracefully degrading the tail sampler

```
- start dropping spans (lossy mode) when buffer > 80%
- always keep spans whose status=ERROR even in lossy mode
- expose a counter: tail_sampler_dropped_spans_total
- alert on slope of dropped_spans
```

A tail-sampler that fails open (drops everything) is worse than one that fails biased (keeps errors). Always tune for the second mode.

---

## 14. Operational Pitfalls

### 14.1 Broken context propagation

The single most common failure. Symptoms: traces exist, but they "stop" at one service. One library wasn't auto-instrumented; `traceparent` is missing on the outgoing call.

Fix: SDK auto-instrumentation for the language; for unsupported libraries, manually set `traceparent`. Add a CI check that asserts `traceparent` round-trips through every internal HTTP route.

### 14.2 Clock skew → negative-duration spans

```
Service A on host with clock at T+0
  emits span at start=T+0, end=T+10ms
Service B on host with clock at T-50ms
  receives the call, emits child span at start=T-45ms, end=T-44ms
```

Result: child span starts before parent. Tracing UIs render this as a negative-duration span or a timing anomaly. NTP skew of 100+ ms is common in cloud VMs without chrony.

Fix: chrony (not ntpd), monitor clock offset per host as a metric.

### 14.3 Missing-parent and orphan spans

Orphan = span with `parent_span_id` set but parent never seen in storage. Causes:

- Parent was head-sampled out, child wasn't.
- Tail sampler dropped the parent's batch but kept the child's.
- Parent span is still in flight when query runs.

Mitigations: tail-sample by trace_id (whole trace kept-or-dropped together); render orphan spans separately in UI.

### 14.4 Trace fragmentation across regions

A request that crosses regions (eu-west-1 → us-east-1) has spans collected by region-local collectors. If each region has its own backend, the trace is split across two stores.

Fix: a single global tail-sampling tier; all collectors forward to one regional set of tail-samplers (load-balanced by trace_id). The OTel Collector's `loadbalancingexporter` does this.

### 14.5 Tail-sampler OOM under burst

Symptom: replicated tail samplers OOM-kill in a wave during a traffic spike. Cause: each replica buffers more in-flight traces than RAM allows.

Fix: horizontal scaling tied to ingest rate; circuit breaker that drops to head-sampling-only when buffer fills; memory limits per processor.

### 14.6 Span explosion from auto-instrumented loops

A `for _, item := range items { db.Get(item) }` loop creates one span per iteration. A 10k-item loop creates a 10k-span trace; the protobuf is 5 MB; the storage cost is 100× normal.

Fixes:

- Batch DB calls in the application code (always preferable).
- Use `INTERNAL` parent span around the loop and don't auto-instrument the inner DB calls.
- Drop spans matching `kind=CLIENT && parent.span_count > 100` in the collector.

### 14.7 Tracestate budget exhaustion

`tracestate` is limited to 512 bytes by W3C. Vendors stack their entries; over 8–10 vendors and the header is truncated, breaking sampling decisions for downstream services.

Fix: prune unused vendor entries at the collector boundary.

---

## 15. Decision Tree

```
What's the dominant query?
│
├── "I follow exemplars from metrics → traces; I rarely search by attribute"
│   → Tempo. Cheapest, simplest, S3-native.
│   + always pair with metrics-generator for span_metrics + service_graph.
│
├── "I aggregate spans by attributes constantly (BubbleUp-style debugging)"
│   → Honeycomb (SaaS) or ClickHouse (self-host).
│
├── "I want one engine for logs, traces, metric exemplars; I have ops budget"
│   → ClickHouse. The lakehouse default.
│
├── "I'm a Datadog / New Relic shop already"
│   → Use their APM. The integration outweighs the cost until 500+ services.
│
├── "Strictly air-gapped / regulated; no SaaS allowed"
│   → Tempo + ClickHouse. Both run locally; both have OSS variants.
│
└── "I'm just starting; <50 services"
    → Whatever your existing observability vendor offers, or Tempo if Grafana-native.
      Don't optimize prematurely; the architecture you adopt at 50 services
      will probably not be the architecture at 500.
```

The single biggest predictor of a happy trace platform is **whether tail sampling is correctly placed and whether spanmetrics/service_graph are emitted before sampling**. Get those two right and any of the four backends is workable. Get them wrong and any architecture is painful.

---

## 16. End-to-End: Life of One Trace

A worked example, in the style of [ROADMAP §8](./ROADMAP.md#8-end-to-end-trace-of-one-request).

```
T+0ms      Browser begins POST /checkout
            Boomerang RUM SDK assigns a trace_id (16 bytes random)
            Adds traceparent: 00-{trace_id}-{span_id}-01
            Sends request to gateway

T+5ms      Envoy gateway receives request
            otel-envoy plugin creates root span S0:
              kind=SERVER, name=POST /checkout
              resource.service.name=gateway
              resource.host.name=ip-10-0-1-29
              status: pending

T+8ms      Gateway calls auth.Validate (gRPC)
            Outgoing client interceptor:
              creates child span S1: kind=CLIENT, parent=S0
              injects traceparent + tracestate into gRPC metadata

T+10ms     Auth service receives the call
            otel server interceptor:
              extracts traceparent
              creates child span S2: kind=SERVER, parent=S1
              attributes: http.method=POST, peer.service=gateway

           Auth queries Postgres
              creates span S3: kind=CLIENT (db), parent=S2
              attributes: db.statement=SELECT ..., db.rows=1
              status=OK, end-S3
            Returns 200

           span S2 ends; auth SDK's BatchSpanProcessor enqueues S2 + S3
           They will flush in the next 1s window or 512-span batch.

T+30ms     Gateway calls cart.Get
T+45ms     Gateway calls pricing.Compute
            pricing calls vendor.foo (slow, 950ms)
            pricing emits a log line:
              {level=warn, msg="vendor.timeout", trace_id, span_id}
            pricing emits an OTel span event on its span:
              "vendor.timeout"  attrs={vendor=foo, retry=2}

T+1.0s     pricing returns degraded
T+1.21s    Gateway responds 200 to client; root span S0 closed
            All processes' BatchSpanProcessors flushed in the last second

           Spans now flowing to OTel Collector (agent → gateway):

           collector pipelines:
             receivers: otlp (gRPC + http)
             processors:
               - resource (k8s.attributes processor — adds k8s metadata)
               - transform (normalize http.url path params)
               - SPANMETRICS  (BEFORE tail sampler!)
               - SERVICEGRAPH (BEFORE tail sampler!)
               - TAILSAMPLING:
                   - status=ERROR        keep
                   - latency > 1s        keep
                   - service=critical    keep
                   - else                p=1%
             exporters:
               - otlp/tempo               (traces → Tempo)
               - prometheusremotewrite    (spanmetrics + service_graph metrics)

T+2s       This trace had pricing latency >1s → tail sampler KEEPS it.
           Spans for trace_id=6f... are flushed:
             ┌─ Tempo ───────────────────────────────────────────────────┐
             │ distributor hashes trace_id → 3 ingester replicas         │
             │ ingesters assemble spans for the trace_id over ~10s       │
             │ once idle, build a Parquet row group + bloom + index      │
             │ upload block to S3                                         │
             └────────────────────────────────────────────────────────────┘
           In parallel:
             ┌─ ClickHouse-via-Kafka ────────────────────────────────────┐
             │ collector → kafka topic otel.traces                       │
             │ clickhouse-kafka-engine consumer → INSERT INTO otel_traces│
             │ background merges promote tiny parts to bigger ones       │
             │ TTL moves parts >7d to s3_disk volume                     │
             └────────────────────────────────────────────────────────────┘

T+2s       spanmetrics emitted to Mimir:
             traces_spanmetrics_calls_total{service="pricing",
                                            span_name="HTTP GET /v1/discounts",
                                            status_code="STATUS_CODE_OK"} += 1
             traces_spanmetrics_duration_seconds_bucket{...le="1.0"} += 1
           Crucially, an EXEMPLAR is attached:
             # {trace_id="6f..."} 0.951

T+3s       service_graph metrics emitted:
             service_graph_request_total{client="gateway",server="pricing"} += 1
             service_graph_request_server_seconds_bucket{... ,le="1.0"} += 1

T+5min     Burn rate rule:
             expr: sum(rate(http_requests_total{status="500"}[5m]))
                  / sum(rate(http_requests_total[5m]))
                  > slo_burn_threshold
             → not firing yet for one slow request, but burning above baseline.

T+1h       On-call engineer Mary opens Grafana checkout dashboard.
           p99 panel shows a spike at the time of this request.
           Mary clicks the bucket → exemplar opens → datasource link →
             Tempo /api/traces/6f... → trace renders as flame graph.

           Tempo lookup:
             - block_id range narrowed by trace_id encoding
             - bloom filters across ~3 candidate blocks; 1 hits
             - row group fetched from S3
             - all spans of trace assembled
             - flame graph rendered: pricing.HTTP GET /v1/discounts is the long bar

T+1h+10s   Mary sees pricing's outbound vendor span 950ms.
           Click the span → "Logs for trace" link → Loki query:
             {service="pricing"} |= "6f..."
           Returns 3 lines including {level=warn, msg="vendor.timeout", vendor="foo"}.
           Mary opens the runbook for "vendor.foo degraded":
             1. enable kill switch for vendor.foo via feature-flag console
             2. page vendor liaison

T+30d      Tempo block compacted into a larger block; bloom filters merged.
           ClickHouse parts moved to s3_disk; query latency for this trace
             rises from ~50ms to ~300ms (S3 GET) but still works.

T+90d      Both stores delete this trace per retention policy.

The same trace passed through:
  - 6 services
  - ~30 spans
  - 1 head sampling decision (sampled in)
  - 1 tail sampling decision (kept due to latency)
  - 1 spanmetrics emission (statistically valid metrics)
  - 1 service_graph edge update (visible on the service map)
  - 1 exemplar embedding (clickable from the metric panel)
  - 1 log correlation (joined by trace_id)
  - 1 incident response cycle

That is the whole life cycle.
```

---

**TL;DR.** A trace is a DAG joined by trace_id. The four production architectures split on a single axis: **trace_id-keyed object stores** (Tempo) cost the least and serve exemplar→trace flows; **attribute-indexed columnar stores** (Honeycomb, Lightstep, ClickHouse) serve attribute-shaped exploration; **legacy attribute-indexed Lucene stores** (Jaeger ES) work but get expensive; **vendor APMs** (Datadog, New Relic, Dynatrace) bundle everything at premium prices. Across all four, the same five rules apply: **propagate context everywhere**, **tail-sample by trace_id with errors-and-latency keep**, **emit spanmetrics + service_graph BEFORE tail sampling**, **normalize high-cardinality attributes at the collector**, and **wire exemplars from metrics to traces**. Get those right and any backend is workable.
