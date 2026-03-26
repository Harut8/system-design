# Storage Engine Fundamentals: A Deep Dive

How databases physically store, retrieve, and manage data on disk and in memory. This document covers the core primitives that every storage engine is built on: disk pages, the buffer pool, and the I/O subsystem. Understanding these fundamentals is essential before studying indexes, transactions, or query execution, because every higher-level feature ultimately reduces to reading and writing pages.

---

## Table of Contents

1. [The Storage Engine's Role](#1-the-storage-engines-role)
2. [Pages: The Fundamental Unit of Storage](#2-pages-the-fundamental-unit-of-storage)
3. [Page Layout and Internal Organization](#3-page-layout-and-internal-organization)
4. [Heap Files and Page Organization](#4-heap-files-and-page-organization)
5. [The Buffer Pool](#5-the-buffer-pool)
6. [Page Replacement Policies](#6-page-replacement-policies)
7. [Disk I/O: The Bottleneck](#7-disk-io-the-bottleneck)
8. [Write Path: From Memory to Durable Storage](#8-write-path-from-memory-to-durable-storage)
9. [The Write-Ahead Log (WAL)](#9-the-write-ahead-log-wal)
10. [Checksums and Data Integrity](#10-checksums-and-data-integrity)
11. [Storage Engine Architectures Compared](#11-storage-engine-architectures-compared)

---

## 1. The Storage Engine's Role

### Where the Storage Engine Sits

The storage engine is the lowest layer of a database that the query engine interacts with. It owns the on-disk data format, the in-memory cache, and the durability guarantees. Everything above it -- parsing, optimization, execution -- eventually calls down to the storage engine to fetch or modify pages.

```
┌─────────────────────────────────────────────────────────────────────┐
│                          CLIENT                                      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  SQL / Wire Protocol
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     QUERY ENGINE LAYER                                │
│  Parser → Optimizer → Executor                                       │
│  "Give me rows from 'users' where id = 42"                          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  Page-level API
                               │  read_page(table_id, page_no)
                               │  write_page(table_id, page_no, data)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     STORAGE ENGINE LAYER                              │
│                                                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │  Buffer Pool │  │  WAL Manager │  │  Space Mgmt  │               │
│  │  (Page Cache)│  │  (Durability)│  │  (Free Space)│               │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘               │
│         │                 │                 │                         │
│         └─────────────────┼─────────────────┘                        │
│                           │                                           │
│                    ┌──────▼───────┐                                   │
│                    │  Disk I/O    │                                   │
│                    │  Manager     │                                   │
│                    └──────┬───────┘                                   │
│                           │                                           │
└───────────────────────────┼──────────────────────────────────────────┘
                            │  read() / write() / fsync()
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     OPERATING SYSTEM                                  │
│              Filesystem → Block Device → Disk Controller             │
└─────────────────────────────────────────────────────────────────────┘
```

### Core Responsibilities

| Responsibility | What It Means |
|---------------|---------------|
| **Page management** | Organizing data into fixed-size pages on disk |
| **Buffer management** | Caching frequently-used pages in RAM |
| **Concurrency control** | Latching pages in memory so concurrent threads don't corrupt data |
| **Durability** | Guaranteeing that committed data survives crashes (WAL, fsync) |
| **Space reclamation** | Tracking free space within pages and across files |
| **Serialization** | Encoding rows/columns into byte layouts within pages |

### Storage Engines Across Databases

| Database | Storage Engine(s) | Page Size | Notes |
|----------|-------------------|-----------|-------|
| PostgreSQL | Heap-based (built-in) | 8 KB | Single built-in engine, extensible via table access methods (PG 12+) |
| MySQL | InnoDB (default), MyISAM | 16 KB (InnoDB) | Pluggable storage engine architecture |
| SQL Server | In-Memory OLTP, traditional | 8 KB | Two engines; traditional is page-based |
| SQLite | B-Tree based | 4 KB (default, 512B–64KB) | Entire database is one file |
| Oracle | Automatic Segment Space Mgmt | 8 KB (default) | Tablespace-managed |
| RocksDB | LSM-Tree | Variable (block-based, default 4 KB) | Not page-oriented; uses SST files with blocks |

---

## 2. Pages: The Fundamental Unit of Storage

### Why Fixed-Size Pages?

Databases do not read or write individual rows. They read and write **pages** -- fixed-size blocks of data, typically 4 KB to 16 KB. This is a deliberate design choice driven by how hardware works.

```
WHY PAGES, NOT ROWS?

  1. HARDWARE ALIGNMENT
     ─────────────────
     SSDs read/write in 4 KB "flash pages."
     HDDs read in 512-byte sectors but seek costs dominate, so reading
     a full 8 KB page costs nearly the same as reading 1 byte.
     → Aligning database pages to hardware boundaries avoids
       read-modify-write amplification.

  2. BUFFER POOL SIMPLICITY
     ──────────────────────
     Fixed-size pages → fixed-size slots in memory.
     No memory fragmentation. Simple free-list management.
     A 1 GB buffer pool with 8 KB pages = exactly 131,072 slots.

  3. I/O AMORTIZATION
     ─────────────────
     One page read brings in many rows. A point query for 1 row
     loads ~100 neighboring rows into cache for free.
     → Subsequent queries on nearby rows are instant (buffer pool hit).

  4. CRASH RECOVERY
     ───────────────
     WAL records reference page numbers. Fixed page boundaries make it
     possible to redo/undo at page granularity after a crash.
```

### Page Addressing

Every page in the database is identified by a unique address. The addressing scheme varies:

```
PostgreSQL:  (tablespace_oid, database_oid, relfilenode, block_number)
             Simplified: (relation_id, block_number)
             Block number is 0-indexed, each block = 8192 bytes
             File offset = block_number × 8192

InnoDB:      (space_id, page_number)
             space_id identifies the tablespace
             page_number is offset within the tablespace file
             File offset = page_number × 16384

SQLite:      page_number (1-indexed)
             File offset = (page_number - 1) × page_size
```

### Page Size Trade-offs

```
               Small Pages (4 KB)              Large Pages (16 KB–32 KB)
              ┌─────────────────┐             ┌──────────────────────┐
  Pros:       │ • Less wasted    │             │ • Higher B-Tree      │
              │   space per page │             │   fan-out → fewer    │
              │ • Less I/O for   │             │   tree levels        │
              │   point queries  │             │ • Better for large   │
              │ • Better SSD     │             │   sequential scans   │
              │   alignment      │             │ • Fewer page fetches │
              │   (4 KB native)  │             │   for range queries  │
              ├─────────────────┤             ├──────────────────────┤
  Cons:       │ • More tree      │             │ • More wasted space  │
              │   levels needed  │             │   (internal frag.)   │
              │ • More metadata  │             │ • Larger WAL records │
              │   overhead       │             │ • Higher buffer pool │
              │ • Worse for wide │             │   memory per slot    │
              │   rows           │             │ • Write amplification│
              └─────────────────┘             └──────────────────────┘

Practical guidance:
  - OLTP with small rows (< 200 bytes): 8 KB is the sweet spot
  - OLAP with wide rows or large BLOBs: 16–32 KB can help
  - SSD-only environments: 4 KB aligns with flash page size
  - Most databases: just use the default. Changing later requires a full dump/reload.
```

---

## 3. Page Layout and Internal Organization

### Slotted Page Architecture

The dominant page layout in row-oriented databases is the **slotted page**. It decouples the logical position of a tuple from its physical position within the page, which is critical for supporting in-place updates and compaction.

```
┌─────────────────────────────────────────────────────────────────┐
│                        PAGE HEADER                               │
│  ┌───────────┬──────────┬───────────┬──────────┬──────────────┐ │
│  │  Page ID  │  LSN     │ Checksum  │ Free     │ # of Tuples  │ │
│  │  (4 B)    │  (8 B)   │ (4 B)     │ Space    │ (2 B)        │ │
│  │           │          │           │ Ptr (2B) │              │ │
│  └───────────┴──────────┴───────────┴──────────┴──────────────┘ │
├─────────────────────────────────────────────────────────────────┤
│                      LINE POINTER ARRAY                          │
│  (Grows downward from top of page)                               │
│                                                                   │
│  ┌───────────┬───────────┬───────────┬───────────┬──── ...      │
│  │ Slot 1    │ Slot 2    │ Slot 3    │ Slot 4    │              │
│  │ offset:   │ offset:   │ offset:   │ offset:   │              │
│  │ 8040      │ 7880      │ 7720      │ 7560      │              │
│  │ len: 160  │ len: 160  │ len: 160  │ len: 160  │              │
│  └───────────┴───────────┴───────────┴───────────┴──── ...      │
│                                                                   │
│                         ▼ FREE SPACE ▼                           │
│                                                                   │
│          (Line pointers grow down, tuples grow up)               │
│                                                                   │
│                         ▲ FREE SPACE ▲                           │
│                                                                   │
│  ┌───────────────────────────────────────────────────────────┐   │
│  │ Tuple 4: {id=4, name="Dave", email="dave@ex.com", ...}   │   │
│  ├───────────────────────────────────────────────────────────┤   │
│  │ Tuple 3: {id=3, name="Charlie", email="charlie@...", ...}│   │
│  ├───────────────────────────────────────────────────────────┤   │
│  │ Tuple 2: {id=2, name="Bob", email="bob@example.com",...} │   │
│  ├───────────────────────────────────────────────────────────┤   │
│  │ Tuple 1: {id=1, name="Alice", email="alice@ex.com",...}  │   │
│  └───────────────────────────────────────────────────────────┘   │
│                                                                   │
│                       SPECIAL SPACE (optional)                   │
│              (B-Tree pages store right-sibling pointer here)     │
└─────────────────────────────────────────────────────────────────┘
```

### Why Indirection via Line Pointers?

The line pointer array provides a level of indirection between "slot number" and "physical byte offset within the page." This is essential because:

1. **Tuple movement within a page**: When a tuple is updated and changes size, it may need to move. The line pointer is updated to point to the new location, but external references (from indexes) still use the same (page_id, slot_number) and remain valid.

2. **Compaction**: When tuples are deleted, they leave holes. The page can be compacted (defragmented) by moving tuples and updating line pointers, without invalidating any external references.

3. **Variable-length rows**: Rows have different sizes. The slot array tracks each tuple's offset and length.

### Tuple Identifier (TID / ROWID / RID)

Every row in the database has a physical address, commonly called a **TID** (Tuple Identifier) or **RID** (Row Identifier):

```
TID = (page_number, slot_number)

PostgreSQL:  ctid = (page_number, item_offset)
             Example: (0, 1) = first tuple on first page
             Visible in queries: SELECT ctid, * FROM users;

InnoDB:      Rows live inside the clustered index (B+Tree organized
             by primary key). The "address" IS the primary key.
             Secondary indexes store the PK value, not a physical TID.

SQL Server:  RID = (FileID:PageID:SlotID) for heap tables
             For clustered index tables: the clustered key IS the locator.
```

The distinction matters: PostgreSQL's heap-based TIDs are physical pointers that can become stale after VACUUM moves tuples. InnoDB's clustered index approach means secondary index lookups always require a "double lookup" (index → PK → clustered index → row) but the row pointer never goes stale.

### Page Header Fields (PostgreSQL 8 KB Page)

| Field | Size | Purpose |
|-------|------|---------|
| `pd_lsn` | 8 bytes | LSN of last WAL record that modified this page. Used by recovery to determine if a page is already up-to-date. |
| `pd_checksum` | 2 bytes | CRC checksum (optional, enabled with `initdb --data-checksums`) |
| `pd_flags` | 2 bytes | Page flags (has free lines, is full, has dead tuples, etc.) |
| `pd_lower` | 2 bytes | Offset to start of free space (end of line pointer array) |
| `pd_upper` | 2 bytes | Offset to end of free space (start of newest tuple) |
| `pd_special` | 2 bytes | Offset to special space (used by index pages) |
| `pd_pagesize_version` | 2 bytes | Page size and layout version |
| `pd_prune_xid` | 4 bytes | Oldest prunable transaction ID |
| **Total header** | **24 bytes** | |

Free space = `pd_upper - pd_lower`. When this reaches zero, the page is full.

### Tuple Header (PostgreSQL HeapTupleHeader)

Each tuple also has its own header, carrying MVCC information:

```
┌──────────────────────────────────────────────────────────────┐
│                     TUPLE HEADER (23 bytes)                    │
├──────────┬──────────┬──────────┬──────────┬─────────────────┤
│ t_xmin   │ t_xmax   │ t_cid    │ t_ctid   │ t_infomask     │
│ (4 B)    │ (4 B)    │ (4 B)    │ (6 B)    │ (4 B) + pad    │
│ Creating │ Deleting │ Command  │ Current  │ Null bitmap,   │
│ txn ID   │ txn ID   │ ID       │ TID      │ has nulls,     │
│          │ (0 if    │          │ (may     │ has varlen,    │
│          │ alive)   │          │ differ   │ is HOT, etc.   │
│          │          │          │ if moved)│                │
└──────────┴──────────┴──────────┴──────────┴─────────────────┘

Then: NULL bitmap (1 bit per column), alignment padding, then actual column data.
```

The tuple header is why PostgreSQL tables have non-trivial per-row overhead: 23 bytes of header + null bitmap + alignment. A table with 10 tiny columns (say, 40 bytes of data) actually stores ~67+ bytes per tuple.

### InnoDB Page Layout (16 KB)

InnoDB uses a different internal page structure, optimized for its clustered B+Tree design:

```
┌────────────────────────────────────────────────────────┐
│  FIL Header (38 bytes)                                  │
│    - Space ID, page number, prev/next page pointers    │
│    - Page type (INDEX, UNDO, BLOB, etc.)               │
│    - LSN, checksum                                      │
├────────────────────────────────────────────────────────┤
│  INDEX Header (36 bytes)    [for INDEX pages]           │
│    - Number of directory slots                          │
│    - Heap top pointer                                   │
│    - Number of records                                  │
│    - Page level in B-Tree                               │
│    - Index ID                                           │
├────────────────────────────────────────────────────────┤
│  Infimum Record (13 bytes)  ← smallest possible record │
│  Supremum Record (13 bytes) ← largest possible record  │
├────────────────────────────────────────────────────────┤
│                                                          │
│  User Records                                           │
│    - Stored in insertion order physically               │
│    - Linked in KEY ORDER via next-record pointers       │
│    - Each record has a header (5 bytes min)             │
│      with delete flag, record type, next-record offset  │
│                                                          │
├────────────────────────────────────────────────────────┤
│  Free Space                                             │
├────────────────────────────────────────────────────────┤
│  Page Directory                                         │
│    - Array of slots pointing to every ~4-8 records     │
│    - Enables binary search within the page             │
├────────────────────────────────────────────────────────┤
│  FIL Trailer (8 bytes)                                  │
│    - Checksum, LSN (must match header for consistency) │
└────────────────────────────────────────────────────────┘
```

Key difference from PostgreSQL: InnoDB's records within a page are linked in logical (key) order via next-record pointers, and a page directory enables binary search. PostgreSQL's heap pages have no ordering -- tuples are simply appended into free space.

---

## 4. Heap Files and Page Organization

### What Is a Heap File?

A heap file is an unordered collection of pages. Rows are inserted wherever there is free space -- there is no sort order. This is the default storage layout in PostgreSQL, SQL Server (for non-clustered tables), and Oracle.

```
HEAP FILE (PostgreSQL relation on disk):

  base/16384/24576          ← Main data file for one table
  ┌────────┬────────┬────────┬────────┬────────┬─── ...
  │ Page 0 │ Page 1 │ Page 2 │ Page 3 │ Page 4 │
  │ 8 KB   │ 8 KB   │ 8 KB   │ 8 KB   │ 8 KB   │
  └────────┴────────┴────────┴────────┴────────┴─── ...
  Offset: 0    8192    16384    24576    32768

  Rows have NO particular order. New rows go to the first
  page with enough free space (tracked by the FSM).

  When the file exceeds 1 GB, PostgreSQL creates segment files:
  24576, 24576.1, 24576.2, ...
```

### Free Space Map (FSM)

To avoid scanning every page looking for free space during INSERT, databases maintain a **Free Space Map**.

```
PostgreSQL FSM (base/16384/24576_fsm):

  A tree structure where each leaf stores the free-space
  category (0–255) for one heap page.

  Category 0  = page is full (< 32 bytes free)
  Category 1  = 32–63 bytes free
  Category 2  = 64–95 bytes free
  ...
  Category 255 = ~8 KB free (empty page)

  INSERT process:
  1. Estimate tuple size (e.g., 160 bytes → need category 5+)
  2. Walk the FSM tree to find a page with enough free space
  3. Pin that page in buffer pool
  4. Insert tuple
  5. Update FSM if the page's free category changed

  The FSM is intentionally approximate -- it may report less
  free space than actually exists, but never more (conservative).
  VACUUM updates the FSM with accurate free-space information.
```

### Visibility Map (VM)

PostgreSQL also maintains a **Visibility Map** to accelerate index-only scans and reduce VACUUM work:

```
Visibility Map (base/16384/24576_vm):

  2 bits per heap page:
  ┌──────────┬──────────────────────────────────────────────────┐
  │ Bit 0    │ ALL_VISIBLE: every tuple on this page is visible │
  │          │ to all current and future transactions            │
  ├──────────┼──────────────────────────────────────────────────┤
  │ Bit 1    │ ALL_FROZEN: every tuple on this page is frozen   │
  │          │ (no longer needs transaction ID wraparound check)│
  └──────────┴──────────────────────────────────────────────────┘

  Benefits:
  - Index-only scans skip heap fetches for all-visible pages
  - VACUUM skips all-visible pages (no dead tuples to clean)
  - Freeze operations skip all-frozen pages
```

### Clustered vs Heap Organization

| Property | Heap (PostgreSQL, Oracle) | Clustered Index (InnoDB, SQL Server) |
|----------|--------------------------|--------------------------------------|
| Row order | Arbitrary (insertion order) | Sorted by primary key |
| Primary key lookup | Index scan → TID → heap page | B+Tree traversal → leaf = data |
| Secondary index lookup | Index → TID → heap page (1 hop) | Index → PK value → clustered index traversal (2 hops) |
| Insert performance | Fast (append to any free page) | May cause page splits if PK is non-sequential |
| Disk fragmentation | High over time | Low for sequential PK, high for random PK (UUIDs!) |
| Range scan on PK | Requires index, may random-I/O heap | Sequential leaf scan (very fast) |
| Table size overhead | Separate heap + index storage | Data is the index (no separate heap) |
| UPDATE behavior | HOT update possible (PG), but may fragment | May move row to different page if row grows |

---

## 5. The Buffer Pool

### Purpose and Architecture

The buffer pool (also called buffer cache, page cache, or shared buffer pool) is an in-memory region that caches disk pages. It is the single most critical performance component of any disk-based database. Without it, every row access would require a disk read.

```
┌──────────────────────────────────────────────────────────────┐
│                      BUFFER POOL                              │
│                                                                │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐           │
│  │ Frame 0 │ │ Frame 1 │ │ Frame 2 │ │ Frame 3 │   ...     │
│  │ ─────── │ │ ─────── │ │ ─────── │ │ ─────── │           │
│  │ Page    │ │ Page    │ │ Page    │ │ [empty] │           │
│  │ (5, 12) │ │ (5, 7)  │ │ (8, 0)  │ │         │           │
│  │         │ │         │ │         │           │           │
│  │ pin: 2  │ │ pin: 0  │ │ pin: 1  │ │ pin: 0  │           │
│  │ dirty:Y │ │ dirty:N │ │ dirty:Y │ │         │           │
│  │ ref: 1  │ │ ref: 1  │ │ ref: 0  │ │ ref: 0  │           │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘           │
│                                                                │
│  Page Table (hash map):                                       │
│  ┌────────────────┬─────────────┐                             │
│  │ (space, page)  │ frame_index │                             │
│  ├────────────────┼─────────────┤                             │
│  │ (5, 12)        │     0       │                             │
│  │ (5, 7)         │     1       │                             │
│  │ (8, 0)         │     2       │                             │
│  └────────────────┴─────────────┘                             │
│                                                                │
│  Free List: [3, 4, 5, ...]                                    │
│                                                                │
└──────────────────────────────────────────────────────────────┘
```

### Key Concepts

**Frame**: A fixed-size slot in the buffer pool that can hold exactly one page. The number of frames is fixed at startup (determined by the buffer pool size configuration).

**Page Table**: A hash map from (tablespace, page_number) to frame index. This is how the buffer pool determines whether a requested page is already in memory. Not to be confused with the OS page table.

**Pin Count**: The number of threads currently using a frame. A page with pin_count > 0 cannot be evicted. When a thread finishes reading/writing, it unpins the frame.

**Dirty Flag**: Set when a page has been modified in memory but not yet written to disk. Dirty pages must be flushed before their frame can be reused.

**Reference Bit / Count**: Used by the replacement policy (e.g., Clock algorithm) to track recent access.

### Buffer Pool Request Flow

```
read_page(table_id=5, page_no=12):

  ┌──────────────────────────────────┐
  │ 1. Hash lookup in page table     │
  │    key = (5, 12)                 │
  └────────────┬─────────────────────┘
               │
        ┌──────▼──────┐
        │ Found in    │──── YES ──→ ┌─────────────────────────┐
        │ page table? │              │ 2a. Increment pin count  │
        └──────┬──────┘              │ 2b. Return pointer to    │
               │                     │     frame contents       │
              NO                     │     (BUFFER POOL HIT)    │
               │                     └─────────────────────────┘
               ▼
  ┌──────────────────────────────────┐
  │ 3. Find a free frame:            │
  │    a. Check free list            │
  │    b. If empty, run replacement  │
  │       policy to pick a victim    │
  │    c. If victim is dirty,        │
  │       flush to disk first        │
  │    d. Remove victim from         │
  │       page table                 │
  └────────────┬─────────────────────┘
               │
               ▼
  ┌──────────────────────────────────┐
  │ 4. Issue disk I/O:               │
  │    pread(fd, buf, 8192,          │
  │          page_no * 8192)         │
  │    (BUFFER POOL MISS)            │
  └────────────┬─────────────────────┘
               │
               ▼
  ┌──────────────────────────────────┐
  │ 5. Insert into page table:       │
  │    page_table[(5,12)] = frame_id │
  │ 6. Set pin_count = 1             │
  │ 7. Set dirty = false             │
  │ 8. Return pointer to frame       │
  └──────────────────────────────────┘
```

### Buffer Pool Sizing

The buffer pool is the single most impactful tuning knob.

```
Database          Config Parameter           Default / Recommended
──────────────    ─────────────────────────  ──────────────────────────────
PostgreSQL        shared_buffers             128 MB default
                                             Recommended: 25% of total RAM
                                             (but rarely > 8-16 GB due to
                                              OS page cache double-buffering)

MySQL/InnoDB      innodb_buffer_pool_size    128 MB default
                                             Recommended: 70-80% of total RAM
                                             (InnoDB bypasses OS page cache
                                              with O_DIRECT)

SQL Server        max server memory          Dynamic (takes all available)
                                             Set ceiling to leave ~10-20%
                                             for OS

Oracle            SGA_TARGET / DB_CACHE_SIZE Varies
                                             Typically 50-70% of RAM
```

Why the difference between PostgreSQL (25%) and InnoDB (70-80%)?

```
PostgreSQL (double-buffered):

  ┌────────────────────────┐  ┌────────────────────────────────┐
  │    shared_buffers      │  │       OS Page Cache             │
  │    (25% of RAM)        │  │       (managed by kernel)       │
  │                        │  │                                  │
  │  PostgreSQL reads into │  │  The OS ALSO caches the same   │
  │  its own buffer pool   │──│  file pages. So data may exist │
  │                        │  │  in both caches simultaneously. │
  └────────────────────────┘  └────────────────────────────────┘

  PostgreSQL uses buffered I/O (read/write through the filesystem).
  The OS page cache acts as a second-level cache. Setting
  shared_buffers too high steals memory from the OS cache,
  which can be counterproductive.

InnoDB (direct I/O):

  ┌────────────────────────────┐
  │  innodb_buffer_pool        │  InnoDB uses O_DIRECT:
  │  (70-80% of RAM)           │  reads/writes bypass the OS page
  │                            │  cache entirely. InnoDB IS the
  │  This is the ONLY cache.   │  only cache, so it should be as
  │  No double-buffering.      │  large as possible.
  └────────────────────────────┘
```

### Latching (Not Locking)

Buffer pool operations require **latches** (lightweight, short-duration mutexes) to protect internal data structures. These are distinct from database locks (which protect logical data for transactions).

```
Latch types in the buffer pool:

  PAGE TABLE LATCH
  ────────────────
  Protects the hash map during lookup/insert/delete.
  Typically partitioned (e.g., 128 partitions in PostgreSQL)
  to reduce contention.

  BUFFER HEADER LATCH (per frame)
  ────────────────────────────────
  Protects the metadata of a single frame (pin count, dirty flag,
  etc.). Very short-lived: acquired, metadata updated, released.

  CONTENT LOCK (per frame)
  ─────────────────────────
  Controls concurrent access to the actual page content.
  - SHARED (read): multiple readers allowed simultaneously
  - EXCLUSIVE (write): only one writer, no readers

  Example: a seq scan takes a SHARED content lock on each page,
  while an INSERT takes an EXCLUSIVE content lock.

  These are NOT transaction locks. They are held for microseconds,
  not for the duration of a transaction.
```

### Multiple Buffer Pools

Large databases use multiple buffer pool instances to reduce latch contention:

```
MySQL/InnoDB:  innodb_buffer_pool_instances = 8 (default for pools > 1 GB)
               Each instance manages ~1/8 of total pool size.
               Pages are assigned to instances by hash(space_id, page_no).

PostgreSQL:    Uses 128 buffer partitions (BufMappingLock partitions)
               for page table lookups, reducing contention on the
               central hash table.

               PG 16+ also has per-backend I/O combining and async
               prefetching improvements to reduce buffer pool bottlenecks.
```

---

## 6. Page Replacement Policies

When the buffer pool is full and a new page must be loaded, the system must evict a page. The replacement policy determines which page to evict. The goal: keep hot (frequently accessed) pages in memory, evict cold ones.

### LRU (Least Recently Used)

The textbook algorithm: evict the page that was accessed least recently.

```
LRU List (most recent → least recent):

  HEAD ←→ Page A ←→ Page C ←→ Page F ←→ Page B ←→ Page D ←→ TAIL
  (hot)                                                       (cold)

  Access Page F:
  - Move F to HEAD:
  HEAD ←→ Page F ←→ Page A ←→ Page C ←→ Page B ←→ Page D ←→ TAIL

  Need to evict? Remove from TAIL → Page D is evicted.
```

**Problem**: LRU is vulnerable to **sequential flooding**. A single full table scan loads thousands of pages, pushing every hot page out of the buffer pool, even though the scan pages will never be accessed again.

```
Sequential scan flooding:

  Before scan: buffer pool = [hot pages used by OLTP queries]

  Full table scan reads pages 1, 2, 3, ..., 100,000
  Each page pushes one hot page off the LRU tail

  After scan: buffer pool = [pages 99,001 – 100,000]
              All OLTP hot pages are gone.
              Every subsequent OLTP query is a cache miss.
```

### Clock (Second-Chance) -- PostgreSQL's Approach

PostgreSQL uses a **Clock sweep** algorithm, which approximates LRU with much lower overhead.

```
CLOCK ALGORITHM:

  Buffer frames arranged in a circular array.
  Each frame has a "usage_count" (0 to 5 in PostgreSQL).
  A "clock hand" sweeps around the array.

  ┌─────────────────────────────────────┐
  │         CLOCK SWEEP                  │
  │                                      │
  │       Frame 0  Frame 1  Frame 2      │
  │      [cnt: 3] [cnt: 0] [cnt: 1]     │
  │         ↑                            │
  │    ┌────┘                            │
  │    │  Frame 7  ...      Frame 3      │
  │    │ [cnt: 2]          [cnt: 5]      │
  │    │                                 │
  │    │  Frame 6  Frame 5  Frame 4      │
  │    │ [cnt: 1] [cnt: 0] [cnt: 0]     │
  │    │         ↑                       │
  │    │    clock hand                   │
  │    └─────────                        │
  └─────────────────────────────────────┘

  WHEN A PAGE IS ACCESSED:
    usage_count = min(usage_count + 1, 5)

  WHEN A VICTIM IS NEEDED:
    Sweep from clock hand position:
    - If usage_count > 0: decrement by 1, skip this frame
    - If usage_count == 0: this frame is the victim!

    Hot pages (high usage_count) survive multiple sweeps.
    Cold pages (usage_count = 0) are evicted immediately.
```

Why cap at 5? It prevents a single burst of accesses (like an index build) from pinning a page in the buffer pool for an unreasonably long time. With a cap of 5, even a very hot page will be evicted after 5 full sweeps without being accessed.

### LRU-K -- SQL Server's Approach

SQL Server uses **LRU-2** (a variant of LRU-K with K=2), which tracks the second-to-last access time.

```
LRU-2: Evict the page whose SECOND most recent access is oldest.

  Page A: last accessed at t=100, second-last at t=5
  Page B: last accessed at t=98,  second-last at t=90
  Page C: last accessed at t=99,  second-last at t=1

  LRU would evict B (least recently accessed).
  LRU-2 evicts C (oldest second-to-last access → t=1).

  Why? Page B has been accessed twice recently (t=90 and t=98),
  suggesting a pattern of repeated use. Page C was accessed
  recently (t=99) but before that not since t=1 -- it's likely
  a one-time access (e.g., from a sequential scan).
```

LRU-K naturally resists sequential flooding because scan pages are accessed exactly once, so their "second most recent access" is -infinity, making them the first eviction candidates.

### InnoDB's Young/Old List

InnoDB splits its LRU list into two segments:

```
┌──────────────────────────────────────────────────────────────────┐
│                     InnoDB LRU LIST                                │
│                                                                    │
│ ◄──────── Young Region (5/8) ────────►◄─── Old Region (3/8) ───► │
│                                                                    │
│ [hot] ←→ [hot] ←→ ... ←→ [warm] ←→ │midpoint│ ←→ [old] ←→ [old]│
│                                       │        │                   │
│ Pages that have been accessed         │ NEW pages are inserted    │
│ at least twice while in the           │ HERE, not at the head.   │
│ old region get promoted to            │                           │
│ the young region.                     │ They must survive ~1 sec  │
│                                       │ (innodb_old_blocks_time)  │
│                                       │ and be accessed again     │
│                                       │ to be promoted to young.  │
└──────────────────────────────────────────────────────────────────┘

Anti-flooding mechanism:
  1. New page enters at the midpoint (old region), NOT the head.
  2. If accessed again within innodb_old_blocks_time (default 1 sec),
     it's still considered a one-time access and stays in old region.
  3. Only if accessed again AFTER innodb_old_blocks_time has elapsed
     does it get promoted to the young region.
  4. Sequential scans load pages and access them once per page,
     all within milliseconds → they never leave the old region
     → they get evicted first → hot OLTP pages stay in young region.
```

### Replacement Policy Comparison

| Policy | Used By | Sequential Flood Resistance | Overhead | Notes |
|--------|---------|----------------------------|----------|-------|
| Pure LRU | (Textbook only) | None | Low | Doubly-linked list, O(1) ops |
| Clock / Second-Chance | PostgreSQL | Moderate (usage_count cap) | Very low | No linked list, just a counter per frame |
| LRU-2 | SQL Server | Good | Moderate | Tracks 2 timestamps per page |
| Young/Old LRU | InnoDB | Good | Low | Split list with time-based promotion |
| ARC (Adaptive) | ZFS, IBM DB2 | Excellent | Higher | Maintains ghost lists, self-tuning |

---

## 7. Disk I/O: The Bottleneck

### The Storage Hierarchy

The fundamental constraint of database design is the speed gap between memory and storage:

```
┌─────────────────────────────────────────────────────────────────┐
│                  STORAGE HIERARCHY                                │
├──────────────┬──────────────┬───────────────┬───────────────────┤
│  Level       │ Latency      │ Throughput     │ $/GB (approx.)   │
├──────────────┼──────────────┼───────────────┼───────────────────┤
│ L1 Cache     │ ~1 ns        │ ~1 TB/s       │ $$$$$$            │
│ L2 Cache     │ ~4 ns        │ ~500 GB/s     │ $$$$$             │
│ L3 Cache     │ ~10 ns       │ ~200 GB/s     │ $$$$              │
│ DRAM         │ ~100 ns      │ ~50 GB/s      │ ~$3-5/GB          │
│ NVMe SSD     │ ~10-100 μs   │ ~3-7 GB/s     │ ~$0.08-0.15/GB   │
│ SATA SSD     │ ~50-200 μs   │ ~0.5 GB/s     │ ~$0.06-0.10/GB   │
│ HDD (7200)   │ ~2-10 ms     │ ~0.15 GB/s    │ ~$0.02/GB         │
└──────────────┴──────────────┴───────────────┴───────────────────┘

Ratio of DRAM to HDD random access: ~100,000x
Ratio of DRAM to NVMe SSD random access: ~100-1000x

This is why the buffer pool exists: DRAM is 100-100,000x faster.
```

### Sequential vs Random I/O

The distinction between sequential and random I/O is the single most important concept in database I/O performance.

```
SEQUENTIAL I/O: Reading/writing contiguous disk blocks in order.

  Disk:  [Page 1][Page 2][Page 3][Page 4][Page 5][Page 6]
          ▲       ▲       ▲       ▲       ▲       ▲
          └───────┴───────┴───────┴───────┴───────┘
          One seek, then continuous reading.

  HDD:  ~150-200 MB/s  (limited by rotational speed)
  SSD:  ~500-7000 MB/s (limited by interface bandwidth)

RANDOM I/O: Reading/writing scattered disk blocks.

  Disk:  [Page 1][    ][    ][Page 734][    ][Page 2891][    ]
          ▲                   ▲                ▲
          │                   │                │
          └── seek ──────────►└── seek ────────┘
          Each read requires repositioning.

  HDD:  ~100-200 IOPS  (~0.8-1.6 MB/s for 8 KB pages)
        Bottleneck: seek time (4-10 ms per seek)

  SSD:  ~10,000-1,000,000 IOPS  (80-8000 MB/s for 8 KB pages)
        Much better, but still ~100x slower than sequential.
```

This is why:
- **Full table scans** (sequential) can be faster than **index scans** (random) for large result sets
- **B+Tree leaf pages** are linked for sequential range scans
- **WAL** writes sequentially (append-only) for maximum write throughput
- **LSM-Trees** convert random writes to sequential writes

### The I/O Stack

When a database issues a read or write, the request passes through multiple layers, each of which can buffer, reorder, or batch operations:

```
┌────────────────────────────────────────────────────────────┐
│  DATABASE PROCESS                                           │
│  pread(fd, buffer, 8192, offset)                           │
└─────────────────────────┬──────────────────────────────────┘
                          │  System call
                          ▼
┌────────────────────────────────────────────────────────────┐
│  OS KERNEL (VFS Layer)                                      │
│  - Check page cache (OS buffer cache)                      │
│  - If cached: copy to userspace (no disk I/O!)             │
│  - If not cached: schedule disk I/O, add to page cache     │
│                                                              │
│  With O_DIRECT: skip page cache entirely                   │
└─────────────────────────┬──────────────────────────────────┘
                          │  Block I/O request
                          ▼
┌────────────────────────────────────────────────────────────┐
│  I/O SCHEDULER (noop / deadline / mq-deadline / bfq)       │
│  - Merge adjacent requests                                 │
│  - Reorder for seek optimization (HDD)                     │
│  - NVMe often uses "none" scheduler (device handles it)    │
└─────────────────────────┬──────────────────────────────────┘
                          │  Merged/reordered requests
                          ▼
┌────────────────────────────────────────────────────────────┐
│  DEVICE DRIVER → DISK CONTROLLER                           │
│  - HDD: Translate to head seek + rotational wait + read    │
│  - SSD: Translate to flash page read from NAND chip        │
│  - NVMe: Direct PCIe submission queue, no legacy overhead  │
│                                                              │
│  VOLATILE WRITE CACHE (danger!)                            │
│  - Most drives have a RAM write cache (128 MB – 4 GB)      │
│  - Writes may be acknowledged before hitting stable storage│
│  - Power loss → DATA LOSS unless battery-backed (BBU)      │
│  - Database fsync() forces cache flush for durability      │
└────────────────────────────────────────────────────────────┘
```

### I/O Syscalls Used by Databases

| Syscall | Purpose | Used By |
|---------|---------|---------|
| `read()` / `pread()` | Read data from file. `pread` is thread-safe (includes offset). | All databases for buffered reads |
| `write()` / `pwrite()` | Write data to file. Goes to OS page cache unless O_DIRECT. | All databases for buffered writes |
| `fsync()` / `fdatasync()` | Force flush to stable storage. `fdatasync` skips metadata flush. | Critical for WAL durability |
| `open(O_DIRECT)` | Bypass OS page cache. Database manages its own caching. | InnoDB, Oracle, some PostgreSQL configs |
| `open(O_DSYNC)` | Every write is implicitly durable (like write + fdatasync). | Some WAL implementations |
| `mmap()` | Map file directly into process address space. OS manages paging. | SQLite (optional), MongoDB (WiredTiger mmapv1 legacy), LMDB |
| `io_uring` | Async I/O interface (Linux 5.1+). Batch submissions, kernel-side polling. | Newer databases: ScyllaDB, TiKV, PostgreSQL 16+ (experimental) |
| `posix_fadvise()` | Hint to OS about access patterns (sequential, random, willneed, dontneed). | PostgreSQL (for sequential scans and prefetching) |

### Direct I/O vs Buffered I/O

```
BUFFERED I/O (default):

  Database ──write()──► OS Page Cache ──(eventually)──► Disk
  Database ◄──read()─── OS Page Cache ◄──(on miss)──── Disk

  Pros:
  - OS provides a "free" second-level cache
  - read-ahead and write-behind handled by kernel
  - Simpler implementation

  Cons:
  - Data is copied twice: disk → OS cache → buffer pool
  - Database and OS compete for memory management decisions
  - OS may evict pages the database considers hot
  - fsync() must flush the OS cache, which can be slow

DIRECT I/O (O_DIRECT):

  Database ──write()──► Disk    (bypasses OS cache)
  Database ◄──read()─── Disk    (bypasses OS cache)

  Pros:
  - No double-caching (saves memory)
  - Database has full control over caching decisions
  - Predictable fsync() behavior (nothing to flush in OS cache)
  - Better for databases that carefully manage their own buffer pool

  Cons:
  - Alignment requirements (reads/writes must be sector-aligned)
  - No OS read-ahead (database must prefetch manually)
  - No OS write coalescing (database must batch writes)
  - More complex implementation

  Who uses O_DIRECT?
  - InnoDB (innodb_flush_method = O_DIRECT, recommended for Linux)
  - Oracle
  - ScyllaDB
  - RocksDB (optional)

  Who uses buffered I/O?
  - PostgreSQL (relies on OS page cache as second-level cache)
  - SQLite
  - DuckDB
```

### Prefetching and Read-Ahead

Databases often know which pages they'll need before they need them. Prefetching loads pages into the buffer pool asynchronously, overlapping I/O with computation.

```
WITHOUT PREFETCH (synchronous):

  CPU:  [process page 1] [wait] [process page 2] [wait] [process page 3]
  I/O:                  [read 2]                [read 3]

  Total time: 3 × (process + I/O)  ← I/O latency fully exposed

WITH PREFETCH (asynchronous):

  CPU:  [process page 1] [process page 2] [process page 3]
  I/O:  [read 2][read 3][read 4][read 5]

  Total time: 3 × process + 1 × I/O  ← I/O hidden behind processing
```

PostgreSQL examples:
- `effective_io_concurrency`: tells PG how many concurrent I/O requests to issue for bitmap heap scans (default 1; set to 200 for NVMe SSDs).
- Sequential scans use `posix_fadvise(POSIX_FADV_WILLNEED)` to hint the OS to read ahead.
- PG 16+ introduced `io_combine_limit` for batching I/O requests.

---

## 8. Write Path: From Memory to Durable Storage

### The Durability Problem

When a transaction commits, the database promises the data will survive crashes. But writing to disk is slow. The challenge: make writes durable without blocking the transaction for disk I/O.

```
NAIVE APPROACH (write data pages on commit):

  1. Transaction modifies pages in buffer pool (fast, in-memory)
  2. On COMMIT: flush every dirty page to disk (SLOW!)
     - Random I/O: modified pages are scattered across the file
     - Write amplification: changing 1 byte still writes 8 KB page
     - If 10 pages were modified, that's 10 random writes
     - Latency: ~50-100 ms on HDD, ~1-5 ms on SSD

  This is unacceptable for OLTP workloads (need < 1 ms commits).

WRITE-AHEAD LOGGING APPROACH (the actual solution):

  1. Transaction modifies pages in buffer pool (fast, in-memory)
  2. On COMMIT: write a small WAL record describing the change (FAST!)
     - Sequential I/O: WAL is append-only
     - Small writes: only the delta, not the whole page
     - One fsync for many transactions (group commit)
     - Latency: ~0.01-0.5 ms
  3. Dirty pages are flushed to disk LATER by background writer
     (not on the commit path)
```

### The Checkpoint Process

Dirty pages can't stay in the buffer pool forever -- the WAL would grow unboundedly, and crash recovery would take hours. **Checkpoints** periodically flush dirty pages to disk and advance the WAL recovery start point.

```
CHECKPOINT PROCESS:

  Time ─────────────────────────────────────────────────────────►

  WAL:   [rec1][rec2][rec3][rec4][rec5][rec6][rec7][rec8][rec9]...
                             ▲                       ▲
                          CKPT #1                  CKPT #2

  At CHECKPOINT #1:
  1. Mark the current WAL position (REDO point)
  2. Flush ALL dirty buffer pool pages to their data files
  3. Record "checkpoint at WAL position X" in pg_control
  4. WAL before position X can now be recycled

  CRASH RECOVERY:
  - Find last checkpoint in pg_control
  - Replay WAL from that checkpoint's REDO point forward
  - Pages that were already flushed will have LSN >= WAL record LSN,
    so those WAL records are skipped (idempotent replay)

  PostgreSQL parameters:
    checkpoint_timeout = 5min     (max time between checkpoints)
    max_wal_size = 1GB            (WAL growth triggers checkpoint)
    checkpoint_completion_target = 0.9
      (spread I/O over 90% of the checkpoint interval
       to avoid I/O spikes)
```

### Background Writer vs Checkpointer

Most databases have background processes that flush dirty pages without waiting for a checkpoint:

```
PostgreSQL:

  BACKGROUND WRITER (bgwriter)
  ─────────────────────────────
  - Runs continuously
  - Scans buffer pool, flushes pages with low usage_count
  - Purpose: maintain a supply of free (clean) frames
  - Avoids "victim is dirty" stalls during page replacement
  - Parameters: bgwriter_delay (200ms), bgwriter_lru_maxpages (100)

  CHECKPOINTER
  ─────────────
  - Runs periodically (checkpoint_timeout) or when WAL grows
  - Flushes ALL dirty pages
  - Purpose: advance WAL recovery point, limit recovery time
  - Spread writes over time (checkpoint_completion_target)

InnoDB:

  PAGE CLEANER THREADS (innodb_page_cleaners)
  ────────────────────────────────────────────
  - Multiple threads (default = 4)
  - Continuously flush dirty pages
  - Adaptive flushing: flush rate increases as dirty page
    percentage approaches innodb_max_dirty_pages_pct (75%)
  - Also handles the "sharp checkpoint" at shutdown
```

---

## 9. The Write-Ahead Log (WAL)

### WAL Protocol (ARIES)

The WAL protocol is governed by a simple but ironclad rule called the **WAL rule** (from the ARIES recovery algorithm):

> **Before a dirty page is flushed to disk, all WAL records that describe changes to that page must first be flushed to the WAL.**

This ensures that, after a crash, the WAL contains enough information to reconstruct any change that might have been lost.

```
THE THREE RULES OF WAL:

  1. WAL RULE (Write-Ahead):
     Before flushing data page P to disk,
     flush all WAL records with LSN <= P.lsn to WAL.

  2. COMMIT RULE:
     A transaction is not "committed" until its COMMIT
     WAL record reaches stable storage.

  3. REDO RULE:
     WAL records contain enough information to redo
     the operation if the data page was not yet flushed.

Sequence for a single UPDATE:

  1. Acquire locks
  2. Modify page in buffer pool
  3. Write WAL record: (LSN=17, txn=42, page=(5,12),
                        op=UPDATE, before_image=..., after_image=...)
  4. Update page LSN in page header: page.lsn = 17
  5. Transaction continues (page stays dirty in buffer pool)
  6. On COMMIT: write COMMIT WAL record, fsync WAL → committed!
  7. Later: background writer flushes page (5,12) to disk
     (WAL record 17 was already on disk, so WAL rule is satisfied)
```

### WAL Record Structure

```
┌────────────────────────────────────────────────────────────────┐
│                     WAL RECORD                                  │
├──────────┬──────────┬──────────┬──────────┬───────────────────┤
│ LSN      │ Txn ID   │ Prev LSN │ Type     │ Payload            │
│ (8 B)    │ (4 B)    │ (8 B)    │ (1 B)    │ (variable)         │
├──────────┴──────────┴──────────┴──────────┴───────────────────┤
│                                                                 │
│ LSN: Log Sequence Number. Monotonically increasing. Used to    │
│      determine the order of operations and whether a page is   │
│      up-to-date (compare page LSN with WAL record LSN).        │
│                                                                 │
│ Prev LSN: LSN of the previous WAL record for this transaction. │
│           Forms a per-transaction linked list for undo/abort.  │
│                                                                 │
│ Type: INSERT, UPDATE, DELETE, COMMIT, ABORT, CHECKPOINT, etc.  │
│                                                                 │
│ Payload: Depends on type.                                      │
│   - PHYSIOLOGICAL record (PostgreSQL):                         │
│     Page reference + redo function ID + data                   │
│   - PHYSICAL record: full before/after images of changed bytes │
│   - LOGICAL record: high-level operation description           │
│                                                                 │
└────────────────────────────────────────────────────────────────┘
```

### Physiological Logging (PostgreSQL's Approach)

PostgreSQL uses **physiological logging**: records are physical at the page level (they reference a specific page) but logical within the page (they describe the operation, not the exact byte changes).

```
PHYSICAL logging:       "On page (5,12), change bytes 1024-1056 from X to Y"
LOGICAL logging:        "Insert row (id=42, name='Alice') into table 'users'"
PHYSIOLOGICAL logging:  "On page (5,12), perform heap_insert with data (42, 'Alice')"

                  Physical         Physiological        Logical
                  ─────────        ─────────────        ────────
  Record size     Large (full      Medium (page ref     Small (just the
                  byte diffs)      + operation data)    operation)

  Redo speed      Very fast (just  Fast (replay the     Slow (re-execute
                  apply bytes)     operation on page)   the query)

  Works after     Always           Only if page layout  Only if schema
  schema change?                   unchanged            unchanged

  Page-level      Yes              Yes                  No
  idempotent?
```

### Full-Page Writes (FPW) / Double-Write Buffer

A critical problem: what if the OS writes only part of an 8 KB page before crashing? (The OS writes in 4 KB filesystem blocks, but a database page is 8 KB.) This is a **torn page** or **partial write**.

```
TORN PAGE PROBLEM:

  Database page (8 KB) = [first 4 KB half] [second 4 KB half]

  OS writes first 4 KB → CRASH → second 4 KB never written.

  Result: page on disk has new first half + old second half.
  The WAL record says "apply change to page" but the page is
  in an inconsistent state. Redo might produce garbage.

PostgreSQL solution: FULL PAGE WRITES
────────────────────────────────────
  After each checkpoint, the FIRST time a page is modified,
  write the ENTIRE page image into the WAL record.

  During recovery: if a page is torn, restore the full page
  image from WAL, then apply subsequent WAL records on top.

  Cost: WAL becomes much larger right after a checkpoint
  (every modified page writes its full 8 KB image once).
  Controlled by: full_page_writes = on (default, do NOT turn off)

InnoDB solution: DOUBLE-WRITE BUFFER
─────────────────────────────────────
  Before flushing dirty pages to their final locations, write
  them to a sequential "doublewrite buffer" area on disk.

  1. Write pages to doublewrite buffer (sequential, fast)
  2. fsync doublewrite buffer
  3. Write pages to their actual locations (random I/O)

  Recovery: if a page is torn, copy the intact version from
  the doublewrite buffer. Then apply WAL records.

  Cost: every page write happens twice, but the first write
  is sequential so the overhead is ~5-10%.
```

### Group Commit

The `fsync()` call is expensive (~0.1–10 ms depending on hardware). Databases amortize this cost by grouping multiple transactions into a single fsync.

```
WITHOUT GROUP COMMIT:

  Txn 1: write WAL → fsync()   (0.5 ms)
  Txn 2: write WAL → fsync()   (0.5 ms)
  Txn 3: write WAL → fsync()   (0.5 ms)

  Total: 1.5 ms for 3 transactions

WITH GROUP COMMIT:

  Txn 1: write WAL ─┐
  Txn 2: write WAL ──┼─→ single fsync()  (0.5 ms)
  Txn 3: write WAL ─┘

  Total: 0.5 ms for 3 transactions

  The leader transaction performs the fsync, and all follower
  transactions that arrived during the write window are
  committed together.

PostgreSQL: commit_delay (default 0) adds a deliberate microsecond
  wait to gather more transactions per fsync. Useful at very high
  throughput. commit_siblings (default 5) is the minimum number of
  concurrent transactions before applying the delay.

InnoDB: innodb_flush_log_at_trx_commit:
  = 1: fsync on every commit (safest, default)
  = 2: write to OS cache on commit, fsync once per second
       (1 sec data loss risk on crash)
  = 0: write and fsync once per second
       (up to 1 sec data loss risk)
```

---

## 10. Checksums and Data Integrity

### The Silent Corruption Problem

Hardware can silently corrupt data. Bit flips in RAM, firmware bugs in SSDs, cosmic rays, bad sectors on HDDs. Without checksums, the database reads corrupted data and returns wrong results -- silently.

```
CORRUPTION SOURCES:

  ┌──────────────────┬───────────────────────────────────────────┐
  │ Source            │ Description                               │
  ├──────────────────┼───────────────────────────────────────────┤
  │ Bit rot          │ Magnetic media degrades over years         │
  │ Firmware bugs    │ SSD controller writes wrong block          │
  │ RAM bit flips    │ Cosmic rays, heat (ECC RAM mitigates)      │
  │ Phantom writes   │ Disk reports success but didn't write      │
  │ Misdirected I/O  │ Write lands on wrong block                 │
  │ Kernel bugs      │ Filesystem or block layer corrupts data    │
  │ Torn writes      │ Partial page write during crash            │
  └──────────────────┴───────────────────────────────────────────┘
```

### Page-Level Checksums

| Database | Checksum Method | Enabled By Default? | Notes |
|----------|----------------|---------------------|-------|
| PostgreSQL | CRC-32C (hardware-accelerated) | No (`initdb --data-checksums`) | Cannot be enabled after creation without `pg_checksums` (offline) |
| InnoDB | CRC-32C (default), or innodb_checksum_algorithm | Yes | Stored in FIL header and trailer |
| SQL Server | Page checksum | Yes (after 2005) | `CHECKSUM` option per database |
| SQLite | Per-page checksum in WAL mode | Optional | Compile-time option |
| Oracle | DB_BLOCK_CHECKSUM | Configurable (TYPICAL/FULL/OFF) | TYPICAL checks only on writes |

### How Page Checksums Work

```
WRITE PATH:
  1. Page modified in buffer pool
  2. Before flushing to disk: compute checksum over page bytes
     (excluding the checksum field itself)
  3. Store checksum in page header
  4. Write page to disk

READ PATH:
  1. Read page from disk into buffer pool frame
  2. Compute checksum over page bytes
  3. Compare with stored checksum
  4. If mismatch → DATA CORRUPTION DETECTED
     - PostgreSQL: ERROR "invalid page in block X of relation Y"
     - InnoDB: attempts to read from doublewrite buffer;
       if that fails, reports corruption error
  5. If match → page is intact, continue
```

### End-to-End Checksums

Page checksums only protect data at rest. For full protection, checksums should verify data at every layer:

```
  Application ──► Database ──► OS ──► Disk Controller ──► NAND Flash
       │              │          │          │                  │
       │          page cksum  filesystem  T10-DIF/PI       ECC per
       │                      metadata    (SCSI)           flash cell
       │              ▲          ▲          ▲                  ▲
       │              │          │          │                  │
       └──── end-to-end data integrity chain ─────────────────┘

  Gaps in the chain = opportunities for silent corruption.

  Best practice: enable database checksums + ECC RAM + filesystem
  with checksums (ZFS, btrfs) + battery-backed write cache on RAID.
```

---

## 11. Storage Engine Architectures Compared

### Architecture Summary

```
┌──────────────────────────────────────────────────────────────────────┐
│                   STORAGE ENGINE ARCHITECTURES                        │
├──────────────┬───────────────────────┬───────────────────────────────┤
│              │  B-TREE / PAGE-BASED   │  LSM-TREE BASED               │
├──────────────┼───────────────────────┼───────────────────────────────┤
│ Examples     │ PostgreSQL, InnoDB,    │ RocksDB, LevelDB, Cassandra, │
│              │ SQL Server, SQLite     │ HBase, CockroachDB (storage) │
├──────────────┼───────────────────────┼───────────────────────────────┤
│ Write path   │ In-place update:       │ Out-of-place (append-only):  │
│              │ Find page → modify →  │ Write to memtable → flush    │
│              │ mark dirty             │ to sorted SST files on disk  │
├──────────────┼───────────────────────┼───────────────────────────────┤
│ Read path    │ B-Tree traversal →    │ Check memtable → check L0    │
│              │ leaf page → row       │ SSTables → L1 → ... → Ln    │
├──────────────┼───────────────────────┼───────────────────────────────┤
│ Write amp.   │ Moderate (page-level  │ High (compaction rewrites     │
│              │ writes for small      │ data multiple times)          │
│              │ changes)              │                               │
├──────────────┼───────────────────────┼───────────────────────────────┤
│ Read amp.    │ Low (single B-Tree    │ Higher (may check multiple    │
│              │ traversal)            │ levels; Bloom filters help)   │
├──────────────┼───────────────────────┼───────────────────────────────┤
│ Space amp.   │ Low-moderate (dead    │ Moderate (stale versions      │
│              │ tuples until VACUUM)  │ until compaction)             │
├──────────────┼───────────────────────┼───────────────────────────────┤
│ Best for     │ Read-heavy OLTP,      │ Write-heavy workloads,        │
│              │ mixed workloads       │ time-series, logging          │
└──────────────┴───────────────────────┴───────────────────────────────┘
```

### Write Amplification Deep Dive

Write amplification (WA) is the ratio of bytes written to disk vs bytes written by the application. Lower is better.

```
B-TREE WRITE AMPLIFICATION:

  Application writes 100 bytes (one row update):
  1. WAL record: ~150 bytes (record header + payload)
  2. Full page write (first after checkpoint): 8,192 bytes (!)
  3. Data page flush: 8,192 bytes
  4. Index page(s) update: 8,192 bytes per index

  Total disk writes for 100 bytes of data:
    ~150 + 8,192 + 8,192 + 8,192 = ~24,726 bytes
    Write amplification = ~247x (worst case, right after checkpoint)

  Amortized (after first FPW):
    ~150 + 8,192 + 8,192 = ~16,534 bytes
    Write amplification = ~165x

  In practice, many changes accumulate on the same page before
  flush, so amortized WA is typically 10-30x.

LSM-TREE WRITE AMPLIFICATION:

  Application writes 100 bytes:
  1. WAL record: ~150 bytes
  2. Memtable → L0 SST flush: 100 bytes (sorted, compressed)
  3. L0 → L1 compaction: 100 bytes (merged + rewritten)
  4. L1 → L2 compaction: 100 bytes
  ... and so on for each level

  With 10:1 size ratio and 5 levels:
  Total rewrites: 150 + 100 × 5 = 650 bytes
  Write amplification = ~6.5x (much lower than B-Tree!)

  But: compaction is bursty and uses significant I/O bandwidth.
```

### The Three Amplification Factors

Every storage engine trades off between three amplification factors. You cannot optimize all three simultaneously.

```
                    WRITE AMPLIFICATION
                          ▲
                         / \
                        /   \
                       /     \
                      / trade- \
                     /   offs   \
                    /             \
   READ           /               \           SPACE
   AMPLIFICATION ◄─────────────────► AMPLIFICATION

  B-Tree:    Moderate write amp | Low read amp | Low-moderate space amp
  LSM-Tree:  Low write amp | Higher read amp | Moderate space amp
  B-epsilon: Balanced (between B-Tree and LSM)

  There is no free lunch: improving one factor worsens another.
```

### When to Choose What

| Workload | Recommended Architecture | Why |
|----------|------------------------|-----|
| General OLTP (mixed read/write) | B-Tree (PostgreSQL, InnoDB) | Good read performance, reasonable write throughput, mature tooling |
| Write-heavy (IoT, logs, events) | LSM-Tree (RocksDB, Cassandra) | High write throughput, sequential I/O |
| Read-heavy analytics | Columnar (Parquet, DuckDB) | Compression, column pruning, vectorized scans |
| Key-value cache/store | LSM or hash (RocksDB, Bitcask) | Simple access pattern, high throughput |
| Embedded / single-file | B-Tree (SQLite) | Simple, zero-config, portable |

---
