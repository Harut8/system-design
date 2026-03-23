# OLTP Databases: A Staff-Engineer Deep Dive

A comprehensive guide to Online Transaction Processing databases -- architecture, internals, performance tuning, and scaling strategies for PostgreSQL, MySQL/InnoDB, Oracle, SQL Server, NewSQL, and embedded databases.

---

## Table of Contents

1. [What Makes a Database OLTP](#1-what-makes-a-database-oltp)
2. [PostgreSQL Internals Deep Dive](#2-postgresql-internals-deep-dive)
3. [MySQL / InnoDB Internals Deep Dive](#3-mysql--innodb-internals-deep-dive)
4. [Oracle Database Internals](#4-oracle-database-internals)
5. [SQL Server Internals](#5-sql-server-internals)
6. [NewSQL Databases](#6-newsql-databases)
7. [Embedded OLTP Databases](#7-embedded-oltp-databases)
8. [OLTP Performance Tuning](#8-oltp-performance-tuning)
9. [OLTP Scaling Strategies](#9-oltp-scaling-strategies)

---

## 1. What Makes a Database OLTP

### Core Characteristics

OLTP databases are optimized for **transactional workloads** -- short-lived operations that read or modify small amounts of data with strict correctness guarantees. The defining traits:

| Characteristic | Description |
|---|---|
| **Short transactions** | Typically < 100ms; touch a handful of rows |
| **Low latency** | Single-digit millisecond p99 is the target |
| **High concurrency** | Thousands of concurrent sessions doing independent work |
| **Row-oriented access** | Queries fetch entire rows, not columns across many rows |
| **ACID compliance** | Atomicity, Consistency, Isolation, Durability are non-negotiable |
| **Write-heavy or mixed** | Frequent INSERT, UPDATE, DELETE alongside reads |

### OLTP Workload Patterns

```
Typical OLTP Operations
========================

1. Point Lookup (by primary key):
   SELECT * FROM orders WHERE order_id = 98432;

2. Small Range Scan (bounded):
   SELECT * FROM order_items WHERE order_id = 98432;

3. Single-Row Insert:
   INSERT INTO orders (customer_id, total, status)
   VALUES (1042, 59.99, 'pending');

4. Single-Row Update:
   UPDATE orders SET status = 'shipped' WHERE order_id = 98432;

5. Lookup by Secondary Index:
   SELECT * FROM users WHERE email = 'alice@example.com';

6. Short Join (few rows each side):
   SELECT o.order_id, c.name
   FROM orders o JOIN customers c ON o.customer_id = c.id
   WHERE o.order_id = 98432;
```

### OLTP vs OLAP Comparison

| Dimension | OLTP | OLAP |
|---|---|---|
| **Primary operation** | INSERT, UPDATE, DELETE, point SELECT | Aggregations, scans, GROUP BY |
| **Query complexity** | Simple, predefined | Complex, ad-hoc |
| **Rows per query** | 1 -- 100 | Millions -- billions |
| **Columns per query** | All columns of a row | Few columns across many rows |
| **Concurrency** | Thousands of users | Tens of analysts |
| **Latency target** | < 10ms | Seconds to minutes acceptable |
| **Data freshness** | Real-time | Near-real-time or batch |
| **Storage layout** | Row-oriented | Column-oriented |
| **Indexing** | B-tree, hash indexes | Bitmap, zone maps, min/max |
| **Schema** | Normalized (3NF+) | Star/snowflake schema |
| **Data volume** | GBs -- low TBs | TBs -- PBs |
| **Example systems** | PostgreSQL, MySQL, Oracle | ClickHouse, Snowflake, BigQuery |

### Why Row-Oriented Storage Is Ideal for OLTP

```
ROW-ORIENTED STORAGE (OLTP)
============================

Page N (8KB / 16KB):
+-------+-------+-------+-------+-------+
| Row 1 | Row 2 | Row 3 | Row 4 | Row 5 |
+-------+-------+-------+-------+-------+
  All columns of each row stored together

  Point lookup:  SELECT * FROM users WHERE id = 3;
  --> Read 1 page, get ALL columns of row 3   [FAST]

  Insert:        INSERT INTO users VALUES (...);
  --> Append to 1 page                        [FAST]


COLUMN-ORIENTED STORAGE (OLAP)
===============================

Column "name":   | Alice | Bob | Carol | Dave | Eve |
Column "email":  | a@..  | b@..| c@..  | d@.. | e@..|
Column "age":    | 30    | 25  | 35    | 28   | 32  |

  Aggregation:   SELECT AVG(age) FROM users;
  --> Read only "age" column, skip rest        [FAST]

  Point lookup:  SELECT * FROM users WHERE id = 3;
  --> Must read from EVERY column file         [SLOW]

  Insert:        INSERT INTO users VALUES (...);
  --> Must write to EVERY column file          [SLOW]
```

**Key insight**: OLTP queries need all columns of few rows. Row storage places all columns of a row in the same disk page, so a single I/O fetches everything. Column storage scatters a single row across many files.

---

## 2. PostgreSQL Internals Deep Dive

### Architecture

PostgreSQL uses a **multi-process architecture**. Each client connection gets its own OS process (forked from the postmaster). This is the opposite of MySQL's thread-per-connection model.

```
PostgreSQL Process Architecture
================================

                     +-------------------+
                     |    Client Apps    |
                     +--------+----------+
                              |
                              | TCP/Unix Socket
                              |
                     +--------v----------+
                     |    Postmaster     |  <-- Main daemon (PID 1 of cluster)
                     |   (listener)      |      Forks backends on connect
                     +--------+----------+
                              |
              +---------------+---------------+
              |               |               |
      +-------v----+  +------v-----+  +------v-----+
      |  Backend   |  |  Backend   |  |  Backend   |
      | Process 1  |  | Process 2  |  | Process N  |
      | (client 1) |  | (client 2) |  | (client N) |
      +------+-----+  +------+-----+  +------+-----+
              |               |               |
              +-------+-------+-------+-------+
                      |               |
              +-------v-------+  +----v-----------+
              | Shared Memory |  | Background     |
              |               |  | Workers        |
              | shared_buffers|  |                 |
              | WAL buffers   |  | - WAL writer   |
              | CLOG buffers  |  | - Checkpointer |
              | Lock tables   |  | - Bgwriter     |
              | Proc array    |  | - Autovacuum   |
              +---------------+  | - Stats coll.  |
                                 | - Archiver     |
                                 +-----------------+
```

#### Key Processes

| Process | Role |
|---|---|
| **Postmaster** | Listens for connections, forks backend processes, restarts crashed backends |
| **Backend** | One per client connection; parses SQL, plans, executes queries |
| **WAL Writer** | Periodically flushes WAL buffers to disk (between commits) |
| **Checkpointer** | Writes dirty pages to disk, creates checkpoint records in WAL |
| **Background Writer** | Evicts dirty pages from shared_buffers to reduce checkpoint spikes |
| **Autovacuum Launcher** | Spawns autovacuum workers to clean dead tuples |
| **Stats Collector** | Aggregates table/index usage statistics for the planner |
| **Archiver** | Copies completed WAL segments to archive storage |
| **Logical Replication Worker** | Applies logical changes from publisher to subscriber |

#### Shared Memory Layout

```
Shared Memory (shared_buffers + overhead)
==========================================

+----------------------------------------------------------+
|                    shared_buffers                         |
|  +--------+--------+--------+--------+--------+----+    |
|  | Page 0 | Page 1 | Page 2 | Page 3 | Page 4 |... |    |
|  | (8KB)  | (8KB)  | (8KB)  | (8KB)  | (8KB)  |    |    |
|  +--------+--------+--------+--------+--------+----+    |
|                                                          |
|  Buffer Descriptors (tag, flags, refcount, usage_count)  |
|  Buffer Hash Table (maps RelFileNode+BlockNum -> bufID)  |
+----------------------------------------------------------+
|  WAL Buffers (wal_buffers, default 1/32 of shared_buffers)|
+----------------------------------------------------------+
|  CLOG Buffers (pg_xact -- transaction commit status)      |
+----------------------------------------------------------+
|  Subtransaction Buffers (pg_subtrans)                     |
+----------------------------------------------------------+
|  Lock Tables (heavyweight locks, lightweight locks)       |
+----------------------------------------------------------+
|  Proc Array (per-backend PGPROC structs with xid, pid)   |
+----------------------------------------------------------+
|  Predicate Lock Manager (for Serializable isolation)      |
+----------------------------------------------------------+
```

**shared_buffers** is the page cache managed by PostgreSQL itself (separate from the OS page cache). Typical sizing: 25% of RAM, up to ~8GB on dedicated servers (beyond that, the OS cache handles the rest).

#### System Catalogs

System catalogs are regular PostgreSQL tables that store metadata:

| Catalog | Purpose |
|---|---|
| `pg_class` | All relations (tables, indexes, sequences, views, etc.) |
| `pg_attribute` | All columns of all relations |
| `pg_index` | Index metadata (which columns, uniqueness, etc.) |
| `pg_type` | Data type definitions |
| `pg_namespace` | Schemas |
| `pg_proc` | Functions and procedures |
| `pg_statistic` | Column statistics for the query planner |
| `pg_depend` | Dependency tracking between objects |

### Storage Engine

#### Heap Tables

PostgreSQL stores table data in **heap files** -- unordered collections of pages. There is no clustered index by default (unlike InnoDB).

```
Heap File Structure
====================

Table "users" on disk:
  base/<db_oid>/<relfilenode>         (main fork -- data)
  base/<db_oid>/<relfilenode>_fsm     (free space map)
  base/<db_oid>/<relfilenode>_vm      (visibility map)

  If table > 1GB:
  base/<db_oid>/<relfilenode>.1       (second segment)
  base/<db_oid>/<relfilenode>.2       (third segment)
  ...
```

#### Page Layout (8KB)

```
PostgreSQL 8KB Page Layout
===========================

Offset  Size    Content
------  ------  ------------------------------------------
0       24B     PageHeaderData
                  pd_lsn (8B)       -- LSN of last WAL affecting page
                  pd_checksum (2B)  -- page checksum
                  pd_flags (2B)     -- page flags
                  pd_lower (2B)     -- offset to end of line pointers
                  pd_upper (2B)     -- offset to start of free space
                  pd_special (2B)   -- offset to start of special space
                  pd_pagesize_version (2B)
                  pd_prune_xid (4B) -- oldest unpruned xid

24      4B*N    ItemIdData array (line pointers)
                  Each entry: (offset, length, flags) = 4 bytes
                  Points to actual tuple within the page

                 ...free space grows from both ends...

varies  varies  Heap Tuples (stored from bottom of page upward)
                  Each tuple has a HeapTupleHeaderData:
                    t_xmin (4B)    -- inserting transaction ID
                    t_xmax (4B)    -- deleting/locking transaction ID
                    t_cid (4B)     -- command ID within transaction
                    t_ctid (6B)    -- current TID (self, or forwarding)
                    t_infomask (2B)  -- status bits
                    t_infomask2 (2B) -- more status bits
                    t_hoff (1B)    -- offset to user data
                    null bitmap    -- which columns are NULL
                    user data      -- actual column values

+------+------+------+------+-------------------+------+------+------+
|Header| LP 1 | LP 2 | LP 3 |    Free Space     |Tuple3|Tuple2|Tuple1|
+------+------+------+------+-------------------+------+------+------+
0      24                    pd_lower    pd_upper              8192
         Line Pointers --->                  <--- Tuples grow backward
```

#### TOAST (The Oversized-Attribute Storage Technique)

When a row exceeds approximately 2KB, PostgreSQL uses TOAST:
- **PLAIN**: No compression, no out-of-line storage
- **EXTENDED**: Compress first, then move out-of-line if still too big (default for varlena types)
- **EXTERNAL**: Move out-of-line without compression
- **MAIN**: Compress but avoid out-of-line storage if possible

TOAST data is stored in a separate heap table (the TOAST table), with each chunk stored as a row indexed by chunk_id and chunk_seq.

#### Free Space Map (FSM) and Visibility Map (VM)

```
Free Space Map (_fsm fork)
===========================
- One byte per heap page recording approximate free space
- Organized as a tree of pages for efficient search
- Used by INSERT to find pages with enough room
- Updated by VACUUM

Visibility Map (_vm fork)
==========================
- Two bits per heap page:
  Bit 0: "all-visible" -- all tuples visible to all current transactions
  Bit 1: "all-frozen"  -- all tuples frozen (safe from wraparound)
- Index-only scans skip heap fetch for all-visible pages
- VACUUM skips all-frozen pages entirely
```

#### HOT Updates and Fillfactor

**Heap-Only Tuples (HOT)** is a critical optimization. When an UPDATE does not modify any indexed column, PostgreSQL can store the new tuple version on the *same page* as the old one, avoiding index updates entirely.

```
HOT Update Mechanism
=====================

Before UPDATE (fillfactor = 90, page has 10% reserved):
+------+------+------+------+---------+--------+--------+--------+
|Header| LP 1 | LP 2 | LP 3 |Reserved |Tuple 3 |Tuple 2 |Tuple 1 |
+------+------+------+------+---------+--------+--------+--------+

After UPDATE of Tuple 2 (no indexed column changed):
+------+------+------+------+------+--------+--------+--------+--------+
|Header| LP 1 | LP 2 | LP 3 | LP 4 |Tuple 2'|Tuple 3 |Tuple 2 |Tuple 1 |
+------+------+------+------+------+--------+--------+--------+--------+
                 |                     ^
                 | LP 2 now points to  |
                 +--- LP 4 (redirect) -+

  - Old Tuple 2: t_ctid points to (same_page, LP 4)
  - New Tuple 2': stored on same page, no index entry needed
  - Index still points to LP 2, which redirects to LP 4
  - Massive performance win: avoids index bloat
```

**fillfactor** controls how much of each page to fill during INSERT. Default is 100 (fill completely). For tables with frequent HOT-eligible updates, set to 70-90 to reserve space.

```sql
CREATE TABLE orders (
    id       bigint PRIMARY KEY,
    status   text,
    updated  timestamptz
) WITH (fillfactor = 80);

-- 20% of each page reserved for HOT updates
-- status/updated changes don't touch the PK index
```

### MVCC Implementation

PostgreSQL implements MVCC (Multi-Version Concurrency Control) by keeping old row versions in the main heap table. This is fundamentally different from MySQL/InnoDB, which stores old versions in a separate undo log.

#### Tuple Header Fields for MVCC

| Field | Size | Purpose |
|---|---|---|
| `t_xmin` | 4B | Transaction ID that **inserted** this tuple |
| `t_xmax` | 4B | Transaction ID that **deleted/updated** this tuple (0 if live) |
| `t_cid` | 4B | Command ID within the transaction (for statement-level visibility) |
| `t_ctid` | 6B | Tuple ID of the **next version** (points to self if latest) |
| `t_infomask` | 2B | Status bits: committed, aborted, locked, HOT, etc. |

#### Visibility Rules

A tuple is visible to a transaction's snapshot if:

```
VISIBILITY DECISION TREE
==========================

Is t_xmin committed?
  |
  +-- NO (xmin aborted or still in progress) --> INVISIBLE
  |
  +-- YES --> Is t_xmax set (non-zero)?
                |
                +-- NO --> VISIBLE (live tuple, never deleted)
                |
                +-- YES --> Is t_xmax committed?
                              |
                              +-- NO --> VISIBLE (delete not committed)
                              |
                              +-- YES --> Was t_xmax committed BEFORE
                                          our snapshot was taken?
                                            |
                                            +-- NO  --> VISIBLE
                                            +-- YES --> INVISIBLE (deleted)
```

A snapshot contains:
- `xmin`: lowest still-running xid at snapshot time -- all xids below this are known committed or aborted
- `xmax`: first unassigned xid at snapshot time
- `xip_list`: list of in-progress xids between xmin and xmax

#### CLOG (Commit Log) / pg_xact

The CLOG (stored in `pg_xact/` directory) records the commit status of every transaction:

```
CLOG Structure
===============

Two bits per transaction ID:
  00 = IN_PROGRESS
  01 = COMMITTED
  10 = ABORTED
  11 = SUB_COMMITTED (subtransaction committed, parent still running)

Storage:
  Each 8KB page holds status for 4 * 8192 = 32,768 transactions
  Pages are cached in shared memory CLOG buffers
  Older pages spill to pg_xact/ on disk

Lookup path:
  page_number = xid / 32768
  byte_offset = (xid % 32768) / 4
  bit_offset  = (xid % 4) * 2
```

#### Hint Bits Optimization

Looking up the CLOG for every visibility check would be expensive. PostgreSQL uses **hint bits** in the tuple header (t_infomask) to cache the commit status:

```
Hint Bits in t_infomask
========================

HEAP_XMIN_COMMITTED  (0x0100)  -- inserting xact known committed
HEAP_XMIN_INVALID    (0x0200)  -- inserting xact known aborted
HEAP_XMAX_COMMITTED  (0x0400)  -- deleting xact known committed
HEAP_XMAX_INVALID    (0x0800)  -- deleting xact known aborted

Flow:
  1. First access: hint bits not set
     --> Look up CLOG to determine xmin/xmax status
     --> SET hint bits in tuple header (dirty the page)
  2. Subsequent accesses: hint bits set
     --> Skip CLOG lookup entirely (fast path)

Side effect: Read-only queries can dirty pages (by setting hint bits).
This is why PostgreSQL generates WAL even for SELECT-heavy workloads.
(Mitigated by enabling data checksums, which batches hint bit WAL writes.)
```

### Vacuum

#### Why Vacuum Exists

PostgreSQL's MVCC stores old tuple versions in the heap. Unlike InnoDB (which uses a separate undo log that the purge thread cleans), PostgreSQL needs VACUUM to:

1. **Remove dead tuples** -- rows no longer visible to any transaction
2. **Reclaim space** -- mark free space in FSM so INSERTs can reuse it
3. **Update visibility map** -- mark pages as all-visible for index-only scans
4. **Freeze old tuples** -- prevent transaction ID wraparound

```
VACUUM Operation
=================

Before VACUUM:
+------+--------+--------+--------+--------+--------+
|Header| Live 1 | Dead 2 | Live 3 | Dead 4 | Live 5 |
+------+--------+--------+--------+--------+--------+
                    ^                  ^
              Deleted by a        Updated; old
              committed txn       version no longer
                                  visible to anyone

After VACUUM:
+------+--------+--------+--------+--------+--------+
|Header| Live 1 | [free] | Live 3 | [free] | Live 5 |
+------+--------+--------+--------+--------+--------+
                    ^                  ^
              Space returned      Space returned
              to FSM              to FSM
              (reusable by        (reusable by
               future INSERTs)     future INSERTs)

Note: Pages are NOT returned to the OS. Table file size does not shrink.
```

#### Regular VACUUM vs VACUUM FULL

| Aspect | VACUUM | VACUUM FULL |
|---|---|---|
| **Locking** | Does NOT block reads or writes (concurrent) | Acquires ACCESS EXCLUSIVE lock (blocks everything) |
| **Space reclaim** | Marks space as reusable within the file | Rewrites entire table to a new file, returns space to OS |
| **Speed** | Fast, incremental | Slow, rewrites everything |
| **When to use** | Routine maintenance (autovacuum) | Table is severely bloated (rarely needed) |
| **Index handling** | Scans indexes to remove dead entries | Rebuilds indexes from scratch |

#### Autovacuum Tuning

Autovacuum triggers when dead tuples exceed a threshold:

```
Autovacuum Trigger Formula
===========================

Trigger when:
  dead_tuples > autovacuum_vacuum_threshold
                + autovacuum_vacuum_scale_factor * n_live_tuples

Defaults:
  autovacuum_vacuum_threshold     = 50    (minimum dead tuples)
  autovacuum_vacuum_scale_factor  = 0.2   (20% of live tuples)

Example: Table with 1,000,000 rows
  Trigger at: 50 + 0.2 * 1,000,000 = 200,050 dead tuples
  --> Must accumulate 200K dead rows before vacuum kicks in!

For large tables, lower the scale factor:
  ALTER TABLE hot_table SET (autovacuum_vacuum_scale_factor = 0.01);
  --> Now triggers at 50 + 0.01 * 1,000,000 = 10,050 dead tuples

Key autovacuum parameters:
  autovacuum_max_workers          = 3     (parallel vacuum workers)
  autovacuum_vacuum_cost_delay    = 2ms   (throttle to reduce I/O impact)
  autovacuum_vacuum_cost_limit    = 200   (cost budget per delay cycle)
  autovacuum_naptime              = 1min  (how often launcher checks)
```

#### Transaction ID Wraparound and Anti-Wraparound Vacuum

PostgreSQL transaction IDs are 32-bit unsigned integers (~4.2 billion values). They are compared using modular arithmetic: the "past" is the 2 billion xids before the current one, and the "future" is the 2 billion after.

```
Transaction ID Space (Circular)
================================

           Current XID
               |
               v
   ... -------[X]------- ...
   |                       |
   |  ~2 billion xids      |  ~2 billion xids
   |  in the "past"        |  in the "future"
   |  (visible)            |  (not yet assigned)
   |                       |
   +----------+------------+
              |
         Wraparound boundary

Problem: If a tuple has xmin = 100 and we reach xid = 2,147,483,748,
         then xmin 100 is now in the "future" -- the tuple becomes INVISIBLE.
         Data effectively vanishes.

Solution: FREEZE old tuples
  - Replace xmin with FrozenTransactionId (= 2)
  - Frozen tuples are ALWAYS visible (special-cased)
  - Autovacuum triggers anti-wraparound when oldest unfrozen xid
    approaches the danger zone (default: 200 million xids from wrap)
```

**Warning**: If autovacuum cannot keep up (e.g., long-running transactions hold back the horizon, or vacuum is blocked), PostgreSQL will eventually **shut down** to prevent data loss, refusing all new transactions with:
```
ERROR: database is not accepting commands to avoid wraparound data loss
```

### Query Processing Pipeline

```
PostgreSQL Query Processing
=============================

SQL Text
   |
   v
+----------+     Lexer + Grammar (gram.y)
|  Parser  | --> Produces raw parse tree
+----------+     No semantic validation yet
   |
   v
+------------+   Resolves table/column names against catalogs
|  Analyzer  |   Type-checks expressions, resolves functions
| (Semantic  |   Produces Query tree (internal representation)
|  Analysis) |
+------------+
   |
   v
+------------+   Applies rewrite rules (views are implemented as rules)
|  Rewriter  |   Expands view references
+------------+   RLS (Row-Level Security) policies applied here
   |
   v
+------------+   Generates candidate plans, estimates costs
|  Planner / |   Chooses joins, scan methods, sort strategies
| Optimizer  |   Uses pg_statistic for cardinality estimates
+------------+   Produces Plan tree
   |
   v
+------------+   Iterates through Plan tree (Volcano model)
|  Executor  |   Each node: Init -> GetNext -> GetNext -> ... -> End
+------------+   Produces result rows to client
```

#### Cost Model Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `seq_page_cost` | 1.0 | Cost of reading one page sequentially |
| `random_page_cost` | 4.0 | Cost of reading one page randomly (set to 1.1 for SSD) |
| `cpu_tuple_cost` | 0.01 | Cost of processing one tuple |
| `cpu_index_tuple_cost` | 0.005 | Cost of processing one index entry |
| `cpu_operator_cost` | 0.0025 | Cost of executing one operator/function |
| `effective_cache_size` | 4GB | Planner's estimate of available cache (OS + shared_buffers) |
| `work_mem` | 4MB | Memory for sorts, hash tables (per operation, not per query) |

#### Join Strategies

| Strategy | Algorithm | Best When |
|---|---|---|
| **Nested Loop** | For each outer row, scan inner | Small outer, indexed inner; or very small tables |
| **Hash Join** | Build hash table on inner, probe with outer | Medium-to-large equi-joins; inner fits in `work_mem` |
| **Merge Join** | Sort both sides, merge | Both sides pre-sorted (e.g., index scan); large joins |

```sql
-- Force the planner to show its work:
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT o.id, c.name
FROM orders o
JOIN customers c ON o.customer_id = c.id
WHERE o.created_at > '2025-01-01';

-- Example output:
-- Hash Join  (cost=45.00..312.00 rows=1200 width=36)
--            (actual time=0.5..3.2 rows=1150 loops=1)
--   Hash Cond: (o.customer_id = c.id)
--   Buffers: shared hit=89
--   ->  Bitmap Heap Scan on orders o  (cost=12.00..267.00 rows=1200 width=12)
--         Filter: (created_at > '2025-01-01')
--         Buffers: shared hit=45
--   ->  Hash  (cost=22.00..22.00 rows=800 width=28)
--         Buckets: 1024  Memory Usage: 52kB
--         ->  Seq Scan on customers c  (cost=0..22 rows=800 width=28)
--               Buffers: shared hit=44
```

### Replication

#### Streaming Replication (Physical)

```
Physical Streaming Replication
===============================

+----------------+           WAL Stream           +----------------+
|    Primary     | ------- (continuous) ---------> |    Replica      |
|                |                                 |                 |
| Generates WAL  |    walreceiver <-- walsender    | Replays WAL     |
| from all       |                                 | byte-for-byte   |
| transactions   |    TCP connection               | (exact copy)    |
+----------------+                                 +----------------+

Configuration (primary - postgresql.conf):
  wal_level = replica              -- minimum for streaming replication
  max_wal_senders = 10             -- max concurrent replicas
  wal_keep_size = 1GB              -- WAL to retain for slow replicas

Configuration (replica - standby.signal + postgresql.conf):
  primary_conninfo = 'host=primary port=5432 user=replicator'
  hot_standby = on                 -- allow read queries on replica
```

#### Logical Replication

```
Logical Replication
====================

+----------------+      Logical changes       +----------------+
|   Publisher    | ---- (decoded from WAL) --> |   Subscriber   |
|                |                             |                |
| wal_level =    |   INSERT INTO t VALUES(...) | Applies as SQL |
|   logical      |   UPDATE t SET x=1 WHERE..  | Can have       |
|                |   DELETE FROM t WHERE ...    | different      |
| Uses output    |                             | schema, indexes|
| plugin to      |   (No DDL replication)      | even different |
| decode WAL     |                             | PG version     |
+----------------+                             +----------------+

Key differences from physical:
  - Table-level granularity (replicate specific tables)
  - Cross-version compatible
  - Can replicate to different schema
  - Does NOT replicate DDL, sequences, or large objects
  - Higher overhead (decoding + re-executing)
```

#### Synchronous vs Asynchronous Replication

| Mode | Behavior | Trade-off |
|---|---|---|
| **Async** (default) | Primary commits immediately; replica may lag | Fast writes, potential data loss on failover |
| **Sync** (`synchronous_commit = on`) | Primary waits for replica to write WAL to disk | No data loss, higher write latency |
| **Remote write** (`synchronous_commit = remote_write`) | Primary waits for replica to receive WAL (not fsync) | Compromise: lower latency than sync, better than async |
| **Remote apply** (`synchronous_commit = remote_apply`) | Primary waits for replica to replay WAL | Read-your-writes on replica, highest latency |

---

## 3. MySQL / InnoDB Internals Deep Dive

### Architecture

MySQL uses a **thread-per-connection** model (in contrast to PostgreSQL's process-per-connection). It also has a pluggable storage engine architecture, though InnoDB has been the default since MySQL 5.5.

```
MySQL Architecture
===================

                  +-------------------+
                  |   Client Apps     |
                  +--------+----------+
                           |
                  +--------v----------+
                  | Connection Layer  |
                  | (thread per conn) |
                  +--------+----------+
                           |
                  +--------v----------+
                  |    SQL Layer      |
                  |                   |
                  | Parser            |
                  | Optimizer         |
                  | Executor          |
                  | Query Cache (rem) |
                  +--------+----------+
                           |
              +------------+------------+
              |            |            |
       +------v---+  +----v-----+ +----v-----+
       |  InnoDB  |  |  MyISAM  | | Memory   |
       | (default)|  |(legacy)  | |(temp tbl)|
       +----------+  +----------+ +----------+
       |Buffer Pool|
       |Redo Log   |
       |Undo Log   |
       |Doublewrite|
       +-----------+

Key difference from PostgreSQL:
  - MySQL: Threads in one process, shared address space
  - PostgreSQL: Separate OS processes, shared via mmap/shmem

Thread model advantages:
  + Lower overhead per connection (~256KB stack vs ~10MB per process)
  + Faster context switching
  + Shared memory without IPC complexity

Thread model disadvantages:
  - One crashed thread can crash the entire server
  - Global locks (e.g., LOCK_open) can become bottlenecks
  - Harder to isolate misbehaving queries
```

#### Plugin Storage Engine Architecture

```
Storage Engine API (Handler Interface)
========================================

SQL Layer calls generic handler methods:
  handler::ha_open()
  handler::write_row()
  handler::update_row()
  handler::delete_row()
  handler::rnd_next()        -- table scan
  handler::index_read()      -- index lookup
  handler::index_next()      -- index range scan

Each engine implements these differently:
  InnoDB: B+tree clustered index, MVCC, crash-safe
  MyISAM: Heap + separate B-tree index files, table-level locking
  Memory: In-memory hash/B-tree, no persistence
  NDB: Distributed, shared-nothing (MySQL Cluster)
```

### InnoDB Storage

#### Clustered Index (Primary Key IS the Table)

This is the most fundamental difference from PostgreSQL: in InnoDB, **the primary key index IS the table data**. Rows are stored in primary key order within a B+tree.

```
InnoDB Clustered Index
=======================

B+Tree organized by Primary Key:

                    [Internal Node]
                   /       |       \
                  /        |        \
         [Leaf Page 1]  [Leaf Page 2]  [Leaf Page 3]
         PK: 1,2,3      PK: 4,5,6     PK: 7,8,9
         +full rows     +full rows     +full rows

Each leaf page (16KB default) contains:
  +------+-------------------+
  | PK=1 | col1, col2, col3  |  <-- Full row data
  +------+-------------------+
  | PK=2 | col1, col2, col3  |
  +------+-------------------+
  | PK=3 | col1, col2, col3  |
  +------+-------------------+

Implications:
  1. Range scans on PK are extremely fast (data is physically sequential)
  2. PK lookup = one B+tree traversal to get full row
  3. Random PK (UUID) causes massive page splits and fragmentation
  4. AUTO_INCREMENT PK is optimal: always appends to rightmost leaf
```

#### Secondary Indexes

```
InnoDB Secondary Index
=======================

Secondary index on (email):

                    [Internal Node]
                   /       |       \
                  /        |        \
         [Leaf Page]    [Leaf Page]    [Leaf Page]
         email: a@..    email: d@..    email: m@..
         +PK value      +PK value      +PK value

Each leaf entry:
  +-------------+------+
  | email value | PK=5 |  <-- Stores PK, NOT row pointer!
  +-------------+------+

Lookup path for: SELECT * FROM users WHERE email = 'alice@example.com';
  1. Search secondary index (email)  --> Find PK = 42
  2. Search clustered index (PK=42)  --> Find full row   (DOUBLE LOOKUP)

This is why "covering indexes" matter even more in InnoDB:
  CREATE INDEX idx_email_name ON users(email, name);
  -- Now: SELECT name FROM users WHERE email = 'alice@example.com'
  --      can be answered from the secondary index alone (index-only scan)
```

#### Page Structure (16KB)

```
InnoDB 16KB Page Layout
========================

+--------------------------------------------------+
| FIL Header (38 bytes)                            |
|   space_id, page_no, prev_page, next_page,       |
|   LSN, page_type, flush_LSN                      |
+--------------------------------------------------+
| Page Header (56 bytes)                           |
|   n_dir_slots, heap_top, n_heap, free,           |
|   garbage, last_insert, direction, n_direction,  |
|   n_recs, max_trx_id, level, index_id            |
+--------------------------------------------------+
| Infimum Record (13 bytes)                        |
| Supremum Record (13 bytes)                       |
+--------------------------------------------------+
| User Records                                     |
|   (stored as singly-linked list in key order)    |
|   Each record has: header (5B) + field data      |
|   Header: info_bits, n_owned, order, record_type,|
|           next_record_offset                     |
+--------------------------------------------------+
| Free Space                                       |
+--------------------------------------------------+
| Page Directory (variable)                        |
|   Array of slots pointing to every 4-8th record  |
|   Used for binary search within page             |
+--------------------------------------------------+
| FIL Trailer (8 bytes)                            |
|   Checksum, LSN (for corruption detection)       |
+--------------------------------------------------+
```

#### Tablespace Types

| Type | Description |
|---|---|
| **System tablespace** (`ibdata1`) | Shared; contains undo logs (older versions), change buffer, doublewrite buffer |
| **File-per-table** (default since 5.6) | Each table gets its own `.ibd` file; easier management, `OPTIMIZE TABLE` reclaims space |
| **General tablespace** | User-created shared tablespace; group related tables together |
| **Undo tablespace** (since 8.0) | Dedicated files for undo logs; can be truncated to reclaim space |
| **Temporary tablespace** | For temporary tables and sort/join spills |

#### Change Buffer

```
InnoDB Change Buffer
=====================

Problem: Secondary index updates on non-unique indexes require
         random I/O to read the target leaf page.

Solution: Buffer the changes in memory + system tablespace.
          Merge later when the page is read for another reason.

Write path for secondary index update:
  1. Is the target page in buffer pool?
     +-- YES --> Modify directly in memory
     +-- NO  --> Record change in Change Buffer (skip disk read!)

  Later, the change is "merged" when:
    - The page is read by a subsequent query
    - Background merge thread processes it
    - Buffer becomes full

Only works for:
  - Non-unique secondary indexes (unique requires a read to check)
  - INSERT, UPDATE, DELETE operations

Benefit: Reduces random I/O dramatically for write-heavy workloads
         with many secondary indexes.
```

### InnoDB MVCC

#### Undo Log Version Chains

```
InnoDB MVCC: Undo Log Approach
================================

Current row in clustered index:
+------+--------+-----------+------------------+
| PK=5 | name=  | trx_id=   | roll_ptr ------->+----> Undo Log
|      | "Bob"  |  150      |                  |      Segment
+------+--------+-----------+------------------+
                                                       |
                                               +-------v--------+
                                               | Previous version|
                                               | name="Robert"   |
                                               | trx_id=120      |
                                               | roll_ptr ------->+
                                               +-----------------+ |
                                                                   |
                                                            +------v---------+
                                                            | Oldest version  |
                                                            | name="Rob"      |
                                                            | trx_id=80       |
                                                            | roll_ptr = NULL |
                                                            +-----------------+

Key difference from PostgreSQL:
  PostgreSQL: Old versions live in the HEAP (same table)
              --> Table bloats; needs VACUUM
  InnoDB:     Old versions live in UNDO LOG (separate space)
              --> Table stays compact; purge thread cleans undo
              --> No vacuum needed!
```

#### Read Views

When a transaction starts a consistent read (SELECT in REPEATABLE READ), InnoDB creates a **read view**:

```
Read View Contents
===================

  m_low_limit_id   = next xid to be assigned (upper bound)
  m_up_limit_id    = lowest active xid (lower bound)
  m_ids            = list of active (uncommitted) xids
  m_creator_trx_id = xid of the transaction that created this view

Visibility rule for a row with trx_id T:
  if T < m_up_limit_id        --> VISIBLE (committed before snapshot)
  if T >= m_low_limit_id      --> INVISIBLE (started after snapshot)
  if T in m_ids               --> INVISIBLE (was active at snapshot)
  if T == m_creator_trx_id    --> VISIBLE (our own changes)
  else                        --> VISIBLE (committed, not in active list)
```

#### Purge Thread

The purge thread is InnoDB's equivalent of PostgreSQL's VACUUM:

```
Purge Thread Operation
=======================

1. Find the oldest active read view across all transactions
2. Any undo log entry older than this view is "safe to purge"
3. Delete the undo log records
4. Remove delete-marked records from the clustered index
5. Remove delete-marked records from secondary indexes

Key tuning:
  innodb_purge_threads = 4     -- parallel purge threads (default: 4)
  innodb_max_purge_lag = 0     -- throttle DML if purge falls behind

If purge cannot keep up (long-running transactions hold the horizon):
  - Undo tablespace grows
  - History list length increases (SHOW ENGINE INNODB STATUS)
  - Queries slow down (longer version chains to traverse)
```

### InnoDB Logging

#### Redo Log (WAL Equivalent)

```
InnoDB Redo Log Architecture
==============================

+-------------------+     +-------------------+
|  redo log file 0  | --> |  redo log file 1  | --> (circular)
|  (ib_logfile0)    |     |  (ib_logfile1)    |
+-------------------+     +-------------------+
         ^                          |
         |                          |
         +------ wrap around -------+

Write path:
  1. Modify page in buffer pool
  2. Write redo log record to log buffer (in memory)
  3. On COMMIT: flush log buffer to redo log files (fsync)
  4. Page eventually flushed to tablespace by checkpoint

Key parameters:
  innodb_log_file_size   = 1GB     -- size of each redo log file
  innodb_log_files_in_group = 2    -- number of redo log files
  innodb_flush_log_at_trx_commit:
    = 1  (default) -- fsync on every commit (ACID-safe)
    = 0  -- flush to OS cache every second (fast, data loss risk)
    = 2  -- write to OS cache on commit, fsync every second

Since MySQL 8.0.30: redo log is dynamically sized, replacing
the fixed ib_logfile0/ib_logfile1 approach with #innodb_redo/
directory containing numbered files.
```

#### Doublewrite Buffer

```
Doublewrite Buffer: Partial Write Protection
==============================================

Problem: OS may crash mid-write of a 16KB InnoDB page.
         Result: page is partially old, partially new = CORRUPTED.
         Redo log cannot fix this (redo applies changes to a valid page).

Solution: Write pages twice.
  1. Write dirty pages to doublewrite buffer (sequential area on disk)
  2. fsync the doublewrite buffer
  3. Write pages to their actual locations in tablespace
  4. fsync the tablespace

Recovery:
  - If crash during step 3: page in tablespace is corrupt
  - InnoDB reads the intact copy from doublewrite buffer
  - Copies it to the tablespace, then applies redo log

Performance impact: ~5-10% overhead (sequential writes are fast)
Can be disabled on filesystems with atomic writes (ZFS, some SSDs):
  innodb_doublewrite = OFF
```

#### Binary Log (Binlog)

```
Binary Log vs Redo Log
========================

              Redo Log                    Binary Log
              --------                    ----------
Scope:        InnoDB only                 Server-level (all engines)
Content:      Physical page changes       Logical SQL statements or
                                          row-change events
Purpose:      Crash recovery              Replication + point-in-time
                                          recovery
Format:       Circular buffer (fixed)     Append-only numbered files
                                          (binlog.000001, .000002, ...)
Formats:      N/A                         STATEMENT, ROW, MIXED

Binlog formats:
  STATEMENT: logs SQL text (compact, but non-deterministic functions
             like NOW(), RAND() cause replica divergence)
  ROW:       logs before/after row images (safe, larger)
  MIXED:     STATEMENT by default, ROW for unsafe statements
```

### MySQL Replication

```
MySQL Replication Architecture
================================

+------------------+                    +------------------+
|     Source        |                    |     Replica      |
|                   |                    |                  |
| SQL Thread        |                    | I/O Thread       |
|     |             |                    |     |            |
|     v             |                    |     v            |
| Binlog Files  ----+----- network ------+-> Relay Log      |
| (binlog.00000N)   |                    | (relay-log.0000N)|
|                   |                    |     |            |
|                   |                    |     v            |
|                   |                    | SQL Thread       |
|                   |                    | (applies events) |
+------------------+                    +------------------+

GTID (Global Transaction Identifier):
  Format: source_uuid:transaction_id
  Example: 3E11FA47-71CA-11E1-9E33-C80AA9429562:42

  Benefits:
  - Unique across all servers
  - Easy failover: replica knows exactly which transactions it has
  - No need to track binlog file + position manually
```

#### Group Replication / InnoDB Cluster

```
MySQL Group Replication
========================

     +--------+      +--------+      +--------+
     |  Node  |<---->|  Node  |<---->|  Node  |
     |   A    |      |   B    |      |   C    |
     +--------+      +--------+      +--------+
          ^               ^               ^
          |               |               |
          +-------+-------+-------+-------+
                  |               |
            Paxos-based Group Communication

Modes:
  Single-Primary:  One node accepts writes, others are read-only.
                   Automatic failover if primary fails.
  Multi-Primary:   All nodes accept writes.
                   Conflict detection on commit (certification).
                   First committer wins.

InnoDB Cluster = Group Replication + MySQL Router + MySQL Shell
  Router: connection routing and failover
  Shell:  cluster administration and provisioning
```

#### Semi-Synchronous Replication

| Mode | Source behavior | Data safety |
|---|---|---|
| **Async** (default) | Commits immediately, sends binlog async | Potential data loss on crash |
| **Semi-sync** | Waits for at least 1 replica to acknowledge receiving the event | At most 1 transaction lost on crash |
| **Semi-sync (after_sync)** | Waits before committing to storage engine | No phantom reads on failover |

---

## 4. Oracle Database Internals

### Memory Architecture

```
Oracle Memory Architecture
============================

+---------------------------------------------------------------+
|                    SGA (System Global Area)                    |
|  Shared across all server processes                           |
|                                                               |
|  +------------------+  +------------------+  +--------------+ |
|  | Buffer Cache     |  | Shared Pool      |  | Redo Log     | |
|  | (data blocks)    |  | - Library Cache  |  | Buffer       | |
|  |                  |  |   (parsed SQL,   |  | (WAL before  | |
|  | LRU lists        |  |    exec plans)   |  |  writing to  | |
|  | Touch count      |  | - Dictionary     |  |  redo files) | |
|  | DBWR flushes     |  |   Cache (meta)   |  |              | |
|  +------------------+  | - Result Cache   |  | LGWR flushes | |
|                        +------------------+  +--------------+ |
|  +------------------+  +------------------+                   |
|  | Large Pool       |  | Java Pool        |                   |
|  | (RMAN, shared    |  | (JVM memory)     |                   |
|  |  server, parallel|  +------------------+                   |
|  |  exec buffers)   |  +------------------+                   |
|  +------------------+  | Streams Pool     |                   |
|                        | (replication)    |                   |
|                        +------------------+                   |
+---------------------------------------------------------------+

+---------------------------------------------------------------+
|                    PGA (Program Global Area)                   |
|  Private to each server process                               |
|                                                               |
|  +------------------+  +------------------+  +--------------+ |
|  | Sort Area        |  | Hash Area        |  | Session      | |
|  | (ORDER BY,       |  | (hash joins)     |  | Memory       | |
|  |  GROUP BY)       |  |                  |  | (variables,  | |
|  |                  |  |                  |  |  cursors)    | |
|  | Spills to temp   |  | Spills to temp   |  |              | |
|  | tablespace       |  | tablespace       |  |              | |
|  +------------------+  +------------------+  +--------------+ |
+---------------------------------------------------------------+
```

### Undo Tablespace and Flashback

Oracle uses undo segments (like InnoDB's undo log) for MVCC:

```
Oracle Undo and Flashback
===========================

Undo Tablespace:
  - Dedicated tablespace for undo data
  - Automatic undo management (AUM): Oracle manages segment allocation
  - undo_retention parameter: minimum time to keep undo (default 900s)
  - Enables consistent reads (MVCC) and rollback

Flashback Features (built on undo + redo):
  1. Flashback Query:
     SELECT * FROM employees AS OF TIMESTAMP (SYSTIMESTAMP - INTERVAL '1' HOUR);

  2. Flashback Table:
     FLASHBACK TABLE employees TO TIMESTAMP (SYSTIMESTAMP - INTERVAL '1' HOUR);

  3. Flashback Database (uses flashback logs, not just undo):
     FLASHBACK DATABASE TO TIMESTAMP (SYSTIMESTAMP - INTERVAL '1' DAY);

  4. Flashback Drop (recycle bin):
     FLASHBACK TABLE employees TO BEFORE DROP;

ORA-01555 "Snapshot Too Old":
  - Occurs when undo needed for consistent read has been overwritten
  - Equivalent to PostgreSQL's "snapshot too old" or serialization failure
  - Fix: increase undo_retention and undo tablespace size
```

### Redo Logs and Archive Logs

```
Oracle Redo Log Architecture
==============================

Online Redo Logs (circular groups):

  Group 1:  redo01a.log, redo01b.log  (multiplexed members)
  Group 2:  redo02a.log, redo02b.log
  Group 3:  redo03a.log, redo03b.log

  LGWR writes to current group.
  When full --> log switch to next group.
  If ARCHIVELOG mode: ARCn copies filled group to archive dest.

  +----------+     +----------+     +----------+
  | Group 1  |---->| Group 2  |---->| Group 3  |---+
  | CURRENT  |     | INACTIVE |     | INACTIVE |   |
  +----------+     +----------+     +----------+   |
       ^                                            |
       +-------- circular rotation -----------------+

Archive Log Mode:
  - Every redo log group is archived before reuse
  - Enables point-in-time recovery (PITR)
  - Required for Data Guard (standby databases)
  - Archive logs are numbered sequentially: arch_0001.arc, arch_0002.arc, ...
```

### RAC (Real Application Clusters)

```
Oracle RAC Architecture
========================

                    +-------------------+
                    | Shared Storage    |
                    | (ASM / SAN)       |
                    |                   |
                    | Datafiles         |
                    | Redo Logs         |
                    | Control Files     |
                    +--------+----------+
                             |
              +--------------+--------------+
              |                             |
     +--------v--------+          +--------v--------+
     |   Instance 1    |          |   Instance 2    |
     |   (Node 1)      |          |   (Node 2)      |
     |                  |          |                  |
     | Own SGA          |          | Own SGA          |
     | Own Redo Logs    |          | Own Redo Logs    |
     | Own Undo         |          | Own Undo         |
     +--------+---------+         +--------+---------+
              |                             |
              +---------- Cache -----------+
              |          Fusion            |
              | (inter-node block transfer)|
              |   via high-speed           |
              |   interconnect             |
              +----------------------------+

Cache Fusion:
  - When Instance 1 needs a block held by Instance 2
  - Block transferred directly via interconnect (no disk I/O)
  - Global Cache Service (GCS) coordinates ownership
  - Global Enqueue Service (GES) manages distributed locks

Scaling model: Shared-disk (all instances see same data)
  vs. Shared-nothing (each node owns a partition) in CockroachDB etc.
```

### Exadata Architecture

```
Oracle Exadata
===============

  +-----------------------------------+
  | Database Servers (compute nodes)  |
  | - Run Oracle instances            |
  | - SQL processing, PGA, SGA       |
  +----------------+------------------+
                   |
          InfiniBand / RoCE
          (high-bandwidth, low-latency)
                   |
  +----------------v------------------+
  | Storage Servers (Exadata cells)   |
  | - Smart Scan: offload filtering   |
  |   and projection to storage       |
  | - Storage indexes (min/max per    |
  |   MB region -- zone maps)        |
  | - Hybrid Columnar Compression    |
  | - Flash cache (L2 caching)       |
  +-----------------------------------+

Smart Scan: Instead of sending all data to compute nodes,
  the storage server applies WHERE predicates and column
  projection, returning only matching rows. Dramatically
  reduces data transfer for analytical queries.
```

---

## 5. SQL Server Internals

### Buffer Pool and Lazy Writer

```
SQL Server Memory Architecture
=================================

+------------------------------------------------------------+
|                     Buffer Pool                            |
|  (single largest consumer of memory)                       |
|                                                            |
|  +----------+----------+----------+----------+----------+  |
|  | Page 0   | Page 1   | Page 2   | Page 3   | Page N   |  |
|  | (8KB)    | (8KB)    | (8KB)    | (8KB)    | (8KB)    |  |
|  | clean    | dirty    | clean    | dirty    | clean    |  |
|  +----------+----------+----------+----------+----------+  |
|                                                            |
|  Managed by:                                               |
|  - Lazy Writer: evicts least-recently-used clean pages     |
|                 under memory pressure                      |
|  - Checkpoint: writes all dirty pages to disk periodically |
|  - Eager Writer: for bulk operations                       |
+------------------------------------------------------------+
|  Plan Cache (compiled query plans)                         |
+------------------------------------------------------------+
|  Lock Manager Memory                                       |
+------------------------------------------------------------+
|  CLR / Extended Stored Proc Memory                         |
+------------------------------------------------------------+

Key difference: SQL Server manages its own memory in one large
buffer pool (similar to InnoDB buffer pool, unlike PostgreSQL
which relies heavily on the OS page cache).
```

### Transaction Log Architecture

```
SQL Server Transaction Log
============================

Single log file per database (.ldf):

+---------------------------------------------------+
| VLF 1  | VLF 2  | VLF 3  | VLF 4  | VLF 5  | ...|
| (used) | (used) | (active| (free) | (free) |     |
|        |        |  txn)  |        |        |     |
+---------------------------------------------------+
                      ^
                      |
               MinLSN (oldest active transaction)
               Log cannot be truncated before this point

VLF = Virtual Log File
  - SQL Server internally divides the log into VLFs
  - VLFs are marked active/reusable
  - Log truncation marks VLFs as reusable (does not shrink file)

Recovery Models:
  SIMPLE:   Log truncated at checkpoint (no PITR)
  FULL:     Log retained until backed up (enables PITR)
  BULK_LOGGED: Like FULL but minimal logging for bulk ops
```

### TempDB and Version Store

```
TempDB Architecture
====================

TempDB: System database for temporary objects

Uses:
  1. Temp tables (#table, ##table)
  2. Table variables (@table)
  3. Sort spills (when memory insufficient)
  4. Hash join spills
  5. VERSION STORE for snapshot isolation
  6. Online index rebuild scratch space

Version Store (in TempDB):
  - Used when READ_COMMITTED_SNAPSHOT_ISOLATION (RCSI) is ON
  - Stores row versions for consistent reads (like MVCC)
  - Row in data page has a 14-byte version pointer to TempDB
  - Background cleanup removes old versions

  Without RCSI:  Readers block writers, writers block readers
  With RCSI:     Readers see consistent snapshot (no blocking)
                 Similar to PostgreSQL's default behavior

Best practices:
  - Multiple TempDB data files (1 per CPU core, up to 8)
  - Place on fastest storage (SSD/NVMe)
  - Pre-size to avoid autogrow during production
```

### Columnstore Indexes (Hybrid OLTP/OLAP)

```
SQL Server Columnstore Indexes
================================

Clustered Columnstore Index (CCI):
  - Table stored in columnar format
  - Excellent compression (5-10x)
  - Batch mode execution for analytics
  - NOT suitable for point lookups

Nonclustered Columnstore Index (NCCI):
  - Added to a traditional rowstore table
  - Enables "real-time operational analytics"
  - OLTP workload uses rowstore; analytics uses columnstore
  - Maintained in real-time (no ETL needed)

Architecture:
  +-----------------------+
  | Rowstore (B-tree)     |  <-- OLTP: INSERT, UPDATE, point lookups
  +-----------------------+
  | + NCCI Columnstore    |  <-- OLAP: aggregations, scans
  |   +------+------+     |
  |   | Col A| Col B|     |
  |   | seg1 | seg1 |     |     Row Group (~1M rows)
  |   | seg2 | seg2 |     |     Each segment compressed independently
  |   +------+------+     |
  +-----------------------+

  Delta Store: New rows go to a rowstore "delta store"
               Background tuple mover compresses into columnstore
               when delta store reaches ~1M rows
```

### Always On Availability Groups

```
Always On Availability Groups
===============================

+------------------+           +------------------+
|   Primary        |           |  Secondary (1)   |
|   Replica        | --------> |  (sync/async)    |
|                  |    Log    |  Read-only or     |
| Read + Write     |  shipping |  no access        |
+------------------+           +------------------+
         |
         |            +------------------+
         +----------> |  Secondary (2)   |
              Log     |  (async)         |
            shipping  |  Read-only       |
                      +------------------+

Features:
  - Database-level failover (not instance-level like FCI)
  - Up to 9 replicas (8 secondary)
  - Synchronous commit for zero data loss
  - Readable secondaries for read scale-out
  - Automatic failover with Windows/Linux clustering
  - Distributed AGs for cross-cluster/cross-datacenter
```

### In-Memory OLTP (Hekaton)

```
In-Memory OLTP (Hekaton) Architecture
========================================

Traditional Row Format:
  +----------------------------------+
  | Page-based B-tree                |
  | 8KB pages, buffer pool managed   |
  | Latch-based concurrency          |
  +----------------------------------+

Memory-Optimized Table:
  +----------------------------------+
  | Lock-free, latch-free data       |
  | structures in main memory        |
  |                                  |
  | Bw-tree indexes (latch-free)     |
  | Hash indexes (lock-free buckets) |
  |                                  |
  | Rows: chained via version links  |
  | No pages, no buffer pool         |
  | No WAL latches (log is lockfree) |
  +----------------------------------+

Natively Compiled Stored Procedures:
  - T-SQL compiled to C, then to native DLL
  - No interpreter overhead
  - 10-100x throughput improvement for eligible workloads

Durability options:
  SCHEMA_AND_DATA:  Logged, survives restart (checkpoint files + log)
  SCHEMA_ONLY:      Data lost on restart (temp table replacement)

Limitations:
  - Max 2TB per database (memory-bound)
  - Not all T-SQL features supported
  - LOB types not supported (until SQL Server 2019+)
  - Foreign keys across memory-optimized and disk-based not supported
```

---

## 6. NewSQL Databases

NewSQL databases aim to provide the scalability of NoSQL with the ACID guarantees and SQL interface of traditional RDBMS.

### CockroachDB

```
CockroachDB Architecture
==========================

                     +----------------+
                     |   SQL Gateway  |
                     |   (any node)   |
                     +-------+--------+
                             |
                     +-------v--------+
                     | SQL Layer       |
                     | (PostgreSQL     |
                     |  wire protocol) |
                     +-------+--------+
                             |
                     +-------v--------+
                     | Distribution   |
                     | Layer          |
                     | (transactions, |
                     |  routing)      |
                     +-------+--------+
                             |
              +--------------+--------------+
              |              |              |
         +----v----+    +----v----+    +----v----+
         | Range 1 |    | Range 2 |    | Range 3 |
         | [a-f)   |    | [f-m)   |    | [m-z)   |
         |         |    |         |    |         |
         | Leaseholder  | Leaseholder  | Leaseholder
         | + 2 replicas | + 2 replicas | + 2 replicas
         +---------+    +---------+    +---------+

Key concepts:
  - Key-value store underneath (Pebble, originally RocksDB)
  - Data divided into ranges (~512MB each)
  - Each range replicated 3x using Raft consensus
  - Range leaseholder serves reads; Raft leader coordinates writes
  - Ranges split and merge automatically
  - Any node can be SQL gateway (no single coordinator)

Distributed Transactions:
  - Parallel commits (optimized 2PC)
  - Write intents (like row-level locks visible to readers)
  - Txn record stored alongside data
  - Clock skew handled with hybrid-logical clocks (HLC)
    + clock uncertainty window for linearizability
```

### YugabyteDB

```
YugabyteDB Architecture
=========================

+---------------------------------------------------------+
|                     YB-TServer                          |
|                                                         |
|  +-------------------+    +-------------------+         |
|  |     YSQL Layer    |    |     YCQL Layer    |         |
|  | (PostgreSQL fork) |    | (Cassandra-compat)|         |
|  +--------+----------+    +--------+----------+         |
|           |                        |                    |
|  +--------v------------------------v----------+         |
|  |              DocDB (Storage Engine)        |         |
|  |                                            |         |
|  |  +----------+  +----------+  +----------+  |         |
|  |  | Tablet 1 |  | Tablet 2 |  | Tablet 3 |  |         |
|  |  | (Raft    |  | (Raft    |  | (Raft    |  |         |
|  |  |  group)  |  |  group)  |  |  group)  |  |         |
|  |  +----------+  +----------+  +----------+  |         |
|  |                                            |         |
|  |  Each tablet = RocksDB instance            |         |
|  +--------------------------------------------+         |
+---------------------------------------------------------+

+---------------------------------------------------------+
|                     YB-Master                           |
|  - Catalog manager (table schemas)                      |
|  - Tablet assignment and load balancing                 |
|  - Cluster configuration                               |
|  - Uses Raft for its own metadata replication           |
+---------------------------------------------------------+

Key differences from CockroachDB:
  - Based on PostgreSQL fork (not just wire-compatible)
  - DocDB is a document store (supports JSON natively)
  - Each tablet is a RocksDB instance
  - Supports Cassandra Query Language (YCQL) alongside SQL
  - Automatic tablet splitting at configurable size
```

### TiDB

```
TiDB Architecture
==================

+-------------------+     +-------------------+
|    TiDB Server    |     |    TiDB Server    |
|  (stateless SQL)  |     |  (stateless SQL)  |
|  MySQL protocol   |     |  MySQL protocol   |
+--------+----------+     +--------+----------+
         |                         |
         +------------+------------+
                      |
              +-------v-------+
              |      PD       |
              | (Placement    |
              |  Driver)      |
              | - TSO (time   |
              |   oracle)     |
              | - Region      |
              |   scheduling  |
              | - Raft leader |
              |   balancing   |
              +-------+-------+
                      |
         +------------+------------+
         |            |            |
    +----v----+  +----v----+  +----v----+
    | TiKV    |  | TiKV    |  | TiKV    |
    | Node 1  |  | Node 2  |  | Node 3  |
    |         |  |         |  |         |
    | Region  |  | Region  |  | Region  |
    | replicas|  | replicas|  | replicas|
    |         |  |         |  |         |
    |RocksDB  |  |RocksDB  |  |RocksDB  |
    +---------+  +---------+  +---------+

    Optional: TiFlash (columnar replica for OLAP -- HTAP)

Key concepts:
  - TiDB Server: stateless, horizontally scalable SQL layer
  - TiKV: distributed key-value store, Raft-based replication
  - PD: cluster metadata + Timestamp Oracle (TSO) for MVCC
  - Region: ~96MB key range (like CockroachDB range)
  - Percolator-based distributed transactions (from Google)
  - TiFlash: Raft learner replicas in columnar format for HTAP
```

### Google Spanner

```
Google Spanner Architecture
=============================

+-----------------------------------------------------------+
|                        Spanner                            |
|                                                           |
|  +------------------+  +------------------+               |
|  |   Zone A         |  |   Zone B         |  ... (N zones)|
|  |                  |  |                  |               |
|  | Spanserver 1     |  | Spanserver 1     |               |
|  | Spanserver 2     |  | Spanserver 2     |               |
|  | ...              |  | ...              |               |
|  |                  |  |                  |               |
|  | Each spanserver: |  |                  |               |
|  |  - Tablet(s)     |  |                  |               |
|  |  - Paxos group   |  |                  |               |
|  |  - Lock table    |  |                  |               |
|  |  - Txn manager   |  |                  |               |
|  +------------------+  +------------------+               |
|                                                           |
|  TrueTime API:                                            |
|    TT.now() returns [earliest, latest] interval           |
|    Guaranteed that actual time is within the interval      |
|    Uses GPS + atomic clocks for <7ms uncertainty           |
|                                                           |
|  External Consistency (linearizability):                   |
|    If T1 commits before T2 starts (real time),            |
|    then T1's timestamp < T2's timestamp. GUARANTEED.       |
|    Achieved by: commit-wait = wait out the uncertainty.   |
+-----------------------------------------------------------+

TrueTime enables:
  - Lock-free read-only transactions at any timestamp
  - Globally consistent snapshots without coordination
  - No need for hybrid-logical clocks or clock skew handling
  - Trade-off: commit-wait adds ~7ms latency to writes
```

### NewSQL Comparison

| Feature | CockroachDB | YugabyteDB | TiDB | Google Spanner |
|---|---|---|---|---|
| **SQL Compatibility** | PostgreSQL wire protocol | PostgreSQL (fork) + Cassandra | MySQL wire protocol | Google SQL (custom) |
| **Storage Engine** | Pebble (LSM-tree) | DocDB (RocksDB per tablet) | TiKV (RocksDB) | Colossus (Google) |
| **Consensus** | Raft | Raft | Raft | Paxos |
| **Sharding Unit** | Range (~512MB) | Tablet (configurable) | Region (~96MB) | Split (~8GB) |
| **Time Model** | HLC + uncertainty | Hybrid Time | TSO (centralized) | TrueTime (GPS+atomic) |
| **Consistency** | Serializable (default) | Snapshot (YCQL), Serializable (YSQL) | Snapshot Isolation | External consistency |
| **HTAP Support** | No | No | TiFlash (columnar) | No |
| **License** | BSL (source-available) | Apache 2.0 (core) | Apache 2.0 | Proprietary (Cloud) |
| **Deployment** | Self-hosted, Cloud | Self-hosted, Cloud | Self-hosted, Cloud | Google Cloud only |

---

## 7. Embedded OLTP Databases

### SQLite

SQLite is the most widely deployed database engine in the world (estimated in every smartphone, browser, and most applications).

```
SQLite Architecture
====================

+--------------------------------------------+
|              Application Process           |
|                                            |
|  +--------------------------------------+  |
|  |          SQLite Library               |  |
|  |  (linked directly into the app)      |  |
|  |                                      |  |
|  |  +----------+  +----------+          |  |
|  |  | SQL       |  | B-tree   |          |  |
|  |  | Compiler  |  | Module   |          |  |
|  |  | (parser,  |  | (page    |          |  |
|  |  |  planner, |  |  cache,  |          |  |
|  |  |  codegen) |  |  cursor) |          |  |
|  |  +-----+-----+  +-----+----+          |  |
|  |        |              |               |  |
|  |  +-----v--------------v----+          |  |
|  |  |      Pager Module       |          |  |
|  |  | (page-level I/O, locks, |          |  |
|  |  |  journaling, WAL)       |          |  |
|  |  +----------+--------------+          |  |
|  |             |                         |  |
|  +--------------------------------------+  |
|                |                            |
+----------------v----------------------------+
                 |
          +------v------+
          | Single File |    database.db     (main database)
          |   on Disk   |    database.db-wal (WAL mode)
          +-------------+    database.db-shm (shared memory for WAL)

Key characteristics:
  - Single file = entire database (schema, data, indexes)
  - No separate server process (in-process library)
  - Entire database locked for writes (one writer at a time)
  - Readers never block writers (with WAL mode)
  - Zero configuration, zero administration
  - ~600KB compiled size
  - ACID-compliant, full SQL support
```

#### SQLite Concurrency Modes

| Mode | Readers | Writers | Use Case |
|---|---|---|---|
| **DELETE journal** (default) | Multiple, blocked during write | One, exclusive lock | Simple single-user apps |
| **WAL mode** | Multiple, concurrent with writer | One, but does not block readers | Web apps, multi-threaded apps |
| **WAL2 mode** (experimental) | Multiple | Two concurrent writers | Higher write throughput |

```sql
-- Enable WAL mode (persistent, per-database):
PRAGMA journal_mode=WAL;

-- Optimize for performance:
PRAGMA synchronous=NORMAL;       -- fsync only on checkpoint, not every commit
PRAGMA cache_size=-64000;        -- 64MB page cache
PRAGMA mmap_size=268435456;      -- 256MB memory-mapped I/O
PRAGMA journal_size_limit=67108864; -- 64MB WAL size before auto-checkpoint
```

### DuckDB (Embedded OLAP -- Contrast)

```
SQLite vs DuckDB
==================

                    SQLite                 DuckDB
                    ------                 ------
Orientation:        Row-store              Column-store
Best for:           OLTP (point lookups,   OLAP (aggregations,
                    single-row writes)     scans, analytics)
Concurrency:        One writer, many       One writer, many
                    readers (WAL mode)     readers
Query execution:    Row-at-a-time          Vectorized (batch)
                    (tuple-volcano)        (column-chunks)
Compression:        None                   Lightweight compression
                                           per column
Use case:           Mobile apps, config    Data science, local
                    storage, caching       analytics, CSV/Parquet
```

### LevelDB / RocksDB

```
LSM-Tree Based Embedded Stores
================================

Write Path:
  1. Write to WAL (sequential append)
  2. Insert into MemTable (sorted, in-memory -- skiplist or B-tree)
  3. When MemTable full --> flush to SSTable (L0) on disk
  4. Background compaction merges SSTables into deeper levels

              MemTable (in memory, sorted)
                  |
                  v (flush)
  Level 0:  [SST] [SST] [SST]     (overlapping key ranges)
                  |
                  v (compaction)
  Level 1:  [SST] [SST] [SST] [SST]  (non-overlapping)
                  |
                  v (compaction)
  Level 2:  [SST] [SST] [SST] [SST] [SST] [SST]  (10x larger)

Read Path:
  1. Check MemTable
  2. Check immutable MemTable (being flushed)
  3. Check L0 SSTables (may check all -- overlapping)
  4. Check L1+ SSTables (binary search -- non-overlapping)
  5. Bloom filters skip SSTables that definitely don't contain key

RocksDB improvements over LevelDB:
  - Multi-threaded compaction
  - Column families (logical separation within one DB)
  - Rate limiter for compaction I/O
  - Universal and FIFO compaction styles
  - Transactions (optimistic and pessimistic)
  - Merge operator (read-modify-write without read)
  - Block-based bloom filters, partitioned indexes
```

### BoltDB / bbolt

```
BoltDB Architecture
====================

  - Pure Go embedded key-value store
  - B+tree storage (NOT LSM-tree)
  - Single-file database (like SQLite)
  - MVCC with copy-on-write B+tree pages
  - Single writer, multiple readers (no blocking)
  - Used by etcd (Kubernetes backing store)

Structure:
  +-------------------------------------------+
  | Meta Page 0 | Meta Page 1 |               |
  | (root ptr,  | (alternate  |               |
  |  txn id,    |  for crash  |               |
  |  freelist)  |  safety)    |               |
  +-------------------------------------------+
  | B+tree pages (4KB default)                |
  |                                           |
  | Buckets (like tables/collections)         |
  |   - Nested buckets supported              |
  |   - Each bucket is a B+tree               |
  |                                           |
  | Copy-on-write:                            |
  |   - Write txn copies modified pages       |
  |   - Atomically updates meta page on commit|
  |   - Old pages added to freelist           |
  +-------------------------------------------+

Trade-offs vs RocksDB:
  + Simpler, no background compaction
  + Consistent read performance (no compaction stalls)
  + Strong isolation (serializable)
  - Slower writes (in-place B+tree vs append-only LSM)
  - Write amplification from copy-on-write
  - No compression
```

---

## 8. OLTP Performance Tuning

### Connection Pooling

Database connections are expensive: PostgreSQL forks a process (~10MB RSS), MySQL creates a thread (~256KB stack + connection buffers). Without pooling, a 10,000-connection web tier crushes the database.

```
Connection Pooling Architecture
=================================

Without pooling:
  [App 1] --conn--> [DB]
  [App 2] --conn--> [DB]
  ...
  [App N] --conn--> [DB]     N connections = N processes/threads

With pooling (e.g., PgBouncer):
  [App 1] --\                  /--> [DB conn 1]
  [App 2] ---+--> [PgBouncer] +---> [DB conn 2]
  ...        |    (100 conns  |     ...
  [App N] --/     to DB)       \--> [DB conn 100]

  10,000 app connections multiplexed onto 100 DB connections
```

#### PgBouncer Modes (PostgreSQL)

| Mode | Behavior | Trade-off |
|---|---|---|
| **Session** | Client gets a dedicated server connection for the session | Safest, least savings |
| **Transaction** | Client gets a connection only during a transaction | Best balance; cannot use session-level features (LISTEN, prepared stmts with named handles) |
| **Statement** | Connection returned after every statement | Maximum sharing; no multi-statement transactions |

#### ProxySQL (MySQL)

```
ProxySQL Features
==================

+------------------+          +------------------+
|   Application    |          |   Application    |
+--------+---------+          +--------+---------+
         |                             |
         +-------------+---------------+
                       |
              +--------v---------+
              |    ProxySQL      |
              |                  |
              | - Connection     |
              |   multiplexing   |
              | - Query routing  |
              |   (read/write    |
              |    splitting)    |
              | - Query caching  |
              | - Query rewrite  |
              | - Failover       |
              +--------+---------+
                       |
         +-------------+---------------+
         |                             |
+--------v---------+          +--------v---------+
|  MySQL Primary   |          | MySQL Replica(s) |
| (writes)         |          | (reads)          |
+------------------+          +------------------+
```

### Query Optimization Patterns

```sql
-- 1. Use covering indexes to avoid heap/table access
CREATE INDEX idx_orders_cover ON orders(customer_id, status, created_at)
  INCLUDE (total);  -- PostgreSQL: INCLUDE for non-key columns

-- Query satisfied entirely from index:
SELECT status, total FROM orders
WHERE customer_id = 42 AND created_at > '2025-01-01';

-- 2. Use partial indexes for common filters
CREATE INDEX idx_active_orders ON orders(customer_id)
  WHERE status = 'active';
-- Much smaller than indexing all orders

-- 3. Avoid SELECT * -- fetch only needed columns
-- Bad:  SELECT * FROM users WHERE id = 42;
-- Good: SELECT name, email FROM users WHERE id = 42;

-- 4. Batch operations instead of row-by-row
-- Bad:
--   INSERT INTO logs VALUES (1, 'a');
--   INSERT INTO logs VALUES (2, 'b');
--   INSERT INTO logs VALUES (3, 'c');
-- Good:
INSERT INTO logs VALUES (1, 'a'), (2, 'b'), (3, 'c');

-- 5. Use EXISTS instead of IN for correlated checks
-- Potentially slow:
SELECT * FROM orders WHERE customer_id IN (
  SELECT id FROM customers WHERE region = 'US'
);
-- Often faster:
SELECT * FROM orders o WHERE EXISTS (
  SELECT 1 FROM customers c WHERE c.id = o.customer_id AND c.region = 'US'
);

-- 6. Avoid functions on indexed columns
-- Bad (cannot use index):
SELECT * FROM users WHERE LOWER(email) = 'alice@example.com';
-- Good (use expression index in PostgreSQL):
CREATE INDEX idx_users_email_lower ON users(LOWER(email));
-- Or store normalized in a column
```

### Index Strategies for OLTP

| Strategy | When to Use | Example |
|---|---|---|
| **B-tree** (default) | Equality, range, sorting, LIKE 'prefix%' | `CREATE INDEX idx ON t(col)` |
| **Hash** (PostgreSQL) | Equality only, faster than B-tree for = | `CREATE INDEX idx ON t USING hash(col)` |
| **Partial** | Only a subset of rows queried | `CREATE INDEX idx ON t(col) WHERE active = true` |
| **Covering / INCLUDE** | Avoid table access entirely | `CREATE INDEX idx ON t(a) INCLUDE (b, c)` |
| **Composite** | Multi-column lookups | `CREATE INDEX idx ON t(a, b, c)` -- leftmost prefix rule |
| **Expression** | Functions on columns | `CREATE INDEX idx ON t(LOWER(email))` |
| **GIN** (PostgreSQL) | Full-text search, JSONB, arrays | `CREATE INDEX idx ON t USING gin(tags)` |

### Lock Contention Diagnosis

```sql
-- PostgreSQL: Find blocking queries
SELECT
  blocked.pid       AS blocked_pid,
  blocked.query     AS blocked_query,
  blocking.pid      AS blocking_pid,
  blocking.query    AS blocking_query,
  blocking.state    AS blocking_state
FROM pg_stat_activity blocked
JOIN pg_locks bl ON bl.pid = blocked.pid AND NOT bl.granted
JOIN pg_locks gl ON gl.locktype = bl.locktype
  AND gl.database IS NOT DISTINCT FROM bl.database
  AND gl.relation IS NOT DISTINCT FROM bl.relation
  AND gl.page IS NOT DISTINCT FROM bl.page
  AND gl.tuple IS NOT DISTINCT FROM bl.tuple
  AND gl.pid != bl.pid
  AND gl.granted
JOIN pg_stat_activity blocking ON blocking.pid = gl.pid;

-- MySQL: Find InnoDB lock waits
SELECT
  r.trx_id              AS waiting_trx_id,
  r.trx_mysql_thread_id AS waiting_thread,
  r.trx_query           AS waiting_query,
  b.trx_id              AS blocking_trx_id,
  b.trx_mysql_thread_id AS blocking_thread,
  b.trx_query           AS blocking_query
FROM information_schema.innodb_lock_waits w
JOIN information_schema.innodb_trx b ON b.trx_id = w.blocking_trx_id
JOIN information_schema.innodb_trx r ON r.trx_id = w.requesting_trx_id;
```

### Monitoring

```
Key Monitoring Views / Tables
===============================

PostgreSQL:
  pg_stat_statements     -- Query-level stats (calls, time, rows, blocks)
  pg_stat_user_tables    -- Table-level stats (seq scans, idx scans, dead tuples)
  pg_stat_user_indexes   -- Index usage (scans, tuples read/fetched)
  pg_stat_activity       -- Current queries, wait events, state
  pg_stat_bgwriter       -- Checkpoint and background writer stats
  pg_stat_replication    -- Replication lag, WAL send/write/flush/replay

MySQL:
  performance_schema.events_statements_summary_by_digest
                         -- Query-level stats (like pg_stat_statements)
  information_schema.innodb_metrics
                         -- InnoDB internal counters
  SHOW ENGINE INNODB STATUS
                         -- InnoDB status dump (txns, locks, buffer pool)
  sys.schema_unused_indexes
                         -- Indexes never used (candidates for removal)
  sys.statements_with_full_table_scans
                         -- Queries doing full table scans
```

### Common Bottlenecks and Solutions

| Bottleneck | Symptoms | Solution |
|---|---|---|
| **Too many connections** | Fork/thread overhead, OOM | Connection pooling (PgBouncer, ProxySQL) |
| **Lock contention** | High wait times, deadlocks | Shorter transactions, optimistic locking, advisory locks |
| **Sequential scans** | High `seq_scan` count, slow queries | Add appropriate indexes, fix missing WHERE clauses |
| **Bloated tables** | Table size >> data size | Tune autovacuum, run VACUUM FULL during maintenance window |
| **Checkpoint spikes** | I/O spikes every `checkpoint_timeout` | Increase `checkpoint_completion_target` (0.9), spread writes |
| **WAL write bottleneck** | High `WALWrite` wait events | Faster disk for pg_wal, `synchronous_commit = off` for non-critical |
| **Replication lag** | Read replicas return stale data | Add replicas, parallel apply, check slow queries on replica |
| **TempDB contention** (SQL Server) | PFS/GAM/SGAM page latch waits | Multiple TempDB files, TF 1118 |

---

## 9. OLTP Scaling Strategies

### Scaling Progression

```
OLTP Scaling Ladder
=====================

Stage 1: Vertical Scaling
  +---------------------------+
  | Bigger box                |
  | More CPU, RAM, NVMe SSD  |
  | Simple. Works to ~$50K/mo|
  +---------------------------+
         |
         v  (hit single-machine limits)

Stage 2: Read Replicas
  +----------+     +----------+
  | Primary  |---->| Replica  |  Offload reads
  | (writes) |---->| Replica  |  Typical ratio: 80% reads
  +----------+     +----------+
         |
         v  (write bottleneck or connection limits)

Stage 3: Connection Pooling
  [Apps] --> [PgBouncer/ProxySQL] --> [DB]
  10K app connections --> 100 DB connections
         |
         v  (single-machine write throughput limit)

Stage 4: Functional Partitioning
  Users DB <---> Orders DB <---> Payments DB
  Split by service domain (microservices)
         |
         v  (single table too large for one machine)

Stage 5: Horizontal Sharding
  +-----------+  +-----------+  +-----------+
  | Shard 0   |  | Shard 1   |  | Shard 2   |
  | users 0-N |  | users N-M |  | users M-Z |
  +-----------+  +-----------+  +-----------+
```

### Sharding Strategies

```
Sharding Key Selection
========================

Hash-Based Sharding:
  shard_id = hash(user_id) % num_shards

  + Even distribution
  + Simple to implement
  - Range queries span all shards (scatter-gather)
  - Resharding requires data movement (consistent hashing helps)

Range-Based Sharding:
  shard_id = lookup_range(user_id)
  Shard 0: user_id [0, 1000000)
  Shard 1: user_id [1000000, 2000000)
  ...

  + Range queries on shard key are efficient
  + Easy to split hot ranges
  - Hot spots if access is skewed (recent users are most active)
  - Requires rebalancing when ranges grow unevenly

Directory-Based Sharding:
  Lookup table: user_id -> shard_id

  + Maximum flexibility (move individual users between shards)
  + No hot spots (can rebalance)
  - Lookup table is a single point of failure
  - Additional hop for every query
  - Lookup table itself needs to be highly available
```

### Vitess (MySQL Sharding)

```
Vitess Architecture
====================

+-------------------+
|   Application     |
+--------+----------+
         |
+--------v----------+
|      VTGate       |   Query router, connection pooling
| (stateless proxy) |   Parses SQL, routes to correct shard
+--------+----------+   Handles scatter-gather for cross-shard
         |
+--------v----------+
|     VTTablet      |   One per MySQL instance
| (shard-aware      |   Manages replication, schema changes
|  agent)           |   Health checking, backup/restore
+--------+----------+
         |
+--------v----------+
|      MySQL        |   Actual data storage
| (one per shard/   |   Standard MySQL/InnoDB
|  replica)         |
+--------------------+

Topology Service:
  - etcd / ZooKeeper / Consul
  - Stores shard map, tablet locations, schema

VSchema (Vitess Schema):
  - Defines sharding key, vindexes (virtual indexes)
  - Maps logical tables to physical shards

Resharding:
  1. Create new shards (target)
  2. Start VReplication from source to target
  3. Switch reads to target
  4. Switch writes to target
  5. Remove source shards

Used by: Slack, Square, GitHub, HubSpot, JD.com
```

### Citus (PostgreSQL Sharding)

```
Citus Architecture
===================

+-------------------+
|   Application     |
+--------+----------+
         |
+--------v----------+
|  Coordinator Node |   Receives queries, plans distribution
|  (PostgreSQL +    |   Stores metadata (shard map)
|   Citus extension)|   Routes / parallelizes across workers
+--------+----------+
         |
+--------+---+---+---+---+----------+
         |       |       |
+--------v-+ +---v---+ +-v--------+
| Worker 1 | |Worker 2| | Worker 3 |
| (PG +    | |(PG +   | | (PG +    |
|  Citus)  | | Citus) | |  Citus)  |
|          | |        | |          |
| Shards:  | |Shards: | | Shards:  |
| orders_1 | |orders_2| | orders_3 |
| users_1  | |users_2 | | users_3  |
+----------+ +--------+ +----------+

Distribution methods:
  - Hash distribution (most common): hash(shard_key) % shard_count
  - Range distribution: for time-series data
  - Reference tables: small tables replicated to all workers

-- Create a distributed table
SELECT create_distributed_table('orders', 'customer_id');

-- Co-located tables: tables sharded on same key land on same worker
-- Enables local joins without network round-trips
SELECT create_distributed_table('order_items', 'customer_id',
  colocate_with => 'orders');

-- Reference table (replicated to all workers)
SELECT create_reference_table('countries');
```

### Scaling Decision Matrix

| Scale Challenge | Solution | Complexity | When to Use |
|---|---|---|---|
| CPU saturated | Vertical scaling | Low | First option, always |
| Read throughput | Read replicas | Low | 80%+ read workload |
| Connection count | Connection pooling | Low | Always (even before issues) |
| Write throughput (single table) | Sharding (Vitess/Citus) | High | After vertical scaling maxed |
| Write throughput (different tables) | Functional partitioning | Medium | Microservice boundaries clear |
| Global distribution | NewSQL (CockroachDB, Spanner) | Medium | Multi-region latency requirements |
| Complex cross-shard queries | Denormalize or CQRS | High | Analytics on sharded OLTP data |

### Connection Pooling at Scale

```
Connection Math
================

Without pooling:
  50 app servers x 20 threads each = 1,000 connections
  PostgreSQL: 1,000 processes x ~10MB = 10GB just for connections
  Max practical: ~300-500 connections for PostgreSQL

With PgBouncer (transaction mode):
  1,000 app connections --> PgBouncer --> 100 DB connections
  Each app thread holds a DB connection only during its transaction
  Typical transaction: 5ms --> each connection does 200 txn/sec
  100 connections x 200 txn/sec = 20,000 txn/sec throughput

Multi-layer pooling for massive scale:
  [App] --> [App-side pool] --> [PgBouncer per-AZ] --> [DB]

  App-side pool (e.g., HikariCP, SQLAlchemy pool):
    - Limits connections from each app instance
    - Fast: no network hop for connection checkout

  PgBouncer:
    - Limits total connections to DB
    - Handles thundering herd on reconnect
    - Transparent to application
```

---

## Appendix: Quick Reference

### PostgreSQL vs MySQL/InnoDB Internals Comparison

| Aspect | PostgreSQL | MySQL/InnoDB |
|---|---|---|
| **Process model** | Process-per-connection | Thread-per-connection |
| **Table storage** | Heap (unordered) | Clustered index (PK-ordered B+tree) |
| **MVCC old versions** | In heap (same table) | In undo log (separate) |
| **Cleanup mechanism** | VACUUM (explicit) | Purge thread (automatic) |
| **Page size** | 8KB (compile-time) | 16KB (configurable) |
| **Secondary index leaf** | Points to heap TID (ctid) | Stores primary key value |
| **Replication log** | WAL (physical) or logical decoding | Binary log (logical) + redo log (physical) |
| **Partial write protection** | Full-page writes after checkpoint | Doublewrite buffer |
| **Default isolation** | Read Committed | Repeatable Read |
| **Serializable implementation** | SSI (Serializable Snapshot Isolation) | Gap locks + next-key locking |

### Essential Tuning Parameters

```
PostgreSQL (postgresql.conf):
  shared_buffers           = 25% of RAM (up to ~8GB)
  effective_cache_size     = 75% of RAM
  work_mem                 = 256MB / max_connections (start 4-64MB)
  maintenance_work_mem     = 1-2GB (for VACUUM, CREATE INDEX)
  random_page_cost         = 1.1 (SSD) or 4.0 (HDD)
  effective_io_concurrency = 200 (SSD) or 2 (HDD)
  max_connections          = 100-200 (use pooler for more)
  wal_level                = replica (minimum for replication)
  checkpoint_completion_target = 0.9
  max_wal_size             = 4-8GB
  autovacuum_max_workers   = 3-6

MySQL (my.cnf):
  innodb_buffer_pool_size  = 70-80% of RAM
  innodb_log_file_size     = 1-2GB
  innodb_flush_log_at_trx_commit = 1 (ACID) or 2 (performance)
  innodb_flush_method      = O_DIRECT (Linux)
  innodb_io_capacity       = 2000 (SSD) or 200 (HDD)
  innodb_io_capacity_max   = 4000 (SSD) or 400 (HDD)
  max_connections          = 151 (use ProxySQL for more)
  innodb_purge_threads     = 4
  innodb_read_io_threads   = 4
  innodb_write_io_threads  = 4
```
