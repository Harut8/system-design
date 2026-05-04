# 07 — Logs Storage Internals

> The log line is the cheapest signal to *emit* and the most expensive to *store*. This chapter is about why. We trace one JSON-encoded log record from `stdout` through Fluent Bit, Kafka, and into the three architectural archetypes that dominate production: the inverted-index store (Elasticsearch / OpenSearch / Splunk), the label-indexed grep store (Loki), and the columnar lakehouse (ClickHouse). By the end you should be able to size each one from raw log volume and label cardinality alone, and pick the one that matches your team's query patterns and budget.

This chapter assumes you've read [03 — Instrumentation](./03-instrumentation.md) (where structured records are produced), [04 — Collection & Edge](./04-collection-and-edge.md) (how they reach the store), and [05 — Transport & Buffering](./05-transport-and-buffering.md) (Kafka / Pub/Sub between them). Cardinality framing comes from [ROADMAP §5.1](./ROADMAP.md#51-cardinality--how-many-unique-time-series-exist); the cost discipline lives in [chapter 18](./18-cardinality-and-cost.md). Trace and metric stores live in chapters [06](./06-metrics-storage.md) and [08](./08-traces-storage.md); the query engine that reads from any of these stores is [chapter 10](./10-query-layer.md).

---

## Table of Contents

1. [What a Log Line Actually Is](#1-what-a-log-line-actually-is)
2. [The Three Archetypes](#2-the-three-archetypes)
3. [Lucene & Elasticsearch Internals](#3-lucene--elasticsearch-internals)
4. [Loki Internals](#4-loki-internals)
5. [ClickHouse-on-Logs Internals](#5-clickhouse-on-logs-internals)
6. [Ingest Path Compared](#6-ingest-path-compared)
7. [Query Path Compared](#7-query-path-compared)
8. [Compression Deep Dive](#8-compression-deep-dive)
9. [Retention Tiers and Lifecycle](#9-retention-tiers-and-lifecycle)
10. [PII, Audit, and Right-to-Erasure](#10-pii-audit-and-right-to-erasure)
11. [Multi-Tenancy Patterns](#11-multi-tenancy-patterns)
12. [Cost Model: 1 TB/day Worked Example](#12-cost-model-1-tbday-worked-example)
13. [Failure Modes and Operational Pitfalls](#13-failure-modes-and-operational-pitfalls)
14. [Decision Tree](#14-decision-tree)
15. [End-to-End: Life of One Log Line](#15-end-to-end-life-of-one-log-line)

---

## 1. What a Log Line Actually Is

A log line is an **event with structure** — one row of a never-aggregated table whose schema is loose. Three properties differentiate it from the other signals.

```
metric    : an aggregate over events           — bounded by cardinality
trace span: an event tied to a DAG of events   — bounded by sampling
log line  : a raw event, kept verbatim         — bounded by retention $$$
```

A log record (per OpenTelemetry's data model) carries:

```
LogRecord {
  timestamp_unix_nano    int64    // event time
  observed_timestamp     int64    // ingestion time (set by collector)
  severity_number        int      // 1..24 (TRACE..FATAL4)
  severity_text          string   // "ERROR"
  body                   any      // the message; usually string, can be map
  attributes             map      // key/value, free-form (event-scope)
  resource.attributes    map      // immutable per process (service, host, k8s)
  trace_id               bytes16  // join key to traces (CRITICAL)
  span_id                bytes8   // join key to a specific span
  trace_flags            byte
}
```

The split between **resource attributes** (what emitted) and **attributes** (what happened) is the most important distinction in log modeling. Resource attributes are constant per process — `service.name`, `service.version`, `host.name`, `k8s.pod.uid` — and the store de-duplicates them once per stream. Event attributes — `user_id`, `order_id`, `latency_ms`, `error.code` — are per-record and are exactly the cardinality monster.

> **Mental model.** A log store is a **schema-on-read database for events**. The TSDB pre-computes `(series → samples)` because the labels are stable; the log store cannot, because the next field added is whatever a developer thought to log five minutes ago. Every architectural decision below is a way to cope with that one fact.

### 1.1 Two unbounded dimensions, one bounded one

| Dimension | Bounded by | Implication |
|---|---|---|
| **Volume (bytes/sec)** | traffic + verbosity | Scales linearly with QPS — usually 100 B – 2 KB per request line. |
| **Field cardinality** | application creativity | Unbounded. Every new attribute is a potential index column. |
| **Retention** | $$$ | The only knob you actually have. Default to short hot, long cold. |

Logs scale with **bytes**, not with series. This is the root of why a 50× cheaper architecture (Loki, ClickHouse) can exist alongside an "index everything" architecture (Elasticsearch, Splunk): the index of every term is genuinely optional if you're willing to brute-force scan, and the bytes themselves compress 10–20× because log lines repeat.

### 1.2 Severity, body, and the structured-vs-unstructured spectrum

Most production fleets sit somewhere on this gradient:

```
Plaintext printf logs           ← legacy, expensive to index
"2026-05-03 10:31:02 INFO checkout: user 42 paid 12.99"

   ↓ parse at the agent (Fluent Bit parsers, Vector VRL)

Semi-structured key=value
ts=2026-05-03 level=info svc=checkout user=42 amount=12.99

   ↓ structured logger emits JSON directly

Fully structured JSON                  ← the modern default
{"ts":"2026-05-03T10:31:02Z","level":"info","msg":"checkout.paid",
 "user_id":42,"amount":12.99,"trace_id":"6f...","span_id":"a1..."}

   ↓ semantic conventions (OTel)

Schema-conformant OTLP LogRecord       ← what a vendor-neutral pipeline ships
```

Every step up the gradient cuts ingest CPU (no parser regex), index size (typed columns), and query cost (no full-text scan to find a numeric range). The single highest-leverage change in 90 % of legacy stacks is "make every log line carry `trace_id` and emit JSON".

### 1.3 The two unforgivable omissions

A log without `trace_id` cannot join to a trace. A log without `service.name` cannot be attributed. Everything else is recoverable; these two are not. Enforce them at the SDK/collector layer, not at the store.

---

## 2. The Three Archetypes

Every production log store sits in one of three architecture buckets. The choice is *not* about the query language — it's about what you index at write time and what you brute-force at read time.

```
                  WRITE COST                           READ COST
                  ─────────                            ─────────

A) INDEX EVERYTHING (Elasticsearch / OpenSearch / Splunk)
   ┌──────────────┐                          ┌──────────────────┐
   │ doc + index  │ all terms tokenized      │ inverted index   │
   │ inverted idx │ posting lists per term   │ → seek by term   │
   │ doc_values   │ stored field values      │ → fetch doc      │
   │ +translog    │ durability               │ ~10 ms per query │
   └──────────────┘                          └──────────────────┘
   write amp: 5–15×          query: ad hoc, full text, fast

B) INDEX THE LABELS (Loki / Grafana Logs)
   ┌──────────────┐                          ┌──────────────────┐
   │ stream meta  │ index = labels only      │ pick streams by  │
   │ chunk store  │ chunks = compressed log  │   label match    │
   │  on object   │   lines, append-only     │ then GREP every  │
   │  storage     │                          │   chunk in range │
   └──────────────┘                          └──────────────────┘
   write amp: ~1×           query: cheap by label, expensive by content

C) COLUMNAR (ClickHouse / BigQuery / Snowflake / DuckDB-on-S3)
   ┌──────────────┐                          ┌──────────────────┐
   │ MergeTree    │ row→column, sort key     │ partition prune  │
   │ part files   │ ZSTD/LZ4 columnar blocks │   then mark scan │
   │ skipping idx │ minmax/bloom/set/ngrambf │ + skipping idx   │
   │ TTL/move     │ SSD→S3 by age            │ → SQL anything   │
   └──────────────┘                          └──────────────────┘
   write amp: ~2×           query: fast aggregate, slower by-id
```

### 2.1 The summary trade-off

| | A) Index everything | B) Index labels | C) Columnar |
|---|---|---|---|
| Examples | Elasticsearch, OpenSearch, Splunk, Datadog Logs | Loki, Grafana Cloud Logs, VictoriaLogs (≈) | ClickHouse, BigQuery, Snowflake, Athena/Iceberg, DuckDB |
| Query languages | KQL, Lucene, SPL, ES|QL | LogQL | SQL (+ dialects) |
| Storage cost (relative) | 1× | 0.05–0.1× | 0.1–0.2× |
| Ingest CPU | high (tokenize + write postings) | low (just label and append) | medium (column encoding) |
| Ad-hoc full-text | excellent | linear-scan / regex | linear-scan / regex / `LIKE` / `hasToken` |
| Aggregations | OK (doc_values) | poor (re-scan to compute) | excellent |
| Retention scaling | painful (per-doc cost) | great (chunks on S3) | great (parts on S3 via TTL) |
| Schema-on-read | yes, partial | yes (parse on query) | optional via `JSONExtract*` |
| Kills you when | cardinality of indexed fields | label cardinality | bad sort key, runaway mutations |

### 2.2 Why three, not one?

Because three different teams have three different dominant queries:

- **"My service died — show me the error logs in the last 30 minutes by request_id."** That is a *find this string fast* query. Index everything wins.
- **"This deploy is regressing checkout — tail logs for `{app=checkout, env=prod}`."** That is a *narrow stream, brute force the chunk* query. Loki wins because it doesn't pay for indexes you don't use.
- **"Compute the top-100 customer_ids by 5xx error count over the last 24 h, grouped by API route."** That is a SQL aggregation. Columnar wins by 10–100×.

Every other architectural detail in this chapter is downstream of which query class dominates the team's life.

---

## 3. Lucene & Elasticsearch Internals

The single most-deployed log architecture: **Apache Lucene under Elasticsearch / OpenSearch**, used by Elastic Cloud, Splunk's index store (different code, same idea), AWS OpenSearch Service, and a long tail of self-hosted clusters. We focus on Elasticsearch 8.x; OpenSearch 2.x is binary-compatible at the Lucene layer.

### 3.1 The Lucene segment

A Lucene **index** is a directory of immutable **segments**. Each segment is a self-contained mini-index over a subset of documents. Writes always create new segments; updates are tombstone+rewrite; deletes are tombstones merged later.

```
segments/
├── _0.si        segment info (doc count, version, codec)
├── _0.cfs       compound file (most files merged for fewer FDs)
├── _0.cfe       compound file entries (offsets within .cfs)
└── _0.del       deleted-doc bitset (sparse)

Inside _0.cfs (logical view):
├── *.fdt / *.fdx   stored fields (the original "_source" values)
├── *.tim / *.tip   term index + dictionary (FST → posting offsets)
├── *.doc           postings (docID, freq, skip lists)
├── *.pos           positions (for phrase queries; optional)
├── *.dvm / *.dvd   doc values (column-oriented per-doc field values)
├── *.nvm / *.nvd   norms (length, boost factors)
├── *.fnm           field metadata
└── *.kdd / *.kdi   BKD tree (numeric / geo / point fields)
```

The two on-disk structures that matter for log workloads are the **inverted index** (`.tim/.tip/.doc`) and the **doc values** (`.dvm/.dvd`).

### 3.2 The inverted index, term-by-term

For a tokenized text field, every distinct term gets a **postings list**: a sorted list of `(doc_id, term_freq, positions)` entries.

```
field "msg" of segment _0:

  term  "checkout"   → [3, 17, 42, 88, 91, 137, ...]
  term  "error"      → [3, 17, 19, 88, 137, 200, ...]
  term  "timeout"    → [88, 137, 415, ...]
  term  "user"       → [3, 17, 42, 88, 91, 137, 200, ...]

Query: msg:(checkout AND error AND timeout)
       = INTERSECT(checkout, error, timeout)
       = [88, 137]                      ← two candidate docs
```

The intersection uses the **skip lists** baked into the postings: instead of scanning all of "user", advance past it to find the next docID ≥ the next candidate from the smaller list. Lucene picks the *shortest* postings list to drive the iteration. This is why `error` (rare) is fast and `the` (common) is not — costs scale with the rarest term that survives short-circuiting.

#### 3.2.1 The term dictionary: FSTs

Looking up "checkout" in a segment with 50 million distinct terms cannot be a linear scan and cannot be an in-RAM hashtable (too big). Lucene uses a **finite-state transducer (FST)** stored in `.tip`:

```
FST for terms {check, checkout, checker}:

       c
       │
       h
       │
       e
       │
       c
       │
       k───── (output: posting offset for "check")
       │
       e ── r ── (output: ...for "checker")
       │
       o ── u ── t ── (output: ...for "checkout")
```

The FST is a minimized DAG that maps each term to its postings offset in `.tim`. A term lookup costs O(term_length) memory accesses, fits the hot terms in CPU cache, and amortizes the dictionary cost across millions of terms. The FST is *memory-mapped*, so cold terms cost a single page-in.

> **Mental model.** A Lucene segment is a *write-once columnar database* whose primary index is a per-column FST and whose value column is a postings list. "Indexing a log line" = tokenizing every text field, looking each token up in the in-memory term-buffer, and appending the docID to its postings list. Then on flush, merge-sort the term buffer into the segment FST.

### 3.3 Doc values: the columnar half

For sorting, faceting, and aggregations you don't want postings — you want a **column** of `field → value` keyed by docID. That's `.dvd`/`.dvm`:

```
doc_values for "level":
  doc 0 → "info"
  doc 1 → "error"
  doc 2 → "info"
  doc 3 → "warn"
  ...

stored as:
  ord_table        : ["debug", "error", "info", "warn"]      (sorted unique)
  per-doc ords     : [2, 1, 2, 3, ...]                        (packed bits)
```

Doc values are dictionary-encoded for low-cardinality fields (perfect for `level`, `service`, `region`), and packed-int-encoded for numerics. They make `terms` aggregations on a 100-million-doc index return in tens of ms.

### 3.4 The translog and durability

A write to Elasticsearch goes:

```
   POST /logs-2026.05.03/_doc
        │
        ▼
1. parse JSON, derive routing (default: _id hash)
2. send to primary shard's node
        │
        ▼
3. write to in-memory buffer (Lucene IndexWriter)
   AND append to translog (flushed to disk per request, by default)
        │
        ▼
4. ack to client                                ← durability point
        │
        ▼
5. every 1s (default refresh_interval): flush buffer → new searchable segment
6. every 30 min OR translog full: fsync segments + truncate translog
```

The translog is the **Lucene WAL**. If the node crashes between step 5 and 6, the segments are lost but the translog can replay. If you set `index.translog.durability: async`, you batch fsyncs (cheaper, brief data-loss window on crash); `request` (default) fsyncs every write (safer, higher CPU).

The 1 s refresh interval is why ES is "near-real-time": writes are durable immediately but not searchable until the next refresh. Bumping `refresh_interval` to `30s` for log indices halves indexing CPU at the cost of search staleness — a trade most observability teams take.

### 3.5 Segment merging: the silent killer

Refreshes produce many small segments. Lucene merges them in the background by tier (TieredMergePolicy is default):

```
Tier 0: 5–10 small segments (~5 MB each)  ← fresh refreshes
Tier 1: ~50 MB segments      ← merged from tier 0
Tier 2: ~500 MB
Tier 3: ~5 GB (max_merged_segment, default 5 GB)
```

Merges are I/O- and CPU-intensive: they read all input segments, merge-sort the postings, rewrite doc values, reapply the deleted-doc bitset, and produce a new segment. **A poorly tuned merge schedule is the most common cause of "ES is slow today" tickets.**

> **Pitfall.** Index lifecycle: hot (write-heavy, many small segments), warm (read-only, force-merged to one segment per shard), cold (frozen, partial caching), delete. Failing to force-merge before warming guarantees query latency 5–20× higher than necessary because every shard has 50 segments instead of 1.

### 3.6 A query, end-to-end

```
GET logs-*/_search
{
  "query": {
    "bool": {
      "filter": [
        { "term":  { "service.keyword": "checkout" } },
        { "range": { "@timestamp": { "gte": "now-1h" } } },
        { "match": { "msg": "vendor timeout" } }
      ]
    }
  },
  "aggs": {
    "by_route": { "terms": { "field": "http.route.keyword", "size": 10 } }
  }
}
```

1. **Coordinator** receives query, picks indices that match `logs-*`.
2. For each matching index, calculate the **shards** that could contain matches (filtered by index-level date-range metadata + routing).
3. **Scatter**: send a `query` phase to one replica of each shard.
4. On each shard:
   - Skip via the **search_after / index sort** if the index is sorted by `@timestamp`.
   - For `term`/`range`/`match` filters, intersect postings (cheap shortest-list-first).
   - Compute hits, score them (or skip scoring for `filter` clauses).
   - Aggregate using doc values.
5. Coordinator gathers (top_hits, aggs partial) results, reduces.
6. **Fetch phase**: for the final top_N docIDs, fetch the `_source` from `.fdt`.

For log workloads, two optimizations are non-negotiable:

- **Index sort by `@timestamp` desc** (`index.sort.field`, `index.sort.order: desc`) — turns range filters into early termination and cuts query cost dramatically when results are time-bounded.
- **Skip scoring** for log queries — wrap your search in a `bool.filter` clause, never `bool.must`. Scoring requires norms and term-frequency math you don't care about.

### 3.7 Splunk: same family, different code

Splunk's indexer is conceptually the same: tokenize on write, write per-day "buckets" (segments) of `tsidx` (time-series index, the inverted index analog), `journal.gz` (raw events, doc-store analog), and `bloomfilter`. Differences:

- Splunk is **schema-on-read** — fields are extracted at search time via SPL, not at index time. Faster ingest, slower search.
- Buckets graduate **hot → warm → cold → frozen** by age (similar to ES ILM but native to the product).
- The pricing model (per-GB-ingested) is what made the entire alternative-architecture market exist.

### 3.8 Datadog Logs and "indexed vs flex" tiers

Datadog (and most SaaS log vendors) maintain two tiers:

- **Indexed** logs are inverted-indexed — fast queries, expensive bytes.
- **Flex / archive** logs are object-storage-backed columnar / Parquet — cheap, slow queries.

You "rehydrate" archive logs back into indexed form when you need them. This is the SaaS take on the same hot/warm/cold tiering ES users do via ILM, packaged behind a single dashboard.

---

## 4. Loki Internals

Loki is the answer to the question "how cheap can a log store get if I refuse to index any text?" Its design slogan is **"Prometheus, but for logs"**: streams instead of series, label-only index, brute force the rest.

### 4.1 The data model: streams and chunks

```
stream     := unique label set
            e.g. {app="checkout", env="prod", pod="checkout-7c5b-9f2"}
chunk      := append-only compressed buffer of log lines for ONE stream
chunk_id   := (tenant, stream_hash, from_ts, to_ts, checksum)
```

Every log line belongs to exactly one stream, identified by its label set (and only its label set). The label set is the *only* thing Loki indexes; the log message itself is brute-force scanned.

```
{app="checkout", env="prod", pod="..."}     ← stream
  ts=10:00:00.000  "GET /cart 200 12 ms"
  ts=10:00:00.013  "GET /cart 200 11 ms"
  ts=10:00:00.027  "POST /checkout 200 87 ms"
  ts=10:00:01.044  "POST /checkout 500 vendor.timeout"
  ts=10:00:01.211  "POST /checkout 200 102 ms"
  ...                                       ← all packed into one chunk
```

A chunk fills up by line count (`chunk_target_size`, ~1.5 MB compressed), or time (`max_chunk_age`, default 1h), or stream churn (pod restart → new label, new stream).

### 4.2 The component layout

```
                   ┌─────────────────┐
   logs ──→        │ DISTRIBUTOR     │ hash(stream) → ingester replica set
                   └────────┬────────┘
                            │ gRPC
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
       ┌────────┐      ┌────────┐      ┌────────┐
       │INGESTER│      │INGESTER│      │INGESTER│
       │  WAL   │      │  WAL   │      │  WAL   │   ← 3-replica quorum
       │ chunks │      │ chunks │      │ chunks │
       └───┬────┘      └───┬────┘      └───┬────┘
           │               │               │
           └───── flush ───┴───── flush ───┘
                            │
                   ┌────────▼────────┐
                   │  OBJECT STORAGE │   chunks/<id>
                   │   (S3 / GCS)    │   index/<period>/<period>.tsdb
                   └────────┬────────┘
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
       ┌────────┐      ┌────────┐      ┌────────┐
       │QUERIER │ ←→   │QUERIER │ ←→   │QUERIER │   pulls index + chunks
       └────────┘      └────────┘      └────────┘
```

**Distributors** are stateless; they hash the stream label set, pick `replication_factor` (default 3) ingester replicas, forward writes, and ack on quorum.

**Ingesters** hold chunks in memory while they fill. They write a WAL on local disk for crash recovery. Once a chunk is full (or a flush deadline hits), it is uploaded to object storage and the index is updated.

**Object storage** is the sole source of truth for old data. Ingesters are ephemeral.

**Queriers** are stateless; they consult the index for matching streams, then fan out chunk reads from S3 + ingesters (for in-memory recent data). The fan-out is parallel and the chunks themselves are 1–5 MB compressed — a query that touches 10 streams over 1 hour reads ~50 MB and `grep`s through it.

### 4.3 The index: from boltdb-shipper to TSDB

Loki has had three index implementations:

- **boltdb-shipper** (deprecated): per-day BoltDB files, shipped to object storage, downloaded by queriers.
- **TSDB index** (current default since Loki 2.8/3.x): the **same TSDB code** Prometheus uses, repurposed to map labels → chunk IDs. This is why Loki and Prometheus share a cardinality model.

```
TSDB index entry:

  postings "app=checkout"       → [stream_id_1, stream_id_2, ...]
  postings "env=prod"           → [stream_id_1, stream_id_3, ...]
  series stream_id_1            → label set + list of chunk_ids in time order
```

Resolving `{app="checkout", env="prod"} |= "vendor.timeout"`:

1. Intersect postings for each label matcher → candidate streams.
2. For each stream, fetch chunk_id list intersecting `[from, to]`.
3. Fetch chunks from object storage (or from in-memory ingesters).
4. Decompress, scan with the line-filter `|= "vendor.timeout"`.
5. Return matching lines.

> **Mental model.** Loki = `Prometheus index + S3 grep`. Cardinality of *labels* is the cost driver, identical to Prometheus. Cardinality of *log content* is free. This is why putting `request_id` in a label is fatal but putting it in the log line is fine.

### 4.4 Chunk format

A Loki chunk is a sequence of **blocks**, each with a target uncompressed size (`block_size`, default 256 KB):

```
chunk
├── header                     [version, encoding, block count]
├── block 0
│   ├── compressed entries     [zstd | snappy | lz4 | gzip]
│   ├── ts of first entry
│   └── ts of last entry
├── block 1
├── ...
└── footer
    ├── per-block index        (offset, ts range, entry count)
    └── checksum
```

The footer holds a small index that lets a querier *seek* into the chunk and skip blocks whose `[from, to]` doesn't intersect the query range. With the default zstd encoder, log lines compress 8–15× depending on repetition.

### 4.5 The cardinality knife edge

A Loki "stream" is one unique label set. A 1000-pod deployment with `{app, env, pod}` labels has 1000 streams. Add `request_id` as a label, and now every request creates a new stream. Each stream has its own chunk; a chunk holds at minimum a few KB of overhead. **A 1 M-stream cluster has a multi-GB index and chunks that are too small to compress well.**

Loki enforces this with `max_streams_per_user` and `max_global_streams_per_user`. Hitting the limit returns 429 to the agent, which buffers and retries (and eventually drops). Production Loki teams treat label cardinality with the same paranoia Prometheus teams treat metric cardinality.

### 4.6 LogQL: from query to chunks

```
{cluster="prod", app="checkout"}
  |= "vendor"               # line filter (substring)
  | logfmt                  # parse logfmt → label-like fields
  | latency_ms > 500        # numeric filter on parsed field
  | __error__ = ""          # drop lines that failed parsing
```

Execution:

1. **Selector** `{cluster, app}` → index lookup → list of chunks.
2. **Line filters** `|= "vendor"` → applied during chunk scan in the querier (cheap; Boyer-Moore on uncompressed bytes).
3. **Pipeline stages** `logfmt`, `latency_ms > 500` → applied after decompression.
4. **Result** streamed back to the frontend, then merged.

Aggregations (`rate({app="checkout"} |= "error" [5m])`) push down line filters but compute the rate at the querier — there's no "log metric" pre-aggregate. Heavy aggregations get expensive fast; for those, Loki's recording rules pre-compute series into a Prometheus-compatible TSDB.

### 4.7 What Loki is bad at

- **Ad-hoc full-text search across many streams**: the cost is "open every chunk in scope and scan." Acceptable for tens of streams; painful for thousands.
- **Wide aggregations**: `count by (user_id)` requires re-parsing every line every time.
- **Cardinality blow-ups via labels**: same as Prometheus. The temptation to "just add a label" is the recurring outage cause.

The architecture is brilliantly tuned for the dominant log query pattern in microservices ("narrow stream, recent time, find a string"), and brutally bad outside it.

---

## 5. ClickHouse-on-Logs Internals

ClickHouse is the rising lakehouse choice for logs. It's a column-store OLAP engine that, with a logs-shaped schema, gives you SQL across petabytes, retention via TTL, and the lowest $/TB ingested of any architecture. Cloudflare, Uber, eBay, and Posthog have public write-ups on this exact pattern.

### 5.1 The MergeTree engine, briefly

```
CREATE TABLE logs (
  timestamp        DateTime64(9, 'UTC') CODEC(DoubleDelta, ZSTD(1)),
  trace_id         FixedString(16),
  span_id          FixedString(8),
  service          LowCardinality(String),
  env              LowCardinality(String),
  level            LowCardinality(String),
  host             LowCardinality(String),
  body             String CODEC(ZSTD(3)),
  attrs            Map(LowCardinality(String), String) CODEC(ZSTD(3)),
  resource_attrs   Map(LowCardinality(String), String) CODEC(ZSTD(3)),
  -- skipping indexes
  INDEX idx_trace      trace_id   TYPE bloom_filter(0.01) GRANULARITY 1,
  INDEX idx_body_ngram body       TYPE ngrambf_v1(4, 1024, 3, 0) GRANULARITY 4,
  INDEX idx_attrs_keys mapKeys(attrs) TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toStartOfHour(timestamp)
ORDER BY (service, timestamp)
TTL timestamp + INTERVAL 7 DAY TO VOLUME 'cold',
    timestamp + INTERVAL 90 DAY DELETE
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;
```

Key properties:

- **Parts**: a write produces one or more part directories, each a self-contained mini-table (columns + marks + indexes).
- **Background merges**: parts merge by level (similar to LSM trees, see `databases/13-lsm-trees-and-compaction.md`), maintaining sort order.
- **Sparse primary index**: every `index_granularity` rows (default 8192), one index entry. Lookups jump to the nearest granule, then scan within.
- **Skipping indexes**: per-granule mini-summaries (bloom filters, minmax, set, ngrambf) for *secondary* fields. Cheap, optional, dramatically prune scans.
- **TTL clauses**: parts can be moved between volumes (SSD → HDD → S3) or deleted entirely as data ages.

### 5.2 The sort key choice

The sort key (`ORDER BY (service, timestamp)`) is the single most important decision. It determines:

- **Compression ratio**: data with a sort key is locally similar — `service` repeats, timestamps are monotonic, body texts cluster.
- **Query speed**: filters on the leading key columns prune the most data via the sparse primary index.
- **Insert speed**: incoming batches are sorted in memory before being written; bad sort keys = expensive sorts.

Common log sort keys:

| Sort key | Wins when | Loses when |
|---|---|---|
| `(service, timestamp)` | most queries filter by service | cross-service trace_id lookup |
| `(timestamp)` | cross-service time-range queries dominate | per-service zoom queries |
| `(toStartOfHour(timestamp), service, level)` | partition-aligned, highly grouped reads | unusual filter combinations |
| `(tenant_id, service, timestamp)` | multi-tenant; tenant filters every query | global queries across tenants |

> **Mental model.** ClickHouse has no per-row index. The sparse primary index is a *hint* that gets you to roughly the right granule in O(log N); the column scan within the granule is brute force. This is why the sort key dictates everything — a bad sort key turns a 10 ms query into 10 s.

### 5.3 Column codecs and compression

Each column gets its own codec chain. Three stand out for logs:

```
DoubleDelta + ZSTD     for monotonic int64 (timestamp, sequence number)
LowCardinality(String) for fields with <10k distinct values (service, level, env)
ZSTD(3..9)             for the message body and large strings
```

Realistic compression ratios on production logs:

| Column | Raw bytes/row | Codec | Compressed bytes/row | Ratio |
|---|---|---|---|---|
| timestamp | 8 | DoubleDelta+ZSTD | 0.4 | 20× |
| trace_id | 16 | ZSTD | 4 | 4× |
| service | 16 (avg) | LowCardinality | 0.5 | 30× |
| level | 5 | LowCardinality | 0.05 | 100× |
| body | 250 | ZSTD(3) | 25 | 10× |
| attrs map | 200 | ZSTD(3) | 30 | 7× |
| **total** | ~500 B/row | | ~60 B/row | **~8×** |

A 1 TB/day raw stream becomes ~125 GB/day on disk. Over 30 days at $0.023/GB-month on S3, that's <$100/month for storage of 30 TB raw equivalent.

### 5.4 Skipping indexes

The skipping index is ClickHouse's answer to "how do I avoid scanning columns I don't filter on?"

```
INDEX idx_body_ngram body TYPE ngrambf_v1(4, 1024, 3, 0) GRANULARITY 4

  → for every 4 granules (4 × 8192 = ~32k rows),
    build a bloom filter over all 4-grams in `body`
  → query "body LIKE '%vendor.timeout%'" extracts 4-grams,
    checks the bloom; if absent, skip those 32k rows entirely
```

Index types you'll use most for logs:

| Type | Good for | Cost |
|---|---|---|
| `bloom_filter(p)` | exact equality on high-cardinality fields (`trace_id`, `user_id`) | ~1 % FP @ 1 % space |
| `ngrambf_v1(n, size, hashes, seed)` | substring search on text | ~5–15 % space, ~5–10× speedup |
| `tokenbf_v1(size, hashes, seed)` | word search on text | similar |
| `minmax` | numeric range filters | ~24 B/granule |
| `set(max_rows)` | low-cardinality string filters (`level IN ('error','warn')`) | small |

A common production pattern: `bloom_filter` on `trace_id` and `user_id`; `ngrambf` on `body`; `set` on `level` and `env`; `minmax` on numeric attrs.

### 5.5 Materialized views and projections

Two ways to pre-compute aggregations:

**Materialized view** (a separate table with its own MergeTree, populated by an `AS SELECT` triggered on inserts):

```
CREATE MATERIALIZED VIEW logs_per_service_5m
ENGINE = SummingMergeTree
ORDER BY (service, env, level, ts5m)
AS SELECT
  service, env, level,
  toStartOfFiveMinute(timestamp) AS ts5m,
  count() AS log_count,
  sum(length(body)) AS bytes
FROM logs
GROUP BY service, env, level, ts5m;
```

**Projections** (a secondary materialized view *attached to the same table*, transparently used by the optimizer):

```
ALTER TABLE logs ADD PROJECTION p_by_trace (
  SELECT * ORDER BY trace_id
);
```

A query that filters on `trace_id` will silently use `p_by_trace` instead of the main sort, dropping a full-scan to a granule scan. The cost: every part has a copy of itself sorted by `trace_id`, doubling storage. Pick projections for queries that need to bypass the primary sort key.

### 5.6 TTL: the lifecycle weapon

```
TTL timestamp + INTERVAL 1 DAY  TO VOLUME 'ssd_warm',
    timestamp + INTERVAL 7 DAY  TO VOLUME 'cold_s3',
    timestamp + INTERVAL 90 DAY DELETE
```

ClickHouse moves whole parts between storage volumes when their TTL expires, or drops them. With `ttl_only_drop_parts=1`, the TTL is enforced per part instead of per row, which avoids expensive mutations. The volume layout (`disks` config) defines `ssd_hot`, `ssd_warm`, `cold_s3` — the same MergeTree table seamlessly spans local SSD and S3.

> **Pitfall.** TTL `WHERE` clauses with column predicates (e.g., `TTL ... WHERE level='debug' DELETE`) require *mutations*, which rewrite parts. Mutations are the slowest operation in ClickHouse and cause replication lag. Prefer a TTL on whole parts when possible.

### 5.7 A query, end-to-end

```sql
SELECT
  toStartOfMinute(timestamp) AS m,
  service,
  countIf(level = 'error') AS errors,
  count()                   AS total
FROM logs
WHERE timestamp >= now() - INTERVAL 1 HOUR
  AND service IN ('checkout', 'pricing')
  AND has(mapKeys(attrs), 'http.route')
GROUP BY m, service
ORDER BY m, service;
```

Execution:

1. **Partition prune**: keep only parts whose partition (`toStartOfHour(timestamp)`) is in the last hour.
2. **Primary index lookup**: within each part, find granules where `service` is in the IN list (sort key prefix).
3. **Skipping indexes**: for `has(mapKeys(attrs),'http.route')`, the `idx_attrs_keys` bloom filter prunes granules.
4. **Mark-level scan**: read `timestamp`, `service`, `level`, `attrs` columns *only* for surviving granules.
5. **Aggregation pipeline**: vectorized hash aggregation, parallel by part.
6. **Merge**: distributed-engine combines partial states and returns.

With a well-chosen sort key + indexes, ClickHouse routinely sustains **>1 GB/s scan throughput per node** and answers a 1-hour query over 100 GB of log data in subseconds.

### 5.8 The Replicated and Distributed engines

Production deployments use:

- `ReplicatedMergeTree` — same data on N replicas, coordinated via Keeper / ZooKeeper.
- `Distributed` table on top — a façade that fans queries across shards and merges results.

Sharding is by some hash (`cityHash64(service)`) so each tenant or service is local to a shard set, while the Distributed table makes the user think they're talking to a single table.

---

## 6. Ingest Path Compared

The hot write path for each archetype.

### 6.1 Durability

| Store | Durability mechanism | Default ACK semantics |
|---|---|---|
| Elasticsearch | Translog fsync per request | Strong (translog `request`) |
| Splunk | Indexer queue + journal.gz fsync | Configurable |
| Loki | Per-ingester WAL on local disk + 3-replica quorum | Quorum on RF/2+1 ingesters |
| ClickHouse | Async insert buffer or part-flush + replication | Configurable per-query |
| Datadog Logs | Internal queue with at-least-once + multi-region replication | At least once |

The loudest production failure mode is **agent-side data loss when the store backpressures**. Always size the agent (Fluent Bit, Vector) to buffer at least 5–15 minutes to disk so a 10-minute backend outage does not turn into a permanent data loss event.

### 6.2 Schema

| Store | Schema-on-write | Schema-on-read |
|---|---|---|
| Elasticsearch | Yes — dynamic mapping per first-seen field; aggressive type inference | Partial (runtime fields, ES|QL) |
| Loki | No — only labels are typed | Yes — `| logfmt`, `| json`, `| regexp` |
| ClickHouse | Yes — columns are typed | Partial via `Map(...)` + `JSONExtract*` |
| Splunk | No — fields extracted at search time | Yes (the SPL philosophy) |

The cost of dynamic-mapping ES is **mapping explosion**: a careless application that emits 50,000 distinct attribute names creates a 50,000-field index, and every query pays that overhead. Production ES setups disable dynamic mapping and enforce explicit templates.

### 6.3 Sharding & partitioning

```
Elasticsearch  : index = N primary shards × (1 + replica_count) replicas
                 routing = hash(_id) mod N (or custom routing key)
Loki           : stream → hash(stream_labels) → 3-of-N ingesters
ClickHouse     : Distributed table → hash(sharding_key) → shard
Splunk         : indexer cluster, hot bucket per peer, replication factor
```

The cardinal sin is "1 primary shard for 1 PB of data" (a too-fat shard ⇒ oversized merges, slow recovery, hot node) and the inverse sin is "1000 shards for 100 GB" (per-shard overhead ⇒ memory blowup, slow searches). Aim for 10–50 GB primary shards for ES, 1–5 GB chunks for Loki, 50–500 GB parts for ClickHouse.

### 6.4 Tenant isolation at ingest

| Store | Tenant boundary | Per-tenant rate limits |
|---|---|---|
| Elasticsearch | Index-per-tenant or doc-level routing + role | Per-index, via Elastic Stack security |
| Loki | `X-Scope-OrgID` header → separate index + chunk space | Yes, in limits config |
| ClickHouse | Database, table, or row-level policy | Per-user/`profile` quotas |

Cross-tenant leakage is always a **read-side** problem (a query with the wrong tenant filter returns another tenant's data). Enforce at the read path with mandatory predicates injected by the query layer; never trust application code to add the right `WHERE tenant_id = ?` clause.

---

## 7. Query Path Compared

What happens when a user asks a question.

### 7.1 The two query archetypes

```
QUERY A: "Find this string in the last hour"
   ES   : term/match → postings intersect → fetch → score
   Loki : labels → chunks → grep → return matching lines
   CH   : sort key + skipping index → granule scan → string search

QUERY B: "Aggregate by attribute over the last day"
   ES   : doc_values terms agg
   Loki : decode every chunk, parse, group, count (slow)
   CH   : columnar vectorized aggregation (fast)
```

Both ES and ClickHouse are fast at both archetypes (with appropriate index/sort). Loki is fast at A and slow at B by design.

### 7.2 Pushdown filters

The store-side filter is always 100–1000× cheaper than the engine-side filter. Tools to push down:

- **ES**: filter clauses in `bool.filter`; `index.sort` for time queries.
- **Loki**: `|= "..."` line filter applied during chunk decompression, before parser stages.
- **ClickHouse**: WHERE clauses on sort-key columns; skipping indexes for non-key columns; `PREWHERE` to apply cheap predicates first.

> **Pitfall.** A query like `count() WHERE JSONExtractString(body, 'user_id') = '42'` in ClickHouse cannot be pushed down — the JSON is still inside `body`. Materialize hot fields to typed columns or use `Map(...)` with bloom indexes.

### 7.3 Distributed scatter / gather

```
   client
      │
      ▼
  ┌────────────┐
  │ COORDINATOR│───────fan-out──────────┐
  └────────────┘                        │
   ▲                  ▲                 ▲
   │                  │                 │
 ┌─┴─┐              ┌─┴─┐             ┌─┴─┐
 │ S1 │            │ S2 │           │ S3 │
 └────┘            └────┘           └────┘
   │                  │                 │
   ▼                  ▼                 ▼
 partial            partial           partial
   └────────── merge ────────────────────┘
                      │
                      ▼
                   client
```

For ES, partial = top-K per shard + agg state; for Loki, partial = matching lines per chunk; for ClickHouse, partial = aggregation hash table per shard. The coordinator merges. The slowest shard sets the latency — **tail-skewed shards** (one tenant 100× the others) ruin distributed query latency more than any other production issue.

### 7.4 Live tail

| Store | Live tail mechanism |
|---|---|
| ES | `_search` polling, or refresh interval ≤ 1 s + scroll/PIT |
| Loki | gRPC stream `/tail` from ingesters that hold the in-memory tail |
| ClickHouse | `INSERT INTO logs ... ENGINE=Buffer` or `LIVE VIEW` (experimental) |
| Splunk | Real-time search via `index=*` + indexer push |

Loki's tail UX is the smoothest — ingesters already hold the most recent 30 minutes in memory; tailing is a label-matched gRPC stream out of those buffers.

### 7.5 Pagination

Logs page in time order, almost always descending. The right primitives:

- **ES**: `search_after` with `[ts, _id]` instead of `from/size` (which is O(N) per page).
- **Loki**: `direction=BACKWARD`, `start`/`end` window, `step` plus a stable order on `(ts, line)`.
- **ClickHouse**: `ORDER BY timestamp DESC LIMIT N OFFSET M` with the sort key actually matching that order; otherwise the whole result is materialized first (catastrophic on logs).

---

## 8. Compression Deep Dive

Most of the cost story.

### 8.1 Why log lines compress

Production log streams are full of repetition:

- The same JSON keys appear on every record (`"timestamp"`, `"level"`, `"msg"`, `"trace_id"`).
- The same service name, host, k8s pod label appear on every record.
- HTTP status codes, URL prefixes, error class names cluster in a few hundred values.
- Timestamps are monotonic; deltas are 0–100 ms.

A naive LZ4 over a raw JSON log stream gets 5–8×. ZSTD level 3 gets 8–12×. Columnar formats (with dictionary + delta encoding *before* the entropy coder) get 15–25×.

### 8.2 The four big tricks

```
1. DICTIONARY ENCODING                  for repeated strings
   "service":"checkout"                 service_dict[7] = "checkout"
   "service":"checkout"                 service_dict[7]
   "service":"pricing"                  service_dict[3] = "pricing"
   ...                                  rows: [7, 7, 3, 7, 3, 3, 7, ...]

2. DELTA / DOUBLE-DELTA ENCODING        for monotonic ints (timestamps)
   ts = [t0, t0+15, t0+30, t0+45, ...]
   delta = [t0, 15, 15, 15, 15, ...]
   double-delta = [t0, 15, 0, 0, 0, ...]   ← run-length friendly

3. RLE                                  for low-cardinality columns post-sort
   level = [info, info, info, info, error, info, info, ...]
   rle   = [(info, 4), (error, 1), (info, 2), ...]

4. ENTROPY CODING (ZSTD / LZ4 / ZLIB)   on top of all of the above
```

The trick is doing 1–3 before 4. A column of timestamps as raw int64 + ZSTD gets 3×; double-delta + ZSTD gets 20×.

### 8.3 What hurts compression

- **Embedded UUIDs and request IDs** in the body string. High entropy, no repetition.
- **Stack traces** — long, but actually compress okay because frames repeat.
- **Base64-encoded binary** in the body. Entropy is high; ratio is ~1.3×.
- **Huge body fields** (>10 KB per line, e.g., dumped HTTP requests). Compression works, but transfer / scan dominates cost.

### 8.4 ZSTD vs LZ4 vs Snappy

| Codec | Compression | Speed (write) | Speed (read) | Use when |
|---|---|---|---|---|
| LZ4 | ~5× | ~500 MB/s | ~1 GB/s | Hot path, low CPU budget |
| Snappy | ~5× | ~500 MB/s | ~1 GB/s | Like LZ4, bit older |
| ZSTD(1) | ~8× | ~400 MB/s | ~700 MB/s | Default for most stores |
| ZSTD(3) | ~10× | ~200 MB/s | ~700 MB/s | Storage tier |
| ZSTD(9) | ~12× | ~50 MB/s | ~600 MB/s | Cold tier, batch |
| ZSTD(19) | ~14× | ~10 MB/s | ~500 MB/s | Archive only |

Most stores default to ZSTD(1) for the hot path and re-encode at a higher level when compacting / merging cold parts.

---

## 9. Retention Tiers and Lifecycle

Log value drops fast. Cost stays flat. The standard tiering:

```
HOT    (0–24 h)   SSD/NVMe       full index, indexed everything    fast queries
WARM   (1–7 d)    SSD or HDD     read-only, force-merged          moderate queries
COLD   (7–90 d)   object storage smaller index, scan-heavy         slow queries
ARCHIVE(>90 d)    Glacier / DR   may need rehydration              rare access
```

### 9.1 ES ILM (Index Lifecycle Management)

```
PUT _ilm/policy/logs
{
  "policy": {
    "phases": {
      "hot":   { "actions": { "rollover": { "max_size":"50GB","max_age":"1d" } } },
      "warm":  { "min_age":"1d",  "actions": { "shrink":{"number_of_shards":1},
                                                "forcemerge":{"max_num_segments":1},
                                                "allocate":{"include":{"data":"warm"}}}},
      "cold":  { "min_age":"7d",  "actions": { "freeze":{}, "searchable_snapshot":{
                                                "snapshot_repository":"s3-archive"}}},
      "delete":{ "min_age":"30d", "actions": { "delete":{} } }
    }
  }
}
```

ILM moves a per-day index through phases automatically. The `searchable_snapshot` action is the key cold-tier feature: the index lives on S3 with only metadata in the cluster, queryable on demand at higher latency.

### 9.2 Loki retention

Loki has no concept of "warm tier" — chunks are on object storage from minute one. Retention is two knobs:

```
limits_config:
  retention_period: 720h        # 30d
table_manager:
  retention_deletes_enabled: true
  retention_period: 720h
```

The compactor walks the chunk store and deletes chunks older than the retention window. Cheap and reliable.

### 9.3 ClickHouse TTL

```
ALTER TABLE logs MODIFY TTL
  timestamp + INTERVAL 1 DAY  TO VOLUME 'ssd_warm',
  timestamp + INTERVAL 7 DAY  TO VOLUME 'cold_s3',
  timestamp + INTERVAL 90 DAY DELETE;
```

Background TTL evaluator moves whole parts; with `s3_disk` the move is a multi-part S3 upload and the local copy is freed. Queries against cold data go through ClickHouse's S3 disk reader transparently — you get higher latency but the same SQL.

### 9.4 Two pipelines, not one

A common mistake is "one log pipeline, 90-day retention for everything". The smarter pattern:

```
Application logs:    7 days hot, 30 days cold        (debugging)
Audit logs:          90 days hot, 7 years archive    (compliance)
Security logs:       30 days hot, 1 year cold        (forensics)
Billing event logs:  Forever, in a transactional DB  (correctness)
```

Don't pay billing-grade retention for application debug logs.

---

## 10. PII, Audit, and Right-to-Erasure

### 10.1 Where to redact

```
APPLICATION → AGENT → AGGREGATOR → STORE
              ↑↑↑    ↑↑↑           ✗ too late, already leaked

REDACT HERE: at the agent or aggregator, before the store.
```

Agent-level redaction (Fluent Bit Lua, Vector VRL, OTel Collector `redaction` processor):

```
attributes:
  actions:
    - key: http.url
      action: update
      pattern: "(\\?|&)(token|api_key)=([^&]+)"
      replacement: "$1$2=REDACTED"
    - key: card_number
      action: hash
```

Once a credit card number lands in an immutable segment / chunk / part, removing it requires **rewriting the whole file**. Cheap to redact at write; catastrophically expensive after.

### 10.2 Audit trails

For SOC2 / HIPAA / ISO 27001, you need an audit log of *who searched for what user_id when*. Every store has the hooks:

- **ES**: audit logging in xpack.security.
- **Loki**: query logging in the frontend.
- **ClickHouse**: `system.query_log` is on by default; tail it to a separate audit index.
- **Splunk**: `index=_audit`.

A common pattern: ship the audit log into a **separate, immutable, longer-retention** store from the application logs, so a compromised admin cannot rewrite their own search history.

### 10.3 GDPR right-to-erasure

When a user requests deletion under GDPR/CCPA, you must remove all events tied to them. Store implications:

| Store | Erasure strategy |
|---|---|
| ES | `_delete_by_query` on `user_id`; expensive — rewrites segments. Better: hash user_id at ingest with a per-user salt, then delete the salt to make user records unrecoverable ("crypto shredding"). |
| Loki | No per-line delete. Either crypto-shredding (don't store user_id; hash it with a salt) or partition by user_id and `delete_chunks` by partition (rare). |
| ClickHouse | `ALTER TABLE ... DELETE WHERE user_id=?` is a *mutation*: rewrites parts, slow. Better: partition by user_id range or use crypto-shredding. |

> **Mental model.** In immutable stores, *delete is always a rewrite*. Plan for erasure at design time: either keep PII out of logs entirely (preferred), or store it in a form that can be invalidated by deleting a key.

---

## 11. Multi-Tenancy Patterns

Every shared platform faces the same five questions:

1. **Authentication boundary.** Who can write? Who can read?
2. **Storage boundary.** Where do tenant A's bytes live, and can tenant B see them?
3. **Quota.** How do I prevent one tenant from consuming all capacity?
4. **Cardinality / cost attribution.** Who is paying for what?
5. **Noisy neighbor.** Can I throttle a tenant whose query is melting the cluster?

### 11.1 Shapes

```
A) Index/database per tenant
   ES: indices logs-tenantA-2026.05.03
   CH: database tenant_a, table logs
   ✓ strong isolation, simple RBAC
   ✗ poor at small tenants (per-index overhead)
   ✗ schema management N times

B) One index, tenant column + RBAC
   ES: doc-level security on tenant_id field
   CH: ROW POLICY logs_tenant USING tenant_id = currentUser()
   ✓ efficient at small tenants
   ✗ cross-tenant blast radius if RBAC is mis-set

C) Header-based tenant (Loki / Mimir / Cortex pattern)
   X-Scope-OrgID: tenantA   → distinct chunk + index space
   ✓ baked into the stack, mature
   ✗ requires a trusted gateway to inject the header
```

The shape that scales is usually **C for hot-path multi-tenant log platforms** and **A for analytic columnar stores serving regulated tenants**.

### 11.2 Per-tenant rate limits

Not optional. Sample knobs:

```
loki limits_config (per tenant):
  ingestion_rate_mb: 50
  ingestion_burst_size_mb: 100
  max_streams_per_user: 5000
  max_query_parallelism: 32
  max_entries_limit_per_query: 5000

clickhouse profiles (per user/tenant):
  max_memory_usage: 10G
  max_execution_time: 60
  max_threads: 8
  max_rows_to_read: 1_000_000_000
```

A tenant exceeding ingest rate is throttled at the distributor. A tenant exceeding query budget is killed. Enforcement is what stops "noisy neighbor" from being an outage.

### 11.3 Cost attribution

Every piece of telemetry should be **auditable to a tenant**. Practical pattern:

- Tag every chunk / part / segment at write time with `tenant_id` (or its slot ID).
- Compute a daily report: bytes ingested, GB stored, queries served, scan-bytes per tenant.
- Show this in a dashboard the tenant owns.

Tenants that *see* their bill self-regulate. Tenants that don't never do.

---

## 12. Cost Model: 1 TB/day Worked Example

Assume:

- 1 TB/day raw JSON, ~500 B/line ⇒ ~2 billion lines/day.
- 30-day retention required, 7 days "hot".
- ~50 % daytime burst factor (peak ingest 2× average).
- Query mix: 80 % "narrow stream, last hour", 20 % "aggregate by attribute, last 24 h".

### 12.1 Elasticsearch / OpenSearch

```
ingest hot     : 1 TB raw → 1.5 TB on-disk (index 0.6× ratio)
ingest CPU     : ~2 vCPU per 100 GB/day  → 20 vCPU sustained
RAM            : 50 % of heap ratio + filter cache → ~40 GB heap × 6 nodes
storage 30d    : 30 × 1.5 TB = 45 TB hot SSD          ($$$$)
warm tier      : forcemerged, ~30 % smaller → ~30 TB
cold tier      : searchable_snapshot on S3 → 10 TB    ($)
total cluster  : 6 hot + 4 warm + 2 master + 2 ingest ≈ 14 nodes
typical bill   : $20–40k/month at hyperscaler list prices
```

### 12.2 Loki

```
ingest         : 1 TB raw → ~80 GB compressed chunks (zstd)
ingester RAM   : depends on streams; assume 100 k streams → 8 GB heap × 3
chunks store   : 30 × 80 GB = 2.4 TB on S3              ($)
index store    : ~2–5 % of chunk size = ~50–120 GB
queriers       : stateless, 4–8 cores ample for the workload
typical bill   : $1.5–4k/month
```

### 12.3 ClickHouse

```
ingest         : 1 TB raw → ~120 GB on-disk (8× ratio)
ingest CPU     : ~1 vCPU per 100 GB/day → ~10 vCPU sustained
RAM            : 16–32 GB per node, 3-node shard for redundancy
storage 30d    : 3.6 TB total, 7d hot SSD = 840 GB, rest on s3_disk
queriers       : same nodes, query throughput ~1 GB/s/node
typical bill   : $2–5k/month self-hosted, ~$3–8k on ClickHouse Cloud
```

### 12.4 The choice

| | Bill | Ad-hoc full-text | Aggregations | Ops complexity |
|---|---|---|---|---|
| ES | 5–10× | Excellent | Good | Medium-high |
| Loki | 1× | OK (slow at scale) | Poor | Low |
| ClickHouse | 1.5× | Good (with ngrambf) | Excellent | Medium |

Most teams that don't have a hard "must search any string in 100 ms" requirement are on Loki or ClickHouse and never look back.

---

## 13. Failure Modes and Operational Pitfalls

The list of mistakes that ship every log architecture into a 3 AM page.

### 13.1 Elasticsearch / OpenSearch

- **Mapping explosion.** App emits 50 k attributes; index has 50 k fields; cluster state goes red. Fix: explicit templates, `dynamic: strict` on logs.
- **Hot shard.** All writes for "today's" index land on one node because routing isn't randomized. Fix: rollover to multiple primary shards; use `index.routing_partition_size`.
- **Translog flushes.** Backed-up translogs ⇒ memory pressure ⇒ GC stalls. Fix: tune `index.translog.flush_threshold_size`; raise heap.
- **Frozen segments.** A node restart with 5,000 segments takes hours. Fix: force-merge before warm.
- **Split-brain on master loss.** Mitigated by ≥3 master-eligible nodes and `discovery.zen.minimum_master_nodes`. Modern versions handle this automatically; older 6.x clusters did not.
- **Query of doom.** `wildcard:*foo*` over 90 days. Kill via `search.allow_expensive_queries: false` and `search.max_buckets`.

### 13.2 Loki

- **Out-of-order writes.** A retried log line with an older timestamp than the chunk's max_ts is rejected. Fix: enable `out_of_order_window` (recent versions) or keep agents NTP-synced.
- **Chunk-too-small thrash.** Many short-lived streams (e.g., 1 line per stream because pod_uid is in labels) ⇒ 100k tiny chunks/min ⇒ object-store metadata blow-up. Fix: drop high-churn labels at the agent.
- **High cardinality 429s.** Adding a new label crosses `max_streams_per_user`; ingestion drops. Fix: alert on `loki_ingester_streams_created_total` slope and on 429 rate.
- **Querier OOM on a 7-day search.** Brute-force scan over many chunks. Fix: `max_query_length`, `max_query_parallelism`, `max_chunks_per_query`.

### 13.3 ClickHouse

- **Bad sort key.** Picking `(timestamp)` only when 99 % of queries filter by service ⇒ full scans. Fix: re-pick sort key, recreate table, backfill (painful but worth it).
- **Mutation backlog.** A `DELETE WHERE` with a non-prefix predicate runs forever; replication lag soars. Fix: prefer TTL on partitions; run mutations in batches.
- **Too many parts.** A small-batch ingest pipeline creates millions of tiny parts; `Merge` task can't keep up; SELECTs slow. Fix: batch via `Buffer` engine or async inserts; raise `parts_to_throw_insert` slowly only after fixing the cause.
- **Distributed lag.** A misconfigured `Distributed` table without `internal_replication=true` can silently dual-write. Fix: explicit `internal_replication`.

### 13.4 General

- **Agent backpressure data loss.** Backend down; agent buffer small; data discarded silently. Fix: 5–15 min disk buffer in Fluent Bit / Vector / OTel Collector + alert on agent dropped-records counter.
- **Time skew.** A laptop's clock is 30 min off; logs land in an unexpected time partition; queries miss them. Fix: trust the collector's `observed_timestamp`, not the app's; reject lines with a clock too far off.
- **PII in `body`.** Once shipped, basically irreversible without rewriting all data. Fix: agent-level redaction; review at PR time.

---

## 14. Decision Tree

```
What's the dominant query?
│
├── "Find this exact string fast across many tenants and a year of data"
│   → Elasticsearch / OpenSearch (or Splunk if you accept the bill)
│
├── "Tail the logs of one app/pod/env for the last hour"
│   → Loki, every time. The Grafana integration is the closer.
│
├── "Aggregate by arbitrary attribute over millions of events"
│   → ClickHouse, BigQuery, Snowflake, or Athena/Iceberg.
│   → If you already have a data warehouse, route logs there too.
│
├── "We're a 5-person startup; we need this fast and small"
│   → A SaaS log vendor (Datadog Logs, Better Stack, Grafana Cloud).
│   Cost-rationalize at scale, not at week one.
│
├── "Strict regulator wants 7 years of audit logs, hard schema"
│   → Columnar (ClickHouse / BigQuery / Snowflake) with row-level security.
│
└── "Mixed (everyone wants everything)"
    → Two pipelines: ClickHouse for events / SQL workloads;
      Loki for live tail / k8s pod logs.
```

The single biggest predictor of a happy log platform is **honest team alignment on the dominant query**. Splitting is cheap and good; trying to make ES be Loki, or Loki be ClickHouse, is the path to misery.

---

## 15. End-to-End: Life of One Log Line

A worked example in the spirit of [ROADMAP §8](./ROADMAP.md#8-end-to-end-trace-of-one-request).

```
T+0       Application code (Go, slog):
            slog.Info("checkout.paid", "user_id", 42, "amount", 12.99,
                      "trace_id", "6f...", "span_id", "a1...")

T+0.05ms  zerolog/slog formats the record as JSON, writes to stdout.

T+0.1ms   Container runtime captures stdout to:
            /var/log/containers/checkout-7c5b-9f2_default_app-fa1d.log

T+1ms     Fluent Bit's tail input picks up the new line:
            - parses JSON
            - enriches with k8s metadata: namespace, pod, node, labels
            - applies filter: `redact http.url ?token=`
            - emits to its forward output (load-balanced)

T+5ms     OTel Collector gateway receives the record on OTLP/gRPC:
            - applies tail-side redaction
            - batches with thousands of others (10 MB / 5 s)
            - duplicates to multiple exporters (Loki + S3 archive)

T+10ms    Per-archetype paths:
            ┌─ Loki ───────────────────────────────────────────────┐
            │ distributor → hash({app=checkout, env=prod, pod=...})│
            │   → quorum write to 3 ingesters (gRPC)              │
            │ ingester appends to in-memory chunk for that stream;│
            │   WAL fsync; ack                                     │
            └──────────────────────────────────────────────────────┘
            ┌─ ClickHouse (via Kafka) ─────────────────────────────┐
            │ collector → Kafka topic logs.raw                     │
            │   (5 MB batches, snappy)                             │
            │ clickhouse-kafka-engine consumer → INSERT INTO logs  │
            │ background merge writes new part to local SSD        │
            └──────────────────────────────────────────────────────┘
            ┌─ Elasticsearch ──────────────────────────────────────┐
            │ collector → ES bulk API (10 MB batches)              │
            │ ingest pipeline applies routing, derives @timestamp  │
            │ primary shard writer:                                │
            │   - tokenize fields, update in-memory term buffer    │
            │   - append to translog, fsync (per-request)          │
            │   - replicate to 1 replica shard, await ack          │
            └──────────────────────────────────────────────────────┘

T+1s      Loki's ingester chunk fills (1.5 MB compressed) → flush
          to S3 + commit to TSDB index.

T+2s      ES refresh triggers: in-memory buffer becomes a new searchable
          segment in the open IndexWriter.

T+3s      ClickHouse merge task picks up the new small parts and
          merges them into a 50 MB part; sort order maintained.

T+5min    Loki compactor compacts hourly chunks for the stream into
          larger zstd-encoded chunks for cheaper future scans.

T+30min   ES segment merge: 5 small segments → 1 medium.

T+1h      A user clicks an exemplar in Grafana for trace_id=6f...
            ┌─ Loki (LogQL) ───────────────────────────────────────┐
            │ {app="checkout"} |= "6f..."                          │
            │ index lookup → ~15 streams                           │
            │ chunk fetch → ~12 chunks from S3                     │
            │ grep within each chunk for "6f..."                   │
            │ stream matching lines back; clickable in Grafana     │
            └──────────────────────────────────────────────────────┘
            ┌─ ClickHouse (SQL) ───────────────────────────────────┐
            │ SELECT * FROM logs WHERE trace_id = unhex('6f...')   │
            │   AND timestamp > now() - INTERVAL 2 HOUR            │
            │ partition prune by hour partition                    │
            │ idx_trace bloom filter prunes granules               │
            │ scan ~5 granules × 8192 rows × 40 columns            │
            │ return ~30 matching rows in <50 ms                   │
            └──────────────────────────────────────────────────────┘
            ┌─ Elasticsearch (KQL) ────────────────────────────────┐
            │ trace_id:"6f..." AND @timestamp > now-2h             │
            │ term lookup in inverted index                        │
            │ shards return top hits + _source fetch               │
            │ aggregations off (filter only) → fast                │
            └──────────────────────────────────────────────────────┘

T+7d      Loki: chunk now beyond hot retention; still on S3, queries cost
          higher latency due to colder S3 cache.
          ClickHouse: TTL moves the part from `ssd_hot` to `cold_s3` volume;
          queries transparently read through s3_disk reader.
          ES: ILM moves index to warm phase; force-merged to one segment.

T+30d     Loki retention deletes the chunk.
          ClickHouse TTL DELETE drops the part.
          ES ILM deletes the index.

The same single log line participated in:
  - one redaction (URL token)
  - one trace correlation join (user clicked exemplar)
  - one operator search (incident triage)
  - one aggregation (daily error breakdown by service)
  - one compliance audit (logged, who searched, when)
  - and one final tombstone.

That is the whole life cycle.
```

---

**TL;DR.** A log line is an event, retained verbatim, joined to traces by `trace_id`. Three production architectures dominate. **Index everything (ES, Splunk)** wins ad-hoc full-text at the cost of $$$. **Index the labels (Loki)** trades search flexibility for 10× cheaper bytes. **Columnar (ClickHouse)** wins SQL aggregations and rides the same retention-tier pattern at a fraction of ES's cost. Across all three, the same five rules apply: redact at the agent, not the store; cardinality of indexed dimensions is the cost driver; tier the storage hot→warm→cold→archive; correlate via `trace_id`; and run two pipelines (one optimized for live tail, one optimized for SQL) before you run one giant unified pipeline.
