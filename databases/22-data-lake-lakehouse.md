# Data Lake & Lakehouse: Parquet, Iceberg, Delta Lake — A Staff-Engineer Deep Dive

A comprehensive guide to the data lake and lakehouse paradigm — how open table formats bring ACID transactions, schema evolution, and time travel to petabyte-scale analytics on cheap object storage. Covers the Parquet file format internals, Apache Iceberg, Delta Lake, and Apache Hudi from first principles, with production architecture patterns.

---

## Table of Contents

1. [Why Data Lakes Exist](#1-why-data-lakes-exist)
2. [The Parquet File Format: Internals](#2-the-parquet-file-format-internals)
3. [From Data Lake to Data Lakehouse](#3-from-data-lake-to-data-lakehouse)
4. [Apache Iceberg Deep Dive](#4-apache-iceberg-deep-dive)
5. [Delta Lake Deep Dive](#5-delta-lake-deep-dive)
6. [Apache Hudi Deep Dive](#6-apache-hudi-deep-dive)
7. [Object Storage as the Foundation](#7-object-storage-as-the-foundation)
8. [Catalog and Metadata Services](#8-catalog-and-metadata-services)
9. [Query Engine Integration](#9-query-engine-integration)
10. [Write Patterns and Ingestion](#10-write-patterns-and-ingestion)
11. [Compaction, Optimization, and Maintenance](#11-compaction-optimization-and-maintenance)
12. [Schema Evolution and Partition Evolution](#12-schema-evolution-and-partition-evolution)
13. [Time Travel, Branching, and Auditing](#13-time-travel-branching-and-auditing)
14. [Performance: Predicate Pushdown, Pruning, and Statistics](#14-performance-predicate-pushdown-pruning-and-statistics)
15. [Production Architecture Patterns](#15-production-architecture-patterns)
16. [Table Format Comparison and Decision Framework](#16-table-format-comparison-and-decision-framework)
17. [Anti-Patterns and Pitfalls](#17-anti-patterns-and-pitfalls)

---

## 1. Why Data Lakes Exist

### The Two-System Problem

For decades, organizations ran two parallel data systems: an **OLTP database** for operations and a **data warehouse** for analytics. Data flowed one way — ETL jobs moved rows nightly from OLTP into the warehouse. This worked but created problems.

```
The Two-System Era (2000s–2010s):
══════════════════════════════════

 ┌────────────────┐   ETL    ┌──────────────────────┐
 │  OLTP Database │ ──────►  │  Data Warehouse      │
 │  (PostgreSQL,  │  nightly │  (Teradata, Oracle    │
 │   MySQL)       │  batch   │   Exadata, Netezza)   │
 └────────────────┘          └──────────────────────┘

 Problems:
  ① Data is stale (nightly batch = hours behind)
  ② Warehouses are expensive ($100K–$1M+/year licensing)
  ③ Rigid schema — warehouse enforces strict types on ingest
  ④ Unstructured data (logs, JSON, images) doesn't fit
  ⑤ Vendor lock-in (proprietary storage formats)
```

### The Data Lake: Cheap Storage, Open Formats

The insight: dump **everything** — structured, semi-structured, unstructured — into object storage (S3, GCS, ADLS) in open file formats. Query it later with whatever engine you choose.

```
The Data Lake Era (2010s):
══════════════════════════

 ┌──────────────────────────────────────────────────────┐
 │           Object Storage (S3 / HDFS)                 │
 │                                                      │
 │  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  │
 │  │ CSV  │  │ JSON │  │Parqt.│  │ Avro │  │ ORC  │  │
 │  │files │  │files │  │files │  │files │  │files │  │
 │  └──────┘  └──────┘  └──────┘  └──────┘  └──────┘  │
 │                                                      │
 │  ┌──────────────┐  ┌────────────────┐  ┌─────────┐  │
 │  │  Log files   │  │  Images/Video  │  │  Avro   │  │
 │  │  (GB/day)    │  │  (ML training) │  │  events │  │
 │  └──────────────┘  └────────────────┘  └─────────┘  │
 └──────────────────────────────────────────────────────┘
                        │
         ┌──────────────┼──────────────┐
         ▼              ▼              ▼
    ┌─────────┐   ┌──────────┐   ┌─────────┐
    │  Spark  │   │  Presto  │   │  Hive   │
    │ (batch) │   │  (ad-hoc)│   │ (SQL)   │
    └─────────┘   └──────────┘   └─────────┘

 Wins:
  ✓ Storage cost: ~$23/TB/month (S3) vs ~$500+/TB/month (warehouse)
  ✓ Store everything (structured + unstructured)
  ✓ Open formats — no vendor lock-in
  ✓ Decouple storage from compute (scale independently)

 What went wrong — the "data swamp":
  ✗ No ACID transactions (concurrent writers corrupt data)
  ✗ No schema enforcement (garbage in, garbage out)
  ✗ No isolation — a reader sees partially written files
  ✗ No update/delete (only append; GDPR compliance = nightmare)
  ✗ No time travel or audit trail
  ✗ Stale metadata — the Hive Metastore knows the partition list,
    but not which files are valid
  ✗ Small-file problem — streaming ingest creates millions of tiny files
```

### The Key Insight: A Metadata Layer Fixes Everything

The problem was never the storage or the file format. It was the lack of a **transaction log** on top of the files. Add a metadata layer that tracks which files are part of a table, and you get ACID, schema enforcement, and time travel — without changing the storage.

```
What a table format adds:
═════════════════════════

  Before (raw data lake):        After (lakehouse table format):
  ─────────────────────          ──────────────────────────────

  s3://bucket/sales/             s3://bucket/sales/
  ├── file-001.parquet           ├── metadata/
  ├── file-002.parquet           │   ├── v1.metadata.json
  ├── file-003.parquet           │   ├── snap-1.avro
  ├── file-004.parquet           │   └── manifest-*.avro
  └── ??? which are valid ???    ├── data/
                                 │   ├── file-001.parquet  ✓ tracked
                                 │   ├── file-002.parquet  ✓ tracked
                                 │   └── file-003.parquet  ✓ tracked
                                 └── file-004.parquet      ✗ orphaned/deleted

  No schema → enforced schema    Read: "give me snapshot 7" → exact file list
  No txn   → ACID commits        Write: atomic swap of metadata pointer
  No audit → full version history
```

---

## 2. The Parquet File Format: Internals

Parquet is the dominant file format in the lakehouse ecosystem. Every table format (Iceberg, Delta, Hudi) stores data in Parquet files. Understanding Parquet internals is foundational.

### Design Goals

Parquet was designed at Twitter and Cloudera (2013) for one purpose: make analytical queries over massive datasets fast by reading only the columns and rows you need.

### Physical Layout

```
Parquet File Layout:
════════════════════

┌──────────────────────────────────────────────────────────────┐
│  Magic Number: "PAR1" (4 bytes)                              │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  Row Group 0                                                 │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Column Chunk: user_id (INT64)                         │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │  Page 0 (Data Page)                              │  │  │
│  │  │  ┌──────────┬──────────┬──────────────────────┐  │  │  │
│  │  │  │ Page Hdr │ Rep/Def  │ Encoded Values       │  │  │  │
│  │  │  │ (type,   │ Levels   │ (RLE, DELTA,         │  │  │  │
│  │  │  │  count,  │          │  DICTIONARY, PLAIN)  │  │  │  │
│  │  │  │  codec)  │          │                      │  │  │  │
│  │  │  └──────────┴──────────┴──────────────────────┘  │  │  │
│  │  │  Page 1 (Data Page)                              │  │  │
│  │  │  └─── ...                                        │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  │  Column Chunk: name (BYTE_ARRAY)                       │  │
│  │  └─── ...                                              │  │
│  │  Column Chunk: amount (DOUBLE)                         │  │
│  │  └─── ...                                              │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  Row Group 1                                                 │
│  └─── ...                                                    │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│  Footer                                                      │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  FileMetaData (Thrift-encoded)                         │  │
│  │  ├── version                                           │  │
│  │  ├── schema (column names, types, nesting)             │  │
│  │  ├── num_rows                                          │  │
│  │  ├── row_groups[]                                      │  │
│  │  │   ├── columns[]                                     │  │
│  │  │   │   ├── file_offset                               │  │
│  │  │   │   ├── total_compressed_size                     │  │
│  │  │   │   ├── total_uncompressed_size                   │  │
│  │  │   │   ├── num_values                                │  │
│  │  │   │   └── statistics (min, max, null_count)         │  │
│  │  │   └── total_byte_size, num_rows                     │  │
│  │  └── key_value_metadata[]                              │  │
│  └────────────────────────────────────────────────────────┘  │
│  Footer Length (4 bytes, little-endian)                       │
│  Magic Number: "PAR1" (4 bytes)                              │
└──────────────────────────────────────────────────────────────┘
```

### Why the Footer Is at the End

The footer-at-the-end design is intentional. Writers stream row groups sequentially and only compute aggregate statistics after all data is written. Readers issue one small read at the end of the file to get the footer, then use offsets in the footer to seek directly to the columns they need.

```
Read path for "SELECT SUM(amount) WHERE date = '2024-01-15'":
═════════════════════════════════════════════════════════════

Step 1: Read footer (last 8 bytes → footer length → read footer)
        1 HTTP range request, ~10-50 KB

Step 2: Parse schema, find column chunks for "amount" and "date"
        Skip all other columns entirely (column pruning)

Step 3: Check row group statistics:
        Row Group 0: date min='2024-01-01', max='2024-01-10' → SKIP
        Row Group 1: date min='2024-01-11', max='2024-01-20' → READ
        Row Group 2: date min='2024-01-21', max='2024-01-31' → SKIP
        (row group pruning — 67% I/O eliminated)

Step 4: Read only "amount" and "date" column chunks from Row Group 1
        2 HTTP range requests (or 1 coalesced request)

Step 5: Within the column chunk, check page-level statistics
        (Parquet v2 column indexes), skip pages where date ≠ target

Total I/O: ~1% of the file instead of reading the whole thing
```

### Encodings

```
Parquet Encoding Strategies:
════════════════════════════

PLAIN: raw values, no compression
  [100, 200, 300, 150, 200, 100, ...]
  → 8 bytes per INT64 value = 8N bytes

DICTIONARY (most common for low-cardinality):
  Dictionary: {0: "USA", 1: "GBR", 2: "DEU", 3: "JPN"}
  Data:       [0, 0, 1, 2, 0, 3, 1, 0, 2, ...]
  → 4-byte dict entries + bit-packed indices
  → For country column with 200 values: ~400 bytes dict + N bits data
  → Falls back to PLAIN if cardinality > page size

RLE (Run-Length Encoding, for repeated values):
  [1, 1, 1, 1, 1, 2, 2, 2, 3, 3, 3, 3]
  → [(1, 5), (2, 3), (3, 4)]
  → 12 values stored in 3 pairs

DELTA_BINARY_PACKED (for sorted/sequential integers):
  [1000, 1001, 1003, 1006, 1010]
  → base=1000, deltas=[1, 2, 3, 4]
  → Bit-packed deltas use far fewer bits than full values

DELTA_LENGTH_BYTE_ARRAY (for variable-length strings):
  ["hello", "world", "hi"]
  → lengths: delta-encoded [5, 5, 2]
  → concatenated bytes: "helloworldhi"

BYTE_STREAM_SPLIT (for floating-point columns):
  Splits IEEE 754 bytes across streams — byte 0 of all values together,
  byte 1 of all values together, etc. Similar float values share leading
  bytes, so the resulting streams compress extremely well with general
  codecs (ZSTD, Snappy).

  [3.14, 3.15, 3.16] (each 4 bytes as float32)
  → Stream 0: [byte0_of_3.14, byte0_of_3.15, byte0_of_3.16]
  → Stream 1: [byte1_of_3.14, byte1_of_3.15, byte1_of_3.16]
  → Stream 2: ...
  → Stream 3: ...
  → 2-5x better compression ratio for scientific/financial data
```

### Nested Data: Repetition and Definition Levels

Parquet handles nested and repeated structures (e.g., arrays, maps, structs) using Dremel's repetition/definition level encoding.

```
Schema: message Document {
  required int64 doc_id;
  repeated group links {
    required string url;
    optional string title;
  }
}

Record: {doc_id: 1, links: [{url: "a.com", title: "A"}, {url: "b.com"}]}
Record: {doc_id: 2, links: []}

Column "links.url":
  Value    Rep Level  Def Level
  "a.com"  0          2          (first link in first record)
  "b.com"  1          2          (second link in same record)
  NULL     0          0          (second record has no links)

  Rep level: 0 = new record, 1 = new element in same repeated field
  Def level: how many optional/repeated fields in the path are defined
             0 = links is empty, 1 = links exists but url is null,
             2 = url is defined

  This columnar encoding of nested data is what makes Parquet able to
  store JSON-like structures while still enabling column pruning.
```

### Compression

```
Compression happens AFTER encoding, per page:

  Raw values → Encoding (DICT/RLE/DELTA) → Compression (codec)

Supported codecs and trade-offs:

┌────────────┬──────────────┬───────────────┬─────────────────────┐
│ Codec      │ Compression  │ Speed         │ Best for            │
│            │ Ratio        │ (decompress)  │                     │
├────────────┼──────────────┼───────────────┼─────────────────────┤
│ SNAPPY     │ ~2-4x        │ ~500 MB/s     │ Hot data, low       │
│            │              │               │ latency queries     │
├────────────┼──────────────┼───────────────┼─────────────────────┤
│ ZSTD       │ ~4-8x        │ ~400 MB/s     │ Best all-around     │
│            │              │               │ (2024+ default)     │
├────────────┼──────────────┼───────────────┼─────────────────────┤
│ GZIP       │ ~4-6x        │ ~150 MB/s     │ Legacy, max compat  │
├────────────┼──────────────┼───────────────┼─────────────────────┤
│ LZ4_RAW    │ ~2-3x        │ ~800 MB/s     │ Real-time analytics │
├────────────┼──────────────┼───────────────┼─────────────────────┤
│ BROTLI     │ ~5-8x        │ ~250 MB/s     │ Archival, cold data │
├────────────┼──────────────┼───────────────┼─────────────────────┤
│ NONE       │ 1x           │ ∞             │ Already-compressed  │
│            │              │               │ or in-memory only   │
└────────────┴──────────────┴───────────────┴─────────────────────┘

Production recommendation (2025):
  Default to ZSTD level 3 — best ratio-to-speed trade-off.
  Use Snappy only when decompression latency is the bottleneck.
  Use ZSTD level 9+ for archival/cold storage tiers.
```

### Row Group Sizing

```
Row group sizing is one of the most impactful tuning knobs:

Too small (< 32 MB):
  ✗ More row groups → more footer metadata → larger planning cost
  ✗ More S3 GET requests (1 request per column chunk)
  ✗ Worse compression (less data per column chunk → fewer patterns)

Too large (> 1 GB):
  ✗ More memory needed for writing (buffering full row group)
  ✗ Coarser-grained row group pruning (min/max spans more values)
  ✗ Larger I/O when reading a single column chunk

Sweet spot:
  ┌──────────────────────────────────────────────────────┐
  │  Row group size: 64–256 MB (compressed)              │
  │  Target row count: 100K–1M rows per row group        │
  │  File size: 256 MB–1 GB per Parquet file             │
  │                                                      │
  │  These numbers match:                                │
  │  • S3 multipart upload boundaries                    │
  │  • Object storage GET request granularity             │
  │  • Worker memory during vectorized execution          │
  │  • Good compression ratio (enough data for patterns) │
  └──────────────────────────────────────────────────────┘
```

### Column Index and Offset Index (Parquet v2)

```
Column Index (page-level statistics):
═════════════════════════════════════

Before column index (Parquet v1):
  Statistics only at the row group level.
  Row group has 1M rows → min/max spans wide range → poor pruning.

After column index (Parquet v2 / page index):
  Each data page (~8K–64K rows) carries its own min/max/null_count.
  Row group with 50 pages → 50 separate min/max entries.

  ┌────────────────────────────────────────────────────────┐
  │  Column Index for "sale_date" in Row Group 3:          │
  │                                                        │
  │  Page 0:  min=2024-01-01, max=2024-01-05, nulls=0     │
  │  Page 1:  min=2024-01-05, max=2024-01-10, nulls=0     │
  │  Page 2:  min=2024-01-10, max=2024-01-15, nulls=2     │
  │  Page 3:  min=2024-01-15, max=2024-01-20, nulls=0     │
  │  ...                                                   │
  │                                                        │
  │  Query: WHERE sale_date = '2024-01-12'                 │
  │  → Only read pages 2 and 3 (skip pages 0, 1, 4+)      │
  └────────────────────────────────────────────────────────┘

Offset Index:
  Maps page number → byte offset in the file.
  After the column index tells you WHICH pages to read,
  the offset index tells you WHERE to seek.

  Combined: page-level predicate pushdown with O(1) seek.
```

---

## 3. From Data Lake to Data Lakehouse

### The Lakehouse Architecture

The data lakehouse combines the low cost and openness of a data lake with the reliability and performance of a data warehouse.

```
Lakehouse Architecture Stack:
═════════════════════════════

┌──────────────────────────────────────────────────────────────┐
│  Applications                                                │
│  BI dashboards, ML training, data science, streaming apps    │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│  Query Engines (decoupled compute)                           │
│  Spark · Trino · Flink · DuckDB · Presto · Dremio · StarRocks│
└──────────────────────────────┬───────────────────────────────┘
                               │ reads table metadata
┌──────────────────────────────▼───────────────────────────────┐
│  Catalog Service                                             │
│  Iceberg REST Catalog · AWS Glue · Unity Catalog · Nessie    │
│  Hive Metastore · Polaris (Snowflake open-source)            │
└──────────────────────────────┬───────────────────────────────┘
                               │ points to
┌──────────────────────────────▼───────────────────────────────┐
│  Table Format (the metadata layer)                           │
│  Apache Iceberg · Delta Lake · Apache Hudi                   │
│                                                              │
│  What it provides:                                           │
│  • ACID transactions (snapshot isolation)                     │
│  • Schema and partition evolution                            │
│  • Time travel (query any historical snapshot)               │
│  • Row-level deletes/updates (merge-on-read or copy-on-write)│
│  • Statistics for query planning                             │
│  • File-level tracking (which files are live)                │
└──────────────────────────────┬───────────────────────────────┘
                               │ data stored as
┌──────────────────────────────▼───────────────────────────────┐
│  Open File Formats                                           │
│  Apache Parquet (dominant) · Apache ORC · Apache Avro        │
└──────────────────────────────┬───────────────────────────────┘
                               │ stored on
┌──────────────────────────────▼───────────────────────────────┐
│  Object Storage                                              │
│  Amazon S3 · Google Cloud Storage · Azure ADLS · MinIO       │
│                                                              │
│  Properties:                                                 │
│  • ~$23/TB/month (S3 Standard)                               │
│  • 99.999999999% (11 9s) durability                          │
│  • Eventual consistency (S3 now strong read-after-write)     │
│  • No random writes — objects are immutable                  │
│  • High throughput for sequential reads                      │
└──────────────────────────────────────────────────────────────┘
```

### What "Open Table Format" Means

A table format is not a file format. It is a **specification** for organizing files into a logical table with transactional guarantees.

```
File format (Parquet):
  "How to encode columns inside one file"

Table format (Iceberg / Delta / Hudi):
  "Which Parquet files belong to this table right now,
   what schema they share, and how to atomically update that set"

Analogy to databases:
  Parquet = the page format (how bytes are laid out on a page)
  Table format = the catalog + WAL + MVCC layer
  Object storage = the disk
```

---

## 4. Apache Iceberg Deep Dive

Apache Iceberg was created at Netflix to solve the planning scalability problem — Hive Metastore couldn't handle tables with millions of partitions and files.

### Metadata Hierarchy

```
Iceberg Metadata Tree:
══════════════════════

Catalog  ──────────►  Metadata File (JSON)
(e.g., REST,              │
 Glue, Nessie)            │  contains: current-snapshot-id, schemas[],
                           │  partition-specs[], sort-orders[],
                           │  snapshot-log[]
                           │
                           ▼
                    Snapshot (pointer)
                           │
                           │  snapshot-id: 394875028475
                           │  timestamp-ms: 1705363200000
                           │  manifest-list: s3://…/snap-394.avro
                           │
                           ▼
                    Manifest List (Avro file)
                           │
                           │  Lists all manifest files for this snapshot.
                           │  Each entry has:
                           │  • manifest path
                           │  • partition spec id
                           │  • added/existing/deleted file counts
                           │  • partition field summaries (min/max per partition)
                           │
                    ┌──────┼──────────────┐
                    ▼      ▼              ▼
             Manifest    Manifest       Manifest
             File 0      File 1         File 2
             (Avro)      (Avro)         (Avro)
                │            │              │
                │  Each entry per data file: │
                │  • file_path               │
                │  • file_format             │
                │  • partition (values)       │
                │  • record_count            │
                │  • file_size_in_bytes       │
                │  • column_sizes {}          │
                │  • value_counts {}          │
                │  • null_value_counts {}     │
                │  • nan_value_counts {}      │
                │  • lower_bounds {}          │
                │  • upper_bounds {}          │
                │  • sort_order_id            │
                │
                ▼
         Data Files (Parquet / ORC / Avro)
         s3://bucket/db/table/data/00001.parquet
         s3://bucket/db/table/data/00002.parquet
         ...

  Read path:
  ──────────
  1. Catalog → current metadata file location     (1 RPC)
  2. Metadata file → current snapshot ID           (1 GET, ~1 KB)
  3. Snapshot → manifest list                      (1 GET, ~10 KB)
  4. Manifest list → prune manifests by partition  (in memory)
  5. Read surviving manifests → prune files by     (N GETs, ~100 KB each)
     column min/max statistics
  6. Read surviving data files                     (M GETs, actual data)

  Total metadata reads: 3 + N (typically N < 10)
  vs. Hive: LIST all files in all partition dirs (could be millions)
```

### Snapshot Isolation

```
Iceberg MVCC (Multi-Version Concurrency Control):
═════════════════════════════════════════════════

Every write produces a NEW snapshot. Readers always see a consistent snapshot.

Time ─────────────────────────────────────────────────────────►

  Snap 1          Snap 2              Snap 3
  ┌────┐          ┌────┐              ┌────┐
  │ S1 │          │ S2 │              │ S3 │
  └──┬─┘          └──┬─┘              └──┬─┘
     │               │                   │
  files:           files:              files:
  [A, B, C]        [A, B, C, D]        [A, C, D, E]
                    (added D)           (deleted B, added E)

  Reader R1 starts at Snap 2 → sees [A, B, C, D]
  Writer W1 commits Snap 3  → deletes B, adds E
  Reader R1 still sees Snap 2 → [A, B, C, D] (no interference)
  Reader R2 starts now → sees Snap 3 → [A, C, D, E]

Conflict resolution (optimistic concurrency):
  Writer W1: read snap 2, compute changes → try commit snap 3
  Writer W2: read snap 2, compute changes → try commit snap 3
  → One wins (atomic metadata file swap), the other retries
  → Retry validates: do my changes conflict with the committed snap 3?
     - Conflicts checked at file level (not row level)
     - Non-overlapping partition writes never conflict
```

### Row-Level Operations

```
Copy-on-Write (CoW) vs Merge-on-Read (MoR):
════════════════════════════════════════════

Iceberg supports both strategies for UPDATE and DELETE.

Copy-on-Write:
  DELETE FROM orders WHERE order_id = 42;

  1. Find data file containing order_id = 42 (using column stats)
     → file-001.parquet (10,000 rows)
  2. Read entire file, filter out row 42
  3. Write NEW file with 9,999 rows → file-005.parquet
  4. Commit: add file-005, remove file-001

  ┌──────────────────┐        ┌──────────────────┐
  │ file-001.parquet │        │ file-005.parquet │
  │ (10,000 rows)    │ ─────► │ (9,999 rows)     │
  │ includes row 42  │ rewrite│ row 42 removed   │
  └──────────────────┘        └──────────────────┘

  ✓ Read performance: no merge at query time
  ✗ Write amplification: rewrite entire file for 1 delete


Merge-on-Read (Iceberg v2 — position delete files):
  DELETE FROM orders WHERE order_id = 42;

  1. Find data file and position of row → file-001.parquet, pos 41
  2. Write a SMALL delete file: file-001-deletes.parquet
     Contains: {file_path: "file-001.parquet", pos: 41}
  3. Commit: add delete file, keep original data file

  ┌──────────────────┐     ┌───────────────────────┐
  │ file-001.parquet │     │ file-001-deletes.pqt  │
  │ (10,000 rows)    │  +  │ {file: 001, pos: 41}  │
  │ row 42 at pos 41 │     │ (tiny file)            │
  └──────────────────┘     └───────────────────────┘

  At read time: read data file, apply delete file → skip pos 41

  ✓ Write performance: tiny append instead of full rewrite
  ✗ Read overhead: must merge deletes at query time
  ✗ Accumulates delete files → needs periodic compaction

  Equality deletes (Iceberg v2):
  Delete file contains: {order_id: 42} instead of positions.
  Reader must check every row against delete predicates.
  Slower reads, but writer doesn't need to find the exact position.
```

### Hidden Partitioning

```
Traditional partitioning (Hive-style):
  CREATE TABLE events (
    event_time TIMESTAMP,
    user_id BIGINT,
    event_type STRING
  ) PARTITIONED BY (date STRING);

  Writer must extract the partition: INSERT INTO events PARTITION(date='2024-01-15') ...
  Reader must know the partition column: WHERE date = '2024-01-15'
  Change partitioning = rewrite all data

Iceberg hidden partitioning:
  CREATE TABLE events (
    event_time TIMESTAMP,
    user_id BIGINT,
    event_type STRING
  );

  ALTER TABLE events ADD PARTITION FIELD day(event_time);

  Writer: just writes event_time. Iceberg extracts day() automatically.
  Reader: WHERE event_time > '2024-01-15'. Iceberg translates to partition pruning.
  User never references partition columns directly.

  Supported transforms:
  ┌──────────────────┬────────────────────────────────────────┐
  │ Transform        │ Example                                │
  ├──────────────────┼────────────────────────────────────────┤
  │ identity(col)    │ Partition by exact column value        │
  │ year(ts)         │ Extract year from timestamp            │
  │ month(ts)        │ Extract year-month                     │
  │ day(ts)          │ Extract year-month-day                 │
  │ hour(ts)         │ Extract year-month-day-hour            │
  │ bucket(N, col)   │ Hash into N buckets                    │
  │ truncate(W, col) │ Truncate string to W chars, int to W   │
  │ void(col)        │ Always null (unpartitioned)            │
  └──────────────────┴────────────────────────────────────────┘

  The key win: partition evolution without rewriting data.

  ALTER TABLE events DROP PARTITION FIELD day(event_time);
  ALTER TABLE events ADD PARTITION FIELD month(event_time);

  Old files keep day-level partitioning. New files use month-level.
  Iceberg plans queries across both layouts transparently.
```

---

## 5. Delta Lake Deep Dive

Delta Lake was created at Databricks to bring ACID to Spark on data lakes.

### Transaction Log Architecture

```
Delta Lake Table on S3:
═══════════════════════

s3://bucket/sales/
├── _delta_log/                                ◄── Transaction log
│   ├── 00000000000000000000.json              ◄── Version 0
│   ├── 00000000000000000001.json              ◄── Version 1
│   ├── 00000000000000000002.json              ◄── Version 2
│   ├── ...
│   ├── 00000000000000000010.checkpoint.parquet ◄── Checkpoint at v10
│   ├── 00000000000000000020.checkpoint.parquet ◄── Checkpoint at v20
│   └── _last_checkpoint                        ◄── Points to latest
│
├── year=2024/month=01/
│   ├── part-00000-abc123.snappy.parquet
│   ├── part-00001-def456.snappy.parquet
│   └── part-00002-ghi789.snappy.parquet
├── year=2024/month=02/
│   └── ...
└── (orphaned/vacuum-eligible files)

Each JSON log entry is a sequence of ACTIONS:
─────────────────────────────────────────────

{
  "commitInfo": {
    "timestamp": 1705363200000,
    "operation": "WRITE",
    "operationParameters": {"mode": "Append", "partitionBy": "[year,month]"},
    "engineInfo": "Apache-Spark/3.5.0 Delta-Lake/3.1.0"
  }
}
{
  "add": {
    "path": "year=2024/month=01/part-00000-abc123.snappy.parquet",
    "partitionValues": {"year": "2024", "month": "01"},
    "size": 104857600,
    "modificationTime": 1705363200000,
    "dataChange": true,
    "stats": "{\"numRecords\":500000, \"minValues\":{\"amount\":0.50}, \"maxValues\":{\"amount\":9999.99}, \"nullCount\":{\"amount\":0}}"
  }
}
{
  "remove": {
    "path": "year=2024/month=01/part-00001-old.snappy.parquet",
    "deletionTimestamp": 1705363200000,
    "dataChange": true
  }
}
{
  "metaData": {
    "schemaString": "{\"type\":\"struct\",\"fields\":[...]}",
    "partitionColumns": ["year", "month"],
    "configuration": {"delta.autoOptimize.optimizeWrite": "true"}
  }
}
```

### Checkpoint Mechanism

```
Problem: reading 10,000 JSON log files to reconstruct table state is slow.

Solution: periodic checkpoints — a Parquet file that captures the full table
state at a given version.

  _delta_log/
  ├── 00000000.json
  ├── 00000001.json
  ├── ...
  ├── 00000009.json
  ├── 00000010.checkpoint.parquet   ◄── Full state at v10
  ├── 00000011.json                 ◄── Incremental changes
  ├── 00000012.json
  ├── ...
  ├── 00000020.checkpoint.parquet   ◄── Full state at v20

  To read current state (at v23):
  1. Read _last_checkpoint → v20
  2. Read checkpoint at v20 (one Parquet file, all live "add" actions)
  3. Read JSON logs v21, v22, v23 (3 small files)
  4. Merge: checkpoint + incremental = current state

  Checkpoint frequency: every 10 versions by default (configurable).

Multi-part checkpoints (large tables):
  For tables with millions of files, a single checkpoint Parquet can be huge.
  Delta Lake supports multi-part checkpoints:
  ├── 00000100.checkpoint.0000000001.0000000003.parquet
  ├── 00000100.checkpoint.0000000002.0000000003.parquet
  └── 00000100.checkpoint.0000000003.0000000003.parquet
  (checkpoint at v100, split into 3 parts)
```

### Optimistic Concurrency Control

```
Delta Lake Conflict Resolution:
═══════════════════════════════

Writer 1 and Writer 2 both read version 5.

Writer 1 (appending to partition month=01):
  1. Read version 5
  2. Write new Parquet files
  3. Atomically create 00000006.json → SUCCESS ✓

Writer 2 (appending to partition month=02):
  1. Read version 5
  2. Write new Parquet files
  3. Try to create 00000006.json → CONFLICT (already exists)
  4. Read version 6 (Writer 1's commit)
  5. Check: does version 6 conflict with my changes?
     → Writer 1 touched month=01, I touch month=02 → NO CONFLICT
  6. Retry: create 00000007.json → SUCCESS ✓

Conflict rules:
  ┌──────────────────┬──────────────────┬────────────────┐
  │ Writer 1 op      │ Writer 2 op      │ Conflict?      │
  ├──────────────────┼──────────────────┼────────────────┤
  │ Append to P1     │ Append to P2     │ No             │
  │ Append to P1     │ Append to P1     │ No (both add)  │
  │ Append to P1     │ Overwrite P1     │ Yes            │
  │ Delete from P1   │ Delete from P1   │ Yes            │
  │ Delete from P1   │ Append to P2     │ No             │
  │ Schema change    │ Any write        │ Yes            │
  │ OPTIMIZE         │ Any data change  │ Yes            │
  └──────────────────┴──────────────────┴────────────────┘

Atomicity guarantee:
  On S3: conditional PutObject (If-None-Match) or DynamoDB-based
         locking for cross-writer coordination.
  On HDFS: atomic rename of the log file.
  On ADLS: conditional append with ETag check.
```

### Deletion Vectors (Delta Lake 3.0+)

```
Before deletion vectors (copy-on-write only):
  DELETE FROM table WHERE id = 42;
  → Find file with id=42 (1M rows) → rewrite without that row → 1M-1 row file
  Write amplification: ~100%

With deletion vectors:
  DELETE FROM table WHERE id = 42;
  → Find file and row position
  → Write a deletion vector (bitmap) marking that position as deleted
  → Original data file is untouched

  Deletion vector storage:
  ┌──────────────────────┐
  │  file: part-00001    │
  │  bitmap: [0,0,...,1, │  ← bit 41 set = row at position 41 is deleted
  │           0,0,...,0] │
  │  cardinality: 1      │  ← 1 row deleted
  └──────────────────────┘

  Stored as a separate small file or inline in the transaction log.

  Read path: read data file + apply deletion vector (bitmap AND)
  → Same as Iceberg merge-on-read, but using bitmaps instead of position files.

  Compaction later merges deletion vectors back into rewritten files.
```

---

## 6. Apache Hudi Deep Dive

Apache Hudi (Hadoop Upserts Deletes and Incrementals) was created at Uber for near-real-time incremental data ingestion. Its design prioritizes low-latency upserts and change data capture (CDC).

### Table Types

```
Hudi Table Types:
═════════════════

Copy-on-Write (CoW) Table:
  Every write rewrites affected data files.
  ┌──────────────────────────────────────────────────────┐
  │  Write: UPSERT 1 row into file-001 (100K rows)      │
  │  → Read file-001, merge update, write file-005       │
  │  → Commit: replace file-001 with file-005            │
  │                                                      │
  │  Read: always reads latest complete files             │
  │  → No merge needed → fast reads                      │
  │  → Write latency: higher (full file rewrite)         │
  │                                                      │
  │  Best for: read-heavy workloads, batch upserts       │
  └──────────────────────────────────────────────────────┘

Merge-on-Read (MoR) Table:
  Writes go to a delta log (Avro); reads merge base + delta.
  ┌──────────────────────────────────────────────────────┐
  │  Base file: file-001.parquet (100K rows)             │
  │  Delta log: .file-001.log.1 (Avro, 50 upserts)      │
  │  Delta log: .file-001.log.2 (Avro, 30 upserts)      │
  │                                                      │
  │  Write: append to delta log → fast (small Avro file) │
  │  Read (snapshot query): merge base + all delta logs   │
  │  Read (read-optimized query): read only base files    │
  │    → Stale data but zero merge cost                  │
  │                                                      │
  │  Compaction: periodically merge deltas into base      │
  │  → Converts MoR to CoW-equivalent state              │
  │                                                      │
  │  Best for: write-heavy / CDC / near-real-time ingest  │
  └──────────────────────────────────────────────────────┘

  Decision:
  ┌───────────────────┬──────────┬──────────┐
  │                   │ CoW      │ MoR      │
  ├───────────────────┼──────────┼──────────┤
  │ Write latency     │ High     │ Low      │
  │ Read latency      │ Low      │ Medium   │
  │ Write amplifictn. │ High     │ Low      │
  │ Read amplifictn.  │ None     │ Medium   │
  │ Storage overhead  │ Low      │ Medium   │
  │ Compaction needed │ No       │ Yes      │
  └───────────────────┴──────────┴──────────┘
```

### Hudi Timeline

```
Hudi uses a timeline of instants to track table history:

Timeline:
  ┌──────────┬──────────┬──────────┬──────────┬──────────┐
  │ Instant 1│ Instant 2│ Instant 3│ Instant 4│ Instant 5│
  │ COMMIT   │ COMMIT   │ DELTA_   │ COMPACT  │ CLEAN    │
  │ (write)  │ (write)  │ COMMIT   │ ION      │          │
  │          │          │ (upsert) │ (merge)  │ (GC old) │
  │ 20240115 │ 20240115 │ 20240115 │ 20240115 │ 20240115 │
  │ 100000   │ 110000   │ 120000   │ 130000   │ 140000   │
  └──────────┴──────────┴──────────┴──────────┴──────────┘

  Instant = (timestamp, action, state)
  States: REQUESTED → INFLIGHT → COMPLETED (or FAILED)

  Actions:
  • COMMIT — CoW write
  • DELTA_COMMIT — MoR write (to delta log)
  • COMPACTION — merge delta logs into base files
  • CLEAN — remove old file versions
  • ROLLBACK — undo a failed commit
  • SAVEPOINT — mark a point for restore
  • REPLACE — bulk insert or clustering result

Incremental queries:
  "Give me all rows that changed since instant 3"
  → Hudi reads the timeline, finds commits 4 and 5,
    returns only the changed/new rows.
  → Powers CDC pipelines without full scans.
```

### Record-Level Indexing

```
Hudi's record key index enables O(1) lookup of which file contains a record:

Global index (HBase-backed or internal):
  ┌───────────────────────────────────────────┐
  │  Record Key → (Partition, FileGroup, File)│
  │                                           │
  │  user_123  → (region=US, fg-01, file-005) │
  │  user_456  → (region=EU, fg-03, file-012) │
  │  user_789  → (region=US, fg-01, file-005) │
  └───────────────────────────────────────────┘

  UPSERT user_123:
  1. Index lookup → file-005 in partition region=US
  2. Read file-005, merge update, write new version
  (No need to scan all files to find the record)

  Index types:
  ┌───────────────┬──────────────────────────────────────┐
  │ Index Type    │ Properties                           │
  ├───────────────┼──────────────────────────────────────┤
  │ BLOOM         │ In-file bloom filters, no external   │
  │               │ state. Fast for partition-level.     │
  ├───────────────┼──────────────────────────────────────┤
  │ SIMPLE        │ Joins incoming with existing data.   │
  │               │ Slow for large tables.               │
  ├───────────────┼──────────────────────────────────────┤
  │ HBASE         │ External HBase for global lookups.   │
  │               │ O(1) but requires HBase cluster.     │
  ├───────────────┼──────────────────────────────────────┤
  │ BUCKET        │ Hash-based bucketing for fast        │
  │               │ partition-level lookups.             │
  ├───────────────┼──────────────────────────────────────┤
  │ RECORD_INDEX  │ Built-in secondary index (Hudi 0.14+)│
  │               │ Stored as a Hudi metadata table.     │
  └───────────────┴──────────────────────────────────────┘
```

---

## 7. Object Storage as the Foundation

### S3 Consistency Model

```
S3 Consistency (post-December 2020):
════════════════════════════════════

  ✓ Strong read-after-write consistency for PUTs and DELETEs
    (PUT object → immediately readable at latest version)

  ✓ Strong list consistency
    (PUT object → immediately appears in LIST)

  ✗ No atomic rename
    (critical implication for table formats — see below)

  ✗ No conditional write (until S3 conditional writes, 2024)
    S3 added If-None-Match header support in August 2024.
    Before that: Delta Lake used DynamoDB for atomic commits.
    After: native S3 conditional PutObject for log files.

  ✗ No append
    Objects are immutable. "Update" = write new object.
    This is why Parquet files are never modified in place.

How table formats handle the no-atomic-rename problem:
──────────────────────────────────────────────────────

  Iceberg:
    Metadata file is a new object for each commit.
    Catalog (REST, Glue, Nessie) atomically swaps the pointer
    from old metadata file to new metadata file.
    → Atomicity delegated to the catalog service.

  Delta Lake:
    Transaction log entries are sequentially numbered JSON files.
    Version N+1 is committed by writing 000...N+1.json.
    On S3: uses If-None-Match (2024+) or DynamoDB conditional put.
    On HDFS/ADLS: uses atomic rename.

  Hudi:
    Timeline instant files use atomic rename (HDFS) or
    DynamoDB-based locking (S3).
```

### Cost Model

```
S3 Pricing Breakdown (relevant to lakehouse workloads):
═══════════════════════════════════════════════════════

┌─────────────────────────┬────────────────┬─────────────────────┐
│ Operation               │ Cost           │ Lakehouse impact    │
├─────────────────────────┼────────────────┼─────────────────────┤
│ Storage (Standard)      │ $0.023/GB/mo   │ Data files (bulk)   │
│ Storage (IA)            │ $0.0125/GB/mo  │ Old snapshots       │
│ Storage (Glacier)       │ $0.004/GB/mo   │ Archived data       │
├─────────────────────────┼────────────────┼─────────────────────┤
│ PUT/POST/COPY           │ $0.005/1K reqs │ Writes, commits     │
│ GET/SELECT              │ $0.0004/1K reqs│ Reads, scans        │
│ LIST                    │ $0.005/1K reqs │ Planning (Hive-style│
│                         │                │ — avoided by Iceberg)│
├─────────────────────────┼────────────────┼─────────────────────┤
│ Data transfer (out)     │ $0.09/GB       │ Cross-region reads  │
│ Data transfer (same-AZ) │ Free           │ Compute in same AZ  │
└─────────────────────────┴────────────────┴─────────────────────┘

Why Iceberg's metadata tree saves money:
  Hive-style planning: LIST s3://bucket/table/year=2024/month=01/
                       LIST s3://bucket/table/year=2024/month=02/
                       ... (12 LIST requests × $0.005/1K = pennies,
                            but 10,000 partitions = $0.05 per query plan)
                       Also slow: S3 LIST returns 1000 objects per page.

  Iceberg planning:    GET manifest-list (1 request)
                       GET manifest files (3-5 requests, pruned)
                       File list is IN the manifests — no LIST needed.
                       → Fixed cost per query plan, regardless of table size.
```

---

## 8. Catalog and Metadata Services

### Role of the Catalog

```
The catalog answers one question:
  "Where is the current metadata for table X?"

Without catalog:                      With catalog:
  Reader knows:                        Reader asks catalog:
  s3://bucket/table/metadata/          "Where is db.sales?"
  v1.metadata.json                     → s3://…/metadata/v47.json
  v2.metadata.json                     (one RPC, always current)
  ...
  v47.metadata.json ← current?
  (Must guess or scan)

Catalog is the atomic swap point:
  Old snapshot → v46.metadata.json
  Writer commits v47.metadata.json to S3
  Writer atomically updates catalog pointer to v47
  → All subsequent readers see the new snapshot

Catalog types:
┌───────────────────┬───────────────────────────────────────────┐
│ Catalog           │ Characteristics                           │
├───────────────────┼───────────────────────────────────────────┤
│ Hive Metastore    │ Legacy. No branching. Single-writer only. │
│                   │ Still widely used. JDBC-based.            │
├───────────────────┼───────────────────────────────────────────┤
│ AWS Glue          │ Managed HMS-compatible. No branching.     │
│                   │ Tight S3 integration. Pay-per-request.    │
├───────────────────┼───────────────────────────────────────────┤
│ Iceberg REST      │ Open standard (Iceberg spec). Any backend.│
│ Catalog           │ Multi-engine. Namespace support.          │
├───────────────────┼───────────────────────────────────────────┤
│ Nessie            │ Git-like branching and tagging for tables. │
│                   │ Compare table states across branches.     │
│                   │ Open source (Dremio).                     │
├───────────────────┼───────────────────────────────────────────┤
│ Polaris           │ Open-source Iceberg REST catalog          │
│                   │ (Snowflake). Multi-engine interop.        │
├───────────────────┼───────────────────────────────────────────┤
│ Unity Catalog     │ Databricks. Governs Delta, Iceberg, Hudi. │
│ (Databricks)      │ Fine-grained ACLs. Data lineage.          │
├───────────────────┼───────────────────────────────────────────┤
│ Gravitino         │ Apache incubating. Unified metadata for   │
│ (Apache)          │ Iceberg, Hive, JDBC, file systems.        │
└───────────────────┴───────────────────────────────────────────┘
```

### Nessie: Git for Data

```
Nessie branching model:

  main ─────●─────●─────●─────●─────●─────►
              \                   ↑
  feature     ●─────●─────●─────●  (merge)
  branch         add      update
                 table    schema

  Operations:
  • CREATE BRANCH feature FROM main
  • Write to tables on the feature branch (isolated)
  • MERGE feature INTO main (catalog-level atomic merge)
  • TAG v2.0 AT main (immutable reference to table state)

  Use cases:
  1. Test schema changes on a branch before merging to production
  2. Run ML experiments against a consistent data snapshot
  3. Audit: "what did the table look like at tag v1.5?"
  4. CI/CD for data: validate data quality on branch, then merge
```

---

## 9. Query Engine Integration

### How Engines Read Lakehouse Tables

```
Query execution flow (e.g., Trino reading an Iceberg table):
═══════════════════════════════════════════════════════════

  SELECT region, SUM(amount)
  FROM iceberg.sales.orders
  WHERE order_date >= '2024-01-01'
  GROUP BY region;

  ┌─────────────────────────────────────────────────────────────┐
  │ Step 1: Catalog resolution                                  │
  │   Trino → Iceberg REST Catalog → current metadata file      │
  │   Metadata file → current snapshot ID                       │
  └────────────────────────────────────┬────────────────────────┘
                                       ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ Step 2: Manifest pruning                                    │
  │   Read manifest list → 20 manifests                         │
  │   Partition summary in manifest list:                       │
  │     manifest-0: order_date min=2023-06, max=2023-12 → SKIP │
  │     manifest-1: order_date min=2024-01, max=2024-06 → READ │
  │   Surviving manifests: 5 out of 20                          │
  └────────────────────────────────────┬────────────────────────┘
                                       ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ Step 3: File pruning (within surviving manifests)           │
  │   Each manifest entry has column-level min/max:             │
  │     file-001: amount min=0.50, max=9999 → READ             │
  │     file-002: order_date max=2023-12-31 → SKIP             │
  │   Surviving files: 150 out of 2000                          │
  └────────────────────────────────────┬────────────────────────┘
                                       ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ Step 4: Split planning                                      │
  │   Assign file splits to worker nodes for parallel execution │
  │   150 files ÷ 10 workers = ~15 files per worker             │
  └────────────────────────────────────┬────────────────────────┘
                                       ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ Step 5: Parquet-level pushdown (per worker)                 │
  │   Read Parquet footer → row group statistics                │
  │   Row group pruning: skip row groups where date < 2024-01   │
  │   Column pruning: read only "region" and "amount" chunks    │
  │   Page-level pruning (column index): skip pages             │
  └────────────────────────────────────┬────────────────────────┘
                                       ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ Step 6: Vectorized execution                                │
  │   Decompress column chunks → Arrow batches (1024 rows)      │
  │   Filter → Project → Hash Aggregate → Partial results       │
  │   Shuffle partial results → Final aggregation               │
  └─────────────────────────────────────────────────────────────┘

  I/O reduction: 2000 files → 150 files (manifest pruning)
                 → ~50 row groups (Parquet row group pruning)
                 → 2 columns out of 15 (column pruning)
  Total: ~0.3% of raw data read
```

### Engine Compatibility

```
Engine support matrix (2025):

┌─────────────────┬───────────┬────────────┬──────────┐
│ Engine          │ Iceberg   │ Delta Lake │ Hudi     │
├─────────────────┼───────────┼────────────┼──────────┤
│ Apache Spark    │ ✓ Full    │ ✓ Full     │ ✓ Full   │
│ Trino / Presto  │ ✓ Full    │ ✓ Read+    │ ✓ Read   │
│ Apache Flink    │ ✓ Full    │ ✓ Read     │ ✓ Full   │
│ DuckDB          │ ✓ Full    │ ✓ Read     │ ✗        │
│ Snowflake       │ ✓ Full    │ ✓ Read     │ ✗        │
│ BigQuery        │ ✓ Read    │ ✗          │ ✗        │
│ Redshift        │ ✓ Read    │ ✗          │ ✗        │
│ Databricks      │ ✓ Full    │ ✓ Native   │ ✓ Read   │
│ Dremio          │ ✓ Native  │ ✓ Read     │ ✗        │
│ StarRocks       │ ✓ Full    │ ✓ Read     │ ✓ Read   │
│ Apache Doris    │ ✓ Full    │ ✓ Read     │ ✓ Read   │
│ ClickHouse      │ ✓ Read    │ ✓ Read     │ ✗        │
│ Polars          │ ✓ Read    │ ✓ Read     │ ✗        │
└─────────────────┴───────────┴────────────┴──────────┘

Key trend (2025): Iceberg has the broadest engine support.
Delta Lake is strongest in the Databricks/Spark ecosystem.
Hudi remains niche, strongest for CDC/streaming use cases.
```

---

## 10. Write Patterns and Ingestion

### Batch Ingestion

```
Batch write to Iceberg (Spark):
═══════════════════════════════

df = spark.read.parquet("s3://raw/events/2024-01-15/")

df.writeTo("catalog.db.events") \
  .option("write-format", "parquet") \
  .option("target-file-size-bytes", str(256 * 1024 * 1024)) \
  .append()

What happens internally:
  1. Spark partitions the DataFrame across executors
  2. Each executor writes Parquet files to a staging location
  3. Files are moved to the table's data directory
  4. A new snapshot is committed with the new file list
  5. If commit conflicts → retry with validation

File sizing strategy:
  ┌──────────────────────────────────────────────────────────┐
  │  Target: 256 MB per Parquet file                         │
  │                                                          │
  │  Too small (< 32 MB):                                    │
  │    ✗ Small-file problem — millions of files              │
  │    ✗ High S3 request cost (one GET per file per column)  │
  │    ✗ High metadata overhead in manifests                 │
  │    ✗ Poor compression ratio                              │
  │                                                          │
  │  Too large (> 1 GB):                                     │
  │    ✗ Single-file read takes too long for interactive     │
  │    ✗ Write failures lose more work (restart from scratch)│
  │    ✗ Coarser row-group statistics → worse pruning        │
  │                                                          │
  │  256 MB sweet spot:                                      │
  │    ✓ Good compression (~4:1 ZSTD → ~64 MB compressed)   │
  │    ✓ 1 PB table = ~4M files (manageable)                │
  │    ✓ Each file scans in ~1-2s from S3                    │
  │    ✓ Parallel reads across workers scale linearly        │
  └──────────────────────────────────────────────────────────┘
```

### Streaming Ingestion

```
Streaming write to Iceberg (Flink):
════════════════════════════════════

  ┌──────────┐     ┌──────────┐     ┌────────────────┐
  │  Kafka   │ ──► │  Flink   │ ──► │  Iceberg Table │
  │  topic   │     │  job     │     │  (S3)          │
  └──────────┘     └──────────┘     └────────────────┘

  Commit interval: every 1-5 minutes (configurable)

  Problem: streaming creates many small files per commit.
  Each 1-minute commit might produce a 5 MB Parquet file per partition.
  After 24 hours: 1440 files × 50 partitions = 72,000 small files.

  Solutions:
  ┌──────────────────────────────────────────────────────────┐
  │ 1. Increase commit interval (trade latency for file size)│
  │    → 5 min = 5x larger files, 5 min data delay          │
  │                                                          │
  │ 2. In-memory buffering (Flink checkpoints hold data)     │
  │    → Flush larger batches to Parquet                     │
  │                                                          │
  │ 3. Background compaction (preferred)                     │
  │    → Accept small files on ingest, compact async later   │
  │    → Separate compaction job merges small files into      │
  │      optimally-sized files every 15-60 minutes           │
  └──────────────────────────────────────────────────────────┘
```

### Change Data Capture (CDC)

```
CDC pipeline from OLTP to lakehouse:
═════════════════════════════════════

  ┌──────────┐    ┌───────────┐    ┌──────────┐    ┌────────────┐
  │ Postgres │──► │ Debezium  │──► │  Kafka   │──► │ Hudi/      │
  │ (source) │    │ (CDC      │    │  topic   │    │ Iceberg    │
  │          │    │  connector)│    │          │    │ table      │
  └──────────┘    └───────────┘    └──────────┘    └────────────┘

  Debezium captures: INSERT, UPDATE, DELETE from Postgres WAL
  Each message: {op: "u", before: {...}, after: {...}, ts_ms: ...}

  Hudi excels here:
  ┌──────────────────────────────────────────────────────────┐
  │  Hudi UPSERT with record key:                           │
  │  1. Incoming CDC event: {op: "u", key: user_123, ...}   │
  │  2. Index lookup: user_123 → file-005, partition=US     │
  │  3. MoR: append to delta log (fast, no rewrite)         │
  │  4. Eventually compact delta logs into base Parquet     │
  │                                                          │
  │  Result: near-real-time replica of OLTP table in the    │
  │  lakehouse, queryable with SQL by any engine.           │
  └──────────────────────────────────────────────────────────┘
```

---

## 11. Compaction, Optimization, and Maintenance

### Why Compaction Is Necessary

```
The small-file problem:
═══════════════════════

After streaming ingestion + updates + deletes:

  Table state before compaction:
  ├── partition=2024-01/
  │   ├── file-001.parquet (256 MB) ← original batch load
  │   ├── file-002.parquet (5 MB)   ← streaming commit
  │   ├── file-003.parquet (5 MB)   ← streaming commit
  │   ├── file-004.parquet (5 MB)   ← streaming commit
  │   ├── file-002-deletes.parquet  ← delete file
  │   ├── file-005.parquet (3 MB)   ← update result
  │   └── ... (200 more small files)
  │
  Total: 203 files, many tiny, some with pending deletes

  Problems:
  ✗ Query planning enumerates 203 files (slow)
  ✗ 203 S3 GET requests per query per column (expensive + slow)
  ✗ Merge-on-read must apply delete files at query time
  ✗ Compression ratio is poor (small files have fewer patterns)

  After compaction:
  ├── partition=2024-01/
  │   ├── compacted-001.parquet (256 MB) ← merged, sorted, compressed
  │   ├── compacted-002.parquet (256 MB)
  │   └── compacted-003.parquet (180 MB)
  │
  Total: 3 files, optimally sized, no delete files, sorted
```

### Iceberg Compaction

```
Iceberg rewrite_data_files():
═════════════════════════════

-- Spark SQL:
CALL catalog.system.rewrite_data_files(
  table => 'db.events',
  strategy => 'sort',                    -- bin-pack | sort | z-order
  sort_order => 'event_date ASC, region ASC',
  options => map(
    'target-file-size-bytes', '268435456',  -- 256 MB
    'min-file-size-bytes', '67108864',      -- 64 MB (files smaller → rewrite)
    'max-file-size-bytes', '536870912',     -- 512 MB
    'min-input-files', '5'                  -- at least 5 files to trigger
  )
);

-- What it does:
-- 1. Reads all files smaller than min-file-size-bytes
-- 2. Sorts by the given sort order
-- 3. Writes new files of target size
-- 4. Commits: add new files, remove old files (atomic snapshot)
-- 5. Old files are NOT deleted yet (still referenced by old snapshots)

-- To actually delete old files:
CALL catalog.system.expire_snapshots('db.events', TIMESTAMP '2024-01-01 00:00:00');
CALL catalog.system.remove_orphan_files('db.events');
```

### Sort Order and Z-Order

```
Why sort order matters:
═══════════════════════

Unsorted data (random insertion order):
  File 1: date range [2023-01-01 to 2024-12-31], region=[US,EU,JP,...]
  File 2: date range [2023-01-01 to 2024-12-31], region=[US,EU,JP,...]
  → Every file spans the full range → no file can be pruned

Sorted by date:
  File 1: date range [2024-01-01 to 2024-01-15]
  File 2: date range [2024-01-16 to 2024-01-31]
  → WHERE date = '2024-01-20' → only read file 2 → 50% pruned

Sorted by date, then region:
  File 1: date=[2024-01-01 to 2024-01-15], region=[AP,EU]
  File 2: date=[2024-01-01 to 2024-01-15], region=[JP,US]
  File 3: date=[2024-01-16 to 2024-01-31], region=[AP,EU]
  File 4: date=[2024-01-16 to 2024-01-31], region=[JP,US]
  → WHERE date = '2024-01-20' AND region = 'US' → only file 4 → 75% pruned

Z-order (interleaving bits of multiple columns):
  Maps multi-dimensional ranges into a 1D ordering that preserves
  locality across ALL z-ordered columns simultaneously.

  Linear sort on (date, region):
    → Great pruning on date (primary sort key)
    → Moderate pruning on region (secondary)
    → No pruning on amount (not in sort key)

  Z-order on (date, region, amount):
    → Good (not great) pruning on ALL three columns
    → Each file has tight ranges in all three dimensions

  Best for: tables with unpredictable query patterns across multiple columns.
  Cost: ~2-3x slower compaction (interleaved sorting is expensive).
```

---

## 12. Schema Evolution and Partition Evolution

### Schema Evolution

```
Iceberg schema evolution (safe, metadata-only operations):
═════════════════════════════════════════════════════════

-- Add a column (new files have it, old files return NULL):
ALTER TABLE db.orders ADD COLUMNS (
  discount DOUBLE COMMENT 'Applied discount'
);

-- Rename a column (metadata-only, no data rewrite):
ALTER TABLE db.orders RENAME COLUMN amt TO amount;

-- Widen a type (int → long, float → double):
ALTER TABLE db.orders ALTER COLUMN quantity TYPE bigint;

-- Drop a column (metadata-only, data files untouched):
ALTER TABLE db.orders DROP COLUMN legacy_field;

-- Reorder columns:
ALTER TABLE db.orders ALTER COLUMN discount AFTER amount;

How it works internally:
  Iceberg uses column IDs (integers), not column names.
  Schema version 1: {1: "id" INT, 2: "name" STRING, 3: "amt" DOUBLE}
  Schema version 2: {1: "id" INT, 2: "name" STRING, 3: "amount" DOUBLE,
                     4: "discount" DOUBLE}

  Old Parquet files: columns have IDs embedded in the file schema.
  Reader maps file column ID 3 → current name "amount" (was "amt").
  Reader maps missing ID 4 → NULL for "discount".

  Key: column identity is by ID, never by name or position.
  This is why renames and reorders are safe metadata-only operations.

Delta Lake schema evolution:
  ✓ Add columns
  ✓ Rename columns (requires column mapping mode)
  ✗ Drop columns (mark as dropped, physical data remains)
  ✗ Reorder columns (with column mapping mode)

  Delta uses column mapping modes:
  • "none" (default): columns matched by name → rename is a breaking change
  • "name": columns have physical names + logical names → rename is safe
  • "id": columns matched by ID → most flexible, like Iceberg
```

### Partition Evolution

```
Iceberg partition evolution (unique advantage):
════════════════════════════════════════════════

Scenario: your table was partitioned by day. After a year, daily partitions
are too granular (365 partitions × 5 years = 1825). You want monthly.

Traditional (Hive, Delta):
  1. Create a new table with monthly partitioning
  2. Copy all data from old table to new table (full rewrite!)
  3. Swap table names
  → Expensive, risky, requires downtime

Iceberg:
  ALTER TABLE db.events REPLACE PARTITION FIELD day(event_time)
    WITH month(event_time);

  What happens:
  1. Metadata records a NEW partition spec (spec-id: 2)
  2. Old data files keep their old partition spec (spec-id: 1)
  3. New writes use the new partition spec
  4. Query planning handles BOTH specs transparently

  Table state:
  ┌──────────────────────────────────────────────────────┐
  │  Partition spec 1 (old files): day(event_time)       │
  │  ├── data/event_time_day=2024-01-01/file-001.parquet│
  │  ├── data/event_time_day=2024-01-02/file-002.parquet│
  │  └── ... (365 partitions)                           │
  │                                                      │
  │  Partition spec 2 (new files): month(event_time)     │
  │  ├── data/event_time_month=2024-02/file-500.parquet │
  │  ├── data/event_time_month=2024-03/file-501.parquet │
  │  └── ...                                             │
  └──────────────────────────────────────────────────────┘

  Query: WHERE event_time >= '2024-02-01'
  Planner: spec 1 files → check day partition → prune
           spec 2 files → check month partition → prune
  → Both specs evaluated, correct results, zero data rewrite.

  Over time, compaction rewrites old files under the new spec.
```

---

## 13. Time Travel, Branching, and Auditing

### Time Travel

```
Time travel queries:
════════════════════

-- Iceberg: query at a specific snapshot
SELECT * FROM db.orders VERSION AS OF 394875028475;

-- Iceberg: query at a specific timestamp
SELECT * FROM db.orders FOR SYSTEM_TIME AS OF TIMESTAMP '2024-01-15 10:00:00';

-- Delta Lake: query at a specific version
SELECT * FROM db.orders VERSION AS OF 5;

-- Delta Lake: query at a specific timestamp
SELECT * FROM db.orders TIMESTAMP AS OF '2024-01-15 10:00:00';

-- Compare snapshots (Iceberg):
SELECT * FROM db.orders.snapshots;  -- list all snapshots
SELECT * FROM db.orders.history;    -- snapshot history with timestamps

-- Diff between snapshots (what changed):
CALL catalog.system.ancestors_of('db.orders', 394875028475);

Use cases:
  1. Debugging: "what did the data look like before yesterday's pipeline broke?"
  2. Auditing: "what was the state of this table during the compliance window?"
  3. Reproducibility: "train the model on the exact same data from last week"
  4. Undo: "roll back to snapshot N because the latest load was wrong"
```

### Rollback

```
Rollback operations:
════════════════════

-- Iceberg: rollback to a previous snapshot
CALL catalog.system.rollback_to_snapshot('db.orders', 394875028475);

-- Iceberg: rollback to a timestamp
CALL catalog.system.rollback_to_timestamp('db.orders',
  TIMESTAMP '2024-01-15 10:00:00');

-- Delta Lake: restore to a version
RESTORE TABLE db.orders TO VERSION AS OF 5;

-- Delta Lake: restore to a timestamp
RESTORE TABLE db.orders TO TIMESTAMP AS OF '2024-01-15 10:00:00';

What rollback does:
  NOT a destructive operation. It creates a NEW snapshot whose file list
  is the same as the old snapshot. The history is preserved.

  Snap 1 → Snap 2 → Snap 3 → Snap 4 (bad load) → Snap 5 (rollback to 3)
                                                      │
                                                      └── file list = snap 3's files
  All 5 snapshots exist. Time travel still works to any of them.
```

### Snapshot Lifecycle and Expiry

```
Snapshot management:
════════════════════

Problem: every write creates a snapshot. After 100,000 writes,
the table has 100,000 snapshots — each referencing data files that
can't be garbage-collected.

Snapshot expiry:
  CALL catalog.system.expire_snapshots(
    table => 'db.orders',
    older_than => TIMESTAMP '2024-06-01 00:00:00',
    retain_last => 100  -- always keep at least 100 snapshots
  );

  What it does:
  1. Removes snapshot metadata entries older than the threshold
  2. Identifies data files ONLY referenced by expired snapshots
  3. Those files become eligible for deletion

  Then:
  CALL catalog.system.remove_orphan_files(
    table => 'db.orders',
    older_than => TIMESTAMP '2024-06-01 00:00:00'
  );
  → Actually deletes the unreferenced Parquet files from S3.

  Delta Lake equivalent:
  VACUUM db.orders RETAIN 168 HOURS;  -- delete files older than 7 days
  (7-day default retention protects concurrent readers)
```

---

## 14. Performance: Predicate Pushdown, Pruning, and Statistics

### Multi-Level Pruning Pipeline

```
Query: SELECT SUM(amount) FROM orders
       WHERE order_date = '2024-03-15' AND region = 'US';

Level 1: Partition pruning (table format level)
═══════════════════════════════════════════════
  Table partitioned by month(order_date).
  → Prune all partitions except month=2024-03
  → Eliminate ~92% of manifests/files immediately

Level 2: Manifest pruning (Iceberg-specific)
═════════════════════════════════════════════
  Manifest list has partition summaries per manifest.
  Manifest-0: order_date range [2024-01, 2024-02] → SKIP
  Manifest-1: order_date range [2024-03, 2024-04] → READ
  → Read only surviving manifests

Level 3: File pruning (column statistics in manifest entries)
═════════════════════════════════════════════════════════════
  Manifest-1 entries:
    file-100: region min="AP", max="EU" → SKIP (US not in range)
    file-101: region min="JP", max="US" → READ
    file-102: region min="US", max="US" → READ
  → 2 files out of 50 survive

Level 4: Row group pruning (Parquet footer statistics)
══════════════════════════════════════════════════════
  file-101 has 4 row groups:
    RG-0: order_date min=2024-03-01, max=2024-03-10 → SKIP
    RG-1: order_date min=2024-03-11, max=2024-03-20 → READ
    RG-2: order_date min=2024-03-21, max=2024-03-31 → SKIP
    RG-3: ... → SKIP
  → 1 row group out of 4

Level 5: Page pruning (Parquet column index)
════════════════════════════════════════════
  RG-1, column "order_date", 8 pages:
    Page 0: min=2024-03-11, max=2024-03-12 → SKIP
    Page 1: min=2024-03-13, max=2024-03-14 → SKIP
    Page 2: min=2024-03-15, max=2024-03-16 → READ
    ... rest SKIP
  → 1 page out of 8

Level 6: Column pruning (Parquet columnar layout)
═════════════════════════════════════════════════
  Query needs only "amount" (for SUM) and "order_date" + "region" (for filter).
  Table has 15 columns → read 3, skip 12 → 80% I/O saved

Overall: ~0.01% of raw table data actually read from S3
```

### Statistics Collection

```
Iceberg statistics:
═══════════════════

Column-level stats stored in manifest entries:
  ┌────────────────────────────────────────────────────────────┐
  │  For each data file, for each column:                      │
  │                                                            │
  │  lower_bounds: {1: <binary>, 2: <binary>, ...}  ← min val │
  │  upper_bounds: {1: <binary>, 2: <binary>, ...}  ← max val │
  │  null_value_counts: {1: 0, 2: 150, ...}                   │
  │  nan_value_counts:  {1: 0, 2: 0, ...}                     │
  │  value_counts:      {1: 100000, 2: 99850, ...}            │
  │  column_sizes:      {1: 400000, 2: 2500000, ...}          │
  └────────────────────────────────────────────────────────────┘

  These stats are computed DURING write (no separate "ANALYZE" step).
  Query planners read manifest files (Avro, compact) to get all stats
  in a few S3 GETs — vs. reading Parquet footers of every file.

Puffin files (Iceberg spec, advanced statistics):
  Store NDV (number of distinct values) sketches (Theta Sketch)
  for cost-based optimization in query engines.
  → Stored separately, referenced from metadata.

Delta Lake statistics:
  Stored inline in the transaction log JSON ("stats" field in "add" actions).
  Column-level min/max per file.
  Configurable: delta.dataSkippingNumIndexedCols (default: 32 columns).
```

---

## 15. Production Architecture Patterns

### Medallion Architecture (Bronze/Silver/Gold)

```
The medallion architecture is the dominant lakehouse data organization pattern:

┌───────────────────────────────────────────────────────────────────────┐
│                                                                       │
│  ┌─────────────┐    ┌──────────────┐    ┌──────────────────┐         │
│  │   BRONZE     │    │   SILVER      │    │   GOLD            │         │
│  │  (Raw)       │──► │  (Cleaned)    │──► │  (Business-Ready) │         │
│  └─────────────┘    └──────────────┘    └──────────────────┘         │
│                                                                       │
│  Bronze:                Silver:               Gold:                   │
│  • Raw ingest           • Deduplicated         • Aggregated           │
│  • Append-only          • Schema-enforced      • Star/snowflake       │
│  • All formats (JSON,   • Data types cast       schema                │
│    CSV, Avro, Parquet)  • Nulls handled         • Business metrics    │
│  • Full history         • PII masked            • Pre-joined dims     │
│  • Partition by ingest  • Partition by biz key  • BI-ready            │
│    date                 • Quality-validated     • Low-latency reads   │
│                                                                       │
│  Example tables:        Example tables:         Example tables:       │
│  raw_orders             clean_orders            daily_revenue         │
│  raw_events             clean_events            customer_360          │
│  raw_clickstream        clean_sessions          product_performance   │
│                                                                       │
│  Storage tier:          Storage tier:            Storage tier:         │
│  S3 Standard            S3 Standard             S3 Standard           │
│  (keep 90 days)         (keep 1 year)            (keep indefinitely)  │
│                                                                       │
│  Table format:          Table format:            Table format:        │
│  Iceberg (MoR,          Iceberg (CoW,            Iceberg (CoW,        │
│   append-heavy)          cleaned data)             aggregated data)    │
└───────────────────────────────────────────────────────────────────────┘

Pipeline: Bronze → Silver transformation:
  • Deduplicate by primary key
  • Cast types (string → timestamp, etc.)
  • Apply data quality checks (Great Expectations, Soda, dbt tests)
  • Mask/hash PII columns (GDPR)
  • Write to Silver Iceberg table with MERGE INTO

Pipeline: Silver → Gold transformation:
  • Join fact + dimension tables
  • Compute business metrics (revenue, retention, churn)
  • Pre-aggregate by common dimensions
  • Optimize file layout (sort, z-order) for dashboard queries
```

### Lakehouse on AWS (Reference Architecture)

```
Production AWS Lakehouse:
═════════════════════════

  ┌──────────────────────────────────────────────────────────────┐
  │  Orchestration: Airflow / Step Functions / Dagster           │
  └──────────┬──────────────────────────────┬───────────────────┘
             │                              │
  ┌──────────▼──────────┐      ┌───────────▼────────────────┐
  │  Batch Processing   │      │  Stream Processing          │
  │  Spark on EMR /     │      │  Flink on Kinesis Data      │
  │  Glue / Databricks  │      │  Analytics / MSK Connect    │
  └──────────┬──────────┘      └───────────┬────────────────┘
             │                              │
  ┌──────────▼──────────────────────────────▼───────────────┐
  │  Table Format: Apache Iceberg                           │
  │  Catalog: AWS Glue Data Catalog (Iceberg-compatible)    │
  └──────────┬──────────────────────────────────────────────┘
             │
  ┌──────────▼──────────────────────────────────────────────┐
  │  Storage: Amazon S3 (Standard + IA + Glacier tiers)     │
  │  Layout: s3://lakehouse-prod/{bronze,silver,gold}/db/tbl│
  └─────────────────────────────────────────────────────────┘
             │
  ┌──────────▼──────────────────────────────────────────────┐
  │  Query Engines:                                         │
  │  • Athena (serverless SQL, pay per query)               │
  │  • Redshift Spectrum (federated from Redshift)          │
  │  • Trino/Starburst (multi-source federation)            │
  │  • EMR Spark SQL (complex analytics)                    │
  └─────────────────────────────────────────────────────────┘
             │
  ┌──────────▼──────────────────────────────────────────────┐
  │  Governance:                                            │
  │  • AWS Lake Formation (fine-grained access control)     │
  │  • Column-level and row-level security                  │
  │  • Data lineage via Glue Data Catalog                   │
  │  • PII detection + masking (Macie + custom transforms)  │
  └─────────────────────────────────────────────────────────┘
```

---

## 16. Table Format Comparison and Decision Framework

### Feature Comparison

```
Detailed comparison (2025 state):

┌─────────────────────────────┬──────────────┬──────────────┬──────────────┐
│ Feature                     │ Iceberg      │ Delta Lake   │ Hudi         │
├─────────────────────────────┼──────────────┼──────────────┼──────────────┤
│ Creator                     │ Netflix      │ Databricks   │ Uber         │
│ Apache top-level project    │ Yes          │ Yes          │ Yes          │
│                             │              │              │              │
│ ACID transactions           │ ✓            │ ✓            │ ✓            │
│ Snapshot isolation          │ ✓            │ ✓ (serialzbl)│ ✓            │
│ Time travel                 │ ✓            │ ✓            │ ✓            │
│ Schema evolution            │ Full (ID-    │ Add/rename   │ Add columns  │
│                             │ based)       │ (mapping)    │              │
│ Partition evolution         │ ✓ (no rewrite│ ✗ (rewrite)  │ ✗            │
│                             │  needed)     │              │              │
│ Hidden partitioning         │ ✓            │ ✗ (generated │ ✗            │
│                             │              │  columns)    │              │
│ Row-level deletes           │ CoW + MoR    │ CoW + DV     │ CoW + MoR   │
│                             │ (pos/eq)     │              │ (native)     │
│ Merge-on-read               │ ✓ (position  │ ✓ (deletion  │ ✓ (delta     │
│                             │  deletes)    │  vectors)    │  logs)       │
│ Streaming ingest            │ ✓ (Flink)    │ ✓ (Spark SS) │ ✓ (native)  │
│ Incremental queries         │ ✓            │ ✓ (CDF)      │ ✓ (native)  │
│ Record-level index          │ ✗            │ ✗            │ ✓ (bloom/   │
│                             │              │              │  HBase/etc) │
│                             │              │              │              │
│ Multi-engine support        │ Broadest     │ Spark-centric│ Spark/Flink │
│ Catalog standard            │ REST Catalog │ Unity Catalog│ Hive MS     │
│ File formats                │ Parquet, ORC,│ Parquet only │ Parquet      │
│                             │ Avro         │              │ (primary)   │
│                             │              │              │              │
│ Planning scalability        │ O(manifests) │ O(log files) │ O(timeline) │
│ (10M+ files)                │ excellent    │ good (ckpt)  │ moderate    │
│                             │              │              │              │
│ Community momentum (2025)   │ Very high    │ High         │ Moderate    │
│ Adoption trajectory         │ Accelerating │ Stable       │ Niche       │
└─────────────────────────────┴──────────────┴──────────────┴──────────────┘
```

### Decision Framework

```
Which table format should you choose?
══════════════════════════════════════

Start here:
  │
  ├── Using Databricks as your primary platform?
  │   └── Yes → Delta Lake (native, best optimized, Unity Catalog)
  │
  ├── Need multi-engine interop (Spark + Trino + Flink + Snowflake)?
  │   └── Yes → Iceberg (broadest engine support, open REST catalog)
  │
  ├── Primary use case is CDC from OLTP with near-real-time upserts?
  │   └── Yes → Hudi (purpose-built for upserts, record-level index)
  │
  ├── Need partition evolution (change partitioning without rewrite)?
  │   └── Yes → Iceberg (unique capability)
  │
  ├── Want git-like branching for data (dev/staging/prod)?
  │   └── Yes → Iceberg + Nessie catalog
  │
  ├── Already using Snowflake and want open lakehouse alongside?
  │   └── Yes → Iceberg (Snowflake's Polaris catalog, native support)
  │
  └── Unsure / greenfield?
      └── Iceberg (largest community, broadest compatibility, most features)

The honest take (2025):
  Iceberg has won the format war. Delta Lake remains strong in Databricks.
  Hudi serves a niche (CDC/upserts). New projects should default to Iceberg
  unless a specific platform (Databricks) or use case (CDC) dictates otherwise.

  UniForm (Databricks) and format interop layers are blurring the lines:
  Delta UniForm writes Delta + Iceberg metadata simultaneously,
  so the "which format" question is becoming less important over time.
```

---

## 17. Anti-Patterns and Pitfalls

### The Deadly Sins of Lakehouse Operations

```
1. Ignoring small-file compaction
   ─────────────────────────────
   Symptom: queries that once took 2s now take 60s.
   Cause: streaming ingest created 500,000 files of 5 MB each.
   Fix: schedule compaction (rewrite_data_files) every 1-6 hours.
   Prevention: set target file size (256 MB), use auto-compaction.

2. Never expiring snapshots
   ────────────────────────
   Symptom: S3 bill doubles every quarter despite stable data volume.
   Cause: 100,000 snapshots, each referencing its own set of files.
   Files can't be deleted because some snapshot references them.
   Fix: expire_snapshots + remove_orphan_files on a schedule.
   Retention: 7 days for hot tables, 30 days for audit-required.

3. Over-partitioning
   ──────────────────
   Symptom: query planning takes 30 seconds before any data is read.
   Cause: PARTITIONED BY (year, month, day, hour, user_region, device_type)
   → 365 × 24 × 10 × 5 = 438,000 partitions.
   → Each partition might have 1 tiny file.
   Fix: partition by month(event_time) only. Use sort/z-order for
   sub-partition locality.
   Rule of thumb: aim for 100–10,000 partitions, not millions.

4. Not sorting within files
   ────────────────────────
   Symptom: column statistics (min/max) span the entire value range
   for every file. No file pruning happens.
   Cause: data written in random insertion order.
   Fix: sort by the most-filtered column (usually timestamp) during
   compaction. Z-order for multi-column filter workloads.

5. Using Parquet with Hive Metastore and calling it a "lakehouse"
   ──────────────────────────────────────────────────────────────
   Symptom: concurrent writers corrupt the table. No time travel.
   No schema enforcement. Readers see partial writes.
   Cause: Hive Metastore tracks partitions but not files. It's a catalog,
   not a table format. Without Iceberg/Delta/Hudi, there's no transaction log.
   Fix: adopt a table format. Iceberg + REST catalog is the modern path.

6. Treating the lakehouse as a database
   ────────────────────────────────────
   Symptom: running single-row lookups or high-concurrency OLTP queries
   against lakehouse tables.
   Cause: lakehouse is optimized for analytical scans, not point lookups.
   Minimum read unit is a Parquet column chunk (~1 MB). S3 GET latency
   is ~50-200ms. Unsuitable for p99 < 10ms SLAs.
   Fix: use OLTP databases for transactional workloads. Feed them into
   the lakehouse via CDC for analytics.

7. Skipping data quality validation in the Bronze → Silver pipeline
   ────────────────────────────────────────────────────────────────
   Symptom: dashboards show impossible values. ML models trained on
   corrupted features.
   Cause: "Bronze = raw, it's fine" mindset. Bad data flows through
   unchecked.
   Fix: enforce constraints at Silver layer: NOT NULL checks, range
   validation, referential integrity checks, deduplication.
   Tools: dbt tests, Great Expectations, Soda, Iceberg table constraints.
```

---

*This chapter complements [08-olap-databases.md](./08-olap-databases.md) (which covers OLAP architecture broadly, including a brief overview of the lakehouse concept) by providing a comprehensive deep dive into the data lake, lakehouse, and open table format ecosystem. For the columnar file encoding internals that underpin Parquet, see [02-data-storage-formats-and-encoding.md](./02-data-storage-formats-and-encoding.md). For query engine fundamentals, see [04-query-engine-internals.md](./04-query-engine-internals.md). For in-process analytical engines that query Parquet and Iceberg directly, see [21-in-process-olap-duckdb-chdb.md](./21-in-process-olap-duckdb-chdb.md).*
