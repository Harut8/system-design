# Storage Engine Fundamentals: A Deep Dive

How databases physically store, retrieve, and manage data on disk and in memory. This document covers the core primitives that every storage engine is built on: disk pages, the buffer pool, and the I/O subsystem. Understanding these fundamentals is essential before studying indexes, transactions, or query execution, because every higher-level feature ultimately reduces to reading and writing pages.

---

## Table of Contents

**Start here (beginner path):** [The storage engine in plain words](#start-here-the-storage-engine-in-plain-words)
→ [One row's journey](#one-rows-journey-read-update-crash)
→ [Where the numbers come from](#where-the-numbers-come-from)
→ [Key terms at a glance](#key-terms-at-a-glance)
→ [Key terms explained](#key-terms-explained-definition-real-world-example-why-it-exists)

**Main chapters:**

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

**Review and interview prep:**

12. [Common Pitfalls and Misconceptions](#12-common-pitfalls-and-misconceptions)
13. [Interview Questions and System Design Prompts](#13-interview-questions-and-system-design-prompts)
14. [One-Page Cheat Sheet](#14-one-page-cheat-sheet)

---

## Start here: the storage engine in plain words

**The problem.** A database promises two things at once: *(1) once it says "saved", your data
survives a crash or power cut*, and *(2) it answers fast, even under thousands of requests per
second*. The storage engine is the part that keeps both promises, and it has to do so despite
three physical facts:

| Fact | Number | Consequence |
|---|---|---|
| RAM is fast but **forgets everything** on power loss | ~100 ns per access | Good place to *work*, terrible place to *keep* data |
| Disk (SSD/HDD) **remembers** but is slow | NVMe ~80–100 µs, HDD ~5–10 ms per random read | Good place to *keep* data, terrible place to *work* |
| Disks move data in **blocks**, never single bytes | 4 KB (SSD flash page / OS block) | Reading 100 bytes costs the same as reading 4–8 KB |

RAM is roughly **1,000× faster** than an SSD and **100,000× faster** than a spinning disk. Every
idea in this chapter (pages, buffer pool, WAL, checkpoints) is a trick to get RAM-like speed
with disk-like safety.

**The everyday analogy: an office with a basement archive.**

| Office | Storage engine piece | What it does | Section |
|---|---|---|---|
| The basement archive of identical **boxes** | Data files made of fixed-size **pages** | Cheap, huge, permanent, but a trip downstairs is slow | §2 |
| Inside each box: an **index card on the lid** listing where each document sits | **Slotted page** (header + line pointers + rows) | Find a document inside the box without reading all of it | §3 |
| "Box 2,104, document 3" | **TID** (page number, slot number) | The exact address of one row | §3 |
| Boxes stacked wherever there is room | **Heap file** | Rows are not kept in any order | §4 |
| A sheet by the door saying which boxes still have room | **Free space map** | Find space for a new row without opening every box | §4 |
| The **desk** upstairs holding the boxes you are working on | **Buffer pool** | Keep popular pages in RAM so you skip the basement trip | §5 |
| Deciding which box goes back downstairs when the desk is full | **Page replacement policy** (LRU, clock) | Keep hot pages, evict cold ones | §6 |
| A trip to the basement: one long walk vs fetching boxes from random aisles | **Sequential vs random I/O** | Reading in order is far cheaper than jumping around | §7 |
| A **receipt book**: before changing any document, write one line saying what you are changing | **WAL (write-ahead log)** | After a fire, replay the receipts to rebuild every agreed change | §9 |
| Pressing the receipt into the fireproof safe before saying "done" | **fsync** at commit | "Saved" means the receipt is physically on disk | §8, §9 |
| End-of-day: carry all edited boxes back downstairs, note "all receipts before #5,000 are filed" | **Checkpoint** | Bounds how many receipts you must replay after a fire | §8 |
| A seal on every box that shows if someone tampered with it | **Checksum** | Detect silent corruption from disks, RAM or firmware | §10 |

Keep this picture in your head. Every technical detail below maps onto one row of this table.

### One row's journey: read, update, crash

**The setup** (used throughout the chapter). An online shop running PostgreSQL. Table `users` has
**10 million rows**, about **100 bytes** of data each. Pages are **8 KB**. There is a B+Tree index on
`users.id`. The buffer pool (`shared_buffers`) is **1 GB** = **131,072 page frames**. The disk is an
NVMe SSD (~**100 µs** per random 8 KB read). All numbers are derived in
[Where the numbers come from](#where-the-numbers-come-from).

**Read: a customer logs in.** `SELECT name FROM users WHERE id = 42`

1. The query engine (doc 04) decides to use the index and asks the storage engine for pages,
   not rows. *A storage engine never reads "a row"; it reads the page that contains it.*
2. The index (doc 06) is 3 levels deep. The root and middle pages are touched by every query, so
   they are always on the desk (in the buffer pool). The leaf page says: **row 42 lives at
   TID (2104, 3)**, i.e. page 2,104, slot 3.
3. The buffer pool looks up page 2,104 in its hash table (§5).
   - **Hit** (page already in RAM): ~1 µs. Done.
   - **Miss**: pick a victim frame with the clock algorithm (§6). If the victim is *dirty*,
     it must be written first. Then `pread()` 8 KB at file offset 2,104 × 8,192 = 17,235,968.
     ~100 µs.
4. Inside the page, slot 3 of the line-pointer array says "row starts at byte 7,720, length
   128" (§3). The tuple header says which transaction created it, so MVCC (doc 05) can decide
   whether you may see it.
5. Return `'Ana'`. Total: ~5 µs if every page was hot, ~100–200 µs with one or two misses.

**Write: the customer changes their name.** `UPDATE users SET name = 'Ana B.' WHERE id = 42`

6. Bring page 2,104 into the buffer pool (as above) and take a short **latch** on it (§5).
7. **Log first.** Append a ~150-byte WAL record "txn 311: on page 2,104 write a new version of
   row 42" to the WAL buffer in RAM. It gets a number, its **LSN**, say 9,001 (§9).
8. Change the page in RAM, write `page_lsn = 9,001` into the page header, mark the frame
   **dirty**. The 8 KB page is **not** written to disk now.
9. **COMMIT = one `fsync` of the WAL.** The WAL (sequential, append-only) is forced to disk:
   ~20–50 µs on a datacenter NVMe, ~0.5–2 ms on a cloud network disk. Only now does the client
   hear "COMMIT OK". With **group commit**, 50 transactions committing at once share one fsync.
10. **Later**, the background writer or checkpointer writes the dirty page to its place in the
    data file. If 30 more updates hit page 2,104 first, all 30 ride on that one 8 KB write.

**Crash: the power goes out one second after COMMIT OK.**

11. RAM is gone, so the dirty page 2,104 in the buffer pool is gone. The data file still holds
    the **old** name.
12. On restart, recovery (§9, doc 14) finds the last **checkpoint** and replays the WAL from there.
    For each record it compares LSNs: record 9,001 > page 2,104's on-disk `page_lsn` → apply it.
    The rename is back. Records for transactions that never committed are ignored or undone.
13. *Why it works:* the WAL record reached disk **before** "OK" was sent. The data page never
    needed to.

```
 READ                                   WRITE                                  CRASH
 index → TID (2104,3)                   WAL record (~150 B, LSN 9001)          RAM lost
   → buffer pool lookup                   → change page in RAM, mark dirty     → find last checkpoint
       hit  ~1 µs                         → COMMIT: fsync WAL (20 µs – 2 ms)   → replay WAL records
       miss ~100 µs (+ evict)             → "OK" to client                       with LSN > page_lsn
   → slot 3 → tuple → MVCC check          → page written to disk LATER         → same state as before
```

**The single sentence to remember:** *reads are made fast by the buffer pool; writes are made
safe by the WAL; checkpoints keep the WAL short.*

### Where the numbers come from

Every number above, derived, so you can redo them for your own system. Units: 1 KB = 1,024 B.

**A. Rows per page and table size**

```
page                 = 8,192 B
page header          =    24 B
usable               = 8,192 − 24 = 8,168 B

one row on disk      = 23 B tuple header → padded to 24 B (8-byte alignment)
                     + 100 B user data
                     = 124 B → padded to 128 B
                     + 4 B line pointer (slot) in the page's slot array
                     = 132 B per row

rows per page        = 8,168 / 132 ≈ 61
pages for the table  = 10,000,000 / 61 ≈ 164,000 pages
table size on disk   = 164,000 × 8 KB ≈ 1.25 GB   (vs 1.0 GB of "raw" data: 25% overhead)
```

Lesson: per-row overhead matters. With 40-byte rows the 28 bytes of header + slot would be
~70% overhead (§3).

**B. Buffer pool frames**

```
1 GB / 8 KB = 1,073,741,824 / 8,192 = 131,072 frames
```

The whole 1.25 GB table does not fit, but the *hot* part (recent users, the index's upper levels)
easily does.

**C. Index depth**

```
index entry   ≈ 8 B key + 6 B TID + ~6 B overhead ≈ 20 B
entries/page  ≈ 8,168 / 20 ≈ 400   (the "fan-out")
levels needed : 400¹ = 400, 400² = 160,000, 400³ = 64,000,000 ≥ 10,000,000  →  3 levels
```

**D. What a hit ratio is worth**

```
average page access = hit_ratio × 1 µs + (1 − hit_ratio) × 100 µs

hit ratio 99%   → 0.99 × 1 + 0.01 × 100 = 1.99 µs
hit ratio 90%   → 0.90 × 1 + 0.10 × 100 = 10.9 µs   (5.5× slower, 10× more disk reads)
hit ratio 0%    → 100 µs                            (50× slower than 99%)
```

A drop from 99% to 90% *sounds* small; it is a 10× increase in disk reads.

**E. Why the WAL makes commits cheap**

```
Commit WITHOUT a WAL (write every changed page on commit):
  1 row + 2 index pages = 3 random 8 KB writes + fsync each  ≈ 3 × ~100 µs (NVMe) … 3 × ~8 ms (HDD)

Commit WITH a WAL:
  ~150 B appended to one sequential file + 1 fsync            ≈ 20–50 µs (NVMe), 0.5–2 ms (cloud disk)
  group commit: 50 concurrent commits share that one fsync
```

**F. Sequential vs random**

```
HDD random 8 KB read  ≈ 8 ms seek+rotate  → ~125 reads/s ≈ 1 MB/s
HDD sequential        ≈ 150–200 MB/s                    → ~150× faster
NVMe random 8 KB      ≈ 100 µs at queue depth 1         → ~10,000 reads/s ≈ 80 MB/s per thread
NVMe sequential       ≈ 3–7 GB/s
```

That ratio is why the WAL is append-only, why LSM trees exist, and why a full table scan can beat
an index when you need a large fraction of the table.

### Key terms at a glance

| Term | One-line definition | Real-world example | Why it exists | § |
|---|---|---|---|---|
| Storage engine | The layer that stores, caches and protects data on disk | The archive department of an office | Separates "what to fetch" (query engine) from "how bytes live on disk" | 1 |
| Page | Fixed-size block (8 KB PG, 16 KB InnoDB), the unit of every read and write | A standard shipping container | Hardware moves blocks anyway; fixed size makes caching and recovery simple | 2 |
| Slotted page | Page layout: header, array of slot pointers, rows packed from the end | A box with a contents card on the lid | Rows can move inside the page without changing their address | 3 |
| TID / RID / ctid | Physical row address = (page number, slot number) | "Box 2,104, document 3" | Indexes need a short, stable way to point at a row | 3 |
| Tuple header | Per-row metadata (creating/deleting txn, flags, null bitmap) | The stamp on each document: who filed it, who voided it | MVCC needs to know which transaction can see which row version | 3 |
| Heap file | Unordered collection of pages; rows go wherever there is room | Boxes stacked wherever there is space | Fastest possible insert | 4 |
| Clustered index | Rows stored *inside* the primary-key B+Tree, sorted by key | A dictionary: the entries are the index | Primary-key lookups and range scans hit the data directly | 4 |
| Free space map (FSM) | Per-page record of how much room is left | The "boxes with room" sheet by the door | Insert without scanning every page | 4 |
| Visibility map (VM) | Bit per page: "every row here is visible to everyone" | A "nothing changed in this box" sticker | Lets index-only scans and VACUUM skip pages | 4 |
| Buffer pool | RAM cache of pages, managed by the database | The desk upstairs | RAM is 1,000× faster than disk | 5 |
| Frame | One page-sized slot in the buffer pool | One spot on the desk | Fixed-size slots, no memory fragmentation | 5 |
| Pin | Counter: "someone is using this frame right now" | A hand on the box | Don't evict a page while a thread reads it | 5 |
| Dirty page | Page changed in RAM, not yet written to disk | Edited document still on the desk | Writes are batched and delayed, not done on every change | 5 |
| Hit ratio | % of page requests served from the buffer pool | % of times the box was already on the desk | The single best health number of a disk-based database | 5 |
| Latch | Microsecond-long lock on an in-memory structure | Holding a door for a second | Two threads must not corrupt the same page bytes | 5 |
| Replacement policy | Rule for choosing which page to evict (LRU, clock, LRU-K) | Which box goes back downstairs | The desk is finite; keep the useful boxes | 6 |
| Sequential flooding | One big scan evicts every hot page | A one-time visitor dumps 1,000 boxes on your desk | Explains why databases don't use plain LRU | 6 |
| Sequential vs random I/O | Reading neighbouring blocks in order vs jumping around | One walk down an aisle vs crisscrossing the basement | Sequential is 10–150× faster, so designs chase it | 7 |
| Buffered vs direct I/O | Through the OS page cache vs bypassing it (`O_DIRECT`) | Using the building's shared shelf vs your own private one | Who controls caching: the OS or the database | 7 |
| fsync | Syscall that forces written data to durable storage | Locking the receipt in the safe | `write()` only reaches RAM in the OS; fsync makes it survive power loss | 7, 8 |
| WAL | Append-only log of every change, written before the data page | The receipt book | Turns many random page writes into one sequential append | 9 |
| LSN | Position of a record in the WAL; also stamped on each page | The receipt number | Recovery compares numbers to know what is already applied | 9 |
| Checkpoint | Flush dirty pages, then record "recovery may start here" | End-of-day filing | Keeps the WAL and recovery time bounded | 8 |
| Torn page | A page only half-written when the power went out | Half the pages of a document photocopied | 8 KB page vs 4 KB atomic disk write | 9 |
| Full-page write / doublewrite | Keep a whole-page copy so a torn page can be restored | Keep a photocopy before editing | Makes recovery possible even from torn pages | 9 |
| Group commit | Many commits share one fsync | One elevator trip for 50 people | fsync is the slowest step of a commit | 9 |
| Checksum | Small hash stored in the page, checked on every read | A tamper seal | Disks and RAM corrupt data silently | 10 |
| Amplification | Extra bytes read/written/stored per byte the user asked for | Carrying a whole box to read one page | Where the cost of a design goes; you can't minimize all three | 11 |
| B-Tree vs LSM | Update pages in place vs append and merge later | Edit the filed document vs keep a stack of correction notes | Read-optimized vs write-optimized | 11 |

### Key terms explained: definition, real-world example, why it exists

**Jump to a term:**
[Storage engine](#storage-engine) ·
[Page](#page) ·
[Slotted page and line pointers](#slotted-page-and-line-pointers) ·
[TID (tuple ID)](#tid-tuple-id) ·
[Tuple header](#tuple-header) ·
[Heap file vs clustered index](#heap-file-vs-clustered-index) ·
[Free space map and visibility map](#free-space-map-and-visibility-map) ·
[Buffer pool](#buffer-pool) ·
[Frame, page table, pin and dirty flag](#frame-page-table-pin-and-dirty-flag) ·
[Hit ratio](#hit-ratio) ·
[Page replacement (eviction)](#page-replacement-eviction) ·
[Latch vs lock](#latch-vs-lock) ·
[Random vs sequential access](#random-vs-sequential-access) ·
[Buffered vs direct I/O](#buffered-vs-direct-io) ·
[fsync](#fsync) ·
[WAL (write-ahead log)](#wal-write-ahead-log) ·
[LSN (log sequence number)](#lsn-log-sequence-number) ·
[Checkpoint](#checkpoint) ·
[Torn page, full-page writes and doublewrite](#torn-page-full-page-writes-and-doublewrite) ·
[Group commit (one fsync, many commits)](#group-commit-one-fsync-many-commits) ·
[Checksum](#checksum) ·
[Amplification (read, write, space)](#amplification-read-write-space) ·
[B-Tree vs LSM engine](#b-tree-vs-lsm-engine)

Every card has the same shape: **Definition** (memorize this sentence), **Real-world example**,
**Why it exists** (what breaks without it), **In real databases**, **Common confusion**, and a
**Remember it like this** line.

#### Storage engine

**Definition.** The storage engine is the lowest layer of a database: it decides how rows are laid
out in bytes on disk, which pages are cached in RAM, and how committed changes survive a crash.
The layers above it (parser, optimizer, executor) only ask it for pages or rows; they never touch
files.

**Real-world example.** In an office, a manager says "bring me Ana's contract". The manager doesn't
care which basement aisle it is in. The archive department (the storage engine) knows the box
numbering, keeps popular boxes upstairs, and keeps the receipt book.

**Why it exists.** Separating "what data do I want" from "how is it stored" lets each side change
independently. MySQL exploits this directly: the same SQL layer runs on InnoDB (B+Tree, crash
safe), MyISAM (no transactions) or MyRocks (LSM).

**In real databases.** PostgreSQL has one built-in heap engine (table access methods since PG 12
allow others). MySQL has a pluggable engine API. MongoDB uses WiredTiger. CockroachDB and TiDB
put SQL on top of LSM key-value engines (Pebble, RocksDB).

**Common confusion.** "Storage engine" is not "the disk". It is software: caching, logging and
layout logic that sits between SQL and the operating system.

**Remember it like this.** *Query engine decides **what**; storage engine decides **where** and
**how safely**.*

#### Page

**Definition.** A page (also called a block) is a fixed-size chunk of a data file, typically 4 KB,
8 KB (PostgreSQL, SQL Server, Oracle) or 16 KB (InnoDB). It is the smallest unit the storage
engine reads from or writes to disk, and the unit it caches in RAM.

**Real-world example.** Shipping containers. Before them, every crate and sack was loaded by hand
in a different way. After them, cranes, ships and trucks only handle one standard box, whatever is
inside. A database file is a row of identical containers: page *N* starts at byte *N* × 8,192,
so finding page 2,104 is arithmetic, not a search.

**Why it exists.**
- The hardware already works in blocks (SSD flash page 4–16 KB, OS page 4 KB). Asking for 100
  bytes still reads at least 4 KB.
- Fixed-size pages fit into fixed-size RAM slots (frames), so the cache never fragments.
- One page read brings ~60 neighbouring rows for free.
- The WAL and recovery can talk about "page 2,104" as a precise unit.

**In real databases.** PostgreSQL 8 KB (compile-time), InnoDB 16 KB (`innodb_page_size`), SQLite
4 KB default. Changing the page size usually requires a full dump and reload.

**Common confusion.** A database page is not the same thing as an OS memory page (4 KB) or an SSD
flash page, although their sizes are chosen to line up.

**Remember it like this.** *You never read a row; you read the page that contains it.*

#### Slotted page and line pointers

**Definition.** A slotted page is the standard layout of a row-store page: a small **header**
at the start, an array of **line pointers** (slots) that grows forward, free space in the middle,
and **rows** packed from the end of the page backwards. Each slot stores the offset and length of
one row.

**Real-world example.** A box with a contents card taped to the lid: "doc 1 at the bottom,
doc 2 above it, doc 3 …". If you re-arrange the documents inside, you only rewrite the card;
anyone told "box 2,104, doc 3" still finds it.

```
┌──────────────────────────────────────────────────────┐
│ header (24 B): page_lsn, checksum, free-space bounds │
│ slots: [1:8040,128][2:7912,128][3:7720,192] → grows  │
│                    free space                        │
│               ← rows grow backwards: │row3│row2│row1│ │
└──────────────────────────────────────────────────────┘
```

**Why it exists.** Rows have different lengths, get deleted (leaving holes) and get updated
(changing size). Without the slot indirection, moving a row inside the page would change its
address and break every index that points at it. With it, the page can be compacted freely.

**In real databases.** PostgreSQL heap pages (`pd_lower`/`pd_upper` mark the free-space hole).
InnoDB uses a variant: records are linked in key order and a sparse **page directory** allows
binary search inside the page (§3).

**Common confusion.** The slot number is the row's *logical* position in the page, not its byte
offset.

**Remember it like this.** *Slots grow forward, rows grow backward, the page is full when they
meet.*

#### TID (tuple ID)

**Definition.** A TID (PostgreSQL `ctid`), RID or ROWID is the physical address of a row:
**(page number, slot number)**. Indexes in heap-organized databases store TIDs to point at rows.

**Real-world example.** "Aisle irrelevant, box 2,104, document 3." Short, precise and enough to
fetch the document in one trip.

**Why it exists.** An index needs a compact pointer (6 bytes in PostgreSQL) to the row, and the
pointer must stay valid when the row moves inside its page. The slot indirection guarantees that.

**In real databases.** Try `SELECT ctid, * FROM users LIMIT 3;` in PostgreSQL. In InnoDB there is
no physical TID: the row's address *is* its primary key, and secondary indexes store the primary
key value instead.

**Common confusion.** A TID is not permanent. In PostgreSQL an `UPDATE` writes a new row version
with a new TID, and `VACUUM FULL` rewrites the table. Never store `ctid` in application data.

**Remember it like this.** *TID = box number + slot number.*

#### Tuple header

**Definition.** The tuple header is the metadata stored in front of every row. In PostgreSQL it is
23 bytes: `xmin` (transaction that created this version), `xmax` (transaction that deleted or
replaced it, 0 if alive), the row's current TID, flag bits, and a null bitmap.

**Real-world example.** Every document in the archive carries a stamp: "filed by clerk 107" and,
later, "voided by clerk 311". A reader checks the stamps to decide whether this version is the
current one *for them*.

**Why it exists.** MVCC (doc 05): readers must see a consistent snapshot while writers create new
versions. The visibility decision is made from `xmin`/`xmax` without taking any lock.

**In real databases.** PostgreSQL: 23 B + alignment ≈ 24 B per row. InnoDB records have a
5-byte header plus hidden `DB_TRX_ID` (6 B) and `DB_ROLL_PTR` (7 B) columns pointing to undo.

**Common confusion.** Tiny rows are expensive: a 40-byte row costs ~70 bytes on disk in PostgreSQL.

**Remember it like this.** *Every row carries "born in txn X, died in txn Y".*

#### Heap file vs clustered index

**Definition.** A **heap file** is a table stored as an unordered set of pages; new rows go into
any page with free space, and every index (including the primary key) points to rows by TID. A
**clustered index** (index-organized table) stores the rows themselves inside the primary-key
B+Tree, sorted by key.

**Real-world example.** Heap: a warehouse where boxes are shelved wherever there is space, plus
separate catalogues that list each box's shelf. Clustered: a dictionary, where the entries are
sorted and the book itself is the index.

**Why it exists.** Two different bets:
- Heap: inserts are fastest (any free spot), and all indexes are equal. Range scans by primary key
  may jump around the file.
- Clustered: primary-key lookups and ranges read the data directly in order. Secondary-index
  lookups need two hops (secondary index → primary key → clustered tree), and random keys such as
  UUIDv4 cause page splits all over the tree.

**In real databases.** PostgreSQL and Oracle (default) use heaps. InnoDB and SQL Server (with a
clustered index) use clustered organization. `CLUSTER` in PostgreSQL sorts a heap once but does not
maintain the order.

**Common confusion.** "Clustered index" does not mean "an index on a cluster of machines".

**Remember it like this.** *Heap: data here, indexes point at it. Clustered: the index **is** the
data.*

#### Free space map and visibility map

**Definition.** The **free space map (FSM)** records, for each page, roughly how much free space it
has, so an `INSERT` can find a page with room without scanning the table. The **visibility map
(VM)** stores bits per page meaning "all rows on this page are visible to every transaction" and
"all rows are frozen".

**Real-world example.** By the archive door hang two sheets: "boxes that still have room" (FSM)
and "boxes nobody has touched since the last audit" (VM). The first speeds up filing; the second
lets auditors skip boxes.

**Why it exists.** Without the FSM, an insert into a 160,000-page table might scan many pages
looking for space. Without the VM, every index-only scan would have to visit the heap to check
visibility, and every VACUUM would read the whole table.

**In real databases.** PostgreSQL stores them as separate files next to the table (`_fsm`, `_vm`).
VACUUM keeps both up to date. InnoDB tracks free space per extent/segment instead.

**Common confusion.** The FSM is approximate on purpose: it may under-report free space, never
over-report.

**Remember it like this.** *FSM answers "where can I write?"; VM answers "can I skip this page?".*

#### Buffer pool

**Definition.** The buffer pool (shared buffers, buffer cache) is a large region of RAM, managed by
the database itself, that holds copies of disk pages. Every page read and write goes through it.

**Real-world example.** The desk upstairs. Fetching a box from the basement takes a minute; a box
already on the desk takes a second. A good clerk keeps the boxes used every day on the desk and
sends rarely used ones back downstairs.

**Why it exists.** RAM is ~1,000× faster than NVMe. With a 99% hit ratio the average page access
costs ~2 µs instead of ~100 µs. It also lets many changes to the same page be combined into one
later disk write.

**In real databases.** PostgreSQL `shared_buffers` (typically ~25% of RAM, because the OS page cache
is a second cache). InnoDB `innodb_buffer_pool_size` (typically 60–80% of RAM, because it uses
`O_DIRECT` and is the only cache). See §5.

**Common confusion.** "Why not just let the OS cache the file (or `mmap` it)?" Because the
database must control *eviction* (resist scans), *write ordering* (WAL before page) and *I/O
errors*; the OS controls none of these for you (doc 00).

**Remember it like this.** *The buffer pool is the desk; disk is the basement; every page passes
across the desk.*

#### Frame, page table, pin and dirty flag

**Definition.** A **frame** is one page-sized slot in the buffer pool. The **page table** is a hash
map from page ID to frame number ("is page 2,104 in RAM, and where?"). The **pin count** is how
many threads are currently using the frame; a pinned frame cannot be evicted. The **dirty flag**
says the page was changed in RAM and must be written to disk before the frame is reused.

**Real-world example.** Desk spots (frames); a sheet listing which box sits on which spot (page
table); a hand resting on a box means "I'm reading this, don't take it" (pin); a red sticky note
"edited, must be refiled" (dirty).

**Why it exists.** The page table makes "is it cached?" an O(1) check. Pins stop the evictor from
yanking a page out from under a reader. The dirty flag ensures no change is lost when a frame is
recycled.

**In real databases.** PostgreSQL keeps a buffer descriptor per frame with a usage count, pin count
and flags; the page table is split into 128 partitions to reduce latch contention.

**Common confusion.** The buffer pool's page table is unrelated to the CPU/OS page table used for
virtual memory.

**Remember it like this.** *Frame = desk spot, page table = seating chart, pin = hand on the box,
dirty = red sticker.*

#### Hit ratio

**Definition.** The buffer pool hit ratio is the fraction of page requests served from RAM without
a disk read: `hits / (hits + misses)`.

**Real-world example.** If 99 of every 100 boxes you need are already on the desk, you walk to the
basement once per 100 requests. At 90%, ten times per 100: the same job takes far longer.

**Why it exists.** It is the quickest single measure of whether the hot data fits in RAM.
Calculation D shows 99% → ~2 µs per page access, 90% → ~11 µs, and 10× more disk reads.

**In real databases.** PostgreSQL: `pg_stat_database` (`blks_hit`, `blks_read`) and `EXPLAIN
(ANALYZE, BUFFERS)`. InnoDB: `Innodb_buffer_pool_read_requests` vs `Innodb_buffer_pool_reads`.
Healthy OLTP is usually above 99%.

**Common confusion.** In PostgreSQL a "miss" may still be served by the OS page cache (fast), so
`blks_read` overstates real disk reads. A high hit ratio is also not proof of health: a query
reading 10 million cached pages is still slow.

**Remember it like this.** *Think in misses: 1% → 10% is ten times the disk traffic.*

#### Page replacement (eviction)

**Definition.** When the buffer pool is full and a new page is needed, the **replacement policy**
chooses which unpinned page (the *victim*) to evict. Classic choices: **LRU** (least recently used),
**clock / second chance** (PostgreSQL), **LRU-K** (SQL Server), **young/old LRU** (InnoDB).

**Real-world example.** The desk is full. Do you send back the box you touched longest ago (LRU)?
What if someone just dumped 1,000 boxes from a one-time audit on your desk: should they push out the
boxes you use every hour? That is **sequential flooding**, and good policies prevent it.

**Why it exists.** RAM is finite. A bad policy turns a 99% hit ratio into 50% after one report query.

**In real databases.**
- PostgreSQL **clock sweep**: each frame has `usage_count` (0–5). A hit increments it; the clock
  hand decrements counts as it sweeps and evicts the first frame at 0. Large sequential scans use a
  small ring buffer so they can't flood the pool.
- InnoDB **young/old list**: new pages enter in the middle (old 3/8); they move to the young part
  only if accessed again after `innodb_old_blocks_time` (1 s). A scan touches each page once, so
  its pages leave quickly.
- SQL Server **LRU-2**: evicts the page whose *second-to-last* access is oldest; pages read once
  are evicted first.

**Common confusion.** Plain LRU is rarely used: besides flooding, moving a page to the list head on
every hit needs a global lock.

**Remember it like this.** *Evict the cold, protect the hot, and never let one big scan clear the
desk.*

#### Latch vs lock

**Definition.** A **latch** is a lightweight, short-lived (microseconds) mutex that protects an
in-memory structure such as a page's bytes or a hash bucket. A **lock** protects logical data (a
row, a table) for the duration of a transaction and supports deadlock detection.

**Real-world example.** A latch is holding a door closed for a second while you squeeze a box
through. A lock is booking a meeting room for the afternoon.

**Why it exists.** Two different dangers. Latches stop two threads from corrupting the same bytes
(a torn in-memory update). Locks stop two transactions from making logically conflicting changes.

**In real databases.** PostgreSQL buffer content locks (shared/exclusive) are latches; row locks
are recorded in the tuple header. InnoDB uses page latches inside B+Tree operations and a separate
lock manager for row locks. See doc 17.

**Common confusion.** Latch waits look like CPU contention, not "lock waits". They are avoided by
acquiring in a fixed order rather than by deadlock detection.

**Remember it like this.** *Latch = a second, protects memory. Lock = a transaction, protects data.*

#### Random vs sequential access

**Definition.** **Sequential I/O** reads or writes adjacent blocks in order; **random I/O** jumps to
unrelated locations. On an HDD each jump costs a 4–10 ms seek; on an SSD random access is far
cheaper but still well below sequential throughput.

**Real-world example.** Collecting 100 boxes from one aisle in a row (sequential) vs 100 boxes each
in a different aisle across the basement (random).

**Why it exists (as a design force).** Calculation F: on HDD sequential is ~150× faster; on NVMe
still several times faster. So databases bend over backwards to be sequential: the WAL is
append-only, B+Tree leaves are linked for range scans, LSM trees turn random writes into sequential
ones, and the optimizer prefers a full scan when a query needs a large share of the table.

**In real databases.** PostgreSQL's `random_page_cost` (default 4.0) vs `seq_page_cost` (1.0)
encodes this ratio for the optimizer; on SSDs people lower it to ~1.1.

**Common confusion.** "SSDs made this irrelevant." They narrowed the gap, they didn't close it, and
SSD wear and write amplification still reward sequential writes.

**Remember it like this.** *Walk the aisle, don't crisscross the basement.*

#### Buffered vs direct I/O

**Definition.** With **buffered I/O** the database reads and writes through the operating system's
page cache. With **direct I/O** (`O_DIRECT`) it bypasses the OS cache and transfers data straight
between its buffer pool and the device.

**Real-world example.** Buffered: you use the building's shared shelf in the hallway, and the
building manager decides what stays there. Direct: you have your own shelf and decide everything
yourself, but you must also do your own planning (prefetching, batching).

**Why it exists.** Buffered I/O is simple and gives a free second-level cache, but the same page
may sit in RAM twice (double buffering) and the OS may evict what the database considers hot.
Direct I/O gives full control and no duplicate copies, at the cost of more engineering.

**In real databases.** PostgreSQL uses buffered I/O (hence `shared_buffers` ≈ 25% of RAM). InnoDB
recommends `innodb_flush_method = O_DIRECT` (hence a buffer pool of 60–80% of RAM). See §7.

**Common confusion.** Direct I/O does **not** mean durable. You still need `fsync`/`O_DSYNC` to
flush the device's write cache.

**Remember it like this.** *Buffered = share the OS cache; direct = own your cache.*

#### fsync

**Definition.** `fsync(fd)` is the system call that blocks until all previously written data of a
file is on durable storage (including flushing the drive's volatile write cache). `fdatasync`
skips non-essential metadata. Until fsync returns, a "written" byte can still vanish on power loss.

**Real-world example.** Writing a receipt (the `write()` call) puts it in your pocket. `fsync` is
walking it to the fireproof safe and locking the door. Only then may you tell the customer "done".

**Why it exists.** `write()` only copies data into the OS page cache in RAM. The OS flushes it
later, in any order. A database that answers "COMMIT OK" after `write()` alone can lose committed
data.

**In real databases.** Every commit fsyncs the WAL (PostgreSQL `wal_sync_method`, InnoDB
`innodb_flush_log_at_trx_commit = 1`). Latency: ~20–50 µs on datacenter NVMe with power-loss
protection, 0.5–2 ms on cloud block storage, several ms on HDD.

**Common confusion.** If fsync returns an error, the data may already be lost and the OS may have
marked the pages clean; retrying is not safe. PostgreSQL now panics and recovers from the WAL
instead (the 2018 "fsyncgate").

**Remember it like this.** *`write()` = in my pocket. `fsync()` = in the safe.*

#### WAL (write-ahead log)

**Definition.** The write-ahead log (redo log in InnoDB) is an append-only file where the database
records every change **before** the changed data page is allowed to reach disk. A transaction is
committed exactly when its commit record is durable in the WAL.

**Real-world example.** A shop keeps a receipt book. Before moving any stock, the clerk writes
"sold 2 lamps to Ana, receipt #9,001" in the book. If the shop burns down overnight, the owner
rebuilds the inventory by replaying the receipts. Updating the stock shelves themselves can happen
at leisure.

**Why it exists.** A transaction may change pages scattered all over the disk. Writing all of them
at commit means several random writes and fsyncs. The WAL turns that into **one small sequential
append + one fsync**, and lets pages be written later in batches. After a crash, replaying the log
restores every committed change.

**The rules (§9).**
1. *Write-ahead:* a dirty page may be written only after the WAL records describing its changes are
   durable (`flushed_wal_lsn ≥ page_lsn`).
2. *Commit:* "COMMIT OK" only after the commit record is durable.
3. *Redo:* each record contains enough information to repeat the change.

**In real databases.** PostgreSQL `pg_wal/` (16 MB segments), InnoDB redo log, SQLite WAL mode,
RocksDB WAL. The same log feeds replication (replicas replay it) and point-in-time recovery.

**Common confusion.** The WAL does not replace the data files. Every change is written twice: once
to the log (now) and once to the page (later). That is the price of fast commits.

**Remember it like this.** *Log first, page later; "saved" means the log is on disk.*

#### LSN (log sequence number)

**Definition.** An LSN is a monotonically increasing number identifying a position in the WAL.
Every WAL record has one, and every page header stores `page_lsn`: the LSN of the last record that
changed that page.

**Real-world example.** Receipt numbers. Each box carries a sticker "includes all changes up to
receipt #8,950". After a fire, for receipt #9,001 you check the box: sticker says 8,950 < 9,001, so
apply it. Receipt #8,900 is already included, so skip it.

**Why it exists.** It makes recovery **idempotent** (applying the log twice gives the same result)
and enforces the WAL rule: before writing a page, check that the WAL is flushed at least up to that
page's `page_lsn`.

**In real databases.** PostgreSQL shows LSNs like `0/16B3748` (`pg_current_wal_lsn()`); replication
lag is measured as an LSN difference. InnoDB stores the LSN in the page header and trailer.

**Common confusion.** An LSN is a log *position*, not a transaction ID. One transaction writes many
records with many LSNs.

**Remember it like this.** *Page LSN ≥ record LSN → already applied, skip.*

#### Checkpoint

**Definition.** A checkpoint writes all pages that were dirty at its start to the data files and
then records a **redo point** in the WAL: crash recovery may start replaying from there, and older
WAL can be recycled.

**Real-world example.** End of day in the shop: all edited boxes go back to the basement and the
owner writes "everything up to receipt #50,000 is filed". After a fire, only receipts after #50,000
need replaying.

**Why it exists.** Without checkpoints the WAL would grow forever and recovery would replay days of
history. Checkpoints trade background write I/O for a bounded WAL size and recovery time.

**In real databases.** PostgreSQL: `checkpoint_timeout` (5 min default), `max_wal_size` (1 GB), and
`checkpoint_completion_target` (0.9) spreads the writes to avoid I/O spikes. The **background
writer** separately cleans pages so backends rarely have to write a dirty victim themselves.
InnoDB uses continuous "fuzzy" checkpointing with page cleaner threads.

**Common confusion.** Checkpoints don't make commits durable (the WAL fsync does). They only limit
how much WAL must be replayed. More frequent checkpoints = faster recovery but more page writes
(and more full-page images in the WAL).

**Remember it like this.** *Checkpoint = "everything before here is filed; start recovery here".*

#### Torn page, full-page writes and doublewrite

**Definition.** A **torn page** is a database page that was only partially written to disk when a
crash happened (e.g. the first 4 KB of an 8 KB page is new, the second 4 KB old). **Full-page
writes** (PostgreSQL) put a complete copy of a page into the WAL the first time it is changed after
each checkpoint. The **doublewrite buffer** (InnoDB) writes pages to a separate sequential area
first, then to their real place.

**Real-world example.** You are rewriting a two-page contract and the power cuts out after page
one. The receipt "change clause 7" can't be applied to a half-old, half-new contract. Keeping a
photocopy of the whole contract before editing it fixes that.

**Why it exists.** Disks guarantee atomic writes only for 512 B–4 KB sectors, but database pages
are 8–16 KB. A WAL record that says "change slot 3" assumes the rest of the page is consistent.

**In real databases.** PostgreSQL `full_page_writes = on` (never turn it off unless the storage
guarantees atomic 8 KB writes). InnoDB `innodb_doublewrite = ON`. The cost is visible as a WAL
volume spike just after each checkpoint.

**Common confusion.** Checksums *detect* torn pages; full-page writes / doublewrite *repair* them.

**Remember it like this.** *Photocopy the page before you edit it for the first time after a
checkpoint.*

#### Group commit (one fsync, many commits)

**Definition.** Group commit lets many transactions that commit at about the same time share a
single WAL fsync: one leader flushes the log up to the latest commit record, and every transaction
whose record was included is acknowledged together.

**Real-world example.** An elevator. Sending it up once per person is slow; waiting a moment and
taking 50 people per trip moves far more people with the same number of trips.

**Why it exists.** fsync is the slowest step of a commit (20 µs–2 ms). Without grouping,
commits/second ≤ 1 / fsync latency (≈ 500/s on a 2 ms cloud disk). With grouping, throughput grows
with concurrency.

**In real databases.** Automatic in PostgreSQL and InnoDB. PostgreSQL `commit_delay` can wait a
few microseconds to collect more commits. Related knobs that trade durability for speed:
PostgreSQL `synchronous_commit = off` and InnoDB `innodb_flush_log_at_trx_commit = 2/0` may lose
the last fraction of a second of commits after a crash, but never corrupt data.

**Common confusion.** Group commit does not weaken durability; asynchronous commit does.

**Remember it like this.** *One elevator trip, many passengers.*

#### Checksum

**Definition.** A page checksum is a small hash (e.g. CRC-32C) of the page's bytes, stored in the
page header when the page is written and recomputed when it is read. A mismatch means the page was
corrupted somewhere between write and read.

**Real-world example.** A tamper-evident seal on each box. If the seal is broken when you open it,
you know not to trust the contents.

**Why it exists.** Storage and memory fail silently: bit rot, firmware bugs, misdirected writes, RAM
bit flips. Without a checksum the database would return wrong data without any error.

**In real databases.** InnoDB and SQL Server checksum by default. PostgreSQL requires
`initdb --data-checksums` (default on since PostgreSQL 18) or offline `pg_checksums --enable`. A
mismatch in PostgreSQL shows up as `invalid page in block X of relation Y`.

**Common confusion.** A checksum detects corruption; it does not fix it. Recovery comes from a
replica, a backup or a full-page image in the WAL.

**Remember it like this.** *Checksums are the smoke alarm, not the fire brigade.*

#### Amplification (read, write, space)

**Definition.** **Write amplification** = bytes physically written ÷ bytes the user changed. **Read
amplification** = bytes (or pages) read ÷ bytes the query needed. **Space amplification** = bytes
stored on disk ÷ bytes of live data.

**Real-world example.** Changing one sentence in a document but refiling the whole box (write
amplification); carrying a whole box upstairs to read one page (read amplification); keeping old
voided copies in the box until the next clean-up (space amplification).

**Why it matters.** It tells you *where the cost of a design goes*. Updating a 100-byte row in a
B-Tree engine writes a ~150 B WAL record, possibly an 8 KB full-page image, then the 8 KB data page
and index pages: 10–30× amortized, ~250× worst case (§11).

**In real databases.** B-Tree engines: low read amplification, higher write amplification. LSM
engines: lower write amplification for random writes, higher read and space amplification. The
**RUM conjecture**: you can optimize at most two of Read, Update (write) and Memory (space).

**Common confusion.** "Low write amplification" doesn't mean "low I/O": LSM compaction does its
writing in large background bursts.

**Remember it like this.** *Every engine pays; the question is in which currency.*

#### B-Tree vs LSM engine

**Definition.** A **B-Tree (page-based) engine** updates data in place: find the page, change it in
the buffer pool, flush it later. An **LSM (log-structured merge) engine** never updates in place:
writes go to an in-memory table (memtable), are flushed as immutable sorted files (SSTables), and
are merged in the background (compaction).

**Real-world example.** B-Tree: editing the filed document directly in its box. LSM: putting a
dated correction note on top of a pile; reading means checking the newest notes first; at night
someone merges the notes into a fresh clean copy.

**Why both exist.** B-Trees give predictable, cheap reads (one tree walk), so they fit read-heavy
and mixed OLTP. LSMs turn random writes into sequential ones, so they fit write-heavy ingestion
(events, IoT, logs) and compress well, at the cost of read amplification (mitigated by Bloom
filters) and compaction work.

**In real databases.** B-Tree: PostgreSQL, InnoDB, SQL Server, SQLite. LSM: RocksDB, LevelDB,
Cassandra, ScyllaDB, HBase, Pebble (CockroachDB). See §11 and doc 13.

**Common confusion.** LSM engines still have a WAL; the memtable lives in RAM and must be
recoverable.

**Remember it like this.** *B-Tree: edit in place, read fast. LSM: append now, merge later, write
fast.*

---


## 1. The Storage Engine's Role

> **In plain words.** A database is two programs stacked on top of each other. The top one (the
> query engine) understands SQL: it decides *what* data is needed and in which order to combine it.
> The bottom one (the storage engine) understands bytes and disks: it decides *where* each row
> lives, keeps the most useful pages in RAM, and makes sure a committed change is never lost. The
> two talk through a narrow interface, roughly "give me page 2,104 of table `users`" and "this page
> changed". Everything in this chapter happens below that line.
>
> **Real-world example.** In MySQL you can run `CREATE TABLE t (...) ENGINE=InnoDB` or
> `ENGINE=MyRocks`: same SQL, same optimizer, completely different storage engines underneath (a
> B+Tree and an LSM tree). That is the boundary made visible.

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

> **In plain words.** A database file is not a long stream of rows; it is a sequence of identical,
> fixed-size boxes called pages. Every time the database touches the disk it moves whole pages, and
> every slot in its RAM cache holds exactly one page. Once you accept that "the page is the unit of
> everything", the rest of the chapter follows: the buffer pool caches pages, the WAL describes
> changes to pages, checksums protect pages, and the cost of a query is roughly "how many pages did
> it touch, and were they in RAM?".
>
> **Real-world example.** Running `SELECT * FROM users WHERE id = 42` in PostgreSQL returns ~100
> bytes, but the engine reads at least one full 8 KB heap page plus a few 8 KB index pages. You can
> see this with `EXPLAIN (ANALYZE, BUFFERS)`: it reports `shared hit=4` (four pages found in RAM),
> not "100 bytes".

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

Because every page has the same size, the database never needs a table of contents to find one.
The address of a page is simply "which file" plus "which page number in that file", and the byte
position is a multiplication. PostgreSQL identifies a page by the table's file (its
*relfilenode*) and a 0-based block number; once a table file reaches 1 GB it continues in a new
segment file (`24576.1`, `24576.2`, …), so block 200,000 lives in segment 1. InnoDB uses a
tablespace ID and a page number. This arithmetic addressing is what makes TIDs, WAL records and
buffer-pool keys so compact: a few integers identify any page in the database.

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

Choosing a page size is a balance between two kinds of waste. **Small pages** waste less I/O when
you only need one row (you read 4 KB instead of 16 KB) and match SSD flash pages nicely, but a
B+Tree needs more levels because each page holds fewer keys, and each page carries a fixed header,
so metadata overhead grows. **Large pages** give higher fan-out (fewer tree levels, fewer page
fetches for range scans) and suit wide rows, but every point lookup drags in more unneeded bytes,
every frame in the buffer pool is bigger, and every full-page image written to the WAL after a
checkpoint is bigger. In practice the defaults (8 KB PostgreSQL, 16 KB InnoDB) are good for OLTP,
and changing them later means dumping and reloading the whole database, so you almost never do.

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

> **In plain words.** Once we have a box, we need to organize what goes inside. Rows have different
> lengths, they get deleted (leaving holes) and updated (growing or shrinking). The *slotted page*
> solves this with a small directory at the front of the page: slot 1 says "row 1 starts at byte
> 8,040 and is 128 bytes long", slot 2 says the same for row 2, and so on. Indexes point at
> *(page, slot)*, never at a raw byte offset, so the page is free to shuffle rows around internally,
> for example to squeeze out holes, without anyone outside noticing. The page also starts with a
> header (its LSN, a checksum, and where the free space begins and ends), and each row carries its
> own small header that MVCC uses to decide who may see it.
>
> **Real-world example.** In PostgreSQL, `SELECT ctid, id FROM users LIMIT 3;` shows addresses like
> `(0,1)`, `(0,2)`, `(0,3)`: page 0, slots 1–3. Run `UPDATE users SET name = name WHERE id = 1;` and
> the row's `ctid` changes, because PostgreSQL wrote a new version of the row into a new slot.

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

> **In plain words.** Now zoom out from one page to a whole table. There are two big philosophies.
> In a **heap** (PostgreSQL, Oracle), the table is a pile of pages with no order: a new row goes into
> whichever page has room, and *all* indexes, including the primary key, are separate structures
> that point at rows by TID. In a **clustered index** (InnoDB, SQL Server), the table *is* the
> primary-key B+Tree: rows are stored in its leaf pages, sorted by key. Heaps make inserts cheap and
> treat all indexes equally; clustered tables make primary-key lookups and ranges cheap but make
> secondary indexes take two hops and punish random primary keys. Two helper structures make heaps
> practical: the **free space map** (where is there room for a new row?) and the **visibility map**
> (which pages contain only rows everyone can see, so VACUUM and index-only scans can skip them?).
>
> **Real-world example.** Inserting 10 million rows with a UUIDv4 primary key is fine in a
> PostgreSQL heap (rows are appended; only the PK index suffers). In InnoDB the same load inserts
> each row into a random leaf of the clustered tree, causing page splits and a working set equal to
> the whole table, which is why InnoDB users are told to use auto-increment or UUIDv7 keys.

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

> **In plain words.** The buffer pool is the database's own RAM cache of pages, and it is the
> single biggest reason a database is fast. It is a big array of page-sized slots (frames) plus a
> hash table that answers "is page X in memory, and in which frame?". Every read asks the buffer pool
> first; only on a miss does the database go to disk. Every write also happens here: the page is
> changed in RAM and marked *dirty*, and it is written to disk later, often after many more changes
> have accumulated on it. Two small counters per frame keep this safe: a **pin count** (someone is
> using this page right now, don't evict it) and a **dirty flag** (this page has changes that must
> reach disk before the frame can be reused). Because many threads use the buffer pool at once, its
> structures are protected by **latches**, very short locks that last microseconds.
>
> **Real-world example.** On a PostgreSQL server where the hot data fits in RAM, `pg_stat_database`
> typically shows `blks_hit` around 99% of `blks_hit + blks_read`. Restart the server and the cache
> is empty ("cold"): the same queries become many times slower until the hot pages are read back in,
> which is why tools like `pg_prewarm` exist.

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

Read the flow below as the answer to "what happens when the executor needs page (5, 12)?". First,
a hash lookup in the page table. If the page is there (a **hit**), the thread pins it and gets a
pointer to the frame; no disk is involved and the whole thing costs about a microsecond. If it is
not there (a **miss**), the buffer pool needs an empty frame. It takes one from the free list if
possible; otherwise the replacement policy (§6) picks a victim that nobody has pinned. If that
victim is dirty, it must first be written to disk, and before *that*, the WAL must be flushed up
to the victim's page LSN (the WAL rule, §9). Only then is the requested page read into the frame,
registered in the page table, pinned, and returned. This is why background writers exist: if clean
frames are always available, a query never has to wait for someone else's dirty page to be written.

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

> **In plain words.** The buffer pool is always smaller than the database, so sooner or later a new
> page needs a frame that is already occupied. The replacement policy decides which page to throw
> out. The ideal is to evict the page that won't be needed for the longest time; since nobody knows
> the future, policies guess from the past. The obvious guess, *least recently used* (LRU), has a
> famous failure: one big table scan touches thousands of pages exactly once and pushes every
> genuinely hot page out of memory (**sequential flooding**). Real databases therefore use smarter
> variants that reward pages used *repeatedly* rather than pages used *recently*: PostgreSQL's clock
> sweep with usage counters, InnoDB's young/old lists, SQL Server's LRU-2.
>
> **Real-world example.** A nightly `SELECT count(*) FROM orders` over a 200 GB table on a server
> with 64 GB of buffer pool would, under plain LRU, leave the cache full of old order pages and make
> the morning's checkout traffic miss on every request. With PostgreSQL's ring buffer for large
> scans or InnoDB's midpoint insertion, the scan cycles through a small part of the cache and the
> hot pages survive.

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

Think of the frames arranged around a clock face, with a single hand pointing at one frame. Every
time a page is used, its frame's counter goes up (to a maximum of 5). When a free frame is needed,
the hand moves around the circle: a frame with a counter above zero gets "a second chance", meaning
its counter is decreased and the hand moves on; the first frame found at zero is evicted. Pages
that are used often keep getting their counters topped up and survive many sweeps; pages used once
drop to zero quickly. The big advantage over LRU is cost: a hit only increments a small counter in
the frame, instead of moving the page to the head of a shared list under a global lock.

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

> **In plain words.** Everything the buffer pool cannot answer becomes disk I/O, and disk I/O is
> where database time goes. Three ideas matter. First, the **speed gap**: RAM answers in ~100 ns, an
> NVMe SSD in ~100 µs, a hard disk in ~5–10 ms. Second, **access pattern**: reading neighbouring
> blocks in order (sequential) is much faster than jumping around (random), dramatically so on hard
> disks and still noticeably on SSDs. Third, **the path** a request takes: through the OS page cache
> or around it (`O_DIRECT`), through an I/O scheduler, into a device that may hold writes in its own
> volatile cache. A write is only safe after `fsync` forces it through all of these layers. Most
> storage-engine designs (append-only logs, linked B+Tree leaves, LSM trees, prefetching) exist to
> make I/O rarer, more sequential, or overlapped with useful work.
>
> **Real-world example.** On a cloud volume rated at 3,000 IOPS, a query that needs 30,000 random
> page reads takes at least 10 seconds no matter how fast the CPU is. The same 30,000 pages read
> sequentially (≈ 240 MB) take well under a second at typical 250+ MB/s throughput.

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

When the database calls `pread()` or `pwrite()`, the request does not go straight to the disk. It
first enters the kernel, which checks its own page cache; with buffered I/O a cached read is just a
memory copy, and a write simply lands in that cache and returns. Only on a cache miss, or when the
kernel decides to flush, does a block request go to the I/O scheduler, which may merge or reorder
requests, and then to the device driver and the drive itself. The final trap is the drive's own
**volatile write cache**: the drive may say "done" while the data is still in its RAM. That is why
durability requires `fsync` (which also tells the drive to flush its cache) or drives with
power-loss protection. Each layer can make I/O faster, and each layer is a place where a "written"
byte can still disappear.

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

> **In plain words.** The write path has a tension at its heart. Users want "COMMIT OK" in well
> under a millisecond, but writing the changed data pages to their places on disk is slow and
> random. The solution is to split the work in two. At commit, write only a small, sequential
> description of the change to the WAL and fsync it: that is enough to guarantee the change can be
> rebuilt. Later, in the background, write the actual pages, batching many changes into each page
> write. Two background processes handle the "later": the **background writer** keeps a supply of
> clean frames for the buffer pool, and the **checkpointer** periodically makes sure every old change
> has reached the data files, so the WAL before that point can be deleted and crash recovery stays
> short.
>
> **Real-world example.** A PostgreSQL server doing 5,000 updates per second may write only a few
> MB/s of WAL, while its data pages are flushed in waves every few minutes during checkpoints. If
> `max_wal_size` is too small you will see "checkpoints are occurring too frequently" in the log and
> I/O spikes on the disk graph.

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

A checkpoint answers the question "after a crash, how far back in the WAL do we have to start?".
It begins by noting the current WAL position (the *redo point*). Then it writes every page that was
dirty at that moment to the data files, spreading the writes over time so it does not flood the
disk. When all of them are on disk, it records "the last completed checkpoint started at redo point
X" in a control file. From then on, recovery can start at X, because every change before X is
already in the data files, and WAL segments older than X can be recycled. Pages changed *during* the
checkpoint are fine: their WAL records come after X and will be replayed if needed. The LSN stored
in each page lets recovery skip records the page already contains.

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

> **In plain words.** The WAL is the storage engine's diary. Every change is first written as a
> record at the end of an append-only file, and each record gets a growing number, its **LSN**. The
> golden rule is in the name: *write ahead*. The log record describing a change must be durable on
> disk before the changed data page is allowed to be written, and before the client is told the
> transaction committed. If the machine crashes, the data files may be missing recent changes or
> even contain changes from transactions that never committed, but the log has the full story: the
> database replays it from the last checkpoint (redo) and rolls back what never committed (undo).
> This section also covers the practical details that make the WAL work in real life: what a record
> contains, how much of the page change it describes, how torn half-written pages are repaired, and
> how many commits share one fsync (group commit).
>
> **Real-world example.** PostgreSQL's WAL lives in `pg_wal/` as 16 MB segment files. The same
> records drive three features: crash recovery (replay locally), streaming replication (ship the
> records to a replica and replay them there), and point-in-time recovery (replay archived WAL on
> top of a backup up to, say, 14:03 yesterday, just before someone ran `DELETE` without `WHERE`).

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

Every WAL record answers four questions: *where am I in the log* (its LSN), *which transaction made
me* (transaction ID), *what was this transaction's previous record* (prev LSN, which chains a
transaction's records together so it can be rolled back by walking the chain backwards), and *what
changed* (type and payload). The payload can be written at different levels of detail, which is
the topic of the next subsection: exact bytes (physical), a high-level operation (logical), or the
common middle ground used by PostgreSQL and InnoDB, an operation applied to one specific page
(physiological).

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

> **In plain words.** Hardware lies occasionally. A disk can return the wrong block, firmware can
> write to the wrong place, a cosmic ray can flip a bit in RAM, and a crash can leave a page half
> written. None of these raise an error by themselves; the database would just return wrong data.
> A **checksum** is a small fingerprint of the page's bytes, computed when the page is written and
> checked when it is read back. If the fingerprint doesn't match, the database knows the page is
> corrupt and refuses to use it instead of silently serving garbage. Checksums detect; replicas,
> backups and full-page images in the WAL are what repair.
>
> **Real-world example.** A PostgreSQL server with data checksums enabled that hits a bad SSD block
> raises `invalid page in block 48211 of relation base/16384/24576` on the first read of that page.
> Without checksums, the same query might return a row with a garbled email address, and nobody would
> notice until a customer complains.

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

> **In plain words.** All the pieces in this chapter can be assembled in two main ways. A
> **page-based B-Tree engine** keeps one copy of each row in a page and edits it in place (through
> the buffer pool and WAL); reads are a short tree walk, writes dirty whole pages. A **log-structured
> (LSM) engine** never edits in place: it buffers writes in memory, flushes them as sorted immutable
> files, and merges those files in the background; writes are sequential and cheap, reads may have to
> check several files. Neither is better in general. Each pays a different mix of *read*, *write* and
> *space* amplification, and the right choice depends on whether your workload is mostly reads,
> mostly writes, or mostly scans.
>
> **Real-world example.** PostgreSQL and MySQL (B-Tree) dominate classic OLTP like orders and users.
> Cassandra, ScyllaDB and RocksDB-based systems (LSM) dominate high-volume ingestion like event logs,
> metrics and messaging. Analytics engines like DuckDB and ClickHouse use a third design, columnar
> storage (doc 08).

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

Write amplification is easiest to understand by following one small change all the way to disk.
In a B-Tree engine, updating a 100-byte row writes a WAL record, possibly a full 8 KB copy of the
page into the WAL (the first change after a checkpoint), later the whole 8 KB data page, and the
8 KB page of every index that changed. The user changed 100 bytes; the disk received thousands of
times more in the worst case. In practice many updates land on the same page between flushes, so
the real amortized factor is far smaller. An LSM engine writes the change into a small sorted file,
then rewrites it a few more times as compaction merges files level by level; each rewrite is
sequential and compressed, so the total is usually lower for random writes, but it arrives in big
background bursts.

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

## 12. Common Pitfalls and Misconceptions

> **In plain words.** Most storage-engine bugs and outages come from a short list of wrong mental
> models. Each item below states the myth, the reality, and the section that explains it.

1. **"`write()` returned, so the data is saved."** No. `write()` copies bytes into the OS page
   cache in RAM. Only `fsync`/`fdatasync` (or `O_DSYNC`) makes them survive power loss (§7, §9).
   Any home-grown storage code that skips fsync loses data on the first power cut.
2. **"The database writes my row to the data file when I commit."** No. It writes a small WAL
   record and fsyncs the log; the data page is written later by the background writer or
   checkpointer (§8). This is why commits are fast and why recovery exists.
3. **"A bigger buffer pool is always better."** For InnoDB with `O_DIRECT`, mostly yes, up to ~80%
   of RAM. For PostgreSQL, no: it also relies on the OS page cache, and connections, sorts and
   hashes (`work_mem`) need memory too. Starving the OS leads to swapping, which is far worse than
   a cache miss (§5).
4. **"99% hit ratio means we're fine."** Hit ratio says nothing about *how many* pages a query
   touches. A query reading 2 million cached pages still burns seconds of CPU. Look at buffers per
   query (`EXPLAIN (ANALYZE, BUFFERS)`) as well as the global ratio (§5).
5. **"An index scan is always faster than a full scan."** Each row fetched through an index can be
   a random page read. Past roughly 5–20% of the table, a sequential scan wins (§7).
6. **"SSDs made sequential vs random irrelevant."** They narrowed the gap. Sequential is still
   faster, and random small writes increase SSD-internal write amplification and wear (§7).
7. **"Turning off `full_page_writes` / doublewrite is a free speed-up."** Only if your storage
   guarantees atomic page-sized writes (e.g. ZFS with matching record size, some cloud volumes).
   Otherwise a crash can leave torn pages that recovery cannot repair (§9).
8. **"Checksums protect my data."** They *detect* corruption. You still need replicas, backups, and
   full-page images to *repair* it. PostgreSQL clusters created before v18 often have checksums off
   (§10).
9. **"UUIDv4 primary keys are free."** In a clustered index (InnoDB) or any B+Tree, random keys
   insert into random leaf pages: constant page splits, half-empty pages, and a hot set equal to the
   whole index. Prefer sequential keys (bigint identity, UUIDv7) (§4).
10. **"Checkpoints make commits durable."** The WAL fsync does that. Checkpoints only bound WAL size
    and recovery time. Too-frequent checkpoints cause I/O spikes and WAL bloat from full-page images
    (§8, §9).
11. **"A latch wait is a lock wait."** Latches protect memory for microseconds; locks protect rows
    for a transaction. Latch contention appears as CPU/spinning on hot pages (e.g. the right-most
    leaf of a sequential index), not as blocked transactions (§5).
12. **"LSM is just faster."** It is faster at ingesting random writes. It pays with read
    amplification, space amplification and background compaction that can stall writes (§11).

---

## 13. Interview Questions and System Design Prompts

> **In plain words.** For this chapter, interviewers check whether you can reason *through the
> layers*: "this is slow because the buffer pool misses, because the working set no longer fits,
> because the index is on random UUIDs". Answer every question in the same order: **one-sentence
> definition → one number → one trade-off**. Naming products without mechanisms is a weak signal.
>
> **Example of a strong answer.** *"Why doesn't the database write the data page at commit?"* →
> "Because a commit can touch pages scattered all over the disk. Instead it appends ~150 bytes to
> the WAL and fsyncs that sequential file, ~20–50 µs on NVMe, shared by many commits through group
> commit. Pages are written later in batches, and after a crash they are rebuilt from the log. The
> trade-off: every change is written twice, and recovery time depends on checkpoint frequency."

### 13.1 Conceptual questions: "explain X"

**Q1. What is a storage engine, and what are its responsibilities?**
*Sections: Start here, §1*
The layer below the query engine that owns the on-disk format, the in-memory page cache and
durability. Responsibilities: page layout and space management, buffer management, latching for
concurrent access, durability through WAL + fsync, and crash recovery. Good bonus: MySQL's
pluggable engines (InnoDB vs MyRocks) show the boundary concretely.

**Q2. Why do databases read and write fixed-size pages instead of individual rows?**
*Sections: §2*
Hardware transfers blocks anyway (4 KB flash page / OS block), so reading 100 bytes costs the same
as 4–8 KB. Fixed sizes make the buffer pool a simple array of frames without fragmentation, make
addresses arithmetic (offset = page_no × page_size), bring neighbouring rows into cache for free,
and give the WAL and recovery a precise unit. Trade-off of the size: larger pages → higher B-Tree
fan-out and better scans, but more wasted I/O for point lookups and bigger full-page images.

**Q3. Draw a slotted page. Why the indirection through line pointers?**
*Sections: §3*
Header (LSN, checksum, free-space bounds) → slot array growing forward → free space → rows growing
backward. Indexes point to (page, slot), not byte offsets, so rows can be moved, compacted or
resized inside the page without updating any index. Page is full when slots and rows meet.

**Q4. Heap table vs clustered index: what changes for lookups, inserts, and secondary indexes?**
*Sections: §4*
Heap: rows in any page with space; every index stores a TID; inserts cheap; PK range scans may be
random. Clustered (InnoDB): rows live in PK B+Tree leaves, sorted; PK lookups and ranges are direct;
secondary indexes store the PK, so lookups are two tree walks; random PKs cause page splits.
Mention PostgreSQL HOT updates as the heap's trick to avoid index updates.

**Q5. Walk me through a buffer pool page request, hit and miss.**
*Sections: §5, §6*
Hash the page ID in the page table. Hit: pin, return pointer (~1 µs). Miss: take a free frame or
run the replacement policy to choose an unpinned victim; if dirty, write it out first (after
ensuring WAL is flushed to its page LSN); read the page from disk (~100 µs NVMe); insert into the
page table; pin; return. Mention latches on the page-table partition and frame header.

**Q6. Why does a database have its own buffer pool instead of relying on the OS page cache or
`mmap`?**
*Sections: §5, §7, doc 00*
The database needs to control eviction (scan resistance, knowing which pages are hot), write
ordering (WAL must hit disk before the page), and error handling. With `mmap` the OS may write a
dirty page at any time, violating the WAL rule, and I/O shows up as unpredictable page-fault stalls.
Nuance: PostgreSQL still uses buffered I/O (double caching); InnoDB uses `O_DIRECT`.

**Q7. What is sequential flooding and how do real engines prevent it?**
*Sections: §6*
A large scan, under plain LRU, evicts every hot page with pages used exactly once. PostgreSQL: clock
sweep with usage counts plus a small ring buffer for big scans. InnoDB: new pages enter the old
sublist at the midpoint and are promoted only if re-accessed after 1 s. SQL Server: LRU-2, which
evicts pages with only one recent access first.

**Q8. Why do PostgreSQL and InnoDB recommend such different buffer pool sizes (~25% vs ~70–80%)?**
*Sections: §5, §7*
PostgreSQL uses buffered I/O, so the OS page cache is a second cache; a huge `shared_buffers` just
duplicates pages and starves the OS. InnoDB with `O_DIRECT` bypasses the OS cache, so its buffer
pool is the only cache and should take most of the RAM.

**Q9. Explain the WAL protocol. What exactly must be on disk, and when?**
*Sections: §8, §9*
Rule 1: before a dirty page is written, the WAL up to that page's LSN must be durable. Rule 2: a
transaction is committed only when its commit record is durable. Rule 3: records contain enough to
redo. Consequence: no-force (pages aren't written at commit) and steal (dirty pages of uncommitted
transactions may be written, so undo info is needed). Steal + no-force = fastest commits and a
buffer pool that never has to hold a whole transaction.

**Q10. What is an LSN and how does it make recovery idempotent?**
*Sections: §9*
A monotonically increasing WAL position. Each page stores the LSN of its last change. During redo,
a record is applied only if `record.lsn > page.lsn`, so replaying the log twice (e.g. crashing
during recovery) produces the same result.

**Q11. What does a checkpoint do, and what is the trade-off in how often it runs?**
*Sections: §8*
Flush dirty pages, then record a redo point so older WAL can be recycled and recovery starts there.
Frequent checkpoints → short recovery, but more page writes and more full-page images in the WAL.
Infrequent → less write I/O, longer recovery, more WAL on disk. PostgreSQL spreads the writes with
`checkpoint_completion_target`.

**Q12. What is a torn page, and how do PostgreSQL and InnoDB handle it?**
*Sections: §9*
A crash during an 8–16 KB page write leaves a page half old, half new, because only 512 B–4 KB is
written atomically. PostgreSQL logs a full page image in the WAL on the first change after each
checkpoint; InnoDB writes pages to the doublewrite buffer first. Checksums detect torn pages; these
mechanisms repair them.

**Q13. What does group commit buy, and how is it different from asynchronous commit?**
*Sections: §9*
Group commit: many commits share one fsync; durability unchanged; throughput no longer capped at
1/fsync latency. Asynchronous commit (`synchronous_commit=off`, `innodb_flush_log_at_trx_commit=2`)
acknowledges before fsync, so a crash can lose the last fraction of a second of commits, but never
corrupts the database.

**Q14. B-Tree vs LSM: when would you choose each?**
*Sections: §11*
B-Tree: in-place updates, low read amplification, predictable latency; read-heavy and mixed OLTP.
LSM: buffered sequential writes, good compression, low write amplification for random writes;
write-heavy ingestion. LSM pays with read amplification (Bloom filters help), space amplification
and compaction stalls. Frame it with the RUM conjecture.

**Q15. Compute the write amplification of updating one 100-byte row in PostgreSQL.**
*Sections: §11*
~150 B WAL record + 8 KB full-page image (if first change since checkpoint) + 8 KB heap page flush
+ 8 KB per index page touched. Worst case ~250×; amortized 10–30× because many changes share a page
flush. HOT updates (no indexed column changed, room on the page) avoid the index writes.

### 13.2 System design round

**Q. Size the memory for a PostgreSQL server: 400 GB database, of which the last 30 days of orders
and all indexes on them (~40 GB) are queried constantly. 128 GB RAM machine.**

```
1. FIND THE WORKING SET
   - Hot data ≈ 40 GB (recent orders + their indexes) + upper levels of all other indexes (small).
   - Goal: hot set in RAM → hit ratio > 99%.

2. SPLIT THE RAM (PostgreSQL uses buffered I/O)
   - shared_buffers ≈ 32 GB (25%).
   - OS page cache gets most of the rest (~80 GB) → hot set fits in shared_buffers + OS cache.
   - Reserve for connections: e.g. 200 conns × work_mem 16 MB worst case → pool connections
     (PgBouncer) instead of raising RAM.

3. VERIFY WITH NUMBERS
   - Hit ratio from pg_stat_database; per-query buffers from EXPLAIN (ANALYZE, BUFFERS).
   - If the hot set grows past RAM: misses jump 10× when hit ratio goes 99% → 90%.

4. PROTECT THE HOT SET
   - Analytics/reporting on a replica so big scans don't compete for cache and I/O.
   - Partition orders by month so "recent" data is physically clustered in recent pages.

5. TRADE-OFFS
   - InnoDB equivalent would be innodb_buffer_pool_size ≈ 90–100 GB with O_DIRECT.
   - Bigger RAM vs faster disks: RAM wins while the working set can fit; past that, NVMe latency
     sets your p99.
```

*What interviewers listen for:* working set, not database size; the double-buffering reason for 25%;
hit-ratio math; isolation of analytics.

**Q. Design a simple single-node key-value store with crash safety (put/get/delete, 1 KB values,
10K writes/s).**

```
1. CHOOSE THE SHAPE
   - Simplest durable design: append-only log + in-memory hash index (Bitcask).
       put  → append (key, value, crc) to the active log file, update hash: key → (file, offset)
       get  → hash lookup → one pread
       delete → append a tombstone
   - Alternative: B+Tree of pages + WAL (if range scans are needed) or LSM (if keys don't fit in RAM).

2. DURABILITY
   - Commit = fsync of the log; group commit to batch 10K writes/s into ~100–1,000 fsyncs/s.
   - CRC per record → detect torn tail after crash; truncate at first bad record.

3. RECOVERY
   - Rebuild the hash by scanning log files, or load a periodic "hint file" (a checkpoint of the
     index) and scan only newer data.

4. SPACE RECLAMATION
   - Old values and tombstones accumulate → background compaction rewrites live keys into new
     files (this is space amplification vs write amplification).

5. LIMITS AND TRADE-OFFS
   - All keys must fit in RAM (hash index); no range queries.
   - 10K × 1 KB = 10 MB/s of sequential writes: trivial for any SSD.
```

*What interviewers listen for:* fsync placement, checksums for torn writes, recovery path,
compaction, and an honest statement of limits.

**Q. An IoT platform ingests 300,000 readings/s (~200 B each) and serves "last 24 h for device
X". B-Tree or LSM?**
*Sections: §7, §11, docs 13, 20*
300K × 200 B ≈ 60 MB/s of small random-key writes. A B-Tree would dirty random leaf pages all over
the index: page-sized writes per tiny row, heavy write amplification. Choose an LSM (or a
time-series engine): memtable + WAL with group commit, sequential SSTable flushes, key
`(device_id, timestamp)` so one device's data is contiguous and the query becomes a range scan,
time-window compaction so expired data drops as whole files, Bloom filters for point lookups. Shard
by `device_id` when one node is not enough.

### 13.3 Rapid-fire questions

| Question | Strong answer | § |
|---|---|---|
| PostgreSQL page size? InnoDB? | 8 KB; 16 KB | 2 |
| How do you find page N in a file? | offset = N × page_size; no lookup needed | 2 |
| What is a TID? | (page number, slot number) | 3 |
| PostgreSQL tuple header size? | 23 bytes (+ alignment, + 4 B line pointer) | 3 |
| Why is `ctid` unsafe to store? | Updates create new versions with new TIDs; VACUUM FULL rewrites the table | 3 |
| What does the FSM do? | Finds a page with enough free space for an insert | 4 |
| What does the visibility map enable? | Index-only scans and VACUUM skipping all-visible pages | 4 |
| Can a pinned page be evicted? | No | 5 |
| What must happen before evicting a dirty page? | WAL flushed up to its LSN, then the page is written | 5, 9 |
| Hit ratio 99% → 90%: how much more disk I/O? | 10× (misses 1% → 10%) | 5 |
| Why clock instead of LRU? | No global list update on every hit; bit/counter per frame | 6 |
| PostgreSQL max usage_count? | 5 | 6 |
| InnoDB's scan resistance? | Midpoint insertion + `innodb_old_blocks_time` (1 s) | 6 |
| Does `O_DIRECT` make writes durable? | No; you still need fsync / O_DSYNC | 7 |
| NVMe random read latency? | ~80–100 µs (QD1) | 7 |
| Commit durability point in PostgreSQL? | fsync of WAL through the commit record | 9 |
| Why is the WAL append-only? | Sequential writes are the cheapest I/O | 7, 9 |
| What is steal / no-force? | Uncommitted pages may be flushed / committed pages needn't be flushed at commit | 9 |
| Physiological logging? | Physical to a page, logical within it | 9 |
| Async commit: can it corrupt data? | No, it can lose the last few hundred ms of commits | 9 |
| What do checksums detect? Fix? | Silent corruption; they don't fix anything | 10 |
| B-Tree vs LSM in one line each? | Edit in place, read fast / append and merge, write fast | 11 |
| RUM conjecture? | Optimize at most two of read, update, memory overheads | 11 |

### 13.4 Debugging prompts: "here are the symptoms, diagnose"

**"Every night at 2 a.m. the API gets slow for ~30 minutes after the reporting job starts."**
The report does large scans that compete for the buffer pool and disk bandwidth (§5, §6). Even with
scan-resistant replacement, its I/O saturates the disk and evicts some hot pages; hit ratio dips,
misses multiply. Fix: run reports on a replica or columnar copy; throttle; add the right index if
the report is filtering a small subset.

**"Latency spikes every 5 minutes, and the disk write graph shows a sawtooth."**
Checkpoints (§8). Each one flushes many dirty pages, and the WAL briefly swells with full-page images.
Check `checkpoint_timeout`, `max_wal_size` (frequent "requested" checkpoints mean it is too small),
and raise `checkpoint_completion_target` to spread writes.

**"After moving to a cheaper cloud disk, commits went from 0.3 ms to 4 ms."**
The commit path's floor is WAL fsync latency (§7, §9). Measure with `pg_test_fsync` or `fio
--fsync=1`; check IOPS/throughput quotas. Fix: put the WAL on a low-latency volume, verify group
commit is effective under load, or use async commit for non-critical writes.

**"Inserts into an InnoDB table got 5× slower as it grew past the buffer pool size. PK is a
UUIDv4."**
Random keys insert into random leaf pages of the clustered index (§4). Once the index exceeds the
buffer pool, almost every insert is a miss plus a dirty page plus frequent page splits. Fix:
sequential keys (auto-increment or UUIDv7), or more RAM as a stopgap.

**"PostgreSQL logs `ERROR: invalid page in block 48211 of relation base/16384/24576`."**
A page checksum mismatch (§10): corruption from disk, firmware, RAM, or a torn write without
protection. Don't keep writing to it blindly: check hardware/dmesg, fail over to a healthy replica
or restore from backup + WAL, then find the root cause.

**"The `events` table holds 5 GB of live rows but uses 60 GB on disk."**
Dead tuples from updates/deletes not yet reclaimed (§3, §4). Something blocks VACUUM (long-running
or idle-in-transaction session, old replication slot), or autovacuum can't keep up. Fix the
blocker, tune autovacuum; reclaim space with `pg_repack` or `VACUUM FULL` (takes a lock).

### 13.5 Common interview mistakes

1. **Saying "the database writes the row to disk on commit".** It writes the WAL; pages come later.
2. **Equating `write()` with durability.** Always name fsync and where it happens.
3. **Quoting database size instead of working set** when sizing memory.
4. **Ignoring the cost side.** Every index costs page writes on each insert; every guarantee (sync
   commit, full-page writes) costs latency or bytes.
5. **Treating SSDs as random-access-for-free.** Sequential still wins, and random writes wear flash.
6. **Mixing up latches and locks**, or checkpoints and commits.
7. **Product-name answers.** "Use RocksDB" is weak; "write-heavy, random keys → LSM, because it
   converts random writes into sequential ones" is strong.

---

## 14. One-Page Cheat Sheet

```
UNITS          page 8 KB (PG) / 16 KB (InnoDB); ~60 rows of 100 B per 8 KB page
LATENCY        RAM ~100 ns | buffer hit ~1 µs | NVMe read ~100 µs | HDD ~5–10 ms
               fsync: NVMe (PLP) 20–50 µs | cloud disk 0.5–2 ms | HDD several ms
LAYOUT         file = pages; page = header + slots → free ← rows; row address = TID (page, slot)
ORGANIZATION   heap (PG): unordered, indexes → TID | clustered (InnoDB): rows inside PK B+Tree
HELPERS        FSM = where is free space | VM = which pages are all-visible
BUFFER POOL    frames + page table (hash) + pin + dirty; PG ~25% RAM, InnoDB ~70–80% RAM
EVICTION       clock (PG), young/old LRU (InnoDB), LRU-2 (SQL Server); goal: resist scans
HIT RATIO      99% → 90% = 10× disk reads
I/O            sequential ≫ random; buffered (PG) vs O_DIRECT (InnoDB); write() ≠ durable
WAL RULES      log before page | commit = commit record durable | records can redo
LSN            page_lsn ≥ record_lsn → skip (idempotent redo)
CHECKPOINT     flush dirty pages, set redo point; bounds recovery time and WAL size
TORN PAGES     PG full-page writes | InnoDB doublewrite; checksums detect, these repair
GROUP COMMIT   one fsync, many commits (durable); async commit = may lose last ~ms (not corrupt)
ENGINES        B-Tree: read-friendly, in-place | LSM: write-friendly, append + compact
AMPLIFICATION  read / write / space — pick two (RUM)
```

**The three sentences to say in any interview about this chapter:**

1. *Data lives in fixed-size pages, and a page is the unit of every read, write and cache entry.*
2. *Reads are made fast by the buffer pool, which keeps the working set in RAM and evicts cold pages
   without letting big scans flush the hot ones.*
3. *Writes are made safe by the WAL: log first, fsync the log at commit, write pages later, and
   use checkpoints to keep recovery short.*
