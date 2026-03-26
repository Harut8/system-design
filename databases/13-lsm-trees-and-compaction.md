# LSM Trees and Compaction: A Staff-Engineer Deep Dive

A comprehensive guide to Log-Structured Merge Trees -- the dominant write-optimized storage engine architecture powering RocksDB, LevelDB, Cassandra, HBase, ScyllaDB, CockroachDB, and dozens of other modern databases. Covers internal data structures, write/read paths, compaction strategies, write amplification analysis, Bloom filters, production tuning, and cutting-edge research optimizations. Includes ASCII diagrams, complexity analysis, and real-world guidance.

---

## Table of Contents

1. [B-Trees vs LSM Trees: Fundamental Trade-offs](#1-b-trees-vs-lsm-trees-fundamental-trade-offs)
2. [LSM Tree Architecture (Detailed)](#2-lsm-tree-architecture-detailed)
3. [The Read Path](#3-the-read-path)
4. [Compaction Strategies (Core)](#4-compaction-strategies-core)
5. [Write Amplification Analysis](#5-write-amplification-analysis)
6. [RocksDB Deep Dive](#6-rocksdb-deep-dive)
7. [LevelDB](#7-leveldb)
8. [Cassandra's LSM Implementation](#8-cassandras-lsm-implementation)
9. [Bloom Filters in LSM Trees](#9-bloom-filters-in-lsm-trees)
10. [LSM Tree Optimizations and Research](#10-lsm-tree-optimizations-and-research)
11. [Comparison Tables](#11-comparison-tables)

---

## 1. B-Trees vs LSM Trees: Fundamental Trade-offs

### The Two Dominant Storage Engine Paradigms

Every database storage engine must solve the same fundamental problem: maintain an ordered mapping of keys to values on durable storage. The two dominant approaches -- B-Trees and LSM Trees -- make fundamentally different trade-offs about where to spend I/O.

```
B-TREE: UPDATE IN PLACE                    LSM TREE: OUT-OF-PLACE (APPEND)
========================                    ================================

Write "key=42, val=X":                      Write "key=42, val=X":

  ┌──────────┐                                ┌──────────────┐
  │ Root Node│                                │   MemTable   │  ← Write here (RAM)
  │ [30|60]  │                                │  (sorted)    │
  └──┬───┬───┘                                └──────┬───────┘
     │   │                                           │ Flush when full
  ┌──▼┐ ┌▼──────┐                                   ▼
  │...│ │ Leaf  │ ← FIND the page             ┌──────────────┐
  └───┘ │[40|50]│ ← READ it                   │  SSTable L0  │  Sorted, immutable file
        │       │ ← MODIFY in memory          ├──────────────┤
        │[42=X] │ ← WRITE it back             │  SSTable L1  │  Merged, sorted files
        └───────┘   (random I/O)              ├──────────────┤
                                              │  SSTable L2  │  Larger sorted runs
  Random reads + random writes                └──────────────┘
  Good for reads, expensive writes              Sequential writes only
                                                Good for writes, reads cost more
```

### The RUM Conjecture

The **RUM Conjecture** (Read, Update, Memory) states that a storage data structure can optimize for at most two of three overhead dimensions. Minimizing one amplification necessarily increases at least one of the others.

```
THE RUM CONJECTURE
==================

                        Read Overhead
                             /\
                            /  \
                           /    \
                          / B-Tree\
                         /  (read  \
                        /  optimized)\
                       /──────────────\
                      /                \
                     /    IMPOSSIBLE    \
                    /     REGION:       \
                   /   Optimize all 3    \
                  /                       \
                 /─────────────────────────\
                /             |             \
    Update     /   LSM Tree   |   Hash Index \   Memory
    Overhead  /  (write opt.) | (memory opt.) \  Overhead
             ──────────────────────────────────

  ┌─────────────────┬───────────────┬───────────────┬───────────────┐
  │ Structure       │ Read Amp (R)  │ Write Amp (W) │ Space Amp (S) │
  ├─────────────────┼───────────────┼───────────────┼───────────────┤
  │ B+Tree          │ LOW  (1-4)    │ HIGH (10-30)  │ LOW  (~1.5x)  │
  │ LSM (Leveled)   │ MED  (2-10)  │ MED  (10-30)  │ LOW  (~1.1x)  │
  │ LSM (Tiered)    │ HIGH (5-30)  │ LOW  (3-5)    │ HIGH (~2-3x)  │
  │ Hash Index      │ LOW  (1)     │ LOW  (1-2)    │ HIGH (varies) │
  │ Heap/Log        │ HIGH (N)     │ LOW  (1)      │ LOW  (~1x)    │
  └─────────────────┴───────────────┴───────────────┴───────────────┘
```

### Amplification Factors Defined

```
THREE AMPLIFICATIONS
====================

READ AMPLIFICATION:
  Number of I/O operations needed to satisfy a single read.

  B-Tree:  1 seek (point lookup via index)
           = traverse tree height + 1 leaf page read
           Typically 2-4 disk reads (root/internal cached)

  LSM:     Must check MemTable + each level that might contain the key.
           Bloom filters reduce this but don't eliminate it.
           Worst case: 1 (memtable) + L0_files + L1 + L2 + ... + LN
           With Bloom filters: ~1-2 disk reads for point lookups

WRITE AMPLIFICATION:
  Total bytes written to storage / bytes of original data.

  B-Tree:  Write 1 row (100 bytes) → rewrite 8KB/16KB page = 80-160x
           Plus WAL entry. But page is often already dirty in buffer pool.
           Effective WA: ~2-5x for buffered workloads, up to 30x worst case

  LSM:     Write goes to WAL + MemTable (in RAM) → sequential flush.
           But data gets rewritten during compaction.
           Leveled: ~10-30x total across all levels
           Tiered:  ~3-5x total

SPACE AMPLIFICATION:
  Total storage used / logical data size.

  B-Tree:  ~1.5x (pages are ~67% full on average due to splits)

  LSM Leveled:  ~1.1x (only 1 extra level being compacted)
  LSM Tiered:   ~2-3x (multiple copies at same level before merge)
```

### When to Use B-Trees vs LSM Trees

| Workload | Best Choice | Why |
|---|---|---|
| **Read-heavy OLTP** (90% reads) | B-Tree | Lower read amplification, predictable latency |
| **Write-heavy** (logging, IoT, time-series) | LSM Tree | Sequential writes, much lower write amp |
| **Mixed OLTP** (50/50 read/write) | Either (depends) | LSM if write latency matters; B-Tree if read latency matters |
| **Range scans dominant** | B-Tree (slight edge) | Sorted pages, no multi-level merge overhead |
| **SSD-based storage** | LSM Tree (often) | Reduced random writes extend SSD lifetime |
| **Space-constrained** | LSM Leveled | Best space amplification (~1.1x) |
| **Bulk ingestion** | LSM Tree | Sequential writes, can batch memtable flushes |
| **Point lookups dominant** | B-Tree | Single seek path, cached internal nodes |

### Industry Adoption

```
B-TREE BASED                          LSM-TREE BASED
===========                           ==============

PostgreSQL (heap + B-tree)            RocksDB (Facebook/Meta)
MySQL/InnoDB (clustered B+tree)       LevelDB (Google)
Oracle (B-tree indexes)               Cassandra (Apache)
SQL Server (B-tree / columnstore)     HBase (Apache)
MongoDB WiredTiger (B-tree mode)      ScyllaDB
SQLite (B-tree)                       CockroachDB (uses RocksDB/Pebble)
                                      TiKV/TiDB (uses RocksDB)
                                      BadgerDB (Go, inspired by WiscKey)
                                      DynamoDB (uses custom LSM)
                                      InfluxDB (TSI uses LSM)
                                      MongoDB WiredTiger (LSM mode)
                                      YugabyteDB (uses RocksDB)
                                      Pebble (CockroachDB, Go)
```

---

## 2. LSM Tree Architecture (Detailed)

### High-Level Architecture

```
LSM TREE ARCHITECTURE
=====================

   Client Write (PUT key, value)
          │
          ▼
  ┌───────────────────────────────────────────────┐
  │                WRITE PATH                      │
  │                                                │
  │  1. Append to WAL (Write-Ahead Log)           │
  │     Sequential write to disk for durability    │
  │                                                │
  │  2. Insert into MemTable (in-memory sorted)   │
  │     Skip list or red-black tree                │
  │     ACK to client here (write is "done")      │
  │                                                │
  └───────────────────┬───────────────────────────┘
                      │
                      │ When MemTable reaches size threshold
                      │ (typically 64MB in RocksDB)
                      ▼
  ┌──────────────────────────────────────────────┐
  │  3. MemTable → Immutable MemTable            │
  │     New empty MemTable created for new writes │
  │     Background thread flushes immutable to    │
  │     disk as SSTable                           │
  └───────────────────┬──────────────────────────┘
                      │
                      ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │                     ON-DISK STRUCTURE                            │
  │                                                                  │
  │  Level 0 (L0):  SST_001  SST_002  SST_003  SST_004             │
  │                 ─────────────────────────────────                │
  │                 Unsorted! Key ranges MAY OVERLAP                 │
  │                 Each file is individually sorted                 │
  │                 Flushed directly from MemTable                   │
  │                                                                  │
  │  Level 1 (L1):  ┌──────┬──────┬──────┬──────┬──────┐          │
  │                 │ a-d  │ e-h  │ i-m  │ n-r  │ s-z  │          │
  │                 └──────┴──────┴──────┴──────┴──────┘          │
  │                 Sorted, NON-OVERLAPPING key ranges              │
  │                 Total size ~target (e.g., 256MB)                │
  │                                                                  │
  │  Level 2 (L2):  ┌────┬────┬────┬────┬────┬────┬────┬────┐    │
  │                 │a-b │c-d │e-f │g-i │j-m │n-p │q-s │t-z │    │
  │                 └────┴────┴────┴────┴────┴────┴────┴────┘    │
  │                 Sorted, NON-OVERLAPPING. ~10x size of L1       │
  │                                                                  │
  │  Level 3 (L3):  ┌──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┐   │
  │                 │..│..│..│..│..│..│..│..│..│..│..│..│..│   │
  │                 └──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┘   │
  │                 ~10x size of L2                                  │
  │                                                                  │
  └──────────────────────────────────────────────────────────────────┘

  Size ratios (Leveled Compaction with ratio T=10):
    L1: 256 MB     (target_file_size * num_files)
    L2: 2.56 GB    (10x L1)
    L3: 25.6 GB    (10x L2)
    L4: 256 GB     (10x L3)

  For 1 TB of data:
    L0: ~256 MB (4 files x 64MB)
    L1: 256 MB
    L2: 2.56 GB
    L3: 25.6 GB
    L4: 256 GB
    L5: ~715 GB (partially filled)
    Total levels: ~5-6
```

### MemTable: The In-Memory Sorted Buffer

The MemTable is the write buffer -- an in-memory sorted data structure where all writes land first. Common implementations:

```
MEMTABLE IMPLEMENTATIONS
=========================

1. SKIP LIST (most common -- used by RocksDB, LevelDB)
   ─────────────────────────────────────────────────────

   Level 3:  ────────────────────────────── 42 ─────────────────────── NIL
   Level 2:  ──── 12 ─────────────────────  42 ──── 67 ──────────── NIL
   Level 1:  ──── 12 ──── 25 ──── 33 ────  42 ──── 67 ──── 78 ─── NIL
   Level 0:  3 ── 12 ── 15 ── 25 ── 33 ── 42 ── 55 ── 67 ── 78 ── 99 ── NIL

   Properties:
   - O(log n) insert, search, delete (probabilistic)
   - Lock-free concurrent reads (CAS for inserts)
   - No rebalancing needed (unlike RB-tree)
   - Cache-friendly sequential iteration for flush
   - Memory overhead: ~1.33x per entry (pointers per level)

   Why skip lists win for MemTables:
   - Concurrent writes without global lock
   - In-order iteration for SSTable flush is O(n)
   - Simple implementation

2. RED-BLACK TREE (used by some implementations)
   ──────────────────────────────────────────────

            ┌───42(B)───┐
         ┌─25(R)─┐   ┌─67(R)─┐
        12(B)  33(B) 55(B)  78(B)

   Properties:
   - O(log n) guaranteed (not probabilistic)
   - Requires rebalancing on insert/delete
   - Typically needs mutex for concurrent access
   - Slightly less memory overhead per entry

   Used by: C++ std::map-based MemTables

3. HASH SKIP LIST (RocksDB option)
   ────────────────────────────────
   Hash table of skip lists, one per hash bucket.
   Optimizes prefix lookups but loses total ordering.

4. VECTOR + SORT (RocksDB "Vector" MemTable)
   ──────────────────────────────────────────
   Append-only vector, sorted on flush.
   Best for bulk-load workloads (no reads during writes).
```

### MemTable Size Threshold and Rotation

```
MEMTABLE LIFECYCLE
==================

  Active MemTable          Immutable MemTable          SSTable on Disk
  (accepts writes)         (read-only, being flushed)  (permanent)

  ┌─────────────┐         ┌─────────────┐             ┌─────────────┐
  │ MemTable #3 │ ──full──► MemTable #2 │ ──flush──►  │  SST file   │
  │  (active)   │         │ (immutable) │   thread    │  (L0)       │
  │  writable   │         │  read-only  │             │  immutable  │
  │  64 MB      │         │  64 MB      │             │  sorted     │
  └─────────────┘         └─────────────┘             └─────────────┘
        ▲                        ▲
        │ writes                 │ reads still served
        │ go here               │ from here

  Timeline:
  ─────────────────────────────────────────────────────────────────
  t0: MemTable #1 active, accepting writes
  t1: MemTable #1 reaches 64MB → becomes immutable
      MemTable #2 created (new active)
      Background thread starts flushing #1 to SSTable
  t2: Flush of #1 completes → SSTable in L0, #1 freed
      MemTable #2 still active
  t3: MemTable #2 reaches 64MB → becomes immutable
      MemTable #3 created (new active)
      ...

  RocksDB config:
    write_buffer_size = 64MB          # Size of each MemTable
    max_write_buffer_number = 3       # Max MemTables (active + immutable)
    min_write_buffer_number_to_merge = 1  # Min immutable before flush

  If all write buffers are full and flush can't keep up:
    → WRITE STALL (back-pressure on writers)
```

### Write-Ahead Log (WAL)

The WAL provides durability for data in the MemTable that hasn't been flushed to SSTables yet.

```
WAL (Write-Ahead Log)
=====================

  Every write goes to TWO places:

  Client PUT(k,v)
       │
       ├──── 1. Append to WAL file (sequential disk write)
       │         This is the DURABILITY guarantee
       │
       └──── 2. Insert into MemTable (RAM)
                 This is for FAST READS of recent data

  WAL Record Format:
  ┌──────────┬──────────┬──────┬───────────┬──────┬──────┐
  │ Checksum │ Length   │ Type │ Sequence# │ Key  │Value │
  │ (CRC32)  │ (varint) │(1B)  │ (varint)  │(var) │(var) │
  └──────────┴──────────┴──────┴───────────┴──────┴──────┘

  Type: kFullType    = complete record in one block
        kFirstType   = start of multi-block record
        kMiddleType  = continuation
        kLastType    = end of multi-block record

  WAL file lifecycle:
  ──────────────────
  1. New WAL created with each new MemTable
  2. WAL is append-only (sequential writes)
  3. When MemTable flushes to SSTable → WAL can be deleted
  4. On crash recovery: replay WAL to rebuild MemTable

  WAL + MemTable relationship:
  ┌─────────┐     ┌──────────────┐
  │ WAL #1  │ ◄──►│ MemTable #1  │  (being flushed, immutable)
  └─────────┘     └──────────────┘
  ┌─────────┐     ┌──────────────┐
  │ WAL #2  │ ◄──►│ MemTable #2  │  (active, accepting writes)
  └─────────┘     └──────────────┘

  Crash recovery:
  ┌──────────────────────────────────────────────────┐
  │ 1. Find all WAL files not yet flushed            │
  │ 2. Replay each WAL in sequence number order      │
  │ 3. Rebuild MemTable from WAL records             │
  │ 4. Resume normal operation                       │
  │                                                   │
  │ Recovery time = WAL_size / disk_read_throughput   │
  │ With 64MB WAL, NVMe: ~0.1 seconds               │
  └──────────────────────────────────────────────────┘
```

### SSTable Format (Sorted String Table)

The SSTable is the on-disk format for LSM data. Each SSTable is immutable and sorted by key.

```
SSTABLE FILE FORMAT (RocksDB Block-Based Table)
================================================

  ┌─────────────────────────────────────────────────────┐
  │                  DATA BLOCKS                         │
  │                                                      │
  │  ┌───────────────────────────────────────────┐      │
  │  │ Data Block 0 (default 4KB)                │      │
  │  │  ┌────────────────────────────────────┐   │      │
  │  │  │ Restart 0: key1=val1               │   │      │
  │  │  │   shared=0, non_shared=5           │   │      │
  │  │  │ Delta:     key2=val2               │   │      │
  │  │  │   shared=3, non_shared=2           │   │      │
  │  │  │ Delta:     key3=val3               │   │      │
  │  │  │   shared=3, non_shared=2           │   │      │
  │  │  │   ...                              │   │      │
  │  │  │ [Restart point offsets array]       │   │      │
  │  │  │ [Num restarts: 4 bytes]            │   │      │
  │  │  └────────────────────────────────────┘   │      │
  │  └───────────────────────────────────────────┘      │
  │  ┌───────────────────────────────────────────┐      │
  │  │ Data Block 1                              │      │
  │  │  ... more key-value pairs ...             │      │
  │  └───────────────────────────────────────────┘      │
  │  ...                                                 │
  │  ┌───────────────────────────────────────────┐      │
  │  │ Data Block N                              │      │
  │  └───────────────────────────────────────────┘      │
  ├─────────────────────────────────────────────────────┤
  │                  FILTER BLOCK                        │
  │  ┌───────────────────────────────────────────┐      │
  │  │ Bloom filter for Data Block 0             │      │
  │  │ Bloom filter for Data Block 1             │      │
  │  │ ...                                       │      │
  │  │ Bloom filter for Data Block N             │      │
  │  │ [Filter offsets array]                    │      │
  │  └───────────────────────────────────────────┘      │
  ├─────────────────────────────────────────────────────┤
  │                COMPRESSION DICT BLOCK                │
  │  (Optional: dictionary for Zstd compression)        │
  ├─────────────────────────────────────────────────────┤
  │                RANGE DELETION BLOCK                  │
  │  (Range tombstones: delete key range [a, b))        │
  ├─────────────────────────────────────────────────────┤
  │                  INDEX BLOCK                         │
  │  ┌───────────────────────────────────────────┐      │
  │  │ separator_key_0 → offset of Data Block 0  │      │
  │  │ separator_key_1 → offset of Data Block 1  │      │
  │  │ separator_key_2 → offset of Data Block 2  │      │
  │  │ ...                                       │      │
  │  │ separator_key_N → offset of Data Block N  │      │
  │  └───────────────────────────────────────────┘      │
  │  (Separator key: shortest key >= last key in        │
  │   block i and < first key in block i+1)             │
  ├─────────────────────────────────────────────────────┤
  │              META-INDEX BLOCK                        │
  │  Points to filter block, properties block, etc.     │
  ├─────────────────────────────────────────────────────┤
  │                PROPERTIES BLOCK                      │
  │  ┌───────────────────────────────────────────┐      │
  │  │ num_entries: 50000                        │      │
  │  │ raw_key_size: 2400000                     │      │
  │  │ raw_value_size: 12000000                  │      │
  │  │ data_size: 9800000                        │      │
  │  │ index_size: 120000                        │      │
  │  │ filter_size: 62000                        │      │
  │  │ compression_type: zstd                    │      │
  │  │ creation_time: 1679000000                 │      │
  │  │ oldest_key_time: 1678000000               │      │
  │  └───────────────────────────────────────────┘      │
  ├─────────────────────────────────────────────────────┤
  │                    FOOTER                            │
  │  ┌───────────────────────────────────────────┐      │
  │  │ Meta-index block handle (offset + size)   │      │
  │  │ Index block handle (offset + size)        │      │
  │  │ Padding                                   │      │
  │  │ Magic number (0xdb4775248b80fb57)         │      │
  │  │ Version                                   │      │
  │  └───────────────────────────────────────────┘      │
  │  Fixed 48 bytes at end of file                      │
  └─────────────────────────────────────────────────────┘

DATA BLOCK PREFIX COMPRESSION (Delta Encoding):
================================================

  Key prefix compression reduces storage for keys with common prefixes:

  Full key:    "user:1000:email"    → stored fully (restart point)
  Delta key:   "user:1000:name"     → shared=10, unshared=4, "name"
  Delta key:   "user:1000:phone"    → shared=10, unshared=5, "phone"
  Delta key:   "user:1001:email"    → shared=5,  unshared=10, "1001:email"

  Restart points every 16 keys (default) for binary search within block.

  Entry format within a data block:
  ┌──────────────┬────────────────┬─────────────┬─────────┬───────┐
  │shared_bytes  │unshared_bytes  │value_length  │key_delta│ value │
  │(varint)      │(varint)        │(varint)      │(bytes)  │(bytes)│
  └──────────────┴────────────────┴─────────────┴─────────┴───────┘
```

### Multi-Level Structure: L0 vs L1+

```
L0 vs L1+ DIFFERENCES
======================

L0 (Level 0) -- SPECIAL LEVEL:
───────────────────────────────
- Files are flushed directly from MemTable
- Each file is sorted internally
- Key ranges BETWEEN files CAN OVERLAP
- Must check ALL L0 files during reads
- Typically limited to 4-12 files before triggering compaction

  L0 Files (overlapping ranges):

  SST_001: [aaa ────────── mmm]
  SST_002:      [ccc ──────────── rrr]
  SST_003: [bbb ─────── jjj]
  SST_004:           [fff ──────────────── zzz]

  Key "ggg" could be in ANY of these files!
  → Must check ALL L0 files (after Bloom filter)

L1+ (Level 1 and deeper) -- SORTED RUNS:
─────────────────────────────────────────
- Files within a level have NON-OVERLAPPING key ranges
- Binary search can find the ONE file containing a key
- Forms a single "sorted run" split into fixed-size files

  L1 Files (non-overlapping, sorted):

  SST_010: [aaa ── ccc]
  SST_011:              [ddd ── fff]
  SST_012:                          [ggg ── iii]
  SST_013:                                      [jjj ── lll]
  SST_014:                                                  [mmm ── zzz]

  Key "ggg" → binary search → must be in SST_012 only!
  → Check exactly ONE file per level (after Bloom filter)

SIZE RATIOS BETWEEN LEVELS:
────────────────────────────
  Level    Max Size       Files (64MB each)   Key Range Coverage
  ────────────────────────────────────────────────────────────────
  L0       256 MB*        4 (overlapping)      Partial, overlapping
  L1       256 MB         4                    Full keyspace
  L2       2.56 GB        40                   Full keyspace
  L3       25.6 GB        400                  Full keyspace
  L4       256 GB         4,000                Full keyspace
  L5       2.56 TB        40,000               Full keyspace

  * L0 triggers compaction by FILE COUNT, not total size

  Size ratio (T) = 10 is the default in RocksDB.
  Each level is T times larger than the previous.

  Total levels needed: ceil(log_T(data_size / L1_size)) + 1
  For 1 TB data, L1=256MB, T=10:
    ceil(log_10(1TB / 256MB)) + 1 = ceil(log_10(4096)) + 1 = 4 + 1 = 5 levels
```

### The Complete Write Path

```
COMPLETE WRITE PATH (PUT Operation)
====================================

  Client: PUT("user:1234", "{name: Alice, age: 30}")
    │
    ▼
  ┌──────────────────────────────────────────────────┐
  │ 1. ACQUIRE WRITE GROUP / WRITE BATCH             │
  │    Multiple concurrent writes batched together    │
  │    Leader of write group does the actual I/O      │
  └─────────────────────┬────────────────────────────┘
                        │
                        ▼
  ┌──────────────────────────────────────────────────┐
  │ 2. WRITE TO WAL                                  │
  │    Format: [CRC32][Length][Type][SeqNum][KV]      │
  │    Sync mode:                                     │
  │      sync=true  → fsync() after write (durable)  │
  │      sync=false → OS buffer (faster, risk loss)   │
  │    Group commit: batch multiple writes in one     │
  │    fsync for efficiency                           │
  └─────────────────────┬────────────────────────────┘
                        │
                        ▼
  ┌──────────────────────────────────────────────────┐
  │ 3. INSERT INTO MEMTABLE                          │
  │    Skip list insertion: O(log n)                  │
  │    Key format: [user_key][sequence_number][type]  │
  │    Type: kTypeValue (put) or kTypeDeletion (del)  │
  │    Sequence number provides MVCC ordering         │
  │                                                   │
  │    Internal key layout:                           │
  │    ┌──────────────┬──────────┬──────┐            │
  │    │  user_key    │ seq_num  │ type │            │
  │    │  (variable)  │ (7 byte) │(1 B) │            │
  │    └──────────────┴──────────┴──────┘            │
  └─────────────────────┬────────────────────────────┘
                        │
                        ▼
  ┌──────────────────────────────────────────────────┐
  │ 4. RETURN SUCCESS TO CLIENT                      │
  │    Write is considered durable (if sync=true)     │
  │    Total latency: ~10-100us (NVMe + sync)        │
  └──────────────────────────────────────────────────┘
                        │
                        │  (Background, asynchronous)
                        ▼
  ┌──────────────────────────────────────────────────┐
  │ 5. MEMTABLE FULL → SWITCH                       │
  │    Current MemTable becomes immutable             │
  │    New MemTable + new WAL created                 │
  │    Old WAL kept until flush completes             │
  └─────────────────────┬────────────────────────────┘
                        │
                        ▼
  ┌──────────────────────────────────────────────────┐
  │ 6. FLUSH TO L0 SSTABLE                          │
  │    Iterate MemTable in sorted order               │
  │    Build SSTable: data blocks, index, filters     │
  │    Write file sequentially (great for SSD/HDD)    │
  │    Add to L0 manifest                             │
  │    Delete old WAL                                 │
  │    Typical flush time: 50-500ms for 64MB          │
  └─────────────────────┬────────────────────────────┘
                        │
                        ▼
  ┌──────────────────────────────────────────────────┐
  │ 7. TRIGGER COMPACTION (if needed)                │
  │    L0 file count > threshold (default 4) → start │
  │    Background compaction thread merges files      │
  │    (See Section 4 for compaction details)         │
  └──────────────────────────────────────────────────┘
```

### Delete Operations: Tombstones

LSM Trees cannot delete in place (SSTables are immutable). Instead, they write a **tombstone** -- a special marker that says "this key is deleted."

```
TOMBSTONES IN LSM TREES
========================

  PUT("key_A", "value1")  →  [key_A | seq=100 | type=VALUE  | "value1"]
  PUT("key_A", "value2")  →  [key_A | seq=200 | type=VALUE  | "value2"]
  DELETE("key_A")          →  [key_A | seq=300 | type=DELETE | (empty) ]

  Read("key_A"):
    Search from newest to oldest.
    Find tombstone at seq=300 → Key is deleted. Return NOT FOUND.

  Tombstone stays until compaction reaches the BOTTOM level:

  ┌───────────────────────────────────────────────────┐
  │ L0: [key_A | seq=300 | DELETE]  ← tombstone       │
  │ L1: [key_A | seq=200 | VALUE="value2"]            │
  │ L2: [key_A | seq=100 | VALUE="value1"]            │
  └───────────────────────────────────────────────────┘

  After L0→L1 compaction:
  ┌───────────────────────────────────────────────────┐
  │ L1: [key_A | seq=300 | DELETE]  ← kept! (L2 has) │
  │ L2: [key_A | seq=100 | VALUE="value1"]            │
  └───────────────────────────────────────────────────┘

  After L1→L2 compaction (bottom level):
  ┌───────────────────────────────────────────────────┐
  │ L2: (key_A removed entirely)  ← garbage collected │
  └───────────────────────────────────────────────────┘

  Tombstones can ONLY be dropped at the bottommost level
  (because a deeper level might still have the old value).

  RANGE TOMBSTONES:
    DELETE RANGE ["user:1000", "user:2000")
    Stored in a separate range deletion block in SSTable.
    Efficiently deletes thousands of keys without individual tombstones.
```

---

## 3. The Read Path

### Point Lookup: Step by Step

```
READ PATH: GET("user:5678")
============================

  ┌──────────────────────────────────┐
  │  1. Check Active MemTable       │  ← O(log n) skip list search
  │     Found? → Return value       │     FASTEST: data is in RAM
  └──────────┬───────────────────────┘
             │ Not found
             ▼
  ┌──────────────────────────────────┐
  │  2. Check Immutable MemTable(s) │  ← Also in RAM
  │     Check newest first          │     May have 0-2 immutables
  │     Found? → Return value       │
  └──────────┬───────────────────────┘
             │ Not found
             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  3. Check L0 SSTables (ALL of them! Overlapping ranges) │
  │                                                          │
  │  For EACH L0 file (newest first):                       │
  │    a. Check Bloom filter → likely not here? SKIP         │
  │    b. Search index block → find data block offset        │
  │    c. Read data block (from block cache or disk)         │
  │    d. Binary search within block for key                 │
  │    e. Found? → Return value                              │
  │                                                          │
  │  Worst case: check ALL L0 files (4-12 files)            │
  │  With Bloom filters: ~1% false positive per file        │
  └──────────┬───────────────────────────────────────────────┘
             │ Not found in any L0 file
             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  4. Check L1 (ONE file only -- non-overlapping)         │
  │                                                          │
  │    a. Binary search file boundaries → find target file   │
  │    b. Check Bloom filter → skip if negative              │
  │    c. Search index block → find data block               │
  │    d. Read data block, binary search for key             │
  │    e. Found? → Return value                              │
  └──────────┬───────────────────────────────────────────────┘
             │ Not found
             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  5. Check L2 (ONE file only)                            │
  │     Same process as L1                                   │
  └──────────┬───────────────────────────────────────────────┘
             │ Not found
             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  6. Check L3... L4... LN                                │
  │     Same process, one file per level                     │
  └──────────┬───────────────────────────────────────────────┘
             │ Not found in ANY level
             ▼
  ┌──────────────────────────────────┐
  │  7. Return NOT FOUND             │
  └──────────────────────────────────┘

TOTAL READS (WORST CASE, key does not exist):
  = MemTable(s) + L0_files + L1 + L2 + ... + LN
  = 1-3 (RAM) + 4-12 (L0) + 1 + 1 + ... + 1
  = Up to ~20 disk reads without Bloom filters

WITH BLOOM FILTERS (1% FP rate per filter):
  Each level has ~1% chance of false positive read.
  Expected disk reads for non-existent key:
  = L0_files * 0.01 + num_levels * 0.01
  ≈ 0.04 + 0.05 = ~0.09 disk reads (< 1 read on average!)
```

### Block Cache

```
BLOCK CACHE ARCHITECTURE
=========================

  ┌────────────────────────────────────────────────────────┐
  │                    BLOCK CACHE (LRU)                    │
  │                                                         │
  │  Cached Block Types:                                    │
  │  ┌─────────────────────────────────────────────┐       │
  │  │ Data Blocks    (key-value pairs, 4KB each)  │       │
  │  │ Index Blocks   (one per SSTable)            │       │
  │  │ Filter Blocks  (Bloom filters per SSTable)  │       │
  │  └─────────────────────────────────────────────┘       │
  │                                                         │
  │  Typical sizing:                                        │
  │    Total data: 100 GB                                   │
  │    Block cache: 4-8 GB (4-8% of data)                  │
  │    Hot data is a small fraction → high hit rate         │
  │                                                         │
  │  Two-tier cache in RocksDB:                            │
  │  ┌───────────────┐    ┌───────────────────────┐       │
  │  │ Block Cache   │    │ Compressed Block Cache │       │
  │  │ (uncompressed)│    │ (compressed, larger)   │       │
  │  │ Fast access   │    │ Slower, more capacity  │       │
  │  └───────────────┘    └───────────────────────┘       │
  │                                                         │
  │  Cache miss path:                                       │
  │  1. Check block cache (RAM)           → HIT? Return    │
  │  2. Check OS page cache               → HIT? Decompress│
  │  3. Read from SSD/HDD (actual I/O)   → Decompress      │
  │  4. Insert into block cache                             │
  └────────────────────────────────────────────────────────┘

  PIN_L0_FILTER_AND_INDEX_BLOCKS_IN_CACHE:
    Important RocksDB option. Keeps L0 index + filter blocks
    permanently in cache. Since L0 is checked on EVERY read,
    this avoids repeated I/O for the most frequently accessed metadata.
```

### Read Amplification Analysis

```
READ AMPLIFICATION BY SCENARIO
================================

  Scenario 1: Key exists, was just written (in MemTable)
  ──────────────────────────────────────────────────────
  Disk reads: 0  (all in RAM)

  Scenario 2: Key exists, in L0 (recently flushed)
  ──────────────────────────────────────────────────────
  Bloom filter on correct L0 file: TRUE POSITIVE → read data block
  Disk reads: 1 (data block) + possibly 1 (index block if not cached)
  Best case: 1 read

  Scenario 3: Key exists, in L3
  ──────────────────────────────────────────────────────
  Check MemTable:          0 reads (RAM)
  Check L0 (4 files):      4 Bloom checks → all negative (0 reads)
  Check L1:                1 Bloom check → negative (0 reads)
  Check L2:                1 Bloom check → negative (0 reads)
  Check L3:                1 Bloom check → positive! → 1 read
  Total disk reads: 1 (with cached index/filter blocks)

  Scenario 4: Key does NOT exist
  ──────────────────────────────────────────────────────
  Must check all levels. Bloom filters save us:
  L0: 4 files × 1% FP = 0.04 expected reads
  L1: 0.01 expected reads
  L2: 0.01 expected reads
  L3: 0.01 expected reads
  L4: 0.01 expected reads
  Total: ~0.08 expected disk reads

  WITHOUT Bloom filters (worst case):
  L0: 4 files → 4 reads
  L1-L4: 4 reads
  Total: 8 reads → THIS IS WHY BLOOM FILTERS ARE CRITICAL

  ┌────────────────────┬────────────────┬──────────────────┐
  │ Scenario           │ With Bloom     │ Without Bloom    │
  │                    │ Filters        │ Filters          │
  ├────────────────────┼────────────────┼──────────────────┤
  │ Key in MemTable    │ 0 disk reads   │ 0 disk reads     │
  │ Key in L0          │ ~1 read        │ 1-4 reads        │
  │ Key in L3          │ ~1 read        │ ~7 reads         │
  │ Key not found      │ ~0.08 reads    │ ~8 reads         │
  │ Range scan (100)   │ N/A (no bloom) │ Merge all levels │
  └────────────────────┴────────────────┴──────────────────┘
```

### Range Scans (Iterators)

```
RANGE SCAN: Scan("user:1000", "user:2000")
===========================================

  LSM range scans use a MERGE ITERATOR across all levels:

  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
  │ MemTable     │  │ Immutable    │  │ L0 File 1    │
  │  Iterator    │  │  MemTable    │  │  Iterator    │
  │  user:1001   │  │  Iterator    │  │  user:1050   │
  │  user:1100   │  │  user:1003   │  │  user:1200   │
  │  user:1500   │  │  user:1400   │  │  user:1500   │
  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
         │                 │                  │
         └────────┬────────┴──────────────────┘
                  ▼
  ┌──────────────────────────────────────────────┐
  │          MERGE ITERATOR (min-heap)           │
  │                                               │
  │  Priority queue of all level iterators        │
  │  Always yields the smallest key across all    │
  │  For duplicate keys: newest version wins      │
  │                                               │
  │  Output: user:1001, user:1003, user:1050,    │
  │          user:1100, user:1200, user:1400,    │
  │          user:1500, ...                       │
  └──────────────────────────────────────────────┘

  Range scan cost:
  - Bloom filters DO NOT help (they're for point lookups)
  - Must open iterator on every L0 file + one per L1+ level
  - Merge k sorted streams → O(n * log(k)) where k = number of iterators
  - Sequential I/O within each SSTable (good for SSD/HDD)

  Optimization: For L1+, only open files whose key range overlaps the scan range.
```

---

## 4. Compaction Strategies (Core)

Compaction is the most critical background operation in an LSM Tree. It controls the trade-offs between read amplification, write amplification, and space amplification.

### Why Compaction Exists

```
WITHOUT COMPACTION:
  - L0 grows unbounded → read must check hundreds of files
  - Deleted/overwritten data never reclaimed → space grows forever
  - Tombstones accumulate → reads slow down scanning dead entries

WITH COMPACTION:
  - Merge overlapping SSTables → reduce file count
  - Drop obsolete versions → reclaim space
  - Drop tombstones (at bottom level) → clean up deletes
  - Maintain sorted order → efficient reads
```

### Strategy 1: Size-Tiered Compaction (STCS)

Used by: Cassandra (default), HBase, ScyllaDB (option).

```
SIZE-TIERED COMPACTION STRATEGY (STCS)
========================================

Core idea: Group SSTables of SIMILAR SIZE together.
When enough similar-sized SSTables accumulate, merge them into one larger SSTable.

  min_threshold = 4 (Cassandra default)

  Step 1: Small SSTables accumulate from MemTable flushes
  ─────────────────────────────────────────────────────────

  Tier 0 (small, ~64MB each):
  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐
  │ 60MB │ │ 62MB │ │ 58MB │ │ 65MB │  ← 4 similar-size files!
  └──────┘ └──────┘ └──────┘ └──────┘    Trigger compaction

  Step 2: Merge 4 small → 1 medium
  ──────────────────────────────────

  Tier 0 (small):
  (empty, new flushes accumulate here)

  Tier 1 (medium, ~256MB each):
  ┌────────────┐
  │   245MB    │  ← merged result
  └────────────┘

  Step 3: More small SSTables accumulate and compact
  ───────────────────────────────────────────────────

  Tier 1 (medium):
  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐
  │   245MB    │ │   250MB    │ │   240MB    │ │   255MB    │
  └────────────┘ └────────────┘ └────────────┘ └────────────┘
  ← 4 similar-size files! Trigger compaction again

  Step 4: Merge 4 medium → 1 large
  ──────────────────────────────────

  Tier 2 (large, ~1GB each):
  ┌─────────────────────┐
  │       990MB         │  ← merged result
  └─────────────────────┘

  And so on: 4 x ~1GB → 1 x ~4GB → ...

STCS VISUAL TIMELINE:

  Time → → → → → → → → → → → → → → → → → → → → → → → →

  t0:  [S1]
  t1:  [S1][S2]
  t2:  [S1][S2][S3]
  t3:  [S1][S2][S3][S4]  → compact →  [M1]
  t4:  [S5]                            [M1]
  t5:  [S5][S6]                        [M1]
  t6:  [S5][S6][S7]                    [M1]
  t7:  [S5][S6][S7][S8]  → compact →  [M1][M2]
  ...
  tN:                      compact →   [M1][M2][M3][M4] → [L1]

PROPERTIES:
┌────────────────────────────┬────────────────────────────────────────┐
│ Write amplification        │ LOW (~4-5x). Data rewritten ~once per │
│                            │ tier merge. log_T(N) tiers total.     │
├────────────────────────────┼────────────────────────────────────────┤
│ Read amplification         │ HIGH. Multiple SSTables per tier may  │
│                            │ overlap. Must check many files.       │
├────────────────────────────┼────────────────────────────────────────┤
│ Space amplification        │ HIGH (~2-3x worst case). During merge │
│                            │ of tier: input + output both exist    │
│                            │ temporarily. Also, duplicate keys     │
│                            │ across tiers before merge.            │
├────────────────────────────┼────────────────────────────────────────┤
│ Temporary space during     │ Up to 2x the largest tier (input +   │
│ compaction                 │ output coexist until complete).       │
├────────────────────────────┼────────────────────────────────────────┤
│ Best for                   │ Write-heavy workloads. Time-series.  │
│                            │ Bulk loading.                         │
└────────────────────────────┴────────────────────────────────────────┘
```

### Strategy 2: Leveled Compaction (LCS)

Used by: RocksDB (default), LevelDB, Cassandra (option), ScyllaDB (option).

```
LEVELED COMPACTION STRATEGY (LCS)
===================================

Core idea: Each level (L1+) is a single sorted run of non-overlapping files.
When a level exceeds its size limit, pick a file and merge it DOWN into the
overlapping files in the next level.

Size ratio T = 10 (default).
L1 target: 256 MB. L2 target: 2.56 GB. L3: 25.6 GB. etc.

STEP-BY-STEP LEVELED COMPACTION:
──────────────────────────────────

  Step 1: L0 has too many files (>4). Compact L0 → L1.

  L0:  [SST_a: aaa-mmm] [SST_b: bbb-zzz] [SST_c: ccc-ppp] [SST_d: ddd-qqq]
        (overlapping ranges, all must be included)

  L1:  [aaa-ddd] [eee-hhh] [iii-lll] [mmm-ppp] [qqq-zzz]
        (non-overlapping, sorted)

  L0 files cover range [aaa-zzz] → overlaps ALL L1 files.

  Merge: ALL L0 files + ALL overlapping L1 files → new L1 files

  Result L1: [aaa-bbb] [ccc-ddd] [eee-fff] [ggg-hhh] [iii-jjj]
             [kkk-lll] [mmm-nnn] [ooo-ppp] [qqq-rrr] [sss-zzz]

  Now L1 might exceed its 256 MB limit!

  Step 2: L1 exceeds 256 MB. Pick ONE file, compact into L2.

  L1: [aaa-bbb] [ccc-ddd]* [eee-fff] [ggg-hhh] ...
                     ↑
               Pick this file (64MB, range ccc-ddd)

  L2: [aaa-ccc] [ddd-fff] [ggg-iii] [jjj-lll] [mmm-ooo] [ppp-zzz]
       ↑overlap↑  ↑overlap↑

  Files in L2 that overlap [ccc-ddd]: [aaa-ccc] and [ddd-fff]

  Merge: L1's [ccc-ddd] + L2's [aaa-ccc] + L2's [ddd-fff]

  Result L2: [aaa-bbb] [ccc-ccc] [ddd-eee] [fff-fff] [ggg-iii] ...
             (still non-overlapping, still sorted)

  Step 3: If L2 now exceeds 2.56 GB → pick file, compact to L3.
          Process continues recursively down levels.

COMPACTION PICKING STRATEGY (RocksDB):
───────────────────────────────────────
  For each level that exceeds its target size:
    score = current_size / target_size

  Pick the level with the highest score.
  Within that level, pick the file with:
    - Round-robin selection (spread compaction across key range)
    - Or: file with most overlapping data in next level (minimize write amp)
    - Or: file that has been coldest the longest

DETAILED MERGE PROCESS:
────────────────────────
  Input:  1 file from L(n) + K overlapping files from L(n+1)

  ┌─────────┐  ┌─────────┬─────────┬─────────┐
  │ L(n)    │  │  L(n+1) │  L(n+1) │  L(n+1) │
  │ file    │  │  file 1 │  file 2 │  file 3 │
  │ [c-d]   │  │  [a-c]  │  [d-f]  │  [f-g]  │
  └────┬────┘  └────┬────┴────┬────┴────┬────┘
       │            │         │         │
       └─────┬──────┴────┬────┘         │
             ▼           ▼              │
       ┌─────────────────────────┐      │
       │   K-way Merge Sort     │      │
       │   Keep newest version  │◄─────┘
       │   Drop obsolete values │
       │   Drop tombstones      │  (only at bottom level)
       └───────────┬────────────┘
                   │
    ┌──────────────┼──────────────┐
    ▼              ▼              ▼
  ┌─────────┐  ┌─────────┐  ┌─────────┐
  │  New    │  │  New    │  │  New    │
  │  L(n+1) │  │  L(n+1) │  │  L(n+1) │
  │  [a-b]  │  │  [c-d]  │  │  e-g]  │
  └─────────┘  └─────────┘  └─────────┘

  Output: New non-overlapping files in L(n+1).
  Old input files are deleted after compaction completes.

PROPERTIES:
┌────────────────────────────┬────────────────────────────────────────┐
│ Write amplification        │ HIGH. Each byte rewritten ~T times    │
│                            │ per level. Total: O(T * num_levels).  │
│                            │ With T=10: ~10-30x typical.           │
├────────────────────────────┼────────────────────────────────────────┤
│ Read amplification         │ LOW. At most 1 file per L1+ level.    │
│                            │ With Bloom filters: ~1 read total.    │
├────────────────────────────┼────────────────────────────────────────┤
│ Space amplification        │ LOW (~1.1x). Only 1 file from L(n)   │
│                            │ being merged at a time. Temporary     │
│                            │ overhead is small.                    │
├────────────────────────────┼────────────────────────────────────────┤
│ Temporary space during     │ ~1 file from L(n) + overlapping files │
│ compaction                 │ from L(n+1). Bounded and predictable. │
├────────────────────────────┼────────────────────────────────────────┤
│ Best for                   │ Read-heavy workloads. Point lookups.  │
│                            │ Space-constrained environments.       │
└────────────────────────────┴────────────────────────────────────────┘
```

### Strategy 3: FIFO Compaction

```
FIFO COMPACTION
================

  Simplest strategy. Designed for TTL-based data (logs, metrics, events).

  Rule: When total SSTable size exceeds a threshold, delete the OLDEST files.
  No merging. No sorting across files. Just drop old data.

  ┌──────────────────────────────────────────────────────────────┐
  │                                                              │
  │  Time ───────────────────────────────────────────────►       │
  │                                                              │
  │  [SST_old] [SST_older] [SST_mid] [SST_recent] [SST_new]    │
  │   ↑ DROP    ↑ DROP                                           │
  │   (expired) (expired)                                        │
  │                                                              │
  │  Max size: 10 GB. Current: 12 GB.                           │
  │  Drop oldest files until under 10 GB.                        │
  └──────────────────────────────────────────────────────────────┘

  Properties:
    Write amplification: 1x (no rewriting!)
    Read amplification:  HIGH (no merging, many files)
    Space amplification: ~1x (no duplicates kept long)

  Use case: Time-series with TTL. Metrics pipelines.
  NOT suitable for: Data that needs updates or deletes.
```

### Strategy 4: Universal Compaction (RocksDB)

```
UNIVERSAL COMPACTION (RocksDB)
================================

  A generalization of size-tiered compaction with more control.
  Also called "sorted runs" compaction.

  Core concept: Maintain a list of "sorted runs" (each run is
  either a single L0 file or a compacted sorted run).

  Trigger conditions (any of):
  1. Number of sorted runs > max_sorted_runs (default: depends)
  2. Space amplification > max_size_amplification_percent (default: 200%)
  3. Size ratio between adjacent runs is off

  Compaction picking:
  ┌────────────────────────────────────────────────────────────┐
  │ Sorted runs (newest first):                                │
  │   R1(10MB) R2(12MB) R3(50MB) R4(200MB) R5(1GB) R6(5GB)   │
  │                                                            │
  │ Check size_ratio (default 1%):                             │
  │   R1/R2 = 0.83 < 1.01 → include R1, R2                    │
  │   (R1+R2)/R3 = 0.44 < 1.01 → include R3                   │
  │   (R1+R2+R3)/R4 = 0.36 < 1.01 → include R4                │
  │   (R1+R2+R3+R4)/R5 = 0.27 < 1.01 → include R5             │
  │   STOP if ratio > 1 + size_ratio                           │
  │                                                            │
  │ → Merge R1..R5 into one new sorted run                     │
  │ → Result placed before R6                                  │
  └────────────────────────────────────────────────────────────┘

  Properties:
    Write amplification: Lower than leveled, higher than FIFO
    Read amplification:  Moderate (controlled by max_sorted_runs)
    Space amplification: Controlled by max_size_amplification_percent

  When to use:
    - Write-heavy workloads where leveled WA is too high
    - Need better space amp than pure size-tiered
    - Willing to accept higher read amp than leveled
```

### Strategy 5: Hybrid (Tiered + Leveled)

```
HYBRID COMPACTION (Used by ScyllaDB, modern RocksDB setups)
=============================================================

  Idea: Use TIERED compaction for upper levels (L0-L1) where write
  throughput matters, and LEVELED for deeper levels where read
  performance and space efficiency matter.

  ┌───────────────────────────────────────────────┐
  │  L0: Tiered (multiple sorted runs allowed)    │  ← Write-optimized
  │      Multiple overlapping SSTables ok         │
  │      Low write amplification here             │
  ├───────────────────────────────────────────────┤
  │  L1: Transition zone                          │  ← Merge tiered → leveled
  │      May have some overlap during transition  │
  ├───────────────────────────────────────────────┤
  │  L2+: Leveled (single sorted run per level)  │  ← Read-optimized
  │       Non-overlapping files                   │
  │       Low space amplification                 │
  └───────────────────────────────────────────────┘

  Benefits:
    - Lower write amp than pure leveled (saves SSD writes)
    - Lower read amp than pure tiered (structured deeper levels)
    - Better space amp than pure tiered

  ScyllaDB's ICS (Incremental Compaction Strategy):
    - Extension of this hybrid concept
    - Compacts within a sorted run incrementally
    - Avoids huge temporary space spikes during compaction
```

### Compaction Strategy Comparison

```
COMPACTION STRATEGY TRADE-OFFS
================================

  Write Amp    ▲
  (bad=high)   │
               │  Leveled ●
     HIGH      │            ╲
               │             ╲
               │              ╲
               │               ╲  Hybrid
     MEDIUM    │                ●
               │               ╱
               │              ╱
               │             ╱
     LOW       │  Tiered ●  ╱     FIFO ●
               │
               └──────────────────────────────► Read Amp (bad=high)
                   LOW      MEDIUM     HIGH

  ┌───────────────┬──────────┬──────────┬──────────┬──────────┐
  │ Strategy      │Write Amp │Read Amp  │Space Amp │Use Case  │
  ├───────────────┼──────────┼──────────┼──────────┼──────────┤
  │ Leveled (LCS) │ ~10-30x  │ ~1-2     │ ~1.1x    │ Read     │
  │               │          │ reads    │          │ heavy    │
  ├───────────────┼──────────┼──────────┼──────────┼──────────┤
  │ Tiered (STCS) │ ~3-5x    │ ~5-30    │ ~2-3x    │ Write    │
  │               │          │ reads    │          │ heavy    │
  ├───────────────┼──────────┼──────────┼──────────┼──────────┤
  │ FIFO          │ ~1x      │ ~20-100  │ ~1x      │ TTL/logs │
  │               │          │ reads    │          │          │
  ├───────────────┼──────────┼──────────┼──────────┼──────────┤
  │ Universal     │ ~5-10x   │ ~5-10    │ ~1.5-2x  │ Mixed    │
  │               │          │ reads    │          │          │
  ├───────────────┼──────────┼──────────┼──────────┼──────────┤
  │ Hybrid        │ ~5-15x   │ ~2-5     │ ~1.2-1.5x│ General  │
  │               │          │ reads    │          │ purpose  │
  └───────────────┴──────────┴──────────┴──────────┴──────────┘
```

---

## 5. Write Amplification Analysis

### Definition and Impact

```
WRITE AMPLIFICATION (WA) EXPLAINED
====================================

  Definition:
    WA = Total bytes written to storage / Bytes of user data written

  Example:
    User writes 1 GB of data.
    Storage device sees 15 GB of writes (due to compaction).
    WA = 15x

  Why it matters:
  ┌──────────────────────────────────────────────────────────────────┐
  │ 1. SSD LIFETIME                                                 │
  │    SSDs have limited write endurance (TBW - Total Bytes Written)│
  │    Enterprise NVMe: ~1-3 DWPD (Drive Writes Per Day)           │
  │    A 1 TB SSD with 1 DWPD can sustain 1 TB/day of writes      │
  │    With 15x WA: effective user write rate = 1TB/15 = 67 GB/day │
  │                                                                 │
  │ 2. WRITE THROUGHPUT                                             │
  │    SSD write bandwidth is finite (e.g., 3 GB/s for NVMe)       │
  │    With 15x WA: effective user throughput = 3/15 = 200 MB/s    │
  │    Compaction steals bandwidth from foreground writes            │
  │                                                                 │
  │ 3. CPU OVERHEAD                                                 │
  │    Compaction requires reading, merging, compressing, writing   │
  │    CPU cycles proportional to write amplification               │
  └──────────────────────────────────────────────────────────────────┘
```

### Leveled Compaction Write Amplification

```
LEVELED COMPACTION WA DERIVATION
==================================

  Setup: Size ratio T = 10. File size = F.

  When a file from L(n) is compacted into L(n+1):
  - The L(n) file covers some key range
  - In the worst case, it overlaps T files in L(n+1)
    (because L(n+1) is T times larger)
  - All T+1 files (1 from L(n) + T from L(n+1)) are read and rewritten

  WA per compaction at one level:
    Read: 1F + T*F = (T+1)*F bytes
    Write: (T+1)*F bytes  (output ≈ input, minus obsolete data)

    But only 1F of NEW data entered this level.
    So WA for this level = (T+1)*F / 1*F ≈ T+1 ≈ T

    With T=10: WA per level ≈ 11x (simplified to ~10x)

  Total WA across all levels:
    WA_total = WA_flush + WA_L0→L1 + WA_L1→L2 + ... + WA_L(N-1)→LN

    ≈ 1 + T * (number_of_levels - 1)

    For T=10, 5 levels:
    WA ≈ 1 + 10 * 4 = 41x (theoretical worst case)

    In practice: ~10-30x because:
    - Not all files overlap T files (key distribution matters)
    - Obsolete data is dropped (reducing output size)
    - Some levels may not be full

  VISUALIZING WA ACROSS LEVELS:

  User writes 1 byte
       │
       ▼
  WAL: 1 byte written (WA contribution: 1x)
       │
       ▼
  MemTable → L0: 1 byte written (WA: 1x, total: 2x)
       │
       ▼
  L0 → L1 compaction: ~1 byte rewritten (WA: ~1x, total: 3x)
       │
       ▼
  L1 → L2 compaction: ~10 bytes rewritten* (WA: ~10x, total: 13x)
       │
       ▼
  L2 → L3 compaction: ~10 bytes rewritten* (WA: ~10x, total: 23x)
       │
       ▼
  L3 → L4 compaction: ~10 bytes rewritten* (WA: ~10x, total: 33x)

  * Each byte of L(n) "causes" ~T bytes of rewriting in L(n+1)
    because it must merge with T overlapping files.
```

### Size-Tiered Write Amplification

```
SIZE-TIERED COMPACTION WA DERIVATION
======================================

  Setup: Merge threshold T = 4 (compact when T same-size files exist).

  When T files of size S are merged:
  - Read: T * S bytes
  - Write: ~T * S bytes (one output file, minus obsolete data)
  - Each of the T files was written once before
  - WA for this merge: T * S / (T * S) = 1x (!)

  BUT: data goes through O(log_T(N/S)) merge rounds:

  Round 1: T files of 64MB → 1 file of 256MB    (4x data rewritten)
  Round 2: T files of 256MB → 1 file of 1GB     (4x data rewritten)
  Round 3: T files of 1GB → 1 file of 4GB       (4x data rewritten)
  Round 4: T files of 4GB → 1 file of 16GB      (4x data rewritten)

  Each byte is rewritten once per round.
  Number of rounds = log_T(N/memtable_size)

  For T=4, 16 GB data, 64MB memtable:
    Rounds = log_4(16GB/64MB) = log_4(256) = 4
    WA = 1 (WAL) + 1 (flush) + 4 (compaction rounds) = 6x

  For T=4, 1 TB data, 64MB memtable:
    Rounds = log_4(1TB/64MB) = log_4(16384) = 7
    WA = 1 + 1 + 7 = 9x

  Comparison:
  ┌──────────────────┬──────────────────┬──────────────────┐
  │ Data Size        │ Leveled WA (T=10)│ Tiered WA (T=4)  │
  ├──────────────────┼──────────────────┼──────────────────┤
  │ 1 GB             │ ~12x             │ ~4x              │
  │ 10 GB            │ ~22x             │ ~5x              │
  │ 100 GB           │ ~32x             │ ~7x              │
  │ 1 TB             │ ~42x             │ ~9x              │
  └──────────────────┴──────────────────┴──────────────────┘
```

### SSD Lifetime Impact

```
SSD LIFETIME CALCULATION
=========================

  Given:
    SSD capacity: 1 TB
    SSD endurance: 1 DWPD (1 TB writes per day for 5 years)
    Total TBW: 1 TB × 365 × 5 = 1,825 TBW
    User write rate: 100 GB/day

  ┌──────────────┬──────────────┬──────────────┬──────────────┐
  │ Strategy     │ WA           │ Actual writes│ SSD lifetime │
  │              │              │ per day      │              │
  ├──────────────┼──────────────┼──────────────┼──────────────┤
  │ Leveled      │ 25x          │ 2,500 GB/day │ 730 days     │
  │ (T=10)       │              │ (2.5 DWPD)   │ (2.0 years)  │
  ├──────────────┼──────────────┼──────────────┼──────────────┤
  │ Tiered       │ 6x           │ 600 GB/day   │ 3,042 days   │
  │ (T=4)        │              │ (0.6 DWPD)   │ (8.3 years)  │
  ├──────────────┼──────────────┼──────────────┼──────────────┤
  │ FIFO         │ 1x           │ 100 GB/day   │ 18,250 days  │
  │              │              │ (0.1 DWPD)   │ (50 years)   │
  └──────────────┴──────────────┴──────────────┴──────────────┘

  Key insight: For write-heavy workloads on SSDs, the compaction
  strategy choice can determine whether your SSDs last 2 years or 8+.
```

### Tuning Write Amplification

```
WA TUNING KNOBS
================

  1. INCREASE SIZE RATIO (T)
     Higher T → fewer levels → less WA per byte
     But: higher per-level WA (more overlapping files per compaction)
     Sweet spot: T=10 for leveled, T=4 for tiered

  2. INCREASE L0 FILE COUNT THRESHOLD
     More L0 files before compaction → batch more data
     Reduces frequency of L0→L1 compaction
     But: more L0 files = worse read latency for L0 lookups
     RocksDB: level0_file_num_compaction_trigger (default: 4)

  3. LARGER MEMTABLE
     Larger MemTable → less frequent flushes → fewer L0 files
     Absorbs more overwrites in memory
     But: more RAM, longer recovery time
     RocksDB: write_buffer_size (default: 64MB)

  4. COMPRESSION
     Compress data blocks → less physical I/O per compaction
     Reduces effective WA on the device
     But: CPU overhead for compress/decompress
     Typical: no compression L0-L1, LZ4 for L2-L3, ZSTD for L4+

  5. DYNAMIC LEVEL SIZE (RocksDB)
     level_compaction_dynamic_level_bytes = true
     Adjusts level sizes based on actual data size
     Avoids unnecessary levels when data is small
     Reduces WA during data growth phase
```

---

## 6. RocksDB Deep Dive

### Architecture Overview

```
ROCKSDB ARCHITECTURE
=====================

  ┌─────────────────────────────────────────────────────────────────┐
  │                        CLIENT API                               │
  │  Put() | Get() | Delete() | Merge() | Iterator | WriteBatch    │
  └────────────────────────────┬────────────────────────────────────┘
                               │
  ┌────────────────────────────▼────────────────────────────────────┐
  │                     COLUMN FAMILIES                             │
  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
  │  │   "default"  │  │   "metadata" │  │  "user_data" │         │
  │  │  ┌────────┐  │  │  ┌────────┐  │  │  ┌────────┐  │         │
  │  │  │MemTable│  │  │  │MemTable│  │  │  │MemTable│  │         │
  │  │  └────────┘  │  │  └────────┘  │  │  └────────┘  │         │
  │  │  ┌────────┐  │  │  ┌────────┐  │  │  ┌────────┐  │         │
  │  │  │  L0    │  │  │  │  L0    │  │  │  │  L0    │  │         │
  │  │  │  L1    │  │  │  │  L1    │  │  │  │  L1    │  │         │
  │  │  │  L2    │  │  │  │  L2    │  │  │  │  L2    │  │         │
  │  │  │  ...   │  │  │  │  ...   │  │  │  │  ...   │  │         │
  │  │  └────────┘  │  │  └────────┘  │  │  └────────┘  │         │
  │  └──────────────┘  └──────────────┘  └──────────────┘         │
  │  Each CF has its own MemTable + levels + compaction config     │
  │  All CFs share the same WAL (for atomicity across CFs)        │
  └────────────────────────────────┬────────────────────────────────┘
                                   │
  ┌────────────────────────────────▼────────────────────────────────┐
  │                    BACKGROUND THREADS                           │
  │  ┌──────────────────┐  ┌──────────────────────────────────┐   │
  │  │ Flush Threads    │  │ Compaction Threads               │   │
  │  │ (MemTable→L0)    │  │ (L0→L1→L2→...→LN)              │   │
  │  │ max_background_  │  │ max_background_compactions       │   │
  │  │ flushes = 2      │  │ = 4 (or more)                   │   │
  │  └──────────────────┘  └──────────────────────────────────┘   │
  └────────────────────────────────┬────────────────────────────────┘
                                   │
  ┌────────────────────────────────▼────────────────────────────────┐
  │                     STORAGE LAYER                               │
  │  ┌──────────┐  ┌──────────────────────────────────────────┐   │
  │  │   WAL    │  │          SSTable Files                    │   │
  │  │  Files   │  │  ┌────────┐ ┌────────┐ ┌────────┐       │   │
  │  │          │  │  │000010  │ │000011  │ │000012  │ ...   │   │
  │  │          │  │  │.sst    │ │.sst    │ │.sst    │       │   │
  │  │          │  │  └────────┘ └────────┘ └────────┘       │   │
  │  └──────────┘  └──────────────────────────────────────────┘   │
  │  ┌────────────────────────────────────────────────────────┐   │
  │  │                    MANIFEST                             │   │
  │  │  Tracks: which SSTables belong to which level           │   │
  │  │  Version edits: atomic metadata updates                 │   │
  │  │  Column family configs                                  │   │
  │  └────────────────────────────────────────────────────────┘   │
  └────────────────────────────────────────────────────────────────┘
```

### Column Families

```
COLUMN FAMILIES
================

  Column families are logical partitions within a single RocksDB instance.
  Each column family has:
    - Its own MemTable(s)
    - Its own set of SSTable levels
    - Its own compaction configuration
    - Its own Bloom filter settings
    - Its own compression settings

  They SHARE:
    - The WAL (enables atomic writes across CFs)
    - The block cache (configurable per-CF or shared)
    - The rate limiter
    - Background thread pools

  Use cases:
  ┌─────────────────────────────────────────────────────────────┐
  │ CF "default":   Main user data. Leveled compaction.        │
  │ CF "metadata":  Small metadata. High read rate. Pin in     │
  │                 cache. Leveled compaction. No compression.  │
  │ CF "timeseries": Write-heavy metrics. Universal or FIFO   │
  │                  compaction. ZSTD compression.             │
  │ CF "temp":      Short-lived data. FIFO compaction + TTL.  │
  └─────────────────────────────────────────────────────────────┘

  CockroachDB uses column families to separate MVCC versions.
  TiKV uses column families for "default" (values), "write" (commit info),
  "lock" (lock info), and "raft" (Raft log).
```

### Compression Per Level

```
COMPRESSION STRATEGY PER LEVEL
================================

  Rationale: Upper levels are accessed more frequently (hot data).
  Lower levels hold cold data and benefit more from compression.

  ┌────────┬─────────────────┬──────────────────────────────────┐
  │ Level  │ Compression     │ Rationale                        │
  ├────────┼─────────────────┼──────────────────────────────────┤
  │ L0     │ None or LZ4     │ Written/read most frequently.    │
  │        │                 │ Speed > space savings.           │
  ├────────┼─────────────────┼──────────────────────────────────┤
  │ L1     │ LZ4 or Snappy   │ Frequently accessed. Fast       │
  │        │                 │ decompression is key.            │
  ├────────┼─────────────────┼──────────────────────────────────┤
  │ L2     │ LZ4 or ZSTD     │ Transition zone. Balance.       │
  ├────────┼─────────────────┼──────────────────────────────────┤
  │ L3+    │ ZSTD            │ Cold data. Max compression.     │
  │        │                 │ ~30-50% smaller than LZ4.       │
  │        │                 │ Slower decompress is acceptable. │
  └────────┴─────────────────┴──────────────────────────────────┘

  Compression comparison:
  ┌──────────────┬────────────┬────────────────┬─────────────────┐
  │ Algorithm    │ Ratio      │ Compress Speed │ Decompress Speed│
  ├──────────────┼────────────┼────────────────┼─────────────────┤
  │ None         │ 1.0x       │ N/A            │ N/A             │
  │ Snappy       │ ~1.5-1.8x  │ ~500 MB/s      │ ~1500 MB/s      │
  │ LZ4          │ ~1.8-2.1x  │ ~700 MB/s      │ ~3000 MB/s      │
  │ ZSTD (lvl 1) │ ~2.5-3.0x  │ ~400 MB/s      │ ~1200 MB/s      │
  │ ZSTD (lvl 5) │ ~2.8-3.5x  │ ~150 MB/s      │ ~1200 MB/s      │
  │ ZSTD + Dict  │ ~3.0-4.0x  │ ~300 MB/s      │ ~1000 MB/s      │
  └──────────────┴────────────┴────────────────┴─────────────────┘

  RocksDB config:
    options.compression_per_level = {
      kNoCompression,      // L0
      kLZ4Compression,     // L1
      kLZ4Compression,     // L2
      kZSTD,               // L3
      kZSTD,               // L4
      kZSTD,               // L5
      kZSTD                // L6
    };
```

### Write Stalls

```
WRITE STALLS (BACK-PRESSURE MECHANISM)
========================================

  RocksDB throttles or stops writes when compaction can't keep up:

  ┌─────────────────────────────────────────────────────────────────┐
  │                    WRITE STALL CONDITIONS                       │
  │                                                                 │
  │  SLOWDOWN (soft limit -- throttle writes):                     │
  │  ┌───────────────────────────────────────────────────────────┐ │
  │  │ L0 file count >= level0_slowdown_writes_trigger (20)     │ │
  │  │ OR                                                        │ │
  │  │ Pending compaction bytes >= soft_pending_compaction_limit │ │
  │  │ OR                                                        │ │
  │  │ Memtable count >= max_write_buffer_number - 1            │ │
  │  └───────────────────────────────────────────────────────────┘ │
  │  Effect: Artificial delay inserted before each write.          │
  │  Delay increases exponentially as condition worsens.           │
  │                                                                 │
  │  STOP (hard limit -- block all writes):                        │
  │  ┌───────────────────────────────────────────────────────────┐ │
  │  │ L0 file count >= level0_stop_writes_trigger (36)         │ │
  │  │ OR                                                        │ │
  │  │ Pending compaction bytes >= hard_pending_compaction_limit │ │
  │  │ OR                                                        │ │
  │  │ All write buffers full (no space for new MemTable)       │ │
  │  └───────────────────────────────────────────────────────────┘ │
  │  Effect: All writes BLOCKED until compaction catches up.       │
  │  This is a CRITICAL production issue.                          │
  └─────────────────────────────────────────────────────────────────┘

  Write stall timeline:

  Write rate ▲
             │ ████████████████████  ← normal write rate
             │ ████████████████████
             │ ████████████░░░░░░░░  ← L0=20, slowdown starts
             │ ████████████░░░░░░░░     (throttled)
             │ ████████░░░░░░░░░░░░  ← more throttling
             │ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  ← L0=36, STOP (blocked!)
             │ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
             │ ████████████████████  ← compaction catches up, resume
             └──────────────────────────────────────────► Time

  Monitoring: RocksDB exposes stall metrics via Statistics:
    rocksdb.stall.micros
    rocksdb.l0.slowdown.micros
    rocksdb.memtable.compaction.micros
    rocksdb.l0.num.files.stall.micros
```

### Rate Limiter

```
RATE LIMITER
=============

  Controls total I/O bandwidth used by background operations
  (compaction + flush) to prevent starving foreground reads/writes.

  ┌─────────────────────────────────────────────────────────────┐
  │                    DISK BANDWIDTH                            │
  │                                                              │
  │  Total: 2 GB/s (NVMe)                                       │
  │  ┌──────────────────────────────────────────────────────┐   │
  │  │ Foreground reads/writes:  ~800 MB/s (uncapped)       │   │
  │  ├──────────────────────────────────────────────────────┤   │
  │  │ Background compaction:    ~500 MB/s (rate limited)   │   │
  │  ├──────────────────────────────────────────────────────┤   │
  │  │ Background flushes:       ~200 MB/s (rate limited)   │   │
  │  ├──────────────────────────────────────────────────────┤   │
  │  │ Headroom for OS/other:    ~500 MB/s                  │   │
  │  └──────────────────────────────────────────────────────┘   │
  │                                                              │
  │  RocksDB config:                                             │
  │    rate_limiter = NewGenericRateLimiter(                     │
  │      rate_bytes_per_sec = 500 * 1024 * 1024,  // 500 MB/s  │
  │      refill_period_us = 100000,                // 100ms     │
  │      fairness = 10                             // ratio     │
  │    );                                                        │
  │                                                              │
  │  Auto-tuned: can adapt based on actual I/O latency           │
  └─────────────────────────────────────────────────────────────┘
```

### Merge Operator

```
MERGE OPERATOR (Read-Modify-Write Optimization)
=================================================

  Problem: Incrementing a counter requires:
    1. Read current value
    2. Modify in application
    3. Write new value
  Three operations for one logical update.

  Merge operator: Push the "modify" logic into RocksDB.

  Without Merge:                    With Merge:
  ─────────────                     ───────────
  val = db.Get("counter")          db.Merge("counter", "+1")
  val = val + 1                    // Done! No read needed.
  db.Put("counter", val)
  (Requires read + write)          (Write-only path)

  How it works internally:
  ┌──────────────────────────────────────────────────────────┐
  │ Writes accumulate as MERGE operands:                     │
  │                                                          │
  │ L0: [counter | MERGE | "+5"]                             │
  │ L0: [counter | MERGE | "+3"]                             │
  │ L1: [counter | MERGE | "+1"]                             │
  │ L2: [counter | PUT   | "100"]  ← base value             │
  │                                                          │
  │ On read (or compaction), apply merge operands:           │
  │ 100 → +1 → +3 → +5 → result = 109                      │
  │                                                          │
  │ During compaction, operands are collapsed:               │
  │ L2: [counter | PUT | "109"]  ← fully merged             │
  └──────────────────────────────────────────────────────────┘

  Use cases:
  - Counters (increment/decrement)
  - Append to a list/set
  - Partial JSON updates
  - HyperLogLog merges

  Types:
    Full Merge:    base_value + [operand1, operand2, ...] → new_value
    Partial Merge: operand1 + operand2 → combined_operand
                   (reduces merge chain without base value)
```

### RocksDB Production Tuning Guide

```
ROCKSDB TUNING CHEAT SHEET
============================

  ┌──────────────────────────────────────────────────────────────────┐
  │ WRITE-OPTIMIZED CONFIGURATION                                   │
  ├──────────────────────────────────────────────────────────────────┤
  │ write_buffer_size = 256MB          // Larger MemTable           │
  │ max_write_buffer_number = 5        // More write buffers        │
  │ min_write_buffer_number_to_merge=2 // Merge before flush        │
  │ level0_file_num_compaction_trigger = 8  // Delay L0 compaction  │
  │ level0_slowdown_writes_trigger = 20                             │
  │ level0_stop_writes_trigger = 36                                 │
  │ compaction_style = kCompactionStyleUniversal                    │
  │ max_background_compactions = 8                                  │
  │ max_background_flushes = 4                                      │
  │ target_file_size_base = 256MB                                   │
  ├──────────────────────────────────────────────────────────────────┤
  │ READ-OPTIMIZED CONFIGURATION                                    │
  ├──────────────────────────────────────────────────────────────────┤
  │ write_buffer_size = 64MB           // Standard                  │
  │ max_write_buffer_number = 3                                     │
  │ level0_file_num_compaction_trigger = 4  // Compact L0 quickly   │
  │ compaction_style = kCompactionStyleLevel                        │
  │ max_bytes_for_level_base = 256MB   // L1 target size            │
  │ max_bytes_for_level_multiplier = 10 // Size ratio               │
  │ bloom_locality = 1                 // Cache-friendly Bloom      │
  │ optimize_filters_for_hits = true   // No Bloom on bottom level  │
  │ block_cache_size = 8GB             // Large block cache         │
  │ pin_l0_filter_and_index = true     // Pin L0 metadata in cache  │
  ├──────────────────────────────────────────────────────────────────┤
  │ SPACE-OPTIMIZED CONFIGURATION                                   │
  ├──────────────────────────────────────────────────────────────────┤
  │ compaction_style = kCompactionStyleLevel                        │
  │ compression_per_level = [kNoComp, kLZ4, kZSTD, kZSTD, ...]    │
  │ bottommost_compression = kZSTD                                  │
  │ level_compaction_dynamic_level_bytes = true                     │
  │ max_bytes_for_level_multiplier = 10                             │
  │ target_file_size_base = 64MB       // Smaller files             │
  └──────────────────────────────────────────────────────────────────┘

CRITICAL SETTINGS TO ALWAYS CONFIGURE:
───────────────────────────────────────
  1. block_cache: Size to ~1/3 of available RAM (after OS + app overhead)
  2. write_buffer_size: Balance between flush frequency and recovery time
  3. max_background_compactions: Set to number of CPU cores / 4
  4. rate_limiter: Set to ~50-70% of disk bandwidth
  5. compression: ZSTD for deeper levels, LZ4 for upper
  6. bloom_bits_per_key: 10 (1% FP) or 14 (0.1% FP)
```

---

## 7. LevelDB

### Original Google Implementation

```
LEVELDB ARCHITECTURE
=====================

  Created by Jeff Dean and Sanjay Ghemawat at Google (2011).
  The "reference implementation" of LSM trees.
  Simpler than RocksDB -- single-threaded compaction, no column families.

  ┌──────────────────────────────────────────────────┐
  │                  LevelDB                          │
  │                                                   │
  │  ┌────────────┐    ┌────────────────────────┐    │
  │  │  MemTable  │    │   Immutable MemTable   │    │
  │  │ (skip list)│    │   (being flushed)      │    │
  │  └─────┬──────┘    └───────────┬────────────┘    │
  │        │                       │                  │
  │        ▼                       ▼                  │
  │  ┌────────────┐    ┌────────────────────────┐    │
  │  │    WAL     │    │  .ldb SSTable files    │    │
  │  │  (log file)│    │  Level 0: ≤4 files     │    │
  │  └────────────┘    │  Level 1: 10 MB total  │    │
  │                    │  Level 2: 100 MB total  │    │
  │                    │  Level 3: 1 GB total    │    │
  │                    │  Level 4: 10 GB total   │    │
  │                    │  Level 5: 100 GB total  │    │
  │                    │  Level 6: 1 TB total    │    │
  │                    └────────────────────────┘    │
  └──────────────────────────────────────────────────┘

KEY DIFFERENCES: LevelDB vs RocksDB
─────────────────────────────────────
  ┌──────────────────────┬────────────────┬─────────────────────┐
  │ Feature              │ LevelDB        │ RocksDB             │
  ├──────────────────────┼────────────────┼─────────────────────┤
  │ Compaction threads   │ 1 (single)     │ Multiple (parallel) │
  │ Column families      │ No             │ Yes                 │
  │ Compression          │ Snappy only    │ Snappy, LZ4, ZSTD  │
  │ Bloom filters        │ Basic          │ Full + Blocked +    │
  │                      │                │ Ribbon + Prefix     │
  │ Compaction styles    │ Leveled only   │ Leveled, Universal, │
  │                      │                │ FIFO, Custom        │
  │ Write batch          │ Basic          │ WriteBatchWithIndex  │
  │ Merge operator       │ No             │ Yes                 │
  │ Backup/Checkpoint    │ No             │ Yes                 │
  │ Column families      │ No             │ Yes                 │
  │ Rate limiter         │ No             │ Yes                 │
  │ Write stall mgmt     │ Basic          │ Sophisticated       │
  │ Block cache          │ Basic LRU      │ LRU + Clock cache   │
  │ Statistics           │ Minimal        │ Extensive           │
  │ Transactions         │ No             │ Pessimistic +       │
  │                      │                │ Optimistic           │
  │ Max DB size          │ ~few GB        │ Multi-TB            │
  │ Maintenance          │ Minimal        │ Active (Meta/FB)    │
  └──────────────────────┴────────────────┴─────────────────────┘

  LevelDB is still used in:
    - Chrome browser (IndexedDB backend)
    - Bitcoin Core (UTXO set, chainstate)
    - Ethereum (go-ethereum, historical state)
    - Embedded applications needing a simple KV store
```

### LevelDB Table Format

```
LEVELDB SSTABLE FORMAT (.ldb / .sst)
======================================

  ┌─────────────────────────┐
  │     Data Block 0        │  ← Key-value pairs, prefix compressed
  ├─────────────────────────┤
  │     Data Block 1        │
  ├─────────────────────────┤
  │     ...                 │
  ├─────────────────────────┤
  │     Data Block N        │
  ├─────────────────────────┤
  │   Meta Block            │  ← Bloom filter (if enabled)
  │   (filter data)         │
  ├─────────────────────────┤
  │   Meta Index Block      │  ← Points to filter block
  ├─────────────────────────┤
  │   Index Block           │  ← separator_key → data_block_handle
  ├─────────────────────────┤
  │   Footer (48 bytes)     │  ← meta_index_handle + index_handle
  │                         │     + magic number
  └─────────────────────────┘

  Simpler than RocksDB: no compression dictionary block,
  no range deletion block, no properties block.

  Default block size: 4 KB
  Default SSTable size: 2 MB (much smaller than RocksDB's 64 MB)
```
