# In-Process OLAP Engines: DuckDB & chDB — A Staff-Engineer Deep Dive

A comprehensive guide to the new paradigm of embedded, in-process analytical databases that bring warehouse-grade query performance directly into your application's memory space — no servers, no network hops, no infrastructure.

---

## Table of Contents

1. [The In-Process Revolution](#1-the-in-process-revolution)
2. [Why In-Process OLAP Is State of the Art](#2-why-in-process-olap-is-state-of-the-art)
3. [DuckDB Deep Dive](#3-duckdb-deep-dive)
4. [chDB Deep Dive](#4-chdb-deep-dive)
5. [Storage & Buffer Management](#5-storage--buffer-management)
6. [Query Execution Internals](#6-query-execution-internals)
7. [Out-of-Core Processing: Bigger Than RAM](#7-out-of-core-processing-bigger-than-ram)
8. [Zero-Copy Data Integration](#8-zero-copy-data-integration)
9. [Concurrency & Transaction Model](#9-concurrency--transaction-model)
10. [Production Use Cases & Patterns](#10-production-use-cases--patterns)
11. [DuckDB vs chDB vs Polars: When to Use What](#11-duckdb-vs-chdb-vs-polars-when-to-use-what)
12. [Anti-Patterns & Pitfalls](#12-anti-patterns--pitfalls)
13. [The Bigger Picture: Where In-Process OLAP Fits](#13-the-bigger-picture-where-in-process-olap-fits)

---

## 1. The In-Process Revolution

### The Paradigm Shift

For decades, analytical databases followed a client-server model: your application sends SQL over a network to a separate database process (or cluster), which returns results. In-process OLAP eliminates the entire middle layer.

```
Traditional OLAP Stack:                  In-Process OLAP Stack:
═══════════════════════                  ═══════════════════════

┌──────────────────┐                     ┌──────────────────────────────┐
│  Your Application│                     │  Your Application            │
│                  │                     │                              │
│  Python / Go /   │                     │  ┌────────────────────────┐  │
│  Java / Node     │                     │  │  DuckDB / chDB Engine  │  │
│                  │                     │  │  (shared address space) │  │
│  ┌────────────┐  │                     │  │                        │  │
│  │ DB Client  │  │                     │  │  Parser → Optimizer →  │  │
│  │ (driver)   │  │                     │  │  Executor → Storage    │  │
│  └─────┬──────┘  │                     │  │                        │  │
└────────┼─────────┘                     │  │  Memory: shared heap   │  │
         │  TCP/IP, HTTP, gRPC           │  │  Disk: local files     │  │
         │  serialization overhead       │  └────────────────────────┘  │
         │  auth, TLS handshake          │                              │
         ▼                               │  Zero network overhead       │
┌──────────────────┐                     │  Zero serialization          │
│  OLAP Server     │                     │  Zero auth ceremony          │
│  (ClickHouse,    │                     └──────────────────────────────┘
│   Redshift,      │
│   BigQuery)      │                       pip install duckdb
│                  │                       pip install chdb
│  Needs:          │
│  - Server infra  │                       Total setup time: 0 seconds
│  - Networking    │
│  - Auth/RBAC     │
│  - Monitoring    │
│  - Upgrades      │
└──────────────────┘
```

### What "In-Process" Actually Means

When we say "in-process", we mean the database engine runs as a **library** linked into your application's process. This has profound implications:

| Property | Client-Server DB | In-Process DB |
|----------|-----------------|---------------|
| **Memory space** | Separate process/machine | Shared with your app |
| **Data transfer** | Serialize → network → deserialize | Pointer pass (zero-copy) |
| **Latency floor** | ~100 μs minimum (loopback TCP) | ~1 μs (function call) |
| **Startup time** | Seconds to minutes (server boot) | Milliseconds (library load) |
| **Configuration** | Config files, ports, users, TLS | None (sensible defaults) |
| **Scaling model** | Horizontal (add nodes) | Vertical (use all local cores) |
| **Deployment** | Server + client + infra | Single binary / pip install |
| **Failure domain** | App crash ≠ DB crash | App crash = DB crash |

### The Key Insight

Modern hardware has gotten **absurdly powerful**. A single machine with 32 cores, 256 GB RAM, and NVMe SSDs can process billions of rows in seconds — if the software is designed to exploit it. In-process OLAP engines are that software.

```
The "One Machine" Reality Check (2025):
═══════════════════════════════════════

  ┌─────────────────────────────────────────────────────────┐
  │  MacBook Pro M3 Max (consumer laptop!)                  │
  │                                                         │
  │  CPU:    16 cores (12 performance + 4 efficiency)       │
  │  RAM:    128 GB unified memory                          │
  │  SSD:    8 TB NVMe, ~7 GB/s sequential read             │
  │  Memory BW: ~400 GB/s                                   │
  │                                                         │
  │  What DuckDB can do on this machine:                    │
  │  ─────────────────────────────────────                  │
  │  • Scan 1 billion rows/sec (narrow columns)             │
  │  • Aggregate 100M groups in < 2 seconds                 │
  │  • Join two 500M-row tables in < 10 seconds             │
  │  • Query 1 TB of Parquet files (with column pruning)    │
  │                                                         │
  │  For context, this exceeds the throughput of a           │
  │  typical 4-node Redshift cluster from 2020.             │
  └─────────────────────────────────────────────────────────┘
```

---

## 2. Why In-Process OLAP Is State of the Art

### The Five Wins

```
                     ┌──────────────┐
                     │   IN-PROCESS │
                     │     OLAP     │
                     └──────┬───────┘
        ┌────────┬────────┬─┴──┬──────────┐
        ▼        ▼        ▼    ▼          ▼
   ┌────────┐┌───────┐┌──────┐┌─────┐┌────────┐
   │ Zero   ││ Zero  ││ Full ││Zero ││ Good   │
   │ Infra  ││ Copy  ││ Core ││Conf ││ Memory │
   │ Ops    ││ I/O   ││Usage ││igur ││Citizen │
   └────────┘└───────┘└──────┘└─────┘└────────┘
```

**1. Zero Infrastructure Ops**
No servers to provision, no clusters to scale, no ports to open, no TLS certs to rotate. The database is a library dependency in your `requirements.txt`.

**2. Zero-Copy Data Exchange**
Your Pandas DataFrame, Arrow Table, or Polars DataFrame lives in the same process memory. The DB engine reads it directly — no serialization, no network transfer, no data duplication.

**3. Full Core Utilization**
Morsel-driven parallelism (DuckDB) and vectorized execution (both DuckDB and chDB) saturate all available CPU cores with a work-stealing scheduler. No inter-node coordination overhead.

**4. Zero Configuration**
Sensible defaults for everything. No tuning of `shared_buffers`, `work_mem`, `max_connections`, or any of the 300+ knobs that a production PostgreSQL demands.

**5. Good Memory Citizen**
Unlike a standalone database server that grabs as much RAM as possible, in-process engines are designed to coexist with your application. They respect memory limits, spill to disk gracefully, and don't OOM-kill your process.

### Historical Context: How We Got Here

```
Timeline:
═════════

2000s     SQLite becomes the "embedded database" standard
          ├── But: row-oriented, single-threaded, OLTP-focused
          └── Analytical queries on SQLite = painfully slow

2010s     Big Data era: Hadoop, Spark, Presto
          ├── "Throw a cluster at it" mentality
          ├── ETL pipelines: Extract → Load into warehouse → Query
          └── Even simple analytics required distributed infra

2018      DuckDB created (CWI Amsterdam, Raasveldt & Mühleisen)
          ├── Key paper: "DuckDB: An Embeddable Analytical Database"
          ├── Insight: columnar + vectorized + in-process = fast
          └── "SQLite for analytics"

2023      chDB created (Auxten / ClickHouse community)
          ├── Embeds the full ClickHouse engine as a library
          └── "ClickHouse without the server"

2024      ClickHouse Inc. acquires chDB
          ├── First-party support for embedded ClickHouse
          ├── chDB 4.0: DataStore API, lazy evaluation
          └── Both DuckDB and chDB hit mainstream adoption

2025+     In-process OLAP is the default for single-node analytics
          ├── Jupyter notebooks: DuckDB > Spark for most workloads
          ├── Microservices: embedded analytics without DB dependency
          └── Edge computing: analytics on devices, no cloud required
```

---

## 3. DuckDB Deep Dive

### Core Architecture

DuckDB is an **embedded, columnar, vectorized analytical database** designed to be the "SQLite of analytics". It runs in-process with zero external dependencies.

```
┌──────────────────────────────────────────────────────────────────┐
│                        DuckDB Architecture                        │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────┐     │
│  │  Client API Layer                                        │     │
│  │  Python │ R │ Java │ Node.js │ Rust │ Go │ C/C++ │ WASM  │     │
│  └─────────────────────────┬────────────────────────────────┘     │
│                             │ function calls (no network)          │
│  ┌─────────────────────────▼────────────────────────────────┐     │
│  │  SQL Parser (PostgreSQL-compatible)                       │     │
│  │  ┌──────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐   │     │
│  │  │Lexer │→│ Parser   │→│ Binder   │→│ Logical Planner│   │     │
│  │  └──────┘ └──────────┘ └──────────┘ └────────┬───────┘   │     │
│  │                                               │            │     │
│  │  ┌────────────────────────────────────────────▼─────────┐ │     │
│  │  │  Optimizer                                            │ │     │
│  │  │  • Filter pushdown    • Join reordering (DP-based)    │ │     │
│  │  │  • Projection pruning • Common subexpression elim.    │ │     │
│  │  │  • Cardinality est.   • Top-N optimization            │ │     │
│  │  │  • Predicate transfer • Unnesting correlated subq.    │ │     │
│  │  └────────────────────────────────────────────┬─────────┘ │     │
│  │                                               │            │     │
│  │  ┌────────────────────────────────────────────▼─────────┐ │     │
│  │  │  Physical Planner → Pipeline Builder                  │ │     │
│  │  │  Breaks plan into pipelines at "pipeline breakers"    │ │     │
│  │  │  (hash join build, sort, aggregate)                   │ │     │
│  │  └────────────────────────────────────────────┬─────────┘ │     │
│  └───────────────────────────────────────────────┼───────────┘     │
│                                                   │                  │
│  ┌───────────────────────────────────────────────▼───────────┐     │
│  │  Vectorized Execution Engine                               │     │
│  │                                                             │     │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────────────┐  │     │
│  │  │ Pipeline 1   │ │ Pipeline 2   │ │ Pipeline 3          │  │     │
│  │  │              │ │              │ │                     │  │     │
│  │  │ TableScan    │ │ HashJoin     │ │ HashAggregate       │  │     │
│  │  │ Filter       │ │   (probe)   │ │ Sort                │  │     │
│  │  │ Projection   │ │ Projection  │ │ Limit               │  │     │
│  │  └─────────────┘ └─────────────┘ └─────────────────────┘  │     │
│  │                                                             │     │
│  │  Morsel-Driven Scheduler:                                   │     │
│  │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐                      │     │
│  │  │Core 0│ │Core 1│ │Core 2│ │Core N│  Work-stealing queue  │     │
│  │  └──────┘ └──────┘ └──────┘ └──────┘                      │     │
│  └─────────────────────────────────────────────────────────────┘     │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │  Storage Layer                                               │     │
│  │                                                               │     │
│  │  ┌─────────────┐ ┌─────────────────┐ ┌──────────────────┐   │     │
│  │  │ Native      │ │ External File   │ │ In-Memory        │   │     │
│  │  │ Storage     │ │ Readers         │ │ Sources          │   │     │
│  │  │ (.duckdb)   │ │                 │ │                  │   │     │
│  │  │             │ │ • Parquet       │ │ • Pandas DF      │   │     │
│  │  │ Row groups  │ │ • CSV / TSV     │ │ • Arrow Tables   │   │     │
│  │  │ Column      │ │ • JSON / NDJSON │ │ • Polars DF      │   │     │
│  │  │ segments    │ │ • Excel         │ │ • NumPy Arrays   │   │     │
│  │  │ Zone maps   │ │ • Iceberg       │ │ • R data.frames  │   │     │
│  │  │ Statistics  │ │ • Delta Lake    │ │                  │   │     │
│  │  │             │ │ • S3 / HTTP     │ │ (zero-copy via   │   │     │
│  │  │             │ │ • GCS / Azure   │ │  Apache Arrow)   │   │     │
│  │  └─────────────┘ └─────────────────┘ └──────────────────┘   │     │
│  │                                                               │     │
│  │  Buffer Manager: 256 KB blocks, LRU eviction, disk spill     │     │
│  └─────────────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────────┘
```

### Vectorized Execution Model

DuckDB uses a **push-based, vectorized** execution model. Data flows through the pipeline in **vectors** of up to 2,048 rows (called the "standard vector size"). This design exploits CPU cache locality and SIMD instructions.

```
Row-at-a-time (Volcano):              Vectorized (DuckDB):
════════════════════════               ═══════════════════

  next() → 1 row                        next_chunk() → 2048 rows
  next() → 1 row                        (one vector per column)
  next() → 1 row
  next() → 1 row                      ┌─────────────────────────────┐
  ...                                  │  Vector (column "price")    │
                                       │  [12.5, 99.0, 3.14, ...]   │
  Per-row overhead:                    │  2048 values, contiguous    │
  • Virtual function call              │  in memory                  │
  • Branch mispredictions              └─────────────────────────────┘
  • Cache misses (data not             
    contiguous)                        Per-vector overhead:
                                       • One function call per 2048 rows
  Throughput: ~millions rows/sec       • SIMD-friendly (auto-vectorization)
                                       • Data stays in L1/L2 cache
                                       
                                       Throughput: ~billions rows/sec

Column vectors during Filter operation:
───────────────────────────────────────

Input vectors:                    Selection vector:        Output vectors:
┌──────────┬──────────┐          ┌───────────────┐        ┌──────────┬──────────┐
│  price   │  region  │          │ [0, 2, 5, 7,  │        │  price   │  region  │
│──────────│──────────│   Filter │  11, 15, ...]  │        │──────────│──────────│
│  12.50   │  "US"    │   ────►  │               │  ────►  │  12.50   │  "US"    │
│   3.00   │  "EU"    │ price>10 │ Indices of     │        │  99.00   │  "US"    │
│  99.00   │  "US"    │          │ qualifying     │        │  45.00   │  "APAC"  │
│   1.50   │  "EU"    │          │ rows           │        │  ...     │  ...     │
│  ...     │  ...     │          └───────────────┘        └──────────┴──────────┘
└──────────┴──────────┘
                                 No data movement! Just indices.
                                 Subsequent operators read through
                                 the selection vector.
```

### Morsel-Driven Parallelism

The morsel-driven model (from the 2014 TUM paper by Leis et al.) is DuckDB's approach to intra-query parallelism. It avoids the static partitioning problems of Volcano-style exchange operators.

```
How Morsel-Driven Parallelism Works:
════════════════════════════════════

Step 1: Break query into pipelines at "pipeline breakers"
─────────────────────────────────────────────────────────

  Pipeline 1:                Pipeline 2:
  ┌───────────┐              ┌───────────────┐
  │ Scan(T1)  │              │ Scan(T2)      │
  │ Filter    │              │ HashJoin Probe│
  │ HashJoin  │              │ Aggregate     │
  │   Build   │◄─────────── │ Result        │
  └───────────┘   depends    └───────────────┘
  (builds hash table)        (probes hash table)


Step 2: Each pipeline processes data in "morsels"
─────────────────────────────────────────────────

  Table T1 (100M rows):
  ┌───────┬───────┬───────┬───────┬───────┬───────┬─── ...
  │Morsel │Morsel │Morsel │Morsel │Morsel │Morsel │
  │  0    │  1    │  2    │  3    │  4    │  5    │
  │ 10K   │ 10K   │ 10K   │ 10K   │ 10K   │ 10K   │
  │ rows  │ rows  │ rows  │ rows  │ rows  │ rows  │
  └───┬───┴───┬───┴───┬───┴───────┴───┬───┴───┬───┘
      │       │       │               │       │
      ▼       ▼       ▼               ▼       ▼
  ┌──────┐┌──────┐┌──────┐       ┌──────┐┌──────┐
  │Core 0││Core 1││Core 2│  ...  │Core 6││Core 7│
  └──┬───┘└──┬───┘└──┬───┘       └──┬───┘└──┬───┘
     │       │       │               │       │
     ▼       ▼       ▼               ▼       ▼
  ┌──────────────────────────────────────────────┐
  │       Shared Hash Table (lock-free inserts)   │
  │       Thread-local partitions merged at end   │
  └──────────────────────────────────────────────┘


Step 3: Work-stealing for load balancing
────────────────────────────────────────

  Core 0: ████████ done!  → steals from Core 3's queue
  Core 1: ████████████ done!
  Core 2: ██████████ done!  → steals from Core 5's queue
  Core 3: ██████████████████░░░░  (slow partition, being helped)
  Core 4: ████████████ done!
  Core 5: ████████████████░░░░░░  (slow partition, being helped)

  Result: near-perfect load balancing regardless of data skew
```

### Native Storage Format

DuckDB's native `.duckdb` file format is a single-file, columnar, compressed database with built-in metadata.

```
DuckDB Native Storage File Layout:
═══════════════════════════════════

┌────────────────────────────────────────────────────────────┐
│  File Header                                                │
│  • Magic bytes: "DUCK"                                      │
│  • Version number                                           │
│  • Checkpoint offset                                        │
│  • Free list pointer                                        │
└─────────────────────────────┬──────────────────────────────┘
                              │
┌─────────────────────────────▼──────────────────────────────┐
│  Catalog (Schema Metadata)                                  │
│  • Table definitions                                        │
│  • Column types, constraints                                │
│  • View definitions                                         │
│  • Macro / function definitions                             │
└─────────────────────────────┬──────────────────────────────┘
                              │
┌─────────────────────────────▼──────────────────────────────┐
│  Row Group 0 (default: ~122,880 rows per group)             │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  Column Segment: "id" (INTEGER)                        │ │
│  │  ┌──────────────┐                                      │ │
│  │  │ Compression: │ BitPacking / RLE / Dictionary /      │ │
│  │  │              │ Constant / Uncompressed               │ │
│  │  │ Zone Map:    │ min=1, max=122880                     │ │
│  │  │ Null Bitmap: │ [0000...0000] (no nulls)              │ │
│  │  │ Data:        │ [compressed column values]            │ │
│  │  └──────────────┘                                      │ │
│  ├────────────────────────────────────────────────────────┤ │
│  │  Column Segment: "name" (VARCHAR)                      │ │
│  │  ┌──────────────┐                                      │ │
│  │  │ Compression: │ Dictionary encoding                   │ │
│  │  │ Zone Map:    │ min="Alice", max="Zara"               │ │
│  │  │ Dictionary:  │ ["Alice","Bob","Carol",...,"Zara"]     │ │
│  │  │ Data:        │ [dictionary indices, bit-packed]       │ │
│  │  └──────────────┘                                      │ │
│  ├────────────────────────────────────────────────────────┤ │
│  │  Column Segment: "amount" (DOUBLE)                     │ │
│  │  ┌──────────────┐                                      │ │
│  │  │ Compression: │ Chimp / Patas (floating-point)        │ │
│  │  │ Zone Map:    │ min=0.01, max=99999.99                │ │
│  │  │ Data:        │ [compressed values]                   │ │
│  │  └──────────────┘                                      │ │
│  └────────────────────────────────────────────────────────┘ │
├────────────────────────────────────────────────────────────┤
│  Row Group 1                                                │
│  ...                                                        │
├────────────────────────────────────────────────────────────┤
│  Row Group N                                                │
│  ...                                                        │
└────────────────────────────────────────────────────────────┘
```

### Compression Algorithms

DuckDB automatically selects the best compression per column segment based on data characteristics:

| Algorithm | Best For | How It Works | Ratio |
|-----------|----------|-------------|-------|
| **Constant** | All-same values | Store value once | ~1000:1 |
| **RLE** | Sorted / many repeated runs | Run-length encoding | 10:1 – 100:1 |
| **Dictionary** | Low-cardinality strings | Map values to small integer codes | 5:1 – 50:1 |
| **BitPacking** | Integers with small range | Use minimum bits per value | 2:1 – 8:1 |
| **Frame of Reference** | Clustered integers | Store base + small offsets | 3:1 – 10:1 |
| **Chimp / Patas** | Floating-point data | XOR-based delta encoding | 2:1 – 5:1 |
| **FSST** | High-cardinality strings | Fast Static Symbol Table | 2:1 – 4:1 |
| **ALP** | Floating-point (adaptive) | Adaptive Lossless float compression | 2:1 – 6:1 |
| **Uncompressed** | Random / incompressible | Raw storage | 1:1 |

```
Automatic Compression Selection:
════════════════════════════════

  Column data → Analyzer samples values → Picks best algorithm:

  ┌────────────────────────┐
  │ Sample column values   │
  └───────────┬────────────┘
              │
              ▼
  ┌─────────────────────────────────────────────┐
  │ All identical?  ──yes──►  Constant           │
  │       │ no                                   │
  │ Long runs of same value?  ──yes──►  RLE      │
  │       │ no                                   │
  │ Low cardinality (< 4096)?  ──yes──►  Dict    │
  │       │ no                                   │
  │ Integer with small range?  ──yes──►  BitPack │
  │       │ no                                   │
  │ Float data?  ──yes──►  Chimp/ALP             │
  │       │ no                                   │
  │ String data?  ──yes──►  FSST                 │
  │       │ no                                   │
  │ Fallback  ──────────►  Uncompressed          │
  └─────────────────────────────────────────────┘
```

### Indexing: ART and Zone Maps

DuckDB uses two indexing strategies, both fundamentally different from traditional B-Trees.

```
Zone Maps (Automatic, Always-On):
═════════════════════════════════

  Every column segment stores min/max metadata.
  The query engine uses these to SKIP entire row groups.

  Query: SELECT * FROM sales WHERE date = '2024-06-15'

  Row Group 0:  date range [2024-01-01, 2024-03-31]  → SKIP ✗
  Row Group 1:  date range [2024-04-01, 2024-06-30]  → SCAN ✓
  Row Group 2:  date range [2024-07-01, 2024-09-30]  → SKIP ✗
  Row Group 3:  date range [2024-10-01, 2024-12-31]  → SKIP ✗

  Result: Only 1/4 of data is read from disk.
  Pro tip: Sort your data on high-selectivity filter columns
           during ingestion to maximize zone map effectiveness.


ART Index (Adaptive Radix Tree):
════════════════════════════════

  Used for PRIMARY KEY / UNIQUE constraints and selective point lookups.

  Structure (simplified):

           ┌──┐
           │48│  Node4 (≤4 children)
      ┌────┴──┴────┐
      │             │
    ┌─▼─┐        ┌─▼─┐
    │ 16│        │ 48│  Node16 (≤16 children)
    └─┬─┘        └─┬─┘
      │             │
   ┌──▼──┐      ┌──▼──┐
   │Leaf │      │Leaf │  → Row ID pointer
   │ 256 │      │ 768 │
   └─────┘      └─────┘

  Key properties:
  • Adaptive: uses 4/16/48/256-child nodes based on density
  • Cache-friendly: path compression collapses single-child chains
  • Persistent: stored on disk alongside column data
  • Limitation: must fit in memory during creation
  • Best for: <0.1% selectivity point lookups
```

---

## 4. chDB Deep Dive

### Core Architecture

chDB embeds the **full ClickHouse server engine** as a shared library. It's not a "lite" version — it's the same C++ code that powers ClickHouse clusters, compiled to run in-process.

```
┌──────────────────────────────────────────────────────────────────┐
│                         chDB Architecture                         │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────┐     │
│  │  Host Application (Python / Go / Node.js / Rust / Bun)   │     │
│  └─────────────────────────┬────────────────────────────────┘     │
│                             │ C FFI / Python bindings              │
│  ┌─────────────────────────▼────────────────────────────────┐     │
│  │  chDB API Layer                                           │     │
│  │  ┌────────────┐  ┌───────────────┐  ┌────────────────┐   │     │
│  │  │ query()    │  │ Session()     │  │ DataStore API  │   │     │
│  │  │ (stateless)│  │ (stateful,    │  │ (lazy eval,    │   │     │
│  │  │            │  │  temp tables) │  │  Pandas-like)  │   │     │
│  │  └────────────┘  └───────────────┘  └────────────────┘   │     │
│  └─────────────────────────┬────────────────────────────────┘     │
│                             │                                      │
│  ┌─────────────────────────▼────────────────────────────────┐     │
│  │  ClickHouse Core Engine (full C++ engine, not a subset)   │     │
│  │                                                            │     │
│  │  ┌──────────────────┐  ┌────────────────────────────┐     │     │
│  │  │ SQL Parser        │  │ Query Pipeline Engine       │     │     │
│  │  │ (ClickHouse SQL)  │  │ • Vectorized execution      │     │     │
│  │  │                   │  │ • SIMD-optimized operators   │     │     │
│  │  │ Supports:         │  │ • Parallel pipeline exec    │     │     │
│  │  │ • Array functions │  │ • JIT compilation (LLVM)    │     │     │
│  │  │ • Window funcs    │  │                              │     │     │
│  │  │ • Lambda exprs    │  │ Codegen:                     │     │     │
│  │  │ • WITH (CTE)      │  │ Hot inner loops compiled     │     │     │
│  │  │ • PREWHERE        │  │ to native machine code       │     │     │
│  │  └──────────────────┘  └────────────────────────────┘     │     │
│  │                                                            │     │
│  │  ┌────────────────────────────────────────────────────┐   │     │
│  │  │ Table Engines (all available!)                      │   │     │
│  │  │ • MergeTree family (ReplacingMergeTree, etc.)       │   │     │
│  │  │ • Memory engine                                     │   │     │
│  │  │ • File engines (Parquet, CSV, JSON, ORC, Avro...)   │   │     │
│  │  │ • URL engine (read from HTTP endpoints)             │   │     │
│  │  │ • S3 engine                                         │   │     │
│  │  └────────────────────────────────────────────────────┘   │     │
│  │                                                            │     │
│  │  ┌────────────────────────────────────────────────────┐   │     │
│  │  │ 70+ Input/Output Formats                           │   │     │
│  │  │ CSV │ TSV │ JSON │ Parquet │ ORC │ Arrow │ Avro │   │   │     │
│  │  │ MsgPack │ Protobuf │ CapnProto │ Native │ ...      │   │     │
│  │  └────────────────────────────────────────────────────┘   │     │
│  └────────────────────────────────────────────────────────────┘     │
│                                                                      │
│  Key difference from DuckDB:                                         │
│  chDB inherits ClickHouse's MergeTree engine, which gives it:        │
│  • Sparse primary indexes (not B-Tree, not ART)                      │
│  • Background merges and compaction                                   │
│  • Materialized views and projections                                │
│  • TTL-based data lifecycle management                               │
└──────────────────────────────────────────────────────────────────────┘
```

### ClickHouse SQL vs PostgreSQL SQL

chDB uses ClickHouse SQL syntax, which differs from PostgreSQL-compatible SQL (used by DuckDB). Key differences:

```sql
-- ═══════════════════════════════════════════════════════════
-- Feature: PREWHERE (chDB-exclusive optimization)
-- ═══════════════════════════════════════════════════════════

-- Standard SQL (DuckDB / PostgreSQL):
SELECT user_id, event_name, properties
FROM events
WHERE event_date = '2024-06-15'
  AND event_name = 'purchase';

-- ClickHouse SQL (chDB):
-- PREWHERE reads only the filter column FIRST,
-- then loads remaining columns only for matching rows.
SELECT user_id, event_name, properties
FROM events
PREWHERE event_date = '2024-06-15'  -- read this column first
WHERE event_name = 'purchase';       -- then filter again

-- On wide tables (100+ columns), PREWHERE can reduce I/O by 10-100x
-- because it avoids reading the heavy "properties" column for
-- rows that don't match the date filter.
```

```sql
-- ═══════════════════════════════════════════════════════════
-- Feature: Array functions (chDB excels here)
-- ═══════════════════════════════════════════════════════════

-- Flatten nested arrays, filter, and aggregate in one query:
SELECT
    user_id,
    arrayFilter(x -> x > 100, purchase_amounts) AS big_purchases,
    arrayReduce('sum', purchase_amounts) AS total_spent,
    arrayDistinct(categories) AS unique_categories
FROM user_activity;

-- Lambda expressions inside SQL — not available in DuckDB or PostgreSQL.
```

### Session Modes

chDB offers three interaction patterns, from stateless to fully persistent:

```
Mode 1: Stateless Query (simplest)
═══════════════════════════════════
  import chdb
  result = chdb.query("SELECT 1 + 1", "CSV")
  # No state persisted. Each call is independent.
  # Good for: one-off file queries, Jupyter exploration.

Mode 2: Session (temp tables, UDFs)
════════════════════════════════════
  from chdb import session
  sess = session.Session()
  sess.query("CREATE DATABASE analytics")
  sess.query("CREATE TABLE analytics.events (ts DateTime, event String) ENGINE=Memory")
  sess.query("INSERT INTO analytics.events VALUES (now(), 'click')")
  result = sess.query("SELECT * FROM analytics.events")
  # State lives in memory for the session lifetime.
  # Good for: multi-step analysis, temp staging tables.

Mode 3: Persistent Session (survives restarts)
═══════════════════════════════════════════════
  sess = session.Session(path="/data/chdb_state")
  sess.query("""
      CREATE TABLE IF NOT EXISTS metrics (
          ts DateTime,
          cpu Float64,
          host String
      ) ENGINE = MergeTree()
      ORDER BY (host, ts)
  """)
  # Data persisted to /data/chdb_state/ using MergeTree format.
  # Survives process restarts. Full ClickHouse storage engine.
  # Good for: embedded analytics in long-running services.
```

### DataStore API (chDB 4.0+)

The DataStore API provides a **lazy, Pandas-like** interface that compiles operations into optimized ClickHouse SQL under the hood:

```python
from chdb import datastore

# Lazy evaluation: nothing executes until .collect()
ds = datastore.DataStore("s3://bucket/events/*.parquet")

result = (
    ds
    .filter("event_date >= '2024-01-01'")     # → becomes PREWHERE
    .select("user_id", "event_name", "revenue")  # → column pruning
    .group_by("event_name")
    .agg(
        total_revenue="sum(revenue)",
        unique_users="uniqExact(user_id)"
    )
    .order_by("total_revenue", ascending=False)
    .limit(100)
    .collect()  # NOW it executes: compiles to SQL, runs vectorized engine
)

# Under the hood, this generated:
# SELECT event_name, sum(revenue) AS total_revenue,
#        uniqExact(user_id) AS unique_users
# FROM s3('s3://bucket/events/*.parquet', Parquet)
# PREWHERE event_date >= '2024-01-01'
# GROUP BY event_name
# ORDER BY total_revenue DESC
# LIMIT 100
```

---

## 5. Storage & Buffer Management

### DuckDB Buffer Manager

DuckDB's buffer manager is the key to being a "good memory citizen" — it must share the process's address space without starving the host application.

```
Buffer Manager Architecture:
════════════════════════════

  ┌──────────────────────────────────────────────────────────┐
  │  Application Process Memory                               │
  │                                                            │
  │  ┌────────────────────┐  ┌─────────────────────────────┐  │
  │  │  Application Heap  │  │  DuckDB Buffer Pool          │  │
  │  │  (your code, libs) │  │                               │  │
  │  │                    │  │  ┌─────┐┌─────┐┌─────┐       │  │
  │  │  Pandas DataFrames │  │  │Blk 0││Blk 1││Blk 2│ ...   │  │
  │  │  NumPy Arrays      │  │  │256KB││256KB││256KB│       │  │
  │  │  Your objects       │  │  └─────┘└─────┘└─────┘       │  │
  │  │                    │  │                               │  │
  │  │                    │  │  Memory limit: configurable    │  │
  │  │                    │  │  Default: 80% of available RAM │  │
  │  └────────────────────┘  │                               │  │
  │                          │  When full → LRU eviction:     │  │
  │                          │  least-recently-used blocks     │  │
  │                          │  written to temp directory      │  │
  │                          └───────────────┬─────────────┘  │
  └──────────────────────────────────────────┼────────────────┘
                                             │
                                             ▼
                                   ┌──────────────────┐
                                   │  Temp Directory    │
                                   │  /tmp/duckdb/      │
                                   │                    │
                                   │  Spilled blocks    │
                                   │  from evicted      │
                                   │  buffer pool       │
                                   │  entries           │
                                   └──────────────────┘

Memory Limit Configuration:
───────────────────────────
  import duckdb
  con = duckdb.connect()
  con.execute("SET memory_limit = '4GB'")        # hard cap
  con.execute("SET temp_directory = '/fast_ssd'") # spill location
  con.execute("SET threads = 8")                  # parallelism
```

### chDB Memory Model

chDB inherits ClickHouse's memory tracking system, which uses a hierarchical allocation tracker:

```
chDB Memory Hierarchy:
═════════════════════

  ┌──────────────────────────────────────────────┐
  │  Global Memory Tracker                        │
  │  max_memory_usage = 10GB (configurable)       │
  │                                                │
  │  ┌─────────────────────┐                      │
  │  │ Query 1 Tracker     │                      │
  │  │ max_memory_per_query│                      │
  │  │ = 2GB               │                      │
  │  │                     │                      │
  │  │ ┌────────────────┐  │                      │
  │  │ │ HashJoin: 800MB│  │                      │
  │  │ └────────────────┘  │                      │
  │  │ ┌────────────────┐  │                      │
  │  │ │ Aggregate: 200M│  │                      │
  │  │ └────────────────┘  │                      │
  │  └─────────────────────┘                      │
  │                                                │
  │  ┌─────────────────────┐                      │
  │  │ Query 2 Tracker     │                      │
  │  │ ...                 │                      │
  │  └─────────────────────┘                      │
  │                                                │
  │  If any tracker exceeds limit:                 │
  │  • ClickHouse throws MEMORY_LIMIT_EXCEEDED     │
  │  • chDB can optionally spill to disk           │
  │    (via external sort / partial merge)          │
  └──────────────────────────────────────────────┘
```

---

## 6. Query Execution Internals

### Push-Based vs Pull-Based Execution

```
Pull-Based (Volcano / Iterator Model):
═══════════════════════════════════════

  Result      "Give me a row"
    │           │
    ▼           │
  Sort ─────── next() ──────► Sort calls child.next()
    │                           │
    ▼                           ▼
  Join ─────── next() ──────► Join calls left.next(), right.next()
    │                           │
    ▼                           ▼
  Scan ─────── next() ──────► Scan reads one row from storage

  Problem: deep call stack per row, virtual dispatch overhead.


Push-Based (DuckDB's Model):
════════════════════════════

  Source operator PUSHES chunks through the pipeline:

  Scan ──push──► Filter ──push──► Project ──push──► Sink
   │                                                  │
   │   "Here's 2048 rows"  "Here's 1800 rows"         │
   │   (full vector)       (after filtering)           │
   │                                                   │
   └───────────── Pipeline executes as tight loop ─────┘

  Advantage: no virtual dispatch per row, compiler can
  optimize the entire pipeline as a single loop.
  Each operator processes a vector and passes it forward.
```

### Pipeline Breakers

Not all operators can work in a streaming fashion. Some need to see all data before producing output. These are "pipeline breakers":

```
Pipeline Breakers (materialize all input before producing output):
═════════════════════════════════════════════════════════════════

  ┌─────────────────┬──────────────────────────────────────────┐
  │ Operator        │ Why it breaks the pipeline               │
  ├─────────────────┼──────────────────────────────────────────┤
  │ Hash Join Build │ Must build entire hash table before       │
  │                 │ probe side can start                      │
  ├─────────────────┼──────────────────────────────────────────┤
  │ Sort            │ Must see all rows to determine order      │
  ├─────────────────┼──────────────────────────────────────────┤
  │ Hash Aggregate  │ Must process all groups before emitting   │
  │ (non-streaming) │ (but DuckDB can do streaming aggregates   │
  │                 │  for ORDER BY + LIMIT patterns)           │
  ├─────────────────┼──────────────────────────────────────────┤
  │ Window Function │ Needs full partition to compute rank/lag  │
  └─────────────────┴──────────────────────────────────────────┘


Example query with two pipelines:
──────────────────────────────────

  SELECT region, SUM(amount)
  FROM orders JOIN customers USING (customer_id)
  WHERE order_date > '2024-01-01'
  GROUP BY region
  ORDER BY SUM(amount) DESC;

  Pipeline 1:                Pipeline 2:
  ┌────────────────┐         ┌────────────────────────┐
  │ Scan(customers)│         │ Scan(orders)            │
  │       │        │         │ Filter(date > ...)      │
  │       ▼        │         │       │                 │
  │ HashJoin BUILD │ ══════► │ HashJoin PROBE          │
  │ (pipeline      │ hash    │       │                 │
  │  breaker)      │ table   │ HashAggregate           │
  └────────────────┘         │       │                 │
                             │ Sort                    │
                             │       │                 │
                             │ Result                  │
                             └────────────────────────┘
```

### ClickHouse Query Pipeline (chDB)

chDB uses ClickHouse's **query pipeline** execution model, which is also push-based but organized differently from DuckDB:

```
ClickHouse Query Pipeline (used by chDB):
═════════════════════════════════════════

  ┌─────────────────────────────────────────────────┐
  │  Query Pipeline                                  │
  │                                                   │
  │  ┌────────┐    ┌───────────┐    ┌────────────┐   │
  │  │ Source  │───►│ Transform │───►│ Transform  │   │
  │  │ (read   │    │ (filter)  │    │ (aggregate)│   │
  │  │  data)  │    │           │    │            │   │
  │  └────────┘    └───────────┘    └─────┬──────┘   │
  │                                        │          │
  │  ┌────────┐    ┌───────────┐    ┌─────▼──────┐   │
  │  │ Source  │───►│ Transform │───►│ Merge      │   │
  │  │ (read   │    │ (filter)  │    │ Transform  │   │
  │  │  data)  │    │           │    │            │   │
  │  └────────┘    └───────────┘    └─────┬──────┘   │
  │                                        │          │
  │                                  ┌─────▼──────┐   │
  │                                  │   Sink     │   │
  │                                  │ (output)   │   │
  │                                  └────────────┘   │
  │                                                   │
  │  Key difference: ClickHouse pipeline has separate  │
  │  "ports" for connect/disconnect, allowing dynamic  │
  │  DAG construction and parallel execution.          │
  └─────────────────────────────────────────────────┘
```

---

## 7. Out-of-Core Processing: Bigger Than RAM

This is where in-process OLAP truly shines. Both DuckDB and chDB can process datasets **far larger than available RAM** through careful streaming and spilling strategies.

### DuckDB's Approach

```
How DuckDB Handles 500GB Dataset with 16GB RAM:
════════════════════════════════════════════════

Scenario: SELECT region, SUM(revenue), COUNT(*)
          FROM 'sales_500gb/*.parquet'
          GROUP BY region

Step 1: Streaming Scan (never loads all data)
─────────────────────────────────────────────
  ┌──────────────────────────────┐
  │  Parquet File 1 (10GB)       │
  │  ┌──────────┐               │
  │  │ Row Group│ → Read ONLY   │   Only 2 columns read:
  │  │   0      │   "region"    │   "region" and "revenue"
  │  │          │   "revenue"   │   
  │  │          │   columns     │   Other 30 columns?
  │  └──────────┘               │   Never touch disk.
  │  ┌──────────┐               │
  │  │ Row Group│ → Zone map    │   Zone map says this group
  │  │   1      │   check:      │   has region="APAC" only.
  │  │          │   SKIP if     │   If query filters region='US',
  │  │          │   not needed  │   skip entirely.
  │  └──────────┘               │
  └──────────────────────────────┘

Step 2: Streaming Aggregation
─────────────────────────────
  Each morsel (2048 rows) flows through:
    Scan → Filter → Hash Aggregate

  Hash table stays small because GROUP BY region
  has low cardinality (~10 groups).

  Total memory used: ~1 MB for hash table + read buffers.


What if GROUP BY has high cardinality (1M groups)?
──────────────────────────────────────────────────

Step 2b: External Aggregation (spill to disk)
─────────────────────────────────────────────

  ┌──────────────────────────────────────────────────┐
  │  Phase 1: Build partial hash tables in memory     │
  │                                                    │
  │  ┌────────┐  ┌────────┐  ┌────────┐              │
  │  │ HT     │  │ HT     │  │ HT     │              │
  │  │ Part 0 │  │ Part 1 │  │ Part 2 │  ...          │
  │  │ 4 GB   │  │ 4 GB   │  │ 4 GB   │              │
  │  └───┬────┘  └───┬────┘  └───┬────┘              │
  │      │           │           │                     │
  │      ▼ spill     ▼ spill     ▼ spill              │
  │  ┌────────┐  ┌────────┐  ┌────────┐              │
  │  │ /tmp/  │  │ /tmp/  │  │ /tmp/  │              │
  │  │ part0  │  │ part1  │  │ part2  │              │
  │  └────────┘  └────────┘  └────────┘              │
  │                                                    │
  │  Phase 2: Merge spilled partitions                 │
  │  Load one partition at a time, merge, emit results │
  └──────────────────────────────────────────────────┘
```

### External Hash Join (DuckDB)

```
Join that exceeds memory:
═════════════════════════

  Build side (large): 200GB
  Available RAM: 16GB

  Phase 1: Partition both sides by join key hash
  ─────────────────────────────────────────────

  Build side → hash(key) % N → write to N partition files
  Probe side → hash(key) % N → write to N partition files

  ┌──────────┐    ┌───┬───┬───┬───┬───┐
  │Build data│───►│ P0│ P1│ P2│...│PN │  (on disk)
  └──────────┘    └───┴───┴───┴───┴───┘

  ┌──────────┐    ┌───┬───┬───┬───┬───┐
  │Probe data│───►│ P0│ P1│ P2│...│PN │  (on disk)
  └──────────┘    └───┴───┴───┴───┴───┘

  Phase 2: Join matching partitions (each fits in RAM)
  ────────────────────────────────────────────────────

  For i in 0..N:
    Load Build_Pi into hash table  (fits in 16GB)
    Stream Probe_Pi, probe hash table
    Emit matching rows
    Discard Build_Pi, move to next partition

  Result: Arbitrary-size joins with bounded memory.
```

### chDB's Approach

chDB inherits ClickHouse's external sorting and partial merge strategies:

```
chDB Out-of-Core Strategy:
══════════════════════════

  ClickHouse engine uses:

  1. External Sort:
     • Sort each block in memory
     • Spill sorted runs to disk
     • K-way merge sort across runs
     • Used for ORDER BY on large datasets

  2. Two-Level Aggregation:
     • First level: thread-local hash tables (parallel)
     • Second level: merge thread-local results
     • If too large: flush to disk, re-aggregate

  3. MergeTree Background Merges:
     • In persistent mode, data is written as sorted "parts"
     • Background threads merge parts (like LSM compaction)
     • Sorted data = excellent zone map effectiveness

  Key difference from DuckDB:
  ClickHouse was designed for server workloads with
  more aggressive memory usage. chDB inherits this,
  so it may be less "polite" as a memory citizen in
  constrained environments.
```

---

## 8. Zero-Copy Data Integration

### The Zero-Copy Pipeline

The killer feature of in-process OLAP is querying data structures that **already exist in your application's memory** without copying them.

```
Traditional ETL Pipeline (copy-heavy):
══════════════════════════════════════

  Python Pandas DF        Serialize          DB Server
  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
  │ 2GB DataFrame│──►│ Convert to   │──►│ Deserialize  │
  │ in memory    │   │ CSV/JSON/    │   │ Load into    │
  │              │   │ wire format  │   │ server tables│
  └──────────────┘   └──────────────┘   └──────┬───────┘
                                                │ query
                                                ▼
  Result (Python)     Deserialize          ┌──────────┐
  ┌──────────────┐   ┌──────────────┐      │ Execute  │
  │ Small result │◄──│ Parse wire   │◄─────│ query on │
  │              │   │ protocol     │      │ copy of  │
  └──────────────┘   └──────────────┘      │ data     │
                                           └──────────┘
  Total copies: 3 (serialize + deserialize + internal copy)
  Total overhead: seconds for 2GB dataset


Zero-Copy In-Process Pipeline (DuckDB/chDB):
════════════════════════════════════════════

  Python Pandas DF              DuckDB Engine
  ┌──────────────┐         ┌──────────────────────┐
  │ 2GB DataFrame│         │                      │
  │ in memory    │         │  Scan operator reads  │
  │              │◄────────│  DataFrame's memory   │
  │ [numpy array]│  ptr    │  directly via Arrow   │
  │ [numpy array]│  pass   │  columnar layout      │
  │ [numpy array]│         │                      │
  └──────────────┘         │  No copy. No convert. │
                           │  Same physical bytes. │
                           └──────────────────────┘
  Total copies: 0
  Total overhead: microseconds
```

### Apache Arrow: The Lingua Franca

Both DuckDB and chDB use Apache Arrow as their in-memory columnar format for zero-copy interop:

```
Apache Arrow Columnar Layout:
═════════════════════════════

  Logical table:
  ┌─────┬────────┬─────────┐
  │ id  │ name   │ amount  │
  ├─────┼────────┼─────────┤
  │  1  │ "Alice"│  100.50 │
  │  2  │ NULL   │  200.75 │
  │  3  │ "Carol"│  300.00 │
  └─────┴────────┴─────────┘

  Arrow in-memory representation:
  ┌─────────────────────────────────────────────────┐
  │  Column: "id" (Int32)                            │
  │  Validity bitmap: [1, 1, 1]                      │
  │  Values buffer:   [1, 2, 3]                      │
  │                    ▲                              │
  │                    │ contiguous, cache-friendly    │
  ├─────────────────────────────────────────────────┤
  │  Column: "name" (Utf8)                           │
  │  Validity bitmap: [1, 0, 1]  ← NULL at index 1  │
  │  Offsets buffer:  [0, 5, 5, 10]                  │
  │  Data buffer:     "AliceCarol"                   │
  ├─────────────────────────────────────────────────┤
  │  Column: "amount" (Float64)                      │
  │  Validity bitmap: [1, 1, 1]                      │
  │  Values buffer:   [100.50, 200.75, 300.00]       │
  └─────────────────────────────────────────────────┘

  This layout is the SAME in:
  • Pandas (with Arrow backend)
  • Polars (native Arrow)
  • DuckDB (internal vector format)
  • chDB (via Arrow output format)
  • PyArrow tables

  → Any of these can read each other's data with ZERO copies.
```

### Practical Zero-Copy Examples

```python
import duckdb
import pandas as pd
import polars as pl
import pyarrow as pa

# ═══════════════════════════════════════════════════
# Example 1: Query Pandas DataFrame with SQL (DuckDB)
# ═══════════════════════════════════════════════════

# 50 million rows in Pandas — imagine this came from your ETL
sales_df = pd.DataFrame({
    'region': ['US', 'EU', 'APAC'] * 16_666_667,
    'revenue': [100.0, 200.0, 150.0] * 16_666_667,
    'product_id': range(50_000_000)
})

# DuckDB reads sales_df DIRECTLY from Pandas memory.
# No copy. No conversion. Zero overhead.
result = duckdb.sql("""
    SELECT region,
           SUM(revenue) AS total,
           COUNT(*) AS cnt,
           AVG(revenue) AS avg_rev
    FROM sales_df
    GROUP BY region
    ORDER BY total DESC
""").df()  # .df() returns result as Pandas DataFrame


# ═══════════════════════════════════════════════════
# Example 2: Query Polars DataFrame with SQL (DuckDB)
# ═══════════════════════════════════════════════════

events_pl = pl.scan_parquet("s3://bucket/events/*.parquet")
events_df = events_pl.collect()  # Polars DataFrame

# DuckDB scans Polars DataFrames via Arrow zero-copy
result = duckdb.sql("""
    SELECT event_type, COUNT(*) as cnt
    FROM events_df
    WHERE event_date >= '2024-01-01'
    GROUP BY event_type
    HAVING cnt > 1000
""").pl()  # .pl() returns result as Polars DataFrame


# ═══════════════════════════════════════════════════
# Example 3: PyArrow integration (both DuckDB and chDB)
# ═══════════════════════════════════════════════════

# Read Parquet into Arrow table
arrow_table = pa.parquet.read_table('large_dataset.parquet')

# DuckDB: query Arrow table directly
duckdb.sql("SELECT * FROM arrow_table WHERE id > 1000 LIMIT 10")

# chDB: query Arrow table via conversion
import chdb
result = chdb.query(
    "SELECT * FROM arrow('arrow_table') WHERE id > 1000 LIMIT 10",
    "Arrow"
)


# ═══════════════════════════════════════════════════
# Example 4: chDB querying in-memory data
# ═══════════════════════════════════════════════════

import chdb

# Query a Pandas DataFrame
result = chdb.query(
    "SELECT region, sum(revenue) FROM Python(sales_df) GROUP BY region",
    "Dataframe"
)

# Query Parquet files on S3 (no download needed — streaming read)
result = chdb.query("""
    SELECT event_name, count()
    FROM s3('s3://bucket/events/year=2024/**/*.parquet')
    GROUP BY event_name
    ORDER BY count() DESC
    LIMIT 20
""", "PrettyCompact")
```

---

## 9. Concurrency & Transaction Model

### DuckDB: Full ACID with MVCC

DuckDB provides **serializable** transaction isolation with multi-version concurrency control, which is unusual for an embedded analytical database.

```
DuckDB Concurrency Model:
═════════════════════════

  ┌───────────────────────────────────────────────────────┐
  │  Concurrency Rules:                                    │
  │                                                         │
  │  • Multiple concurrent READERS: ✓ (always allowed)     │
  │  • Single WRITER at a time: ✓ (serialized writes)      │
  │  • Readers don't block Writers: ✓ (MVCC snapshots)     │
  │  • Writers don't block Readers: ✓ (readers see old     │
  │                                     consistent version) │
  │                                                         │
  │  Isolation level: SNAPSHOT (default)                    │
  │                                                         │
  │  This means:                                            │
  │  Thread 1 (reader): sees consistent snapshot of DB      │
  │  Thread 2 (reader): sees consistent snapshot of DB      │
  │  Thread 3 (writer): writes don't appear in readers      │
  │                      until COMMIT                       │
  └───────────────────────────────────────────────────────┘

  Write Concurrency:
  ──────────────────
  ┌────────────┐     ┌────────────┐     ┌────────────┐
  │ Thread A   │     │ Thread B   │     │ Thread C   │
  │ BEGIN      │     │ BEGIN      │     │ SELECT ... │
  │ INSERT ... │     │ INSERT ... │     │ (reads old │
  │ COMMIT ✓   │     │ WAIT...    │     │  snapshot) │
  └────────────┘     │ COMMIT ✓   │     └────────────┘
                     └────────────┘
                     (acquired write
                      lock after A
                      committed)
```

### chDB: Depends on Table Engine

chDB inherits ClickHouse's per-engine concurrency semantics:

```
chDB Concurrency by Engine:
═══════════════════════════

  ┌────────────────┬─────────────────────────────────────┐
  │ Engine         │ Concurrency Model                    │
  ├────────────────┼─────────────────────────────────────┤
  │ Memory         │ Single-writer, lock-free reads       │
  │                │ Best for: temp tables, sessions      │
  ├────────────────┼─────────────────────────────────────┤
  │ MergeTree      │ Lock-free inserts (append-only parts)│
  │                │ Background merges are non-blocking   │
  │                │ Reads see consistent "parts" list    │
  │                │ Best for: persistent analytics       │
  ├────────────────┼─────────────────────────────────────┤
  │ File           │ Read-only (Parquet, CSV, JSON)       │
  │                │ Each query gets independent reader   │
  ├────────────────┼─────────────────────────────────────┤
  │ S3 / URL       │ Read-only, stateless                 │
  │                │ Each query opens own HTTP connection  │
  └────────────────┴─────────────────────────────────────┘

  Key difference from DuckDB:
  • No traditional ACID transactions in chDB/ClickHouse
  • Writes are "eventually consistent" within parts
  • Designed for append-heavy analytical workloads
  • Not suitable for OLTP-style row-level updates
```

---

## 10. Production Use Cases & Patterns

### Pattern 1: Embedded Analytics in Microservices

```
Scenario: A SaaS app that shows real-time dashboards.
Instead of a separate analytics DB, embed DuckDB in each service.

┌──────────────────────────────────────────────────┐
│  Order Service (Python / FastAPI)                  │
│                                                    │
│  ┌────────────────┐    ┌───────────────────────┐  │
│  │ API Handler    │    │ DuckDB (in-process)    │  │
│  │                │    │                         │  │
│  │ POST /orders   │───►│ INSERT into orders.ddb │  │
│  │ GET /dashboard │───►│ SELECT region, SUM()   │  │
│  │                │◄───│ FROM orders             │  │
│  │  (sub-ms       │    │ GROUP BY region         │  │
│  │   response)    │    │ ORDER BY SUM() DESC     │  │
│  └────────────────┘    └───────────────────────┘  │
│                                                    │
│  Benefits:                                         │
│  • No network hop to analytics DB                  │
│  • Dashboard queries < 10ms (instead of 500ms+)    │
│  • No additional infrastructure to manage           │
│  • Scales: each service instance has its own DuckDB │
└──────────────────────────────────────────────────┘
```

### Pattern 2: Data Science Notebooks

```python
# Jupyter Notebook: Analyze 100GB of Parquet data on a laptop

import duckdb

# No cluster needed. No Spark. No warehouse.
duckdb.sql("""
    -- Query 100GB of partitioned Parquet files
    -- DuckDB reads only needed columns and partitions
    CREATE OR REPLACE VIEW events AS
    SELECT * FROM read_parquet(
        'data/events/year=*/month=*/*.parquet',
        hive_partitioning = true
    );

    -- Funnel analysis
    WITH step1 AS (
        SELECT DISTINCT user_id
        FROM events
        WHERE event_name = 'page_view'
          AND year = 2024
    ),
    step2 AS (
        SELECT DISTINCT user_id
        FROM events
        WHERE event_name = 'add_to_cart'
          AND year = 2024
          AND user_id IN (SELECT user_id FROM step1)
    ),
    step3 AS (
        SELECT DISTINCT user_id
        FROM events
        WHERE event_name = 'purchase'
          AND year = 2024
          AND user_id IN (SELECT user_id FROM step2)
    )
    SELECT
        (SELECT COUNT(*) FROM step1) AS page_views,
        (SELECT COUNT(*) FROM step2) AS add_to_cart,
        (SELECT COUNT(*) FROM step3) AS purchases,
        ROUND(100.0 * (SELECT COUNT(*) FROM step3) /
                       (SELECT COUNT(*) FROM step1), 2) AS conversion_rate;
""").show()

# Result in ~5 seconds on a modern laptop.
# Same query on Spark: 45+ seconds (cluster startup alone).
```

### Pattern 3: ETL and Data Pipeline Testing

```python
# CI/CD pipeline: validate data transformations with DuckDB

import duckdb
import pytest

@pytest.fixture
def db():
    """Create an in-memory DuckDB for each test — instant, isolated."""
    con = duckdb.connect(':memory:')
    # Load test fixtures
    con.execute("""
        CREATE TABLE raw_events AS
        SELECT * FROM read_csv('tests/fixtures/events.csv')
    """)
    return con


def test_revenue_aggregation(db):
    """Verify revenue calculation logic."""
    result = db.execute("""
        SELECT region, SUM(amount * quantity) AS revenue
        FROM raw_events
        WHERE status = 'completed'
        GROUP BY region
    """).fetchall()

    assert len(result) > 0
    for region, revenue in result:
        assert revenue > 0, f"Region {region} has zero revenue"


def test_deduplication(db):
    """Verify event deduplication logic."""
    result = db.execute("""
        WITH deduped AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY event_id ORDER BY ts DESC
            ) AS rn
            FROM raw_events
        )
        SELECT COUNT(*) AS dupes
        FROM deduped
        WHERE rn > 1
    """).fetchone()

    assert result[0] == 0, f"Found {result[0]} duplicate events"
```

### Pattern 4: Edge Computing / IoT Analytics

```
Scenario: Analytics on edge devices (Raspberry Pi, ARM servers)

┌─────────────────────────────────────────────┐
│  Edge Device (Raspberry Pi 5, 8GB RAM)       │
│                                               │
│  ┌───────────────┐    ┌──────────────────┐   │
│  │ Sensor Data   │    │ DuckDB           │   │
│  │ Collector     │───►│ (embedded)        │   │
│  │               │    │                    │   │
│  │ 10K events/s  │    │ Local aggregation: │   │
│  │ from sensors  │    │ • 5-min rollups    │   │
│  │               │    │ • Anomaly flags    │   │
│  └───────────────┘    │ • Threshold alerts │   │
│                       └────────┬───────────┘   │
│                                │                │
│  Only aggregated results       │                │
│  sent to cloud (save BW)      │                │
└────────────────────────────────┼────────────────┘
                                 │  HTTPS (compressed)
                                 ▼
                     ┌──────────────────────┐
                     │  Cloud Data Warehouse │
                     │  (receives summaries  │
                     │   not raw data)       │
                     └──────────────────────┘
```

### Pattern 5: chDB as a ClickHouse Dev/Test Environment

```python
# Local development: test ClickHouse queries without a server

from chdb import session

# Same MergeTree tables you'd use in production ClickHouse
sess = session.Session()

sess.query("""
    CREATE TABLE events (
        event_time DateTime,
        user_id UInt64,
        event_type LowCardinality(String),
        properties String
    ) ENGINE = MergeTree()
    PARTITION BY toYYYYMM(event_time)
    ORDER BY (event_type, user_id, event_time)
""")

# Insert test data
sess.query("""
    INSERT INTO events
    SELECT
        now() - number * 60 AS event_time,
        rand() % 10000 AS user_id,
        ['click', 'view', 'purchase'][rand() % 3 + 1] AS event_type,
        '{}' AS properties
    FROM numbers(1000000)
""")

# Test your production query locally
result = sess.query("""
    SELECT
        event_type,
        count() AS cnt,
        uniqExact(user_id) AS unique_users,
        cnt / unique_users AS events_per_user
    FROM events
    WHERE event_time >= now() - INTERVAL 7 DAY
    GROUP BY event_type
    ORDER BY cnt DESC
""", "PrettyCompact")

print(result)
# ┌────────────┬────────┬──────────────┬─────────────────┐
# │ event_type │    cnt │ unique_users │ events_per_user │
# ├────────────┼────────┼──────────────┼─────────────────┤
# │ view       │ 333521 │         9987 │              33 │
# │ click      │ 333240 │         9991 │              33 │
# │ purchase   │ 333239 │         9988 │              33 │
# └────────────┴────────┴──────────────┴─────────────────┘
```

---

## 11. DuckDB vs chDB vs Polars: When to Use What

### Decision Matrix

| Criterion | DuckDB | chDB | Polars |
|-----------|--------|------|--------|
| **Primary interface** | SQL | SQL (ClickHouse dialect) | DataFrame API |
| **Language** | C++ | C++ (ClickHouse core) | Rust |
| **Python install** | `pip install duckdb` | `pip install chdb` | `pip install polars` |
| **SQL compatibility** | PostgreSQL-like | ClickHouse SQL | Limited SQL via `.sql()` |
| **ACID transactions** | ✅ Full MVCC | ❌ Append-only | ❌ Not a database |
| **Persistent storage** | ✅ `.duckdb` file | ✅ MergeTree on disk | ❌ (not a database) |
| **Parquet query** | ✅ Native, excellent | ✅ Via table functions | ✅ Native, excellent |
| **S3/HTTP remote** | ✅ httpfs extension | ✅ s3() / url() | ✅ Via connectors |
| **Zero-copy Pandas** | ✅ | ✅ (chDB 4.0+) | ✅ |
| **Zero-copy Arrow** | ✅ | ✅ | ✅ (native format) |
| **Out-of-core** | ✅ Excellent (buffer mgr) | ⚠️ Limited spilling | ✅ Streaming/lazy |
| **WASM support** | ✅ (runs in browser!) | ❌ | ❌ |
| **Array functions** | Basic | ✅ Excellent (lambda) | ✅ Excellent |
| **Window functions** | ✅ Full | ✅ Full | ✅ Full |
| **Ecosystem path** | DuckDB → MotherDuck (cloud) | chDB → ClickHouse Cloud | Standalone |
| **License** | MIT | Apache 2.0 | MIT |

### Decision Flowchart

```
Start: "I need to analyze data in-process"
│
├── Do you prefer SQL or DataFrame API?
│   │
│   ├── SQL ─────────────────────────────────────┐
│   │                                             │
│   │   ├── Are you already using ClickHouse     │
│   │   │   in production?                       │
│   │   │   │                                     │
│   │   │   ├── YES → chDB                        │
│   │   │   │   (same SQL, same engines,          │
│   │   │   │    seamless migration path)          │
│   │   │   │                                      │
│   │   │   └── NO → Do you need ACID             │
│   │   │        transactions?                     │
│   │   │        │                                 │
│   │   │        ├── YES → DuckDB                  │
│   │   │        │   (only in-process OLAP with    │
│   │   │        │    true ACID support)            │
│   │   │        │                                  │
│   │   │        └── NO → Either works,            │
│   │   │              but DuckDB has larger        │
│   │   │              community and more           │
│   │   │              extensions                   │
│   │   │                                           │
│   │   └── Do you need advanced array/lambda      │
│   │       functions in SQL?                       │
│   │       │                                       │
│   │       ├── YES → chDB (ClickHouse excels here)│
│   │       └── NO → DuckDB                        │
│   │                                               │
│   └── DataFrame API ──────────────────────────────┘
│       │
│       └── Polars
│           (fastest DataFrame library, Rust-native,
│            lazy evaluation, streaming)
│
└── Special cases:
    │
    ├── Need to run in browser (WASM)? → DuckDB
    ├── Need to run on Raspberry Pi? → DuckDB
    ├── Need ClickHouse table engines? → chDB
    ├── Need 70+ data formats? → chDB
    └── Building data pipelines in Rust? → Polars
```

### Performance Characteristics

```
Benchmark Context (approximate, workload-dependent):
════════════════════════════════════════════════════

Task: TPC-H SF10 (10GB), single machine, 16 cores
──────────────────────────────────────────────────

  ┌───────────────────────────────────────────────┐
  │ Simple aggregation (Q1):                       │
  │   DuckDB:  ~0.8s                               │
  │   chDB:    ~0.9s                               │
  │   Polars:  ~0.7s                               │
  │   Pandas:  ~12s                                │
  │   Spark:   ~8s (+ 30s startup)                 │
  ├───────────────────────────────────────────────┤
  │ Complex multi-join (Q9):                       │
  │   DuckDB:  ~2.5s                               │
  │   chDB:    ~3.1s                               │
  │   Polars:  ~2.8s                               │
  │   Pandas:  OOM at 10GB                         │
  │   Spark:   ~15s (+ 30s startup)                │
  ├───────────────────────────────────────────────┤
  │ String processing (Q13):                       │
  │   DuckDB:  ~1.2s                               │
  │   chDB:    ~0.9s  (ClickHouse string funcs)    │
  │   Polars:  ~1.5s                               │
  └───────────────────────────────────────────────┘

  Key takeaway: All three are within 2x of each other.
  The "right" choice depends on your ecosystem, not raw speed.
```

---

## 12. Anti-Patterns & Pitfalls

### ❌ Anti-Pattern 1: Using In-Process OLAP for OLTP

```
WRONG:
══════
  # Using DuckDB as your primary transactional database
  for order in incoming_orders:
      duckdb.sql(f"INSERT INTO orders VALUES ({order.id}, ...)")
      duckdb.sql(f"UPDATE inventory SET qty = qty - 1 WHERE id = {order.product_id}")

  Problems:
  • Single-writer lock serializes all writes
  • Columnar storage is terrible for single-row updates
  • No row-level locking
  • No replication / failover

  USE INSTEAD: PostgreSQL, MySQL, SQLite for OLTP workloads.
```

### ❌ Anti-Pattern 2: Ignoring Memory Limits

```python
# WRONG: Let DuckDB use all system memory
con = duckdb.connect()
# Default memory_limit = 80% of RAM. If your app also needs RAM, you'll OOM.

# RIGHT: Set explicit memory limits
con = duckdb.connect()
con.execute("SET memory_limit = '4GB'")
con.execute("SET temp_directory = '/fast_ssd/duckdb_tmp'")

# RIGHT for chDB:
import chdb
result = chdb.query(
    "SELECT ...",
    settings={"max_memory_usage": "4000000000"}  # 4GB
)
```

### ❌ Anti-Pattern 3: SELECT * on Wide Tables

```sql
-- WRONG: Defeats columnar storage advantage
SELECT * FROM events;  -- reads all 100 columns

-- RIGHT: Project only needed columns
SELECT user_id, event_name, timestamp
FROM events;  -- reads only 3 columns, 30x less I/O
```

### ❌ Anti-Pattern 4: Not Sorting Data for Zone Maps

```
WRONG: Insert data in random order
═══════════════════════════════════
  Row Group 0: dates [2020-01-05, 2024-12-31, 2021-06-15, ...]
  Row Group 1: dates [2023-03-20, 2020-08-11, 2024-01-01, ...]
  Row Group 2: dates [2021-11-30, 2022-07-04, 2024-06-15, ...]

  Query: WHERE date = '2024-06-15'
  Zone maps: ALL row groups contain the range → must scan ALL data.

RIGHT: Sort data on filter columns during ingestion
═══════════════════════════════════════════════════
  Row Group 0: dates [2020-01-01, ..., 2021-06-30]
  Row Group 1: dates [2021-07-01, ..., 2023-01-31]
  Row Group 2: dates [2023-02-01, ..., 2024-12-31]  ← only this scanned

  Query: WHERE date = '2024-06-15'
  Zone maps: Only Row Group 2 matches → 67% of data skipped.

  -- DuckDB: sort during COPY
  COPY (SELECT * FROM raw_data ORDER BY date)
  TO 'sorted_data.parquet';

  -- chDB: MergeTree sorts automatically by ORDER BY key
  CREATE TABLE events (...) ENGINE = MergeTree()
  ORDER BY (event_date, user_id);
```

### ❌ Anti-Pattern 5: Running In-Process OLAP in Multi-Tenant Services

```
WRONG:
══════
  # Shared DuckDB instance across all API requests
  global_db = duckdb.connect('shared.duckdb')

  @app.route('/analytics/<tenant_id>')
  def analytics(tenant_id):
      return global_db.sql(f"SELECT ... WHERE tenant = '{tenant_id}'")

  Problems:
  • Single writer → write bottleneck across tenants
  • One tenant's heavy query starves others
  • No query-level resource isolation
  • SQL injection risk (string formatting!)

RIGHT: Use per-request connections or read-only mode
════════════════════════════════════════════════════
  db = duckdb.connect('shared.duckdb', read_only=True)

  @app.route('/analytics/<tenant_id>')
  def analytics(tenant_id):
      # Each request gets its own cursor (thread-safe reads)
      cursor = db.cursor()
      return cursor.execute(
          "SELECT ... WHERE tenant = ?", [tenant_id]
      ).fetchdf()
```

---

## 13. The Bigger Picture: Where In-Process OLAP Fits

### The Modern Data Stack Spectrum

```
                    Complexity & Scale
    ─────────────────────────────────────────────►

    │  In-Process OLAP     │  Single-Node     │  Distributed
    │  (DuckDB, chDB)      │  Server OLAP     │  OLAP Cluster
    │                       │  (ClickHouse,    │  (Snowflake,
    │                       │   Postgres OLAP) │   BigQuery,
    │                       │                  │   Databricks)
    │                       │                  │
    ├───────────────────────┼──────────────────┼──────────────
    │ Data size: GB-100s GB │ 100s GB - 10 TB  │ 10 TB - PBs
    │ Users: 1 (embedded)   │ 1-50 concurrent  │ 100s-1000s
    │ Infra: none           │ Single server    │ Cluster/Cloud
    │ Latency: sub-second   │ Seconds          │ Seconds-mins
    │ Cost: $0              │ $100s/month      │ $1000s+/month
    │ Setup: pip install    │ Docker/apt-get   │ Cloud console
    │                       │                  │
    │ Best for:             │ Best for:        │ Best for:
    │ • Data science        │ • Internal       │ • Enterprise
    │ • Prototyping         │   dashboards     │   analytics
    │ • Edge computing      │ • Mid-size       │ • Multi-PB
    │ • CI/CD tests         │   analytics      │   data lakes
    │ • Embedded analytics  │ • API backends   │ • Compliance
    │ • Microservices       │                  │
    └───────────────────────┴──────────────────┴──────────────

    The "graduation path":
    ┌──────────┐     ┌───────────────┐     ┌──────────────────┐
    │ DuckDB   │────►│ MotherDuck    │────►│ Snowflake /      │
    │ (local)  │     │ (cloud DuckDB)│     │ BigQuery         │
    └──────────┘     └───────────────┘     └──────────────────┘

    ┌──────────┐     ┌───────────────┐     ┌──────────────────┐
    │ chDB     │────►│ ClickHouse    │────►│ ClickHouse Cloud │
    │ (local)  │     │ (server)      │     │ (managed)        │
    └──────────┘     └───────────────┘     └──────────────────┘
```

### When NOT to Use In-Process OLAP

| Scenario | Why Not | Use Instead |
|----------|---------|-------------|
| **Multi-user concurrent access** | Single-writer, no query isolation | ClickHouse, Snowflake |
| **Petabyte-scale data** | Single machine bottleneck | BigQuery, Databricks |
| **High-availability requirement** | No replication, single process | Distributed DB cluster |
| **OLTP workloads** | Columnar storage, no row-level locks | PostgreSQL, MySQL |
| **Real-time streaming ingestion** | Not designed for continuous ingestion | Kafka + ClickHouse |
| **Multi-tenant SaaS analytics** | No resource isolation between tenants | Dedicated OLAP service |

### When In-Process OLAP Is the BEST Choice

| Scenario | Why Best | Recommended |
|----------|----------|-------------|
| **Data science / ML notebooks** | Zero setup, query files directly | DuckDB |
| **Embedded analytics in apps** | No infrastructure dependency | DuckDB |
| **CI/CD data validation** | In-memory, instant, disposable | DuckDB |
| **Prototyping ClickHouse queries** | Same engine, no server needed | chDB |
| **Edge / IoT analytics** | Runs on ARM, low resource usage | DuckDB |
| **Replacing Pandas for analytics** | 100x faster, SQL interface | DuckDB or Polars |
| **Local file exploration** | Query Parquet/CSV without loading | DuckDB or chDB |
| **Microservice-embedded OLAP** | Zero network hop, sub-ms response | DuckDB |
| **WASM / browser-based analytics** | DuckDB-WASM runs in browsers | DuckDB |

### Summary: The Staff-Engineer Mental Model

```
How to think about in-process OLAP:
═══════════════════════════════════

  It's NOT a replacement for your production database.
  It IS a replacement for "spin up a Spark cluster to count rows."

  ┌─────────────────────────────────────────────────────────┐
  │  Before in-process OLAP:                                 │
  │                                                           │
  │  "I need to analyze 50GB of Parquet files"                │
  │  → Spin up EMR cluster ($$$, 15 min startup)             │
  │  → Or load into Redshift ($$$, hours of ETL)             │
  │  → Or suffer with Pandas (OOM, slow)                     │
  │                                                           │
  │  After in-process OLAP:                                   │
  │                                                           │
  │  "I need to analyze 50GB of Parquet files"                │
  │  → pip install duckdb                                     │
  │  → duckdb.sql("SELECT ... FROM '*.parquet'")             │
  │  → Done in 5 seconds. $0. Zero infrastructure.           │
  └─────────────────────────────────────────────────────────┘

  The real revolution is not speed — it's the elimination
  of operational complexity for 80% of analytical workloads.
```

---

## Quick Reference: Getting Started

```python
# ═══════════════════════════════════════════════════════════
# DuckDB: 5 lines to query any data
# ═══════════════════════════════════════════════════════════
import duckdb

# Query local Parquet
duckdb.sql("SELECT * FROM 'data/*.parquet' LIMIT 10").show()

# Query remote Parquet on S3
duckdb.sql("SELECT count(*) FROM read_parquet('s3://bucket/path/*.parquet')").show()

# Query Pandas DataFrame
import pandas as pd
df = pd.read_csv('big_file.csv')
duckdb.sql("SELECT col1, SUM(col2) FROM df GROUP BY col1").show()

# Persistent database
con = duckdb.connect('my_analytics.duckdb')
con.sql("CREATE TABLE t AS SELECT * FROM 'data/*.csv'")
con.sql("SELECT * FROM t WHERE date > '2024-01-01'").show()


# ═══════════════════════════════════════════════════════════
# chDB: 5 lines to use ClickHouse without a server
# ═══════════════════════════════════════════════════════════
import chdb

# Stateless query
print(chdb.query("SELECT version()", "CSV"))

# Query local Parquet
print(chdb.query("SELECT * FROM file('data/*.parquet') LIMIT 10", "Pretty"))

# Query S3
print(chdb.query("SELECT count() FROM s3('s3://bucket/path/*.parquet')", "CSV"))

# Persistent session with MergeTree
from chdb import session
sess = session.Session(path="./my_chdb_data")
sess.query("CREATE TABLE t (id UInt64, val String) ENGINE=MergeTree() ORDER BY id")
sess.query("INSERT INTO t VALUES (1, 'hello'), (2, 'world')")
print(sess.query("SELECT * FROM t", "PrettyCompact"))
```

---

*This chapter complements [08-olap-databases.md](file:///Users/harut/system-design/databases/08-olap-databases.md) (which covers OLAP architecture broadly) by going deep on the in-process paradigm. For columnar storage internals, see [02-data-storage-formats-and-encoding.md](file:///Users/harut/system-design/databases/02-data-storage-formats-and-encoding.md). For query engine fundamentals, see [04-query-engine-internals.md](file:///Users/harut/system-design/databases/04-query-engine-internals.md).*
