# Database Indexing Internals: A Deep Dive

A staff-engineer-level guide to how database indexes actually work under the hood -- covering B+Trees, hash indexes, bitmap indexes, Bloom filters, skip lists, specialized index types (GIN, GiST, BRIN, SP-GiST), full-text search internals, index maintenance, and selection strategy. Includes ASCII diagrams, complexity analysis, and production guidance.

---

## Table of Contents

1. [Why Indexes Exist](#1-why-indexes-exist)
2. [B+Tree Index Deep Dive](#2-btree-index-deep-dive)
3. [Hash Indexes](#3-hash-indexes)
4. [Bitmap Indexes](#4-bitmap-indexes)
5. [Bloom Filters](#5-bloom-filters)
6. [Skip Lists](#6-skip-lists)
7. [Specialized Index Types](#7-specialized-index-types)
8. [Full-Text Search Indexing](#8-full-text-search-indexing)
9. [Index Maintenance and Operations](#9-index-maintenance-and-operations)
10. [Index Selection Strategy](#10-index-selection-strategy)

---

## 1. Why Indexes Exist

### The Fundamental Problem: Full Table Scans

Without an index, every query must examine every single row in the table. This is a **sequential scan** (also called a full table scan).

```
Query: SELECT * FROM users WHERE email = 'alice@example.com';

Without index -- FULL TABLE SCAN:

  ┌────────┬─────────────────────┬───────────┐
  │ Row 1  │ bob@example.com     │ ... skip  │  ← Compare
  │ Row 2  │ charlie@example.com │ ... skip  │  ← Compare
  │ Row 3  │ dave@example.com    │ ... skip  │  ← Compare
  │  ...   │       ...           │   ...     │  ← Compare EVERY row
  │ Row N  │ alice@example.com   │ ... MATCH │  ← Found! But had to read N rows
  └────────┴─────────────────────┴───────────┘

  Time complexity: O(N)
  For 10M rows: 10,000,000 comparisons worst case

With B+Tree index -- INDEXED LOOKUP:

  Root ──► Internal Node ──► Leaf Node ──► Row pointer
  3 levels deep = 3 page reads

  Time complexity: O(log N)
  For 10M rows: ~23 comparisons (log2 of 10M)
```

### The Read/Write Trade-off

Indexes are not free. Every index must be updated on every write operation.

```
                      WITHOUT INDEX          WITH 1 INDEX         WITH 5 INDEXES
                    ┌───────────────┐     ┌───────────────┐     ┌───────────────┐
  SELECT (point)    │   O(N) slow   │     │ O(log N) fast │     │ O(log N) fast │
  SELECT (range)    │   O(N) slow   │     │ O(log N + K)  │     │ O(log N + K)  │
  INSERT            │   O(1) fast   │     │ O(log N)      │     │ 5x O(log N)   │
  UPDATE (indexed)  │   O(N)        │     │ 2x O(log N)   │     │ 6x O(log N)   │
  DELETE            │   O(N)        │     │ O(log N)       │     │ 5x O(log N)   │
  Storage           │   data only   │     │ +10-30% more  │     │ +50-150% more │
                    └───────────────┘     └───────────────┘     └───────────────┘
```

Each INSERT must:
1. Write the row to the heap/clustered index.
2. Insert an entry into **every** secondary index on that table.
3. Each index insertion may trigger page splits (expensive).

Each UPDATE on an indexed column must:
1. Remove the old entry from the index.
2. Insert the new entry into the index.

### Storage Overhead

```
Example: 10 million user rows, 200 bytes average row size

  Table data:           10M x 200 bytes = ~2.0 GB
  B+Tree index (email): ~300 MB  (email avg 30 bytes + 6 byte pointer + overhead)
  B+Tree index (name):  ~250 MB
  B+Tree index (city):  ~200 MB
  Composite (a, b, c):  ~400 MB
                        ─────────
  Total index storage:  ~1.15 GB  (57% overhead!)
```

### When NOT to Use an Index

| Scenario | Why an Index Hurts |
|----------|--------------------|
| Tiny tables (< 1,000 rows) | Sequential scan fits in one or two pages; index adds overhead |
| Write-heavy tables with rare reads | Index maintenance cost > read benefit |
| Low-selectivity columns (e.g., boolean `is_active`) | B+Tree returns too many rows; seq scan often faster |
| Columns used with functions: `WHERE UPPER(email) = ...` | Index on `email` is unusable (need expression index) |
| Tables that are bulk-loaded and then dropped | Index rebuilds slow down bulk inserts massively |
| Columns with very high NULL ratio that are never queried on NULLs | Wasted space (use partial index instead) |
| Heap-only tuple (HOT) updates in PostgreSQL | Extra indexes prevent HOT optimization |

**Rule of thumb**: If a query returns more than ~5-15% of the table, the query planner will often prefer a sequential scan even when an index exists.

---

## 2. B+Tree Index Deep Dive

The B+Tree is the **dominant index structure** in virtually every relational database: PostgreSQL, MySQL (InnoDB), SQL Server, Oracle, SQLite. Understanding it deeply is non-negotiable.

### Structure Overview

```
B+Tree Properties:
  - All data pointers are in LEAF nodes (internal nodes only store keys + child pointers)
  - Leaf nodes are linked in a DOUBLY LINKED LIST (enables range scans)
  - All leaf nodes are at the SAME DEPTH (perfectly balanced)
  - Each node = one disk page (typically 4KB-16KB)
  - Internal nodes maximize fan-out (hundreds of children per node)
```

### ASCII Diagram: B+Tree with Order 4

```
Index on: user_id (values: 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60)

                              ┌──────────────────┐
                              │    [25 | 45]      │         ← ROOT NODE
                              │   /    |    \     │           (2 keys, 3 children)
                              └──/─────|─────\────┘
                               /       |       \
                ┌─────────────┐  ┌─────────────┐  ┌──────────────┐
                │  [10 | 20]  │  │  [30 | 40]  │  │  [50 | 55]   │   ← INTERNAL NODES
                │  / |  \     │  │  / |  \     │  │  / |  \      │
                └─/──|───\────┘  └─/──|───\────┘  └─/──|───\─────┘
                /    |    \      /    |    \      /    |    \
              ┌──┐ ┌──┐ ┌──┐ ┌──┐ ┌──┐ ┌──┐ ┌──┐ ┌──┐ ┌──┐
              │5 │→│10│→│15│→│25│→│30│→│35│→│45│→│50│→│55│→ NULL
              │  │ │  │ │20│ │  │ │  │ │  │ │  │ │  │ │60│
              │  │ │  │ │  │ │  │ │  │ │40│ │  │ │  │ │  │
              └──┘ └──┘ └──┘ └──┘ └──┘ └──┘ └──┘ └──┘ └──┘
              ↑                                             ↑
              │←──── LEAF NODES (linked list) ─────────────→│

  Each leaf node contains:
    - Key values
    - Pointers to actual rows (or the row data itself for clustered indexes)
    - Pointer to next leaf node (→)
    - Pointer to previous leaf node (←)  [not shown for clarity]
```

### Search Algorithm Walkthrough

**Query**: `SELECT * FROM users WHERE user_id = 35;`

```
Step 1: Start at ROOT [25 | 45]
        35 >= 25 and 35 < 45  →  follow MIDDLE pointer

Step 2: Arrive at INTERNAL NODE [30 | 40]
        35 >= 30 and 35 < 40  →  follow MIDDLE pointer

Step 3: Arrive at LEAF NODE [30 | 35 | 40]
        Linear scan within leaf: found 35!
        Follow row pointer → fetch actual row from data page

Total disk reads: 3 (one per level)
Total comparisons: ~6 (2 at root + 2 at internal + 2 at leaf)
```

**Range query**: `SELECT * FROM users WHERE user_id BETWEEN 20 AND 40;`

```
Step 1: B+Tree search for user_id = 20  →  arrive at leaf [15 | 20]
Step 2: Read user_id = 20 from this leaf
Step 3: Follow next-leaf pointer → [25]
Step 4: Follow next-leaf pointer → [30]
Step 5: Follow next-leaf pointer → [35 | 40]
Step 6: Read user_id = 40, stop (next would be > 40)

Result set: {20, 25, 30, 35, 40}
```

This is why the leaf-level linked list is critical -- it enables **sequential access** for range queries without going back up through the tree.

### Insertion with Page Splits

**Scenario**: Insert user_id = 22 into a full leaf node.

```
BEFORE INSERT (leaf node is full, max 3 keys per node):

  Internal: [20 | 30]
              |
    ┌─────────┴──────────┐
    ▼                     ▼
  [15|20]  →  [25|28|29]  →  [30|35]
                  ↑
          This leaf is FULL (3 keys)

Inserting user_id = 22 into [25|28|29]:
  22 < 25, so it belongs in position 0
  But the node is full! → SPLIT REQUIRED

AFTER PAGE SPLIT:

  Step 1: Create a new leaf node
  Step 2: Split keys [22, 25, 28, 29] into two halves:
          Left leaf:  [22 | 25]
          Right leaf: [28 | 29]
  Step 3: Push middle key (28) up to parent

  Internal: [20 | 28 | 30]
              |     |
    ┌─────────┘     └──────────┐
    ▼                           ▼
  [15|20]  →  [22|25]  →  [28|29]  →  [30|35]
                  ↑            ↑
              New split     New split
              (left)        (right)

If the parent was also full, the split CASCADES UPWARD.
In the worst case, the split reaches the root and creates a new root level,
increasing tree height by 1.
```

### Deletion with Page Merges

```
BEFORE DELETE (delete user_id = 22):

  Internal: [20 | 28 | 30]
    ▼         ▼         ▼
  [15|20] → [22|25] → [28|29] → [30|35]

AFTER DELETE:

  [22|25] becomes [25] -- only 1 key (below minimum occupancy of 2)

  Option A: REDISTRIBUTE from sibling
    Borrow 28 from right sibling [28|29]
    [25|28] → [29] → [30|35]
    Update parent key accordingly

  Option B: MERGE with sibling
    Merge [25] with [28|29] → [25|28|29]
    Remove separator key from parent

  Parent: [20 | 30]
    ▼         ▼
  [15|20] → [25|28|29] → [30|35]

Merges can also cascade upward if the parent becomes under-occupied.
```

### Node Size = Page Size (Why This Matters)

```
Operating system reads data from disk in PAGES (typically 4KB-16KB).

One disk I/O = one page read = one B+Tree node read

If node size matches page size:
  ┌──────────────────────────────────────────────────┐
  │  1 disk I/O  =  1 node read  =  1 tree level    │
  │  Tree height of 3  =  3 disk I/Os per lookup     │
  └──────────────────────────────────────────────────┘

Database     Page Size    Default
─────────    ─────────    ───────
PostgreSQL     8 KB       8 KB (configurable at compile time)
MySQL/InnoDB  16 KB       16 KB (innodb_page_size)
SQL Server     8 KB       8 KB
Oracle         8 KB       8 KB (db_block_size)
SQLite         4 KB       4 KB (PRAGMA page_size)
```

### Fan-out and Tree Height Calculation

**Fan-out** = number of child pointers per internal node. Higher fan-out = shorter tree = fewer disk I/Os.

```
Assume:
  - Page size: 8 KB (8192 bytes)
  - Key size: 8 bytes (bigint)
  - Pointer size: 6 bytes
  - Page header/metadata: ~100 bytes

Available space per page: 8192 - 100 = 8092 bytes
Each (key, pointer) pair: 8 + 6 = 14 bytes
Fan-out = floor(8092 / 14) = ~578 children per internal node

Tree height calculation for N rows:

  Height 1 (root only):       578 leaves                    ≈ 578 rows
  Height 2 (root + leaves):   578 x 578                     ≈ 334,000 rows
  Height 3:                   578 x 578 x 578               ≈ 193,000,000 rows
  Height 4:                   578^4                          ≈ 111 BILLION rows

┌───────────────────────────────────────────────────────────────────┐
│  CRITICAL INSIGHT:                                                │
│  A B+Tree with ~200 million rows only needs 3 levels.             │
│  The root is almost always cached in memory.                      │
│  So a point lookup requires at most 2 DISK I/Os.                  │
│  That is why B+Trees are so phenomenally efficient for databases.  │
└───────────────────────────────────────────────────────────────────┘
```

### Clustered Index vs Non-Clustered Index

```
CLUSTERED INDEX (InnoDB Primary Key, SQL Server clustered):

  Leaf nodes contain the ACTUAL ROW DATA, not pointers.
  The table IS the B+Tree. There is no separate heap file.

  Root ──► Internal ──► Leaf Node
                          ┌──────────────────────────────────┐
                          │ PK=10 | name="Alice" | age=30 .. │
                          │ PK=11 | name="Bob"   | age=25 .. │
                          │ PK=12 | name="Carol" | age=28 .. │
                          │            → next leaf            │
                          └──────────────────────────────────┘

  - Only ONE clustered index per table (data can only be physically sorted one way)
  - Range queries on PK are very fast (data is contiguous on disk)
  - InnoDB ALWAYS has a clustered index:
      1. PRIMARY KEY if defined
      2. First UNIQUE NOT NULL index
      3. Hidden 6-byte row ID (GEN_CLUST_INDEX)

NON-CLUSTERED INDEX (secondary index):

  Leaf nodes contain the KEY + a POINTER back to the row.

  Root ──► Internal ──► Leaf Node
                          ┌──────────────────────────┐
                          │ email="alice@.." → PK=10 │   ← In InnoDB, pointer = PK value
                          │ email="bob@.."   → PK=11 │   ← This means secondary index
                          │ email="carol@.." → PK=12 │      lookups do a DOUBLE traversal:
                          │        → next leaf        │      secondary B+Tree → primary B+Tree
                          └──────────────────────────┘

  InnoDB secondary index lookup:
  ┌──────────────────┐     ┌──────────────────┐     ┌────────────────┐
  │ Search secondary │ ──► │ Get PK value     │ ──► │ Search primary │ ──► Row data
  │ index B+Tree     │     │ from leaf node   │     │ key B+Tree     │
  └──────────────────┘     └──────────────────┘     └────────────────┘
         3 I/Os                                          2-3 I/Os
                                                    (called "bookmark lookup"
                                                     or "key lookup")

  PostgreSQL uses a different approach:
  - Table data is stored in a HEAP (unordered)
  - All indexes are non-clustered
  - Leaf nodes contain TID (tuple ID) = (page_number, offset_within_page)
  - Only one B+Tree traversal needed, then direct heap fetch
```

### Covering Indexes (Index-Only Scans)

A **covering index** contains all columns needed by the query. The database never needs to access the main table.

```sql
-- Query
SELECT email, name FROM users WHERE email = 'alice@example.com';

-- Non-covering index (requires table lookup):
CREATE INDEX idx_email ON users(email);
-- Path: Index scan on idx_email → find TID → fetch row from heap → extract name

-- Covering index (no table lookup needed!):
CREATE INDEX idx_email_name ON users(email, name);
-- Path: Index scan on idx_email_name → email AND name are both in the index → done

-- PostgreSQL INCLUDE syntax (key column + payload columns):
CREATE INDEX idx_email_incl_name ON users(email) INCLUDE (name);
-- email is in the B+Tree search structure
-- name is stored in leaf nodes but NOT used for tree navigation
-- Advantage: name doesn't inflate internal nodes, keeping fan-out high
```

```
Covering Index Scan (PostgreSQL: "Index Only Scan"):

  ┌──────────────────────────────────────────┐
  │          B+Tree Internal Nodes            │
  │       (only email keys for routing)       │
  └────────────────┬─────────────────────────┘
                   │
                   ▼
  ┌──────────────────────────────────────────┐
  │          B+Tree Leaf Node                 │
  │  email="alice@.."  name="Alice"  →next   │   ← All needed data is HERE
  │  email="bob@.."    name="Bob"    →next   │      No heap access required
  └──────────────────────────────────────────┘

  IMPORTANT (PostgreSQL): Index-only scans still require checking the
  VISIBILITY MAP to determine if the tuple is visible to the current
  transaction. If the page is not all-visible, PostgreSQL must fetch
  the heap tuple to check MVCC visibility. Run VACUUM to mark pages
  all-visible.
```

### Include Columns: PostgreSQL INCLUDE vs SQL Server INCLUDE

```sql
-- PostgreSQL (v11+)
CREATE INDEX idx_orders_customer
  ON orders(customer_id)
  INCLUDE (order_date, total_amount);

-- SQL Server
CREATE NONCLUSTERED INDEX idx_orders_customer
  ON orders(customer_id)
  INCLUDE (order_date, total_amount);
```

```
Why INCLUDE instead of just adding columns to the key?

Key columns:        Stored in INTERNAL + LEAF nodes, used for tree navigation
INCLUDE columns:    Stored in LEAF nodes ONLY, ignored during navigation

┌──────────────────────────────────────────────────────────┐
│  Composite key index: CREATE INDEX ON t(a, b, c, d)      │
│                                                           │
│  Internal node: [a|b|c|d | ptr | a|b|c|d | ptr | ...]   │
│                  ^^^^^^^^^^^^                             │
│                  BIG keys → fewer keys per page           │
│                            → higher tree → more I/O      │
├──────────────────────────────────────────────────────────┤
│  INCLUDE index: CREATE INDEX ON t(a) INCLUDE (b, c, d)   │
│                                                           │
│  Internal node: [a | ptr | a | ptr | a | ptr | ...]      │
│                  ^^^                                      │
│                  SMALL keys → more keys per page          │
│                             → shorter tree → less I/O    │
│                                                           │
│  Leaf node: [a | b | c | d | TID]                        │
│             Still has all data for covering scans         │
└──────────────────────────────────────────────────────────┘
```

### Composite Indexes: Column Order Matters

```sql
CREATE INDEX idx_composite ON orders(customer_id, order_date, status);
```

```
The B+Tree sorts entries LEXICOGRAPHICALLY by (customer_id, order_date, status):

  Leaf node entries (sorted):
    (100, 2024-01-01, 'completed')
    (100, 2024-01-15, 'pending')
    (100, 2024-02-01, 'completed')
    (200, 2024-01-01, 'pending')
    (200, 2024-01-10, 'completed')
    (300, 2024-01-05, 'completed')
    ...

Think of it like a phone book sorted by (last_name, first_name, middle_name).
You can look up:
  - Everyone named "Smith"                           ✓ (uses last_name)
  - "Smith, John"                                    ✓ (uses last_name + first_name)
  - "Smith, John, Michael"                           ✓ (uses all three)
  - Everyone named "John" (any last name)            ✗ (can't skip last_name!)
```

#### The Leftmost Prefix Rule

```
Index: (customer_id, order_date, status)
                a           b         c

┌──────────────────────────────────────────────────────────────────────────┐
│ Query                                            │ Uses Index? │ Why?   │
├──────────────────────────────────────────────────┼─────────────┼────────┤
│ WHERE customer_id = 100                          │ ✓ YES       │ a      │
│ WHERE customer_id = 100 AND order_date = '...'   │ ✓ YES       │ a, b   │
│ WHERE customer_id = 100 AND order_date = '...'   │ ✓ YES       │ a,b,c  │
│       AND status = 'pending'                     │             │        │
│ WHERE customer_id = 100 AND status = 'pending'   │ ✓ PARTIAL   │ a only │
│       (skips order_date)                         │   (a used,  │        │
│                                                  │    c filter)│        │
│ WHERE order_date = '2024-01-01'                  │ ✗ NO        │ skip a │
│ WHERE status = 'pending'                         │ ✗ NO        │ skip a │
│ WHERE order_date = '...' AND status = '...'      │ ✗ NO        │ skip a │
└──────────────────────────────────────────────────┴─────────────┴────────┘

The index can be used for any LEFTMOST PREFIX of its columns.
Once a column is skipped, subsequent columns cannot be used for tree navigation
(though they may still be used as filters within matched rows).
```

#### Column Order Design Heuristic

```
Priority for column ordering in a composite index:

  1. EQUALITY conditions first    (WHERE customer_id = ?)
  2. RANGE conditions next        (WHERE order_date BETWEEN ? AND ?)
  3. ORDER BY / GROUP BY last     (ORDER BY status)

Why? After a range condition, the B+Tree cannot efficiently use subsequent
columns for navigation. The range "opens up" the scan.

Example:
  Query: WHERE customer_id = 100 AND order_date > '2024-01-01' ORDER BY status

  GOOD: INDEX(customer_id, order_date, status)
    customer_id = 100  → exact match, narrow to subtree
    order_date > ...   → range scan within subtree
    status             → already sorted within range? Only if order_date is equality

  For this query, status ordering benefit is limited after the range on order_date.
  But (customer_id, order_date) prefix is fully utilized.
```

### Partial Indexes / Filtered Indexes

Only index a **subset** of rows. Dramatically reduces index size and maintenance cost.

```sql
-- PostgreSQL: partial index
CREATE INDEX idx_orders_pending
  ON orders(customer_id, order_date)
  WHERE status = 'pending';

-- Only ~5% of orders are 'pending' at any time
-- Index is 20x smaller than indexing all rows
-- Faster writes (95% of inserts don't touch this index)

-- SQL Server: filtered index
CREATE NONCLUSTERED INDEX idx_orders_pending
  ON orders(customer_id, order_date)
  WHERE status = 'pending';

-- PostgreSQL: index only non-null values
CREATE INDEX idx_users_phone
  ON users(phone_number)
  WHERE phone_number IS NOT NULL;
-- If 80% of users have no phone, this index is 5x smaller
```

```
Full index:                    Partial index (WHERE status='pending'):

┌──────────────────────┐      ┌──────────────────────┐
│ 10M entries          │      │ 500K entries          │
│ Size: 300 MB         │      │ Size: 15 MB           │
│ Height: 3 levels     │      │ Height: 2 levels      │
│ Insert cost: always  │      │ Insert cost: only for │
│                      │      │   pending orders      │
└──────────────────────┘      └──────────────────────┘
```

### Unique Indexes and Constraint Enforcement

```sql
-- Unique index: the database checks for duplicates on every INSERT/UPDATE
CREATE UNIQUE INDEX idx_users_email ON users(email);

-- Equivalent to:
ALTER TABLE users ADD CONSTRAINT uq_email UNIQUE (email);
```

```
Enforcement mechanism:

  INSERT INTO users (email) VALUES ('alice@example.com');

  1. Search B+Tree for 'alice@example.com'
  2. If found:
       → Check if existing row is visible to current transaction (MVCC)
       → If visible: ERROR: duplicate key value violates unique constraint
       → If not visible (deleted by concurrent txn): allow insert
  3. If not found:
       → Insert into B+Tree
       → Lock the index entry to prevent concurrent duplicates

  Note: In PostgreSQL, unique checks acquire a short-lived lock on the
  index page. Under heavy concurrent inserts to the same key range,
  this can cause contention.
```

---

## 3. Hash Indexes

### Structure

```
Hash Index Structure:

  Key → hash_function(key) → bucket_number → bucket page → chain of entries

  ┌─────────────────────────┐
  │      Hash Function       │
  │  h(key) = key mod N      │
  └───────────┬──────────────┘
              │
              ▼
  ┌──── Bucket Array (N buckets) ────┐
  │                                   │
  │  Bucket 0: ┌─────┐   ┌─────┐    │
  │            │ k=10 │──►│ k=20│──► NULL
  │            │ v=.. │   │ v=..│    │
  │            └─────┘   └─────┘    │
  │                                   │
  │  Bucket 1: ┌─────┐              │
  │            │ k=11 │──► NULL      │
  │            │ v=.. │              │
  │            └─────┘              │
  │                                   │
  │  Bucket 2: ┌─────┐   ┌─────┐   ┌─────┐
  │            │ k=2  │──►│ k=12│──►│ k=22│──► NULL   ← Overflow chain
  │            │ v=.. │   │ v=..│   │ v=..│   │
  │            └─────┘   └─────┘   └─────┘   │
  │                                   │
  │  Bucket 3: ──► NULL  (empty)     │
  │   ...                             │
  │  Bucket N-1: ┌─────┐            │
  │              │ k=.. │──► NULL    │
  │              └─────┘            │
  └───────────────────────────────────┘
```

### Performance Characteristics

```
                    Hash Index          B+Tree
                  ┌──────────────┬──────────────┐
  Point lookup    │    O(1) ✓✓   │   O(log N)   │
  Range scan      │    ✗ CANNOT  │   O(log N+K) │
  ORDER BY        │    ✗ CANNOT  │   ✓ YES      │
  Min/Max         │    ✗ CANNOT  │   O(log N)   │
  Prefix match    │    ✗ CANNOT  │   ✓ YES      │
  Insert          │    O(1) avg  │   O(log N)   │
  Delete          │    O(1) avg  │   O(log N)   │
                  └──────────────┴──────────────┘

Hash indexes win ONLY for exact-match (equality) lookups.
They cannot support: <, >, <=, >=, BETWEEN, LIKE 'prefix%', ORDER BY
```

### Static vs Dynamic Hashing

```
STATIC HASHING:
  - Fixed number of buckets allocated upfront
  - hash(key) mod N gives bucket number
  - Problem: as data grows, overflow chains get long → O(N) worst case
  - Need to REHASH everything when resizing (expensive!)

  Load factor = num_entries / num_buckets
  When load factor > threshold → double bucket array and rehash all entries

DYNAMIC HASHING:
  - Bucket count grows incrementally without full rehash
  - Two main approaches: Extendible Hashing and Linear Hashing
```

### Extendible Hashing

```
Key insight: Use a DIRECTORY of pointers to buckets.
Only double the directory (cheap), not the buckets.

  Hash values (binary):
    h(k1) = 00101
    h(k2) = 01010
    h(k3) = 10110
    h(k4) = 00011
    h(k5) = 10101

  Global depth: 2 (use first 2 bits of hash)

  Directory (2^2 = 4 entries):        Buckets:
  ┌────┬───────────┐                ┌──────────────┐
  │ 00 │ ─────────────────────────► │ k1, k4       │  bucket A (local depth 2)
  ├────┤           │                └──────────────┘
  │ 01 │ ─────────────────────────► ┌──────────────┐
  ├────┤           │                │ k2           │  bucket B (local depth 2)
  │ 10 │ ─────────────────────────► └──────────────┘
  ├────┤           │                ┌──────────────┐
  │ 11 │ ───────────────┐          │ k3, k5       │  bucket C (local depth 2)
  └────┴───────────┘    │          └──────────────┘
                        └────────►  (same bucket C or new)

  When bucket A overflows:
    1. Split bucket A into A1, A2
    2. If needed, double directory (global depth 2 → 3)
    3. Rehash ONLY entries in the split bucket
    4. Directory doubling is just copying pointers (O(2^d), but no data movement)
```

### Linear Hashing

```
Key insight: Split buckets ONE AT A TIME in round-robin order.

  Round 0: buckets 0,1,2,3  (N = 4, split pointer at 0)

  Split pointer: p = 0

  When ANY bucket overflows:
    1. Split the bucket at position p (NOT the overflowing bucket!)
    2. Entries in bucket p are rehashed with h1(k) = k mod (2*N)
    3. Advance p to p+1
    4. When p reaches N: start new round (N doubles, p resets to 0)

  ┌────────────────────────────────────────────────────┐
  │  Round 0, p=0:   [B0] [B1] [B2] [B3]              │
  │                    ↑ split pointer                  │
  │                                                     │
  │  After split at p=0:                                │
  │  Round 0, p=1:   [B0] [B1] [B2] [B3] [B4]         │
  │                         ↑                           │
  │  B0 entries rehashed: some stay in B0, some go to B4│
  │  h0(k) = k mod 4,  h1(k) = k mod 8                │
  │  Lookup: try h0(k). If result < p, use h1(k).      │
  │                                                     │
  │  After split at p=1:                                │
  │  Round 0, p=2:   [B0] [B1] [B2] [B3] [B4] [B5]   │
  │                              ↑                      │
  │                                                     │
  │  After p reaches N=4: new round                     │
  │  Round 1, p=0:   [B0]-[B7]  (8 buckets, N=8)      │
  └────────────────────────────────────────────────────┘

  Advantage over extendible hashing: no directory to manage.
  Disadvantage: split may not target the overflowing bucket.
```

### Where Hash Indexes Are Used

| System | Usage |
|--------|-------|
| PostgreSQL | `CREATE INDEX ... USING hash` -- WAL-logged since v10. Useful only for `=` lookups. Not widely used since B+Tree handles equality efficiently too. |
| MySQL/InnoDB | Adaptive Hash Index (AHI) -- InnoDB automatically builds in-memory hash indexes on frequently accessed B+Tree pages. Not user-controllable. |
| Memcached | Core data structure: hash table for all key lookups |
| Redis | Hash tables for the main keyspace and for Hash data type |
| Oracle | Hash clusters (rarely used in practice) |

---

## 4. Bitmap Indexes

### Structure

```
Table: sales (10 rows)
Column: region (values: 'North', 'South', 'East', 'West')

                    Row:  1  2  3  4  5  6  7  8  9  10
                         ─── ── ── ── ── ── ── ── ── ──
  Bitmap for 'North':    1  0  0  1  0  0  1  0  0   1
  Bitmap for 'South':    0  1  0  0  0  1  0  1  0   0
  Bitmap for 'East':     0  0  1  0  1  0  0  0  0   0
  Bitmap for 'West':     0  0  0  0  0  0  0  0  1   0

  Each distinct value gets ONE bit vector.
  Each bit vector has exactly N bits (one per row in the table).
  Bit = 1 means that row has this value.

Storage: 4 values x 10 bits = 40 bits total = 5 bytes
vs. storing the string 'North'/'South'/etc. for each row
```

### Bitmap Operations (Bitwise AND/OR/NOT)

```sql
-- Query: WHERE region = 'North' AND category = 'Electronics'
```

```
  region = 'North':     1  0  0  1  0  0  1  0  0  1
  category = 'Elec':    0  0  1  1  0  0  1  1  0  0
                        ─────────────────────────────────
  AND result:           0  0  0  1  0  0  1  0  0  0
                                 ↑        ↑
                              Row 4    Row 7    → Only these rows match

  This is a SINGLE CPU INSTRUCTION per 64 bits (on 64-bit CPUs).
  For 10 million rows: 10M / 64 = ~156,250 AND operations.
  At modern CPU speeds: microseconds.
```

```sql
-- Query: WHERE region = 'North' OR region = 'East'
```

```
  region = 'North':     1  0  0  1  0  0  1  0  0  1
  region = 'East':      0  0  1  0  1  0  0  0  0  0
                        ─────────────────────────────────
  OR result:            1  0  1  1  1  0  1  0  0  1
                        ↑     ↑  ↑  ↑     ↑        ↑
                      Rows: 1, 3, 4, 5, 7, 10
```

```sql
-- Query: WHERE region != 'South'   (equivalently: NOT South)
```

```
  region = 'South':     0  1  0  0  0  1  0  1  0  0
  NOT result:           1  0  1  1  1  0  1  0  1  1
```

### Compressed Bitmaps: Roaring Bitmaps

```
Problem: For high-cardinality columns (e.g., user_id with 10M distinct values),
         uncompressed bitmaps are huge: 10M values x 10M bits = 12.5 TB!

Solution: Compressed bitmap representations

RUN-LENGTH ENCODING (RLE):
  Original:  0000000000000000001111111111111111000000001111
  RLE:       18 zeros, 16 ones, 8 zeros, 4 ones
  Encoded:   (0,18)(1,16)(0,8)(1,4)

ROARING BITMAPS (used by Lucene, Spark, ClickHouse, Druid):

  Divide 32-bit integer space into chunks of 2^16 = 65536.
  For each chunk, choose the best container type:

  ┌──────────────────────────────────────────────────────────────┐
  │  Container Type     │ When Used              │ Storage       │
  ├─────────────────────┼────────────────────────┼───────────────┤
  │  Array Container    │ < 4096 values in chunk │ Sorted array  │
  │  Bitmap Container   │ >= 4096 values         │ 8KB bitmap    │
  │  Run Container      │ Consecutive runs       │ Run-length    │
  └──────────────────────────────────────────────────────────────┘

  Example: Roaring bitmap for set {1, 2, 3, 100, 200, 65536, 65537, 65538}

  Chunk 0 (values 0-65535):
    Values: {1, 2, 3, 100, 200}  → 5 values < 4096 → Array Container
    Storage: [1, 2, 3, 100, 200]  (sorted u16 array, 10 bytes)

  Chunk 1 (values 65536-131071):
    Values: {0, 1, 2} (relative to chunk start) → Array Container
    Storage: [0, 1, 2]  (6 bytes)

  Bitwise operations (AND/OR) work across container types efficiently.
```

### Why Bitmap Indexes Are Bad for OLTP

```
OLTP problem with bitmaps:

  Transaction T1: INSERT row with region='North'
    → Must update the 'North' bitmap
    → In Oracle: locks the ENTIRE bitmap segment (not just one bit)
    → All concurrent inserts for region='North' are BLOCKED

  ┌─────────────────────────────────────────────────────────┐
  │  B+Tree: Lock granularity = individual index entry      │
  │  Bitmap:  Lock granularity = entire bitmap segment      │
  │                                                          │
  │  Under concurrent writes, bitmap indexes cause           │
  │  MASSIVE lock contention.                                │
  │                                                          │
  │  Rule: Bitmap indexes are for READ-HEAVY OLAP/DW only.  │
  └─────────────────────────────────────────────────────────┘
```

### Where Bitmap Indexes Are Used

| System | Details |
|--------|---------|
| Oracle | `CREATE BITMAP INDEX` -- primary OLAP index type |
| PostgreSQL | No native bitmap index, but uses **bitmap heap scans** at query time (builds temporary bitmaps from any index type) |
| ClickHouse | Uses bitmap-based filtering internally |
| Apache Druid | Roaring bitmaps for inverted indexes on dimensions |
| Apache Spark | Roaring bitmaps for broadcast joins and filtering |
| Pilosa/FeatureBase | Entire database built on Roaring bitmaps |

---

## 5. Bloom Filters

### Concept: Probabilistic Membership Testing

```
A Bloom filter answers: "Is element X in the set?"

  Response: "DEFINITELY NOT"  →  100% accurate (no false negatives)
  Response: "PROBABLY YES"    →  may be wrong (false positives possible)

┌──────────────────────────────────────────────────────────────┐
│  Key insight for databases:                                   │
│  Before reading an SSTable/file from disk:                    │
│    → Check Bloom filter (in memory): "Is key K in this file?" │
│    → "Definitely not" → skip the disk read entirely (HUGE win)│
│    → "Probably yes" → read the file (occasionally wasted I/O) │
└──────────────────────────────────────────────────────────────┘
```

### Structure: Bit Array + Hash Functions

```
Bloom filter: m = 16 bits, k = 3 hash functions

  Bit array (initially all zeros):
  Position:  0  1  2  3  4  5  6  7  8  9  10 11 12 13 14 15
             0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0

INSERT "apple":
  h1("apple") mod 16 = 2    → set bit 2
  h2("apple") mod 16 = 7    → set bit 7
  h3("apple") mod 16 = 11   → set bit 11

  Position:  0  1  2  3  4  5  6  7  8  9  10 11 12 13 14 15
             0  0  1  0  0  0  0  1  0  0  0  1  0  0  0  0
                  ↑              ↑           ↑

INSERT "banana":
  h1("banana") mod 16 = 3   → set bit 3
  h2("banana") mod 16 = 7   → already set (collision!)
  h3("banana") mod 16 = 14  → set bit 14

  Position:  0  1  2  3  4  5  6  7  8  9  10 11 12 13 14 15
             0  0  1  1  0  0  0  1  0  0  0  1  0  0  1  0
                  ↑  ↑           ↑           ↑        ↑

LOOKUP "cherry":
  h1("cherry") mod 16 = 2   → bit 2 = 1 ✓
  h2("cherry") mod 16 = 7   → bit 7 = 1 ✓
  h3("cherry") mod 16 = 14  → bit 14 = 1 ✓
  All bits set → "PROBABLY YES" (but "cherry" was never inserted!)
  This is a FALSE POSITIVE.

LOOKUP "date":
  h1("date") mod 16 = 5     → bit 5 = 0 ✗
  → At least one bit is 0 → "DEFINITELY NOT IN SET"
  No need to check h2, h3.
```

### Optimal Parameters and False Positive Rate

```
Given:
  n = expected number of elements
  m = number of bits in the filter
  k = number of hash functions

Optimal number of hash functions:
  k = (m/n) * ln(2)  ≈  0.693 * (m/n)

False positive probability:
  p ≈ (1 - e^(-kn/m))^k

Bits per element for target false positive rate:

  ┌──────────────────────────────────────────────────┐
  │  Target FP Rate │ Bits/Element │ Hash Functions  │
  ├─────────────────┼──────────────┼─────────────────┤
  │     10%         │    4.8       │      3          │
  │      5%         │    6.2       │      4          │
  │      1%         │    9.6       │      7          │
  │      0.1%       │   14.4       │     10          │
  │      0.01%      │   19.2       │     13          │
  └──────────────────────────────────────────────────┘

Example: 1 million keys with 1% false positive rate
  m = 9.6 * 1,000,000 = 9,600,000 bits = 1.2 MB
  k = 7 hash functions

  1.2 MB in memory to avoid up to 99% of unnecessary disk reads.
  That is an extraordinary trade-off.
```

### Bloom Filters in LSM-Trees

```
LSM-Tree structure with Bloom filters:

  ┌────────────────────────────────────┐
  │           MemTable (in memory)      │
  │     (skip list or red-black tree)   │
  └──────────────┬─────────────────────┘
                 │ flush
                 ▼
  ┌────────────────────────────────────┐
  │     Level 0: SSTable files          │
  │  ┌─────────┐  ┌─────────┐         │
  │  │ SST-0a  │  │ SST-0b  │         │
  │  │ [Bloom] │  │ [Bloom] │         │   ← Each SSTable has its own
  │  └─────────┘  └─────────┘         │      Bloom filter in memory
  ├────────────────────────────────────┤
  │     Level 1: SSTable files          │
  │  ┌─────────┐  ┌─────────┐         │
  │  │ SST-1a  │  │ SST-1b  │  ...    │
  │  │ [Bloom] │  │ [Bloom] │         │
  │  └─────────┘  └─────────┘         │
  ├────────────────────────────────────┤
  │     Level 2: SSTable files          │
  │  ┌─────────┐  ┌─────────┐         │
  │  │ SST-2a  │  │ SST-2b  │  ...    │
  │  │ [Bloom] │  │ [Bloom] │         │
  │  └─────────┘  └─────────┘         │
  └────────────────────────────────────┘

  Read path for key K:
  1. Check MemTable                     → O(log N)
  2. For each SSTable (L0 first):
     a. Check Bloom filter for K        → O(k) in memory
     b. If "definitely not" → SKIP      → saved a disk read!
     c. If "maybe yes" → read SSTable   → disk I/O
  3. Return first found (most recent)

  Without Bloom filters: worst case reads ALL SSTables
  With Bloom filters: reads only SSTables that likely contain K
```

### Counting Bloom Filters

```
Problem: Standard Bloom filters don't support DELETE.
         You can't "unset" a bit because it might be shared by multiple elements.

Solution: Replace each bit with a COUNTER (typically 4 bits).

  Standard:   [0, 0, 1, 0, 1, 0, 0, 1, ...]   ← bits
  Counting:   [0, 0, 2, 0, 1, 0, 0, 3, ...]   ← counters

  INSERT: increment all k counter positions
  DELETE: decrement all k counter positions
  LOOKUP: check if all k positions are > 0

  Trade-off: 4x more memory (4-bit counters vs 1-bit)
  Risk: Counter overflow (4 bits = max 15, then what?)
```

### Cuckoo Filters (Improvement over Bloom)

```
Cuckoo filter advantages over Bloom:
  ✓ Supports DELETE (without counters)
  ✓ Better space efficiency at low false positive rates (< 3%)
  ✓ Better lookup performance (check only 2 positions)

Structure:
  - Hash table with 2 candidate buckets per element
  - Stores FINGERPRINTS (compact hashes) instead of elements
  - Insertion uses "cuckoo" displacement if both buckets are full

  ┌────────────────────────────────────────────────────────┐
  │  Bucket Array:                                         │
  │  [0]: fp=A3  fp=--  fp=B1  fp=--                      │
  │  [1]: fp=C2  fp=D5  fp=--  fp=--                      │
  │  [2]: fp=E4  fp=--  fp=--  fp=--                      │
  │  [3]: fp=F1  fp=G7  fp=H2  fp=A9                      │
  │  ...                                                    │
  │                                                         │
  │  INSERT key K:                                          │
  │    fp = fingerprint(K)                                  │
  │    b1 = hash1(K)                                        │
  │    b2 = b1 XOR hash(fp)   ← partial-key cuckoo hashing │
  │    If b1 or b2 has space → insert fp there              │
  │    Else → evict existing fp and relocate it             │
  │                                                         │
  │  DELETE key K:                                          │
  │    Find fp in b1 or b2 → remove it                      │
  └────────────────────────────────────────────────────────┘

Used by: DynamoDB (DAX), various in-memory caches
```

### Systems Using Bloom Filters

| System | Usage |
|--------|-------|
| RocksDB/LevelDB | Per-SSTable Bloom filter; avoids unnecessary reads during compaction and lookups |
| Apache Cassandra | Per-SSTable; configurable `bloom_filter_fp_chance` (default 0.01 = 1%) |
| HBase | Per-HFile block; configurable in column family descriptor |
| PostgreSQL | Not built-in, but available via extensions; used internally for hash joins |
| ClickHouse | Data skipping indexes using Bloom filters on columns |
| Ethereum | Bloom filters in block headers for log event search |
| Chrome | Bloom filter for malicious URL checking (Safe Browsing) |

---

## 6. Skip Lists

### Structure

```
A skip list is a PROBABILISTIC data structure that layers multiple linked lists
on top of each other. Higher levels skip over more elements, enabling O(log N)
search in a structure based on linked lists.

Level 3:  HEAD ───────────────────────────────── 50 ─────────────────── NIL
           │                                      │
Level 2:  HEAD ──────── 20 ───────────────────── 50 ──── 70 ────────── NIL
           │             │                        │       │
Level 1:  HEAD ── 10 ── 20 ──── 30 ──────────── 50 ──── 70 ── 80 ── NIL
           │      │      │       │                │       │     │
Level 0:  HEAD ── 10 ── 20 ── 25 ── 30 ── 40 ── 50 ── 60 ── 70 ── 80 ── 90 ── NIL
           (base level: complete sorted linked list)

Each node has a randomly determined HEIGHT.
  - Level 0: every node (probability = 1)
  - Level 1: ~1/2 of nodes (probability p = 0.5)
  - Level 2: ~1/4 of nodes (probability p^2 = 0.25)
  - Level k: ~1/2^k of nodes

Expected height of the skip list: O(log N)
```

### Search Algorithm

```
Search for value 40:

Level 3:  HEAD ──────────────────────[50]──── NIL
           │                          │
           40 < 50, so go DOWN        │

Level 2:  HEAD ────[20]──────────────[50]──── NIL
           │        │                 │
           40 > 20, move RIGHT to 20  │
                    │                 │
           20 ─────────────────────[50]
                                   │
           40 < 50, so go DOWN at 20│

Level 1:  ──[20]────[30]───────────[50]──── NIL
              │       │             │
           40 > 30, move RIGHT to 30
                      │             │
           30 ──────────────────[50]
                                 │
           40 < 50, so go DOWN at 30

Level 0:  ──[30]──[40]──[50]──
                    ↑
                 FOUND! 40

Steps: 3 level changes + 3 horizontal moves = O(log N) expected
```

### Insert Algorithm

```
Insert value 35:

Step 1: Search for position (same as search), remembering UPDATE pointers
        at each level where we go DOWN.

Step 2: Determine random height for new node.
        Flip coins: H, H, T  →  height = 2 (levels 0, 1)

Step 3: Insert at each level up to height:

BEFORE:
Level 1:  HEAD ── 10 ── 20 ──── 30 ──────────── 50 ── NIL
Level 0:  HEAD ── 10 ── 20 ── 25 ── 30 ── 40 ── 50 ── NIL

AFTER:
Level 1:  HEAD ── 10 ── 20 ──── 30 ── [35] ──── 50 ── NIL
                                        ↑ new
Level 0:  HEAD ── 10 ── 20 ── 25 ── 30 ── [35] ── 40 ── 50 ── NIL
                                            ↑ new

Only pointers at the insertion point are modified.
No rebalancing needed (unlike AVL/Red-Black trees).
```

### Delete Algorithm

```
Delete value 30:

Step 1: Search for 30, collecting predecessor pointers at each level.
Step 2: Remove 30 from each level it appears in.
Step 3: If top levels are now empty, reduce max level.

BEFORE:
Level 2:  HEAD ──────── 20 ──────────── 50 ── NIL
Level 1:  HEAD ── 10 ── 20 ── 30 ────── 50 ── NIL
Level 0:  HEAD ── 10 ── 20 ── 30 ── 40 ── 50 ── NIL

AFTER:
Level 2:  HEAD ──────── 20 ──────────── 50 ── NIL
Level 1:  HEAD ── 10 ── 20 ──────────── 50 ── NIL
Level 0:  HEAD ── 10 ── 20 ──── 40 ──── 50 ── NIL
                          ↑     ↑
                     30 removed, 20→40 linked
```

### Complexity Analysis

```
┌─────────────────────────────────────────────────────────────────┐
│  Operation    │  Expected    │  Worst Case  │  Space            │
├───────────────┼──────────────┼──────────────┼───────────────────┤
│  Search       │  O(log N)    │  O(N)        │                   │
│  Insert       │  O(log N)    │  O(N)        │  O(N) expected    │
│  Delete       │  O(log N)    │  O(N)        │  with O(N log N)  │
│  Range scan   │  O(log N+K)  │  O(N)        │  worst case       │
│  Min/Max      │  O(1)        │  O(1)        │  (follow level 0) │
└───────────────┴──────────────┴──────────────┴───────────────────┘

Worst case O(N) happens when all nodes have height 1 (extremely unlikely).
Expected complexity matches balanced BSTs, with simpler implementation.
```

### Lock-Free Skip Lists for Concurrent Access

```
Why skip lists excel at concurrency:

  B+Tree insert/delete:
    - May require page splits/merges
    - Splits propagate upward → must lock parent nodes
    - Lock coupling (crabbing) protocol required
    - Complex concurrent access patterns

  Skip list insert/delete:
    - Only modifies LOCAL pointers
    - No global rebalancing
    - Can use COMPARE-AND-SWAP (CAS) for lock-free operation
    - Different threads can insert at different positions simultaneously

  Lock-free insert (simplified):
  ┌──────────────────────────────────────────────────────┐
  │  1. Search for position (no locks needed)             │
  │  2. Create new node with all forward pointers         │
  │  3. CAS: predecessor.next = new_node                  │
  │     (atomically: if predecessor.next == expected,      │
  │      set it to new_node, else retry)                   │
  │  4. Repeat CAS for each level bottom-up               │
  └──────────────────────────────────────────────────────┘

  Java's ConcurrentSkipListMap uses this approach.
```

### Where Skip Lists Are Used

| System | Component | Why Skip List? |
|--------|-----------|----------------|
| Redis | Sorted Sets (ZSET) | Simpler than balanced trees; good range query support; easy to implement ZRANGEBYSCORE |
| LevelDB / RocksDB | MemTable | Lock-free concurrent writes; in-memory sorted structure before flushing to SSTable |
| WiredTiger (MongoDB) | Internal data structures | Concurrent access patterns |
| Java | `ConcurrentSkipListMap/Set` | Lock-free sorted concurrent collection |
| Apache Lucene | Some internal structures | Concurrent access during indexing |

---

## 7. Specialized Index Types

### 7.1 GIN (Generalized Inverted Index)

Used in PostgreSQL for: full-text search, array containment, JSONB queries, trigram search.

```
Concept: Inverted Index
  Maps VALUES → list of ROWS that contain them.

Example: Full-text search on articles table

  Row 1: "PostgreSQL is a powerful database"
  Row 2: "MySQL is another popular database"
  Row 3: "PostgreSQL supports powerful indexing"

  GIN Index (inverted index):
  ┌──────────────┬───────────────────┐
  │    Term       │    Posting List   │
  ├──────────────┼───────────────────┤
  │  "another"   │  {2}              │
  │  "database"  │  {1, 2}           │
  │  "indexing"  │  {3}              │
  │  "is"        │  {1, 2}           │
  │  "mysql"     │  {2}              │
  │  "popular"   │  {2}              │
  │  "postgresql"│  {1, 3}           │
  │  "powerful"  │  {1, 3}           │
  │  "supports"  │  {3}              │
  └──────────────┴───────────────────┘

  Query: WHERE to_tsvector(body) @@ to_tsquery('postgresql & powerful')

  Look up "postgresql" → {1, 3}
  Look up "powerful"   → {1, 3}
  Intersect:           → {1, 3}   ← Rows 1 and 3 match

  B+Tree structure of the GIN:

  Root: [indexing | popular]
       /        |        \
  Leaf:  [another, database]  [indexing, is, mysql]  [popular, postgresql, powerful, supports]
          {2}      {1,2}       {3}     {1,2} {2}      {2}       {1,3}       {1,3}     {3}
```

```
GIN Pending List (Fast Updates):

  Problem: Inserting a document into GIN requires updating MANY posting lists
           (one per unique term in the document). This is slow.

  Solution: PostgreSQL maintains a PENDING LIST (unordered buffer).

  ┌──────────────────────────────────────────────────────────┐
  │  INSERT new document → append to pending list (fast!)     │
  │                                                           │
  │  Pending List:                                            │
  │    doc_15: ["redis", "cache", "fast"]                     │
  │    doc_16: ["postgresql", "cache", "reliable"]            │
  │    doc_17: ["redis", "pubsub", "fast"]                    │
  │                                                           │
  │  Queries scan BOTH the main GIN tree AND the pending list │
  │                                                           │
  │  VACUUM or gin_clean_pending_list() merges pending list   │
  │  into the main GIN tree (batch update, much more efficient)│
  └──────────────────────────────────────────────────────────┘

  Tuning:
    gin_pending_list_limit = 4MB (default: 4MB)
    fastupdate = on (default: on for GIN)
```

```sql
-- GIN usage examples in PostgreSQL:

-- Full-text search
CREATE INDEX idx_articles_fts ON articles USING gin(to_tsvector('english', body));

-- Array containment
CREATE INDEX idx_tags ON posts USING gin(tags);
-- WHERE tags @> ARRAY['postgresql', 'indexing']

-- JSONB containment
CREATE INDEX idx_metadata ON events USING gin(metadata jsonb_path_ops);
-- WHERE metadata @> '{"type": "click", "source": "mobile"}'

-- Trigram search (pg_trgm extension)
CREATE INDEX idx_name_trgm ON users USING gin(name gin_trgm_ops);
-- WHERE name ILIKE '%alice%'  (supports middle-of-string matching!)
```

### 7.2 GiST (Generalized Search Tree)

A framework for building balanced tree indexes over arbitrary data types. The most common instantiation is the **R-Tree** for spatial data.

```
R-Tree for 2D spatial data (points and rectangles):

  Each internal node contains a BOUNDING RECTANGLE that encloses
  all child entries. Search prunes branches whose bounding box
  doesn't overlap with the query region.

  ┌─────────────────────────────────────────────────┐
  │  2D Space:                                       │
  │                                                   │
  │   ┌─────────────R1──────────┐                    │
  │   │  A(1,8)    B(3,7)      │   ┌──────R2──────┐ │
  │   │                         │   │   E(8,9)     │ │
  │   │     C(2,5)             │   │              │ │
  │   │                         │   │   F(9,6)    │ │
  │   └─────────────────────────┘   └──────────────┘ │
  │                                                   │
  │   ┌─────────R3──────────────┐                    │
  │   │  D(1,2)                │                    │
  │   │         G(4,1)  H(6,3) │                    │
  │   └─────────────────────────┘                    │
  └─────────────────────────────────────────────────┘

  R-Tree Structure:

              Root
             /    \
          ┌──┐   ┌──┐
          │R1│   │R3│
          │R2│   │  │
          └──┘   └──┘
         / |      |  \
       R1  R2    R3
      /|\  /\   /|\
     A B C E F D G H

  Query: "Find all points within rectangle (0,4)-(5,10)"

  1. Check root children:
     R1 bounding box overlaps query? YES → descend
     R2 bounding box overlaps query? NO  → prune
     R3 bounding box overlaps query? NO  → prune

  2. Check R1 children:
     A(1,8) in query range? YES → include
     B(3,7) in query range? YES → include
     C(2,5) in query range? YES → include

  Result: {A, B, C}  -- scanned 6 entries instead of 8
```

```sql
-- GiST usage in PostgreSQL:

-- Spatial indexing (PostGIS)
CREATE INDEX idx_locations ON places USING gist(geom);
-- WHERE ST_DWithin(geom, ST_MakePoint(-73.9, 40.7)::geography, 1000)

-- Range type indexing
CREATE INDEX idx_reservation ON bookings USING gist(during);
-- WHERE during && tsrange('2024-01-01', '2024-01-31')
-- (overlap operator for range types)

-- Exclusion constraints (no overlapping bookings)
ALTER TABLE bookings ADD CONSTRAINT no_overlap
  EXCLUDE USING gist (room_id WITH =, during WITH &&);

-- Full-text search (alternative to GIN, smaller but slower)
CREATE INDEX idx_fts ON articles USING gist(to_tsvector('english', body));
```

### 7.3 BRIN (Block Range Index)

```
BRIN stores SUMMARY INFORMATION (min/max) for each BLOCK RANGE.
Extremely compact. Ideal for naturally ordered data.

Table: events (sorted by created_at, 10 million rows)

  Physical pages:
  ┌──────────────────────────────────────────────────────────────────┐
  │ Pages 0-127:     created_at from 2024-01-01 to 2024-01-15       │
  │ Pages 128-255:   created_at from 2024-01-15 to 2024-01-30       │
  │ Pages 256-383:   created_at from 2024-01-30 to 2024-02-14       │
  │ Pages 384-511:   created_at from 2024-02-14 to 2024-02-28       │
  │ Pages 512-639:   created_at from 2024-02-28 to 2024-03-15       │
  │ ...                                                              │
  └──────────────────────────────────────────────────────────────────┘

  BRIN Index (pages_per_range = 128):
  ┌────────────┬─────────────────────┬─────────────────────┐
  │ Block Range│     Min Value        │     Max Value        │
  ├────────────┼─────────────────────┼─────────────────────┤
  │   0-127    │   2024-01-01 00:00  │   2024-01-15 23:59  │
  │ 128-255    │   2024-01-15 00:00  │   2024-01-30 23:59  │
  │ 256-383    │   2024-01-30 00:00  │   2024-02-14 23:59  │
  │ 384-511    │   2024-02-14 00:00  │   2024-02-28 23:59  │
  │ 512-639    │   2024-02-28 00:00  │   2024-03-15 23:59  │
  │   ...      │        ...          │        ...          │
  └────────────┴─────────────────────┴─────────────────────┘

  Query: WHERE created_at BETWEEN '2024-02-01' AND '2024-02-20'

  Check BRIN:
    0-127:   max=Jan 15 < Feb 01 → SKIP
    128-255: max=Jan 30 < Feb 01 → SKIP
    256-383: min=Jan 30, max=Feb 14 → Feb 01 in range → SCAN THIS RANGE
    384-511: min=Feb 14, max=Feb 28 → Feb 20 in range → SCAN THIS RANGE
    512+:    min=Feb 28 > Feb 20 → SKIP all remaining

  Result: Only scan pages 256-511 instead of entire table
```

```
BRIN vs B+Tree Size Comparison:

  10 million rows, created_at (timestamp, 8 bytes)

  B+Tree index:
    Leaf entries: 10M x (8 byte key + 6 byte TID) = ~140 MB
    Internal nodes: ~1 MB
    Total: ~141 MB

  BRIN index (pages_per_range = 128):
    Blocks: ~75,000 pages / 128 = ~586 range entries
    Each entry: 2 x 8 bytes (min/max) + overhead ≈ 24 bytes
    Total: 586 x 24 ≈ 14 KB  (!!!)

  ┌────────────────────────────────────────┐
  │  B+Tree:  141 MB                       │
  │  BRIN:     14 KB  (10,000x smaller!)   │
  └────────────────────────────────────────┘
```

```sql
-- BRIN usage in PostgreSQL:

-- Perfect for append-only time-series data
CREATE INDEX idx_events_time ON events USING brin(created_at)
  WITH (pages_per_range = 128);

-- Also good for auto-incrementing IDs
CREATE INDEX idx_events_id ON events USING brin(id)
  WITH (pages_per_range = 64);

-- Multi-column BRIN
CREATE INDEX idx_events_multi ON events USING brin(created_at, sensor_id);
```

```
When BRIN beats B+Tree:
  ✓ Data is physically ordered by the indexed column (or nearly so)
  ✓ Table is very large (millions+ rows)
  ✓ Queries filter on ranges (BETWEEN, >, <)
  ✓ Storage is a constraint
  ✓ Insert performance is critical (BRIN barely slows writes)

When BRIN loses:
  ✗ Data is randomly ordered (min/max per range overlap → no pruning)
  ✗ Point lookups (need exact match, BRIN gives range of pages)
  ✗ Small tables (sequential scan is fine)
```

### 7.4 SP-GiST (Space-Partitioned GiST)

```
SP-GiST supports SPACE-PARTITIONING trees:
  - Tries (prefix trees)
  - Quadtrees
  - k-d Trees

These structures recursively divide space into NON-OVERLAPPING regions
(unlike GiST/R-Tree where regions can overlap).

TRIE for text data:
                        (root)
                       /   |   \
                      a    b    c
                     / \   |    |
                    l   n  a    a
                    |   |  |    |
                    i   d  r    t
                    |
                    c
                    |
                    e
               "alice" "and" "bar" "cat"

k-d Tree for 2D points (alternating split dimensions):
                         (5,4)  split on x
                        /      \
                  (2,3)          (7,2)  split on y
                 /    \         /    \
              (1,1)  (3,6)  (6,1)  (8,7)  split on x

  Searching for nearest neighbor to (4,3):
  1. Start at root (5,4), go left (4 < 5)
  2. At (2,3), go right (3 >= 3 on y-axis)
  3. At (3,6), leaf → distance = sqrt(1+9) = sqrt(10)
  4. Backtrack and check other branches if needed
```

```sql
-- SP-GiST usage in PostgreSQL:

-- IP address range queries
CREATE INDEX idx_ip ON network_logs USING spgist(ip_addr inet_ops);
-- WHERE ip_addr <<= '10.0.0.0/8'

-- Text prefix matching
CREATE INDEX idx_text ON documents USING spgist(content text_ops);
-- WHERE content LIKE 'prefix%'

-- Point data (k-d tree)
CREATE INDEX idx_points ON locations USING spgist(coordinates);
```

---

## 8. Full-Text Search Indexing

### Inverted Index Structure (Detailed)

```
Document Collection:
  Doc 1: "The quick brown fox jumps over the lazy dog"
  Doc 2: "A quick brown cat jumps over the lazy fox"
  Doc 3: "The dog barked at the fox"

Processing Pipeline:
  1. TOKENIZATION:     Split into words
  2. LOWERCASING:      "The" → "the"
  3. STOP WORD REMOVAL: Remove "the", "a", "over", "at"
  4. STEMMING:         "jumps" → "jump", "barked" → "bark", "lazy" → "lazi"

Term Dictionary + Posting Lists:

  ┌───────────┬──────┬──────────────────────────────────────────────┐
  │   Term     │  DF  │  Posting List                                │
  ├───────────┼──────┼──────────────────────────────────────────────┤
  │  "bark"   │  1   │  [(doc3, pos:[2], tf:1)]                    │
  │  "brown"  │  2   │  [(doc1, pos:[3], tf:1), (doc2, pos:[3], tf:1)] │
  │  "cat"    │  1   │  [(doc2, pos:[4], tf:1)]                    │
  │  "dog"    │  2   │  [(doc1, pos:[9], tf:1), (doc3, pos:[1], tf:1)] │
  │  "fox"    │  3   │  [(doc1, pos:[4], tf:1), (doc2, pos:[9], tf:1), │
  │           │      │   (doc3, pos:[4], tf:1)]                     │
  │  "jump"   │  2   │  [(doc1, pos:[5], tf:1), (doc2, pos:[5], tf:1)] │
  │  "lazi"   │  2   │  [(doc1, pos:[8], tf:1), (doc2, pos:[8], tf:1)] │
  │  "quick"  │  2   │  [(doc1, pos:[2], tf:1), (doc2, pos:[2], tf:1)] │
  └───────────┴──────┴──────────────────────────────────────────────┘

  DF = Document Frequency (how many documents contain this term)
  tf = Term Frequency (how many times term appears in this document)
  pos = Positions (for phrase queries)
```

### TF-IDF Scoring

```
TF-IDF(term, document) = TF(term, doc) x IDF(term)

  TF (Term Frequency):
    How often does the term appear in THIS document?
    TF = count(term in doc) / total_terms_in_doc

  IDF (Inverse Document Frequency):
    How rare is this term across ALL documents?
    IDF = log(N / DF)     where N = total docs, DF = docs containing term

    Rare terms get HIGH IDF → they're more discriminative.
    Common terms get LOW IDF → less useful for distinguishing docs.

Example: Query "fox" across 3 documents

  IDF("fox") = log(3 / 3) = log(1) = 0  ← appears in ALL docs, zero weight!
  IDF("cat") = log(3 / 1) = 1.1         ← appears in only 1 doc, high weight
  IDF("dog") = log(3 / 2) = 0.41

  Modern systems use BM25 instead of raw TF-IDF:
  BM25 adds:
    - Term frequency saturation (diminishing returns for repeated terms)
    - Document length normalization (long docs aren't unfairly boosted)

  BM25(term, doc) = IDF(term) * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl/avgdl))
    k1 = 1.2 (term frequency saturation)
    b  = 0.75 (length normalization)
    dl = document length
    avgdl = average document length
```

### PostgreSQL Full-Text Search

```sql
-- Create tsvector column (pre-computed for performance)
ALTER TABLE articles ADD COLUMN search_vector tsvector;

UPDATE articles SET search_vector =
  setweight(to_tsvector('english', title), 'A') ||       -- title weight A
  setweight(to_tsvector('english', body), 'B');           -- body weight B

-- Create GIN index on the tsvector
CREATE INDEX idx_articles_search ON articles USING gin(search_vector);

-- Query with tsquery
SELECT title, ts_rank(search_vector, query) AS rank
FROM articles, to_tsquery('english', 'postgresql & indexing') AS query
WHERE search_vector @@ query
ORDER BY rank DESC
LIMIT 10;

-- PostgreSQL tsvector internals:
-- 'postgresql':1A 'index':5B 'powerful':3A,7B
--  ↑ lexeme       ↑ position + weight
```

### Elasticsearch/Lucene Internals

```
Lucene Segment Architecture:

  ┌─────────────────────────────────────────────────────────────┐
  │                     Lucene Index                             │
  │                                                              │
  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
  │  │ Segment 0│  │ Segment 1│  │ Segment 2│  │ Segment 3│   │
  │  │ (large,  │  │ (medium) │  │ (small)  │  │ (tiny,   │   │
  │  │  merged) │  │          │  │          │  │  recent) │   │
  │  │          │  │          │  │          │  │          │   │
  │  │ 500K docs│  │ 100K docs│  │ 10K docs │  │ 1K docs  │   │
  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘   │
  │                                                              │
  │  Each segment is IMMUTABLE after creation.                   │
  │  New documents go into a new segment.                        │
  │  Background MERGE process combines small segments into       │
  │  larger ones (like LSM-Tree compaction).                     │
  └─────────────────────────────────────────────────────────────┘

  Inside a Segment:
  ┌──────────────────────────────────────────────────────────────┐
  │  Term Dictionary (FST - Finite State Transducer)             │
  │    Compressed prefix trie for term lookups                   │
  │                                                               │
  │  Posting Lists (per term)                                     │
  │    - Document IDs (delta-encoded, bit-packed)                │
  │    - Term frequencies                                         │
  │    - Positions (for phrase queries)                            │
  │    - Offsets (for highlighting)                                │
  │    - Payloads (custom per-position data)                      │
  │                                                               │
  │  Stored Fields                                                │
  │    - Original document content (for returning results)        │
  │    - Compressed with LZ4/DEFLATE                              │
  │                                                               │
  │  Doc Values (columnar storage)                                │
  │    - For sorting, aggregations, scripting                     │
  │    - Column-oriented format, very cache-friendly              │
  │                                                               │
  │  Norms                                                        │
  │    - Document length factors for scoring                      │
  │                                                               │
  │  Deletion Bitmap                                              │
  │    - Marks deleted docs (segment is immutable, can't remove)  │
  │    - Deleted docs are actually removed during merge            │
  └──────────────────────────────────────────────────────────────┘

  Search across segments:
  1. Query each segment independently
  2. Merge results using priority queue (sorted by score)
  3. Apply pagination (from/size)
```

```
Segment Merge Process:

  Before merge:
    Seg-A (100 docs) + Seg-B (80 docs) + Seg-C (50 docs)

  During merge:
    - Read all docs from A, B, C
    - Skip deleted docs (from deletion bitmaps)
    - Build new term dictionary and posting lists
    - Write new segment Seg-D (210 docs, minus deleted)

  After merge:
    Seg-D (210 docs)  ← A, B, C are removed

  Merge is I/O intensive but essential for:
    - Removing deleted documents
    - Reducing number of segments (fewer segments = faster queries)
    - Reclaiming disk space
```

---

## 9. Index Maintenance and Operations

### Index Bloat and Fragmentation

```
How B+Tree indexes become bloated over time:

  INITIAL STATE (all pages full):
  ┌────────┐  ┌────────┐  ┌────────┐
  │████████│→│████████│→│████████│
  │████████│  │████████│  │████████│
  └────────┘  └────────┘  └────────┘
  3 pages, 100% utilization

  AFTER MANY DELETE/INSERT CYCLES:
  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐
  │██  ░░  │→│░░██░░  │→│  ░░██  │→│██░░░░  │→│  ░░░░██│
  │░░██░░  │  │  ██░░  │  │██  ░░  │  │░░░░██  │  │██░░░░  │
  └────────┘  └────────┘  └────────┘  └────────┘  └────────┘
  5 pages, ~40% utilization (60% wasted!)

  ██ = live data
  ░░ = dead/free space

  Consequences:
    - Index is 67% larger than necessary (5 pages vs 3)
    - More disk I/O for index scans (reading wasted space)
    - More buffer pool memory consumed by half-empty pages
    - Range scans become slower
```

**PostgreSQL-specific bloat**: Due to MVCC, dead tuples remain in indexes until `VACUUM` removes them. In extreme cases, indexes can be 10x their optimal size.

### REINDEX Operations

```sql
-- PostgreSQL: Rebuild an index from scratch
REINDEX INDEX idx_users_email;           -- Locks the table! (ACCESS EXCLUSIVE)
REINDEX TABLE users;                     -- Rebuilds ALL indexes on the table

-- PostgreSQL 12+: Concurrent reindex (no table lock)
REINDEX INDEX CONCURRENTLY idx_users_email;
-- Creates a new index alongside the old one, then swaps them atomically

-- MySQL/InnoDB:
ALTER TABLE users ENGINE=InnoDB;          -- Rebuilds table + all indexes
OPTIMIZE TABLE users;                     -- Same effect
ALTER TABLE users DROP INDEX idx_email,
                   ADD INDEX idx_email(email);  -- Explicit rebuild

-- SQL Server:
ALTER INDEX idx_users_email ON users REBUILD;       -- Full rebuild
ALTER INDEX idx_users_email ON users REORGANIZE;    -- Defragment in place (lighter)
```

### Concurrent Index Creation

```sql
-- PostgreSQL: CREATE INDEX CONCURRENTLY
-- Does NOT lock the table for writes during index build

CREATE INDEX CONCURRENTLY idx_users_email ON users(email);
```

```
How CONCURRENTLY works internally:

  Phase 1: CREATE empty index structure + register in catalog
           Table is writable. New writes update both old indexes AND new index.

  Phase 2: FIRST TABLE SCAN
           Scan entire table, insert all existing rows into new index.
           Concurrent writes keep updating the index too.

  Phase 3: SECOND TABLE SCAN (validation pass)
           Re-scan to pick up any rows that changed during Phase 2.
           Handle any conflicts with concurrent transactions.

  Phase 4: MARK index as VALID
           Index is now ready for query planning.

  ┌──────────────────────────────────────────────────────────────┐
  │  Caveats:                                                    │
  │  - Takes 2-3x longer than regular CREATE INDEX               │
  │  - Can fail and leave an INVALID index                       │
  │      (check: SELECT * FROM pg_indexes WHERE NOT indisvalid)  │
  │  - Cannot be run inside a transaction block                  │
  │  - Requires waiting for all existing transactions to finish  │
  │  - If it fails, you must DROP the invalid index and retry    │
  └──────────────────────────────────────────────────────────────┘
```

### Index-Only Scans and Visibility Map

```
PostgreSQL Index-Only Scan requirement:

  An index-only scan avoids the heap lookup ONLY if the page is marked
  ALL-VISIBLE in the VISIBILITY MAP.

  Visibility Map:
  ┌─────────────────────────────────────────────────────────────────┐
  │  Heap Page 0:  [all-visible=YES]  → index-only scan OK         │
  │  Heap Page 1:  [all-visible=YES]  → index-only scan OK         │
  │  Heap Page 2:  [all-visible=NO]   → must check heap for MVCC   │
  │  Heap Page 3:  [all-visible=YES]  → index-only scan OK         │
  │  Heap Page 4:  [all-visible=NO]   → must check heap for MVCC   │
  └─────────────────────────────────────────────────────────────────┘

  After a large batch of UPDATEs/DELETEs, many pages are NOT all-visible.
  → Index-only scans degrade to regular index scans + heap fetches.
  → VACUUM marks pages as all-visible again.

  VACUUM timing matters:
  ┌────────────────────────────────────────────────────────┐
  │  Fresh VACUUM → most pages all-visible                 │
  │              → index-only scans are very fast           │
  │                                                         │
  │  Stale VACUUM → many dirty pages                       │
  │              → index-only scans degrade significantly   │
  │              → EXPLAIN shows high "Heap Fetches" count  │
  └────────────────────────────────────────────────────────┘

  Check in EXPLAIN output:
    Index Only Scan using idx_email on users
      Heap Fetches: 42       ← GOOD (low = visibility map effective)
      Heap Fetches: 980000   ← BAD (need VACUUM)
```

### Unused Index Detection

```sql
-- PostgreSQL: Find unused indexes
SELECT
    schemaname || '.' || relname AS table,
    indexrelname AS index,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size,
    idx_scan AS number_of_scans,
    idx_tup_read AS tuples_read,
    idx_tup_fetch AS tuples_fetched
FROM pg_stat_user_indexes
WHERE idx_scan = 0                          -- never used for scanning
  AND indexrelid NOT IN (                   -- not used for unique constraint
      SELECT indexrelid FROM pg_index WHERE indisunique
  )
ORDER BY pg_relation_size(indexrelid) DESC;

-- MySQL: Find unused indexes (requires performance_schema)
SELECT
    object_schema, object_name, index_name,
    count_read, count_write,
    count_fetch
FROM performance_schema.table_io_waits_summary_by_index_usage
WHERE index_name IS NOT NULL
  AND count_read = 0
ORDER BY count_write DESC;
```

```
Index Advisor Strategy:

  ┌──────────────────────────────────────────────────────────────┐
  │  Step 1: Identify SLOW QUERIES                               │
  │    - pg_stat_statements (PostgreSQL)                         │
  │    - slow_query_log (MySQL)                                  │
  │    - Query Store (SQL Server)                                │
  │                                                               │
  │  Step 2: Run EXPLAIN ANALYZE on slow queries                  │
  │    - Look for Seq Scan on large tables                        │
  │    - Look for high-cost operations                            │
  │    - Look for missing index suggestions (some tools)          │
  │                                                               │
  │  Step 3: Identify UNUSED INDEXES                              │
  │    - pg_stat_user_indexes.idx_scan = 0                       │
  │    - Remove unused indexes to speed up writes                 │
  │                                                               │
  │  Step 4: Identify DUPLICATE INDEXES                           │
  │    - Index (a, b) makes index (a) redundant                  │
  │    - But (a) is NOT redundant if used for index-only scans   │
  │      on column a alone (different covering set)               │
  │                                                               │
  │  Step 5: Consider PARTIAL and COVERING indexes                │
  │    - Add WHERE clause to reduce index size                    │
  │    - Add INCLUDE columns to enable index-only scans           │
  └──────────────────────────────────────────────────────────────┘

Tools:
  - PostgreSQL: pg_stat_statements, auto_explain, hypopg (hypothetical indexes)
  - MySQL: EXPLAIN ANALYZE (8.0+), pt-index-usage (Percona)
  - SQL Server: Database Engine Tuning Advisor, Missing Index DMVs
  - General: pgHero, Dexter (auto-index for PostgreSQL)
```

---

## 10. Index Selection Strategy

### Decision Framework: When to Use Which Index Type

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                          INDEX TYPE SELECTION GUIDE                            │
├────────────────────┬──────────────────────────────────────────────────────────┤
│  Query Pattern      │  Recommended Index                                      │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Equality lookup    │  B+Tree (default), Hash (if ONLY equality ever needed)  │
│  WHERE col = val    │                                                          │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Range query        │  B+Tree                                                 │
│  WHERE col > val    │  BRIN if data is naturally ordered                       │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Multi-column       │  B+Tree composite (respect leftmost prefix rule)        │
│  WHERE a=? AND b>?  │  Ordered: equality columns first, then range columns    │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Full-text search   │  GIN (PostgreSQL) or dedicated FTS engine               │
│  WHERE body @@ q    │  (Elasticsearch, Typesense, Meilisearch)                │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Array contains     │  GIN (PostgreSQL)                                       │
│  WHERE tags @> arr  │                                                          │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  JSONB queries      │  GIN with jsonb_path_ops (PostgreSQL)                   │
│  WHERE data @> '{}'│                                                          │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Spatial / geometry │  GiST (R-Tree)                                          │
│  WHERE ST_DWithin   │  SP-GiST for specific partitioning schemes              │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Time-series range  │  BRIN (if append-only, naturally ordered by time)        │
│  WHERE ts BETWEEN   │  B+Tree if data is not ordered or needs point lookups   │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Low-cardinality    │  Bitmap (Oracle) or partial indexes (PostgreSQL)         │
│  WHERE status = ?   │  PostgreSQL: auto bitmap scan combining at runtime      │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Pattern matching   │  GIN with pg_trgm (LIKE '%substr%')                     │
│  LIKE '%substr%'    │  B+Tree only for prefix: LIKE 'prefix%'                │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  IP address ranges  │  SP-GiST (inet_ops)                                     │
│  WHERE ip <<= cidr  │  GiST as alternative                                   │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Nearest neighbor   │  GiST (for PostGIS), SP-GiST (for k-d tree)            │
│  ORDER BY distance  │  IVFFlat/HNSW (pgvector for vector similarity)          │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Exclusion          │  GiST with exclusion constraint                          │
│  (no overlaps)      │  EXCLUDE USING gist (col WITH &&)                       │
└────────────────────┴──────────────────────────────────────────────────────────┘
```

### Composite Index Design Methodology

```
Step-by-step process for designing composite indexes:

  1. GATHER all queries that hit the table
     (from pg_stat_statements, application code, ORM generated SQL)

  2. GROUP queries by their WHERE clause columns

  3. For each group, ORDER columns:
     a. Equality predicates first (highest selectivity leftmost)
     b. Range/inequality predicates next
     c. ORDER BY columns last (if no range predicate between)

  4. CHECK if adding INCLUDE columns enables index-only scans

  5. CHECK if a partial index (WHERE clause) can reduce size

  6. EVALUATE if one composite index can serve multiple query groups
     (via leftmost prefix rule)

Example:

  Query A: WHERE customer_id = ? AND status = ? ORDER BY created_at DESC
  Query B: WHERE customer_id = ? AND created_at > ?
  Query C: WHERE customer_id = ?

  Analysis:
  - All queries use customer_id (equality) → first column
  - Query A also uses status (equality) → second column
  - Query A needs ORDER BY created_at → third column
  - Query B uses created_at (range) → covered by prefix (customer_id)

  Candidate: CREATE INDEX idx ON orders(customer_id, status, created_at DESC);

  ┌─────────────────────────────────────────────────────────────────┐
  │  Query A: customer_id=? AND status=?  → uses (customer_id,     │
  │           ORDER BY created_at DESC       status, created_at)    │
  │           → index scan, no sort needed!  ✓ PERFECT              │
  │                                                                  │
  │  Query B: customer_id=? AND created_at>?                         │
  │           → uses (customer_id) prefix                            │
  │           → status column is "skipped" but scan still works      │
  │           → less optimal but functional  ✓ ACCEPTABLE            │
  │                                                                  │
  │  Query C: customer_id=?                                          │
  │           → uses (customer_id) prefix  ✓ PERFECT                 │
  └─────────────────────────────────────────────────────────────────┘

  Alternative for better Query B performance:
  INDEX 1: (customer_id, status, created_at DESC)  -- for Query A
  INDEX 2: (customer_id, created_at)               -- for Query B
  Trade-off: extra index = extra write overhead + storage
```

### The Index Merge Alternative

```
Instead of one composite index, the database can MERGE results from
multiple single-column indexes.

  Index 1: B+Tree on (customer_id)
  Index 2: B+Tree on (status)

  Query: WHERE customer_id = 100 AND status = 'pending'

  Plan A: Composite index scan
    Single index (customer_id, status) → direct lookup → fast

  Plan B: Index merge (Bitmap AND in PostgreSQL)
    1. Scan idx_customer_id for customer_id=100 → bitmap A (rows: {5,12,47,89,...})
    2. Scan idx_status for status='pending'     → bitmap B (rows: {3,12,33,47,...})
    3. AND bitmaps A and B                      → result:  {12, 47, ...}
    4. Fetch those rows from heap

  ┌───────────────────────────────────────────────────────────────┐
  │  Composite Index:                                             │
  │    + Faster for this specific query pattern                   │
  │    + Single index scan                                        │
  │    - Only useful for queries matching the leftmost prefix     │
  │    - Extra storage and write overhead                         │
  │                                                                │
  │  Index Merge:                                                  │
  │    + Each single-column index serves MANY different queries   │
  │    + More flexible                                             │
  │    - Slower for multi-column queries (two scans + merge)      │
  │    - Bitmap merge has overhead                                 │
  │                                                                │
  │  Rule of thumb:                                                │
  │    If a specific multi-column query is frequent and critical,  │
  │    create a composite index.                                   │
  │    If queries are ad-hoc and varied, single-column indexes     │
  │    with bitmap merge may be more flexible.                     │
  └───────────────────────────────────────────────────────────────┘
```

### Covering Index vs Join Elimination

```sql
-- Scenario: frequently run this query
SELECT o.order_id, o.total, c.customer_name
FROM orders o
JOIN customers c ON o.customer_id = c.id
WHERE o.status = 'shipped'
AND o.order_date > '2024-01-01';
```

```
Approach 1: COVERING INDEX (denormalize into index)

  CREATE INDEX idx_orders_covering ON orders(status, order_date)
    INCLUDE (order_id, total, customer_id);

  + Index-only scan on orders (no heap access)
  + Still need to join to customers for customer_name

Approach 2: MATERIALIZED VIEW or DENORMALIZATION

  -- Add customer_name to orders table (denormalize)
  ALTER TABLE orders ADD COLUMN customer_name TEXT;

  CREATE INDEX idx_orders_full ON orders(status, order_date)
    INCLUDE (order_id, total, customer_name);

  + Eliminates the join entirely
  + Index-only scan gets everything
  - Must maintain denormalized data (triggers, application logic)
  - Storage duplication

Decision Framework:
  ┌─────────────────────────────────────────────────────┐
  │  Query frequency: HIGH                               │
  │  Join cost: HIGH (large customers table)             │
  │  Data staleness tolerance: LOW → covering index      │
  │  Data staleness tolerance: HIGH → materialized view  │
  │  Write frequency on denormalized col: LOW → denorm   │
  │  Write frequency on denormalized col: HIGH → don't   │
  └─────────────────────────────────────────────────────┘
```

### Benchmark: Index Type Performance Comparison

```
Test setup: PostgreSQL 16, 10M rows, SSD storage, 4GB shared_buffers

Table: events (id bigint PK, created_at timestamp, user_id int,
               event_type varchar(20), payload jsonb)

Query: SELECT * FROM events
       WHERE created_at BETWEEN '2024-06-01' AND '2024-06-30'

┌──────────────────┬──────────────┬────────────────┬──────────────────┐
│  Index Type       │  Index Size  │  Query Time    │  Build Time      │
├──────────────────┼──────────────┼────────────────┼──────────────────┤
│  No index (seq)  │  0 MB        │  4,200 ms      │  N/A             │
│  B+Tree          │  214 MB      │  45 ms         │  28 sec          │
│  BRIN (128)      │  48 KB       │  120 ms        │  0.4 sec         │
│  BRIN (32)       │  192 KB      │  70 ms         │  0.5 sec         │
└──────────────────┴──────────────┴────────────────┴──────────────────┘

Query: SELECT * FROM events WHERE event_type = 'click'
  (event_type has 10 distinct values → low cardinality)

┌──────────────────┬──────────────┬────────────────┬──────────────────┐
│  Index Type       │  Index Size  │  Query Time    │  Build Time      │
├──────────────────┼──────────────┼────────────────┼──────────────────┤
│  No index (seq)  │  0 MB        │  4,200 ms      │  N/A             │
│  B+Tree          │  78 MB       │  890 ms        │  12 sec          │
│  Partial B+Tree  │  8 MB        │  85 ms         │  1.2 sec         │
│  (WHERE type=    │              │                │                  │
│   'click')       │              │                │                  │
└──────────────────┴──────────────┴────────────────┴──────────────────┘

Query: SELECT * FROM events WHERE payload @> '{"source":"mobile"}'

┌──────────────────┬──────────────┬────────────────┬──────────────────┐
│  Index Type       │  Index Size  │  Query Time    │  Build Time      │
├──────────────────┼──────────────┼────────────────┼──────────────────┤
│  No index (seq)  │  0 MB        │  12,000 ms     │  N/A             │
│  GIN (default)   │  520 MB      │  15 ms         │  95 sec          │
│  GIN (path_ops)  │  180 MB      │  12 ms         │  45 sec          │
└──────────────────┴──────────────┴────────────────┴──────────────────┘

Key takeaways:
  - BRIN is 4000x smaller than B+Tree with only 2-3x slower queries
  - Partial indexes dramatically reduce size for selective predicates
  - GIN jsonb_path_ops is 3x smaller than default GIN operator class
  - Build time matters for large tables (95 sec for GIN!)
```

### Final Index Design Checklist

```
Before adding an index, ask these questions:

  ┌─────────────────────────────────────────────────────────────────┐
  │  1. Is there actually a slow query that needs this index?       │
  │     → Don't add indexes speculatively                          │
  │                                                                  │
  │  2. Does an existing index already cover this query?            │
  │     → Check leftmost prefix coverage                           │
  │                                                                  │
  │  3. How selective is the predicate?                              │
  │     → If it returns > 15% of rows, seq scan may be faster      │
  │                                                                  │
  │  4. Can a partial index reduce size?                             │
  │     → WHERE active = true, WHERE deleted_at IS NULL             │
  │                                                                  │
  │  5. Can INCLUDE columns enable index-only scans?                │
  │     → Check EXPLAIN for "Heap Fetches"                          │
  │                                                                  │
  │  6. What is the write amplification cost?                       │
  │     → Each index adds O(log N) to every INSERT                  │
  │                                                                  │
  │  7. Is the data naturally ordered?                               │
  │     → Consider BRIN instead of B+Tree (1000x smaller)           │
  │                                                                  │
  │  8. How will this index be maintained?                           │
  │     → Plan for VACUUM, REINDEX, bloat monitoring                │
  │                                                                  │
  │  9. Can you CREATE INDEX CONCURRENTLY?                           │
  │     → Always prefer concurrent creation in production           │
  │                                                                  │
  │  10. Set up monitoring for unused indexes                       │
  │     → Review pg_stat_user_indexes monthly                       │
  └─────────────────────────────────────────────────────────────────┘
```

---

## Appendix: Quick Reference

### Complexity Comparison Table

```
┌────────────────────┬────────────┬────────────┬────────────┬──────────────┐
│  Operation          │  B+Tree    │  Hash      │  Skip List │  Bloom Filter│
├────────────────────┼────────────┼────────────┼────────────┼──────────────┤
│  Point lookup       │  O(log N)  │  O(1) avg  │  O(log N)  │  O(k)        │
│  Range scan         │  O(log N+K)│  N/A       │  O(log N+K)│  N/A         │
│  Insert             │  O(log N)  │  O(1) avg  │  O(log N)  │  O(k)        │
│  Delete             │  O(log N)  │  O(1) avg  │  O(log N)  │  N/A*        │
│  Min/Max            │  O(log N)  │  N/A       │  O(1)      │  N/A         │
│  Ordered iteration  │  O(N)      │  N/A       │  O(N)      │  N/A         │
│  Space              │  O(N)      │  O(N)      │  O(N)      │  O(N) bits   │
└────────────────────┴────────────┴────────────┴────────────┴──────────────┘
  * Counting Bloom filter supports delete, standard does not
  K = number of results in range
  k = number of hash functions in Bloom filter
```

### PostgreSQL Index Types at a Glance

```
┌──────────┬──────────────────────────┬──────────────────────────────────────┐
│  Type     │  Operators Supported      │  Best For                            │
├──────────┼──────────────────────────┼──────────────────────────────────────┤
│  B-Tree  │  <  <=  =  >=  >  BETWEEN│  General purpose, most queries       │
│          │  IN  IS NULL  IS NOT NULL │                                      │
├──────────┼──────────────────────────┼──────────────────────────────────────┤
│  Hash    │  =                        │  Exact equality only                 │
├──────────┼──────────────────────────┼──────────────────────────────────────┤
│  GIN     │  @>  <@  &&  @@  @?      │  Arrays, JSONB, full-text, trigrams  │
├──────────┼──────────────────────────┼──────────────────────────────────────┤
│  GiST    │  <<  >>  &&  @>  <@      │  Geometry, ranges, full-text,        │
│          │  ~=  <->                  │  nearest-neighbor, exclusion         │
├──────────┼──────────────────────────┼──────────────────────────────────────┤
│  SP-GiST │  <<  >>  @>  <@  =       │  IP addresses, text prefix,          │
│          │                           │  points (k-d tree, quadtree)         │
├──────────┼──────────────────────────┼──────────────────────────────────────┤
│  BRIN    │  <  <=  =  >=  >         │  Large, naturally ordered tables      │
│          │                           │  (timestamps, serial IDs)            │
└──────────┴──────────────────────────┴──────────────────────────────────────┘
```
