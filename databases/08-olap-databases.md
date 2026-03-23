# OLAP Databases: A Staff-Engineer Deep Dive

A comprehensive guide to Online Analytical Processing databases, columnar storage, vectorized execution, and the modern analytical data stack.

---

## Table of Contents

1. [What Makes a Database OLAP](#1-what-makes-a-database-olap)
2. [Column-Oriented Storage Deep Dive](#2-column-oriented-storage-deep-dive)
3. [Vectorized Query Execution](#3-vectorized-query-execution)
4. [ClickHouse Deep Dive](#4-clickhouse-deep-dive)
5. [Snowflake Architecture](#5-snowflake-architecture)
6. [Google BigQuery Architecture](#6-google-bigquery-architecture)
7. [Amazon Redshift](#7-amazon-redshift)
8. [DuckDB](#8-duckdb)
9. [Apache Data Formats](#9-apache-data-formats)
10. [MPP Architecture Patterns](#10-mpp-architecture-patterns)
11. [Data Lakehouse](#11-data-lakehouse)
12. [OLAP Performance Optimization](#12-olap-performance-optimization)

---

## 1. What Makes a Database OLAP

### OLTP vs OLAP: Fundamental Differences

| Characteristic | OLTP | OLAP |
|----------------|------|------|
| **Primary use** | Transaction processing | Analytical queries |
| **Query pattern** | Point lookups, small ranges | Full scans, aggregations |
| **Rows per query** | 1 - 1,000 | 1,000,000 - 1,000,000,000 |
| **Columns per query** | Many (SELECT *) | Few (SELECT col1, col2) |
| **Concurrency** | 1,000s of users | 10s of analysts |
| **Latency target** | < 10 ms | Seconds to minutes |
| **Data freshness** | Real-time | Minutes to hours (batch) |
| **Write pattern** | Single-row inserts/updates | Bulk loads, append-only |
| **Schema design** | Normalized (3NF) | Denormalized (star/snowflake) |
| **Indexing** | B-Tree, Hash | Zone maps, sparse indexes |
| **Storage layout** | Row-oriented | Column-oriented |
| **Typical size** | GB to low TB | TB to PB |

### OLAP Workload Characteristics

```
OLTP Query (point lookup):                OLAP Query (analytical scan):
SELECT name, email, balance               SELECT region,
FROM users                                       product_category,
WHERE user_id = 42;                               SUM(revenue),
                                                  COUNT(DISTINCT customer_id)
Reads: 1 row, all columns                 FROM sales
I/O:   Single page fetch                  WHERE sale_date BETWEEN '2024-01-01'
Time:  < 1 ms                                                 AND '2024-12-31'
                                           GROUP BY region, product_category
                                           HAVING SUM(revenue) > 1000000
                                           ORDER BY SUM(revenue) DESC;

                                           Reads: 500M rows, 4 columns out of 30
                                           I/O:   Sequential scan of column files
                                           Time:  2-10 seconds
```

### Star Schema

The star schema is the dominant modeling pattern for OLAP. A central **fact table** records measurable events, surrounded by **dimension tables** that provide context.

```
                        ┌──────────────┐
                        │  dim_product  │
                        ├──────────────┤
                        │ product_id   │
                        │ name         │
                        │ category     │
                        │ brand        │
                        │ unit_price   │
                        └──────┬───────┘
                               │
┌──────────────┐    ┌─────────┴────────────┐    ┌──────────────┐
│  dim_store    │    │    fact_sales         │    │  dim_date     │
├──────────────┤    ├──────────────────────┤    ├──────────────┤
│ store_id     │◄───┤ sale_id              │───►│ date_id       │
│ store_name   │    │ date_id         (FK) │    │ date          │
│ city         │    │ product_id      (FK) │    │ day_of_week   │
│ state        │    │ store_id        (FK) │    │ month         │
│ region       │    │ customer_id     (FK) │    │ quarter       │
└──────────────┘    │ quantity             │    │ year          │
                    │ unit_price           │    │ is_holiday    │
┌──────────────┐    │ discount             │    └──────────────┘
│ dim_customer  │    │ total_amount         │
├──────────────┤    │ tax                  │
│ customer_id  │◄───┤ profit               │
│ name         │    └──────────────────────┘
│ segment      │
│ region       │        Fact table: millions/billions of rows
│ join_date    │        Dimension tables: thousands of rows
└──────────────┘
```

### Snowflake Schema

A snowflake schema normalizes dimension tables into sub-dimensions, reducing redundancy at the cost of more joins.

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ dim_brand    │───►│ dim_product  │    │ dim_region   │
├─────────────┤    ├─────────────┤    ├─────────────┤
│ brand_id    │    │ product_id  │    │ region_id   │
│ brand_name  │    │ name        │    │ region_name │
│ country     │    │ category_id │    │ country     │
└─────────────┘    │ brand_id    │    └──────┬──────┘
                   └──────┬──────┘           │
┌─────────────┐          │           ┌──────┴──────┐
│ dim_category │──────────┘           │ dim_store    │
├─────────────┤                      ├─────────────┤
│ category_id │    ┌────────────┐    │ store_id    │
│ cat_name    │    │ fact_sales  │───►│ store_name  │
│ department  │    ├────────────┤    │ region_id   │
└─────────────┘    │ ...        │    └─────────────┘
                   └────────────┘
```

### Data Vault Modeling

Data Vault is designed for auditability and incremental loading in data warehouses. It uses three entity types:

```
┌────────────┐         ┌─────────────────────┐         ┌────────────┐
│  HUB_Cust  │         │   LINK_Sale         │         │ HUB_Product │
├────────────┤         ├─────────────────────┤         ├────────────┤
│ hub_cust_hk│◄────────┤ link_sale_hk        │────────►│ hub_prod_hk│
│ cust_bk    │         │ hub_cust_hk    (FK) │         │ prod_bk    │
│ load_date  │         │ hub_prod_hk    (FK) │         │ load_date  │
│ rec_source │         │ load_date           │         │ rec_source │
└─────┬──────┘         │ rec_source          │         └─────┬──────┘
      │                └─────────────────────┘               │
      │                                                      │
┌─────┴──────┐                                         ┌─────┴──────┐
│ SAT_Cust   │     HUB  = Business keys (immutable)    │ SAT_Product │
├────────────┤     LINK = Relationships                 ├────────────┤
│ hub_cust_hk│     SAT  = Descriptive attributes        │ hub_prod_hk│
│ load_date  │           (historized, append-only)       │ load_date  │
│ name       │                                          │ name       │
│ email      │                                          │ price      │
│ segment    │                                          │ category   │
└────────────┘                                          └────────────┘
```

### Why Column-Oriented Storage Is Ideal for Analytics

Consider a query that reads 3 columns out of a 30-column table with 1 billion rows:

```
Row Store:  Must read ALL 30 columns x 1B rows = scan entire table
            I/O = 30 columns x 8 bytes avg x 1B rows = 240 GB

Column Store: Read ONLY 3 needed columns
              I/O = 3 columns x 8 bytes avg x 1B rows = 24 GB
              + compression (typically 5-10x) = 2.4 - 4.8 GB

              Result: 50-100x less I/O than row store
```

---

## 2. Column-Oriented Storage Deep Dive

### Row Store vs Column Store: Physical Layout on Disk

```
Original Table (4 rows, 4 columns):
┌────┬─────────┬─────┬────────┐
│ ID │  Name   │ Age │ City   │
├────┼─────────┼─────┼────────┤
│  1 │ Alice   │  30 │ NYC    │
│  2 │ Bob     │  25 │ LA     │
│  3 │ Charlie │  30 │ NYC    │
│  4 │ Diana   │  28 │ Chicago│
└────┴─────────┴─────┴────────┘

═══════════════════════════════════════════════════════════════

ROW STORE (PostgreSQL, MySQL):
Data stored row-by-row in 8KB pages

Page 1:
┌──────────────────────────────────────────────────────────┐
│ [1, Alice, 30, NYC] [2, Bob, 25, LA] [3, Charlie, 30,  │
│  NYC] [4, Diana, 28, Chicago]                            │
└──────────────────────────────────────────────────────────┘
  ▲ Row 1 fields     ▲ Row 2 fields    ▲ Row 3 fields
    are contiguous      are contiguous     are contiguous

  SELECT * FROM t WHERE id = 2;   --> Fast! Read one tuple.
  SELECT AVG(age) FROM t;         --> Slow! Must read all columns.

═══════════════════════════════════════════════════════════════

COLUMN STORE (ClickHouse, Redshift, Parquet):
Each column stored in a separate file/segment

File: id.col       File: name.col       File: age.col      File: city.col
┌─────────────┐    ┌─────────────────┐   ┌────────────┐    ┌─────────────┐
│ 1, 2, 3, 4  │    │ Alice, Bob,     │   │ 30, 25,    │    │ NYC, LA,    │
│              │    │ Charlie, Diana  │   │ 30, 28     │    │ NYC, Chicago│
└─────────────┘    └─────────────────┘   └────────────┘    └─────────────┘

  SELECT AVG(age) FROM t;         --> Fast! Read only age.col (4 values).
  SELECT * FROM t WHERE id = 2;   --> Slow! Must read all column files,
                                        reconstruct row at position 2.
```

### Why Column Stores Win for Analytics

**1. I/O Reduction: Read Only Needed Columns**

```
Query: SELECT city, SUM(revenue) FROM sales GROUP BY city;

Table has 50 columns, 1 billion rows.

Row Store:
  Read = 1B rows x 50 cols x avg 8 bytes = 400 GB from disk
  Used = only 2 columns (city + revenue)
  Waste = 96% of I/O is thrown away

Column Store:
  Read = 1B x 8 bytes (city) + 1B x 8 bytes (revenue) = 16 GB
  Used = 100% of I/O
  + compression = ~2-3 GB actual disk reads

  Speed improvement: ~100-200x less I/O
```

**2. Superior Compression (Similar Values Together)**

```
Row store bytes (mixed types, low locality):
┌───────────────────────────────────────────────────────┐
│ 1|Alice|30|NYC|2|Bob|25|LA|3|Charlie|30|NYC|4|Diana...│
└───────────────────────────────────────────────────────┘
  Compression ratio: ~2-3x (mixed types, poor locality)

Column store bytes (same type, high locality):
age.col:  [30, 25, 30, 28, 31, 25, 30, 29, 30, 25, ...]
           All integers, narrow range --> ~10-20x compression

city.col: [NYC, LA, NYC, Chicago, NYC, LA, NYC, NYC, ...]
           Few distinct values --> dictionary encode --> ~50-100x
```

**3. SIMD-Friendly Vectorized Processing**

```
Column data in memory (age column, int32):
┌────┬────┬────┬────┬────┬────┬────┬────┐
│ 30 │ 25 │ 30 │ 28 │ 31 │ 25 │ 30 │ 29 │  8 x int32 = 256 bits
└────┴────┴────┴────┴────┴────┴────┴────┘

Single AVX2 instruction (256-bit SIMD register):
  VPCMPGTD ymm0, ymm1, ymm2    // Compare 8 ints simultaneously
  Result: 8 comparisons in 1 CPU cycle instead of 8 cycles

Row store: values scattered across memory, no SIMD possible
```

### Column Compression Techniques

**Run-Length Encoding (RLE)**

Best for sorted columns with long runs of repeated values.

```
Original:  [NYC, NYC, NYC, NYC, LA, LA, Chicago, Chicago, Chicago]
Encoded:   [(NYC, 4), (LA, 2), (Chicago, 3)]

Storage: 9 strings --> 3 (value, count) pairs
Compression: ~3x for this example, up to 1000x for sorted columns

Ideal for: sorted dimension columns, status fields, boolean flags
```

**Dictionary Encoding**

Replace strings with integer codes. Near-universal in column stores.

```
Dictionary:                  Encoded column:
┌────┬─────────┐            ┌───┬───┬───┬───┬───┬───┬───┬───┐
│ 0  │ NYC     │            │ 0 │ 1 │ 0 │ 2 │ 0 │ 1 │ 0 │ 0 │
│ 1  │ LA      │            └───┴───┴───┴───┴───┴───┴───┴───┘
│ 2  │ Chicago │              8 values x 2 bits = 16 bits = 2 bytes
└────┴─────────┘              vs. original: 8 x ~6 bytes = 48 bytes
  3 entries                   Compression: 24x

Key insight: queries can operate on dictionary codes directly.
  WHERE city = 'NYC'  -->  WHERE city_code = 0  (integer comparison)
```

**Bit-Packing**

Use the minimum number of bits needed for the value range.

```
Values:    [3, 1, 4, 1, 5, 9, 2, 6]
Max value: 9 --> needs 4 bits (0-15 range)

Standard int32:  8 values x 32 bits = 256 bits
Bit-packed:      8 values x  4 bits =  32 bits

  ┌────┬────┬────┬────┬────┬────┬────┬────┐
  │0011│0001│0100│0001│0101│1001│0010│0110│  = 32 bits = 4 bytes
  └────┴────┴────┴────┴────┴────┴────┴────┘

Compression: 8x
```

**Delta Encoding**

Store differences between consecutive values. Ideal for timestamps and monotonically increasing sequences.

```
Original timestamps (int64, 8 bytes each):
  [1704067200, 1704067260, 1704067320, 1704067380, 1704067440]

Delta encoded:
  Base:   1704067200
  Deltas: [0, 60, 60, 60, 60]

  Original:  5 x 8 bytes = 40 bytes
  Deltas:    8 bytes (base) + 4 x 1 byte (deltas fit in uint8) = 12 bytes
  Compression: 3.3x

Combine with bit-packing: deltas are all 60, so RLE --> (60, 4)
  Final: 8 + 2 bytes = 10 bytes --> 4x compression
```

**Frame of Reference (FOR)**

Store a block of values as offsets from a minimum reference value.

```
Block of 8 values: [1001, 1005, 1003, 1007, 1002, 1004, 1006, 1008]

Min (reference): 1001
Offsets:         [0, 4, 2, 6, 1, 3, 5, 7]

Max offset: 7 --> 3 bits per value
Storage: 8 values x 3 bits = 24 bits = 3 bytes  (+4 bytes for reference)
vs. original: 8 x 4 bytes (int32) = 32 bytes
Compression: ~4.6x
```

**Patched Frame of Reference (PFOR)**

Handles outliers by storing exceptions separately.

```
Values: [100, 102, 101, 103, 99, 9999, 101, 100]
                                  ^^^^
                                  outlier!

Regular FOR: max offset = 9900 --> 14 bits per value = expensive

PFOR approach:
  Reference: 99
  Normal offsets (3 bits): [1, 3, 2, 4, 0, _, 2, 1]  (skip outlier)
  Exception list: [(position=5, value=9999)]

  Storage: 7 x 3 bits + 1 exception (8 bytes) = ~11 bytes
  vs. FOR: 8 x 14 bits = 14 bytes
  vs. raw: 8 x 4 bytes = 32 bytes
```

### Late Materialization vs Early Materialization

```
Query: SELECT name FROM users WHERE age > 30 AND city = 'NYC';

EARLY MATERIALIZATION (reconstruct rows first, then filter):
  Step 1: Read age[], city[], name[] columns
  Step 2: For each position, construct tuple (age, city, name)
  Step 3: Apply filter: age > 30 AND city = 'NYC'
  Step 4: Return name from matching tuples

  Problem: reconstructed full tuples before filtering
           wasted memory and CPU on rows that don't match

LATE MATERIALIZATION (filter on column vectors, reconstruct last):
  Step 1: Read age[] --> filter age > 30 --> bitmap A = [0,1,0,1,1,0,...]
  Step 2: Read city[] --> filter = 'NYC'  --> bitmap B = [1,0,1,0,1,0,...]
  Step 3: AND bitmaps: A & B = [0,0,0,0,1,0,...]  (position 4 matches)
  Step 4: Read name[] only at position 4 --> "Eve"

  Benefit: never read name[] for non-matching rows
           filter operates on compressed dictionary codes
           bitmap AND is a single CPU instruction per 64 positions

  Late materialization is typically 2-5x faster.
```

### Column Groups (Hybrid Row-Column)

Some systems group frequently co-accessed columns together:

```
Table: users (id, name, email, age, city, created_at, ...)

Column Group 1 (identity):    [id, name, email]     -- often queried together
Column Group 2 (demographics): [age, city]           -- analytics grouping
Column Group 3 (metadata):     [created_at, ...]     -- rarely accessed

Each group stored in row format internally, but groups are columnar:

┌─ Group 1 file ──────────────────┐  ┌─ Group 2 file ───────┐
│ (1,Alice,alice@..) (2,Bob,bob@.)│  │ (30,NYC) (25,LA) ... │
└─────────────────────────────────┘  └───────────────────────┘

Used by: SQL Server columnstore, SAP HANA, some Parquet configs
Benefit: best of both worlds for mixed workloads (HTAP)
```

---

## 3. Vectorized Query Execution

### The Problem with Tuple-at-a-Time

Traditional databases (Volcano/iterator model) process one row at a time:

```
TUPLE-AT-A-TIME (Volcano Iterator Model):

  while (row = scan.next()) {        // virtual function call per row
      if (filter.evaluate(row)) {    // branch prediction miss
          result = project(row);     // pointer chasing
          output.emit(result);       // function call overhead
      }
  }

  For 1 billion rows:
    - 1B virtual function calls (scan.next)
    - 1B filter evaluations with branching
    - Constant L1/L2 cache misses (random memory access)
    - ~90% of CPU time spent on interpretation overhead, not actual work
```

### Batch-at-a-Time (Vectorized Execution)

Process a vector (batch) of 1024-4096 values at once:

```
VECTORIZED EXECUTION (MonetDB/X100 Model):

  while (batch = scan.nextBatch(1024)) {   // 1 call per 1024 rows
      mask = filter.evaluate(batch);        // tight loop, no branches
      result = project(batch, mask);        // SIMD-friendly
      output.emit(result, mask);
  }

  For 1 billion rows:
    - ~1M function calls (1B / 1024) instead of 1B
    - Filter runs as tight loop: CPU branch predictor works perfectly
    - Data fits in L1/L2 cache (1024 x 8 bytes = 8KB per column)
    - Compiler auto-vectorizes tight loops to SIMD instructions
```

### Vectorized Filter Example

```
Filter: WHERE age > 30

Scalar (tuple-at-a-time):              Vectorized (batch):

for i in 0..N:                          // Process 8 ages at once (AVX2)
    if ages[i] > 30:                    threshold = [30,30,30,30,30,30,30,30]
        result.add(i)
                                        for i in 0..N step 8:
                                            chunk = LOAD_256(ages + i)
Branch per element:                         mask  = CMP_GT_256(chunk, threshold)
  - branch misprediction ~50%               STORE_MASK(result + i/8, mask)
  - ~4 cycles per element
                                        No branches:
                                          - 8 comparisons per cycle
                                          - ~0.5 cycles per element
                                          - 8x faster from SIMD alone
```

### Operating on Compressed Data

Vectorized engines can often skip decompression entirely:

```
Query: SELECT COUNT(*) FROM sales WHERE city = 'NYC'

Dictionary-encoded city column:
  Dictionary: {0: 'NYC', 1: 'LA', 2: 'Chicago'}
  Data: [0, 1, 0, 2, 0, 1, 0, 0, 2, 0, ...]  (int8 codes)

Execution WITHOUT decompression:
  1. Lookup 'NYC' in dictionary --> code = 0
  2. Vectorized scan: count where data[i] == 0
  3. Never materialize the string 'NYC' at all

  // Tight SIMD loop comparing int8 codes
  for i in 0..N step 32:   // 32 x int8 = 256 bits = one AVX2 register
      chunk = LOAD_256(data + i)
      zeros = SET_ALL_256(0)
      mask  = CMP_EQ_8x32(chunk, zeros)
      count += POPCOUNT(mask)

  Processing rate: ~32 values per CPU cycle = ~30 billion values/sec/core
```

### Cache-Friendly Access Patterns

```
TUPLE-AT-A-TIME memory access:            VECTORIZED memory access:

Row 1: [id][name][age][city][...]          age column: [30][25][30][28][31][25]...
Row 2: [id][name][age][city][...]                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Row 3: [id][name][age][city][...]                      Sequential! Prefetcher loves this.
       ............ skip 200 bytes                     Entire column in L1 cache.
       between age values

Cache line = 64 bytes                      Cache line = 64 bytes
Row store: 1-2 useful values per line      Column store: 8-16 values per line
Cache utilization: ~10%                    Cache utilization: ~100%
```

### Performance Comparison

| Processing Model | Throughput | Overhead |
|------------------|-----------|----------|
| Tuple-at-a-time (Volcano) | ~10M rows/sec/core | High (virtual calls, branching) |
| Vectorized (batch 1024) | ~500M rows/sec/core | Low (tight loops, SIMD) |
| Compiled (JIT) | ~1B rows/sec/core | Compilation time |
| Vectorized on compressed | ~2-5B rows/sec/core | Near-zero |

---

## 4. ClickHouse Deep Dive

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     ClickHouse Cluster                          │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │   Shard 1    │  │   Shard 2    │  │   Shard 3    │          │
│  │ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │          │
│  │ │Replica 1a│ │  │ │Replica 2a│ │  │ │Replica 3a│ │          │
│  │ │(primary) │ │  │ │(primary) │ │  │ │(primary) │ │          │
│  │ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │          │
│  │ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │          │
│  │ │Replica 1b│ │  │ │Replica 2b│ │  │ │Replica 3b│ │          │
│  │ │(standby) │ │  │ │(standby) │ │  │ │(standby) │ │          │
│  │ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                  ZooKeeper / ClickHouse Keeper             │  │
│  │          (metadata, replication log, DDL coordination)     │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### MergeTree Engine Family

The MergeTree is ClickHouse's core storage engine. All variants share the LSM-tree-inspired write path.

```
INSERT DATA (batch)
       │
       ▼
┌──────────────┐    Immutable data part (sorted by primary key)
│  New Part     │    Written to disk immediately (no WAL needed)
│  (unsorted    │    Each part = directory of column files
│   within      │           part_1/
│   partition)  │             ├── id.bin        (column data)
└──────┬───────┘             ├── id.mrk3       (mark file / sparse index)
       │                      ├── name.bin
       │                      ├── name.mrk3
       ▼                      ├── primary.idx   (sparse primary index)
┌──────────────┐             └── ...
│ Background   │
│ Merge Thread │    Merges small parts into larger parts
│              │    (like LSM compaction)
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Merged Part  │    Fewer, larger parts = faster queries
│ (sorted,     │    Old parts deleted after merge
│  compacted)  │
└──────────────┘
```

**Engine Variants:**

| Engine | Behavior on Merge | Use Case |
|--------|-------------------|----------|
| `MergeTree` | Keep all rows | General analytics, event logs |
| `ReplacingMergeTree` | Deduplicate by sort key (keep latest version) | Mutable dimension tables |
| `SummingMergeTree` | Sum numeric columns for same sort key | Pre-aggregated counters |
| `AggregatingMergeTree` | Merge aggregate function states | Materialized aggregates |
| `CollapsingMergeTree` | Cancel rows with Sign = -1/+1 | Mutable data via cancel+reinsert |
| `VersionedCollapsingMergeTree` | Collapsing with version ordering | Out-of-order mutable inserts |

**Example: SummingMergeTree**

```sql
CREATE TABLE daily_metrics (
    date       Date,
    site_id    UInt32,
    views      UInt64,
    clicks     UInt64,
    revenue    Float64
) ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (date, site_id);

-- Multiple inserts for same (date, site_id):
INSERT INTO daily_metrics VALUES ('2024-01-15', 1, 100, 10, 50.0);
INSERT INTO daily_metrics VALUES ('2024-01-15', 1, 200, 20, 75.0);

-- After merge, automatically becomes:
-- ('2024-01-15', 1, 300, 30, 125.0)
```

### Primary Index: Sparse Index (Not B-Tree!)

```
ClickHouse does NOT use B-Trees. It uses a sparse index that stores
one entry per granule (default: 8192 rows).

Data (sorted by primary key: date, user_id):
┌──────────────────────────────────────────────────┐
│ Granule 0 (rows 0-8191)                          │
│   date: 2024-01-01, user_id: 1                   │  ◄── Index entry 0
│   date: 2024-01-01, user_id: 2                   │
│   ... 8190 more rows ...                         │
├──────────────────────────────────────────────────┤
│ Granule 1 (rows 8192-16383)                      │
│   date: 2024-01-01, user_id: 8500                │  ◄── Index entry 1
│   ... 8191 more rows ...                         │
├──────────────────────────────────────────────────┤
│ Granule 2 (rows 16384-24575)                     │
│   date: 2024-01-02, user_id: 200                 │  ◄── Index entry 2
│   ... 8191 more rows ...                         │
└──────────────────────────────────────────────────┘

Primary index (loaded entirely into memory):
┌─────────┬────────────┬──────────┐
│ Granule │    date    │ user_id  │
├─────────┼────────────┼──────────┤
│    0    │ 2024-01-01 │       1  │
│    1    │ 2024-01-01 │    8500  │
│    2    │ 2024-01-02 │     200  │
│   ...   │    ...     │    ...   │
└─────────┴────────────┴──────────┘

For 1 billion rows: 1B / 8192 = ~122,000 index entries
At ~16 bytes each = ~2 MB in memory (tiny!)

Query: WHERE date = '2024-01-02' AND user_id = 500
  --> Binary search index: granule 2 matches
  --> Read only granule 2 (8192 rows) instead of 1B rows
```

### Data Skipping Indexes

```sql
-- MinMax index: stores min/max per granule group
ALTER TABLE events ADD INDEX idx_amount minmax(amount) GRANULARITY 4;
-- "Granularity 4" means one min/max entry per 4 granules (32,768 rows)
-- WHERE amount > 1000 skips granule groups where max(amount) <= 1000

-- Set index: stores set of unique values per granule group
ALTER TABLE events ADD INDEX idx_status set(status, 100) GRANULARITY 1;
-- WHERE status = 'error' skips granules that don't contain 'error'

-- Bloom filter index: probabilistic membership test
ALTER TABLE logs ADD INDEX idx_msg bloom_filter(0.01) GRANULARITY 1;
-- WHERE message = 'timeout' skips granules (1% false positive rate)

-- Ngram bloom filter: for LIKE/substring queries
ALTER TABLE logs ADD INDEX idx_msg_ngram ngrambf_v1(3, 256, 2, 0) GRANULARITY 4;
-- WHERE message LIKE '%timeout%' can use this index
```

### Materialized Views for Pre-Aggregation

```sql
-- Source table: raw events (billions of rows)
CREATE TABLE raw_events (
    timestamp DateTime,
    user_id   UInt64,
    event     String,
    duration  Float64
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (timestamp, user_id);

-- Materialized view: auto-maintained hourly rollup
CREATE MATERIALIZED VIEW hourly_stats
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(hour)
ORDER BY (hour, event)
AS SELECT
    toStartOfHour(timestamp) AS hour,
    event,
    count()                  AS event_count,
    sum(duration)            AS total_duration,
    uniqState(user_id)       AS unique_users  -- HLL sketch
FROM raw_events
GROUP BY hour, event;

-- Query hits the materialized view (thousands of rows, not billions):
SELECT hour, event, event_count, total_duration,
       uniqMerge(unique_users) AS unique_users
FROM hourly_stats
WHERE hour >= '2024-01-01' AND hour < '2024-02-01'
GROUP BY hour, event;
```

### When to Use ClickHouse

| Scenario | Fit |
|----------|-----|
| Event/log analytics (append-only) | Excellent |
| Time-series metrics | Excellent |
| Real-time dashboards | Excellent |
| Ad-hoc analytical queries | Good |
| Point lookups by primary key | Acceptable |
| Frequent single-row updates | Poor (use CollapsingMergeTree) |
| ACID transactions | Not supported |
| Joins on large tables | Limited (prefer denormalized) |

---

## 5. Snowflake Architecture

### Three-Layer Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                      CLOUD SERVICES LAYER                           │
│                        (the "brain")                                │
│                                                                     │
│  ┌──────────┐ ┌──────────────┐ ┌────────────┐ ┌────────────────┐   │
│  │  Query    │ │ Metadata     │ │ Access     │ │ Infrastructure │   │
│  │  Optimizer│ │ Management   │ │ Control    │ │ Management     │   │
│  │  & Planner│ │ (micro-part  │ │ (auth,     │ │ (auto-scale,   │   │
│  │          │ │  catalog,    │ │  RBAC)     │ │  auto-suspend) │   │
│  │          │ │  statistics) │ │            │ │                │   │
│  └──────────┘ └──────────────┘ └────────────┘ └────────────────┘   │
│                                                                     │
│  Always-on, shared across all warehouses, manages query planning    │
├─────────────────────────────────────────────────────────────────────┤
│                    VIRTUAL WAREHOUSES LAYER                          │
│                       (compute)                                     │
│                                                                     │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐           │
│  │  Warehouse A  │  │  Warehouse B  │  │  Warehouse C  │           │
│  │  (XS - ETL)   │  │  (L - Analysts)│  │  (XL - DS/ML) │           │
│  │               │  │               │  │               │           │
│  │  ┌───┐ ┌───┐  │  │  ┌───┐ ┌───┐  │  │  ┌───┐ ┌───┐  │           │
│  │  │CPU│ │CPU│  │  │  │CPU│ │CPU│  │  │  │CPU│ │CPU│  │           │
│  │  │SSD│ │SSD│  │  │  │SSD│ │SSD│  │  │  │SSD│ │SSD│  │           │
│  │  └───┘ └───┘  │  │  └───┘ └───┘  │  │  └───┘ └───┘  │           │
│  │  Local SSD    │  │  Local SSD    │  │  Local SSD    │           │
│  │  Cache        │  │  Cache        │  │  Cache        │           │
│  └───────────────┘  └───────────────┘  └───────────────┘           │
│                                                                     │
│  Independently scalable, can be started/stopped, per-second billing │
├─────────────────────────────────────────────────────────────────────┤
│                    CENTRALIZED STORAGE LAYER                        │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │              S3 / Azure Blob / GCS                          │    │
│  │                                                             │    │
│  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐   │    │
│  │  │ Micro  │ │ Micro  │ │ Micro  │ │ Micro  │ │ Micro  │   │    │
│  │  │Partitn │ │Partitn │ │Partitn │ │Partitn │ │Partitn │   │    │
│  │  │50-500MB│ │50-500MB│ │50-500MB│ │50-500MB│ │50-500MB│   │    │
│  │  │columnar│ │columnar│ │columnar│ │columnar│ │columnar│   │    │
│  │  └────────┘ └────────┘ └────────┘ └────────┘ └────────┘   │    │
│  │                                                             │    │
│  │  Immutable files, automatic compression, managed by cloud   │    │
│  │  services layer. All warehouses read from the same data.    │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

### Micro-Partitions

```
Table: sales (10 billion rows)
  --> Stored as ~50,000 micro-partitions (each 50-500 MB compressed)

Single Micro-Partition:
┌─────────────────────────────────────────────────────┐
│  Header: metadata (row count, column offsets, ...)  │
├──────────┬──────────┬──────────┬───────────────────┤
│ col: date│col: city │col: amt  │ col: product_id   │
│ (sorted) │ (dict)   │ (float)  │ (int)             │
│          │          │          │                   │
│ min/max  │ min/max  │ min/max  │ min/max           │
│ distinct │ distinct │ distinct │ distinct          │
│ null_cnt │ null_cnt │ null_cnt │ null_cnt          │
├──────────┴──────────┴──────────┴───────────────────┤
│  Column data (compressed, columnar within file)    │
└─────────────────────────────────────────────────────┘

Pruning example:
  Query: WHERE date = '2024-06-15' AND city = 'NYC'
  Cloud services checks metadata:
    - 48,000 micro-partitions: date range doesn't include 2024-06-15 --> SKIP
    - 1,500 micro-partitions: date matches, but city != 'NYC' --> SKIP
    - 500 micro-partitions: both match --> READ
  Result: read 1% of data instead of 100%
```

### Key Features

**Zero-Copy Cloning:**
```sql
-- Creates a clone in seconds, regardless of table size
-- No data copied -- just metadata pointers to same micro-partitions
CREATE TABLE sales_backup CLONE sales;

-- Writes to clone create new micro-partitions (copy-on-write)
-- Original unaffected
DELETE FROM sales_backup WHERE date < '2023-01-01';
```

**Time Travel:**
```sql
-- Query data as it existed 30 minutes ago
SELECT * FROM sales AT(OFFSET => -60*30);

-- Query at specific timestamp
SELECT * FROM sales AT(TIMESTAMP => '2024-01-15 10:00:00'::timestamp);

-- Undrop a table
UNDROP TABLE accidentally_dropped_table;

-- Retention: 1 day (standard), up to 90 days (enterprise)
```

**Separation of Storage and Compute:**
```
Before Snowflake (shared-everything):
  ┌─────────────────────────┐
  │  Compute + Storage      │    Scale together (expensive)
  │  (tightly coupled)      │    Idle compute still costs $$$
  └─────────────────────────┘

After Snowflake (separated):
  ┌──────────┐  ┌──────────┐     Scale independently
  │ Compute  │  │ Storage  │     Suspend compute when idle
  │ (pay/sec)│  │ (pay/TB) │     Multiple compute on same data
  └──────────┘  └──────────┘     No data copying between teams
```

---

## 6. Google BigQuery Architecture

### Dremel Execution Engine

```
┌─────────────────────────────────────────────────────────────┐
│                    BigQuery Query Flow                       │
│                                                             │
│   SQL Query                                                 │
│       │                                                     │
│       ▼                                                     │
│  ┌─────────┐    Parses SQL, optimizes, creates exec plan    │
│  │  Root    │    Coordinates execution across tree           │
│  │  Server  │                                               │
│  └────┬────┘                                                │
│       │                                                     │
│  ┌────┴─────────────────────────────┐                       │
│  │        Mixer Level 0             │   Aggregates results  │
│  │  ┌─────────┐    ┌─────────┐     │   from level below    │
│  │  │ Mixer 0 │    │ Mixer 1 │     │                       │
│  │  └────┬────┘    └────┬────┘     │                       │
│  └───────┤──────────────┤──────────┘                       │
│          │              │                                   │
│  ┌───────┤──────────────┤──────────┐                       │
│  │       │  Mixer Level 1          │   Further aggregation  │
│  │  ┌────┴──┐ ┌──┴───┐ ┌──────┐   │                       │
│  │  │Mix 0.0│ │Mix 0.1│ │Mix 1.0│  │                       │
│  │  └───┬───┘ └───┬───┘ └───┬───┘  │                       │
│  └──────┤─────────┤─────────┤──────┘                       │
│         │         │         │                               │
│  ┌──────┴─────────┴─────────┴──────┐                       │
│  │          Leaf Servers           │   Read columnar data   │
│  │  ┌────┐┌────┐┌────┐┌────┐...   │   from Colossus,      │
│  │  │Leaf││Leaf││Leaf││Leaf│      │   apply filters,       │
│  │  │ 0  ││ 1  ││ 2  ││ 3 │      │   partial aggregation  │
│  │  └──┬─┘└──┬─┘└──┬─┘└──┬─┘      │                       │
│  └─────┤─────┤─────┤─────┤────────┘                       │
│        │     │     │     │                                  │
│  ┌─────┴─────┴─────┴─────┴────────┐                       │
│  │        Colossus (Storage)       │   Distributed FS,     │
│  │   ┌──────┐ ┌──────┐ ┌──────┐   │   stores Capacitor    │
│  │   │Column│ │Column│ │Column│   │   columnar files       │
│  │   │Files │ │Files │ │Files │   │                       │
│  │   └──────┘ └──────┘ └──────┘   │                       │
│  └─────────────────────────────────┘                       │
└─────────────────────────────────────────────────────────────┘

Key insight: tree-shaped execution allows aggregation at every level.
A GROUP BY on 1 trillion rows: each leaf partially aggregates its shard,
mixers combine partial results, root produces final answer.
Thousands of leaf servers can process TB of data in seconds.
```

### Slots: Units of Compute

```
A "slot" = 1 virtual CPU + some RAM + network

Query: SELECT region, SUM(sales) FROM big_table GROUP BY region;
       (scans 10 TB of data)

BigQuery allocates:
  2,000 slots for this query
  Each slot reads ~5 GB of data
  All slots execute in parallel
  Total wall-clock time: ~10 seconds

On-demand pricing:  $5 per TB scanned (no slot management)
Flat-rate pricing:  Reserve 500-10,000 slots/month (predictable cost)
```

### Nested and Repeated Fields

BigQuery natively supports nested/repeated fields (from Protocol Buffer heritage):

```sql
-- Schema with nested and repeated fields
CREATE TABLE orders (
    order_id    INT64,
    customer    STRUCT<
        name    STRING,
        email   STRING,
        address STRUCT<
            city    STRING,
            state   STRING
        >
    >,
    items       ARRAY<STRUCT<   -- repeated field
        product_id  INT64,
        name        STRING,
        quantity    INT64,
        price       FLOAT64
    >>
);

-- Query nested fields directly (no joins needed!)
SELECT
    order_id,
    customer.name,
    customer.address.city,
    item.name AS product_name,
    item.quantity * item.price AS line_total
FROM orders, UNNEST(items) AS item
WHERE customer.address.state = 'CA';

-- This eliminates the need for separate items table + JOIN
-- Columnar storage means customer.address.city is a single column read
```

### Capacitor Columnar Format

```
Capacitor (BigQuery's proprietary columnar format):

┌────────────────────────────────────────┐
│         Capacitor File                 │
├────────────────────────────────────────┤
│  Column: order_id (INT64)              │
│    Encoding: delta + bit-packing       │
│    Stats: min=1, max=50000, nulls=0    │
│    Compressed blocks...                │
├────────────────────────────────────────┤
│  Column: customer.name (STRING)        │
│    Encoding: dictionary                │
│    Stats: distinct=12000, nulls=5      │
│    Compressed blocks...                │
├────────────────────────────────────────┤
│  Column: items.product_id (INT64)      │
│    Encoding: RLE + bit-packing         │
│    Definition levels (for nesting)     │
│    Repetition levels (for arrays)      │
│    Compressed blocks...                │
├────────────────────────────────────────┤
│  Footer: column metadata, schema,      │
│          statistics, bloom filters     │
└────────────────────────────────────────┘

Key features vs Parquet:
  - Adaptive encoding: chooses best encoding per column block
  - Rebalancing: automatically re-clusters data over time
  - Encryption: column-level encryption support
```

---

## 7. Amazon Redshift

### MPP Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Redshift Cluster                            │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    Leader Node                          │    │
│  │                                                         │    │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────┐  │    │
│  │  │  SQL     │ │  Query   │ │  Query   │ │  Result   │  │    │
│  │  │  Parser  │ │ Optimizer│ │ Planner  │ │ Assembler │  │    │
│  │  └──────────┘ └──────────┘ └──────────┘ └───────────┘  │    │
│  │                                                         │    │
│  │  Receives queries, plans execution, distributes to      │    │
│  │  compute nodes, aggregates final results                │    │
│  └──────────────────────┬──────────────────────────────────┘    │
│                         │                                       │
│         ┌───────────────┼───────────────┐                       │
│         │               │               │                       │
│         ▼               ▼               ▼                       │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐                │
│  │Compute     │  │Compute     │  │Compute     │                │
│  │Node 1      │  │Node 2      │  │Node 3      │                │
│  │            │  │            │  │            │                │
│  │┌──┐┌──┐┌──┐│  │┌──┐┌──┐┌──┐│  │┌──┐┌──┐┌──┐│                │
│  ││S0││S1││S2││  ││S0││S1││S2││  ││S0││S1││S2││  S = Slice    │
│  │└──┘└──┘└──┘│  │└──┘└──┘└──┘│  │└──┘└──┘└──┘│                │
│  │            │  │            │  │            │                │
│  │  Local SSD │  │  Local SSD │  │  Local SSD │                │
│  │  (cache)   │  │  (cache)   │  │  (cache)   │                │
│  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘                │
│        │               │               │                       │
│  ┌─────┴───────────────┴───────────────┴──────┐                │
│  │          Redshift Managed Storage (RMS)     │  RA3 instances │
│  │              (backed by S3)                 │                │
│  └─────────────────────────────────────────────┘                │
└─────────────────────────────────────────────────────────────────┘

Each slice = independent execution unit with its own:
  - Portion of memory
  - Portion of disk
  - Portion of CPU
  Typical: 2-16 slices per node, 1-128 nodes per cluster
```

### Distribution Styles

```sql
-- EVEN: Round-robin distribution (default)
-- Use when: no clear join key, or table not joined often
CREATE TABLE events (
    event_id BIGINT,
    event_type VARCHAR(50)
) DISTSTYLE EVEN;

-- KEY: Hash distribute by a column
-- Use when: large tables frequently joined on this column
CREATE TABLE orders (
    order_id   BIGINT,
    customer_id BIGINT,
    amount     DECIMAL(10,2)
) DISTSTYLE KEY DISTKEY(customer_id);

CREATE TABLE customers (
    customer_id BIGINT,
    name       VARCHAR(100)
) DISTSTYLE KEY DISTKEY(customer_id);

-- JOIN on customer_id: data is co-located, no network shuffle!
-- orders.customer_id=123 and customers.customer_id=123 on same node

-- ALL: Full copy on every node
-- Use when: small dimension tables joined with large fact tables
CREATE TABLE dim_region (
    region_id   INT,
    region_name VARCHAR(50)
) DISTSTYLE ALL;
-- Every node has full copy --> joins are always local

-- AUTO: Redshift chooses (starts ALL for small, converts to EVEN/KEY)
CREATE TABLE flexible_table (...) DISTSTYLE AUTO;
```

**Distribution impact on joins:**

```
Query: SELECT * FROM orders o JOIN customers c ON o.customer_id = c.customer_id

Case 1: Both DISTKEY(customer_id) --> Co-located join (fastest)
  Node 1: orders(cust 1-1000) JOIN customers(cust 1-1000)  -- local
  Node 2: orders(cust 1001-2000) JOIN customers(cust 1001-2000)  -- local
  No network transfer needed!

Case 2: orders DISTKEY(customer_id), customers DISTSTYLE ALL
  Node 1: orders(cust 1-1000) JOIN customers(ALL)  -- local, full copy
  Node 2: orders(cust 1001-2000) JOIN customers(ALL)  -- local, full copy

Case 3: Different dist keys --> Redistribution required (slowest)
  Must shuffle (redistribute) one table across network before join
  Can be 10-100x slower than co-located join
```

### Sort Keys

```sql
-- Compound sort key: lexicographic order (most common)
CREATE TABLE events (
    event_date  DATE,
    user_id     BIGINT,
    event_type  VARCHAR(50),
    duration    INT
) COMPOUND SORTKEY(event_date, user_id);

-- Data on disk sorted by: event_date first, then user_id
-- Zone maps:
--   Block 1: event_date [2024-01-01, 2024-01-05], user_id [1, 500]
--   Block 2: event_date [2024-01-05, 2024-01-10], user_id [1, 600]

-- WHERE event_date = '2024-01-03'              --> Excellent pruning
-- WHERE event_date = '2024-01-03' AND user_id = 42  --> Excellent pruning
-- WHERE user_id = 42 (no date filter)          --> NO pruning (prefix required)

-- Interleaved sort key: equal weight to all columns
CREATE TABLE events (...)
INTERLEAVED SORTKEY(event_date, user_id, event_type);

-- WHERE event_date = '2024-01-03'  --> Good pruning
-- WHERE user_id = 42               --> Good pruning (unlike compound)
-- WHERE event_type = 'click'       --> Good pruning

-- Trade-off: interleaved is slower to load (VACUUM is expensive)
-- Use compound for time-series data; interleaved for ad-hoc filtering
```

### Redshift Spectrum

```
Query external data in S3 without loading into Redshift:

┌──────────────────────────────────────────────────────────────┐
│                    Redshift Cluster                          │
│                                                              │
│  Leader Node                                                 │
│      │                                                       │
│      ├── Compute Nodes (local data)                          │
│      │                                                       │
│      └── Spectrum Layer ──────────┐                          │
│                                   │                          │
└───────────────────────────────────┤──────────────────────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │ Spectrum Compute     │
                         │ (1000s of nodes,     │
                         │  on-demand, shared)  │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │    S3 Data Lake      │
                         │  (Parquet, ORC, CSV) │
                         │  (PBs of data)       │
                         └─────────────────────┘

-- Create external schema pointing to S3
CREATE EXTERNAL SCHEMA spectrum_schema
FROM DATA CATALOG DATABASE 'my_db'
IAM_ROLE 'arn:aws:iam::123456789:role/RedshiftSpectrumRole';

-- Query S3 data as if it were a local table
SELECT date_trunc('month', event_date), COUNT(*)
FROM spectrum_schema.raw_events     -- lives in S3 as Parquet
WHERE event_date >= '2024-01-01'
GROUP BY 1;

-- Join local Redshift tables with S3 data
SELECT c.name, COUNT(*) as event_count
FROM local_schema.customers c
JOIN spectrum_schema.raw_events e ON c.id = e.customer_id
GROUP BY c.name;
```

---

## 8. DuckDB

### The SQLite of Analytics

```
Traditional OLAP:                      DuckDB:
┌────────────┐                         ┌────────────────────────┐
│ Application│                         │ Application            │
│            │                         │                        │
│  ┌─────┐  │  Network   ┌────────┐   │  ┌──────────────────┐  │
│  │Query├──┼──────────►│ OLAP   │   │  │  DuckDB          │  │
│  │     │  │  TCP/IP    │ Server │   │  │  (in-process)    │  │
│  └─────┘  │            │ (heavy)│   │  │  No network hop  │  │
│            │            └────────┘   │  │  No server       │  │
└────────────┘                         │  └──────────────────┘  │
  Needs: server, infra,               └────────────────────────┘
  network, credentials                  pip install duckdb
                                         Single file, zero config
```

### Architecture

```
┌──────────────────────────────────────────────────────────┐
│                      DuckDB Process                      │
│                                                          │
│  ┌─────────────────────────────────────────────────┐     │
│  │              SQL Parser + Optimizer              │     │
│  │         (PostgreSQL-compatible parser)           │     │
│  └────────────────────┬────────────────────────────┘     │
│                       │                                  │
│  ┌────────────────────▼────────────────────────────┐     │
│  │           Vectorized Execution Engine            │     │
│  │                                                  │     │
│  │  ┌─────────┐ ┌─────────┐ ┌──────────────────┐   │     │
│  │  │ Pipeline│ │ Pipeline│ │  Pipeline        │   │     │
│  │  │    1    │ │    2    │ │     3            │   │     │
│  │  │         │ │         │ │                  │   │     │
│  │  │ Scan    │ │ Hash    │ │  Aggregate       │   │     │
│  │  │ Filter  │ │ Join    │ │  Sort            │   │     │
│  │  │ Project │ │ Probe   │ │  Limit           │   │     │
│  │  └─────────┘ └─────────┘ └──────────────────┘   │     │
│  │                                                  │     │
│  │  Morsel-driven parallelism:                      │     │
│  │  Each pipeline processes "morsels" of ~10K rows  │     │
│  │  Work-stealing scheduler across CPU cores        │     │
│  └──────────────────────────────────────────────────┘     │
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │              Storage Layer                        │    │
│  │                                                   │    │
│  │  ┌──────────┐  ┌──────────┐  ┌───────────────┐   │    │
│  │  │ Native   │  │ Parquet  │  │ CSV / JSON    │   │    │
│  │  │ Storage  │  │ Reader   │  │ Reader        │   │    │
│  │  │ (.duckdb)│  │ (direct) │  │ (direct)      │   │    │
│  │  └──────────┘  └──────────┘  └───────────────┘   │    │
│  │                                                   │    │
│  │  ┌──────────┐  ┌──────────┐  ┌───────────────┐   │    │
│  │  │ Apache   │  │ Pandas   │  │ S3 / HTTP     │   │    │
│  │  │ Arrow    │  │ DataFrames│  │ Remote Files  │   │    │
│  │  │ (zero-   │  │ (zero-   │  │               │   │    │
│  │  │  copy)   │  │  copy)   │  │               │   │    │
│  │  └──────────┘  └──────────┘  └───────────────┘   │    │
│  └──────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────┘
```

### Morsel-Driven Parallelism

```
Traditional parallelism (Volcano):     Morsel-driven (DuckDB):

Thread 1 ──► Partition 1               Thread 1 ──► Morsel A ──► Morsel D ──► ...
Thread 2 ──► Partition 2               Thread 2 ──► Morsel B ──► Morsel E ──► ...
Thread 3 ──► Partition 3               Thread 3 ──► Morsel C ──► Morsel F ──► ...
Thread 4 ──► Partition 4
                                       Morsels = small chunks (~10K rows)
Problem: if partition 3 is 10x         Threads "steal" work from shared queue
larger, thread 3 becomes               --> Near-perfect load balancing
bottleneck. Other threads idle.        --> No thread sits idle
```

### Usage Examples

```python
import duckdb

# Query Parquet files directly (no loading step)
result = duckdb.sql("""
    SELECT region, SUM(amount) as total
    FROM 'sales/**/*.parquet'
    WHERE year = 2024
    GROUP BY region
    ORDER BY total DESC
""")

# Query remote Parquet over HTTP/S3
duckdb.sql("""
    SELECT * FROM read_parquet(
        's3://my-bucket/data/events/*.parquet',
        hive_partitioning=true
    )
    WHERE event_date >= '2024-01-01'
    LIMIT 1000
""")

# Zero-copy integration with Pandas
import pandas as pd
df = pd.DataFrame({'x': range(10_000_000), 'y': range(10_000_000)})

# DuckDB reads the Pandas DataFrame directly from memory (zero-copy!)
result = duckdb.sql("SELECT SUM(x), AVG(y) FROM df").fetchone()

# Integration with Apache Arrow
import pyarrow as pa
arrow_table = pa.table({'a': [1,2,3], 'b': ['x','y','z']})
duckdb.sql("SELECT * FROM arrow_table WHERE a > 1")

# Persistent database
con = duckdb.connect('analytics.duckdb')
con.sql("CREATE TABLE metrics AS SELECT * FROM 'data/*.csv'")
con.sql("SELECT * FROM metrics WHERE date > '2024-01-01'")
```

### Why DuckDB Is Revolutionary

| Aspect | Before DuckDB | With DuckDB |
|--------|---------------|-------------|
| Setup | Install server, configure, manage | `pip install duckdb` |
| Query Parquet | Load into warehouse first | Direct query, zero load time |
| Data science | Export from DB, load to Pandas | Query Pandas DataFrames with SQL |
| Local analysis | Spin up Spark cluster for big CSV | Single process, handles 100s of GB |
| CI/CD testing | Mock database or spin up container | Embedded, in-memory, instant |
| Edge analytics | Not feasible | Runs on laptop, Raspberry Pi |

---

## 9. Apache Data Formats

### Apache Parquet

```
Parquet File Structure:
┌────────────────────────────────────────────────────────┐
│  Magic Number: "PAR1"                                  │
├────────────────────────────────────────────────────────┤
│  Row Group 0 (typically 128 MB - 1 GB)                │
│  ┌──────────────────────────────────────────────────┐  │
│  │ Column Chunk: "date" (all date values for group) │  │
│  │  ┌────────────────────────────────────────────┐  │  │
│  │  │ Data Page 0 (typically 1 MB)               │  │  │
│  │  │  Header: encoding, compressed size,        │  │  │
│  │  │          num_values, statistics (min/max)   │  │  │
│  │  │  Repetition Levels (for nested data)       │  │  │
│  │  │  Definition Levels (for nullability)        │  │  │
│  │  │  Encoded + Compressed Values               │  │  │
│  │  └────────────────────────────────────────────┘  │  │
│  │  ┌────────────────────────────────────────────┐  │  │
│  │  │ Data Page 1 ...                            │  │  │
│  │  └────────────────────────────────────────────┘  │  │
│  ├──────────────────────────────────────────────────┤  │
│  │ Column Chunk: "city"                             │  │
│  │  Pages...                                        │  │
│  ├──────────────────────────────────────────────────┤  │
│  │ Column Chunk: "amount"                           │  │
│  │  Pages...                                        │  │
│  └──────────────────────────────────────────────────┘  │
├────────────────────────────────────────────────────────┤
│  Row Group 1                                          │
│  (same structure as above)                            │
├────────────────────────────────────────────────────────┤
│  Footer                                               │
│  ┌──────────────────────────────────────────────────┐  │
│  │  File Metadata:                                  │  │
│  │    - Schema (column names, types, nesting)       │  │
│  │    - Row group metadata                          │  │
│  │      - Column chunk offsets                      │  │
│  │      - Column chunk statistics (min, max, nulls) │  │
│  │    - Key-value metadata                          │  │
│  └──────────────────────────────────────────────────┘  │
├────────────────────────────────────────────────────────┤
│  Footer Length (4 bytes)                              │
│  Magic Number: "PAR1"                                 │
└────────────────────────────────────────────────────────┘

Predicate pushdown with statistics:
  Query: WHERE amount > 1000
  Footer says Row Group 0's amount column: min=5, max=500
  --> Skip entire row group (millions of rows) without reading data!
```

### Apache Arrow

```
Arrow In-Memory Columnar Format:

┌─────────────────────────────────────────────────────────┐
│                  Arrow Record Batch                     │
│                                                         │
│  Schema: {id: int64, name: utf8, active: bool}          │
│                                                         │
│  Column "id" (Int64Array):                              │
│  ┌─────────────────────────────────────────┐            │
│  │ Validity bitmap: [1,1,1,0,1] (bit 0=null)│           │
│  │ Values buffer:   [42, 7, 13, ?, 99]     │           │
│  │                   ^^^^^^^^^^^^^^^^       │           │
│  │                   Contiguous int64 array  │           │
│  └─────────────────────────────────────────┘            │
│                                                         │
│  Column "name" (StringArray):                           │
│  ┌─────────────────────────────────────────┐            │
│  │ Validity bitmap: [1,1,1,1,1]            │            │
│  │ Offsets buffer:  [0,5,8,15,19,25]       │            │
│  │ Data buffer:     AliceBobCharlieDaveEmily│            │
│  │                  ^^^^^                   │            │
│  │                  offsets[0]:offsets[1]    │            │
│  └─────────────────────────────────────────┘            │
│                                                         │
│  Column "active" (BoolArray):                           │
│  ┌──────────────────────┐                               │
│  │ Validity: [1,1,1,1,1]│                               │
│  │ Values:   [1,0,1,1,0]│  (1 bit per value!)          │
│  └──────────────────────┘                               │
└─────────────────────────────────────────────────────────┘

Key properties:
  - O(1) random access (fixed-width) or O(1) with offset (variable-width)
  - Zero-copy: any language can read the same memory layout
  - No serialization: send buffers directly over IPC or network
  - Alignment: 64-byte aligned for SIMD operations
```

**Arrow Flight Protocol:**

```
┌──────────┐   Arrow Flight (gRPC + Arrow IPC)   ┌──────────┐
│  Client  │ ◄──────────────────────────────────► │  Server  │
│ (Python) │                                      │ (Java)   │
└──────────┘                                      └──────────┘

Flight operations:
  GetFlightInfo()  --> metadata about available datasets
  GetSchema()      --> column names and types
  DoGet()          --> stream Arrow record batches (data transfer)
  DoPut()          --> upload Arrow record batches
  DoAction()       --> custom server-side actions

Performance vs traditional data transfer:
  REST/JSON:  100 MB/s  (serialize, text encode, parse)
  JDBC/ODBC:  300 MB/s  (row-by-row serialization)
  Arrow Flight: 3+ GB/s  (zero-copy columnar streaming)
```

### ORC (Optimized Row Columnar)

```
ORC File Structure:
┌──────────────────────────────────────────┐
│  Stripe 0 (typically 64-250 MB)          │
│  ┌────────────────────────────────────┐  │
│  │ Index Data                        │  │
│  │  - Min/max per column per 10K rows│  │
│  │  - Row positions for seeking      │  │
│  │  - Bloom filters (optional)       │  │
│  ├────────────────────────────────────┤  │
│  │ Row Data                          │  │
│  │  - Column streams (compressed)    │  │
│  │  - Each column encoded separately │  │
│  ├────────────────────────────────────┤  │
│  │ Stripe Footer                     │  │
│  │  - Stream locations and lengths   │  │
│  │  - Encoding for each column       │  │
│  └────────────────────────────────────┘  │
├──────────────────────────────────────────┤
│  Stripe 1 ...                            │
├──────────────────────────────────────────┤
│  File Footer                             │
│  - List of stripes and their metadata    │
│  - Type information (schema)             │
│  - Column-level statistics (entire file) │
│  - Row count                             │
├──────────────────────────────────────────┤
│  Postscript                              │
│  - Compression codec, footer length      │
└──────────────────────────────────────────┘

Parquet vs ORC Comparison:
┌─────────────────┬──────────────┬──────────────┐
│ Feature         │ Parquet      │ ORC          │
├─────────────────┼──────────────┼──────────────┤
│ Ecosystem       │ Universal    │ Hive/Presto  │
│ Nested data     │ Excellent    │ Good         │
│ Predicate push  │ Row group    │ Stripe + row │
│                 │ level        │ group level  │
│ Bloom filters   │ Column-level │ Row-group    │
│ ACID support    │ Via Iceberg/ │ Native (Hive │
│                 │ Delta Lake   │ transactions)│
│ Compression     │ Snappy,ZSTD, │ ZLIB, Snappy,│
│                 │ Gzip, LZ4   │ LZO, ZSTD   │
│ Adoption        │ Broader      │ Hadoop-heavy │
└─────────────────┴──────────────┴──────────────┘
```

---

## 10. MPP Architecture Patterns

### Shared-Nothing Architecture

```
Shared-Nothing (Redshift, ClickHouse, Greenplum):

┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   Node 1     │  │   Node 2     │  │   Node 3     │
│ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │
│ │   CPU    │ │  │ │   CPU    │ │  │ │   CPU    │ │
│ ├──────────┤ │  │ ├──────────┤ │  │ ├──────────┤ │
│ │  Memory  │ │  │ │  Memory  │ │  │ │  Memory  │ │
│ ├──────────┤ │  │ ├──────────┤ │  │ ├──────────┤ │
│ │  Disk    │ │  │ │  Disk    │ │  │ │  Disk    │ │
│ │ (local)  │ │  │ │ (local)  │ │  │ │ (local)  │ │
│ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │
│              │  │              │  │              │
│ Data: A-F    │  │ Data: G-M    │  │ Data: N-Z    │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       └─────────────────┼─────────────────┘
                         │
                    High-speed
                    interconnect

Each node owns its data partition.
Queries execute in parallel across all nodes.
Results are shuffled over network only when needed (joins, aggregations).
```

### Data Distribution Strategies

```
Table: orders (order_id, customer_id, product_id, amount, date)

1. HASH Distribution (most common):
   hash(customer_id) % num_nodes

   Node 0: customer_id % 3 = 0  --> customers 3, 6, 9, 12, ...
   Node 1: customer_id % 3 = 1  --> customers 1, 4, 7, 10, ...
   Node 2: customer_id % 3 = 2  --> customers 2, 5, 8, 11, ...

   Pros: co-located joins on customer_id
   Cons: data skew if some customers have 1000x more orders

2. RANGE Distribution:
   Node 0: date [2024-01, 2024-04]
   Node 1: date [2024-05, 2024-08]
   Node 2: date [2024-09, 2024-12]

   Pros: range queries hit single node, partition pruning
   Cons: hot spots (latest partition gets all new writes)

3. ROUND-ROBIN Distribution:
   Row 0 --> Node 0
   Row 1 --> Node 1
   Row 2 --> Node 2
   Row 3 --> Node 0 (wrap around)

   Pros: perfectly balanced
   Cons: every join requires full data shuffle

4. BROADCAST (replicate to all nodes):
   Every node has a full copy of the table
   Pros: joins are always local
   Cons: only for small dimension tables (< 100MB)
```

### Query Planning in Distributed Systems

```
Query: SELECT c.name, SUM(o.amount)
       FROM orders o JOIN customers c ON o.customer_id = c.customer_id
       WHERE o.date >= '2024-01-01'
       GROUP BY c.name

Distributed Execution Plan:

Step 1: Each node filters locally
  Node 0: SCAN orders WHERE date >= '2024-01-01' --> partial orders
  Node 1: SCAN orders WHERE date >= '2024-01-01' --> partial orders
  Node 2: SCAN orders WHERE date >= '2024-01-01' --> partial orders

Step 2: Redistribute for join (if not co-located)
  ┌────────┐         ┌────────┐         ┌────────┐
  │ Node 0 │ ──────► │ Node 1 │ ◄────── │ Node 2 │
  │        │ ◄────── │        │ ──────► │        │
  └────────┘         └────────┘         └────────┘
  Exchange operator: hash(customer_id) to determine target node

Step 3: Local join on each node
  Each node joins its partition of orders with its partition of customers

Step 4: Local partial aggregation
  Each node computes partial SUM(amount) GROUP BY name

Step 5: Final aggregation
  ┌────────┐
  │ Leader │  Receives partial aggregates from all nodes
  │  Node  │  Computes final SUM, returns result to client
  └────────┘
```

### Handling Data Skew

```
Problem: customer_id = 42 has 50% of all orders (power user)
  --> Node handling customer 42 does 50% of work, others idle

Solutions:

1. Skew-aware partitioning:
   Detect hot keys, split them across multiple nodes
   customer_id=42 --> distributed to nodes 0,1,2 (with secondary hash)

2. Runtime adaptive redistribution:
   If a partition is too large, dynamically split it mid-query

3. Partial aggregation before shuffle:
   Pre-aggregate locally to reduce data volume before redistribution

   Before: shuffle 50M rows for customer 42 to one node
   After:  each node pre-aggregates locally, shuffle 3 partial results

4. Broadcast small side of skewed join:
   If one side of join is small after filtering, broadcast it
   instead of hash-partitioning the large side
```

---

## 11. Data Lakehouse

### The Evolution

```
Generation 1: Data Warehouse          Generation 2: Data Lake
(2000s)                                (2010s)
┌──────────────┐                       ┌──────────────────┐
│  Structured  │                       │  Raw Data Lake   │
│  Data Only   │                       │  (S3, HDFS)      │
│  (SQL, OLAP) │                       │                  │
│              │                       │  JSON, CSV,      │
│  Expensive   │                       │  Parquet, logs,  │
│  Proprietary │                       │  images, video   │
│  Teradata,   │                       │                  │
│  Oracle, etc │                       │  Cheap storage   │
└──────────────┘                       │  No ACID         │
                                       │  "Data swamp"    │
Problems:                              └──────────────────┘
- Expensive                            Problems:
- Rigid schema                         - No transactions
- Can't handle unstructured            - No consistency
                                       - Stale metadata
                                       - Poor performance

Generation 3: Data Lakehouse (2020s)
┌─────────────────────────────────────────────────────────┐
│                                                         │
│   ACID Transactions + Schema Enforcement + SQL Engine   │
│          on top of cheap object storage (S3)            │
│                                                         │
│  ┌──────────────────┐   ┌─────────────────────────┐     │
│  │  Table Format    │   │  Query Engine            │     │
│  │  (Delta Lake /   │   │  (Spark, Trino, Flink,  │     │
│  │   Iceberg /      │   │   DuckDB, Presto)       │     │
│  │   Hudi)          │   │                         │     │
│  └────────┬─────────┘   └─────────────────────────┘     │
│           │                                             │
│  ┌────────▼─────────────────────────────────────────┐   │
│  │              Object Storage (S3 / GCS / ADLS)    │   │
│  │     Parquet files + metadata (JSON / Avro)       │   │
│  └──────────────────────────────────────────────────┘   │
│                                                         │
│  Benefits: warehouse performance + lake economics       │
│            ACID on object storage                       │
│            Schema evolution                             │
│            Time travel                                  │
│            Open formats (no vendor lock-in)             │
└─────────────────────────────────────────────────────────┘
```

### Delta Lake (Databricks)

```
Delta Lake Table Structure on S3:

s3://my-bucket/sales_table/
├── _delta_log/                          <-- Transaction log
│   ├── 00000000000000000000.json        <-- Version 0: initial files
│   ├── 00000000000000000001.json        <-- Version 1: inserts
│   ├── 00000000000000000002.json        <-- Version 2: deletes
│   ├── 00000000000000000003.json        <-- Version 3: updates
│   └── 00000000000000000010.checkpoint.parquet  <-- Checkpoint
│
├── part-00000-a1b2c3.parquet            <-- Data files
├── part-00001-d4e5f6.parquet            (standard Parquet)
├── part-00002-g7h8i9.parquet
└── part-00003-j0k1l2.parquet

Transaction log entry (JSON):
{
  "add": {
    "path": "part-00003-j0k1l2.parquet",
    "size": 104857600,
    "partitionValues": {"date": "2024-01-15"},
    "stats": "{\"numRecords\":1000000,
               \"minValues\":{\"amount\":0.50},
               \"maxValues\":{\"amount\":9999.99}}"
  }
}
{
  "remove": {
    "path": "part-00001-d4e5f6.parquet",
    "timestamp": 1705363200000
  }
}

ACID achieved through optimistic concurrency on the log:
  1. Read current version (e.g., version 3)
  2. Compute changes (new Parquet files)
  3. Atomically write version 4 to _delta_log/
  4. If conflict (someone else wrote version 4), retry
```

### Apache Iceberg

```
Iceberg Table Metadata Hierarchy:

                    ┌─────────────────────┐
                    │  Metadata File       │
                    │  (current snapshot,  │
                    │   schema, partition  │
                    │   spec, properties)  │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
       ┌────────────┐  ┌────────────┐   ┌────────────┐
       │ Snapshot 1  │  │ Snapshot 2  │   │ Snapshot 3  │
       │ (v1)       │  │ (v2)       │   │ (current)  │
       └──────┬─────┘  └──────┬─────┘   └──────┬─────┘
              │               │                │
              ▼               ▼                ▼
       ┌────────────┐  ┌────────────┐   ┌────────────┐
       │ Manifest   │  │ Manifest   │   │ Manifest   │
       │ List       │  │ List       │   │ List       │
       └──────┬─────┘  └──────┬─────┘   └──────┬─────┘
              │               │                │
         ┌────┴────┐     ┌────┴────┐      ┌────┴────┐
         ▼         ▼     ▼         ▼      ▼         ▼
    ┌─────────┐┌──────┐┌──────┐┌──────┐┌──────┐┌──────┐
    │Manifest ││Manif.││Manif.││Manif.││Manif.││Manif.│
    │File 1   ││File 2││File 3││File 4││File 5││File 6│
    └────┬────┘└──┬───┘└──┬───┘└──┬───┘└──┬───┘└──┬───┘
         │        │       │       │       │       │
         ▼        ▼       ▼       ▼       ▼       ▼
    ┌─────────────────────────────────────────────────┐
    │         Data Files (Parquet / ORC / Avro)       │
    │   Each manifest file tracks:                    │
    │   - File paths                                  │
    │   - Partition values                            │
    │   - Column-level min/max stats                  │
    │   - Row counts, file sizes                      │
    └─────────────────────────────────────────────────┘

Key advantages over Delta Lake:
  - Partition evolution (change partitioning without rewriting data)
  - Hidden partitioning (users don't need to know partition layout)
  - Manifest-level stats enable faster planning on huge tables
  - Multi-engine support (Spark, Trino, Flink, Hive, etc.)
```

### Table Formats Comparison

| Feature | Delta Lake | Apache Iceberg | Apache Hudi |
|---------|-----------|----------------|-------------|
| **Creator** | Databricks | Netflix | Uber |
| **ACID transactions** | Yes | Yes | Yes |
| **Time travel** | Yes | Yes | Yes |
| **Schema evolution** | Add/rename cols | Full (add/drop/rename/reorder) | Add cols |
| **Partition evolution** | Requires rewrite | In-place (no rewrite) | Limited |
| **Hidden partitioning** | No | Yes | No |
| **Upsert support** | Merge command | Merge + copy-on-write/merge-on-read | Native (designed for it) |
| **Streaming ingest** | Structured Streaming | Flink integration | Excellent (primary use case) |
| **Compaction** | OPTIMIZE command | Rewrite manifests | Built-in (cleaning) |
| **Multi-engine** | Spark-centric (expanding) | Engine-agnostic | Spark + Flink |
| **Catalog** | Unity Catalog | REST, Hive, AWS Glue, Nessie | Hive Metastore |
| **File format** | Parquet only | Parquet, ORC, Avro | Parquet (primary) |
| **Adoption (2025)** | Strong (Databricks) | Rapidly growing | Niche (CDC/streaming) |

---

## 12. OLAP Performance Optimization

### Pre-Aggregation

```
Raw Table: page_views (100 billion rows)
┌────────────┬──────────┬─────────┬──────────┬─────────┐
│ timestamp  │ user_id  │ page_id │ duration │ country │
├────────────┼──────────┼─────────┼──────────┼─────────┤
│ 2024-01-01 │ 12345    │ /home   │ 3.2s     │ US      │
│ 00:00:01   │          │         │          │         │
│ ...        │ ...      │ ...     │ ...      │ ...     │
└────────────┴──────────┴─────────┴──────────┴─────────┘

Pre-aggregated Materialized View: daily_page_stats (10 million rows)
┌────────────┬─────────┬──────────┬───────┬──────────┬───────────┐
│ date       │ page_id │ country  │ views │ sum_dur  │ uniq_users│
├────────────┼─────────┼──────────┼───────┼──────────┼───────────┤
│ 2024-01-01 │ /home   │ US       │ 50000 │ 160000.0 │ 35000     │
│ ...        │ ...     │ ...      │ ...   │ ...      │ ...       │
└────────────┴─────────┴──────────┴───────┴──────────┴───────────┘

Dashboard query:
  Before: scan 100B rows, aggregate --> 30 seconds
  After:  scan 10M rows (pre-aggregated) --> 0.1 seconds
  Speedup: 300x

OLAP Cube (multi-dimensional pre-aggregation):
  Dimensions: [date, page, country]
  Measures:   [COUNT, SUM(duration), COUNT(DISTINCT user)]

  Pre-compute ALL combinations:
    (date, page, country) --> finest grain
    (date, page, *)      --> rollup country
    (date, *, country)   --> rollup page
    (*, page, country)   --> rollup date
    (date, *, *)         --> just by date
    (*, *, *)            --> grand total

  Any dashboard slice/dice answers instantly from pre-computed result.
```

### Approximate Query Processing

```
Exact COUNT(DISTINCT):                 HyperLogLog (approximate):
  - Requires hash set of all values    - Fixed 12 KB memory (!)
  - Memory: O(n) --> GB for billions   - Error: ~0.8% typical
  - Time: full scan + dedup            - Time: single pass scan

-- ClickHouse example:
SELECT uniqHLL12(user_id) FROM events;       -- ~0.8% error, fast
SELECT uniq(user_id) FROM events;            -- adaptive, ~1-2% error
SELECT uniqExact(user_id) FROM events;       -- exact, slow, memory-hungry

-- Count-Min Sketch (frequency estimation):
-- "How many times did event X occur?" without storing all events
-- Memory: O(1/epsilon * log(1/delta))
-- Error: epsilon (additive), delta (probability of exceeding epsilon)

-- Quantile estimation (t-digest, DDSketch):
SELECT quantile(0.99)(response_time) FROM requests;  -- approximate p99
-- Memory: O(1) (fixed-size sketch)
-- vs exact: sort entire column --> O(n log n) time, O(n) memory

Approximate query processing trade-offs:
┌────────────────┬─────────────┬──────────────┬──────────────┐
│ Algorithm      │ Use Case    │ Memory       │ Error        │
├────────────────┼─────────────┼──────────────┼──────────────┤
│ HyperLogLog    │ COUNT DIST. │ 12 KB        │ ~0.8%        │
│ Count-Min      │ Frequency   │ ~10 KB       │ Configurable │
│ Bloom Filter   │ Membership  │ ~10 bits/elem│ FP only      │
│ t-digest       │ Quantiles   │ ~5 KB        │ ~1% at tails │
│ Theta Sketch   │ Set ops     │ ~16 KB       │ ~2%          │
└────────────────┴─────────────┴──────────────┴──────────────┘
```

### Partition Pruning

```sql
-- Table partitioned by date (monthly)
CREATE TABLE events (
    event_date  DATE,
    user_id     BIGINT,
    event_type  STRING,
    payload     STRING
) PARTITIONED BY (MONTH(event_date));

-- Physical layout on storage:
-- events/
--   ├── event_date_month=2024-01/  (500 Parquet files)
--   ├── event_date_month=2024-02/  (500 Parquet files)
--   ├── event_date_month=2024-03/  (500 Parquet files)
--   └── ... (12 months x 500 files = 6000 files)

-- Query with partition filter:
SELECT COUNT(*) FROM events
WHERE event_date BETWEEN '2024-03-01' AND '2024-03-31';

-- Partition pruning: only reads event_date_month=2024-03/
-- Skips 11 out of 12 partitions = reads 8% of data
-- Combined with column pruning: reads 1 column out of 4 = 2% of data
-- Combined with zone maps: further skip files where min(event_date) > March 31
```

### Zone Maps / Min-Max Indexes

```
Column "amount" stored in 1 MB blocks:

Block 0: min=10.00,  max=99.50     [10.00, 45.20, 78.00, 99.50, ...]
Block 1: min=100.00, max=500.00    [100.00, 250.00, 499.99, ...]
Block 2: min=5.00,   max=25.00     [5.00, 12.50, 25.00, 8.75, ...]
Block 3: min=1000.00, max=9999.00  [1000.00, 5555.55, 9999.00, ...]

Query: WHERE amount > 500
  Block 0: max=99.50 < 500   --> SKIP (guaranteed no matches)
  Block 1: max=500.00 = 500  --> SKIP (> 500 not >= 500)
  Block 2: max=25.00 < 500   --> SKIP
  Block 3: min=1000.00 > 500 --> READ (guaranteed all match? no, still scan)

Result: read 1 out of 4 blocks = 75% I/O reduction

Effectiveness depends on data ordering:
  Sorted by amount:  zone maps eliminate 99%+ of blocks
  Random order:      every block spans full range, no pruning possible
  --> This is why sort keys / clustering keys matter enormously
```

### Result Caching

```
Caching layers in modern OLAP systems:

┌──────────────────────────────────────────────────────────┐
│  Layer 1: Query Result Cache (Snowflake, BigQuery)       │
│                                                          │
│  Exact same SQL + same data version = instant response   │
│  SELECT region, SUM(amt) FROM sales GROUP BY region;     │
│  --> Cached result returned in <100ms (no compute used)  │
│  Invalidated when underlying data changes                │
└──────────────────────────────────────────────────────────┘
                         │ miss
                         ▼
┌──────────────────────────────────────────────────────────┐
│  Layer 2: Block/Page Cache (local SSD)                   │
│                                                          │
│  Hot micro-partitions cached on local NVMe SSDs          │
│  Snowflake: warehouse local SSD cache                    │
│  Redshift: RA3 managed storage cache                     │
│  Avoids S3/GCS round-trip (200ms --> 1ms per block)      │
└──────────────────────────────────────────────────────────┘
                         │ miss
                         ▼
┌──────────────────────────────────────────────────────────┐
│  Layer 3: Object Storage (S3/GCS/ADLS)                   │
│                                                          │
│  Cold data, first-byte latency ~50-200ms                 │
│  High throughput for sequential reads                    │
│  Practically unlimited storage                           │
└──────────────────────────────────────────────────────────┘
```

### Denormalization Strategies for OLAP

```sql
-- OLTP (normalized, 3NF):
-- 5 tables, 4 joins required for a single dashboard query

SELECT o.order_id, c.name, p.product_name, s.store_name, d.quarter
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
JOIN products p ON o.product_id = p.product_id
JOIN stores s ON o.store_id = s.store_id
JOIN dates d ON o.date_id = d.date_id
WHERE d.year = 2024;

-- OLAP (denormalized fact table):
-- Single table scan, no joins, leverages columnar storage

SELECT order_id, customer_name, product_name, store_name, quarter
FROM fact_sales_denormalized
WHERE year = 2024;

-- Trade-offs:
-- Storage: 2-5x more (redundant dimension attributes in fact table)
-- Query speed: 10-100x faster (no joins, better column pruning)
-- ETL complexity: Higher (must maintain consistency during loads)
-- Update cost: Must update fact table when dimension changes
--              (e.g., customer changes address)

-- Practical guideline:
-- Denormalize dimensions that are:
--   1. Slowly changing (Type 1: overwrite, Type 2: version)
--   2. Small enough that redundancy cost is trivial
--   3. Always joined in queries (customer name, product category)
-- Keep as separate dimension if:
--   1. Rapidly changing attributes
--   2. Very large dimension tables (millions of rows)
--   3. Need to query dimension independently
```

---

## Summary: When to Use What

| System | Best For | Scale | Latency | Cost Model |
|--------|----------|-------|---------|------------|
| **ClickHouse** | Real-time analytics, logs, time-series | TB-PB, self-managed | Sub-second | Open source / ClickHouse Cloud |
| **Snowflake** | Enterprise DW, multi-team analytics | TB-PB, fully managed | Seconds | Per-second compute + storage |
| **BigQuery** | Serverless analytics, Google ecosystem | PB+, fully managed | Seconds | Per-TB scanned or flat-rate slots |
| **Redshift** | AWS ecosystem, predictable workloads | TB-PB, managed | Seconds | Provisioned nodes or serverless |
| **DuckDB** | Local analytics, embedded, data science | GB-100s GB, in-process | Sub-second | Free, open source |
| **Databricks** | Lakehouse, ML + analytics unified | PB+, managed | Seconds | DBU (compute units) |
| **Apache Druid** | Real-time OLAP, sub-second at scale | TB+, self-managed | Sub-second | Open source |
| **Apache Pinot** | User-facing real-time analytics | TB+, self-managed | Milliseconds | Open source |

### Decision Framework

```
Start here:
  │
  ├── Need sub-second latency on user-facing dashboards?
  │     ├── Yes --> Druid, Pinot, or ClickHouse
  │     └── No (analyst-facing, seconds OK)
  │           │
  │           ├── Already on AWS?
  │           │     ├── Yes --> Redshift or Athena (serverless)
  │           │     └── No
  │           │           ├── Already on GCP? --> BigQuery
  │           │           └── Multi-cloud / vendor-neutral? --> Snowflake
  │           │
  │           ├── Need ML + analytics together?
  │           │     └── Databricks Lakehouse
  │           │
  │           └── Local / embedded analytics?
  │                 └── DuckDB
  │
  └── Budget-constrained, engineering-heavy team?
        └── ClickHouse (self-managed) or DuckDB
```
