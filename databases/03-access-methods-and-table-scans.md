# Access Methods and Table Scans: A Deep Dive

How databases physically retrieve data from storage. This document covers the access method abstraction layer, every scan type a query executor can use, parallel scan execution, and the cost model that determines which scan is chosen. This is the bridge between data encoding on disk (covered in 02) and the query engine that orchestrates execution (covered in 04).

---

## Table of Contents

1. [What Is an Access Method?](#1-what-is-an-access-method)
2. [Sequential (Full Table) Scans](#2-sequential-full-table-scans)
3. [Index Scans](#3-index-scans)
4. [Bitmap Scans](#4-bitmap-scans)
5. [TID Scans](#5-tid-scans)
6. [Table Sample Scans](#6-table-sample-scans)
7. [Parallel Scans](#7-parallel-scans)
8. [Scan Cost Estimation Fundamentals](#8-scan-cost-estimation-fundamentals)
9. [Access Method Comparison Matrix](#9-access-method-comparison-matrix)

---

## 1. What Is an Access Method?

### The Abstraction Layer

An access method is the interface between the query executor and the physical storage. The executor says "give me the next row matching this predicate" and the access method figures out the physical I/O, page fetching, tuple extraction, and visibility checks.

```
┌──────────────────────────────────────────────────────────────────┐
│                        QUERY EXECUTOR                             │
│                                                                    │
│  Seq Scan Node    Index Scan Node    Bitmap Scan Node    ...     │
│       │                │                    │                     │
│       ▼                ▼                    ▼                     │
├──────────────────────────────────────────────────────────────────┤
│              ACCESS METHOD INTERFACE (API)                         │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  scan_begin(relation, snapshot, nkeys, keys)                 │ │
│  │  scan_getnextslot(scan, direction, slot)  → true/false      │ │
│  │  scan_end(scan)                                              │ │
│  │                                                               │ │
│  │  index_fetch_begin(relation, index)                          │ │
│  │  index_fetch_tuple(scan, tid, slot)  → true/false           │ │
│  │  index_fetch_end(scan)                                       │ │
│  │                                                               │ │
│  │  scan_bitmap_next_block(scan, block)                         │ │
│  │  scan_bitmap_next_tuple(scan, slot)                          │ │
│  │                                                               │ │
│  │  tuple_insert(relation, slot, ...)                           │ │
│  │  tuple_delete(relation, tid, ...)                            │ │
│  │  tuple_update(relation, old_tid, slot, ...)                  │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                    │
├──────────────────────────────────────────────────────────────────┤
│                    STORAGE IMPLEMENTATION                          │
│                                                                    │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐                  │
│  │  Heap AM │  │ Columnar │  │ Index-Organized│                  │
│  │  (default)│  │ AM       │  │ Table AM       │                  │
│  │  heapam.c│  │ (cstore) │  │ (hypothetical) │                  │
│  └──────────┘  └──────────┘  └───────────────┘                  │
│                                                                    │
│              Buffer Pool ──► Disk I/O                              │
└──────────────────────────────────────────────────────────────────┘
```

### PostgreSQL's Table Access Method API (PG 12+)

PostgreSQL 12 introduced the **Table Access Method API** (`tableam.h`), abstracting the heap storage layer behind a set of function pointers. This allows pluggable storage engines.

Key functions in the `TableAmRoutine` struct:

| Function | Purpose |
|----------|---------|
| `scan_begin` | Initialize a sequential scan |
| `scan_getnextslot` | Fetch next tuple into a TupleTableSlot |
| `scan_end` | Clean up scan state |
| `index_fetch_begin` | Begin fetching tuples by TID (for index scans) |
| `index_fetch_tuple` | Fetch a specific tuple by TID |
| `tuple_insert` | Insert a new tuple |
| `tuple_delete` | Delete a tuple by TID |
| `tuple_update` | Update a tuple (delete old + insert new) |
| `relation_size` | Return the size of the table in bytes |
| `scan_analyze_next_block` | Support for ANALYZE sampling |

Third-party storage engines (e.g., `cstore_fdw` → Citus Columnar, Zheap, OrioleDB) implement this interface to plug in alternative storage without modifying the core executor.

### MySQL/InnoDB's Handler API

MySQL's pluggable storage engine architecture predates PostgreSQL's by decades. Each storage engine implements the `handler` C++ class:

```
MySQL Handler API (simplified):

  class handler {
    virtual int open(const char *name, int mode);
    virtual int close();

    // Full table scan
    virtual int rnd_init(bool scan);
    virtual int rnd_next(uchar *buf);     // next row
    virtual int rnd_end();

    // Index scan
    virtual int index_init(uint idx, bool sorted);
    virtual int index_read(uchar *buf, const uchar *key,
                           uint key_len, enum ha_rkey_function find_flag);
    virtual int index_next(uchar *buf);    // next in index order
    virtual int index_prev(uchar *buf);    // previous in index order
    virtual int index_end();

    // Write operations
    virtual int write_row(const uchar *buf);
    virtual int update_row(const uchar *old, const uchar *new_row);
    virtual int delete_row(const uchar *buf);

    // Metadata
    virtual ulong index_flags(uint idx) const;
    virtual ha_rows records_in_range(uint idx, key_range *min, key_range *max);
  };

  InnoDB implements this as ha_innobase.
  MyISAM implements this as ha_myisam.
  Memory engine implements this as ha_heap.
```

---

## 2. Sequential (Full Table) Scans

### How a Sequential Scan Works

A sequential scan (seq scan, full table scan) reads every page of a table in physical order and evaluates the WHERE clause against each tuple.

```
SEQUENTIAL SCAN FLOW:

  Table "users" (3 pages, 8 KB each):

  Page 0              Page 1              Page 2
  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
  │ Row 1 ✓     │    │ Row 5  ✗    │    │ Row 9  ✗    │
  │ Row 2 ✗     │    │ Row 6  ✓    │    │ Row 10 ✓    │
  │ Row 3 ✗     │    │ Row 7  ✗    │    │ Row 11 ✗    │
  │ Row 4 ✓     │    │ Row 8  ✓    │    │ Row 12 ✗    │
  └─────────────┘    └─────────────┘    └─────────────┘
        │                  │                  │
        ▼                  ▼                  ▼
  ┌──────────────────────────────────────────────────────┐
  │                    BUFFER POOL                         │
  │  1. Request page 0 from buffer pool                   │
  │  2. If not cached: read from disk (pread)            │
  │  3. For each tuple on page:                           │
  │     a. Check visibility (MVCC snapshot)              │
  │     b. Evaluate WHERE clause                         │
  │     c. If passes: project columns, return to executor│
  │  4. Unpin page 0                                     │
  │  5. Request page 1 ... repeat                        │
  └──────────────────────────────────────────────────────┘

  I/O pattern: SEQUENTIAL
  Pages read: ALL pages (relpages)
  Tuples evaluated: ALL visible tuples
  Result: only matching rows returned
```

### Buffer Pool Interaction: Ring Buffer Strategy

When a sequential scan reads a large table, it could evict all hot pages from the buffer pool (sequential flooding, discussed in 01). PostgreSQL mitigates this with a **ring buffer**.

```
PostgreSQL Ring Buffer for Large Scans:

  Normal buffer pool usage: scan pages go into shared buffer pool
  → large scan evicts hot OLTP pages → performance cliff

  Ring buffer: for tables > 1/4 of shared_buffers, PostgreSQL
  allocates a small private ring of 256 KB (32 pages) for the scan.

  ┌─────────────────────────────────────────────────┐
  │               SHARED BUFFER POOL                  │
  │  [hot page] [hot page] [hot page] ... [hot page] │
  │                                                    │
  │  ┌──────────────────────────┐                     │
  │  │  Ring Buffer (32 pages)  │  ← seq scan uses   │
  │  │  [pg0][pg1]...[pg31]    │    only these slots │
  │  │  ↑         ↑            │                     │
  │  │  overwritten as scan    │                     │
  │  │  progresses (circular)  │                     │
  │  └──────────────────────────┘                     │
  └─────────────────────────────────────────────────┘

  Pages from the scan cycle through 32 slots.
  Hot OLTP pages are never touched → no eviction.

  Also used for: VACUUM, bulk writes (COPY), large sorts.
```

### Synchronized Scans

When multiple backends perform sequential scans on the same large table simultaneously, PostgreSQL synchronizes them to share I/O.

```
WITHOUT synchronized scans:

  Backend A: reads pages 0, 1, 2, 3, 4, ...
  Backend B: reads pages 0, 1, 2, 3, 4, ...
  → Same pages read from disk twice. Double the I/O.

WITH synchronized scans (PostgreSQL 8.3+):

  Backend A starts scan at page 0: reads 0, 1, 2, 3, 4, ...
  Backend B starts scan while A is at page 50:
    B JOINS at page 50 (where A currently is)
    B reads: 50, 51, 52, ..., N, then wraps: 0, 1, ..., 49
    Both backends share the same physical reads!

  ┌────────────────────────────────────────────────┐
  │  Table pages:  [0] [1] [2] ... [50] [51] ...  │
  │                                  ↑              │
  │  Backend A scan position ────────┘              │
  │  Backend B joins here ───────────┘              │
  │                                                  │
  │  B reads [50..N, 0..49] (circular, same pages) │
  │  Physical disk reads shared between A and B     │
  └────────────────────────────────────────────────┘

  Implementation: a shared scan position is stored per relation
  in shared memory. New scans pick up the current position.

  Note: B's result order differs from A's (starts mid-table),
  but for unordered scans this doesn't matter.
```

### When the Planner Chooses a Sequential Scan

The planner prefers a sequential scan when:

| Condition | Why Seq Scan Wins |
|-----------|-------------------|
| No usable index exists | Only option |
| Query returns > ~5-15% of rows | Sequential I/O cheaper than random index-hop I/O |
| Table is very small (< ~50 pages) | Fits in 1-2 I/O operations regardless |
| No WHERE clause (full scan needed) | Index provides no benefit |
| Low `random_page_cost` ratio | Narrows seq scan advantage, but still can win for large result sets |
| Parallel seq scan available | Divides work across workers, very fast |

---

## 3. Index Scans

### How an Index Scan Works

An index scan uses a B+Tree (or other index) to locate specific rows, then fetches those rows from the heap (for heap-organized tables) or reads directly (for clustered/index-organized tables).

```
INDEX SCAN FLOW (PostgreSQL, heap table):

  Query: SELECT * FROM users WHERE email = 'alice@ex.com';
  Index: btree on users(email)

  Step 1: Traverse B+Tree index
  ┌──────────────────────────────────────────────────┐
  │  Root Node                                        │
  │  [... | "m" | ...]                                │
  │         │                                         │
  │  Internal Node                                    │
  │  [... | "alice" | "bob" | ...]                   │
  │         │                                         │
  │  Leaf Node                                        │
  │  ["alice@ex.com" → TID (5,3)]                    │
  │  ["alice2@ex.com" → TID (12,1)]                  │
  └──────────────────────────────────────────────────┘
                   │
                   │  TID = (page 5, slot 3)
                   ▼
  Step 2: Fetch tuple from heap
  ┌──────────────────────────────────────────────────┐
  │  Heap Page 5                                      │
  │  Slot 3 → {id=1, name="Alice", email="alice@.."} │
  │                                                    │
  │  Check visibility (MVCC): visible? → return row  │
  └──────────────────────────────────────────────────┘

  I/O: ~3-4 random reads (index levels) + 1 random read (heap page)
  For 10M rows: log(10M) ≈ 3-4 index levels + 1 heap fetch = ~4-5 I/O ops
```

### Index Scan on Clustered/Index-Organized Tables (InnoDB)

```
InnoDB INDEX SCAN (clustered index = primary key):

  Query: SELECT * FROM users WHERE id = 42;
  Clustered index: btree on users(id)

  Step 1: Traverse clustered B+Tree
  ┌──────────────────────────────────────────────────┐
  │  Root → Internal → Leaf                           │
  │                                                    │
  │  Leaf page contains THE ACTUAL ROW DATA:          │
  │  [id=42, name="Alice", email="alice@..", age=30]  │
  │                                                    │
  │  No separate heap fetch needed!                   │
  └──────────────────────────────────────────────────┘

  I/O: ~3-4 random reads (index levels only)

InnoDB SECONDARY INDEX SCAN:

  Query: SELECT * FROM users WHERE email = 'alice@ex.com';
  Secondary index: btree on users(email)

  Step 1: Traverse secondary index
  ┌──────────────────────────────────────────────┐
  │  Leaf: ["alice@ex.com" → PK value id=42]     │
  └──────────────────────────────────────────────┘
                   │
                   │  Primary Key = 42
                   ▼
  Step 2: Traverse clustered index (using PK)
  ┌──────────────────────────────────────────────┐
  │  Root → Internal → Leaf → full row           │
  └──────────────────────────────────────────────┘

  I/O: ~3-4 reads (secondary) + ~3-4 reads (clustered) = 6-8 reads
  This "double lookup" is the cost of InnoDB's secondary indexes.
```

### Index-Only Scans

If the index contains **all columns** needed by the query, the heap fetch can be skipped entirely. This is an **index-only scan** (also called a "covering index" scan).

```
INDEX-ONLY SCAN (PostgreSQL):

  Query: SELECT email FROM users WHERE email LIKE 'alice%';
  Index: btree on users(email)

  The index leaf contains "email" values.
  The query only needs "email".
  → No heap access needed!

  ┌──────────────────────────────────────────────────┐
  │  B+Tree Leaf Nodes (sequential scan of leaves):  │
  │                                                    │
  │  ["alice@ex.com"] → return directly               │
  │  ["alice2@ex.com"] → return directly              │
  │  ["bob@ex.com"] → stop (doesn't match prefix)    │
  │                                                    │
  │  Heap pages: NOT READ AT ALL                      │
  └──────────────────────────────────────────────────┘

  BUT WAIT -- PostgreSQL visibility check problem:

  PostgreSQL indexes don't store MVCC visibility info (xmin/xmax).
  How to know if a tuple is visible without reading the heap?

  Answer: VISIBILITY MAP (VM)

  ┌──────────────────────────────────────────────────┐
  │  For each heap page, the VM records:             │
  │  ALL_VISIBLE bit = 1 if ALL tuples on that page  │
  │  are visible to all transactions.                 │
  │                                                    │
  │  Index-only scan:                                 │
  │  1. Get TID from index leaf → (page 5, slot 3)  │
  │  2. Check VM: is page 5 all-visible?             │
  │     YES → tuple is definitely visible, skip heap │
  │     NO  → must fetch heap page to check visibility│
  └──────────────────────────────────────────────────┘

  After VACUUM runs: most pages become all-visible → index-only
  scan becomes very efficient. Before VACUUM: many heap fetches
  needed → degrades toward a regular index scan.

InnoDB covering index:

  InnoDB doesn't have this problem because secondary index entries
  carry enough info for visibility via the undo log. If all needed
  columns are in the secondary index, InnoDB skips the clustered
  index lookup entirely. No visibility map needed.

  CREATE INDEX idx_email ON users(email) INCLUDE (name);  -- PG 11+
  -- or --
  CREATE INDEX idx_email ON users(email, name);  -- composite index

  SELECT name, email FROM users WHERE email = 'alice@ex.com';
  → Index-only scan: both columns are in the index.
```

### Forward vs Backward Scans

```
B+Tree leaf nodes are doubly linked:

  ◄── [leaf 1] ←→ [leaf 2] ←→ [leaf 3] ←→ [leaf 4] ──►

  FORWARD SCAN (default):
    ORDER BY email ASC → traverse leaves left to right

  BACKWARD SCAN:
    ORDER BY email DESC → traverse leaves right to left
    Same index, no separate "descending index" needed.

  PostgreSQL: both directions supported natively for btree.
  The optimizer creates a "Backward Index Scan" node.

  Exception: multi-column indexes with mixed sort orders
    CREATE INDEX ON orders(customer_id ASC, order_date DESC);
    This matters when the query needs ORDER BY customer_id ASC, order_date DESC.
    Without the matching index, PG must sort the result.
```

---

## 4. Bitmap Scans

### The Problem Bitmap Scans Solve

Index scans fetch heap pages one-at-a-time in index order. If many rows match, this causes **random I/O** because index order != physical heap order. A bitmap scan solves this by reordering heap access to physical page order.

```
INDEX SCAN for 1,000 matching rows:

  Index leaf order:  TID(500,2), TID(3,7), TID(412,1), TID(3,2), TID(500,5)...
  Heap access order: page 500, page 3, page 412, page 3 (again!), page 500 (again!)...

  Problems:
  - Random I/O (jumping between pages)
  - Same page fetched multiple times
  - For 1,000 rows across 300 pages: 1,000 random reads

BITMAP SCAN (two phases):

  Phase 1: Build bitmap (which pages contain matching rows)
  Phase 2: Scan heap pages in physical order

  Result:
  - Sequential I/O (pages read in order: 3, 412, 500, ...)
  - Each page read only once
  - For 1,000 rows across 300 pages: 300 sequential reads
```

### Two-Phase Bitmap Scan (PostgreSQL)

```
Phase 1: BITMAP INDEX SCAN
──────────────────────────

  Traverse index, collect TIDs, build a bitmap.

  Query: SELECT * FROM orders WHERE status = 'pending';
  Index on orders(status) returns TIDs:
    (2,1), (2,5), (7,3), (7,8), (15,2), (15,4), (15,9), (23,1) ...

  Exact bitmap (TID-level granularity):
  ┌──────────────────────────────────────────────────────────┐
  │ Page 2:  [slots: 1, 5]                                   │
  │ Page 7:  [slots: 3, 8]                                   │
  │ Page 15: [slots: 2, 4, 9]                                │
  │ Page 23: [slots: 1]                                       │
  │ ...                                                       │
  └──────────────────────────────────────────────────────────┘

Phase 2: BITMAP HEAP SCAN
─────────────────────────

  Read pages in physical order. For each page, read only
  the slots indicated by the bitmap.

  ┌──────────────────────────────────────────────────────────┐
  │ 1. Read page 2  → extract rows at slots 1 and 5         │
  │ 2. Read page 7  → extract rows at slots 3 and 8         │
  │ 3. Read page 15 → extract rows at slots 2, 4, and 9    │
  │ 4. Read page 23 → extract row at slot 1                  │
  │ ...                                                       │
  └──────────────────────────────────────────────────────────┘

  Pages are read in ORDER (2 → 7 → 15 → 23) → sequential-ish I/O.
  Each page is read at most ONCE.
```

### Exact vs Lossy Bitmaps

When the number of matching rows is very large, the bitmap itself can become too large to fit in `work_mem`. PostgreSQL degrades gracefully by switching to **lossy** mode.

```
EXACT BITMAP (small result set):
  Tracks individual tuple slots per page.
  Page 7: [slots 3, 8]
  → Only those specific tuples are returned.

LOSSY BITMAP (large result set, bitmap exceeds work_mem):
  Drops slot-level detail. Only tracks which PAGES have matches.
  Page 7: [all tuples — need recheck!]
  → All tuples on page 7 are read, and the WHERE clause is
    re-evaluated for each one (bitmap heap scan "recheck cond").

  ┌────────────────────────────────────────────────────────────┐
  │ Bitmap too large for work_mem?                              │
  │                                                              │
  │ YES → Convert page entries from exact to lossy:            │
  │       Before: Page 7 [slots: 3, 8, 12, 45, 67, 99, ...]  │
  │       After:  Page 7 [ALL — recheck needed]                │
  │                                                              │
  │ EXPLAIN shows: "Recheck Cond: (status = 'pending')"       │
  │ If all pages are exact: "Heap Blocks: exact=300"           │
  │ If some are lossy:      "Heap Blocks: exact=200 lossy=100"│
  └────────────────────────────────────────────────────────────┘

  Increase work_mem to keep more bitmaps in exact mode.
```

### Combining Multiple Indexes (BitmapAnd / BitmapOr)

A major advantage of bitmap scans: multiple indexes can be combined with logical AND/OR operations, allowing multi-column filtering without a composite index.

```
Query: SELECT * FROM orders
       WHERE status = 'pending' AND region = 'US';

  No composite index on (status, region), but separate indexes exist:
  - idx_status on orders(status)
  - idx_region on orders(region)

  Plan:
  ┌──────────────────────────────────────────────────────┐
  │ Bitmap Heap Scan on orders                            │
  │   Recheck Cond: status = 'pending' AND region = 'US' │
  │   →  BitmapAnd                                        │
  │       →  Bitmap Index Scan on idx_status              │
  │           Index Cond: status = 'pending'              │
  │           Result: bitmap A (pages with pending)       │
  │       →  Bitmap Index Scan on idx_region              │
  │           Index Cond: region = 'US'                   │
  │           Result: bitmap B (pages with US)            │
  │                                                        │
  │   bitmap A AND bitmap B → only pages in BOTH bitmaps  │
  │   Heap scan reads only the intersection pages         │
  └──────────────────────────────────────────────────────┘

  BitmapOr works similarly for OR conditions:
  WHERE status = 'pending' OR status = 'failed'
  → bitmap A OR bitmap B → union of pages
```

---

## 5. TID Scans

### Direct Physical Access

A TID scan fetches a row directly by its physical address (page number + slot number). This is the fastest possible access path -- no index traversal at all.

```
TID SCAN:

  Query: SELECT * FROM users WHERE ctid = '(5,3)';

  1. Parse ctid → page 5, slot 3
  2. Read page 5 from buffer pool
  3. Follow slot 3 in line pointer array → tuple
  4. Check visibility → return

  I/O: exactly 1 page read. O(1).
```

### When TID Scans Are Used

| Use Case | Details |
|----------|---------|
| Explicit ctid query | `SELECT * FROM t WHERE ctid = '(0,1)'` — rare in application code |
| Self-join dedup | Used internally for deduplication: `DELETE FROM t WHERE ctid NOT IN (SELECT min(ctid) FROM t GROUP BY key)` |
| HOT chain following | Internally, PostgreSQL follows HOT chains via ctid pointers |
| Cursor updates | `WHERE CURRENT OF cursor_name` resolves to a TID |
| Debugging | Useful for inspecting specific physical tuples |

**Caution**: TIDs are physical addresses. They change after VACUUM FULL, CLUSTER, or pg_repack. Never store ctids in application code as stable row identifiers.

### InnoDB Equivalent

InnoDB has no TID scan because rows are identified by primary key, not physical address. The closest equivalent is a primary key lookup, which traverses the clustered B+Tree (~3-4 pages).

---

## 6. Table Sample Scans

### TABLESAMPLE (SQL:2003)

The TABLESAMPLE clause returns a random subset of a table without reading the entire table. Useful for approximate analytics, data profiling, and testing.

```
Syntax: SELECT * FROM users TABLESAMPLE method (percentage);

Two built-in methods:

BERNOULLI (row-level sampling):
──────────────────────────────
  SELECT * FROM users TABLESAMPLE BERNOULLI(1);  -- ~1% of rows

  Algorithm:
  1. Read EVERY page of the table (full sequential scan)
  2. For each tuple, generate random number [0, 1)
  3. If random < 0.01 (1%), include the row

  ┌──────────────────────────────────────────────────────────┐
  │ Page 0:  Row 1 (rand=0.45 → skip)                       │
  │          Row 2 (rand=0.008 → INCLUDE)                    │
  │          Row 3 (rand=0.72 → skip)                        │
  │ Page 1:  Row 4 (rand=0.003 → INCLUDE)                   │
  │          Row 5 (rand=0.91 → skip)                        │
  │ ...                                                       │
  └──────────────────────────────────────────────────────────┘

  Pros: Truly uniform random sample. Every row has equal probability.
  Cons: Must read ALL pages. I/O = full table scan.

SYSTEM (page-level sampling):
─────────────────────────────
  SELECT * FROM users TABLESAMPLE SYSTEM(1);  -- ~1% of pages

  Algorithm:
  1. For each page, generate random number [0, 1)
  2. If random < 0.01, read the page and return ALL its rows
  3. Otherwise, skip the page entirely (no I/O)

  ┌──────────────────────────────────────────────────────────┐
  │ Page 0: (rand=0.45 → SKIP entire page)                  │
  │ Page 1: (rand=0.003 → READ page, return ALL 50 rows)   │
  │ Page 2: (rand=0.89 → SKIP)                              │
  │ Page 3: (rand=0.007 → READ page, return ALL 48 rows)   │
  │ ...                                                       │
  └──────────────────────────────────────────────────────────┘

  Pros: Reads only ~1% of pages. Very fast for large tables.
  Cons: Not uniform — rows on the same page are correlated.
        Clustering effects mean some groups are over/under-sampled.
```

### PostgreSQL Extensions

```
tsm_system_rows:
  SELECT * FROM users TABLESAMPLE system_rows(1000);
  Returns exactly 1000 rows (not a percentage).
  Uses page-level sampling internally.

tsm_system_time:
  SELECT * FROM users TABLESAMPLE system_time(100);  -- 100 milliseconds
  Returns as many rows as possible within the time budget.
  Useful for "give me a quick sample, I don't care about size."
```

---

## 7. Parallel Scans

### PostgreSQL Parallel Query Architecture

PostgreSQL can parallelize scans by launching **worker processes** that execute in parallel with the leader process. Results are collected via a **Gather** or **Gather Merge** node.

```
PARALLEL SEQUENTIAL SCAN:

  Leader Process                       Worker Processes
  ┌────────────────────┐
  │  Gather Node       │◄─────────── results ───────────┐
  │  (collects rows    │◄──── results ────┐              │
  │   from workers)    │                  │              │
  └────────┬───────────┘                  │              │
           │                              │              │
  ┌────────▼───────────┐    ┌─────────────▼──┐   ┌──────▼─────────┐
  │  Parallel Seq Scan │    │ Parallel Seq   │   │ Parallel Seq   │
  │  (leader scans too)│    │ Scan (worker 1)│   │ Scan (worker 2)│
  └────────────────────┘    └────────────────┘   └────────────────┘
           │                       │                     │
           └───────────┬───────────┘─────────────────────┘
                       │
                       ▼
  ┌───────────────────────────────────────────────────────────┐
  │  SHARED BLOCK COUNTER                                      │
  │  Atomic counter. Each process grabs the next block number. │
  │                                                             │
  │  Leader:   "I'll take block 0"                             │
  │  Worker 1: "I'll take block 1"                             │
  │  Worker 2: "I'll take block 2"                             │
  │  Leader:   "I'll take block 3"                             │
  │  Worker 1: "I'll take block 4"                             │
  │  ...                                                        │
  │                                                             │
  │  No locks, no coordination beyond the atomic counter.      │
  │  Pages are distributed dynamically (work-stealing-like).   │
  └───────────────────────────────────────────────────────────┘

  With 2 workers + 1 leader = 3x parallelism (ideally).
  Actual speedup is limited by Amdahl's law, tuple transfer
  overhead, and shared buffer pool contention.
```

### Parallel Index Scan

```
PARALLEL INDEX SCAN (PostgreSQL 10+):

  Multiple workers traverse the SAME B+Tree index.
  The index is scanned once; leaf pages are divided among workers.

  ┌──────────────────────────────────────────────────────────┐
  │  B+Tree Leaf Level (linked list):                        │
  │                                                            │
  │  [Leaf 1] → [Leaf 2] → [Leaf 3] → [Leaf 4] → [Leaf 5]  │
  │     ↑          ↑          ↑          ↑          ↑        │
  │  Worker 1   Worker 2   Worker 1   Worker 2   Worker 1    │
  │                                                            │
  │  Each worker grabs the next leaf page from a shared       │
  │  scan position (similar to parallel seq scan's counter).  │
  │  Each worker then fetches corresponding heap pages.       │
  └──────────────────────────────────────────────────────────┘
```

### Parallel Bitmap Scan

```
PARALLEL BITMAP SCAN (PostgreSQL 10+):

  Phase 1: ONE worker builds the bitmap (single-threaded)
  Phase 2: MULTIPLE workers scan heap pages from the bitmap

  ┌──────────────────────────────────────────────────────────┐
  │ Worker 0: Bitmap Index Scan → builds bitmap              │
  │           (only one worker does this)                     │
  │                                                            │
  │ Bitmap complete!                                          │
  │                                                            │
  │ Workers 0, 1, 2: Bitmap Heap Scan                        │
  │   Shared iterator over bitmap pages.                      │
  │   Worker 0: reads page 2, page 15, page 45 ...           │
  │   Worker 1: reads page 7, page 23, page 50 ...           │
  │   Worker 2: reads page 10, page 30, page 52 ...          │
  └──────────────────────────────────────────────────────────┘
```

### Parallel Scan Configuration

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `max_parallel_workers_per_gather` | 2 | Max workers per Gather node |
| `max_parallel_workers` | 8 | Max total parallel workers system-wide |
| `max_worker_processes` | 8 | Max total background workers |
| `min_parallel_table_scan_size` | 8 MB | Table must be this big for parallel seq scan |
| `min_parallel_index_scan_size` | 512 KB | Index must be this big for parallel index scan |
| `parallel_tuple_cost` | 0.1 | Cost of transferring one tuple from worker to leader |
| `parallel_setup_cost` | 1000 | Cost of launching a parallel worker |
| `force_parallel_mode` | off | Force parallel execution (for testing) |

### Amdahl's Law Applied to Parallel Scans

```
Speedup = 1 / (S + P/N)

Where:
  S = serial fraction (Gather overhead, bitmap build, etc.)
  P = parallelizable fraction (heap scan, tuple processing)
  N = number of workers

Example: query is 90% scan, 10% overhead
  1 worker:   speedup = 1 / (0.1 + 0.9/1) = 1.0x
  2 workers:  speedup = 1 / (0.1 + 0.9/2) = 1.82x
  4 workers:  speedup = 1 / (0.1 + 0.9/4) = 3.08x
  8 workers:  speedup = 1 / (0.1 + 0.9/8) = 4.71x
  16 workers: speedup = 1 / (0.1 + 0.9/16) = 6.40x

  Diminishing returns: going from 8→16 workers adds only 1.7x.
  The serial fraction (10%) becomes the dominant bottleneck.

  In practice, disk I/O bandwidth is often the bottleneck,
  not CPU. Adding workers beyond the I/O saturation point
  provides zero benefit and adds overhead.
```

### MySQL/InnoDB Parallel Reads

MySQL 8.0 added parallel read threads for `CHECK TABLE` and some internal scans, but general parallel query execution is limited compared to PostgreSQL. InnoDB's `innodb_parallel_read_threads` controls the number of threads for partition scans and some bulk operations.

---

## 8. Scan Cost Estimation Fundamentals

### How the Planner Decides

PostgreSQL's cost-based optimizer assigns a **cost** to every possible access method for each table in the query, then picks the cheapest plan. Understanding the cost model explains why the planner makes the choices it does.

### Table Statistics (pg_class)

The planner relies on statistics collected by ANALYZE (or autovacuum):

```
Key statistics in pg_class:

  SELECT relname, reltuples, relpages, relallvisible
  FROM pg_class WHERE relname = 'users';

   relname │ reltuples │ relpages │ relallvisible
  ─────────┼───────────┼──────────┼──────────────
   users   │   1000000 │    8334  │         8200

  reltuples:    estimated number of live rows (1 million)
  relpages:     number of disk pages (8334 pages × 8 KB = ~65 MB)
  relallvisible: pages where all tuples are visible (for index-only scans)
```

### Column Statistics (pg_stats)

```
SELECT attname, null_frac, n_distinct, most_common_vals, histogram_bounds
FROM pg_stats WHERE tablename = 'users' AND attname = 'status';

  attname:          status
  null_frac:        0.0     (0% NULLs)
  n_distinct:       5       (5 distinct values)
  most_common_vals: {active, inactive, pending, suspended, deleted}
  most_common_freqs: {0.60, 0.20, 0.10, 0.07, 0.03}
  histogram_bounds: NULL    (MCVs cover all values)

For numeric/date columns:
  histogram_bounds: {1, 100, 200, ..., 9900, 10000}
  (100 equally-spaced boundary values for estimating range selectivity)
  correlation: 0.95  (physical order correlates with logical order)
```

### Selectivity Estimation

**Selectivity** is the fraction of rows that pass a filter. It ranges from 0.0 (no rows match) to 1.0 (all rows match).

```
EQUALITY SELECTIVITY (status = 'active'):

  If 'active' is in most_common_vals with freq 0.60:
    selectivity = 0.60
    estimated rows = 1,000,000 × 0.60 = 600,000

  If value not in MCV list:
    remaining_frac = 1.0 - sum(mcv_freqs)
    remaining_distinct = n_distinct - len(mcv_list)
    selectivity = remaining_frac / remaining_distinct

RANGE SELECTIVITY (age > 30):

  Uses histogram_bounds. If the histogram has 100 buckets and
  value 30 falls in bucket 35:
    selectivity ≈ (100 - 35) / 100 = 0.65
    estimated rows = 1,000,000 × 0.65 = 650,000

  More precisely, linear interpolation within the bucket is used.

AND SELECTIVITY (status = 'active' AND age > 30):

  Assumes independence (default):
    selectivity = sel(status) × sel(age) = 0.60 × 0.65 = 0.39

  This can be wildly wrong if columns are correlated!
  PostgreSQL 10+ has extended statistics (CREATE STATISTICS)
  for multi-column correlation and MCV lists.
```

### Cost Model: Sequential Scan

```
SEQUENTIAL SCAN COST:

  Total cost = (seq_page_cost × relpages) + (cpu_tuple_cost × reltuples)
             + (cpu_operator_cost × reltuples)   [for WHERE evaluation]

  With defaults:
    seq_page_cost    = 1.0
    cpu_tuple_cost   = 0.01
    cpu_operator_cost = 0.0025

  For users table (1M rows, 8334 pages):
    I/O cost  = 1.0 × 8334  = 8,334.0
    CPU cost  = 0.01 × 1,000,000 = 10,000.0
    Filter    = 0.0025 × 1,000,000 = 2,500.0
    ─────────────────────────────────────────
    Total     = 20,834.0

  Startup cost = 0 (can return first row immediately)
  Total cost = 20,834.0
```

### Cost Model: Index Scan

```
INDEX SCAN COST:

  Total cost = (index traversal) + (heap fetches × random_page_cost)
             + (CPU per tuple)

  For WHERE email = 'alice@ex.com' (selectivity = 0.000001, 1 row):
    Index pages read: ~3 (tree height)
    Index I/O cost = random_page_cost × 3 = 4.0 × 3 = 12.0
    Heap page fetch: 1 page
    Heap I/O cost  = random_page_cost × 1 = 4.0 × 1 = 4.0
    CPU cost       = 0.01 × 1 = 0.01
    ─────────────────────────────────────────
    Total          ≈ 16.01

  Compare to seq scan total of 20,834.0 → index scan wins by ~1300x!

  But for WHERE status = 'active' (selectivity = 0.60, 600K rows):
    Heap pages to fetch ≈ up to 8334 (many pages have active rows)
    Heap I/O cost = random_page_cost × 8334 = 4.0 × 8334 = 33,336.0
    Already more expensive than seq scan (20,834.0)!
    → Planner correctly chooses seq scan.
```

### The random_page_cost Knob

```
Default: random_page_cost = 4.0 (assumes random I/O is 4x slower than seq I/O)

  This ratio reflects HDD performance:
    Sequential read: ~150 MB/s → 8 KB page in ~0.05 ms
    Random read: ~100 IOPS → 8 KB page in ~10 ms
    Ratio: ~200x (but 4x is used because of buffer pool caching effects)

  For SSDs:
    Sequential: ~500-3000 MB/s
    Random: ~10,000-500,000 IOPS → 8 KB page in ~0.01-0.1 ms
    Ratio: ~2-5x

  Recommendation:
    HDD:      random_page_cost = 4.0 (default)
    SATA SSD: random_page_cost = 1.5 - 2.0
    NVMe SSD: random_page_cost = 1.1 - 1.5
    All in buffer pool: random_page_cost ≈ seq_page_cost ≈ 0.1

  Lowering random_page_cost makes the planner favor index scans
  more aggressively, which is correct for fast random I/O devices.
```

### Index Correlation

The **correlation** statistic (in `pg_stats`) measures how closely the physical ordering of rows matches the logical ordering of the index. It affects whether an index scan or bitmap scan is preferred.

```
High correlation (≈ 1.0 or -1.0):
  Physical order matches index order.
  Index scan fetches pages roughly sequentially.
  → Index scan is efficient.

Low correlation (≈ 0.0):
  Physical order is random relative to index.
  Index scan causes random I/O (every row on a different page).
  → Bitmap scan is preferred (reads pages in physical order).

Example:
  - auto-increment "id" column: correlation ≈ 1.0
    (rows inserted in order → physical order matches)
  - "email" column: correlation ≈ 0.05
    (alphabetical order ≠ insertion order → random I/O)

  For high-correlation columns: planner prefers Index Scan.
  For low-correlation columns: planner prefers Bitmap Scan
  (or seq scan if selectivity is too low).
```

### Worked Example: Comparing Scan Costs

```
Table: orders (10M rows, 83,000 pages, ~660 MB)
Query: SELECT * FROM orders WHERE amount > 500;
Index: btree on orders(amount)
Statistics: amount has uniform distribution [0, 1000], correlation = 0.05

Selectivity of amount > 500:
  histogram says 50% of values > 500
  selectivity = 0.50, estimated rows = 5,000,000

Option 1: Sequential Scan
  I/O:  seq_page_cost × 83,000 = 1.0 × 83,000 = 83,000
  CPU:  cpu_tuple_cost × 10,000,000 = 0.01 × 10M = 100,000
  Filter: 0.0025 × 10M = 25,000
  TOTAL: 208,000

Option 2: Index Scan
  Index pages: ~4 levels × random_page_cost = 4 × 4.0 = 16
  Heap fetches: 5,000,000 rows.
    With correlation 0.05 (essentially random):
    Estimated distinct pages = 83,000 × (1 - (1 - 0.5)^(10M/83000))
                             ≈ 83,000 (almost all pages touched)
    Heap I/O: 83,000 × random_page_cost = 83,000 × 4.0 = 332,000
  CPU: 0.01 × 5,000,000 = 50,000
  TOTAL: 382,016

Option 3: Bitmap Scan
  Bitmap Index Scan: ~83,000 index leaf pages (covering 50% of keys)
    I/O: random_page_cost × ~1000 (index pages) = 4,000
  Bitmap Heap Scan: read ~83,000 heap pages in PHYSICAL order
    I/O: seq_page_cost × 83,000 = 83,000  (sequential now!)
  CPU: same as index scan ≈ 50,000
  Recheck: 0.0025 × 5,000,000 = 12,500
  TOTAL: 149,500

WINNER: Bitmap Scan (149,500) < Seq Scan (208,000) < Index Scan (382,016)

The planner correctly chooses bitmap scan:
  - Index scan is worst because of random heap I/O for 5M rows
  - Bitmap scan converts random I/O to sequential → beats index scan
  - Seq scan reads all rows but avoids index overhead
  - Bitmap scan reads same pages as seq scan but skips non-matching
    pages? Actually at 50% selectivity it touches almost all pages,
    so it's close to seq scan cost. The win comes from not evaluating
    filter on the non-matching 50% of tuples.

If selectivity were 1% (100K rows), index scan would win.
If selectivity were 80%, seq scan would win.
```

---

## 9. Access Method Comparison Matrix

| Scan Type | When Used | I/O Pattern | Buffer Pool Strategy | Supports Parallel | Supports Index-Only | Best For |
|-----------|-----------|-------------|---------------------|-------------------|-----------------------|----------|
| **Sequential Scan** | No index or low selectivity | Sequential | Ring buffer (large tables) | Yes (shared block counter) | N/A | Full table reads, large result sets |
| **Index Scan** | High selectivity, high correlation | Random (heap hops) | Normal | Yes (shared leaf scan) | Yes (with VM check) | Point lookups, small range queries |
| **Index-Only Scan** | All columns in index | Sequential (index leaves) | Normal | Yes | Yes (by definition) | Covering index queries, COUNT(*) |
| **Bitmap Scan** | Medium selectivity, low correlation | Sequential (reordered) | Normal | Yes (parallel heap scan) | No | Multi-index AND/OR, medium result sets |
| **TID Scan** | Direct ctid access | Single page | Normal | No | N/A | Internal use, debugging |
| **BERNOULLI Sample** | Uniform row sampling | Sequential (all pages) | Ring buffer | No | N/A | Statistical sampling, data profiling |
| **SYSTEM Sample** | Fast approximate sampling | Random (sampled pages) | Normal | No | N/A | Quick estimates, large table previews |

### Selectivity Ranges and Preferred Scan

```
Selectivity (fraction of rows returned):

  0.0001%        0.01%         1%           10%          50%          100%
  │               │             │             │            │            │
  ├───────────────┼─────────────┼─────────────┼────────────┼────────────┤
  │  INDEX SCAN   │ INDEX SCAN  │ BITMAP SCAN │BITMAP/SEQ │  SEQ SCAN  │
  │  (point       │ (small      │ (reorder    │  (toss-up) │ (just read │
  │  lookup)      │  range)     │  random I/O)│            │  everything│
  │               │             │             │            │            │

  The exact crossover points depend on:
  - random_page_cost (SSD vs HDD)
  - Table size (pages) and width (rows per page)
  - Index correlation
  - Number of columns needed (index-only feasible?)
  - Available parallel workers

  This is why you should NEVER force index hints.
  The planner has more information than you do.
```

---
