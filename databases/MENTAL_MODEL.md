# Database Mental Model: From A to Z

The other docs in this folder go deep on individual layers. This file is the **map**: it shows how every layer connects, the order to build them in, and exactly which file/class implements each piece. Read this first; use it as the index when the per-topic files get dense.

If you only ever read one page in this folder, read this one.

Hands-on tasks for every chapter (predict, build, break, measure) are in [LABS.md](./LABS.md).

---

## Table of Contents

- [Start here — the whole map in plain words](#start-here--the-whole-map-in-plain-words)
1. [The One-Page Picture](#1-the-one-page-picture)
2. [The Four Universal Pipelines](#2-the-four-universal-pipelines) — each with *why this path* and *what if it changes*
3. [The Build Order: Phase 0 → Phase 16](#3-the-build-order-phase-0--phase-16)
4. [Component Responsibility Map](#4-component-responsibility-map)
5. [Cross-Cutting Concerns (the 4 Hard Problems)](#5-cross-cutting-concerns-the-4-hard-problems)
6. [Variant Decision Tree](#6-variant-decision-tree)
7. [End-to-End Trace of One Query](#7-end-to-end-trace-of-one-query)
8. [Linear Reading Order](#8-linear-reading-order)
9. [Common Pitfalls When Building Your Own](#9-common-pitfalls-when-building-your-own)
10. [Layer by Layer: Why Exactly This Way](#10-layer-by-layer-why-exactly-this-way)
11. [What If the Architecture Changes? — the change matrix](#11-what-if-the-architecture-changes--the-change-matrix)
12. [Interview Questions and System Design Prompts](#12-interview-questions-and-system-design-prompts)

---

## Start here — the whole map in plain words

**The problem.** A database makes two promises at once: *(1) once I say "saved", your data survives
anything short of losing the disk*, and *(2) I answer quickly, even while thousands of people read
and write the same data at the same time*. Both promises are hard for the same reason: RAM is fast
but forgets everything on power loss, disks remember but are ~1,000× slower, and users collide.
Every layer in this folder exists to keep one of those two promises despite those three facts.

**The everyday analogy: a big library.**

| Library | Database layer | What it does |
|---|---|---|
| The front desk that checks your card | Session / wire layer | Who are you, what language do you speak, one queue per visitor |
| The librarian who plans how to find your books | Query engine (optimizer) | "Walk to shelf 12" vs "check every shelf" — picks the cheapest route |
| The catalogue cards | Indexes | Jump straight to the shelf instead of walking every aisle |
| The rules "one person edits a book at a time; readers see the last published edition" | Transactions + concurrency control | Nobody sees half-written pages; two editors don't overwrite each other |
| The reading desk next to the door | Buffer pool | Keep popular books close so nobody walks to the basement twice |
| The librarian's logbook, written *before* touching any book | WAL | After a fire, replay the logbook to rebuild exactly what was agreed |
| The basement stacks | Disk (pages) | Cheap, huge, slow; books are stored in fixed-size boxes |
| Branch libraries holding copies | Replication / distribution | Survive losing one building, serve more readers |

### One user, all the way through the stack

The scenario: an online shop. Table `users` has **10 million rows** of about **100 bytes** each ≈
**1 GB**. Pages are **8 KB**, so the table is ≈ **125,000 pages**. There is a B+Tree index on
`users.id`. The server has a **256 MB buffer pool** ≈ **32,000 page frames** — it can hold about a
quarter of the table in RAM. The disk is an NVMe SSD (~**80 µs** per random page read). All numbers
are illustrative but consistent with each other.

**Read — a customer logs in:** `SELECT name FROM users WHERE id = 42`

1. **Parse + plan** (doc 04). The text becomes a tree, names are checked against the catalogue,
   and the optimizer compares two routes: read all 125,000 pages (~1 GB) or walk the index (≈4
   pages). It picks the index. *Without care:* stale statistics make it believe the table has 10
   rows, it picks the full scan, and login takes seconds instead of microseconds.
2. **Walk the index** (doc 06). Each 8 KB index page holds ~400 keys, so 10M keys need only **3
   levels** (400³ = 64M). Root and middle pages are touched by every query, so they are always in
   RAM. The leaf page may or may not be. Result: a *tuple ID* — "page 2,104, slot 3".
3. **Fetch the page** (doc 01). The buffer pool is asked for page 2,104. **Hit:** ~1 µs. **Miss:**
   evict a cold page, read from SSD, ~80 µs. *Without a buffer pool:* every one of the ~4 page
   touches is a disk read, and 20,000 logins/s would need 80,000 random reads/s.
4. **Check visibility** (doc 05). The row carries "created by transaction 107, not deleted". Your
   snapshot says 107 committed before you started → you may see it. No lock was taken, so a writer
   updating this row right now does not block you.
5. **Return** `'Ana'`. Total: ~20 µs if everything was hot, ~200 µs with two misses.

**Write — the customer renames themselves:** `UPDATE users SET name = 'Ana B.' WHERE id = 42`

6. **Log first** (doc 14). Append a ~100-byte record "txn 311 changed page 2,104 slot 3 to 'Ana
   B.'" to the write-ahead log (WAL) in memory. Then change the page in the buffer pool and mark it
   *dirty*. The 8 KB page is **not** written to disk now.
7. **Commit = one fsync of the log** (docs 00, 14). `fsync` forces the log to durable storage:
   ~20–50 µs on a datacenter NVMe, ~0.5–2 ms on a cloud network disk. Only then does the client
   hear "COMMIT OK". *Group commit* lets 50 concurrent transactions share one fsync.
8. **Later, in the background**, the dirty page is written to disk. If 30 more updates hit the
   same page first, they all ride on that one write.

**Crash — someone pulls the power cable one second later.**

9. **Recovery** (doc 14). RAM is gone, so the buffer pool (with the dirty page) is gone. On
   restart the database reads the WAL from the last *checkpoint* and re-applies every change the
   disk is missing. The rename is back. A transaction that had not committed is rolled back. *Why
   it works:* the log reached disk before "OK" was sent; the page never needed to.

```
 READ                                         WRITE                              CRASH
 SQL → plan → index (3 levels) → buffer pool  log record (100 B) → page in RAM   replay WAL
 1 query     ~400 keys/page     hit 1 µs /    → fsync log (20 µs–2 ms)           from last
             4 page touches     miss 80 µs    → "OK"  → page to disk later       checkpoint
          → MVCC check → 'Ana'   (~20–200 µs)          (batched, async)          → same state
```

The whole folder is these nine steps, seen up close — plus what changes when you want them faster
(docs 04, 15), on many machines (docs 12, 16, 19), or for a different workload (docs 07–13, 22).

### Key terms in this chapter

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Page | fixed-size block (4–16 KB) — the unit the DB reads and writes | a box in the library basement; you always carry a whole box |
| Tuple / row | one record inside a page | one book in the box |
| TID (tuple ID) | address of a row: (page number, slot number) | "basement box 2,104, position 3" |
| Buffer pool | the DB's own RAM cache of pages | the reading desk near the door |
| Hit / miss | page found in RAM / had to read it from disk | book on the desk / walk to the basement |
| Dirty page | page changed in RAM but not yet written to disk | a book with pencil notes not yet copied into the master |
| WAL (write-ahead log) | append-only list of changes, written before the pages | the librarian's logbook |
| fsync | "OS, put this on durable storage now, and tell me when" | posting the logbook page into the fire safe |
| LSN | position of a record in the WAL; every page remembers the last LSN applied to it | logbook line number |
| Checkpoint | point where all older changes are known to be on disk; recovery starts here | "everything before line 5,000 is already copied into the books" |
| Transaction | a group of changes that all happen or none happen | a bank transfer: debit and credit together |
| MVCC | keep old row versions so readers see a consistent snapshot without locking | readers get the printed edition while an editor works on the draft |
| Lock vs latch | lock = protects a row for a whole transaction; latch = protects a memory structure for microseconds | reserving a meeting room for the afternoon vs holding a door for a second |
| Optimizer | picks the cheapest way to run a query, using statistics | a route planner using traffic data |
| Index | extra structure that maps a key to where rows live | the catalogue |
| Replica | another machine holding a copy, fed from the WAL | a branch library receiving copies of the logbook |
| Amplification | extra bytes read/written/stored per byte the user asked for | carrying a whole box to read one page |

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

> **In plain words.** Plan the route, look the key up in the catalogue, get the box from the desk
> (or the basement), and check you're allowed to see that edition of the book.

**Why exactly this path — what each step solves:**

| Step | Problem it solves | Why it sits *here* in the order |
|---|---|---|
| Optimize before executing | The same SQL has many plans; the best and worst differ by 1,000×+ | Planning costs µs; running a bad plan costs seconds. Decide once, up front |
| Index returns a **TID**, not the row | Many indexes on one table must not each hold a full copy of the row | Indirection: the heap owns the row, indexes own *where it is*. A row can move inside its page (slot directory) without touching any index |
| Buffer pool between everyone and disk | Disk is ~1,000× slower than RAM | Every layer above asks for pages by ID; only one layer decides what stays in RAM |
| MVCC check **after** fetching the tuple | Readers must not block writers | Version info (xmin/xmax) lives *on the row*, so visibility can only be decided once the row is in hand. That's also why indexes can return invisible rows and the heap must re-check |

**What if the architecture changes:**

| Change | What happens to the read path | Who does it |
|---|---|---|
| **Clustered index** instead of heap | Row lives inside the PK B+Tree leaf → PK lookup is one tree walk, no heap hop. Secondary indexes store the *PK*, not a TID → secondary lookup = two tree walks | InnoDB, SQL Server (clustered), SQLite (rowid tables) |
| **Index-only scan** | If all needed columns are in the index *and* the page is known all-visible, skip the heap entirely | Postgres (visibility map), covering indexes everywhere |
| **LSM tree** instead of B+Tree | Read = memtable → L0 files → L1 → … ; bloom filters skip most files, but a miss can still touch several levels (**read amplification**) | RocksDB, Cassandra, ScyllaDB |
| **Columnar** storage | No per-row lookup; read only the needed columns, skip blocks via min/max (zone maps). Point lookups get *slower*, scans of 3 columns out of 100 get ~30× less I/O | ClickHouse, DuckDB, Parquet |
| **In-memory** | Buffer pool disappears; index points straight at the row in RAM | Redis, VoltDB, SAP HANA row store |
| **Read replica** | Same path, but on a copy that is behind the primary by the replication lag → stale reads | Any async replica |
| **Sharded / distributed** | A routing step (which shard owns `id=42`?) and a read timestamp (HLC/TrueTime) are added in front | CockroachDB, Spanner, Vitess |

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

**Two non-negotiable rules.** (1) The WAL record describing a change must be on disk **before** the dirty page it describes is allowed to leave the buffer pool (Write-Ahead Logging). (2) Commit returns to the client only **after** WAL fsync. Get either wrong and your DB silently corrupts under crash.

> **In plain words.** Write one line in the logbook, change the book on the desk, and only when
> the logbook line is in the fire safe say "done". The book goes back to the basement whenever it's
> convenient.

**Why exactly this path — what it solves:**

- **Log first, pages later.** A commit touches pages scattered across the disk (heap page + one
  page per index). Writing all of them at commit = several *random* writes. Writing a ~100-byte
  log record = one *sequential* append. The WAL turns "make N random pages durable now" into
  "make one sequential log durable now, fix the pages later".
- **Pages written lazily.** A hot page updated 1,000 times per second is written to disk once per
  checkpoint, not 1,000 times (**write absorption**).
- **Free-space map before insert.** Finding a page with room by scanning the table would make
  every insert O(table size).
- **Indexes updated in the same transaction.** Otherwise a crash leaves the index pointing at rows
  that don't exist, or rows no index can find.
- **`page_lsn` stamped on each page.** It lets recovery ask "has this page already seen this log
  record?" — the key to idempotent redo (§2.4).

**What if the architecture changes:**

| Change | Effect on the write path | Trade-off |
|---|---|---|
| **No WAL, force pages at commit** | Commit = random write of every touched page; a crash mid-write leaves a *torn* page (half old, half new) | ~10× slower commits and still unsafe, unless you add shadow paging / copy-on-write (LMDB, the old SQLite rollback journal) |
| **LSM tree** | Write = WAL append + in-memory memtable insert. No page read-modify-write at all | Very fast ingest; the cost moves to background **compaction** (write amplification 10–30×) and to reads |
| **Postgres heap vs InnoDB update-in-place** | Postgres writes a *new tuple version* and, unless it's a HOT update, a new entry in *every* index. InnoDB updates in place and writes the old value to an undo log | Postgres: simple MVCC, but index write amplification and VACUUM. InnoDB: fewer index writes, but undo purge and secondary-index indirection |
| **Columnar store** | Rows can't be appended column-by-column cheaply → writes land in a row-format delta/buffer, merged into columns in bulk | Great scans, poor single-row writes (see doc 09 HTAP) |
| **Torn-page protection** | Postgres logs a *full page image* on the first change after each checkpoint; InnoDB uses a doublewrite buffer | Protects against 8 KB pages on 4 KB-atomic hardware, costs extra WAL volume right after checkpoints |

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

> **In plain words.** "Committed" means exactly one thing: the log record saying so is on durable
> storage. Everything else — pages, replicas, cleanup — can happen before or after, but the reply
> waits for that one fsync.

**Why exactly this path:**

- **The durability point is one fsync of one file.** It's the cheapest possible thing that
  survives power loss. Everything that can be deferred is deferred.
- **Group commit.** fsync costs roughly the same for 1 record or 50. With 50 transactions waiting,
  one fsync commits all of them: on a 1 ms cloud disk this is the difference between ~1,000 and
  ~50,000 commits/s.
- **Locks are released *after* the commit record is durable** (strict 2PL). Release earlier and
  another transaction could read a value that a crash then erases (dirty read → cascading abort).
- **Reply is last.** If the client hears "OK" before the fsync, a crash in between loses a
  transaction the client believes is safe.

**What if the architecture changes:**

| Change | Where the durability point moves | Trade-off |
|---|---|---|
| **Async commit** (`synchronous_commit = off`, `innodb_flush_log_at_trx_commit = 2`) | Reply before fsync; WAL flushed every ~200 ms–1 s | Crash loses the last fraction of a second of commits — but never corrupts. Fine for clicks/metrics, not for payments |
| **Sync replication** | Reply after the replica *also* has the WAL | RPO = 0 on primary loss; commit latency += network RTT (~0.5 ms same zone, 30–100 ms cross-region) |
| **Consensus (Raft/Paxos)** | Reply after a *majority* has the log entry | Survives f failures with 2f+1 nodes, no manual failover; every write pays one quorum round trip |
| **Distributed transaction across shards** | Two-phase commit: all shards *prepare* (durable), then coordinator logs *commit* | Two rounds instead of one; a coordinator crash between phases leaves participants blocked holding locks |
| **Spanner-style commit wait** | Reply after waiting out clock uncertainty (a few ms) | External consistency across the globe, paid for in latency on every write |
| **Shared-storage (Aurora, Neon)** | Reply after a write quorum of storage nodes (Aurora: 4 of 6) has the *log* — compute never writes pages | Fast failover and storage that scales separately; you depend on a custom storage tier |

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

> **In plain words.** After the fire, open the logbook at the last "everything before here is
> safe" bookmark, redo every line, then cross out the work of anyone who hadn't signed off.

**Why exactly this path — the buffer-pool policy it makes safe.** Two choices decide how hard
recovery is:

| | **No-force** (don't flush pages at commit) | **Force** (flush every page at commit) |
|---|---|---|
| **Steal** (may flush uncommitted pages to free RAM) | Fast commits + bounded RAM. Needs **redo** *and* **undo** → ARIES | Needs undo only; slow commits |
| **No-steal** (never flush uncommitted pages) | Needs redo only; a huge transaction must fit in RAM | No recovery needed; slowest and RAM-bound |

Every serious disk database picks **steal + no-force**, because it gives the fastest commits and
the most flexible buffer pool. ARIES exists to make that choice safe:

- **Analysis** rebuilds "who was running, which pages were dirty" — because RAM lost that.
- **Redo repeats history**, including uncommitted work — because that is simple, and the
  `page_lsn` check makes it idempotent (a crash *during* recovery just restarts it).
- **Undo** rolls back losers and writes CLRs — so undo itself is never undone twice.
- **Checkpoints** bound how much WAL must be replayed → they bound **recovery time**
  (RTO). More frequent checkpoints = faster recovery, but more background I/O.

**What if the architecture changes:**

| Change | Effect on recovery |
|---|---|
| **Postgres-style MVCC** | No physical undo pass: an uncommitted transaction's row versions stay on the page but are invisible, because the commit log (`pg_xact`) says the xid never committed. VACUUM cleans them later |
| **InnoDB** | Redo from the redo log, then roll back uncommitted transactions using the undo logs (background) |
| **LSM tree** | Replay WAL into a fresh memtable. SSTables are immutable, so they never need repair — only the manifest says which ones are live |
| **In-memory DB** | Load last snapshot + replay command log; recovery time ∝ dataset size, so snapshot often |
| **Replica failover** | Don't recover — promote a replica that's already warm. Seconds instead of minutes, at the cost of losing unreplicated commits if replication was async |
| **Shared storage (Aurora)** | Storage nodes apply redo continuously; compute restart is near-instant because there is nothing to replay locally |

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
| **17** | Data lake & lakehouse: Parquet internals, open table formats (Iceberg, Delta Lake, Hudi), catalogs, medallion architecture | ACID transactions on object storage. The modern analytical data stack decouples compute from storage with open formats. | [22](./22-data-lake-lakehouse.md), also [08](./08-olap-databases.md) §11 | — |

**The sentence to remember.** *Phases 0–10 build a database. Phase 11 makes it fast. Phases 12–14 specialize it. Phases 15–16 scale it. Phase 17 opens it to the lake.* Most production complaints are mis-tuned phase 11 + 13. Most outages are phase 16.

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
├── Petabyte-scale analytics on cheap object storage (S3/GCS)
│   → Data lakehouse, open table formats                                ─── doc 22
│   Examples: Iceberg + Trino, Delta Lake + Spark, Hudi + Flink
│   Parquet files + metadata layer = ACID on object storage.
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
18. **21** — In-process OLAP. DuckDB and chDB bring analytical power without servers.
19. **22** — Data lake & lakehouse. Parquet internals, Iceberg, Delta Lake, Hudi — ACID on object storage.
20. **simpledb.py** — Read end-to-end last. By this point every layer should look familiar.

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

## 10. Layer by Layer: Why Exactly This Way

> **In plain words.** Each layer below gets the same six questions: what is it in one sentence,
> what everyday thing is it like, what problem forces it to exist, why it's built *this* way and
> not another, what it costs, and what happens if you swap it for the alternative.
>
> **How to use this section.** When a design choice in any doc feels arbitrary, find its layer
> here. The "why" is almost always one of three hardware facts: *RAM forgets on power loss*,
> *disk is ~1,000× slower than RAM*, *sequential I/O beats random I/O*. Plus one human fact:
> *many users touch the same data at once*.

### 10.1 Session / Wire Layer

- **Simple explanation.** The front door: it checks who you are, speaks the client's protocol,
  and gives each connection its own conversation state.
- **Analogy.** The reception desk that checks your badge and hands you a visitor number.
- **Problem it solves.** Authentication, message framing, and keeping per-connection state
  (current transaction, prepared statements, `SET` variables) apart.
- **Why exactly this way.** Parse and execute are separate protocol messages (prepared
  statements) so a query can be planned once and run many times, and so parameters travel
  *separately* from SQL text — which is what makes parameter binding immune to SQL injection.
- **Cost / trade-off.** Connections are expensive: Postgres forks one OS process per connection
  (several MB each, plus snapshot-computation cost that grows with connection count).
- **What if you change it.**

| Change | Result |
|---|---|
| Process-per-connection → thread-per-connection (MySQL) | Cheaper connections, but one bad thread can take down the whole server process |
| Add a pooler (PgBouncer, RDS Proxy) in *transaction* mode | 10,000 app connections share ~100 server connections; session state (session-level `SET`, advisory locks, `LISTEN`) no longer survives across transactions |
| HTTP/serverless drivers (Neon, PlanetScale, Data API) | Works from edge functions with no persistent sockets; each request pays connection/auth setup unless the provider pools |

### 10.2 Query Engine (parser → optimizer → executor) — doc 04, 15

- **Simple explanation.** Turns "what I want" (SQL) into "how to get it" (a plan of operators),
  picks the cheapest plan, then runs it.
- **Analogy.** A route planner: you give the destination, it picks roads using live traffic data
  (statistics).
- **Problem it solves.** Users should not have to know which indexes exist or which join order is
  fast — and the right answer *changes as the data grows*.
- **Why exactly this way.**
  - **Declarative SQL** separates *what* from *how*, so the DB can switch plans when a table goes
    from 1,000 to 100M rows without anyone changing application code.
  - **Cost-based optimizer** because join orders explode combinatorially (n! for n tables); only
    estimates from statistics can prune them.
  - **Volcano iterator (`next()`)** because any operator can plug into any other — a join does not
    care whether its input is a scan, a filter or another join.
- **Cost / trade-off.** Estimates are guesses: a 10× cardinality error on a join input can flip the
  plan from hash join to nested loop and turn 50 ms into 5 min. Volcano's one-virtual-call-per-row
  wastes CPU on big scans.
- **What if you change it.**

| Change | Result | Who |
|---|---|---|
| Tuple-at-a-time → **vectorized** (batches of ~1,000 values) | 10–100× faster analytical scans (CPU caches, SIMD); little gain for single-row OLTP | DuckDB, ClickHouse, Snowflake |
| Interpretation → **compiled** plans (codegen) | Removes interpretation overhead; compile time hurts short queries | HyPer/Umbra, Spark whole-stage codegen |
| Pull → **push-based** execution | Easier parallelism and pipeline scheduling | DuckDB, Umbra |
| Cost-based → **rule-based** / no optimizer | Predictable plans, but performance depends on how the query was written | early Oracle RBO, many NoSQL query layers |
| Plan per execution → **cached generic plan** | Saves planning time; can be terrible for skewed parameters (one plan for `country='US'` and `country='IS'`) | Postgres generic plans, SQL Server parameter sniffing |

### 10.3 Access Methods — doc 03

- **Simple explanation.** The adapter between "the executor wants the next row" and "the storage
  has pages and indexes".
- **Analogy.** The warehouse picker who knows whether to walk every aisle or use the shelf map.
- **Problem it solves.** Keeps the executor ignorant of how data is laid out, so new index types or
  storage formats can be added without rewriting the executor.
- **Why exactly this way.** There's no single best scan: a **sequential scan** reads every page but
  sequentially; an **index scan** reads few pages but randomly; a **bitmap scan** collects TIDs
  first, sorts them by page, then reads each page once — the middle ground. The crossover is
  roughly "an index wins when the query needs under a few percent of the table" (driven by
  `random_page_cost` / `seq_page_cost`).
- **Cost / trade-off.** Choosing wrong is the #1 slow-query cause: an index scan over 30% of a
  table does ~1 random read per row.
- **What if you change it.** Columnar engines replace "scan pages, emit rows" with "read column
  chunks, skip chunks whose min/max can't match". Table access methods as a plug-in API (Postgres
  `tableam`) are what let extensions like columnar storage or OrioleDB replace the heap.

### 10.4 Transactions + Concurrency Control — docs 05, 17, 18

- **Simple explanation.** Makes many users behave as if each were alone, and makes a group of
  changes happen completely or not at all.
- **Analogy.** Readers get the printed edition; editors work on drafts; two editors can't edit the
  same paragraph at the same time; a draft is published all at once.
- **Problem it solves.** Two facts: *users collide* (lost updates, reading half-done work) and
  *crashes happen mid-change* (half a bank transfer).
- **Why exactly this way.**
  - **MVCC for reads** because the most common conflict is "long report vs short updates". With
    versions, readers never wait for writers and writers never wait for readers.
  - **Locks for write–write conflicts** because two writers to the same row must be ordered —
    versions alone can't decide who wins.
  - **Latches ≠ locks.** Latches guard in-memory structures for microseconds and never deadlock (by
    ordering); locks guard logical rows for a whole transaction and can deadlock (so they need
    detection). Different lifetimes → different mechanisms.
- **Cost / trade-off.** Old versions pile up → VACUUM/purge is mandatory, and one long-open
  transaction blocks cleanup for everybody. Snapshot isolation still allows **write skew** (two
  doctors each check "someone else is on call", both go off call).
- **What if you change it.**

| Change | Result | Who |
|---|---|---|
| MVCC → **pure 2PL** | Serializable and simple; readers block writers, long reports stall OLTP | SQL Server default (non-RCSI) |
| → **OCC** (validate at commit) | No waiting at low contention; abort storms on hot rows | many in-memory DBs (Hekaton, Silo) |
| Snapshot → **SSI** | True serializability with modest overhead; some *false-positive* aborts, so apps must retry | Postgres `SERIALIZABLE`, CockroachDB |
| → **Deterministic** (Calvin) | Pre-ordered transactions, no concurrency aborts, easy replication; needs the read/write set up front | FaunaDB (Calvin-inspired) |
| Weaker isolation (Read Committed) | Fewer aborts, more anomalies the app must handle (lost updates without `SELECT … FOR UPDATE`) | Postgres/Oracle default |

### 10.5 Indexes — doc 06, 11, 13

- **Simple explanation.** Extra structures that answer "where are the rows with key X?" without
  reading the whole table.
- **Analogy.** The library catalogue, sorted so you can find one card — or a range of cards — fast.
- **Problem it solves.** Finding 1 row in 10M without reading 125,000 pages.
- **Why exactly this way (B+Tree).**
  - **High fanout** (~400 keys per 8 KB page) → only 3–4 levels for billions of rows, and the top
    levels stay in RAM. So a lookup costs ~1 disk read.
  - **Sorted** → the same structure serves `=`, ranges, `ORDER BY` and `MIN/MAX`.
  - **Node = page** → the index uses the same buffer pool, WAL and latches as everything else.
  - **Values only in leaves, leaves linked** → range scans walk sideways without going back up.
- **Cost / trade-off.** Every index is paid on every write (an insert into a table with 6 indexes
  = 7 page modifications). Random inserts (UUIDv4 keys) split pages all over the tree and wreck
  cache locality — which is why time-ordered keys (UUIDv7, sequences) insert faster.
- **What if you change it.**

| Change | Wins | Loses |
|---|---|---|
| **Hash index** | O(1) equality | No ranges, no ordering |
| **LSM tree** (doc 13) | Sequential writes, high ingest | Read + space amplification, compaction stalls |
| **BRIN** | Tiny (KBs for GBs) on naturally ordered data (timestamps) | Useless when the column is random |
| **GIN / inverted** | Full-text, JSONB, arrays | Slow updates (pending list) |
| **HNSW / IVF** (doc 11) | Approximate nearest neighbor for embeddings | Approximate answers, high RAM, recall/latency knobs |
| **No index** | Zero write cost | Every lookup is a full scan |

### 10.6 Storage: Pages, Slotted Layout, Heap — docs 01, 02

- **Simple explanation.** Data is stored in fixed-size boxes (pages); inside each box a small table
  of contents (slot directory) says where each row starts.
- **Analogy.** Identical shipping boxes with a packing list taped inside the lid.
- **Problem it solves.** Rows have different lengths; disks and the OS move data in fixed blocks.
- **Why exactly this way.**
  - **Fixed-size pages** → `offset = page_id × page_size`, interchangeable buffer frames, alignment
    with OS/SSD blocks, simple free-space tracking.
  - **Slotted pages** → rows can move or be compacted *within* the page while their TID (page,
    slot) stays stable, so indexes don't have to change.
  - **Heap (unordered)** → inserts go anywhere with free space, which is the cheapest possible insert.
- **Cost / trade-off.** Page size: small (4 KB) = less wasted I/O for point reads; large (16 KB
  InnoDB, MBs in columnar) = fewer tree levels and better scans. Heap = no physical order, so range
  scans on a non-clustered key are random reads.
- **What if you change it.** Heap → **clustered** (InnoDB): rows ordered by PK, fast PK ranges,
  slower secondary lookups. Row pages → **columnar** blocks (Parquet row groups ~128 MB, column
  chunks with compression): scans read 5–10× fewer bytes, single-row updates become rewrites.
  In-place pages → **append-only** files (LSM, lakehouse): no read-modify-write, immutable files,
  cleanup by compaction.

### 10.7 Buffer Pool — doc 01

- **Simple explanation.** The DB's own RAM cache of pages; every layer reads pages only through it.
- **Analogy.** The reading desk by the door: popular books stay on it, the least-used goes back to
  the basement when space runs out.
- **Problem it solves.** Disk latency (~80 µs NVMe, ~1 ms cloud disk) vs RAM (~100 ns).
- **Why exactly this way (and not "just let the OS cache it").**
  - **Eviction the DB controls** → one big sequential scan must not flush the whole hot set (scan
    resistance: ring buffers, LRU-K, 2Q).
  - **Write ordering the DB controls** → a dirty page may only go to disk after its WAL record
    (§2.2 rule 1). The OS page cache writes back whenever it likes.
  - **Pin counts** → a page in use by an operator can't be evicted mid-read.
  - **Clock instead of true LRU** → LRU must move a list node (under a lock) on *every* hit; clock
    only sets a bit.
- **Cost / trade-off.** Postgres still goes through the OS page cache (double buffering), which is
  why `shared_buffers` is typically ~25% of RAM, not 80%. Engines using `O_DIRECT` (InnoDB) size
  their pool to ~70–80% of RAM.
- **What if you change it.**

| Change | Result |
|---|---|
| Buffer pool → **mmap** | Less code, but no control of eviction or write-back order, I/O stalls show up as page faults, error handling via signals. MongoDB replaced MMAPv1 with WiredTiger; see "Are You Sure You Want to Use MMAP in Your DBMS?" (CIDR 2022). LMDB makes it work by being copy-on-write and read-mostly |
| Remove it (**in-memory**) | No page indirection; 10×+ faster, but dataset must fit in RAM and durability relies on log + snapshots |
| **Pointer swizzling** (LeanStore, Umbra) | In-memory speed for hot data, disk capacity for cold |
| Undersize it | Hit ratio falls from 99% to 90% → 10× more disk reads → latency cliff, not a slope |

### 10.8 Write-Ahead Log — doc 14

- **Simple explanation.** An append-only journal of every change, forced to disk at commit, from
  which the database can rebuild itself.
- **Analogy.** The librarian's logbook in the fire safe.
- **Problem it solves.** Durable commits must be fast, and a crash can leave pages half-written.
- **Why exactly this way.**
  - **Append-only** → sequential I/O, the fastest thing a disk does.
  - **One file to fsync** → the durability cost is one fsync per group of commits, not one per page.
  - **Single source of truth** → the same stream feeds crash recovery, replicas (physical
    replication), point-in-time recovery (archived WAL) and change-data-capture (logical decoding,
    Debezium). One mechanism, four features.
  - **Physiological records** ("on page 88, insert this tuple at slot 5") → small like logical
    logging, but replayable page-by-page like physical logging.
- **Cost / trade-off.** Every byte is written twice (log + page), and the log disk's fsync latency
  becomes your commit latency floor.
- **What if you change it.** WAL on the same slow disk as data → put it on its own low-latency
  device. `fsync = off` → fast until the first power cut, then corruption. Shadow paging instead of
  WAL (LMDB) → no log, but copy-on-write of every path to the root and one writer at a time. "The
  log *is* the database" (Aurora, Neon, Kafka-style designs) → compute ships only log records;
  storage materializes pages on demand.

### 10.9 OS + Hardware — doc 00

- **Simple explanation.** The physical rules every layer above is shaped around.
- **Analogy.** The building's floor plan: where the library can put the stacks and how far the
  basement is.
- **Problem it solves.** Nothing — it *creates* the problems. Knowing it tells you *why* the layers
  look the way they do.
- **Why databases work this way because of it.** Flash erases in blocks → pages. The page cache
  lies about durability → fsync. Random I/O is slower than sequential → WAL, LSM, columnar.
  CPU cache misses cost ~100 cycles → vectorized execution and cache-friendly node layouts.
- **What if you change it.**

| Hardware | What changes above it |
|---|---|
| **HDD** (~10 ms seek) | Sequential is everything; big pages; B+Trees kept shallow; one I/O queue |
| **NVMe** (~80 µs, deep queues) | Random reads are affordable; the bottleneck moves to CPU, latches and syscalls → io_uring, async I/O, lock-free buffer pools |
| **Cloud network disk** (EBS, PD) | ~0.5–2 ms fsync, IOPS and throughput *quotas* → group commit and larger I/Os matter more than on local NVMe |
| **Object storage** (S3) | ~10–100 ms per request, huge throughput, immutable objects, no in-place update → immutable files + metadata log (LSM, Iceberg/Delta, doc 22) |
| **Lots of RAM** | Whole working set fits → in-memory engines (doc 10), buffer pool becomes a formality |

### 10.10 Distribution — docs 12, 16, 19

- **Simple explanation.** Copy the data to several machines (replication) and/or split it across
  them (sharding), then agree on what happened when machines and networks fail.
- **Analogy.** Branch libraries: copies of the logbook are mailed to each branch; for big
  decisions, a majority of branches must agree.
- **Problem it solves.** One machine has a ceiling on capacity, throughput and availability.
- **Why exactly this way.**
  - **Replicate the WAL**, not SQL statements → the log already exists, is deterministic, and
    replays exactly (statements like `NOW()` or `RANDOM()` don't).
  - **Majority quorums** (Raft/Paxos) → any two majorities overlap, so two leaders can never both
    commit conflicting entries.
  - **Suspicion-based failure detection** (phi-accrual, SWIM) → on a real network you can't tell
    "slow" from "dead"; binary heartbeats flap.
- **Cost / trade-off.** Every guarantee costs a network round trip, and the speed of light sets the
  floor (~1 ms RTT per ~100 km of fiber). CAP/PACELC: under a partition choose consistency or
  availability; otherwise choose latency or consistency.
- **What if you change it.**

| Choice | You get | You pay |
|---|---|---|
| **Async replicas** | Read scaling, cheap HA | Replication lag, stale reads, data loss (RPO > 0) on failover |
| **Sync / quorum replication** | RPO = 0 | +1 RTT on every commit; availability depends on replicas being up |
| **Sharding** | Write scaling and capacity | Cross-shard transactions (2PC), cross-shard joins, re-sharding, hot keys |
| **Leaderless** (Dynamo-style) | Writes accepted anywhere, high availability | Conflicts → last-writer-wins (silent loss) or CRDTs; read repair; eventual consistency |
| **Shared storage** (Aurora, Neon, Socrates) | Scale compute separately, fast failover, no data copy for replicas | Single-writer, vendor-specific storage layer |

---

## 11. What If the Architecture Changes? — the change matrix

> **In plain words.** Every "new" database is the same stack with one or two layers swapped. This
> table shows, for the most common swaps, which of the four pipelines (§2) change and what you win
> and lose.

| Architecture change | Read path | Write path | Commit path | Recovery path | You win | You lose | Example |
|---|---|---|---|---|---|---|---|
| B+Tree → **LSM** | Check memtable + several levels, bloom filters | Append WAL + memtable, no page RMW | Same (WAL fsync) | Replay WAL into memtable | 5–10× write throughput, better compression | Read amp, compaction stalls, space amp | RocksDB, Cassandra |
| Row → **columnar** | Read only needed columns, skip blocks | Batched via delta store | Same or batch-level | Rebuild delta from log | 10–100× faster scans/aggregates | Slow point reads and single-row updates | ClickHouse, DuckDB |
| Heap → **clustered** index | PK lookup = 1 tree walk; secondary = 2 | Insert into PK order; page splits | Same | Same | Fast PK ranges, no heap hop | Slower secondary lookups, random-PK splits | InnoDB |
| Disk → **in-memory** | Pointer chase, no buffer pool | Memory write + log | Log fsync (or async/replica-based) | Snapshot + log replay | µs latency | Dataset ≤ RAM, slower restarts | Redis, VoltDB |
| Buffer pool → **mmap** | Page faults instead of pool lookups | OS decides write-back | Must still fsync correctly | Hard: no control over what reached disk | Less code | Control, predictability, error handling | LMDB (works because CoW) |
| MVCC → **2PL only** | Readers take shared locks | Same | Release locks at commit | Same | Simpler, serializable | Readers block writers | SQL Server default |
| Single node → **async replica** | Optional stale reads on replica | Same | Unchanged latency | Failover = promote replica | Read scale, HA | Lag, possible data loss on failover | Postgres streaming |
| → **Raft / sync quorum** | Leader reads (or lease reads) | Same, via leader | +1 quorum RTT | Leader election, no manual failover | RPO = 0, automatic failover | Write latency, needs 3+ nodes | etcd, CockroachDB |
| → **Sharded** | Route to shard; scatter-gather for non-key queries | Route to shard | 2PC for multi-shard txns | Per shard | Write scale, capacity | Cross-shard txns/joins, rebalancing | Vitess, Citus, Spanner |
| → **Shared storage** | Pages fetched from storage tier | Compute ships only log records | Storage write quorum | Storage applies redo continuously | Fast failover, independent scaling | Custom storage, single writer | Aurora, Neon |
| Local disk → **object storage** (lakehouse) | Read Parquet + metadata; heavy caching | Write new immutable files + commit metadata | Atomic metadata swap (catalog CAS) | Nothing to replay; old snapshots remain | Cheap PB storage, open formats, compute separation | Latency, small-file problem, no row-level OLTP | Iceberg, Delta Lake |

**A four-question method for any change you haven't seen before:**

1. **Which primitive moves?** Every change shifts work between *read a page*, *write a page* and
   *log an intention*. LSM moves "write a page" to background compaction; columnar moves "read a
   row" to "read a column chunk".
2. **Which amplification gets worse?** The **RUM conjecture**: you can optimize at most two of
   **R**ead cost, **U**pdate cost and **M**emory/space cost. If a design claims to improve all
   three, look for the hidden cost.
3. **Where is the durability point now?** Local fsync? Replica ack? Quorum? Metadata commit on S3?
   That decides both commit latency and what's lost on failure.
4. **What new failure mode appears?** Stale reads (replicas), blocked 2PC (sharding), compaction
   stalls (LSM), split brain (bad failure detection), lost writes (last-writer-wins).

---

## 12. Interview Questions and System Design Prompts

> **In plain words.** In an interview, answer in this order: one plain sentence, one number, one
> trade-off. The strongest signal for this chapter is that you reason *through the layers* —
> "this is slow because the buffer pool misses, because the index is on a random UUID" — instead
> of naming products.
>
> **Real-world example.** "Why doesn't a database write the data file on every commit?" → "Because
> a commit touches pages all over the disk. Instead it appends ~100 bytes to a log and fsyncs just
> that — one sequential write, ~20 µs on NVMe. The pages are written later in the background and,
> after a crash, rebuilt from the log. The trade-off: every change is written twice, and recovery
> time depends on how often we checkpoint."

Each question names the sections it draws from and gives the answer structure an interviewer is
listening for.

### 12.1 Conceptual questions — "explain X"

**Q: Walk me through what happens when I run `SELECT … WHERE id = 42`.**
*Sections: Start here, §2.1, §7*
Parse → analyze (catalog) → optimize (index vs seq scan from statistics) → executor → index walk
(3–4 levels, top levels cached) → TID → buffer pool (hit ~1 µs / miss ~80 µs NVMe) → slotted page →
MVCC visibility check → project → return. Strong half: say *where time goes* (buffer misses
dominate cold queries) and that no lock and no WAL write happen on this path.

**Q: Why write-ahead logging? Why not just write the page at commit?**
*Sections: §2.2, §2.3, §10.8*
A commit touches several random pages; the WAL turns that into one sequential append plus one
fsync, and group commit shares that fsync across many transactions. Pages are flushed lazily and
absorb repeated updates. Crash safety comes from replaying the log. Mention the rule: the log
record must be durable before its page may be written, and before "COMMIT OK" is sent.

**Q: What are steal and force, and why does everyone pick steal + no-force?**
*Sections: §2.4*
Steal = uncommitted pages may be flushed (needs undo). No-force = committed pages needn't be
flushed at commit (needs redo). Together: fastest commits and a buffer pool that never has to hold
a whole transaction. ARIES (analysis, redo, undo, CLRs) is what makes it safe.

**Q: Why does a database have its own buffer pool instead of using the OS page cache / mmap?**
*Sections: §10.7, §9 pitfall 2*
The DB must control eviction (scan resistance), write ordering (WAL-before-page), and I/O errors;
the OS controls none of these for you. mmap hides I/O as page faults and gives you no say in
write-back. Note the nuance: Postgres still double-buffers through the OS, InnoDB uses O_DIRECT.

**Q: Latch vs lock?**
*Sections: §10.4, doc 17*
Latch: protects an in-memory structure (a page, a hash bucket) for microseconds, no deadlock
detection (avoided by ordering). Lock: protects a logical row/table for a transaction's lifetime,
has modes and deadlock detection. Confusing them causes either corruption or stalls.

**Q: MVCC vs 2PL — which and why?**
*Sections: §10.4, §5.2*
MVCC keeps versions so readers never block writers — the right default when long reads coexist
with short writes. Writers still lock each other. Costs: version garbage (VACUUM), and snapshot
isolation allows write skew → use SSI or explicit locks where invariants span rows.

**Q: B+Tree vs LSM tree?**
*Sections: §10.5, §11, doc 13*
B+Tree: update in place, ~1 random I/O per read, write amplification from page rewrites; best for
read-heavy OLTP. LSM: buffered sequential writes, great ingest and compression, pays with read
amplification (mitigated by bloom filters) and compaction. Frame it with RUM: pick two.

**Q: Why is a row store bad for analytics and a column store bad for OLTP?**
*Sections: §10.6, §11, docs 07, 08*
Analytics read few columns of many rows → a row store reads every column of every row. OLTP reads
and writes all columns of one row → a column store touches one block per column and can't update
compressed blocks in place. HTAP (doc 09) keeps both and syncs them.

**Q: How does replication relate to the WAL?**
*Sections: §2.3, §10.10*
Physical replication *is* shipping the WAL over the network and replaying it on the replica — the
same code as crash recovery, running continuously. Async: reply before the replica has it (lag,
RPO > 0). Sync/quorum: reply after (RPO = 0, +1 RTT per commit).

**Q: What does "committed" actually mean in Postgres, in Aurora, and in CockroachDB?**
*Sections: §2.3*
Postgres: commit record fsynced to local WAL (plus sync standbys if configured). Aurora: log
records acknowledged by 4 of 6 storage nodes across 3 AZs. CockroachDB: Raft log entry replicated
to a majority of the range's replicas. Same idea — the *durability point* — at different places.

### 12.2 System design round

**Q: Design the storage layer for an e-commerce order system: 50M orders/year, peak 2,000
writes/s and 20,000 reads/s, a finance team running dashboards, RPO = 0 and RTO < 1 minute.**

```
1. CLARIFY
   - Access pattern: point reads by order_id / user_id (OLTP) + aggregations (analytics).
   - Consistency: orders + payments + stock must be transactional → ACID relational store.

2. SIZE IT (Start here, §3)
   - 50M orders × ~1 KB (+ line items) ≈ 50 GB/year of data; with indexes ≈ 100–150 GB/year.
   - 2,000 writes/s × ~1 KB WAL ≈ 2 MB/s WAL — trivial bandwidth.
   - Commit latency: 1 ms cloud-disk fsync; group commit keeps 2,000 commits/s far below limits.
   → One well-sized primary handles this for years. Do NOT shard on day one.

3. STORAGE ENGINE CHOICES (§10.5, §10.6)
   - Postgres/MySQL, B+Tree indexes on (order_id), (user_id, created_at).
   - Time-ordered keys (bigint sequence or UUIDv7), not UUIDv4 → no random page splits.
   - Partition orders by month → old partitions drop/archive cheaply, VACUUM stays local.
   - Buffer pool sized for the hot set (last ~3 months of orders + indexes).

4. DURABILITY + HA (§2.3, §10.10)
   - RPO = 0 → synchronous replica in another AZ (+~1 ms per commit), or Aurora-style quorum.
   - RTO < 1 min → automated failover (Patroni / managed service), not WAL replay on a new box.
   - PITR: archived WAL + daily base backup, restore tested monthly.

5. READ SCALING (§2.1)
   - 20,000 reads/s point lookups mostly hit the buffer pool → primary + 2 async read replicas.
   - Read-your-writes paths (order confirmation page) go to the primary.

6. ANALYTICS (§6, §11)
   - Don't run dashboards on the primary: they evict the OLTP hot set and hold snapshots open
     (blocking VACUUM). Stream WAL via CDC (logical decoding → Debezium) into a columnar store
     (ClickHouse / warehouse / lakehouse).

7. OBSERVABILITY + SECURITY
   - Metrics: buffer hit ratio, replication lag, WAL fsync latency, dead tuples, lock waits, p99.
   - TLS in transit, encryption at rest, least-privilege roles per service, PII columns
     identified for GDPR deletion (and remember deletes must reach replicas, backups' retention
     window and the analytics copy).

8. GROWTH PLAN
   - Phase 1: single primary + sync standby.  Phase 2: read replicas + CDC to analytics.
   - Phase 3 (only if writes outgrow one node): shard by user_id (Citus/Vitess) or move to a
     distributed SQL DB — accept 2PC for cross-shard transactions.
```

*What interviewers listen for:* numbers before architecture; "one node is enough" when it is;
the durability point stated explicitly (sync replica = RPO 0); analytics separated from OLTP with
the reason (buffer pool + VACUUM), not just "use a warehouse"; a growth plan instead of day-one
sharding.

**Q: Design a key-value store that ingests 500,000 writes/s of IoT readings (≈200 bytes each) and
serves "last 24 h for device X".**
*Sections: §6, §11, docs 13, 20*
≈100 MB/s raw ingest → B+Tree random writes won't keep up; choose an **LSM** (or a time-series
engine). Key = `(device_id, timestamp)` so one device's readings are contiguous in sorted runs →
range scan per device. WAL with group commit; memtable flush to SSTables; time-window compaction so
whole old files expire by TTL instead of being compacted. Bloom filters for point lookups. Scale
out by sharding on `device_id` (hash) with replication factor 3. *Listen for:* write amplification
and compaction strategy chosen for the TTL pattern, key design that turns the query into a range
scan, and an honest note on read amplification.

**Q: Your team wants to move from a single Postgres to a distributed SQL database. How do you
decide?**
*Sections: §10.10, §11*
First prove the single node is the bottleneck (writes, storage, or regional latency — reads are
solved by replicas). Then list what you pay: every commit gains a quorum round trip, cross-range
transactions become 2PC-like, some Postgres features/extensions disappear, and hot keys still
serialize on one range leader. Pilot with production-shaped traffic and compare p99, not averages.
*Listen for:* "distributed" chosen for a measured reason, not as a default.

### 12.3 Rapid-fire questions

| Question | Strong answer | Section |
|---|---|---|
| What is the durability point of a commit? | The fsync of the WAL up to the commit record (or the replica/quorum ack if configured). | §2.3 |
| Why does a B+Tree over 10M rows need only 3 levels? | ~400 keys per 8 KB page; 400³ = 64M. | Start here, §10.5 |
| Why are the top B+Tree levels basically free? | Every lookup touches them, so they are always in the buffer pool. | §10.5 |
| Buffer hit ratio drops from 99% to 90% — how much more disk I/O? | 10× (misses go from 1% to 10%). | §10.7 |
| Why clock instead of LRU? | LRU updates a shared list on every hit (contention); clock sets a bit. | §10.7 |
| What does group commit buy? | One fsync for many transactions → commits/s no longer capped by fsync latency. | §2.3 |
| Async commit — can it corrupt data? | No. It can lose the last ~fraction of a second of commits. | §2.3 |
| Why does ARIES redo uncommitted changes? | Repeating history is simple; undo then removes losers. `page_lsn` makes it idempotent. | §2.4 |
| Why doesn't Postgres need an undo pass? | Uncommitted versions stay but are invisible (xid not committed in `pg_xact`); VACUUM removes them. | §2.4 |
| What stops VACUUM from cleaning up? | A long-running (or idle-in-transaction) transaction holding an old snapshot. | §10.4, §9 |
| UUIDv4 primary keys — what's the cost? | Random inserts → page splits everywhere, poor cache locality, bigger indexes. | §10.5 |
| Index or seq scan for a query returning 40% of rows? | Seq scan: an index would do ~1 random read per row. | §10.3 |
| LSM's main costs? | Read amplification, space amplification, compaction I/O and stalls. | §11 |
| Why do lakehouse formats need a metadata log? | Object storage has no atomic multi-file rename; commit = atomic swap of a metadata pointer. | §11, doc 22 |
| Sync replica in another region — cost per commit? | One cross-region RTT, ~30–100 ms. | §2.3, §10.10 |
| Why 3 (or 5) nodes for Raft, not 2 (or 4)? | Majority of 3 tolerates 1 failure, of 5 tolerates 2; an even count adds cost without extra tolerance. | §10.10 |

### 12.4 Debugging prompts — "here are the symptoms, diagnose"

**"Commit latency jumped from 1 ms to 20 ms after we moved to a new cloud disk type."**
The commit path's floor is WAL fsync latency (§2.3). Measure fsync latency on the new volume
(`pg_test_fsync`, fio with `--fsync=1`); check IOPS/throughput quota exhaustion. Fixes: WAL on a
low-latency volume, verify group commit is effective, or async commit for non-critical writes.

**"A query that took 5 ms now takes 30 s. Nothing was deployed."**
Plan flip (§10.2). `EXPLAIN (ANALYZE, BUFFERS)`: compare estimated vs actual rows. A 100× estimate
error usually means stale statistics after a bulk load or a skewed value. Run `ANALYZE`, consider
extended statistics; then check whether the new plan is a seq scan or nested loop over a large input.

**"The table is 10 GB of live data but 80 GB on disk, and it keeps growing."**
MVCC garbage (§10.4). Look for a long-running or idle-in-transaction session, an abandoned
replication slot, or a stale prepared transaction pinning the oldest xmin. Fix the holder, then
VACUUM (or `pg_repack` to reclaim space online).

**"p99 read latency spikes every few minutes on a RocksDB-backed service."**
Compaction or memtable-flush stalls (§11, doc 13). Check L0 file count vs slowdown/stop triggers,
pending compaction bytes, and write stalls in the LOG. Fixes: more compaction threads, rate
limiter, a compaction style that fits the workload, or a bigger memtable budget.

**"After failover, customers see orders they placed 2 seconds ago disappear."**
Async replication (§2.3, §10.10): the promoted replica was behind; commits acknowledged by the old
primary never reached it. Fix: synchronous (or quorum) replication for that data, or accept the
RPO explicitly and reconcile from the old primary's WAL if it's recoverable.

**"The dashboard query made checkout slow."**
Shared buffer pool (§10.7): a big scan evicts the OLTP hot set and competes for I/O, and its long
snapshot holds back VACUUM. Move analytics to a replica or a columnar copy via CDC (§12.2).

### 12.5 Common interview mistakes

1. **Naming products instead of mechanisms.** "Use Cassandra" is not an answer; "write-heavy,
   append-mostly → LSM, because sequential writes" is.
2. **Saying `write()` means durable.** It only reaches the OS page cache; durability is `fsync`
   (§2.2, §9 pitfall 3).
3. **Sharding on day one.** Size the data first; most OLTP systems fit on one node plus replicas
   for years (§12.2).
4. **Confusing replication consistency with isolation.** Async replication ≠ read uncommitted —
   different axes (§9 pitfall 11).
5. **Forgetting the cost side of every index and every guarantee.** Each index is paid on every
   write; each sync replica on every commit (§10.5, §2.3).
6. **Treating MVCC as free.** No mention of VACUUM/purge or long-transaction risk (§10.4).
7. **"Distributed" without a durability point.** Always say what a commit waits for: local fsync,
   replica ack, or quorum (§2.3).

---

**TL;DR pipeline.** *SQL → parser → optimizer → executor → access method → index → buffer pool → page → disk*, with **WAL** crosscutting the write side, **MVCC + locks** crosscutting concurrency, and **replication + consensus** wrapping the whole thing for distribution. Build it in that order. Every other doc in this folder is one of those boxes seen up close.
