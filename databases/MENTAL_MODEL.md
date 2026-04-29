# Database Mental Model: From A to Z

The other docs in this folder go deep on individual layers. This file is the **map**: it shows how every layer connects, the order to build them in, and exactly which file/class implements each piece. Read this first; use it as the index when the per-topic files get dense.

If you only ever read one page in this folder, read this one.

---

## Table of Contents

1. [The One-Page Picture](#1-the-one-page-picture)
2. [The Four Universal Pipelines](#2-the-four-universal-pipelines)
3. [The Build Order: Phase 0 → Phase 16](#3-the-build-order-phase-0--phase-16)
4. [Component Responsibility Map](#4-component-responsibility-map)
5. [Cross-Cutting Concerns (the 4 Hard Problems)](#5-cross-cutting-concerns-the-4-hard-problems)
6. [Variant Decision Tree](#6-variant-decision-tree)
7. [End-to-End Trace of One Query](#7-end-to-end-trace-of-one-query)
8. [Linear Reading Order](#8-linear-reading-order)
9. [Common Pitfalls When Building Your Own](#9-common-pitfalls-when-building-your-own)

---

## 1. The One-Page Picture

A database is a stack of layers. Each layer talks only to its neighbors. If you can hold this diagram in your head, every other doc in this folder slots into one of these boxes.

```
┌────────────────────────────────────────────────────────────────────────┐
│  CLIENT  (psql, app, driver)                                           │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │  SQL text over wire protocol
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  SESSION / WIRE LAYER         (auth, connection pool, protocol parser) │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  QUERY ENGINE                                              ─── doc 04  │
│   Lexer → Parser → Analyzer → Rewriter → Optimizer → Executor          │
│   (Volcano / vectorized / push-based iterators, join algorithms,       │
│    aggregation, sorting, parallelism, EXPLAIN)                         │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │  next_tuple() pull, or push of batches
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  ACCESS METHODS                                            ─── doc 03  │
│   SeqScan · IndexScan · BitmapScan · TidScan · SampleScan              │
│   (the bridge between executor and storage)                            │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  TRANSACTION + CONCURRENCY CONTROL                ─── docs 05, 17, 18  │
│   MVCC (snapshot, xmin/xmax) · 2PL · OCC · SSI                         │
│   Lock manager (row/table) · Latches (page) · Isolation enforcement    │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  INDEXES                                                   ─── doc 06  │
│   B+Tree · Hash · GiST/GIN · BRIN · Bloom · LSM-internal · vector      │
│   (point lookups, range scans, sorted iteration)                       │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │  read_page / write_page
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  STORAGE ENGINE                                  ─── docs 01, 02, 13   │
│   ┌────────────────┐ ┌──────────────┐ ┌──────────────────────────┐   │
│   │  Buffer Pool   │ │ WAL Manager  │ │ Heap / SSTable / Memtable │   │
│   │  (clock/LRU,   │ │ (LSN, flush, │ │ Slotted pages, encoding,  │   │
│   │   pin/unpin)   │ │  ARIES recvr)│ │ row vs columnar           │   │
│   └────────┬───────┘ └──────┬───────┘ └────────────┬──────────────┘   │
│            └────────────────┴──────────────────────┘                   │
│                             │  fixed-size page reads/writes            │
└─────────────────────────────┼──────────────────────────────────────────┘
                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│  OS + HARDWARE                                             ─── doc 00  │
│   syscalls (pread/pwrite/fsync/io_uring) · page cache · O_DIRECT       │
│   filesystem · block layer · I/O scheduler · NVMe/SSD/HDD              │
│   CPU caches, TLB, NUMA, virtual memory                                │
└────────────────────────────────────────────────────────────────────────┘

         ╔══════════════════════════════════════════════════════════╗
         ║   DISTRIBUTION (orthogonal — wraps any of the above)     ║
         ║   ─── docs 12, 16, 19                                    ║
         ║   Replication · Sharding · Consensus (Raft/Paxos)        ║
         ║   Failure detection · Leader election · Distributed txn  ║
         ║   Time (HLC, TrueTime) · Consistency models              ║
         ╚══════════════════════════════════════════════════════════╝

         ╔══════════════════════════════════════════════════════════╗
         ║   WORKLOAD VARIANTS — same building blocks, different mix║
         ║   OLTP (07) · OLAP (08) · HTAP (09) · In-Memory (10)     ║
         ║   Vector search (11) · LSM-based (13)                    ║
         ╚══════════════════════════════════════════════════════════╝
```

**The key intuition.** Everything in the database boils down to: read a page, write a page, log an intention. Every higher concept — joins, indexes, transactions, replication — is just a clever choreography of those three primitives.

---

## 2. The Four Universal Pipelines

A database has exactly four hot paths. Memorize these flows and you can reason about any feature.

### 2.1 Read Path: `SELECT * FROM users WHERE id = 42`

```
SQL text
  │
  ▼  [Query Engine — doc 04]
Lex → Parse → AST → Analyze (resolve catalog) → Rewrite → Optimize
  │       (cost model picks IndexScan on users_pk)
  ▼
Physical plan: IndexScan(users, id=42) → Project(*)
  │
  ▼  [Access Method — doc 03]
IndexScan asks B+Tree for matching tuple ID (TID = page_id, slot)
  │
  ▼  [Index — doc 06]
B+Tree root → internal → leaf → returns TID(page=137, slot=4)
  │
  ▼  [Buffer Pool — doc 01]
buffer_pool.fetch_page(137):
   → hit?  return frame                       ── fast path, microseconds
   → miss? evict victim (clock/LRU), pread()  ── slow path, milliseconds
  │
  ▼  [Storage Engine — doc 01, 02]
Slotted page: read slot[4] → tuple bytes
Deserialize using schema → row dict
  │
  ▼  [MVCC — doc 05]
Visibility check: is this version visible to my txn snapshot?
   xmin <= my_xid AND (xmax > my_xid OR xmax = 0)
   → yes → return; no → walk version chain or skip
  │
  ▼  [Executor]
Project columns, filter further, return tuple to client
```

**Where each doc fits:** parsing/optimization → 04 · index → 06 · buffer/page → 01, 02 · access methods → 03 · MVCC → 05 · OS-level read → 00.

### 2.2 Write Path: `INSERT INTO users VALUES (42, 'Ana')`

```
INSERT statement
  │
  ▼  [Txn Manager — doc 05]
Begin txn → assign xid → snapshot
  │
  ▼  [Query Engine]
Plan: InsertOp(users, tuple)
  │
  ▼  [Heap File — doc 03]
Find a page with free space (FSM) → page 88
  │
  ▼  [Buffer Pool]
fetch(88), pin, take page latch (X)          ── doc 17
  │
  ▼  [WAL — doc 14]   ★ MUST happen BEFORE the page mutation is durable ★
Write WAL record:  {LSN=…, xid, type=INSERT, page=88, slot=?, after-image}
Update page header: page_lsn = wal_lsn
  │
  ▼  [Page mutation]
Slotted page: insert tuple at slot, update slot directory, mark dirty
  │
  ▼  [Indexes — doc 06]
For each index on users: insert (key=42, TID=(88, slot)) — also WAL-logged
  │
  ▼  [Commit]
WAL: append COMMIT record(xid)
fsync WAL up to commit LSN  ★ this is the durability point ★
  │
  ▼
Release latches, unpin page, mark txn committed in TxnTable
  │
  ▼  [Asynchronous]
Dirty page eventually written to disk by background flusher
   (NOT required for durability — WAL already has it)
```

**Two non-negotiable rules.** (1) WAL is written **before** the page is dirtied is allowed to leave the buffer pool (Write-Ahead Logging). (2) Commit returns to the client only **after** WAL fsync. Get either wrong and your DB silently corrupts under crash.

### 2.3 Commit Path (the critical second of a transaction's life)

```
client: COMMIT
  │
  ▼
Validate (OCC) or release no locks yet (2PL/MVCC)
  │
  ▼  [WAL]
Append COMMIT log record with xid, commit LSN
Group-commit: bundle with other txns to amortize fsync         ── doc 14
  │
  ▼
fsync(WAL fd) up to COMMIT LSN                                 ── doc 00
  │     (this is what makes the txn durable; ~10µs on NVMe)
  │
  ▼  [Visibility]
Update commit timestamp / mark xid as COMMITTED in shared txn map
  │
  ▼
Release row locks (2PL), drop xact MVCC slot                  ── doc 17
  │
  ▼
[Replication — doc 12]  send WAL to replicas (sync waits, async doesn't)
  │
  ▼
Reply to client: "COMMIT OK"
```

### 2.4 Recovery Path (ARIES, after crash)

```
Database starts. Last checkpoint LSN = C. WAL ends at LSN E.
  │
  ▼  Pass 1: ANALYSIS (C → E)
Scan WAL forward. Rebuild:
  - Active Transaction Table  (txns alive at crash)
  - Dirty Page Table           (pages with updates not flushed)
  │
  ▼  Pass 2: REDO (from oldest dirty page LSN → E)
For each WAL record:
  if page.lsn < record.lsn: re-apply the change (idempotent)
  → after this pass, pages are *exactly* what they would have been
    at crash time, including changes from uncommitted txns
  │
  ▼  Pass 3: UNDO (E → backwards, only loser txns)
For each active txn at crash: undo its records, writing CLR
  (Compensation Log Records, so undo itself is idempotent)
  │
  ▼
DB online. Clients reconnect. No data loss for committed txns.
```

**Mental model:** WAL = "what *should* have happened." After crash, replay the WAL onto whatever the disk happens to look like, then unwind anything uncommitted. The disk pages are essentially a cache that the WAL is the source of truth for.

---

## 3. The Build Order: Phase 0 → Phase 16

If you sat down to build a database from scratch, this is the order. Each phase depends on the previous ones. Skipping ahead is what makes the existing docs feel "messy" — they describe phase 12 features assuming you've internalized phase 3.

`simpledb.py` follows this exact order. The "Class" column points at the concrete code you can read.

| Phase | What you build | Why now | Doc | Class in `simpledb.py` |
|---|---|---|---|---|
| **0** | Mental model of OS + hardware | Pages exist because SSDs erase in blocks. fsync exists because page cache lies. You can't reason about the layers above until you know what `pread` actually does. | [00](./00-os-and-hardware-internals.md) | — |
| **1** | DiskManager: open file, read/write fixed-size pages by ID | The `page_id → byte offset` mapping is the foundation of every storage engine. | [01](./01-storage-engine-fundamentals.md) §1–4 | `DiskManager` (L119) |
| **2** | SlottedPage: header + slot directory + data growing toward each other | Variable-length tuples on a fixed-size page require indirection. Every RDBMS uses some flavor of this. | [01](./01-storage-engine-fundamentals.md) §3, [02](./02-data-storage-formats-and-encoding.md) | `SlottedPage` (L193) |
| **3** | BufferPool: fetch/pin/unpin/dirty + clock or LRU eviction | Disk is 100,000× slower than RAM. Without a buffer pool you have a toy. | [01](./01-storage-engine-fundamentals.md) §5–6 | `BufferPool` (L350) |
| **4** | Tuple encoding + schema + null bitmap | You need a byte-level layout before indexes can store keys. | [02](./02-data-storage-formats-and-encoding.md) | `Schema`, `serialize_tuple` (L665, L685) |
| **5** | HeapFile: unordered table of tuples across pages | The simplest table. Sequential scan works. Now you have something to query. | [03](./03-access-methods-and-table-scans.md) §1–2 | `HeapFile` (L576) |
| **6** | WAL: append-only log of intentions, with LSN | The moment you can crash safely. Without WAL, every kill -9 corrupts. | [14](./14-write-ahead-log-internals.md), [01](./01-storage-engine-fundamentals.md) §8–9 | `WALManager` (L480) |
| **7** | B+Tree index: search, insert with split, range scan | First non-trivial access method. Powers point lookups + ORDER BY. | [06](./06-indexing-internals.md) §B+Tree | `BPlusTree` (L828) |
| **8** | TransactionManager + MVCC headers (xmin/xmax) | Multiple users without corruption. This is when "database" stops being "file format". | [05](./05-transactions-and-concurrency.md) §1–4, §7 | `TransactionManager` (L1020) |
| **9** | Latches (page-level), Lock manager (row-level), deadlock detection | Concurrency primitives. Latches protect data structures, locks protect logical objects. | [17](./17-latches-and-locks-internals.md), [18](./18-concurrency-control-and-scheduling.md) | (latches: ad-hoc) |
| **10** | Query engine: lexer → parser → planner → Volcano executor | Now SQL works. You have a database. | [04](./04-query-engine-internals.md) | `lex`, `Parser`, `SeqScanOp`, `IndexScanOp`, `FilterOp`, `ProjectOp` (L1470, L1595, L1841…) |
| **11** | Cost-based optimizer: stats, selectivity, join orderings | Same SQL, 1000× faster plan. Where DB engineering becomes interesting. | [04](./04-query-engine-internals.md) §4–5, [15](./15-sql-performance-deep-dive.md) | (not in simpledb) |
| **12** | Pick a workload variant (or two) | OLTP vs OLAP isn't a different DB — it's a different mix of the same parts. | [07](./07-oltp-databases.md) [08](./08-olap-databases.md) [09](./09-htap-databases.md) [10](./10-in-memory-databases.md) | — |
| **13** | LSM tree path (alternative storage): memtable → SSTable → compaction → bloom | Write-heavy workloads. RocksDB/Cassandra. | [13](./13-lsm-trees-and-compaction.md) | `MemTable`, `SSTable`, `LSMTree`, `BloomFilter` (L1109, L1151, L1183, L1302) |
| **14** | Specialty indexes (GiST/GIN/BRIN/HNSW) | Postgres ships these. Pick one to stretch the index abstraction. | [06](./06-indexing-internals.md), [11](./11-vector-search-internals.md), [11-hnsw](./11-hnsw-vector-search-internals.md) | — |
| **15** | Replication: ship WAL to N replicas (sync or async) | Single-node fails too often. Replication is "WAL but over a socket". | [12](./12-replication-and-distributed-storage.md) §1–2 | — |
| **16** | Distributed: failure detection (gossip/phi-accrual), leader election (Raft), sharding, distributed txn (2PC, Percolator, Calvin) | Scale beyond one machine. Almost all difficulty here is about **time and partial failure**. | [12](./12-replication-and-distributed-storage.md), [16](./16-failure-detection-and-leader-election.md), [19](./19-distributed-databases-deep-dive.md), `failure_detection_*.py` | — |

**The sentence to remember.** *Phases 0–10 build a database. Phase 11 makes it fast. Phases 12–14 specialize it. Phases 15–16 scale it.* Most production complaints are mis-tuned phase 11 + 13. Most outages are phase 16.

---

## 4. Component Responsibility Map

When something breaks (or when you read someone else's DB code), this is how to attribute blame.

| Component | Owns | Doesn't own | Doc | simpledb class |
|---|---|---|---|---|
| **DiskManager** | Page-aligned reads/writes, file growth | Caching, durability | 00, 01 | `DiskManager` |
| **Buffer Pool** | Page cache, eviction policy, pin counts | Durability, transaction visibility | 01 | `BufferPool` |
| **WAL Manager** | Append, fsync, LSN, recovery | Page contents, transaction logic | 14 | `WALManager` |
| **Slotted Page** | Tuple placement within one 4KB page | Cross-page joins, indexing | 01, 02 | `SlottedPage` |
| **Heap File** | Tuples → page sequence (unordered) | Ordering, uniqueness | 03 | `HeapFile` |
| **B+Tree** | Sorted key→TID, range, splits | Tuple storage, txn visibility | 06 | `BPlusTree` |
| **TxnManager** | xid alloc, snapshot, commit/abort, MVCC visibility | Locking, durability | 05 | `TransactionManager` |
| **Lock Manager** | Row/table logical locks, deadlock detection | Page integrity (that's latches) | 05, 17 | — |
| **Latches** | Short-term mutual exclusion on in-memory structures | Logical correctness across txns | 17, 18 | (ad-hoc) |
| **Parser** | SQL → AST | Semantics | 04 | `lex`, `Parser` |
| **Analyzer** | Catalog binding, type checking | Performance | 04 | (in `Parser`) |
| **Optimizer** | Best plan given stats | Correctness (any plan must be correct) | 04, 15 | (none — naive plans) |
| **Executor** | Run plan, return tuples | Plan choice | 04 | `*Op` operators |
| **Catalog** | Schema, indexes, stats | Data | 01 | `TableInfo`, `SimpleDB` |
| **MemTable + SSTable** | Write-optimized path (LSM) | B+Tree path | 13 | `MemTable`, `SSTable` |
| **Replicator** | Ship WAL to replicas, apply remotely | Conflict resolution (consensus does that) | 12, 19 | — |
| **Consensus** | Linearizable single-leader log | Storage, query | 12, 16, 19 | — |
| **Failure detector** | "Is node X alive?" with bounded false positives | Recovery action | 16 | `failure_detection_*.py` |

The diagonal observation: each component owns *exactly one* concern. When two components seem to overlap (e.g., "do I check visibility in the access method or the executor?"), production DBs split it the way the table above does. Crossing that line is the source of most bugs.

---

## 5. Cross-Cutting Concerns (the 4 Hard Problems)

Every database, no matter the variant, must solve four problems simultaneously. The docs in this folder mostly exist because each problem has many possible solutions.

### 5.1 Durability — "did my write survive the crash?"

Mechanism: **WAL + fsync**. Mostly the same everywhere.
- WAL flushed before commit returns: D in ACID
- Pages can lag behind WAL on disk: that's the whole point
- ARIES gives you redo + undo so partial flushes are recoverable
- Hardware: fsync forces the kernel page cache to disk, but you must have the right write barriers (see 00 §13). On consumer SSDs without power-loss protection, fsync can lie.

**Where it's covered:** docs 00 (fsync), 01 (WAL basics), 14 (deep ARIES), 13 (LSM-WAL).

### 5.2 Concurrency — "what if two clients touch the same row?"

Mechanism choices: **2PL, MVCC, OCC, SSI**. Production databases have specific combos:
- Postgres: MVCC + 2PL on writes (snapshot isolation; SSI optional)
- MySQL/InnoDB: MVCC + next-key locking
- SQL Server: 2PL by default; MVCC opt-in
- CockroachDB: MVCC + SSI
- Oracle: MVCC + minimal locking

**Where it's covered:** doc 05 (everything), 17 (latches vs locks distinction — most beginner confusion lives here), 18 (scheduling).

### 5.3 Consistency — "do replicas agree?"

Single-node: free (the buffer pool is the truth).
Multi-node: hard. Choices:
- Strong/linearizable: Raft/Paxos consensus on the log (Spanner, CockroachDB, etcd)
- Snapshot/serializable across shards: HLC + 2PC, or Percolator, or Calvin
- Eventual: anti-entropy, CRDTs (DynamoDB, Cassandra)

**Where it's covered:** docs 12 (Raft, sharding), 19 (HLC, TrueTime, Percolator, Calvin, CRDTs), 16 (leader election).

### 5.4 Performance — "fast enough at the right scale"

The two performance laws every DB engineer must internalize:
1. **The latency hierarchy** (doc 00 §2): L1 < L2 < L3 < RAM < SSD < network < HDD, each ~10× slower. Every architectural choice is about pushing work up this hierarchy.
2. **Sequential >> random**: even on NVMe, sequential I/O is ~5× faster than random. LSM trees, columnar layouts, log-structured writes, group commit — all variations on this theme.

**Where it's covered:** docs 00 (hierarchy), 04 §6 (vectorized vs Volcano), 08 (columnar), 13 (LSM), 15 (SQL perf).

---

## 6. Variant Decision Tree

"Build my own DB" only makes sense once you've decided which DB. Same building blocks, different mix.

```
What's the workload?
│
├── Lots of small reads/writes per row (orders, users, sessions)
│   → OLTP, row store, B+Tree, MVCC, WAL                                ─── doc 07
│   Examples: Postgres, MySQL, Oracle
│   Build phases 0–10, polish 11.
│
├── Lots of writes, eventually queried (logs, time series, IoT, KV)
│   → LSM tree                                                          ─── doc 13
│   Examples: RocksDB, Cassandra, ScyllaDB
│   Build phase 13 instead of 7's B+Tree path. Still need 0–6, 8.
│
├── Few huge analytical scans (BI, dashboards, ML training)
│   → OLAP, columnar, vectorized exec, no MVCC needed                   ─── doc 08
│   Examples: ClickHouse, DuckDB, Snowflake, Spark
│   Phase 4 changes (column encoding), phase 11 dominates.
│
├── Both OLTP + OLAP on same data, no ETL
│   → HTAP                                                              ─── doc 09
│   Examples: TiDB, SingleStore, AlloyDB
│   Two storage layers (row + column) that share txn boundary.
│
├── Microsecond latency, dataset fits in RAM
│   → in-memory, no buffer pool, often log-only                         ─── doc 10
│   Examples: Redis, MemSQL, VoltDB
│   Phase 3 disappears, phase 6 (WAL) becomes the only durability.
│
├── Similarity over embeddings (recommendation, RAG, image search)
│   → vector DB, HNSW or IVF + quantization                             ─── doc 11
│   Examples: pgvector, Pinecone, Milvus, Weaviate
│   Phase 7's B+Tree is replaced by a graph or partition index.
│
└── Single node not enough (capacity, throughput, geo, HA)
    → distributed: pick one
        ├── single-leader replication (Postgres replicas, MySQL)        ─── doc 12
        ├── consensus-replicated single-leader (CockroachDB, Spanner)   ─── docs 12, 19
        ├── leaderless quorum (DynamoDB, Cassandra)                     ─── doc 19
        └── shared-storage (Aurora, Neon, Socrates)                     ─── doc 19
```

**Picking is mostly about read/write ratio and consistency tolerance.** Everything else (language, sharding scheme, cloud provider) is implementation detail.

---

## 7. End-to-End Trace of One Query

Concrete trace for `SELECT name FROM users WHERE id = 42` against a single-node row-store, with one B+Tree index on `users.id`. Every line ties back to a doc and a class.

```
T+0µs   Client sends: "SELECT name FROM users WHERE id = 42\n"
T+5µs   Wire protocol parser → SQL string                    [doc 04 §1]
T+10µs  Lexer: tokens [SELECT, IDENT(name), FROM, ...]       [doc 04 §2 / lex() L1470]
T+15µs  Parser: SelectStmt(cols=[name], from=users, where=BinOp(=, id, 42))
                                                              [doc 04 §2 / Parser L1595]
T+20µs  Analyzer: resolve 'users' in catalog, type-check id=42  [doc 04 §2]
T+25µs  Rewriter: predicate already simple, no-op              [doc 04 §3]
T+30µs  Optimizer: stats say users has 1M rows, id is unique
        → IndexScan(users_pk, id=42) cheaper than SeqScan
                                                              [doc 04 §4 / 15 §3]
T+35µs  Executor instantiates: ProjectOp(name) → IndexScanOp(users_pk, 42)
                                                              [doc 04 §6 / IndexScanOp L1876]
T+40µs  IndexScanOp.next():
          → BPlusTree.search(42)                              [doc 06 / BPlusTree L828]
            → fetch root page from buffer pool                [doc 01 §5 / BufferPool L350]
              → HIT in cache, pin, return frame
            → binary-search keys, descend to leaf page 73
              → MISS, evict victim via clock, pread(page=73)  [doc 00 §7]
                ↓
                kernel: vfs_read → ext4 → blk_mq → NVMe driver  [doc 00 §7]
                ↓ (~80µs on warm NVMe)
                ← page bytes copied into frame
              → checksum verify (CRC32)                       [doc 01 §10]
              → slot 7: TID = (page=2104, slot=3)
T+125µs   ← TID(2104, 3) returned
T+130µs Acquire S latch on page 2104                          [doc 17 §2]
        Buffer pool fetch(2104) → MISS → pread → ~80µs
T+215µs Slotted page: read slot[3] → tuple bytes
        Deserialize via schema → {id:42, name:'Ana', xmin:107, xmax:0}
                                                              [doc 02 / deserialize_tuple L709]
T+220µs MVCC visibility check:
          my_snapshot = {xmax=200, active={150}}
          xmin=107 ≤ 200 AND 107 ∉ active → committed before me
          xmax=0 → not deleted
          → VISIBLE                                            [doc 05 §4 / TransactionManager L1020]
T+225µs ProjectOp picks 'name' = 'Ana', emits {name:'Ana'}
T+230µs Release latch, unpin pages, encode result row
T+240µs Wire protocol: send DataRow + CommandComplete
T+250µs Client receives 'Ana'
```

**What you just watched:**
- 6 layers of code (executor → access method → index → buffer pool → disk manager → kernel)
- Two cache misses (~160µs of the 250µs total — disk dominates if cold, vanishes if hot)
- One MVCC check (cheap; the magic of snapshot isolation)
- Zero locks, zero WAL writes (read path)

Now multiply by 100,000 queries/sec and you understand why each doc obsesses over the inner loop of its layer.

---

## 8. Linear Reading Order

If you want to read every doc once, this order minimizes "wait, what is X?" moments.

1. **MENTAL_MODEL.md** ← you are here. Don't skip.
2. **00** — OS and hardware. Boring until it's not. Sets up *why* the storage engine looks the way it does.
3. **01** — Storage engine fundamentals. Pages, buffer pool, WAL intro. The vocabulary of every later doc.
4. **02** — Data storage formats and encoding. Row vs column, varlena, compression. Short, foundational.
5. **03** — Access methods. The bridge between "I have pages" and "executor wants tuples".
6. **06** — Indexing internals. B+Tree first, then specialty.
7. **14** — WAL deep dive. ARIES. Re-read 01 §8–9 immediately before this.
8. **05** — Transactions and concurrency. The big one. Fold in 17 (latches/locks distinction) and 18 (scheduling) as you go.
9. **17, 18** — Latches and concurrency control internals.
10. **04** — Query engine. Now you have storage + txn, you can reason about plans.
11. **15** — SQL performance. EXPLAIN-driven. Where 04's theory meets real query tuning.
12. **13** — LSM trees. Alternative path to phase 7's B+Tree.
13. **07–10** — OLTP / OLAP / HTAP / In-memory variants. Pick the one matching your goal first; skim the rest.
14. **11, 11-hnsw** — Vector search. Optional unless building a vector DB.
15. **12** — Replication and distributed storage. Single-node assumptions break.
16. **16** — Failure detection and leader election. Re-read with `failure_detection_*.py` open.
17. **19** — Distributed databases deep dive. Modern landscape. Easier after 12 + 16.
18. **simpledb.py** — Read end-to-end last. By this point every layer should look familiar.

For "I just want to build it" mode, follow phases 0–10 in §3 instead of reading docs end-to-end.

---

## 9. Common Pitfalls When Building Your Own

The list of mistakes you (and every textbook DB) will make on the first try.

1. **Mistaking latches for locks.** Latches protect short-term in-memory invariants (e.g., a page being modified). Locks protect logical objects across a transaction. They have different lifetimes, deadlock semantics, and APIs. Mixing them up causes either lost updates or stalls. → doc 17.
2. **Trusting the OS page cache.** mmap looks elegant but you give up control of eviction, prefetch, write ordering, and durability. Production DBs avoid mmap (notable convert: SQLite is the only mainstream exception). → doc 00 §10–11, 01 §5.
3. **Calling `write` and assuming durability.** `write()` returns when bytes are in the kernel page cache. Without `fsync` they can vanish on crash. Worse, on consumer SSDs, fsync can return before the device's volatile cache is flushed. → doc 00 §13, 14 §1.
4. **Locking pages instead of rows.** Page-level locking caps your concurrency at #pages and creates phantom contention. Use row-level locks (with intent locks on the page/table). → doc 05 §5, 17.
5. **MVCC without GC.** The version chain grows forever. Postgres calls this VACUUM; it's not optional. Long-running transactions are the killer because they pin GC. → doc 05 §7.
6. **WAL written after the page mutation.** Reverses the W in WAL. Crash between mutation and log = silent corruption. The rule: `page.lsn ≤ wal_persist_lsn` always. → doc 14 §1, 01 §9.
7. **Single big lock around the buffer pool.** Works at 100 ops/s, dies at 10K. Shard the buffer pool by hash, use lock-free hash tables, or both. → doc 01 §5, 17.
8. **Cost model with no statistics.** Every join order looks equally good without selectivity estimates. Build histograms before you build a real optimizer. → doc 04 §4.
9. **Recovery without ARIES idempotence (CLRs).** Naïve undo is not idempotent: crash during recovery = catastrophe. CLRs make undo redo-able. → doc 14 §5, §7.
10. **Distributed before single-node is solid.** Adding consensus to a buggy storage engine multiplies the bugs. Get phases 0–10 reliable first. → doc 12, 19.
11. **Confusing replication consistency with transaction isolation.** "Async replicated" ≠ "read uncommitted." They're orthogonal. → doc 12 §9, 19 §1.
12. **Heartbeat-only failure detection on flaky networks.** Use phi-accrual or SWIM-style suspicion levels; binary "alive/dead" causes flap storms. → doc 16, `failure_detection_phi_accrual.py`.

---

**TL;DR pipeline.** *SQL → parser → optimizer → executor → access method → index → buffer pool → page → disk*, with **WAL** crosscutting the write side, **MVCC + locks** crosscutting concurrency, and **replication + consensus** wrapping the whole thing for distribution. Build it in that order. Every other doc in this folder is one of those boxes seen up close.
