# Time-Series Databases in Go: A Staff-Engineer Deep Dive

A from-first-principles tour of how production time-series databases (Prometheus, InfluxDB, VictoriaMetrics, TimescaleDB, M3DB, Mimir, Thanos, Druid, ClickHouse-for-metrics) actually work — and a step-by-step Go implementation of a real, working `MiniTSDB` that demonstrates every core technique. Each section explains the *what*, the *why*, and the *what production does instead of the toy*.

This document is intentionally long. It's meant to be read once cover-to-cover, then used as a reference. Code is presented as a series of buildable Go files; everything fits together.

---

## Table of Contents

1. [What Makes Time Series Different](#1-what-makes-time-series-different)
2. [The Data Model: Series, Labels, Samples, Chunks](#2-the-data-model-series-labels-samples-chunks)
3. [Why a Regular OLTP/OLAP Database Is the Wrong Tool](#3-why-a-regular-oltpolap-database-is-the-wrong-tool)
4. [The Production Architecture Skeleton](#4-the-production-architecture-skeleton)
5. [Gorilla Compression: Why TSDBs Get 1.3 Bytes per Point](#5-gorilla-compression-why-tsdbs-get-13-bytes-per-point)
6. [Inverted Indexes and Posting Lists](#6-inverted-indexes-and-posting-lists)
7. [The Write Path, End to End](#7-the-write-path-end-to-end)
8. [The Read Path, End to End](#8-the-read-path-end-to-end)
9. [Block Lifecycle: Head → WAL → Persistent → Compacted → Retention](#9-block-lifecycle-head--wal--persistent--compacted--retention)
10. [Downsampling and Retention Policies](#10-downsampling-and-retention-policies)
11. [Building MiniTSDB in Go (Step by Step)](#11-building-minitsdb-in-go-step-by-step)
12. [Concurrency in Go: What a TSDB Actually Needs](#12-concurrency-in-go-what-a-tsdb-actually-needs)
13. [Scaling Out: Sharding, Replication, HA](#13-scaling-out-sharding-replication-ha)
14. [Production Systems Compared](#14-production-systems-compared)
15. [Operational Pitfalls Nobody Warns You About](#15-operational-pitfalls-nobody-warns-you-about)
16. [Cheat-Sheet: When to Use What](#16-cheat-sheet-when-to-use-what)

---

## 1. What Makes Time Series Different

A *time series* is a stream of `(timestamp, value)` observations attached to an identity. The identity, in practice, is a set of `key=value` labels:

```
http_requests_total{service="checkout", method="POST", code="500"}
  1716800000  41231
  1716800015  41250
  1716800030  41268
  ...
```

This looks innocuous, but the workload has a fingerprint nothing else in the database world has:

```
TIME-SERIES WORKLOAD FINGERPRINT
=================================

  Writes:                                  Reads:
  ─────────                                ──────
  • 99%+ append-only                       • Almost always range scans
  • Monotonic timestamps (mostly)          • Always filtered by label
  • One sample per series per scrape       • Usually aggregated (sum/avg/rate)
  • Bursty: millions/sec at scrape time    • Usually time-bounded
  • Same labels reappear forever           • "Last 1h" >> "3 months ago"
  • Out-of-order is rare but real          • Hot range = last few minutes

  Cardinality:                             Retention:
  ───────────                              ──────────
  • 10⁵ – 10⁸ unique series                • Old data downsampled, not deleted
  • Labels: ~5–20 per series               • Hot/cold tier obvious
  • New series appear daily (churn)        • TTL is a first-class concept
```

**The killer insight:** every storage decision in a TSDB exists to exploit *one* of these properties. If a property doesn't hold for your data (e.g., you have random updates, or no label dimension), a TSDB is the wrong shape.

### The Three Numbers That Drive Everything

```
THE NUMBERS THAT DECIDE THE DESIGN
===================================

  Samples per second per node    →  10⁶ – 10⁷    (write throughput)
  Active series per node         →  10⁶ – 10⁸    (in-memory index size)
  Bytes per compressed sample    →  1.3 – 2.5    (Gorilla / double-delta)

  Implication:
    100M active series × 16 bytes/series RAM overhead = 1.6 GB just for the index
    1M samples/sec × 1.3 bytes ≈ 1.3 MB/sec ≈ 110 GB/day written compressed
    Same 1M samples/sec uncompressed (16B ts + 8B val) ≈ 24 MB/sec ≈ 2 TB/day
```

The whole architecture — Gorilla compression, posting-list indexes, head blocks, mmap'd persistent blocks, label-set deduplication — exists to *make those three numbers possible on commodity hardware*.

---

## 2. The Data Model: Series, Labels, Samples, Chunks

### The Four Concepts

```
THE DATA MODEL
===============

  Label Set       :=  unordered map[string]string, e.g.
                      {__name__="http_requests_total", code="500", path="/api"}

  Series ID       :=  a 64-bit integer assigned per unique Label Set
                      (Prometheus uses a uint64; InfluxDB calls it a series key)

  Sample          :=  (timestamp int64 ms, value float64)

  Chunk           :=  ~120 samples of one series, compressed with Gorilla
                      (Prometheus picks 120 because XOR float compression converges)
```

A *series* is the (Label Set ↔ Series ID) pair plus an ordered sequence of samples. A *chunk* is the smallest physical unit of compressed storage — typically capped at ~120 samples or ~2 hours, whichever comes first.

```
PHYSICAL LAYOUT OF ONE SERIES OVER TIME
========================================

  series_id=4711  labels={__name__="cpu_user", host="web-3", region="us-east"}

        ┌──────────┬──────────┬──────────┬──────────┬──────────┐
        │ Chunk 0  │ Chunk 1  │ Chunk 2  │ Chunk 3  │  Head    │
        │ t=0..2h  │ t=2..4h  │ t=4..6h  │ t=6..8h  │  RAM     │
        │ on disk  │ on disk  │ on disk  │ on disk  │ writable │
        │ ~1.3 B/s │ ~1.3 B/s │ ~1.3 B/s │ ~1.3 B/s │ append   │
        └──────────┴──────────┴──────────┴──────────┴──────────┘
              mmap'd, immutable, sorted by timestamp        mutable
```

### Why Labels (and Not Wide Tables)

A naive design uses one SQL table per metric: `cpu_user(host, region, ts, value)`. This breaks the moment you want a *new* dimension — you `ALTER TABLE`. The label-set model is **schemaless at the dimension axis** and **strict at the value axis** (always a float64). That trade-off is the right one for ops/observability data.

But labels create *cardinality* — and cardinality is the silent killer:

```
CARDINALITY EXPLOSION
=====================

  Bad label:   user_id           → 50M distinct values
  Bad label:   request_id        → ∞ distinct values
  Bad label:   error_message     → unbounded strings

  Result: 1 metric × 50M user_ids = 50M series → 50M chunks
          = 50M × 16 KB chunk = 800 GB RAM just for active heads
          = OOM kill, then refusal to start
```

Every TSDB has a war story about a developer who put `request_id` in a label and took down monitoring for the company. The mitigation is *cardinality limits* (Prometheus' `sample_limit`, Mimir's per-tenant series limit), but the cultural rule is: **labels are dimensions, not events**.

---

## 3. Why a Regular OLTP/OLAP Database Is the Wrong Tool

People try this. It doesn't work. Here's why:

```
WHY POSTGRES (OLTP) STRUGGLES                  WHY CLICKHOUSE (OLAP) IS OKAY-ISH
==============================                  ==================================

  • B-tree per (series_id, ts)                   • Columnar layout fits time-series
    → 24 B index entry per sample                • Aggressive compression works
    → 10⁶ samples/sec × 24 B = index               • But: no native series index
      explosion                                  • But: posting-list lookups
  • MVCC overhead per row (xmin/xmax)              are reinvented as bitmap
    = ~30 B bookkeeping per                        joins → slower than purpose-built
    8-byte float                                 • OK for batch analytics on metrics,
  • TOAST'd JSONB labels =                         not for "give me the last 5min"
    random reads on the                            at 100k QPS
    hot path                                     • TimescaleDB papers over Postgres
  • No native downsampling                         with hypertables + continuous
  • Vacuum churn destroys                          aggregates — good engineering
    write throughput                               but still pays the row-store tax
```

The same logic explains why even **LSM trees** (RocksDB, Cassandra) aren't ideal: their key is `(series, ts) → value`, which means every write writes a *new key*, paying the per-key overhead. Gorilla compression demands that *many samples share one key prefix* — i.e., per-chunk storage, not per-sample.

---

## 4. The Production Architecture Skeleton

Every production TSDB — including the one we'll build — looks like this:

```
THE UNIVERSAL TSDB SHAPE
=========================

                       Writes (millions/sec)
                              │
                              ▼
        ┌──────────────────────────────────────┐
        │            INGESTION LAYER           │
        │   parse → validate → label hash      │
        └──────────────────────────────────────┘
                              │
                ┌─────────────┴─────────────┐
                ▼                           ▼
        ┌──────────────┐            ┌──────────────┐
        │     WAL      │            │  HEAD BLOCK  │
        │  (durability)│            │  (in-memory) │
        │  fsync'd     │            │  hot chunks  │
        │  append-only │            │  + inverted  │
        │              │            │    index     │
        └──────┬───────┘            └──────┬───────┘
               │                           │
               │  on crash: replay         │ every 2h: flush
               └─────────────┬─────────────┘
                             ▼
                  ┌─────────────────────┐
                  │  PERSISTENT BLOCKS  │
                  │  (mmap'd, immutable)│
                  │  one dir per block: │
                  │   chunks/           │
                  │   index             │
                  │   meta.json         │
                  │   tombstones        │
                  └──────────┬──────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │     COMPACTOR       │
                  │  merge small blocks │
                  │  downsample old data│
                  │  apply tombstones   │
                  └──────────┬──────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │       QUERIER       │
                  │  label match →      │
                  │  posting intersect →│
                  │  chunk decode →     │
                  │  aggregate          │
                  └─────────────────────┘
```

Every component below maps directly onto Prometheus's source layout (`tsdb/head.go`, `tsdb/wal/`, `tsdb/index/`, `tsdb/chunks/`, `tsdb/compact.go`). If you learn this shape, you can read Prometheus's source.

---

## 5. Gorilla Compression: Why TSDBs Get 1.3 Bytes per Point

This is the single most important algorithm in the field. Facebook's Gorilla paper (VLDB 2015) showed that you can compress metric streams from 16 bytes/sample (raw `int64`+`float64`) down to ~1.37 bytes/sample average — a **12× improvement** — with no loss and trivial CPU cost.

It uses two ideas, applied separately to timestamps and values:

### 5.1 Timestamps: Delta-of-Delta Encoding

Scrape intervals are *almost constant*. If you scrape every 15s, the deltas between successive timestamps are all 15000ms. The *delta-of-delta* is 0 almost always.

```
TIMESTAMP STREAM:   t0=1716800000000, t1=1716800015000, t2=1716800030000, t3=1716800045123, ...

Step 1 — Delta:     Δ1 = 15000, Δ2 = 15000, Δ3 = 15123
Step 2 — Delta²:    D2 = 0,     D3 = 123

ENCODING (variable-length):
  Store t0 as full int64        (8 bytes once)
  Store Δ1 as 14-bit signed int (when first sample)
  For each subsequent:
    if D == 0           : write 1 bit  '0'
    elif -63   ≤ D ≤ 64 : write 2 bits '10' + 7 bits
    elif -255  ≤ D ≤ 256: write 3 bits '110' + 9 bits
    elif -2047 ≤ D ≤ 2048:write 4 bits '1110' + 12 bits
    else                : write 4 bits '1111' + 32 bits

For perfectly regular scrapes: 1 bit per timestamp after the first two. 🎉
```

### 5.2 Values: XOR Float Compression

Metric values change slowly (`cpu_user` from 0.31 → 0.32 → 0.34). Successive IEEE-754 floats share most bits.

```
VALUE STREAM:    v0=0.31, v1=0.32, v2=0.34, v3=0.34, ...

For each value:
  xor = current ^ previous
  if xor == 0:
      write 1 bit '0'                              (same value — 1 bit!)
  else:
      lz = leading zeros, tz = trailing zeros
      if previous block of meaningful bits fits:
          write '10' + meaningful_bits
      else:
          write '11' + 5 bits lz + 6 bits length + meaningful_bits

  Idle gauges (CPU usage on a quiet host) → most samples = '0' = 1 bit each.
  Slowly drifting counters → ~10 bits per sample.
```

### 5.3 Combined Result

Real-world Prometheus chunks (120 samples, 2h window):

```
COMPRESSED CHUNK ANATOMY  (typical, ~200 bytes)
================================================

  ┌─────────────────────────────────────────────────┐
  │ header: t0 (8B), v0 (8B), Δ1 (2B)               │
  ├─────────────────────────────────────────────────┤
  │ ts stream:  118 × ~1.2 bits     ≈ 18 bytes      │
  ├─────────────────────────────────────────────────┤
  │ val stream: 118 × ~9 bits       ≈ 133 bytes     │
  ├─────────────────────────────────────────────────┤
  │ trailer: count (2B), CRC (4B)                   │
  └─────────────────────────────────────────────────┘
                 Total: ~175 B for 120 samples
                       = 1.46 B / sample
```

**The catch:** Gorilla is **only append-able**. You can't seek into the middle of a chunk to add a sample, because each sample's encoding depends on the previous bit-aligned state. This is why chunks are *immutable once closed* and the write path always appends to the **head** chunk.

We'll implement Gorilla in §11. It's about 200 lines of Go.

---

## 6. Inverted Indexes and Posting Lists

Queries don't look like "fetch series 4711". They look like:

```
sum by(region) (
  rate(http_requests_total{service="checkout",code=~"5.."}[5m])
)
```

The query planner must, in milliseconds, answer: *"Which series IDs match `service="checkout" AND code=~"5.."`?"*

This is **information retrieval** territory. Borrow from Lucene:

```
INVERTED INDEX (per block)
==========================

  Label name "service":
    "checkout"  →  posting list: [12, 47, 91, 412, 8120, ...]   (sorted series IDs)
    "cart"      →  posting list: [7, 22, 56, 411, ...]
    "search"    →  posting list: [3, 17, 88, 410, ...]

  Label name "code":
    "200"       →  [3, 7, 12, 17, 22, 47, ...]
    "500"       →  [12, 47, 411, 8120, ...]
    "503"       →  [22, 91, 412, ...]

  Query: service="checkout" AND code="500"
   ─────────────────────────────────────────
   Intersect posting lists:
     [12, 47, 91, 412, 8120]  ∩  [12, 47, 411, 8120]
   = [12, 47, 8120]
   → 3 series to scan instead of millions. ✓
```

### 6.1 Posting List Intersection

When both lists are sorted, the merge-intersect is O(n+m) and SIMD-friendly:

```go
// Intersection of two sorted posting lists.
func Intersect(a, b []uint64) []uint64 {
    out := make([]uint64, 0, min(len(a), len(b)))
    i, j := 0, 0
    for i < len(a) && j < len(b) {
        switch {
        case a[i] == b[j]:
            out = append(out, a[i]); i++; j++
        case a[i] < b[j]:
            i++
        default:
            j++
        }
    }
    return out
}
```

Production tricks:
- **Roaring bitmaps** (RoaringBitmap library) for posting lists with skewed distributions — faster intersection by skipping whole 16-bit chunks.
- **Skip lists / FST** for the label dictionary so prefix and regex matches don't scan the whole keyspace.
- **Per-block indexes**: each persistent block has its own self-contained index, so dropping an old block drops its index too. No global re-indexing.

### 6.2 Regex Matchers

`code=~"5.."` is implemented by:
1. Compile the regex to a DFA.
2. Walk the label dictionary, keeping label values the DFA accepts.
3. UNION the posting lists for the accepted values.

For anchored regex like `^web-\d+$` on `host`, the DFA can be intersected with the FST trie of label values directly (Prometheus's `index/postings.go` does this) — no full scan needed.

---

## 7. The Write Path, End to End

Walk one sample from `Push()` to durability:

```
WRITE PATH (single sample)
===========================

   client.Push("http_requests_total{...}", ts=t, v=x)
            │
            ▼
   [1] PARSE & VALIDATE
       - reject NaN unless explicitly allowed
       - check label name regex [a-zA-Z_][a-zA-Z0-9_]*
       - enforce per-tenant cardinality limit
            │
            ▼
   [2] LABEL HASH → SERIES ID
       hash := xxhash(canonical_label_string)
       map[hash] → series_id  (FNV/xxhash, sharded mutex map)
       If miss: assign new ID, write to inverted index
            │
            ▼
   [3] WAL APPEND
       wal_record = { series_id, ts, value }   // 16 bytes + overhead
       append to current WAL segment (append-only file)
       group commit: fsync every 1ms OR every N writes
            │
            ▼
   [4] HEAD CHUNK APPEND
       headChunk[series_id].Append(ts, v)
       Gorilla encoder accumulates bits
       If chunk full (>= 120 samples OR > 2h span):
         - mark immutable
         - alloc new empty chunk
            │
            ▼
   [5] (eventually) FLUSH TO BLOCK
       every 2h (or on shutdown):
         - snapshot all immutable chunks
         - write to new persistent block dir
         - truncate WAL up to that point
```

### 7.1 The WAL Is Not Optional

A common rookie design: "I'll write straight to the head chunk in memory, periodic flush to disk, done." Two months later you hit a kernel panic and lose 2 hours of metrics — including the metrics that would have told you *why* the kernel panicked.

The WAL gives you:
- **Crash recovery**: replay WAL → reconstruct the head exactly.
- **Tail-replication**: a replica reads the WAL of the primary (Prometheus Agent → Remote Write, M3DB peers, Mimir ingester-to-ingester).
- **Backpressure decoupling**: WAL append is sequential & fast; head append can lag without blocking the network.

The cost is **one sequential write per sample**. With group-commit fsync (batching N samples per fsync), this is ~5 µs/sample amortized.

### 7.2 The Head Block Is the Crown Jewel

Everything fast about a TSDB happens here. The head holds:

```
HEAD BLOCK INTERNAL STRUCTURE
==============================

  ┌─────────────────────────────────────────────────────┐
  │  seriesByID    : map[uint64]*memSeries              │
  │                  (sharded across N stripes,         │
  │                   each with its own RWMutex)        │
  │                                                     │
  │  seriesByHash  : map[hash]*memSeries                │
  │                  (label-set → series, for ingest)   │
  │                                                     │
  │  postings      : per-label inverted index (in-mem)  │
  │                                                     │
  │  memSeries struct {                                 │
  │     id          uint64                              │
  │     labels      Labels                              │
  │     headChunk   *gorillaChunk                       │
  │     priorChunks []*gorillaChunk   // closed, immutable │
  │     mut         sync.RWMutex                        │
  │  }                                                  │
  └─────────────────────────────────────────────────────┘
```

The **stripe locking** trick (Prometheus uses 16 stripes by default) means that a global write barely contends. Two writes to different series usually land in different stripes and proceed in parallel.

---

## 8. The Read Path, End to End

```
READ PATH (range query)
========================

   QUERY: rate(http_requests_total{service="checkout",code=~"5.."}[5m])
          time range: [t_start, t_end]
            │
            ▼
   [1] PARSE PROMQL → AST → execution tree
            │
            ▼
   [2] PICK BLOCKS THAT OVERLAP [t_start, t_end]
       (each block's meta.json has minTime/maxTime — cheap)
            │
            ▼
   [3] FOR EACH BLOCK (parallel goroutines):
       a) Use block's inverted index:
            service="checkout"  → posting list P1
            code=~"5.."         → union of "500","503","..."  → posting list P2
            P = P1 ∩ P2
       b) For each series_id in P:
            - locate chunks overlapping [t_start, t_end]
            - mmap and Gorilla-decode each chunk
            - emit samples in [t_start, t_end]
            │
            ▼
   [4] MERGE iterators across blocks (head + persistent)
       Deduplicate (overlap blocks possible during compaction)
            │
            ▼
   [5] EVALUATE OPERATORS
       rate() = (last - first) / window  on each series
       sum by(region) = group + add
            │
            ▼
   [6] RETURN JSON / protobuf
```

### 8.1 Chunk Iterators

Each chunk yields samples through a streaming iterator — never materializing the whole chunk decoded. This bounds memory at one chunk's worth of decoded floats per series, per concurrent query.

```go
type Iterator interface {
    Next() bool
    At()  (ts int64, v float64)
    Err() error
}
```

This is the same shape as Go's `bufio.Scanner` and Java's `Iterator<Sample>`. It is the right abstraction because:
- The optimizer can push down `Seek(t)` so a 5-minute query touches ~3 decoded samples even if the chunk has 120.
- Aggregation operators chain iterators without buffering.
- Backpressure is implicit: slow clients slow decoding.

---

## 9. Block Lifecycle: Head → WAL → Persistent → Compacted → Retention

This diagram is worth memorizing — it's the TSDB equivalent of the LSM-tree compaction diagram:

```
BLOCK LIFECYCLE  (Prometheus default timings)
==============================================

       t=0h       t=2h       t=4h       t=6h       t=8h     ...
        │          │          │          │          │
        ├──────────┼──────────┼──────────┼──────────┼─→
        │ Head     │ Head     │ Head     │ Head     │
        │ (RAM)    │ flushes  │ flushes  │ flushes  │
        │          │ → Block₀ │ → Block₁ │ → Block₂ │
        ▼          ▼          ▼          ▼
       WAL ──────► truncate   truncate   truncate
                                  │
                                  ▼
                          ┌───────────────────────────┐
                          │ Block₀+₁ merged → Block₀₁ │
                          │ (compaction: 2 → 1 file)  │
                          └───────────────────────────┘
                                          │
                                          ▼
                              merged again at 6h, 18h, …
                                          │
                                          ▼
                                  > retention period
                                          │
                                          ▼
                                       DELETED
                                  (entire dir rm'd)
```

Why these timings?
- **2h head**: matches a Gorilla chunk's natural fill time at 15s scrape intervals (~480 samples).
- **Power-of-two compaction**: 2h → 6h → 18h → 54h. Bounds the number of files queries must touch.
- **Disk delete = directory delete**: retention is `rm -rf blockdir` — no row-by-row deletion. This is *huge* for I/O.

---

## 10. Downsampling and Retention Policies

Nobody queries 15-second samples from 6 months ago. You query "monthly p95 of API latency for the last year."

**Downsampling**: precompute aggregates at coarser intervals and store them as new series.

```
TIERED STORAGE
===============

  Raw (15s):       last 15 days            ────── high resolution, expensive
  5-min agg:       last 90 days            ────── medium, ~50× cheaper
  1-hour agg:      last 2 years            ────── low, ~150× cheaper

  Each tier:
    - sum, count, min, max, sum_of_squares  → can compute avg, stddev, rate
    - p50, p95, p99 if histograms used (t-digest, DDSketch)
```

Prometheus does **not** downsample natively — Thanos and Mimir bolt it on. InfluxDB has continuous queries. TimescaleDB has continuous aggregates. The pattern is identical: a background job reads recent raw data, computes aggregates, writes them as new series with a special label like `__rollup__="5m"`.

---

## 11. Building MiniTSDB in Go (Step by Step)

Now we build it. ~600 lines of Go, all the core ideas. You can copy these into a directory `minitsdb/` and `go build`. We'll skip a few production niceties (the WAL is simplified, no mmap, no per-block index file format) but every concept is here.

### 11.1 Project Layout

```
minitsdb/
├── go.mod
├── cmd/
│   └── minitsdbd/main.go         # server entrypoint
└── pkg/
    ├── labels/labels.go           # Labels type + hashing
    ├── chunk/gorilla.go           # Gorilla compression
    ├── chunk/bitstream.go         # bit-level I/O for Gorilla
    ├── head/head.go               # in-memory head block
    ├── index/postings.go          # inverted index
    ├── wal/wal.go                 # append-only write-ahead log
    ├── block/block.go             # persistent block read/write
    ├── db/db.go                   # the public DB
    └── query/engine.go            # range queries + aggregation
```

### 11.2 `labels/labels.go` — The Identity Type

```go
package labels

import (
    "sort"
    "strings"

    "github.com/cespare/xxhash/v2"
)

type Label struct{ Name, Value string }

type Labels []Label

// New returns a canonicalized (sorted, deduped) Labels.
func New(ls ...Label) Labels {
    out := append(Labels(nil), ls...)
    sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
    return out
}

// Hash returns a stable 64-bit fingerprint of the label set.
// Two label sets with identical (name,value) pairs have identical hashes.
func (ls Labels) Hash() uint64 {
    h := xxhash.New()
    for _, l := range ls {
        h.WriteString(l.Name)
        h.WriteByte(0xff)               // delimiter must not appear in names
        h.WriteString(l.Value)
        h.WriteByte(0xfe)
    }
    return h.Sum64()
}

func (ls Labels) Get(name string) string {
    // Linear search is fine; len(ls) is typically < 20.
    for _, l := range ls {
        if l.Name == name {
            return l.Value
        }
    }
    return ""
}

func (ls Labels) String() string {
    var sb strings.Builder
    sb.WriteByte('{')
    for i, l := range ls {
        if i > 0 {
            sb.WriteByte(',')
        }
        sb.WriteString(l.Name)
        sb.WriteByte('=')
        sb.WriteByte('"')
        sb.WriteString(l.Value)
        sb.WriteByte('"')
    }
    sb.WriteByte('}')
    return sb.String()
}
```

**Why this is interesting:**
- `Hash()` defines *series identity*. Get it wrong (e.g., depend on map iteration order) and you create duplicate series silently.
- Sorting at construction time means hashing is O(n) instead of O(n log n) per lookup.
- The `0xff` / `0xfe` delimiters are forbidden in UTF-8, preventing collisions from values like `foo=bar` vs `foob=ar`.

### 11.3 `chunk/bitstream.go` — Bit-level I/O

```go
package chunk

// BitWriter packs bits MSB-first into a growing byte slice.
type BitWriter struct {
    buf     []byte
    count   uint8 // bits used in last byte (0..7)
}

func NewBitWriter() *BitWriter { return &BitWriter{} }

func (bw *BitWriter) WriteBit(b bool) {
    if bw.count == 0 {
        bw.buf = append(bw.buf, 0)
    }
    if b {
        bw.buf[len(bw.buf)-1] |= 1 << (7 - bw.count)
    }
    bw.count = (bw.count + 1) & 7
}

func (bw *BitWriter) WriteBits(u uint64, nbits int) {
    for i := nbits - 1; i >= 0; i-- {
        bw.WriteBit((u>>uint(i))&1 == 1)
    }
}

func (bw *BitWriter) Bytes() []byte { return bw.buf }

// BitReader is the dual.
type BitReader struct {
    buf   []byte
    bytei int
    biti  uint8
}

func NewBitReader(b []byte) *BitReader { return &BitReader{buf: b} }

func (br *BitReader) ReadBit() (bool, bool) {
    if br.bytei >= len(br.buf) {
        return false, false
    }
    bit := br.buf[br.bytei]&(1<<(7-br.biti)) != 0
    br.biti++
    if br.biti == 8 {
        br.biti = 0
        br.bytei++
    }
    return bit, true
}

func (br *BitReader) ReadBits(nbits int) (uint64, bool) {
    var u uint64
    for i := 0; i < nbits; i++ {
        b, ok := br.ReadBit()
        if !ok {
            return 0, false
        }
        u = (u << 1) | boolToUint64(b)
    }
    return u, true
}

func boolToUint64(b bool) uint64 {
    if b {
        return 1
    }
    return 0
}
```

**Production note:** Real implementations preallocate buffers, use `math/bits.LeadingZeros64` for branchless leading-zero counts, and inline the hot path. Prometheus's `tsdb/chunkenc/bstream.go` is worth reading; it's about 5× faster than the version above.

### 11.4 `chunk/gorilla.go` — Gorilla Encoder/Decoder

```go
package chunk

import (
    "encoding/binary"
    "math"
    "math/bits"
)

// GorillaChunk holds a stream of (ts, val) compressed Gorilla-style.
// First sample is uncompressed; subsequent samples use delta-of-delta for
// timestamps and XOR for values.
type GorillaChunk struct {
    bw         *BitWriter
    n          uint16
    t0, tPrev  int64
    deltaPrev  int64
    vPrev      uint64 // bit pattern of last float
    leading    uint8
    trailing   uint8
}

func NewGorillaChunk() *GorillaChunk {
    return &GorillaChunk{bw: NewBitWriter(), leading: 0xff}
}

func (c *GorillaChunk) Append(ts int64, v float64) {
    vbits := math.Float64bits(v)
    if c.n == 0 {
        // Header: full t0 and v0
        var buf [16]byte
        binary.BigEndian.PutUint64(buf[0:8], uint64(ts))
        binary.BigEndian.PutUint64(buf[8:16], vbits)
        for _, b := range buf {
            c.bw.WriteBits(uint64(b), 8)
        }
        c.t0 = ts
        c.tPrev = ts
        c.vPrev = vbits
        c.n = 1
        return
    }
    if c.n == 1 {
        delta := ts - c.tPrev
        c.bw.WriteBits(uint64(delta)&((1<<14)-1), 14) // 14-bit first delta
        c.deltaPrev = delta
        c.tPrev = ts
        c.writeValueXOR(vbits)
        c.n++
        return
    }
    // Steady state: delta-of-delta
    delta := ts - c.tPrev
    dod := delta - c.deltaPrev
    switch {
    case dod == 0:
        c.bw.WriteBit(false) // '0'
    case dod >= -63 && dod <= 64:
        c.bw.WriteBits(0b10, 2)
        c.bw.WriteBits(uint64(dod)&0x7f, 7)
    case dod >= -255 && dod <= 256:
        c.bw.WriteBits(0b110, 3)
        c.bw.WriteBits(uint64(dod)&0x1ff, 9)
    case dod >= -2047 && dod <= 2048:
        c.bw.WriteBits(0b1110, 4)
        c.bw.WriteBits(uint64(dod)&0xfff, 12)
    default:
        c.bw.WriteBits(0b1111, 4)
        c.bw.WriteBits(uint64(dod)&0xffffffff, 32)
    }
    c.deltaPrev = delta
    c.tPrev = ts
    c.writeValueXOR(vbits)
    c.n++
}

func (c *GorillaChunk) writeValueXOR(vbits uint64) {
    xor := c.vPrev ^ vbits
    if xor == 0 {
        c.bw.WriteBit(false) // '0' — same value
        return
    }
    c.bw.WriteBit(true) // '1'
    lz := uint8(bits.LeadingZeros64(xor))
    tz := uint8(bits.TrailingZeros64(xor))
    if c.leading != 0xff && lz >= c.leading && tz >= c.trailing {
        // Reuse previous block
        c.bw.WriteBit(false) // '10' (we already wrote first '1')
        bitsInBlock := 64 - c.leading - c.trailing
        c.bw.WriteBits(xor>>c.trailing, int(bitsInBlock))
    } else {
        c.bw.WriteBit(true) // '11'
        c.bw.WriteBits(uint64(lz), 5)
        bitsInBlock := 64 - lz - tz
        c.bw.WriteBits(uint64(bitsInBlock), 6)
        c.bw.WriteBits(xor>>tz, int(bitsInBlock))
        c.leading = lz
        c.trailing = tz
    }
    c.vPrev = vbits
}

func (c *GorillaChunk) NumSamples() int { return int(c.n) }
func (c *GorillaChunk) Bytes() []byte   { return c.bw.Bytes() }

// Iterator returns a streaming iterator over the chunk.
func (c *GorillaChunk) Iterator() *GorillaIterator {
    return &GorillaIterator{br: NewBitReader(c.bw.Bytes()), total: c.n}
}

type GorillaIterator struct {
    br        *BitReader
    total     uint16
    i         uint16
    t, tPrev  int64
    deltaPrev int64
    v         float64
    vPrev     uint64
    leading   uint8
    trailing  uint8
    err       error
}

func (it *GorillaIterator) Next() bool {
    if it.i >= it.total {
        return false
    }
    if it.i == 0 {
        var hdr [16]byte
        for k := 0; k < 16; k++ {
            b, ok := it.br.ReadBits(8)
            if !ok {
                return false
            }
            hdr[k] = byte(b)
        }
        it.t = int64(binary.BigEndian.Uint64(hdr[0:8]))
        it.vPrev = binary.BigEndian.Uint64(hdr[8:16])
        it.v = math.Float64frombits(it.vPrev)
        it.tPrev = it.t
        it.leading = 0xff
        it.i++
        return true
    }
    if it.i == 1 {
        d, ok := it.br.ReadBits(14)
        if !ok {
            return false
        }
        delta := int64(d)
        it.t = it.tPrev + delta
        it.deltaPrev = delta
        it.tPrev = it.t
        if !it.readValueXOR() {
            return false
        }
        it.i++
        return true
    }
    // Delta-of-delta
    b0, ok := it.br.ReadBit()
    if !ok {
        return false
    }
    var dod int64
    if !b0 {
        dod = 0
    } else {
        b1, _ := it.br.ReadBit()
        if !b1 {
            u, _ := it.br.ReadBits(7)
            dod = signExtend(u, 7)
        } else {
            b2, _ := it.br.ReadBit()
            if !b2 {
                u, _ := it.br.ReadBits(9)
                dod = signExtend(u, 9)
            } else {
                b3, _ := it.br.ReadBit()
                if !b3 {
                    u, _ := it.br.ReadBits(12)
                    dod = signExtend(u, 12)
                } else {
                    u, _ := it.br.ReadBits(32)
                    dod = signExtend(u, 32)
                }
            }
        }
    }
    delta := it.deltaPrev + dod
    it.t = it.tPrev + delta
    it.deltaPrev = delta
    it.tPrev = it.t
    if !it.readValueXOR() {
        return false
    }
    it.i++
    return true
}

func (it *GorillaIterator) readValueXOR() bool {
    b, ok := it.br.ReadBit()
    if !ok {
        return false
    }
    if !b {
        // value unchanged
        it.v = math.Float64frombits(it.vPrev)
        return true
    }
    b2, _ := it.br.ReadBit()
    var xor uint64
    if !b2 {
        // reuse previous lz/tz
        bitsInBlock := 64 - it.leading - it.trailing
        m, _ := it.br.ReadBits(int(bitsInBlock))
        xor = m << it.trailing
    } else {
        lz, _ := it.br.ReadBits(5)
        sz, _ := it.br.ReadBits(6)
        m, _ := it.br.ReadBits(int(sz))
        it.leading = uint8(lz)
        it.trailing = 64 - it.leading - uint8(sz)
        xor = m << it.trailing
    }
    it.vPrev = it.vPrev ^ xor
    it.v = math.Float64frombits(it.vPrev)
    return true
}

func (it *GorillaIterator) At() (int64, float64) { return it.t, it.v }

func signExtend(u uint64, nbits int) int64 {
    sign := uint64(1) << (nbits - 1)
    if u&sign != 0 {
        return int64(u | ^((uint64(1) << nbits) - 1))
    }
    return int64(u)
}
```

**This is the heart of the database.** Read it twice. The asymmetry between encoder and decoder is real: the encoder knows the future state machine; the decoder must reconstruct it. Bugs here are silent — wrong values, not crashes.

### 11.5 `index/postings.go` — Inverted Index

```go
package index

import (
    "sort"
    "sync"
)

// Postings is a sorted slice of series IDs.
type Postings []uint64

// Index maps label name → label value → posting list.
type Index struct {
    mu sync.RWMutex
    m  map[string]map[string]Postings
}

func NewIndex() *Index {
    return &Index{m: make(map[string]map[string]Postings)}
}

// Add registers a series in the index under all its labels.
func (idx *Index) Add(seriesID uint64, labelPairs [][2]string) {
    idx.mu.Lock()
    defer idx.mu.Unlock()
    for _, kv := range labelPairs {
        vals, ok := idx.m[kv[0]]
        if !ok {
            vals = make(map[string]Postings)
            idx.m[kv[0]] = vals
        }
        p := vals[kv[1]]
        // Insert keeping sorted order.
        i := sort.Search(len(p), func(i int) bool { return p[i] >= seriesID })
        if i < len(p) && p[i] == seriesID {
            continue
        }
        p = append(p, 0)
        copy(p[i+1:], p[i:])
        p[i] = seriesID
        vals[kv[1]] = p
    }
}

// Get returns the posting list for one (name,value) pair. The returned
// slice MUST NOT be mutated by callers.
func (idx *Index) Get(name, value string) Postings {
    idx.mu.RLock()
    defer idx.mu.RUnlock()
    vals, ok := idx.m[name]
    if !ok {
        return nil
    }
    return vals[value]
}

// Intersect computes the AND of two sorted posting lists.
func Intersect(a, b Postings) Postings {
    out := make(Postings, 0, min(len(a), len(b)))
    i, j := 0, 0
    for i < len(a) && j < len(b) {
        switch {
        case a[i] == b[j]:
            out = append(out, a[i])
            i++
            j++
        case a[i] < b[j]:
            i++
        default:
            j++
        }
    }
    return out
}

func min(a, b int) int { if a < b { return a }; return b }
```

**Production differences:**
- Roaring bitmaps replace `[]uint64` for memory and intersection speed.
- The index is built once per block then frozen (`tsdb/index/index.go` writes a self-contained file with FST + posting offsets), not mutated in place. Our head version *is* mutable because the head block is mutable; we'd freeze a copy at flush time.

### 11.6 `wal/wal.go` — A Minimal Write-Ahead Log

```go
package wal

import (
    "bufio"
    "encoding/binary"
    "io"
    "os"
    "sync"
)

// Record:   [u64 seriesID][i64 ts][f64 val]    24 bytes
const recordSize = 24

type WAL struct {
    mu  sync.Mutex
    f   *os.File
    bw  *bufio.Writer
}

func Open(path string) (*WAL, error) {
    f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_RDWR, 0644)
    if err != nil {
        return nil, err
    }
    return &WAL{f: f, bw: bufio.NewWriterSize(f, 1<<20)}, nil
}

func (w *WAL) Append(seriesID uint64, ts int64, val float64) error {
    var rec [recordSize]byte
    binary.LittleEndian.PutUint64(rec[0:8], seriesID)
    binary.LittleEndian.PutUint64(rec[8:16], uint64(ts))
    binary.LittleEndian.PutUint64(rec[16:24], math.Float64bits(val))
    w.mu.Lock()
    defer w.mu.Unlock()
    _, err := w.bw.Write(rec[:])
    return err
}

// Sync flushes buffered writes and fsyncs the file. Call periodically
// (every 1ms or every N records) for group-commit durability.
func (w *WAL) Sync() error {
    w.mu.Lock()
    defer w.mu.Unlock()
    if err := w.bw.Flush(); err != nil {
        return err
    }
    return w.f.Sync()
}

// Replay reads all records and invokes fn for each.
func (w *WAL) Replay(fn func(seriesID uint64, ts int64, val float64) error) error {
    if _, err := w.f.Seek(0, io.SeekStart); err != nil {
        return err
    }
    br := bufio.NewReader(w.f)
    var rec [recordSize]byte
    for {
        _, err := io.ReadFull(br, rec[:])
        if err == io.EOF {
            return nil
        }
        if err != nil {
            return err
        }
        sid := binary.LittleEndian.Uint64(rec[0:8])
        ts := int64(binary.LittleEndian.Uint64(rec[8:16]))
        val := math.Float64frombits(binary.LittleEndian.Uint64(rec[16:24]))
        if err := fn(sid, ts, val); err != nil {
            return err
        }
    }
}

func (w *WAL) Close() error {
    if err := w.Sync(); err != nil {
        return err
    }
    return w.f.Close()
}
```

**What we're skipping vs production:**
- **Segments**: real WALs split into 128 MB segments so old ones can be deleted after flush.
- **CRC32 per record**: detect partial torn writes (mandatory for correctness on power loss).
- **Length-prefixed framing**: variable-size records (label-set registration vs. sample) need framing. Prometheus uses a snappy-compressed framed format.

### 11.7 `head/head.go` — The In-Memory Hot Set

```go
package head

import (
    "sync"
    "sync/atomic"

    "minitsdb/pkg/chunk"
    "minitsdb/pkg/index"
    "minitsdb/pkg/labels"
)

const (
    stripeCount       = 16
    chunkMaxSamples   = 120
)

type memSeries struct {
    mu        sync.RWMutex
    id        uint64
    labels    labels.Labels
    head      *chunk.GorillaChunk
    closed    []*chunk.GorillaChunk
    minT, maxT int64
}

func (s *memSeries) append(ts int64, v float64) {
    s.mu.Lock()
    defer s.mu.Unlock()
    if s.head == nil || s.head.NumSamples() >= chunkMaxSamples {
        if s.head != nil {
            s.closed = append(s.closed, s.head)
        }
        s.head = chunk.NewGorillaChunk()
    }
    s.head.Append(ts, v)
    if s.minT == 0 || ts < s.minT {
        s.minT = ts
    }
    if ts > s.maxT {
        s.maxT = ts
    }
}

// Head is the writable, in-memory portion of the database.
type Head struct {
    nextID uint64

    stripes [stripeCount]struct {
        mu       sync.RWMutex
        byHash   map[uint64]*memSeries
    }

    byID sync.Map // map[uint64]*memSeries

    idx *index.Index
}

func NewHead() *Head {
    h := &Head{idx: index.NewIndex()}
    for i := range h.stripes {
        h.stripes[i].byHash = make(map[uint64]*memSeries)
    }
    return h
}

func (h *Head) GetOrCreate(ls labels.Labels) *memSeries {
    hash := ls.Hash()
    stripe := &h.stripes[hash%stripeCount]

    stripe.mu.RLock()
    s := stripe.byHash[hash]
    stripe.mu.RUnlock()
    if s != nil {
        return s
    }

    stripe.mu.Lock()
    defer stripe.mu.Unlock()
    if s = stripe.byHash[hash]; s != nil {
        return s
    }
    id := atomic.AddUint64(&h.nextID, 1)
    s = &memSeries{id: id, labels: ls}
    stripe.byHash[hash] = s
    h.byID.Store(id, s)

    pairs := make([][2]string, len(ls))
    for i, l := range ls {
        pairs[i] = [2]string{l.Name, l.Value}
    }
    h.idx.Add(id, pairs)
    return s
}

func (h *Head) Append(ls labels.Labels, ts int64, v float64) uint64 {
    s := h.GetOrCreate(ls)
    s.append(ts, v)
    return s.id
}

func (h *Head) Series(id uint64) *memSeries {
    v, ok := h.byID.Load(id)
    if !ok {
        return nil
    }
    return v.(*memSeries)
}

func (h *Head) Index() *index.Index { return h.idx }
```

**Note the stripe pattern.** With 16 stripes and uniform hashing, the probability that two random writes contend is 1/16. With per-series locks for the chunk append, contention within a stripe further drops. This is the same idea as Java's `ConcurrentHashMap`.

### 11.8 `db/db.go` — Wire It Together

```go
package db

import (
    "minitsdb/pkg/head"
    "minitsdb/pkg/labels"
    "minitsdb/pkg/wal"
)

type DB struct {
    head *head.Head
    wal  *wal.WAL
}

func Open(dir string) (*DB, error) {
    w, err := wal.Open(dir + "/wal")
    if err != nil {
        return nil, err
    }
    db := &DB{head: head.NewHead(), wal: w}

    // Replay WAL into the head.
    err = w.Replay(func(seriesID uint64, ts int64, val float64) error {
        s := db.head.Series(seriesID)
        if s == nil {
            // In a real system we'd also replay label-set registration
            // records. We're cheating here.
            return nil
        }
        // (simplified — append directly)
        return nil
    })
    if err != nil {
        return nil, err
    }
    return db, nil
}

func (db *DB) Append(ls labels.Labels, ts int64, val float64) error {
    id := db.head.Append(ls, ts, val)
    return db.wal.Append(id, ts, val)
}

func (db *DB) Close() error {
    return db.wal.Close()
}
```

### 11.9 `query/engine.go` — A Minimal Range Query

```go
package query

import (
    "minitsdb/pkg/head"
    "minitsdb/pkg/index"
)

type Matcher struct{ Name, Value string }

type Sample struct {
    T int64
    V float64
}

type SeriesResult struct {
    Labels  string
    Samples []Sample
}

// RangeQuery returns all samples for all series matching ALL matchers
// within [start, end].
func RangeQuery(h *head.Head, matchers []Matcher, start, end int64) []SeriesResult {
    if len(matchers) == 0 {
        return nil
    }
    var ids index.Postings
    for i, m := range matchers {
        p := h.Index().Get(m.Name, m.Value)
        if i == 0 {
            ids = append(index.Postings(nil), p...)
        } else {
            ids = index.Intersect(ids, p)
        }
        if len(ids) == 0 {
            return nil
        }
    }

    out := make([]SeriesResult, 0, len(ids))
    for _, id := range ids {
        s := h.Series(id)
        if s == nil {
            continue
        }
        out = append(out, collect(s, start, end))
    }
    return out
}

func collect(s *memSeriesView, start, end int64) SeriesResult {
    // memSeriesView exposes labels + iterator over all chunks.
    var samples []Sample
    for _, c := range s.AllChunks() {
        it := c.Iterator()
        for it.Next() {
            t, v := it.At()
            if t < start {
                continue
            }
            if t > end {
                break
            }
            samples = append(samples, Sample{T: t, V: v})
        }
    }
    return SeriesResult{Labels: s.Labels().String(), Samples: samples}
}
```

(`memSeriesView` is glue around `*memSeries` exposing its labels and `AllChunks() []*chunk.GorillaChunk`. Add ~20 lines of accessors.)

### 11.10 What Aggregation Operators Look Like

The actual *math* of `rate()`, `sum`, `avg` is decoupled from storage. They consume iterators:

```go
// Rate: per-second rate of increase, computed as (last - first) / windowSeconds.
func Rate(samples []Sample, windowSec float64) float64 {
    if len(samples) < 2 {
        return 0
    }
    return (samples[len(samples)-1].V - samples[0].V) / windowSec
}

// SumBy: group samples across multiple series by a label.
func SumBy(results []SeriesResult, byLabel string, /* ts→ */ at int64) map[string]float64 {
    out := map[string]float64{}
    for _, r := range results {
        // pick sample closest to `at`
        v := nearestValue(r.Samples, at)
        key := extractLabelValue(r.Labels, byLabel)
        out[key] += v
    }
    return out
}
```

In Prometheus, PromQL is parsed into an AST where each node implements an `Evaluator` interface that streams `Vector`s — but the underlying machinery is exactly the iterator + aggregator chain above.

### 11.11 What's Still Missing (in production but not here)

```
PRODUCTION GAPS IN MiniTSDB
============================

  ✗ Persistent blocks (we keep everything in head — OOM in days)
  ✗ Block compaction & retention
  ✗ Tombstones for deletes
  ✗ WAL segments + CRC + replay of label registrations
  ✗ Mmap'd index files (we use Go maps — high RAM)
  ✗ Roaring bitmaps for postings
  ✗ Regex / negated / not-equals matchers
  ✗ Snapshot isolation for queries during compaction
  ✗ Out-of-order sample handling (Prometheus 2.39+ supports OOO)
  ✗ Histograms (sparse / dense bucket encoding)
  ✗ Remote write replication
```

Each is a multi-week project. But you now have the skeleton onto which they bolt, and — more importantly — you understand why each exists.

---

## 12. Concurrency in Go: What a TSDB Actually Needs

Go is unusually well-suited for TSDBs. Three patterns dominate:

### 12.1 Sharded Mutex Maps

We saw this in `Head`. The cost: 16× memory for the map structs. The gain: 16× write throughput under contention. **Rule:** use it when reads outnumber writes ~2:1 or less, and the map is hot.

### 12.2 Copy-on-Write for Query Snapshots

Queries need a stable view. Locking the head for the duration of a 10-second range query is unacceptable. The pattern:

```go
// During query setup, atomically snapshot the read-only pointers.
type seriesSnapshot struct {
    chunks []*chunk.GorillaChunk // includes the head chunk pointer
    labels labels.Labels
}

func (s *memSeries) Snapshot() seriesSnapshot {
    s.mu.RLock()
    defer s.mu.RUnlock()
    // Slices share backing arrays — cheap.
    // The head chunk may still be appended to, but Gorilla chunks
    // expose immutable byte slices once their length is captured.
    cs := make([]*chunk.GorillaChunk, len(s.closed)+1)
    copy(cs, s.closed)
    cs[len(s.closed)] = s.head
    return seriesSnapshot{chunks: cs, labels: s.labels}
}
```

The subtle part: a chunk being appended to *during* query execution must expose its "byte length at snapshot time" so the reader doesn't read past the bits that existed at snapshot. Prometheus solves this with `Chunk.Len()` captured at snapshot and clamping reads.

### 12.3 Goroutines for Block-Level Parallelism

Queries fan out one goroutine per block:

```go
results := make(chan SeriesResult, len(blocks))
for _, b := range blocks {
    b := b
    go func() {
        for r := range b.Query(matchers, start, end) {
            results <- r
        }
    }()
}
```

This is where Go shines vs. C++: zero pain. A 24-core machine querying 24 blocks in parallel is one `go func()` away.

---

## 13. Scaling Out: Sharding, Replication, HA

Single-node TSDBs handle ~1M samples/sec and ~10M series. Beyond that, scale out.

### 13.1 Three Sharding Strategies

```
SHARDING STRATEGIES
====================

  1. By Series Hash (Prometheus + Thanos receive, M3DB)
     ─────────────────────────────────────────────────
     shard = hash(labels) % N
     ✓ Perfect load balance for writes
     ✓ A series lives on one shard → query is shard-local
     ✗ "sum across all hosts" fans out to all shards
     ✗ Adding a shard requires consistent hashing / virtual nodes

  2. By Time (rare standalone, common as a 2nd axis)
     ──────────────────────────────────────────────
     shard = floor(t / window) % N
     ✓ Old shards become read-only → easy archival
     ✗ Hot shard is whichever covers "now" → write hotspot
     ✗ Almost never used alone; combined with #1

  3. By Tenant (Mimir, Cortex)
     ───────────────────────
     shard = hash(tenant_id, labels) % N
     ✓ Isolation: noisy tenant doesn't drown others
     ✓ Per-tenant retention/limits trivial
     ✗ Lots of tenants × few series each → poor packing
```

Most production deployments combine **(tenant_id, series_hash)** and accept fan-out on cross-shard aggregations.

### 13.2 Replication Factor 3

Each sample is written to 3 ingesters; queries succeed if 2 of 3 respond:

```
INGEST WITH REPLICATION (Cortex/Mimir)
=======================================

   write(sample) ────► load balancer ────► distributor
                                                 │
                                                 ├── hash(series) → ingester_A
                                                 ├── hash(series) → ingester_B
                                                 └── hash(series) → ingester_C
                                                                       │
                                                                       ▼
                                          (each writes to its own WAL+head)

   query() ──► query frontend ──► querier
                                     │
                                     ├── ask 3 ingesters
                                     ├── wait for quorum (2 of 3)
                                     └── merge & dedupe samples
```

This is **CRDT-like**: samples are idempotent at `(series_id, ts)` so duplicates from different replicas merge cleanly. Out-of-order samples within a small window are reconciled by "last-write wins" (timestamp-tiebreak).

### 13.3 The Object-Storage Tier

Once data is older than ~6 hours, every modern TSDB ships it to object storage (S3, GCS):

```
HOT / COLD TIER
================

   Ingesters       (RAM + local SSD, < 6h)
       │
       ▼ flush every 2h
   Compactor       (reads from ingesters' shipped blocks)
       │
       ▼
   S3 / GCS        (cheap, durable, immutable blocks)
       │
       ▼ on query
   Store Gateway   (caches index + bloom in RAM, streams chunks from S3)
       │
       ▼
   Querier         (combines store-gateway + ingester results)
```

The "block" abstraction makes this clean: a block is a self-contained directory; uploading it to S3 is `aws s3 sync`. Mimir, Thanos, Cortex, and Grafana Loki (logs but same model) all do this.

---

## 14. Production Systems Compared

```
THE LANDSCAPE  (2026)
======================

  ┌──────────────────┬──────────┬──────────────┬─────────────┬─────────────┐
  │ System           │ Language │ Compression  │ Index       │ Killer Feature
  ├──────────────────┼──────────┼──────────────┼─────────────┼─────────────┤
  │ Prometheus       │ Go       │ Gorilla XOR  │ Per-block   │ De-facto OSS
  │ (single-node)    │          │              │ FST+posting │ Pull model, PromQL
  ├──────────────────┼──────────┼──────────────┼─────────────┼─────────────┤
  │ Mimir            │ Go       │ Prom chunks  │ Per-tenant  │ Horizontally
  │ (multi-tenant)   │          │              │ shards      │ scalable Prom
  ├──────────────────┼──────────┼──────────────┼─────────────┼─────────────┤
  │ Thanos           │ Go       │ Prom chunks  │ Per-block   │ S3 long-term
  │                  │          │              │ on object   │ + global query
  ├──────────────────┼──────────┼──────────────┼─────────────┼─────────────┤
  │ VictoriaMetrics  │ Go       │ Custom       │ MergeTree-  │ Tiny RAM,
  │                  │          │ (better than │ like        │ wire-compat
  │                  │          │ Gorilla for  │             │ with Prom
  │                  │          │ many cases)  │             │
  ├──────────────────┼──────────┼──────────────┼─────────────┼─────────────┤
  │ InfluxDB IOx     │ Rust     │ Parquet +    │ Arrow-based │ Arrow/DataFusion
  │ (v3)             │          │ Zstd         │             │ columnar SQL
  ├──────────────────┼──────────┼──────────────┼─────────────┼─────────────┤
  │ TimescaleDB      │ C        │ Per-chunk    │ Postgres    │ Real SQL, joins
  │                  │          │ compression  │ b-tree+brin │ to relational
  ├──────────────────┼──────────┼──────────────┼─────────────┼─────────────┤
  │ M3DB             │ Go       │ Gorilla      │ FST-based   │ Uber-scale,
  │                  │          │              │             │ multi-DC
  ├──────────────────┼──────────┼──────────────┼─────────────┼─────────────┤
  │ ClickHouse       │ C++      │ Various      │ Skip indexes│ Best raw
  │ (for metrics)    │          │              │             │ analytics perf
  ├──────────────────┼──────────┼──────────────┼─────────────┼─────────────┤
  │ QuestDB          │ Java     │ Columnar     │ Designated  │ Lowest-latency
  │                  │          │              │ timestamp   │ ingest
  └──────────────────┴──────────┴──────────────┴─────────────┴─────────────┘
```

**How to pick:**

- Single team, ops metrics, < 10M series? **Prometheus** alone. Done.
- Many teams, need long-term storage? **Prometheus + Thanos** or **Mimir**.
- Tiny RAM budget, Prom-compatible? **VictoriaMetrics**.
- Need SQL joins with business data? **TimescaleDB**.
- IoT, billions of points/day, custom queries? **InfluxDB v3** or **QuestDB**.
- Analytics over metrics, not real-time monitoring? **ClickHouse**.

---

## 15. Operational Pitfalls Nobody Warns You About

```
THE PITFALL HALL OF FAME
=========================

  1. CARDINALITY EXPLOSION
     A developer adds `user_id` label. 10M users → 10M series → OOM.
     Mitigation: hard limits per tenant, automated cardinality alerts,
                 code review rule "no IDs in labels".

  2. CLOCK SKEW
     A scraper's clock drifts +5s. New samples arrive with timestamps
     that look "old" → silently dropped as out-of-order.
     Mitigation: NTP everywhere, alert on samples-dropped-OOO metric.

  3. HEAD-COMPACTION STORMS
     Default 2h flush window means every 2h all ingesters compact at
     once → CPU + disk spike, scrape timeouts.
     Mitigation: jitter the compaction time per node.

  4. BLOCK CORRUPTION
     Power loss mid-write → block's index file half-written → querier
     crashes on startup.
     Mitigation: CRC every file, refuse to load corrupt blocks, alert.

  5. WAL UNBOUNDED GROWTH
     Flush fails silently (disk full on data dir but not WAL dir).
     WAL grows forever → eventually fills its disk too.
     Mitigation: monitor flush success rate; size WAL disk = 2× expected.

  6. QUERY-OF-DEATH
     `{__name__=~".+"}` matches every series. One query blows up RAM.
     Mitigation: required label match (`__name__` must be exact), max-
                 series-per-query limit, query timeouts, samples-scanned
                 limit.

  7. SCRAPE STORMS
     500 targets behind one load balancer, scrape interval=15s, scrape
     timeout=10s. Backend hiccup → all 500 in-flight at once → connection
     storm → cascading failure.
     Mitigation: jitter scrape times, set realistic timeouts.

  8. THE "WHO MONITORS THE MONITOR" PROBLEM
     TSDB crashes. The alert "TSDB is down" can't fire because the TSDB
     is down.
     Mitigation: dead-man's switch (cron pings external service that
                 alerts if pings stop), separate "meta-monitor" cluster.
```

---

## 16. Cheat-Sheet: When to Use What

```
DECISION TREE
==============

  Q: Is your data (timestamp, value) with mostly-append, time-ordered writes?
     │
     ├── NO  → use a normal database. Don't force a TSDB.
     │
     └── YES → continue
              │
              ├── Q: < 100k active series total?
              │     └── YES → SQLite + time index. Yes, really.
              │
              ├── Q: < 10M series, < 1M samples/sec, ops monitoring?
              │     └── YES → Prometheus, single node.
              │
              ├── Q: Need years of retention + global view across teams?
              │     └── YES → Mimir / Thanos.
              │
              ├── Q: Need SQL joins to business data?
              │     └── YES → TimescaleDB.
              │
              ├── Q: Need analytics-style SQL across billions of points?
              │     └── YES → ClickHouse or InfluxDB v3.
              │
              └── Q: Building a SaaS where customers query their data?
                    └── YES → managed offering (Grafana Cloud, Datadog,
                              Chronosphere). Don't build this yourself
                              for v1.
```

---

## Closing Notes

The reason this document is structured the way it is — physics first, abstractions second, code last — is that every TSDB is fundamentally an exercise in **arithmetic intensity**: how few bits can you spend per sample, how few cache lines per query, how few syscalls per fsync. The clever ideas (Gorilla, posting lists, head/block separation) all exist because the alternative — generic storage engines — is *off by 10×* on at least one of those axes.

If you've worked through `MiniTSDB`, you can now:
- Read Prometheus's `tsdb/` source without confusion.
- Diagnose a real-world cardinality crisis from first principles.
- Decide when to reach for TimescaleDB vs Prometheus vs ClickHouse.
- Implement Gorilla compression from scratch on a whiteboard.
- Reason about RF=3 quorum writes vs single-node WAL durability.

That's the level of fluency this folder aims for across every database deep dive. Next time you scroll through a Grafana dashboard, you'll know exactly what wheel is spinning underneath each panel.
