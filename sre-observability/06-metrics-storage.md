# 06 — Metrics Storage Internals

> Below the `remote_write` boundary. What a TSDB actually is on disk, why Gorilla compression turns 16 bytes into 1.37, what the postings index does on a label match, and which of {Prometheus, Thanos, Mimir, VictoriaMetrics, M3} survives a 50M-active-series fleet. By the end of this chapter you should be able to size a TSDB cluster from cardinality and scrape interval alone — the rest is operations.

This chapter assumes you've read [03 — Instrumentation](./03-instrumentation.md) (where the samples come from), [04 — Collection & Edge](./04-collection-and-edge.md) (how they reach the TSDB), and the cardinality framing in [ROADMAP §5.1](./ROADMAP.md#51-cardinality--how-many-unique-time-series-exist). The query engine that reads from this storage is [chapter 10](./10-query-layer.md); cardinality as a platform discipline is [chapter 18](./18-cardinality-and-cost.md).

---

## 1. What a TSDB Actually Is

A time series database is a database for exactly one shape of data:

```
series        := (metric_name, label_set)
sample        := (timestamp, value)
time series   := series → ordered_list_of(sample)
```

Every unique `(metric_name, label_set)` combination is **one series**. A series is an *append-only*, *time-ordered* sequence of `(int64 ts, float64 val)` pairs. The whole database is a bag of series, indexed by their labels.

```
http_requests_total{method="GET",  status="200", route="/checkout"} → [(t0,v0), (t1,v1), ...]
http_requests_total{method="GET",  status="500", route="/checkout"} → [(t0,v0), (t1,v1), ...]
http_requests_total{method="POST", status="200", route="/checkout"} → [(t0,v0), (t1,v1), ...]
                            ↑ change ANY label value → it's a different series
```

Three properties of metric workloads dictate every design decision below:

1. **Write-heavy and append-only.** Samples arrive at scrape interval (15s typical), in time order, and they never mutate. Updates and random writes do not exist. A TSDB that pretends to be a B-tree pays the wrong tax.
2. **Range-scan dominated reads.** Queries are almost always "give me all samples for these series in `[t0, t1]`". Point lookups are rare. Scans of tens of thousands of series for tens of thousands of samples each are the hot path.
3. **Cardinality is the only dimension that matters for cost.** Sample volume is bounded by `(cardinality × scrape_freq)`. Active series count drives RAM, index size, query fan-out, and shard count. Bytes per sample are a footnote.

### 1.1 Why a relational DB does not work

Try storing this in PostgreSQL:

```sql
CREATE TABLE samples (
  metric  text,
  labels  jsonb,
  ts      timestamptz,
  value   double precision
);
CREATE INDEX ON samples (metric, ts);
```

The math defeats you immediately. At 5M active series × 1 sample per 15s × 86400 s/day = **28.8 billion rows per day**. With Postgres heap overhead (~24 B per tuple) plus jsonb labels (~100 B), that's >3 TB/day raw. A B-tree on `(metric, ts)` has tree height ~7 at that volume, every insert pays log-N writes, and a range query has to do an index scan plus a heap fetch per row. The same 90-day SLO query that returns in ~200 ms from a TSDB takes minutes.

A TSDB wins by being adversarial-by-design to the relational model:

- **Series IDs replace label tuples.** Once at ingest, the label set is hashed into a 64-bit series ID. All on-disk references use the ID; the labels live once in the index.
- **Columnar per-series chunks.** Samples for one series live contiguously, encoded with delta and XOR tricks (§3) that reach ~1.37 B/sample.
- **Append-only blocks, no in-place update.** Once a chunk is sealed it is immutable. Compaction merges blocks; it never updates samples.
- **Inverted index (postings list) per label.** Match `{a="x", b="y"}` → intersect two precomputed lists of series IDs (§4). No tree traversal, no heap fetch.

### 1.2 TSDB vs columnar OLAP vs log store

The same workload looks different in each:

| Property | TSDB (Prometheus) | Columnar OLAP (ClickHouse, BigQuery) | Log store (Loki, ES) |
|---|---|---|---|
| Primary key | series_id + ts | (sort_key, ts) per part | doc_id |
| Compression unit | per-series chunk (~120 samples) | per-column block (~64k–1M rows) | per-document or per-block |
| Index | postings on labels | sparse primary index, sometimes secondary | inverted (ES) or label-only (Loki) |
| Cardinality | the cost driver | a non-issue; columns are wide-and-short | unbounded; storage scales with bytes, not series |
| Sweet spot | "rate of X over [t0,t1] grouped by Y" | "any aggregation over historical raw events" | "find this string in the last 7 days" |
| Anti-pattern | high-cardinality labels | ad-hoc unindexed column lookups | aggregate over 30 days |

A useful mental rule: **a TSDB is a columnar store in which the column is `(series_id, ts) → value`, the sort key is `series_id`, the secondary key is `ts`, and the dictionary of column names is the postings index**. Everything else about Prometheus follows from that one sentence.

---

## 2. Prometheus On-Disk Format (Single-Node)

The Prometheus TSDB — defined by the `prometheus/tsdb` package and inherited by Mimir, Thanos, Grafana Cloud, and most "Prometheus-compatible" backends — has one of the cleanest on-disk layouts in any database. Read it once and you understand the entire product family.

```
/data/
├── 01HX2QF8XKWVR3T9ZQ5W7H8N2P/        ← block (ULID = creation time, sortable)
│   ├── chunks/
│   │   ├── 000001                     ← chunk file, 512 MB cap
│   │   └── 000002
│   ├── index                          ← postings + series + symbols
│   ├── meta.json                      ← {minTime, maxTime, stats, source, compaction}
│   └── tombstones                     ← deleted-sample ranges (for delete API)
├── 01HX3M0NPK5SD7WQF1AYGT8B4F/        ← another block, immediately after
│   └── ...
├── chunks_head/                       ← memory-mapped chunks of the head block
│   ├── 000001
│   └── 000002
├── wal/                               ← write-ahead log
│   ├── 00001234                       ← segment, 128 MB cap
│   ├── 00001235
│   ├── 00001236
│   └── checkpoint.00001233/           ← rolled-up state of older segments
└── queries.active                     ← in-flight query lock file
```

### 2.1 Blocks

A **block** is a self-contained, immutable, time-bounded slice of the database. The path name is a **ULID** — a 128-bit identifier where the high 48 bits are the millisecond timestamp of creation. ULIDs sort lexicographically by time, which is exactly what `ls /data` gives you for free.

Default block sizes are quantised:

| Stage | Size | Source |
|---|---|---|
| Head | 2h (in-memory + WAL on disk) | active scrapes |
| Persistent | 2h | head block flushed to disk |
| Compacted | 6h (3× 2h), then 24h (4× 6h), then up to 10% of retention | compactor |

Compaction merges adjacent blocks into one — copying chunks, rebuilding the index, dropping tombstoned samples. The block itself is never mutated; the merge writes a new ULID and atomically renames it into place, then deletes the inputs after the next compaction tick.

**Why blocks are immutable.** Three reasons. First, querying across blocks is parallelisable when each is read-only — no locks. Second, replication and backup become trivially simple: a block is a directory; copy it, you're done (this is exactly what Thanos and Mimir do to upload to S3). Third, compaction is a *background* operation that produces a new block from old ones; if the process crashes mid-write, the temp directory is just deleted on restart and the inputs are still intact.

### 2.2 The head block

The head block is **the active 2h window**. It is the only block accepting writes, and it lives mostly in memory:

```
                  HEAD BLOCK (2h window)
   ┌─────────────────────────────────────────────────────┐
   │  in-memory:                                         │
   │    series ─→ stripeLock[hash%64] ─→ memSeries{      │
   │       lset, ref, ┌─ chunk[0] (cold, mmap'd to disk)│
   │                  ├─ chunk[1] (cold)                 │
   │                  └─ chunk[2] (head; in-mem buffer)  │
   │       ...                                           │
   │    }                                                │
   │                                                     │
   │  on disk:                                           │
   │    chunks_head/000001  ← cold chunks mmap'd back    │
   │    wal/0000NNNN        ← every sample, durably     │
   └─────────────────────────────────────────────────────┘
```

A scrape arrives. For each `(metric, labels) → value` pair:

1. **Hash the label set** to look up `memSeries` in a striped map (64 stripes by default — concurrency without one giant lock). New series allocate a fresh `series_id`.
2. **Append to the WAL** — the WAL record is `{series_id, ts, value}` in a length-prefixed binary format.
3. **Append to the in-memory chunk buffer**. When the chunk reaches 120 samples (~30 minutes at 15s scrape) or the 2h window closes, the chunk is sealed, encoded with Gorilla XOR (§3), and **memory-mapped** out to `chunks_head/`. The in-memory pointer becomes a mmap reference; the OS page cache holds it as warm or cold based on access.
4. **Update the postings index for new series**. Existing series do not touch the index.

When the 2h window closes, the head **persists** itself: it writes a new block directory with the chunks already on disk (just renamed), builds the index, writes meta.json, fsyncs, and atomically points itself at a new empty 2h window.

### 2.3 The WAL (write-ahead log)

The WAL is `prometheus/tsdb/wal`, segment-structured (one file per ~128 MB), and it is the *only* durability primitive in Prometheus. Every sample, every series creation, every tombstone is appended to the WAL before it is ack'd. Records:

```
type Record =
   | RecordSeries     {ref, labels}
   | RecordSamples    [{ref, ts, value}]
   | RecordTombstones [{ref, ts_min, ts_max}]
   | RecordExemplars  [{ref, ts, labels, value}]
   | RecordMetadata   {ref, type, unit, help}
```

A WAL segment is closed when full and a new one starts. Old segments are kept for two reasons: crash recovery (replay all segments newer than the last block) and remote-write durability (remote-write reads from the WAL, so it can replay if the receiver was down).

**Checkpointing.** Periodically (default: every 1/3 of `retention`), Prometheus checkpoints the WAL: it walks segments older than the current head's start time, drops samples already persisted to a block, keeps the live series records, and writes a new `checkpoint.<segment>` file. Old WAL segments are then deleted. This bounds WAL growth to roughly `(active_series_count × series_record_size) + (samples_in_head × sample_record_size)`.

### 2.4 Crash recovery

Prometheus crashes (OOM, kernel panic, somebody pulled the cord). On restart:

```
1. Open all blocks under /data — read meta.json, mmap chunks, load index.
2. Scan WAL from the most recent checkpoint forward.
   For each record:
     - Series record  → re-create memSeries with the saved series_id.
     - Sample record  → append to the head's in-memory chunk.
     - Tombstone      → register the deletion.
3. Resume scrapes. Resume remote_write from the offset stored in the WAL.
```

The replay is single-threaded but fast: 1M series + 12 hours of samples replays in 30–60 seconds on modern hardware. The replay is also where most "Prometheus is slow to start" pain comes from — at 50M series the replay is no longer fast, which is one of the reasons large fleets move to Mimir (where ingesters are sharded so each one only replays a fraction of the WAL).

---

## 3. Chunk Encoding: Gorilla / XOR / Double-Delta

The chunk is where TSDBs get their famous compression numbers. Prometheus and the `prometheus/tsdb` lineage use the **Gorilla** scheme from Facebook's 2015 VLDB paper [(Pelkonen et al.)](https://www.vldb.org/pvldb/vol8/p1816-teller.pdf). It compresses a `(timestamp, value)` pair to **~1.37 bytes on average for typical metrics** — versus the 16 bytes a naive encoding would use.

Two tricks, applied independently to timestamps and values.

### 3.1 Timestamp encoding: double-delta

Consecutive scrape timestamps differ by approximately the scrape interval — they are *almost evenly spaced*. Encode the *delta-of-deltas*:

```
ts:    1700000000  1700000015  1700000030  1700000045  1700000061
                    ↑+15        ↑+15        ↑+15        ↑+16
deltas:             15          15          15          16
                    ↑           ↑0          ↑0          ↑+1
deltas-of-deltas:               0           0           1
```

A delta-of-delta of zero is the common case. Encode it in a single bit (`0`). A small DoD encodes in a few bits with a length prefix. Only when the schedule jitters wildly do you spend more bits.

```
DoD encoding (Gorilla):
   '0'                                             → DoD == 0          (1 bit)
   '10'  + 7 bit signed                            → DoD ∈ [-63, 64]   (9 bits)
   '110' + 9 bit signed                            → DoD ∈ [-255, 256] (12 bits)
   '1110' + 12 bit signed                          → DoD ∈ [-2047, 2048] (16 bits)
   '1111' + 32 bit signed                          → anything else     (36 bits)
```

For a perfectly regular 15s scrape, after the first two timestamps every subsequent timestamp encodes in **1 bit**.

### 3.2 Value encoding: XOR

Float64 values change slowly between scrapes (a `cpu_usage` going from `0.34721` to `0.34719` differs in only the last few bits). XOR consecutive values:

```
v_n     = 0x3FD63E5C95DBB1A0
v_{n-1} = 0x3FD63E5C82A52345
xor     = 0x0000000017E2922E5  ← lots of leading and trailing zeros
```

If the XOR is zero, emit a single `0` bit (the value didn't change). Otherwise:

- Count leading zeros and trailing zeros.
- If they fit inside the *previous* XOR's window, emit the meaningful bits with the existing window.
- If not, emit a new window header (5 bits leading zeros + 6 bits length) plus the meaningful bits.

```
XOR encoding (Gorilla):
   '0'                                  → value identical to previous
   '10' + meaningful_bits               → fits previous window
   '11' + 5b lz + 6b len + meaningful   → new window
```

For metrics that drift slowly (utilization, memory, latency averages), the typical XOR is a 1-bit "0" most of the time and a few-bit window otherwise. Counters that increase smoothly (rates) compress similarly because their *deltas* are smooth even when their absolute values are not — though Prometheus does not delta-encode values; it relies on the XOR to find the slow-changing bits.

### 3.3 Why ~1.37 B/sample

On the Facebook Gorilla benchmark across a real production fleet:

| Component | Avg bits per sample |
|---|---|
| Timestamp (after warmup) | ~1.5 |
| Value (after warmup) | ~9.5 |
| Per-chunk overhead amortised | ~0.0–0.5 |
| **Total** | **~11 bits ≈ 1.37 B** |

Numbers vary: regular gauges that move smoothly are ~1.0 B; counters that mostly idle are <1 B; jittery histogram-bucket counters can be 2–3 B. Use 1.4 B/sample as a planning constant; it's right within ±30% for almost any real fleet.

A chunk is **120 samples** by default (~30 min at 15s scrape). At 1.4 B/sample that's ~170 bytes per chunk header-and-all. A series storing 13 months of raw 15s data is ~2.5M samples in ~3.5 MB compressed — the kind of number that keeps query plans tractable.

### 3.4 Histogram chunks

A classic Prometheus histogram is N counter series — one per bucket, plus `_sum` and `_count`. They Gorilla-compress like any other counter. The downside: cardinality. A histogram with 12 `le` buckets across 5 services × 200 routes = 12,000 series for *one* metric. Multiply by every histogram you care about and you understand why histograms are the leading cause of Prometheus death.

Prometheus 2.40+ introduced **native histograms** (also known as "sparse histograms"). One series per histogram instead of N. The chunk encoding adapts to a `{schema, zero_threshold, zero_count, positive_buckets[], negative_buckets[]}` layout:

```
schema = log2(2^(1/4)) → bucket_i covers [2^(i/4), 2^((i+1)/4))
                         ← exponential bucketing, ~4 buckets per power of 2
                            (schema=2; lower schemas mean coarser, higher means finer)

per-sample wire format:
   schema, zero_count, sum, count,
   positive_spans:  [(offset, length), ...]  ← sparse: only non-zero bucket runs
   positive_deltas: [d1, d2, ...]            ← varint deltas of bucket counts
   (negative_spans / negative_deltas similarly)
```

Result: a native histogram series is *one* series, holds ~160 buckets at schema=8 (very fine), and compresses to roughly the same size as a single counter chunk. Cardinality of histograms goes from `N_buckets × N_label_combos` to `N_label_combos`. **Migrate when your SDK and storage support it.** SDK support: Prometheus client_golang ≥1.17, Java client ≥0.16, Python client ≥0.18, OTel SDK with the experimental exponential histogram aggregation (`view.aggregation = ExponentialBucketHistogram`). Storage: Prometheus 2.40+, Mimir 2.10+, VictoriaMetrics 1.96+ (read-only as of Q1 2026), Thanos 0.32+. `histogram_quantile()` learned to operate on native histograms transparently.

---

## 4. The Index: Postings Lists

Without an index, "give me `http_requests_total{status="500", route="/checkout"}`" requires scanning every series in the block. With one, it's two list intersections.

### 4.1 The index file format

The `index` file in each block is a packed binary file with seven sections (Prometheus uses format version 2):

```
┌──────────────────────────────────────────────────────────────┐
│  MAGIC (4 B)  +  VERSION (1 B)                               │
├──────────────────────────────────────────────────────────────┤
│  Symbols                                                     │
│    Deduplicated string table — every label name and value    │
│    appears exactly once. References use varint offsets.      │
├──────────────────────────────────────────────────────────────┤
│  Series                                                      │
│    For each series_id (varint-encoded, monotonic):           │
│      [labels (refs into Symbols), chunks (ts_min, ts_max,    │
│       chunk_file_id, chunk_offset)]                          │
├──────────────────────────────────────────────────────────────┤
│  Label Indices                                               │
│    For each label name → sorted list of value-symbol-refs.   │
│    Used for "show me all values of label X" (UI exploration).│
├──────────────────────────────────────────────────────────────┤
│  Postings                                                    │
│    For each (label_name, label_value): a sorted list of      │
│    series_ids (varint-encoded, delta-encoded).               │
├──────────────────────────────────────────────────────────────┤
│  Label Indices Table                                          │
│    label_name → file offset of its label-indices entry.      │
├──────────────────────────────────────────────────────────────┤
│  Postings Table                                              │
│    (label_name, label_value) → file offset of postings list. │
├──────────────────────────────────────────────────────────────┤
│  TOC (last 6×8 B): offsets of each section above             │
└──────────────────────────────────────────────────────────────┘
```

The file is mmap'd. Reads chase pointers without ever copying — except for the postings list itself, which is decoded to a `[]uint32` (or `uint64`) of series IDs.

**Why series ID is a varint.** IDs are dense and monotonic per block. A series-id list `[42, 43, 47, 48, 49, 102]` delta-encodes to `[42, 1, 4, 1, 1, 53]`, then varint-encodes each. Most deltas fit in 1 byte. A postings list of 100k series IDs is ~120 KB on disk — decoded lazily during query.

### 4.2 Postings list intersection

PromQL's label matchers — `{a="x", b="y"}` — translate directly to a set intersection:

```
match(a="x")                    → postings(a, "x")  = [4, 7, 11, 19, 22, 47]
match(b="y")                    → postings(b, "y")  = [7, 9, 19, 21, 47, 50]
match(a="x", b="y")             → INTERSECT          = [7, 19, 47]
                                                       ↑ resolve to series, fetch chunks
```

The intersection algorithm is **galloping search** (also called "exponential search"): at each step, advance the pointer with smaller current value by powers of 2 until it overshoots, then binary-search the bracket. Cost is `O((m + n) log(min(m,n) / max(m,n)))` rather than `O(m + n)`. When one list has 10 series and the other 10M, you skim the 10M list in ~24 jumps.

Three or more matchers intersect pairwise from smallest list outward — Prometheus reads list sizes from the postings table and orders the intersection accordingly.

### 4.3 Regex matchers

`{route=~"/api/.*"}` cannot use the postings table directly because the value is not a literal. Two cases:

- **Anchored prefix or alternation** (e.g. `=~"foo|bar"` or `=~"/checkout/.*"`): Prometheus optimises by matching the regex against the **label values** of `route` (which live in the symbols table) to build a synthetic union-of-postings list. Cost: linear in the number of distinct values of `route`.
- **Arbitrary regex**: linear scan of every value of `route`, regex-match each, union the postings of matches. With a label that has 10M distinct values (think `pod`), this is the slow query that takes down dashboards.

**Implication.** A regex on a high-cardinality label is the most expensive thing you can put in PromQL. The optimiser cannot help you. Move the dimension out of labels (into trace attributes / log fields) or pre-aggregate it.

---

## 5. Cardinality, the Cost Equation

You will hit a cardinality wall before any other limit. The wall is built into the data model.

### 5.1 The series-count formula

For one metric:

```
series_count(metric) = ∏_{label ∈ metric} |distinct_values(label)|
```

The product, not the sum. A single 8-label metric where each label has 10 distinct values is **100 million** series. Cardinality scales multiplicatively with every label dimension; this is why "let's also break down by user_id" is the most expensive sentence in observability.

Across the fleet:

```
total_active_series = Σ_{metric} ∏_{label} |distinct_values(label, metric)|
```

This is what gets reported in `prometheus_tsdb_head_series` and what every TSDB charges for.

### 5.2 RAM per series

Rule of thumb, single-node Prometheus 2.4x:

| Component | Cost per series |
|---|---|
| `memSeries` struct (label refs, head chunk pointer, ref tracking) | ~250 B |
| Postings entries (in symbols + per-label) | ~150 B avg (label-set dependent) |
| Open head chunk (in-mem buffer) | ~1.5 KB (varies with samples in chunk) |
| WAL replay overhead (transient at startup) | ~0.5 KB |
| Mmap chunk pages (warm portion of the block) | varies |
| **Effective steady-state** | **~3 KiB / active series** |

So **1M active series ≈ 3 GiB RAM** for the head and postings, plus working-set chunk pages, plus query-time temp buffers. A real production Prometheus running 1M active series with active queries and remote_write generally lives between **4 and 6 GiB** resident.

For sizing:

```
RAM_GiB ≈ 3.5 × (active_series / 1e6) + 1   # baseline + overhead
       + query_concurrency × avg_query_resident_set
       + remote_write_shards × shard_capacity × 100 B
```

A 5M-series single-node Prometheus is in the 18–25 GiB range. At 10M you are committing one whole 64–96 GiB box to one Prometheus — past that, sharding or moving to Mimir/VM is cheaper than scaling vertically.

### 5.3 The "labels with too many values" disaster

The classic foot-guns, in order of how often I have personally watched them detonate:

| Label | Why it explodes | Where it belongs |
|---|---|---|
| `user_id` | unbounded, one per customer | log/trace attribute |
| `request_id` / `trace_id` | unbounded, one per request | log/trace attribute |
| Full `url` (including query string) | unbounded | log attribute, or `route` (templated) on the metric |
| `pod` (Kubernetes) | thousands, churns on every deploy | keep only when needed; usually drop |
| `job_id` (Slurm/training) | grows with every job | trace/event attribute |
| `instance_ip` | changes on rescheduling | use stable `node` label instead |
| `error_message` | unbounded free text | log line |
| `client_ip` | one per visitor | aggregate by `/24` or drop |

A bad label rolls out on a Tuesday afternoon. By Friday morning you find Prometheus up against the OOM killer. The only thing that saves you is preflight: any new label dimension goes through a CI check that estimates `count by (label) (...)` against staging data and rejects anything above a per-metric budget. This is [chapter 18](./18-cardinality-and-cost.md)'s discipline.

### 5.4 How to detect leaks

The query toolkit:

```promql
# Total active series in the fleet
prometheus_tsdb_head_series

# Top metrics by series count (the canary)
topk(20, count by (__name__) ({__name__=~".+"}))

# Top label values for a suspect metric
topk(10, count by (pod) (DCGM_FI_PROF_SM_ACTIVE))

# Series growth rate (alert when this is positive sustained)
deriv(prometheus_tsdb_head_series[6h])

# Per-job growth (find which scrape config went bad)
deriv(scrape_series_added[6h])
```

The Prometheus TSDB API at `/api/v1/status/tsdb` exposes a richer breakdown — top metrics, top label names, top label values, top label-value pairs — and is the first place to look when `head_series` is rising. Mimir/Thanos expose the same via `/api/v1/cardinality/labels` and `/api/v1/cardinality/values`.

---

## 6. Why Single-Node Prometheus Stops Being Enough

The three walls you hit in this order:

### 6.1 The HA wall

A single Prometheus is a SPOF. Restart for upgrade and you have a 30–120 s gap in your data. Hardware fails and you have a longer one. Two HA replicas (scraping the same targets, written to the same remote) almost works — but their data is *not byte-identical* (scrapes happen at slightly different times, jitter differs, samples differ), so naïve dedup gives you nonsense rates.

### 6.2 The retention wall

Default retention is 15 days. SLO compliance reports want **28-day rolling** at minimum and ideally **13 months** for year-over-year. Capacity planning wants 12+ months. Local TSDB on Prometheus is fundamentally not a long-term store — extending retention to a year means provisioning a 30 TiB local volume per Prometheus replica and accepting that all queries — even week-old ones — read from local disk.

### 6.3 The horizontal-scale wall

A single Prometheus tops out at roughly:

| Resource | Soft limit | Hard limit |
|---|---|---|
| Active series | ~5M comfortable, ~10M heroic | ~15M with very tuned operations |
| Sample ingest rate | ~500k samples/sec | ~1M with custom tuning |
| Concurrent queries | ~16 nontrivial range queries | ~50 with very simple queries |
| Block compaction time | ~30 min per 24h block at 5M series | crashes the box at ~20M |

Past these you need horizontal sharding. The "easy" scaling answer is **federation** — a top-level Prometheus that pulls aggregated metrics from N lower-tier Prometheuses. Federation is good for *one* thing: cluster-level summary panels. It is **not** a horizontal-scaling architecture:

- Federation only ships data the parent scrapes. Drilldown requires hitting the child, which the dashboard does not know how to do.
- Each federation hop loses resolution unless you ship raw, in which case you've just doubled the storage cost.
- The parent has the same cardinality limits as one Prometheus, so federating 10 instances each with 1M series gives you a 10M-series parent.
- HA is unsolved; federation has the same SPOF as the parent.

If your reach for federation is for "long retention + HA + horizontal scale", you have already chosen the wrong tool. The next four sections are the right tools.

---

## 7. Thanos

Thanos (originally from Improbable, now CNCF) is the lightest-touch path to HA + long retention + horizontal scale. It is a set of stateless components draped over **regular Prometheus instances** plus an object store (S3/GCS/Azure Blob).

```
                       ┌───────────────────────────┐
                       │   Object Store (S3 / GCS) │
                       │  blocks/ulid/...          │
                       │  (compacted, downsampled) │
                       └────▲──────────────▲───────┘
                            │ upload       │ read
                            │              │
   ┌─────────────────┐  ┌───┴────┐    ┌────┴────────┐
   │ Prometheus +    │  │Compactor│    │Store Gateway│
   │ THANOS SIDECAR  │  │24h→14d  │    │ block index │
   │ (gRPC :10901)   │  │downsample│    │ chunk cache │
   └─────────┬───────┘  └────────┘    └────┬────────┘
             │ gRPC StoreAPI                │ gRPC StoreAPI
             ▼                              ▼
   ┌──────────────────────────────────────────────────┐
   │  THANOS QUERIER  (PromQL engine, fan-out)        │
   │  - merges all StoreAPI endpoints                 │
   │  - dedups via __replica__                        │
   │  - splits range by hot (sidecar) / cold (store)  │
   └──────────────────────────────────────────────────┘
                            ▲
                            │ HTTP /api/v1/query
                            │
                       Grafana / Alertmanager
```

### 7.1 The components

| Component | Role | Stateful? |
|---|---|---|
| Sidecar | Runs next to each Prometheus. Uploads sealed 2h blocks to object storage. Serves StoreAPI for the live (in-Prometheus) data window. | No |
| Receiver | Push alternative to sidecar — accepts `remote_write` directly, writes its own TSDB blocks, uploads. | Yes |
| Store Gateway | Fronts the object store. Mmaps block indexes locally, caches chunk slices. Serves StoreAPI. | Disk cache |
| Querier | Stateless. Talks to all StoreAPI endpoints (sidecars + store gateways + receivers). Runs PromQL. | No |
| Compactor | Reads blocks from object store, compacts them (24h, larger), downsamples (5m and 1h tiers), writes back, deletes inputs. **Singleton — only one compactor per tenant.** | No |
| Ruler | Evaluates recording and alerting rules against the Querier. Writes results back as series. | No |

### 7.2 Sidecar mode (pull) vs Receive mode (push)

**Sidecar.** The default. Each Prometheus owns its own scrapes; the sidecar uploads. The Prometheus is still a SPOF for its 2h window — if the box dies, you lose up to 2h of data not yet uploaded. Run **2 Prometheus replicas** scraping the same targets, label them with `__replica__="A"` and `__replica__="B"`, and the Querier dedups at read time. This is the "HA pair" pattern.

**Receive.** Push. Prometheus (or Grafana Agent, or anything that speaks `remote_write`) pushes to a Thanos Receive cluster, which writes its own TSDB blocks and uploads them. Receive is a hash ring of stateful nodes; series are sharded by `(tenant, label_set_hash) mod N`. Use Receive when:

- You have many small Prometheuses (e.g. one per cluster) and don't want one sidecar each.
- You can't run Prometheus where the data is generated (some clouds, some isolated networks).
- You want a centralised write path with proper multi-tenancy.

Use Sidecar when you already have Prometheuses and just want long retention.

### 7.3 The shipper and the bucket layout

The sidecar's "shipper" does this every block:

```
1. Watch /data for new blocks. When a 2h block is fully written and synced:
2. Read meta.json. Tag with external labels (cluster=prod-east, replica=A, ...).
3. Verify block (chunk integrity, index TOC).
4. Upload to object store at <bucket>/<tenant>/<ulid>/{chunks/, index, meta.json}.
5. Mark uploaded locally so it is not re-uploaded.
```

Object store layout is flat: every block is a directory at the bucket root. The compactor and store gateway list the bucket and discover blocks by ULID. `meta.json` carries `compaction.level`, `min_time`, `max_time`, `stats`, `thanos.labels`, and `thanos.downsample.resolution` — the last is `0` for raw, `5m` for the 5-min tier, `1h` for the 1-hour tier.

### 7.4 Store Gateway caching

The store gateway is the most operations-sensitive component. It must answer `series → chunks → samples` for any block in the bucket, but the bucket is S3 and S3 is slow and expensive per request. Two caches:

- **Index cache.** The block's `index` file, mmap'd locally (or held in an in-memory bucket — the "index header" is a compact subset of the index). Decoded postings, label values, series-by-id are cached in memory. Default 1 GiB; in real fleets, 8–32 GiB.
- **Chunks cache.** Recently-read chunks held in Memcached or Redis with a TTL. Default off — turn it on. Cuts S3 GET volume by ~80% for typical dashboard load.

```yaml
# thanos store config (extract)
- key: index-cache-size
  value: 16GB
- key: chunk-pool-size
  value: 4GB
- key: --index-cache.config
  value: |
    type: MEMCACHED
    config:
      addresses: [memcached-0:11211, memcached-1:11211]
      max_async_concurrency: 100
- key: --chunks-cache.config
  value: |
    type: MEMCACHED
    config:
      addresses: [memcached-0:11211, memcached-1:11211]
```

### 7.5 Querier deduplication via `__replica__`

The two-replica HA pattern depends on dedup at read time. Sidecar 1 has external label `replica="A"`, sidecar 2 has `replica="B"`. Querier flag: `--query.replica-label=replica`. At query time:

1. The querier removes `replica` from the matcher.
2. It fetches both replicas' samples for matching series.
3. Per series, per timestamp, it picks one sample. The strategy is "take the first one that arrives in the merged stream"; the choice is deterministic per query.

The hazard: **forget `--query.replica-label` and you get double-counted rates**. `rate()` over 2× the samples produces 2× the result. This is the single most common Thanos misconfiguration in the wild.

### 7.6 Compactor lifecycle and downsampling

The compactor is a long-running batch job. It iterates the bucket and:

```
For each tenant's blocks:
  1. Compact: 6× 2h → 1× 12h, 2× 12h → 1× 24h, etc., up to ~10% of retention.
  2. Downsample: at 40h, build a 5m-resolution variant (blocks with .resolution=5m).
                 at 10d, build a 1h variant.
  3. Delete: blocks past retention.compactor.blocks-retention-period.
```

Downsampled blocks store, for every 5m (or 1h) window per series, the **count, sum, min, max, and the last raw sample**. Critically they preserve the gauge/counter semantics: a 1h downsampled counter still answers `rate()` correctly because the raw samples are what `rate()` derives from, and the downsampled block records `(min_t, max_t, count, sum)` plus the windowed sample. The querier's "auto-downsample" picks the lowest-resolution block that is fine enough for the query's step.

```bash
# thanos compactor flags worth knowing
--retention.resolution-raw=15d            # raw blocks past 15d are deleted
--retention.resolution-5m=90d
--retention.resolution-1h=13mo
--compact.concurrency=4                   # parallel compactions
--downsampling.disable=false              # do downsample (default; disable for Mimir-style)
--deduplication.replica-label=replica     # tell compactor to dedup at compaction-time
```

The **only one compactor per tenant** rule matters. Two compactors racing on the same bucket will both try to write the same compacted block, observe the conflict, and one will lose its work. Lock the compactor to a single replica via leader election (the Thanos compactor exposes a `/-/halt` to gracefully retire) or run it as a singleton StatefulSet.

### 7.7 When Thanos is the right choice

- You already run Prometheus and want long retention + HA without rewriting your collection layer.
- Cardinality fits inside individual Prometheus instances (each <5M active series).
- A small number of tenants (1–~10), or no multi-tenancy concerns.
- You can tolerate the compactor singleton limit.

When it is not: if your *single* fleet exceeds 10M active series, you cannot Thanos your way out — each Prometheus is still capped, and federation across many Prometheuses is operationally painful. That's where Mimir takes over.

---

## 8. Mimir

Mimir (Grafana, originally a fork of Cortex) is the microservices-native, horizontally-scalable Prometheus-compatible TSDB. Where Thanos drapes over Prometheus, Mimir replaces it on the storage side; Prometheus or Grafana Agent or anything `remote_write`-compatible becomes a thin scrape-and-ship layer.

```
   remote_write
       │
       ▼
   ┌────────────────────────────────────────────────────────┐
   │  Distributor (stateless)                               │
   │   - validate samples, enforce per-tenant limits        │
   │   - hash series → ingester ring (consistent hashing)   │
   │   - write to N ingesters (replication = 3 typical)     │
   └────────────────────────┬───────────────────────────────┘
                            │ gRPC push
                ┌───────────┼───────────┐
                ▼           ▼           ▼
   ┌──────────────────────────────────────────────────┐
   │  Ingester (stateful, in hash ring)                │
   │   - in-memory TSDB head (2h)                      │
   │   - WAL on disk                                   │
   │   - flushes 2h blocks to S3                       │
   └────┬───────────────┬───────────────┬─────────────┘
        │ uploaded blocks
        ▼
   ┌────────────────────────────────────────────────────────┐
   │  Object Store (S3/GCS) — TSDB blocks per tenant        │
   └─────▲─────────────────────────────▲────────────────────┘
         │                             │
         │                             │
   ┌─────┴────────┐              ┌─────┴──────────┐
   │ Compactor    │              │ Store Gateway  │
   │ (singleton   │              │ (sharded)      │
   │  per tenant) │              └─────┬──────────┘
   └──────────────┘                    │
                                       │
                                       ▼
                ┌──────────────────────────────────────┐
                │ Querier (stateless)                  │
                │  - reads from ingesters (recent)     │
                │  - reads from store gateway (old)    │
                └─────────────┬────────────────────────┘
                              │
                              ▼
                ┌──────────────────────────────────────┐
                │ Query-Frontend (stateless)           │
                │  - splits ranges by time             │
                │  - shards by series                  │
                │  - caches results                    │
                └─────────────┬────────────────────────┘
                              ▼
                          Grafana / API
```

### 8.1 The hash ring

Series are partitioned across ingesters by **consistent hashing**. The ingester ring is stored in a KV store (etcd, Consul, or memberlist — the latter is the default in modern Mimir). Each ingester owns several "tokens" on the ring. To route a series:

```
hash = mmh3(tenant_id || metric_name || sorted_labels)
ring_position = hash mod 2^32
owner = first ingester whose token >= ring_position
replicas = next (replication_factor - 1) ingesters clockwise
```

`replication_factor` is 3 by default. Every sample is written to 3 ingesters; reads use **quorum** (require 2 of 3 to agree). Quorum reads survive a single ingester failure transparently.

When ingesters scale up or down, a fraction of tokens move to new owners. The new owner reads the relevant series from the old owners (in-memory state transfer) and takes over. This is the "shuffle" event; it is graceful but does temporarily double-store some series. In practice you size for it.

### 8.2 Two-replica write, quorum reads

```
Write: distributor → ingester[A], ingester[B], ingester[C]   (parallel; succeed if 2/3 ack)
Read:  querier     → ingester[A], ingester[B], ingester[C]   (parallel; merge if 2/3 respond)
```

The read path **merges** on `(series_id, ts)`: identical samples deduplicate, and any "missing" sample on one replica is filled from the others. Unlike Thanos's `__replica__` dedup (which assumes two truly-independent replicas with their own WALs), Mimir's replication is *internal* — all three copies are bit-identical because they're written from the same distributor.

### 8.3 Block storage

Ingesters' 2h blocks are uploaded to object storage with the **same TSDB on-disk format as Prometheus** — one of Mimir's most useful properties. The block at `s3://mimir-blocks/tenant=foo/01HX2QF.../{chunks,index,meta.json}` is openable by Prometheus's TSDB code. Compactor and store gateway match Thanos's behaviour: compact 2h → 12h → 24h, optionally downsample, retain per policy.

### 8.4 Multi-tenancy as a first-class concern

Every API call carries `X-Scope-OrgID: <tenant>`. Storage is keyed by tenant. Limits are per-tenant. The ingester ring is single-cluster — but each tenant's series live in tenant-prefixed paths in S3 and tenant-isolated postings inside the ingester. Critically, this is enforced at the **distributor**, not at the storage layer; you cannot make a query that smuggles past the tenant boundary even if you craft labels manually.

### 8.5 Limits config

This is where Mimir earns its operational complexity tax — and where it justifies it. Per-tenant limits are extensive:

```yaml
# mimir.yaml (extract from runtime config / overrides)
overrides:
  payments-team:
    # write path
    ingestion_rate: 250000                  # samples/sec/tenant
    ingestion_burst_size: 500000
    max_global_series_per_user: 5000000     # active series
    max_global_series_per_metric: 200000
    max_global_exemplars_per_user: 100000
    max_label_names_per_series: 30
    max_label_value_length: 2048
    max_metadata_length: 1024
    out_of_order_time_window: 10m

    # query path
    max_query_length: 720h                  # 30 days max range
    max_query_parallelism: 32
    max_samples_per_query: 50000000
    max_fetched_chunks_per_query: 2000000
    query_timeout: 2m

    # compactor
    compactor_blocks_retention_period: 13mo

  search-team:
    ingestion_rate: 80000
    max_global_series_per_user: 2000000
    # ...
```

These limits **save your platform**. A team that was about to ingest 50M series gets a 429, files a ticket, and the conversation that should have happened pre-flight happens. Without per-tenant limits, one team's bug is everyone's outage.

### 8.6 Sharded queries via the query-frontend

A 28-day query over 5M series at 5m step is millions of timestamps × millions of series. The query-frontend splits this:

- **Split by time.** A 28-day query is split into 28 parallel 1-day subqueries. The frontend schedules them across the queriers. Results stitch back together.
- **Split by series.** Some queries (especially `sum by`) are split *across the series space* — querier 1 handles series whose hash is in `[0, 0.5)`, querier 2 handles `[0.5, 1.0)`. The frontend re-aggregates.
- **Result cache.** Range queries are cached in Memcached/Redis keyed by `(query, step, start_aligned_to_step)`. A dashboard panel that loads every 30s benefits from ~99% cache hit on the bulk of the range; only the most recent step is fresh.

The query-frontend is stateless (the cache is external); scale it horizontally without coordination.

### 8.7 Why Mimir scales further than Thanos

Three structural reasons:

1. **Sharded ingesters.** Thanos's "ingest" path is a Prometheus that does not horizontally scale. Mimir's ingester ring spreads writes across N nodes; capacity grows linearly with ingester count.
2. **Sharded compactor (per tenant).** Thanos's compactor is a singleton across the whole bucket. Mimir's compactor is a singleton **per tenant** — at 100 tenants, you have 100 effective compactors running in parallel. (Mimir 2.x added the "split-and-merge" compactor that further shards per-tenant compaction across time-series space.)
3. **Sharded queries.** Thanos's querier fans out per-block; Mimir's frontend fans out per-time-range and per-series-shard, which scales further on cross-time, cross-series queries.

The cost is operational complexity: 6 stateful microservices, a KV store, two object caches, S3 lifecycle policies, per-tenant runtime config. **Run Mimir when** you have 10M+ active series, multi-tenancy is a first-class requirement, or you already operate Grafana Cloud–style infrastructure. **Don't run Mimir when** you have one team and 2M series — Thanos or single-node Prometheus is dramatically cheaper to operate.

---

## 9. VictoriaMetrics

VictoriaMetrics ("VM") is the third major design point. Where Thanos and Mimir use Prometheus's TSDB block format, VM uses a different on-disk format — an LSM-style "mergeset" — and a faster ingest path.

```
   remote_write / scrape
            │
            ▼
   ┌──────────────────────┐
   │   vminsert           │   stateless, hashes series
   │   (HTTP /api/v1/...) │   to vmstorage shard
   └──────────┬───────────┘
              │
   ┌──────────┼───────────────────────────────┐
   ▼          ▼          ▼                    ▼
  vmstorage-0  vmstorage-1  vmstorage-2  ...  vmstorage-N
   - mergeset (LSM-style)                           │
   - per-day partitions (parts/)                    │
   - inverted index (also LSM, separate)            │
                                                    │
              ┌─────────────────────────────────────┘
              ▼
   ┌──────────────────────┐
   │   vmselect           │   stateless query layer
   │   (PromQL/MetricsQL) │
   └──────────────────────┘
```

### 9.1 Mergeset vs TSDB blocks

VM's storage is a sequence of **parts** per day-partition. Each part is roughly:

```
part_NNNN/
  index.bin           ← inverted index for the part
  metaindex.bin       ← per-block metadata
  values.bin          ← compressed values
  timestamps.bin      ← compressed timestamps (delta-of-delta + zstd)
  metaindex_rows.bin
```

New samples arrive in memory, are flushed to small parts on disk, and are **merged** (LSM-style — read all involved parts, write one bigger part, delete inputs) in the background. Compared to Prometheus's 2h-block model:

- Ingest path is shorter (no WAL replay on startup; parts are durable from the moment they hit disk).
- Memory footprint per series is lower because VM uses different in-memory data structures (a single MetricNameID → seriesID mapping table, not the per-series struct Prometheus carries).
- The inverted index is itself LSM-merged; lookups walk all parts until enough postings are gathered.

### 9.2 RAM per series

VM's headline claim: **4–7× less RAM per active series than Prometheus**. In practice, around **0.6–1.0 KiB/series** versus Prometheus's ~3 KiB. Two reasons:

- Less per-series state. VM stores `(metric_id, series_id)` mappings centrally in compact tables, whereas Prometheus's `memSeries` carries label refs, head chunk pointers, and ref tracking per series.
- Different chunk model. VM does not hold an open Gorilla chunk per series in memory; samples buffer into a global ingest buffer that flushes to parts.

A 10M-series VictoriaMetrics single-node fits in ~32 GiB; the equivalent Prometheus needs ~64 GiB and is sweating. This is the single biggest reason teams pick VM for raw cost-per-series.

### 9.3 MetricsQL

VM ships its own query language, **MetricsQL**, which is a strict superset of PromQL. Anything PromQL does, MetricsQL does (the parser is PromQL-compatible). On top, MetricsQL adds:

- `keep_metric_names` modifier on functions that would otherwise drop the metric name.
- Implicit `last_over_time(...[interval])` semantics for instant queries (avoids the "no data in last step" surprise).
- Functions like `top_avg`, `bottom_avg`, `histogram_share`, `range_quantile`, `tlast_change_over_time`, `aggr_over_time` that PromQL lacks.
- `WITH` expressions for query-scoped variable definitions.

Practical implication: PromQL written for Prometheus is portable to VM. VM-specific MetricsQL is **not** portable back. Stick to PromQL if you may ever migrate; use MetricsQL when you've committed.

### 9.4 vmagent

`vmagent` is VM's collection-side companion. It:

- Scrapes Prometheus-format `/metrics` endpoints with the same scrape config syntax as Prometheus.
- Buffers to local disk (its own WAL-equivalent).
- `remote_write`s to vminsert (or to any Prometheus-remote-write endpoint).
- Does relabeling, sample-level filtering, deduping when scraping HA pairs, stream aggregation (§13).

It's a drop-in replacement for "Prometheus run in agent mode" with lower RAM footprint and more aggressive batching. For new deployments going to VM, run `vmagent` instead of Prometheus on the collection side.

### 9.5 Single-node vs cluster

VM has two topologies:

- **Single-node binary.** One process, one disk. Up to ~50M active series on a single big box (256 GiB RAM, NVMe). Operationally simpler than a single Prometheus at the same scale.
- **Cluster (vminsert + vmstorage + vmselect).** Sharded ingest and query. vmstorage holds the data; vminsert and vmselect are stateless. Replication via `-replicationFactor=2` on vminsert (data written to 2 storages).

```bash
# vmstorage retention
./vmstorage-prod -retentionPeriod=13mo \
                 -storageDataPath=/data/vmstorage \
                 -dedup.minScrapeInterval=15s

# vminsert
./vminsert-prod -storageNode=vmstorage-0:8400,vmstorage-1:8400,vmstorage-2:8400 \
                -replicationFactor=2

# vmselect
./vmselect-prod -storageNode=vmstorage-0:8401,vmstorage-1:8401,vmstorage-2:8401
```

### 9.6 When VM beats Mimir, and when it loses

**VM wins when:**

- Cost-per-series matters more than multi-tenancy. Smaller teams, single-tenant fleets.
- You want a simpler operational footprint. Three binaries, no etcd/memberlist drama.
- Ingest performance per CPU matters (LSM merges parallelise well).
- You're willing to commit to MetricsQL extensions for query convenience.

**Mimir wins when:**

- Multi-tenancy with strict per-tenant limits is mandatory (regulated workloads, internal platform serving N teams).
- You want exactly Prometheus semantics on disk (TSDB blocks for portability, future migrations).
- You operate at "Grafana Cloud" scale and want every component independently scalable with proven hash-ring behaviour.
- Your operational team is comfortable with Kubernetes-native StatefulSets and per-component tuning.

There is no "best". Pick the one whose failure modes you'd rather operate.

---

## 10. M3DB (Briefer)

M3DB came out of Uber in 2018 and is the only major TSDB on this list designed bottom-up for the metrics workload (rather than evolving from Prometheus's TSDB). Components:

- **m3coordinator** — stateless ingest API, talks Prometheus remote-write and Graphite.
- **m3dbnode** — the storage node. Custom storage engine (not based on TSDB blocks), uses RocksDB-like LSM for the index.
- **m3aggregator** — stream rollups: pre-aggregate metrics on the write path before they hit storage.
- **m3query** — query engine speaking PromQL and Graphite query.

Cluster topology lives in **etcd**. Sharding is by series hash; replication is configurable per "namespace" (think tenant). Retention and resolution are per-namespace too — you can have raw 7d in one namespace and 1m-aggregated 5y in another, side by side.

The reasons fewer companies pick M3 today are operational, not technical:

- etcd as a hard dependency for cluster topology (Mimir/VM are now both etcd-free in their default modes).
- Tooling is sparser. Less Helm chart maturity, fewer Grafana dashboard recipes, smaller community on Slack/forums.
- Uber's open-source investment slowed after their internal needs were met.

The pattern that is *very much* still alive from M3: **the aggregator**. m3aggregator does stream-time pre-aggregation: incoming samples are bucketed and summed *before* storage, which collapses cardinality at the price of losing the raw. Mimir and VM both have aggregator analogues now (Mimir's `streaming aggregations`, VM's `streamaggr`). When you see the pattern in §13, that's M3's contribution to the field.

---

## 11. Comparison Matrix

| Property | Prometheus (single) | Thanos | Mimir | VictoriaMetrics | Cortex (legacy) | InfluxDB 3 | TimescaleDB |
|---|---|---|---|---|---|---|---|
| **Write model** | scrape (pull) | scrape via Prom | remote_write (push) | remote_write or scrape | remote_write | line protocol push | SQL INSERT |
| **Query language** | PromQL | PromQL | PromQL | PromQL + MetricsQL | PromQL | InfluxQL + Flux + SQL | SQL + extensions |
| **On-disk format** | TSDB blocks | TSDB blocks (in S3) | TSDB blocks (in S3) | mergeset (proprietary) | TSDB blocks (Cortex's older chunk store legacy lingers) | Apache Parquet + IOx | Postgres heap + hypertables |
| **Multi-tenancy** | none | basic (external labels) | first-class, per-tenant limits | basic (per-namespace in cluster mode) | first-class | per-bucket | per-schema |
| **Max practical active series** | ~10M | 2× Prom limit per tenant | 1B+ (sharded) | 100M+ (cluster) | similar to Mimir, but EOL | 10M+ (newer arch) | 10M+ but cost climbs |
| **Cost per series (relative, RAM)** | 1.0 | 1.0 (it's still Prom under the hood) | 0.7 | 0.2–0.3 | 0.7 | 0.4 | 1.5 |
| **Operational complexity** | low | medium | high | medium | high (but EOL) | medium | medium |
| **HA out of box** | no (replicate yourself) | yes (replica labels) | yes (RF=3) | yes (RF=2) | yes | yes | replication |
| **Long retention via S3** | no | yes | yes | optional (`-storageDataPath` on object store) | yes | yes (native) | no (extra tooling) |
| **Vendor / origin** | Prom community | CNCF / Improbable | Grafana | VictoriaMetrics OÜ | Grafana / EOL | InfluxData | Timescale Inc |

Cortex is on this matrix because you will read 2018–2022 blog posts that recommend it. **It is functionally Mimir's predecessor; new deployments should pick Mimir.** Grafana announced Cortex's deprecation in 2022.

InfluxDB 3 (the IOx engine) and TimescaleDB are included as the "outside the Prometheus family" options. InfluxDB excels at IoT-style line-protocol ingest with extreme cardinality (its newer engine is genuinely good); TimescaleDB excels when you want full SQL access including JOINs against business data. Neither is the right choice if your dashboards are PromQL — you'd be running a translation layer forever.

---

## 12. Downsampling and Retention Tiering

Telemetry value drops fast; cost stays flat. The tier that matters for "what was production doing 11 months ago for our annual SLO review" is not the same one that matters for "page me at 3 AM if checkout 99p > 500 ms in the last 5 minutes". Different resolutions, different retentions, different storage media.

```
            ┌──────────────┬──────────────┬──────────────┬──────────────┐
   tier  →  │     HOT      │     WARM     │     COLD     │   ARCHIVE    │
            ├──────────────┼──────────────┼──────────────┼──────────────┤
   res   →  │   raw 15s    │     1m       │     5m       │     1h       │
   keep  →  │   7–15 days  │   30–90 d    │   1–2 years  │    5+ years  │
   media →  │  local SSD   │  local SSD   │  S3 standard │ S3 IA / GLAC │
   used  →  │ alerts, live │ recent SLO,  │ year-on-year │ compliance,  │
            │ debugging    │ capacity     │ trends       │ audit only   │
            └──────────────┴──────────────┴──────────────┴──────────────┘
```

The math (revisited from chapter 18):

```
Raw 15s:   333k samples/sec × 86400 s/day × 1.4 B = ~40 GiB/day
1m:        same series, 4× fewer samples            ~10 GiB/day
5m:        same series, 20× fewer samples           ~2 GiB/day
1h:        same series, 240× fewer samples          ~170 MiB/day
```

A 1h rollup for 13 months costs less than raw for 15 days. A 5m rollup for 90 days costs less than raw for 7 days. **Always run the tiers.**

### 12.1 How the compactor produces them

Thanos and Mimir compactors generate downsampled blocks by reading raw blocks and emitting blocks with `meta.json.thanos.downsample.resolution = 5m` (or `1h`). The downsampled block stores per-series, per-window:

- `count` — number of raw samples in the window
- `sum` — sum of raw sample values in the window
- `min`, `max` — extremes
- the **last raw sample's value and timestamp** at the end of the window

These aggregates let `rate()`, `increase()`, `min_over_time()`, `max_over_time()`, `avg_over_time()`, and `sum_over_time()` produce the same results on downsampled data as on raw data within the window's resolution. Quantiles on histograms downsample correctly because the bucket counters downsample correctly.

### 12.2 Query-time tier selection

The querier auto-picks the lowest-resolution block whose resolution is finer than the query's *step*:

```
Query: rate(http_requests_total[5m]) over [now-30d, now] step=5m
   → resolution needed: 5m
   → use the 5m downsampled blocks (faster, smaller)

Query: rate(http_requests_total[1m]) over [now-1h, now] step=15s
   → resolution needed: 15s
   → use raw blocks
```

You can override with `--max-source-resolution` per query (e.g. force raw for accuracy-critical alerts).

### 12.3 Why downsampling is irreversible — and the recording-rule escape hatch

Once a 5m block is the only thing for a window, the raw is gone. A new question that needs raw resolution on 13-month-old data is unanswerable.

The escape: **recording rules at high resolution before downsampling**. If you know `team:gpu_efficiency:15s` matters historically, compute it as a recording rule at scrape time and let it ride into the cold tier. The recording-rule output is still subject to downsampling, but its inputs were preserved at scrape resolution. This is why the standard pattern (chapter 18) emits a small fleet of pre-aggregated time series at multiple intervals (`:1m`, `:5m`, `:1h`) and ships those to long-term storage rather than the raw.

---

## 13. Recording Rules vs Streaming Aggregation

Both pre-compute aggregations. They differ in where in the pipeline the work happens.

### 13.1 Recording rules

Evaluated by Prometheus (or the Mimir/Thanos ruler) at a fixed interval, **after** ingest:

```yaml
groups:
  - name: http
    interval: 30s
    rules:
      - record: service:http_request_rate:5m
        expr: |
          sum by (service, route, status) (
            rate(http_requests_total[5m])
          )
      - record: service:http_p99:5m
        expr: |
          histogram_quantile(0.99,
            sum by (service, route, le) (
              rate(http_request_duration_seconds_bucket[5m])
            )
          )
```

Properties:

- The raw input series are **already in storage** when the rule runs.
- The output is a regular series — written back to TSDB, queryable, alertable, downsampled, replicated.
- Cardinality of the output is bounded by the rule's `by` clause.
- Complex queries (histograms, joins) are fine — full PromQL is available.

### 13.2 Streaming aggregation

Aggregation on the **write path**, before storage:

```yaml
# vmagent stream aggregation config
- match: 'http_requests_total'
  interval: '60s'
  outputs: ['rate_sum']
  by: ['service', 'route', 'status']
```

(Mimir's equivalent is the `mimirtool config` `-ruler.evaluation-interval` plus the streaming aggregations runtime; M3's is the m3aggregator.) Properties:

- Raw samples are aggregated **before** being written to the TSDB. The raw series can be (and usually is) dropped.
- Cardinality reduction is at write time — the ingester sees only the aggregated series.
- Latency: aggregation window completes before any sample is queryable. A 60s window means you can never query the last 60s of these aggregates.
- Limited expression vocabulary: typically `sum`, `count`, `min`, `max`, `quantiles via histogram` — not full PromQL.

### 13.3 When each is correct

| Need | Use |
|---|---|
| You need both raw and aggregate (drilldown + dashboard) | Recording rules |
| You can never afford to write the raw because cardinality is too high | Streaming aggregation |
| The aggregate must be queried via the same PromQL surface as everything else | Recording rules (output is a series like any other) |
| The aggregate must include complex PromQL (histogram_quantile, joins, label_replace) | Recording rules — streaming aggregators don't speak the full language |
| You need the lowest-possible RAM cost on the storage tier | Streaming aggregation (raw never lands) |
| You're running multi-tenant and one tenant emits 100M raw series you'll never query individually | Streaming aggregation, with raw rejected at the distributor |

The two are not mutually exclusive. A common pattern: **streaming-aggregate the obvious cardinality bombs** (per-pod, per-request-id metrics) at the agent, and **recording-rule everything else** at the ruler. The raw series for the streaming-aggregated ones never reach the TSDB; the raw series for the recording-rule-aggregated ones do, with bounded retention.

---

## 14. Native Histograms (Deeper)

Classic histograms blow up cardinality. Native histograms collapse it. The mechanism deserves its own section because the migration is one of the highest-leverage cardinality interventions a Staff Engineer can drive.

### 14.1 The classic histogram problem

```
http_request_duration_seconds_bucket{service="checkout", route="/api/v2/cart", le="0.005"}
                                    {                                     , le="0.01"}
                                    {                                     , le="0.025"}
                                    {                                     , le="0.05"}
                                    {                                     , le="0.1"}
                                    {                                     , le="0.25"}
                                    {                                     , le="0.5"}
                                    {                                     , le="1"}
                                    {                                     , le="2.5"}
                                    {                                     , le="5"}
                                    {                                     , le="+Inf"}
http_request_duration_seconds_sum   {service="checkout", route="/api/v2/cart"}
http_request_duration_seconds_count {service="checkout", route="/api/v2/cart"}
```

That's **13 series for one (service, route) pair**. With 30 services × 200 routes = 78,000 series for one histogram. With three latency histograms (request, db, downstream) = 234,000 series. This is how a single innocent dashboard becomes 5% of your active series budget.

### 14.2 Native histograms: exponential schema

A native histogram represents the distribution as buckets with **exponentially-spaced boundaries**, characterised by a `schema` parameter:

```
schema = N → bucket boundaries at 2^(k / 2^N) for integer k

schema = 2 → buckets at 2^(k/4) → ~4 buckets per power-of-2 (coarsest)
schema = 4 → buckets at 2^(k/16) → ~16 buckets per power-of-2 (medium)
schema = 8 → buckets at 2^(k/256) → ~256 buckets per power-of-2 (finest)
```

Schema 8 gives ~0.27% relative error on quantile estimates — better than any reasonable hand-picked classic histogram.

### 14.3 The wire format

A single sample of a native histogram carries:

```
{
  schema:           int8                    -4 ≤ schema ≤ 8
  zero_threshold:   float64                 anything within this is the zero-bucket
  zero_count:       uint64                  count in the zero bucket
  count:            uint64                  total samples
  sum:              float64                 total sum
  positive_spans:   [(offset, length)]      sparse run-length: bucket k onward
  positive_deltas:  [int64]                 varint delta-encoded counts
  negative_spans:   [(offset, length)]      same for negative values
  negative_deltas:  [int64]
}
```

The "spans" are compact run-length encoding of which buckets have non-zero counts. A latency histogram with samples mostly between 1ms and 1s touches ~30 buckets at schema=8, encoded as one or two spans plus 30 deltas.

### 14.4 Cardinality collapse

```
Classic:  http_request_duration_seconds_{bucket,sum,count}{service, route, le="..."}
          ⇒ N_le × N_service × N_route series

Native:   http_request_duration_seconds{service, route}
          ⇒ 1 × N_service × N_route series
```

For our example: 234,000 → 18,000 series. **13× cardinality reduction.** Storage size per series is ~3× larger because each sample carries the spans+deltas rather than a single float — net win is roughly 4×.

### 14.5 Quantile accuracy

`histogram_quantile(0.99, rate(http_request_duration_seconds[5m]))` works on native histograms transparently. Internally it picks the bucket containing the 99th-percentile rank and linearly interpolates within it. With schema=8 and ~256 buckets per decade of magnitude, the relative error on any quantile is below 0.3%. Compare with a 12-bucket classic histogram, where p99 might fall in a wide `[1, 2.5]` bucket and the answer is "somewhere between 1 and 2.5 seconds".

### 14.6 SDK and storage support (Q1 2026 snapshot)

| Layer | Native histogram support |
|---|---|
| Prometheus client_golang | ≥ 1.17 (stable) |
| Prometheus client_java | ≥ 0.16 (experimental → stable in 1.x) |
| Prometheus client_python | ≥ 0.18 |
| OpenTelemetry SDKs (most languages) | exponential histogram aggregation, ≥ 1.20 (stable in current OTel) |
| Prometheus server | ≥ 2.40 (read/write/query stable; on-disk format stabilised in 2.50) |
| Mimir | ≥ 2.10 (full support) |
| VictoriaMetrics | ≥ 1.96 read; write support behind feature flag as of recent versions |
| Thanos | ≥ 0.32 |
| Grafana | ≥ 10.4 (auto-detects in panels) |

The migration playbook is in chapter 18 — short version: instrument new metrics as native histograms; emit *both* native and classic for the same metric during a transition window using the SDK's `NativeHistogramBucketFactor=1.1` (or equivalent) and `Buckets=[…]` together; cut over the dashboards; drop the classic series.

---

## 15. Operational Sizing Math

The worked example. A medium fleet, planning for 13-month retention with downsampling tiers.

```
Inputs:
  active_series      = 5,000,000
  scrape_interval    = 15s
  bytes_per_sample   = 1.4 (Gorilla average)
  retention_raw      = 15 days
  retention_5m       = 90 days
  retention_1h       = 13 months

Sample rate:
  samples_per_sec = 5,000,000 / 15  = 333,333  ≈ 333k/sec

Bytes/sec, bytes/day (raw, after Gorilla compression):
  bytes_per_sec  = 333k × 1.4 B    = 466 KB/sec
  bytes_per_day  = 466k × 86400    = 40.3 GB/day raw

Downsampling reduction factors:
  5m  blocks: 1/20  → 2.0 GB/day
  1h  blocks: 1/240 → 170 MB/day

Storage at full retention:
  raw 15d:   40.3 × 15  = 605  GB
  5m 90d:    2.0  × 90  = 180  GB
  1h 13mo:   0.17 × 395 = 67   GB
                  total ≈ 850  GB ≈ 0.85 TB

Add 30% overhead (index, replication factor, S3 list bloat):  ~1.1 TB total

RAM (Prometheus or Mimir ingester per ingester instance):
  3 KiB/series × 5e6 = 14.6 GiB head-state
  + 2 GiB working buffers + remote_write
  ≈ 18–22 GiB per ingester
  → for Mimir RF=3 with 3 ingesters: 3 × 22 = 66 GiB cluster-wide RAM

CPU/query (PromQL on the live tier):
  Simple instant query (rate over 5m, sum by service):
     fan-out to ~20 ingester shards × ~10 ms each
     ~200 ms total wall-time, ~1 vCPU sec total
  Heavy range query (28-day, 5m step, 100 series):
     ~5,000 timestamps × 100 series = 500k samples to fetch
     ~2 vCPU sec, ~2-4 sec wall time with query splitting

Network:
  Ingest:  remote_write at 333k samples/sec × ~6 B/sample on wire (Snappy)
                = 2.0 MB/sec from collectors → distributor
  Replication (RF=3): 3× = 6 MB/sec internally between distributor and ingesters
  Object storage upload: 40 GB/day = ~470 KB/sec sustained
```

These numbers are not aggressive. A 5M-series fleet in Mimir runs comfortably on **3 ingesters × 32 GiB RAM × 8 vCPU**, with **2 querier × 16 GiB**, **2 query-frontend × 4 GiB**, **1 compactor × 16 GiB**, **2 store-gateway × 16 GiB**, plus an S3 bucket and a Memcached cluster. All-in compute footprint roughly 200 vCPU, 250 GiB RAM, 1 TB persistent volumes for ingester WALs, and ~5 TB of S3 (most of it cold). At AWS list price that's ~$3-4k/month — a sub-percent fraction of the application fleet whose metrics it stores.

For VictoriaMetrics single-binary on the same workload: **one box, 16 vCPU, 64 GiB RAM, 2 TB NVMe**, retention managed with `-retentionPeriod=13mo -dedup.minScrapeInterval=15s`. Roughly 1/4 the operational footprint with the operational trade-offs from §9. For Thanos sidecar pattern: **2× Prometheus replicas (each ~24 GiB) + sidecar + object store + Querier + Store + Compactor**, somewhere between Mimir and VM in complexity.

---

## 16. Common Pitfalls

The list of mistakes I have personally watched detonate in production. None of these are subtle once you know them; all of them are non-obvious until they happen to you.

1. **Federation as a scaling tool.** Federation aggregates summaries. It does not horizontally shard your TSDB. The federating Prometheus has the same active-series limit as any other Prometheus — and no HA. Use Thanos/Mimir/VM for horizontal scale; use federation only for cross-region summary panels.
2. **Labels in the metric name.** `http_requests_get_total`, `http_requests_post_total`, `http_requests_put_total` — three metric names that should be one metric with a `method` label. Prometheus loses dimensional reasoning (`sum by (method)`) on these. Always name the metric for *what is being measured*; encode the dimension as a label.
3. **`histogram_quantile(0.99, rate(<recording_rule_p99>[5m]))`.** Some genius decided to make `service:http_p99:5m` a recording rule, then a dashboard does `histogram_quantile(0.99, rate(service:http_p99:5m[5m]))`. This is statistically incoherent — you cannot take a quantile of a quantile. Recording-rule the **bucket counters** (which are linear-aggregable), not the derived quantile.
4. **Forgetting `--query.replica-label` on Thanos.** Two HA replicas, both ingested into the same Querier, no replica label configured → every rate is doubled. Detection: production rates roughly 2× your last sane measurement.
5. **Mixing `summary` and `histogram` for the same metric.** `summary` quantiles cannot be aggregated across instances (they're locally computed); `histogram` quantiles can. Some dashboards mix them. Standardise on histograms (and ideally native histograms).
6. **Tombstones never expiring.** The Prometheus delete API writes tombstones; tombstones live in the block until the block itself ages out. A heavy delete operation can leave tombstones for months. Run `clean_tombstones` API or just wait for retention to drop the block.
7. **OOM from `count by (label)` exploration.** A junior engineer wants to know "what labels does this metric have" and runs `count by (pod) ({__name__=~".+"})`. The cross-product is huge; the querier holds millions of series in memory; OOM. Use `/api/v1/status/tsdb` or `/api/v1/cardinality/values` instead.
8. **`irate` for capacity planning.** `irate` uses only the last two samples of the window. It is the right answer for "did we just spike?" and the wrong answer for "are we trending up?". Use `rate` with a 5m window for trends; reserve `irate` for fast-twitch alerting.
9. **`rate()` over a counter that resets.** Prometheus's `rate` is reset-aware (counter that goes from 1000 to 5 is interpreted as a reset), but only if it sees both samples. Sub-rate-window restarts (counter resets twice within the rate window) lose data. Long-running counters with restart frequencies > rate window interval — or always-incrementing alternatives.
10. **`out_of_order_time_window` ignored.** Prometheus 2.x added support for ingesting out-of-order samples (within a configurable window). If your collection layer reorders (Kafka with multiple partitions, retries with backoff), set `out_of_order_time_window: 10m` or you will silently drop samples.
11. **Compactor running on multiple replicas.** Two compactors racing on the same Thanos bucket or Mimir tenant. Both will write the same compacted block; one loses; you've wasted compute and may corrupt the state. Singleton-deploy the compactor (`replicas: 1` with PDB, or use leader election).
12. **Deleting a series doesn't free RAM until the head rotates.** A bad series removed via relabeling is dropped *on next scrape* — but its `memSeries` and head chunks live until the 2h block rolls. If the cardinality leak filled head, you must restart Prometheus to reclaim RAM. Cleanup via relabel is correct but slow; restart is fast and ugly.
13. **Naming `_total` on a gauge.** Convention: `_total` suffix is reserved for counters. A gauge named `cpu_usage_total` is a lie that confuses every reader and breaks linters. Counters end in `_total`, histograms in `_seconds`/`_bytes`/etc., gauges have no special suffix.
14. **`rate()` of a histogram bucket without `sum by (le, ...)`.** `histogram_quantile` requires the buckets aggregated with `le` preserved. A rule that sums by `service` only and drops `le` produces a degenerate input that quantile interprets as a single bucket, returning meaningless answers.

---

## 17. Glossary / Mental Model Summary

A staff-engineer cheat sheet. Internalise these and you can size, scale, and debug any TSDB in this family.

| Term | Precise meaning |
|---|---|
| **Series** | One unique `(metric_name, label_set)` pairing. The atom of cost. |
| **Sample** | One `(timestamp, value)` pair within a series. ~1.4 B compressed. |
| **Active series** | Series receiving writes in the current head window. The number that drives RAM. |
| **Cardinality** | Number of distinct active series. The product of distinct label-value counts. |
| **Block** | A 2h–24h immutable directory: `chunks/`, `index`, `meta.json`, `tombstones`. |
| **Chunk** | A run of ~120 samples for one series, Gorilla-compressed. |
| **Head block** | The 2h block currently accepting writes. Lives in memory + WAL on disk. |
| **WAL** | Write-ahead log. The only durability primitive. Replayed on startup. |
| **Postings list** | Inverted index: `(label_name, label_value) → [series_id, …]`. Sorted, varint-encoded. |
| **Gorilla** | The 2015 Facebook compression scheme: timestamp double-delta + value XOR. ~1.4 B/sample. |
| **Native histogram** | Single-series exponential-bucket histogram. Replaces N-bucket classic histograms. Schema 2–8. |
| **Recording rule** | A precomputed PromQL expression evaluated periodically; output is a series. |
| **Streaming aggregation** | Pre-aggregation on the write path, before storage. Raw is dropped. |
| **Downsampling** | Aggregating raw samples into 5m or 1h windowed aggregates (count, sum, min, max, last). |
| **Replica label** | An external label distinguishing HA Prometheus replicas; used for read-time dedup. |
| **Hash ring** | Consistent-hash mapping of series → ingester instances; the basis of Mimir/VM cluster mode. |
| **Compactor** | Background process that merges adjacent blocks and produces downsampled blocks. Singleton per tenant. |
| **Object store** | S3/GCS/Azure Blob, where blocks live for long retention. The reason TSDBs scale. |

```
The mental model in five sentences:

A TSDB is an inverted-index plus columnar-chunk storage engine, optimised
for monotonic-time append, range scans, and label-based set intersection.

Samples cost almost nothing (Gorilla makes a sample 1.4 bytes); series cost
~3 KiB of RAM each in Prometheus, ~1 KiB in VictoriaMetrics.

Single-node Prometheus tops out at ~10M active series, 15 days, no HA;
to break each of those walls you need Thanos (long retention via S3),
Mimir (sharding + multi-tenancy), VictoriaMetrics (lower RAM/series),
or M3 (the original aggregator pattern).

Native histograms are the highest-leverage cardinality intervention
available: ~13× series reduction with better quantile accuracy.

Downsampling and recording rules are how you keep 13 months of data without
keeping 13 months of raw samples — but downsampling is irreversible, so
encode the questions you'll ask in a year as recording rules today.
```

---

## TL;DR

A TSDB is an append-only columnar store keyed by `(series_id, ts)` with an inverted postings index over labels; Prometheus's TSDB on disk is 2h immutable blocks of Gorilla-compressed chunks plus a postings index, fronted by a 2h in-memory head block backed by a WAL. Single-node Prometheus dies on three walls — HA, retention, horizontal scale — past which you choose between Thanos (drape over Prometheus + S3, simple), Mimir (sharded microservices with first-class multi-tenancy and per-tenant limits, complex but unbounded), VictoriaMetrics (different storage engine, ~3-5× lower RAM/series, fewer multi-tenancy knobs), or M3 (Uber-origin, etcd-backed, declining mindshare but the source of the streaming-aggregator pattern). Cardinality is the only cost knob that matters; the highest-leverage interventions are native histograms (~13× series reduction), recording rules with bounded `by` clauses, streaming aggregation for the labels you can never afford to ingest raw, and downsampling tiers (raw 15d → 5m 90d → 1h 13mo) that keep retention long while keeping bytes low. Size by the rule `1.4 B/sample × samples/sec` for storage, `3 KiB × active_series` for RAM, and you can sanity-check any vendor quote in five lines of arithmetic.
