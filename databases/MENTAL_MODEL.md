# Database Mental Model: From A to Z

The other docs in this folder go deep on individual layers. This file is the **map**: it shows how every layer connects, the order to build them in, and exactly which file/class implements each piece. Read this first; use it as the index when the per-topic files get dense.

If you only ever read one page in this folder, read this one.

Hands-on tasks for every chapter (predict, build, break, measure) are in [LABS.md](./LABS.md).

---

## Table of Contents

**Start here (beginner path):** [The whole map in plain words](#start-here-the-whole-map-in-plain-words)
→ [Where the numbers come from](#where-the-numbers-come-from)
→ [Key terms](#key-terms-in-this-chapter)
→ [Key terms explained: why each one exists](#key-terms-explained-why-each-one-exists)

**Main chapters:**

1. [The One-Page Picture](#1-the-one-page-picture)
2. [The Four Universal Pipelines](#2-the-four-universal-pipelines) — each with *why this path* and *what if it changes*
3. [The Build Order: Phase 0 to Phase 17](#3-the-build-order-phase-0-to-phase-17)
4. [Component Responsibility Map](#4-component-responsibility-map)
5. [Cross-Cutting Concerns (the 4 Hard Problems)](#5-cross-cutting-concerns-the-4-hard-problems)
6. [Variant Decision Tree](#6-variant-decision-tree)
7. [End-to-End Trace of One Query](#7-end-to-end-trace-of-one-query)
8. [Linear Reading Order](#8-linear-reading-order)
9. [Common Pitfalls When Building Your Own](#9-common-pitfalls-when-building-your-own)
10. [Layer by Layer: Why Exactly This Way](#10-layer-by-layer-why-exactly-this-way)
11. [What If the Architecture Changes? The change matrix](#11-what-if-the-architecture-changes-the-change-matrix)
12. [Interview Questions and System Design Prompts](#12-interview-questions-and-system-design-prompts)

---

## Start here: the whole map in plain words

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
are illustrative but consistent with each other; every one of them is derived step by step in
[Where the numbers come from](#where-the-numbers-come-from) right after the walkthrough.

**Read — a customer logs in:** `SELECT name FROM users WHERE id = 42`

1. **Parse + plan** (doc 04). The text becomes a tree, names are checked against the catalogue,
   and the optimizer compares two routes: read all 125,000 pages (~1 GB) or walk the index (≈4
   pages). It picks the index. *Without care:* stale statistics make it believe the table has 10
   rows, it picks the full scan, and login takes ~0.5–1 s instead of ~20 µs.
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

6. **Log first** (doc 14). Append a ~100–150-byte record "txn 311 changed page 2,104 slot 3 to 'Ana
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
 SQL → plan → index (3 levels) → buffer pool  log record (~150 B) → page in RAM  replay WAL
 1 query     ~400 keys/page     hit 1 µs /    → fsync log (20 µs–2 ms)           from last
             4 page touches     miss 80 µs    → "OK"  → page to disk later       checkpoint
          → MVCC check → 'Ana'   (~20–200 µs)          (batched, async)          → same state
```

### Where the numbers come from

Every number above, derived. Units: 1 KB = 1,024 B for pages and RAM, and the "≈" results are
rounded so the arithmetic is easy to redo in your head. The goal is not the exact value but
knowing **which inputs drive each number**, so you can recompute it for your own system.

**A. Table size and page count**

```
row size          ≈ 100 B of user data (id 8 B + name + email + timestamps …)
table data        = 10,000,000 rows × 100 B = 1,000,000,000 B ≈ 1 GB

per-row overhead  = 24 B tuple header (xmin, xmax, flags, null bitmap) + 4 B slot pointer
bytes per row     ≈ 100 + 28 = 128 B on the page
usable per page   = 8,192 B − 24 B page header ≈ 8,168 B
rows per page     = 8,168 / 128 ≈ 64 rows              (if you ignore overhead: 8,192/100 ≈ 82)
table pages       = 10,000,000 / 64 ≈ 156,000 pages
                    (1 GB / 8 KB ≈ 122,000 pages if you ignore overhead)
```

We use **≈125,000 pages** as the round middle value. The takeaway: per-row overhead adds **~25%**
to a narrow table, which is why the Tuple card below calls it out.

**B. Buffer pool capacity**

```
frames            = 256 MB / 8 KB = 268,435,456 / 8,192 = 32,768 ≈ 32,000 frames
share of table    = 32,768 / 125,000 ≈ 26%  → "about a quarter"
share of table+index = 32,768 / (125,000 + 25,000 leaf pages, see C) ≈ 22%
```

So most of the table **cannot** be cached; whether a given lookup is fast depends on whether its
pages are in the hot ~quarter.

**C. B+Tree fanout and depth**

```
index entry       = 8 B key (bigint id) + 6 B TID + 2 B flags/length  = 16 B
                  + 4 B slot pointer                                    = 20 B per entry
keys per page     = 8,168 / 20 ≈ 408  → "~400"  (≈ 360 at a 90% fill factor)

leaf pages        = 10,000,000 / 400 = 25,000          (≈ 200 MB of leaves)
level above       = 25,000 / 400 ≈ 63 internal pages
level above       = 63 / 400 < 1   → 1 root page
depth             = root → internal → leaf = 3 levels
capacity check    = 400³ = 64,000,000 keys ≥ 10M → 3 levels is enough
                    a 4th level is needed only past 400⁴ = 25.6 billion keys
```

Why the top levels are "always in RAM": root + internal = **64 pages = 512 KB**, and *every*
lookup touches them, so the eviction policy never picks them. The 25,000 leaves (200 MB) compete
with the table for the 256 MB pool, so a given leaf may or may not be cached.

**D. Pages touched per lookup: "≈4"**

```
index: root (1) + internal (1) + leaf (1) = 3 pages
heap:  the page named by the TID          = 1 page
total                                     = 4 page touches
```

Compare with the full scan the optimizer rejected: **125,000 pages**. That ratio (≈31,000×) is
why stale statistics that flip this choice are so expensive.

**E. Hit vs miss latency**

```
hit  ≈ 0.1–1 µs   hash-table lookup page_id → frame, pin, take a shared latch
                  (all in RAM; ~100 ns per cache-missing memory access, a few of them)
miss ≈ 80 µs      pick a victim frame (write it first if dirty!), pread() 8 KB from NVMe
                  typical NVMe random 4–8 KB read at low queue depth: ~60–100 µs
ratio            ≈ 80–800× → "a miss costs about as much as a hundred hits"
cloud network disk (EBS/PD): ~0.5–1 ms per read → ~10× worse than local NVMe
```

**F. "20,000 logins/s would need 80,000 random reads/s"**

```
reads/s without a buffer pool = 20,000 queries/s × 4 pages/query = 80,000 random reads/s
latency per query             = 4 × 80 µs = 320 µs of pure I/O   (vs ~4 µs when all hits)
```

A good NVMe drive *can* do 80K IOPS, so throughput isn't the only issue: each query becomes
~80× slower, and a cloud disk capped at e.g. 16,000 IOPS simply couldn't keep up.

**G. The full-scan alternative: "~0.5–1 s"**

```
bytes to read   = 125,000 pages × 8 KB ≈ 1 GB
sequential read ≈ 2–3 GB/s on NVMe   → ~0.3–0.5 s of I/O
CPU per row     ≈ 20–50 ns to check visibility + evaluate id = 42
                  10,000,000 × ~50 ns ≈ 0.5 s
total           ≈ 0.5–1 s  (parallel workers can cut it, but it is still ~30,000× the index route)
```

**H. End-to-end read latency: "~20 µs hot, ~200 µs with two misses"**

```
parse + plan                ≈ 10–15 µs  (≈1–2 µs with a prepared statement / cached plan)
4 page touches (all hits)   ≈ 4 × ~1 µs = 4 µs
visibility check + project  ≈ 1 µs
                            ─────────────
all hot                     ≈ 15–20 µs  → "~20 µs"

two misses (leaf + heap)    ≈ 20 µs + 2 × 80 µs = 180 µs → "~200 µs"
```

Network round trip to the client (~50–500 µs in a datacenter) comes on top — for a hot query it
is often *larger* than the database's own work. §7 shows the same trace with ~250 µs total.

**I. WAL record size: "~100–150 B"**

```
WAL record header             ≈ 24 B  (length, xid, LSN link, record type, CRC)
block reference               ≈ 12–20 B (which relation + which page)
update payload                ≈ the new row version (~100 B) or just the changed bytes
                              ─────────────
typical                       ≈ 100–150 B
```

Exception: the **first** change to a page after a checkpoint also logs the whole 8 KB page (a
*full-page image*, protection against torn writes) — so that one record is ~8 KB, ~60× larger.

**J. fsync latency: "20–50 µs NVMe, 0.5–2 ms cloud disk"**

```
datacenter NVMe with power-loss protection: the drive can acknowledge once data is in its
  capacitor-backed cache → ~20–50 µs
cloud network disk: request travels over the network to replicated storage and back
  → ~0.5–2 ms
consumer SSD without PLP: an honest flush must reach flash → often 1–10 ms
```

**K. Group commit: "50 transactions share one fsync"**

```
one fsync per commit, 1 ms fsync      → max 1 / 0.001 s = 1,000 commits/s  (per log)
arrivals during one 1 ms fsync        = arrival rate × 1 ms
  e.g. 50,000 commits/s arriving      → 50 waiting when the next fsync starts
one fsync commits all 50              → 50 × 1,000 = 50,000 commits/s
each commit's latency                 ≈ up to 2 fsyncs (wait for the current one + its own)
```

The "50" isn't a configured value. It's *however many commits pile up during one fsync*, which is
why group commit helps most exactly when load is high.

**L. Write absorption: "30 more updates ride on one write"**

```
rows on the page           ≈ 64 (from A)
page stays dirty until     the next checkpoint (every ~5 min by default) or eviction
if 30 of those 64 users update their rows within that window:
  without write-back caching: 31 page writes × 8 KB = 248 KB
  with dirty-page caching:     1 page write  × 8 KB =   8 KB   → 31× fewer page writes
  WAL written either way:     31 records × ~150 B ≈ 4.6 KB (sequential)
```

**M. Recovery time after the crash**

```
WAL to replay   = WAL generated since the last checkpoint
                ≤ WAL rate × checkpoint interval
  e.g. 5 MB/s × 300 s (5 min) = 1.5 GB worst case
replay speed    ≈ 100–500 MB/s (mostly limited by random reads of the pages being fixed)
recovery time   ≈ 1.5 GB / (100–500 MB/s) ≈ 3–15 s
```

This is the knob behind the Checkpoint card below: halve the checkpoint interval → roughly halve
worst-case recovery time, at the cost of more checkpoint I/O.

The whole folder is these nine steps, seen up close — plus what changes when you want them faster
(docs 04, 15), on many machines (docs 12, 16, 19), or for a different workload (docs 07–13, 22).

### Key terms in this chapter

Read the table for the one-line version. For any term where you're asking "why does this even
exist?", jump to its card in [Key terms explained](#key-terms-explained-why-each-one-exists) below.

| Term | Plain meaning | Everyday analogy | Why we need it (the problem without it) | Problems it creates |
|---|---|---|---|---|
| Page | fixed-size block (4–16 KB), the unit the DB reads and writes | a box in the library basement; you always carry a whole box | Disks move whole blocks anyway; without a fixed unit there's no simple addressing, caching or free-space tracking | Read amplification for tiny rows, wasted space, torn writes, rows bigger than a page need overflow storage |
| Tuple / row | one record inside a page | one book in the box | The logical unit users insert, update and lock | Per-row overhead (~28 B in Postgres: 24 B header + 4 B slot pointer), alignment padding |
| TID (tuple ID) | address of a row: (page number, slot number) | "basement box 2,104, position 3" | Lets every index point at the row without copying it | Changes when a row moves to another page, so indexes must be updated |
| Buffer pool | the DB's own RAM cache of pages | the reading desk near the door | Disk is ~1,000× slower than RAM; without it every page touch is a disk read | Sizing is hard, it's cold after restart, and it's a contention point |
| Hit / miss | page found in RAM / had to read it from disk | book on the desk / walk to the basement | Hit ratio is the single best predictor of read latency | The ratio hides the real cost: going from 99% to 90% hits means 10× more disk reads |
| Dirty page | page changed in RAM but not yet written to disk | a book with pencil notes not yet copied into the master | Lets 1,000 updates to one page cost one disk write | Lost on crash (so WAL is required); must be flushed before eviction; checkpoint I/O spikes |
| WAL (write-ahead log) | append-only list of changes, written before the pages | the librarian's logbook | Fast durable commits (one sequential append) and a way to repair pages after a crash | Every change is written twice; the disk fills if archiving or a replica stalls; fsync latency sets the commit floor |
| fsync | "OS, put this on durable storage now, and tell me when" | posting the logbook page into the fire safe | `write()` only reaches the OS cache, which power loss erases | Slow (µs–ms), easy to get wrong, and some hardware lies about it |
| LSN | position of a record in the WAL; every page remembers the last LSN applied to it | logbook line number | Lets recovery tell whether a page already has a change, so replay is safe to repeat | Almost none; the cost is 8 bytes per page |
| Checkpoint | point where all older changes are known to be on disk; recovery starts here | "everything before line 5,000 is already copied into the books" | Without it, recovery replays the whole history and old WAL can never be deleted | I/O bursts, extra WAL (full-page images) after each one; a balance between recovery time and I/O |
| Transaction | a group of changes that all happen or none happen | a bank transfer: debit and credit together | Crashes and errors mid-change would leave half-done data that every app has to clean up | Long transactions hold locks and snapshots; apps must retry aborts |
| MVCC | keep old row versions so readers see a consistent snapshot without locking | readers get the printed edition while an editor works on the draft | Without it, a long report blocks every writer on the rows it reads (or vice versa) | Old versions pile up (bloat, VACUUM); snapshot isolation allows write skew; xid wraparound in Postgres |
| Lock vs latch | lock = protects a row for a whole transaction; latch = protects a memory structure for microseconds | reserving a meeting room for the afternoon vs holding a door for a second | Two different dangers (logical conflicts vs corrupting a structure) at two very different timescales | Locks → deadlocks and waiting; latches → contention on hot pages |
| Optimizer | picks the cheapest way to run a query, using statistics | a route planner using traffic data | Plans differ by 1,000×+ and the best plan changes as the data grows | Bad estimates → plan flips; unpredictable latency; planning cost for many-way joins |
| Index | extra structure that maps a key to where rows live | the catalogue | Without it, finding one row reads the whole table | Every write pays for every index; extra space and bloat |
| Replica | another machine holding a copy, fed from the WAL | a branch library receiving copies of the logbook | A single machine is a single point of failure and a read-throughput ceiling | Lag → stale reads; async failover can lose commits |
| Amplification | extra bytes read/written/stored per byte the user asked for | carrying a whole box to read one page | Explains *where the cost of a design goes*; every design trades one kind for another | None itself, but you can't minimize read, write and space amplification all at once (RUM) |

### Key terms explained: why each one exists

**Jump to a term:** [Page](#page) · [Tuple (row)](#tuple-row) · [TID (tuple ID)](#tid-tuple-id) · [Buffer pool](#buffer-pool) · [Hit and miss (hit ratio)](#hit-and-miss-hit-ratio) · [Dirty page](#dirty-page) · [WAL (write-ahead log)](#wal-write-ahead-log) · [fsync](#fsync) · [LSN (log sequence number)](#lsn-log-sequence-number) · [Checkpoint](#checkpoint) · [Transaction](#transaction) · [MVCC (multi-version concurrency control)](#mvcc-multi-version-concurrency-control) · [Lock vs latch](#lock-vs-latch) · [Optimizer](#optimizer) · [Index](#index) · [Replica](#replica) · [Amplification (read, write, space)](#amplification-read-write-space)

Each card has two parts:

1. **A beginner walkthrough built on a real-world analogy.** Numbered steps with small diagrams,
   using the `users` table and the numbers from the walkthrough above. It ends with a
   *Remember it like this* tree. Read this first and memorize the analogy; every detail maps onto it.
2. **Technical details.** The same concept, answering: *What goes wrong without it? How does it fix
   that? Why this size/shape? What new problems does it create? What do others do instead?*

#### Page

The best way to understand a **page** is as a **standard shipping container of fixed size** in
which the database stores its data.

**1. Picture shipping containers 🚢**

Before standard containers, loading a ship looked like this:

> "Here's a crate, here's a sack, here's a huge pipe — load it all by hand."

The ship, the crane and the truck each had to deal with every item differently. Containerization
said:

> "It doesn't matter what's inside. Everything goes into a standard box."

```text
┌──────────────────────┐
│      CONTAINER       │
│        8 KB          │
│                      │
│   data data data     │
│   data data          │
└──────────────────────┘
         = Page
```

A database does exactly the same with its files:

```text
users table file
   │
   ├── Page 0  → 8 KB   (bytes      0 –  8,191)
   ├── Page 1  → 8 KB   (bytes  8,192 – 16,383)
   ├── Page 2  → 8 KB   (bytes 16,384 – 24,575)
   └── …                 page N starts at byte N × 8,192
```

The database never says *"give me bytes 137 to 236 from the disk"*. It says *"give me page 17"*.
And finding page 17 needs no lookup table: it starts at byte 17 × 8,192 = 139,264.

**2. Why is this even needed?**

Because the hardware underneath doesn't work with arbitrary bytes either. Every layer has its own
unit of work:

```text
Database     →  page          8 KB (Postgres) / 16 KB (InnoDB)
   ↓
OS           →  memory page   4 KB
   ↓
Filesystem   →  block         4 KB
   ↓
SSD          →  flash page    4–16 KB   (and it erases in blocks of several MB)
```

If you need just **100 bytes**, the SSD still physically reads at least one whole flash page. So
the database reasons:

> "The hardware works in blocks anyway — so let's organize our data in blocks too."

**3. What a page looks like in practice**

Our `users` table from the walkthrough, with ~64 rows per 8 KB page (see calculation A):

```text
users                              Page 0                        Page 1
id | name | age                    ┌─────────────────────┐       ┌─────────────────────┐
---+------+----                    │ user 1              │       │ user 65             │
1  | Bob  | 25        ──stored──►  │ user 2              │       │ user 66             │
2  | Ann  | 31          as         │ …                   │       │ …                   │
3  | Joe  | 19                     │ user 64             │       │ user 128            │
…  (10 million rows)               └─────────────────────┘       └─────────────────────┘
                                   10,000,000 / 64 ≈ 156,000 pages
```

Inside, a page is a small organized box (the *slotted page*):

```text
Page 17 (8,192 bytes)
┌──────────────────────────────────────────────────────┐
│ header 24 B: page LSN, checksum, free-space pointers │
│ slot directory: [1][2][3][4] … grows →               │
│                                                      │
│                   free space                         │
│                                                      │
│             ← rows grow from the end: │row 4│row 3│row 2│row 1│
└──────────────────────────────────────────────────────┘
```

So a page is the **smallest standard chunk of data the storage engine works with**.

**4. The most important consequence: read amplification**

You need one row of **100 bytes**, and it lives in page 17. You can't say *"read exactly these 100
bytes"*. What happens is:

```text
Disk
 ↓
Page 17 = 8,192 bytes
 ↓
Database
 ↓
the 100 bytes you wanted

requested: 100 B
read:      8,192 B      → 82× more than needed
```

That's **read amplification**: you wanted a small thing and had to bring the whole container. Like
shipping one small TV — you still send a whole container.

Why is this acceptable? Because neighbours come for free. A scan of `users` gets 64 rows per trip,
and rows inserted together are often read together.

**5. Fragmentation (wasted space)**

Picture a half-empty container:

```text
┌──────────────────────┐
│ 📦 📦                │
│                      │
│        empty         │
│                      │
└──────────────────────┘
```

The same happens to a page:

```text
INSERT × 64   → page full
DELETE × 40   → 24 rows left, the page still takes 8 KB

Page 17 — 8 KB
┌──────────────────────┐
│ row  row  row        │
│                      │
│        EMPTY  (~60%) │
│                      │
└──────────────────────┘
```

Lots of space, little useful data. The fix: VACUUM lets new rows reuse that space; `VACUUM FULL`
or `pg_repack` rewrites the table into fewer, fuller pages (repacking the containers).

**6. Oversized cargo → TOAST**

A small TV fits in a container. A car doesn't — it needs special freight.

```text
Normal row (100 B)            →  fits in the page
User's 50 KB profile JSON     →  cannot fit in an 8 KB page
                                    ↓
                              compress it; if still > ~2 KB,
                              cut it into chunks, store them in a side table,
                              and keep an ~18 B pointer in the row
```

In Postgres this is **TOAST**. Standard page = standard container; TOAST = special logistics for
oversized cargo, with a claim ticket left in the container.

**7. Torn write**

Picture a crane lifting a container onto a ship, and the power goes out halfway: half the cargo is
on the ship, half is still on the dock.

A page is 8 KB, but most drives only guarantee that **4 KB** is written atomically. Power cut in
the middle:

```text
Page 17 after the crash
┌──────────────────────┐
│ NEW DATA             │  ← first 4 KB written
│ NEW DATA             │
│ OLD DATA             │  ← second 4 KB never written
│ OLD DATA             │
└──────────────────────┘
```

The page is half new, half old: a **torn write**. The page **checksum** detects it. Postgres
repairs it from a *full-page image* in the WAL; InnoDB from its *doublewrite buffer*.

**8. Why not make the page tiny, or huge?**

| Page size | Keys per index page | Index depth for 10M keys | Bytes read for one 100 B row | Typical use |
|---|---|---|---|---|
| 1 KB | ~50 | 5 levels (50⁴ = 6.25M < 10M) | 1 KB (10×) | too many I/Os and too much per-page bookkeeping |
| **8 KB** | ~400 | **3 levels** | 8 KB (82×) | OLTP — Postgres |
| 16 KB | ~800 | 3 levels | 16 KB (164×) | OLTP — InnoDB |
| 1 MB | ~50,000 | 2 levels | 1 MB (10,000×) | scans / analytics (column chunks) |

```text
Small page                              Large page
─────────────────────                   ─────────────────────
+ less waste                            + fewer pages, less overhead
+ less read amplification               + shallower trees
- more pages to track                   + great for sequential scans
- deeper trees, more I/O operations     - more read amplification
                                        - more waste for small rows
```

So page size is a **compromise**, tuned to the workload: small-ish for point lookups (OLTP),
large for scans (analytics).

**9. Remember it like this**

```text
Page = the database's standard container
 │
 ├── fixed size (8 KB / 16 KB)
 ├── DB reads and writes whole pages
 │
 ├── small request → reads a whole page       → read amplification
 ├── rows deleted  → holes in the page        → fragmentation
 ├── value too big → doesn't fit              → TOAST / overflow
 └── write cut halfway                        → torn write
```

A page doesn't exist because database developers liked cutting data into squares. It exists
because **the hardware and the OS already work in blocks**, so the storage engine builds its own
fixed unit on top. Once this clicks, the buffer pool (a cache *of pages*), indexes (point *to
pages*), sequential vs random I/O (reading pages *in order or not*) and the WAL (describes changes
*to pages*) all fall into place.

**Technical details — Page**

- *Without it:* the DB would read arbitrary byte ranges. But the hardware never works that way:
  SSDs read and program in pages of 4–16 KB, and the OS caches in 4 KB pages. Reading 100 bytes
  still costs a whole block. Without a fixed unit you also can't address data simply, cache it in
  interchangeable slots, or track free space.
- *How it fixes it:* everything is a page of the same size. Address = `page_id × page_size`, every
  buffer frame fits any page, and free space is tracked per page.
- *Why this size:* **4 KB** matches the OS page and the atomic write unit of most drives. **8 KB**
  (Postgres) and **16 KB** (InnoDB) are bigger so that B+Tree nodes hold more keys (fewer levels)
  and scans do fewer I/Os. Bigger is worse for point reads (read 16 KB to get a 100-byte row) and
  wastes cache on cold neighbours. Columnar formats use much larger units (MB-scale column chunks)
  because they only ever scan.
- *Problems it creates:* **torn writes** — an 8 KB page written as two 4 KB halves can be half
  new after power loss (fixed by full-page images in WAL or InnoDB's doublewrite buffer).
  **Overflow** — rows bigger than a page go out of line (Postgres TOAST). **Fragmentation** —
  deleted rows leave holes until compaction.
- *Alternatives:* variable-size blocks in LSM SSTables; immutable files on object storage.

#### Tuple (row)

A **tuple** (row) is best understood as **a parcel with a shipping label**.

**1. Picture a parcel 📦**

A parcel has two parts: the **contents** (what you actually wanted to send) and the **label**
(who sent it, when, and a stamp like "RETURNED" if it was cancelled). The post office never opens
the parcel to route it — it only reads the label.

**2. What a row looks like in a database**

A row is the same: a **header** (the label) plus the **data** (the contents).

```text
One Postgres row for user 42
┌──────────── header (24 B) ─────────────┬──────── data ────────────┐
│ xmin=107 │ xmax=0 │ flags │ null bitmap │ id=42 │ 'Ana' │ age=31   │
└────────────────────────────────────────┴──────────────────────────┘
  │          │                  │
  │          │                  └─ which columns are NULL (so NULLs take no space)
  │          └─ deleted by txn … (0 = not deleted)
  └─ created by transaction 107
```

**3. Why does it need a label?**

- `xmin` / `xmax` say **who created and who deleted this version** — that's what lets each
  transaction decide "can I see this row?" without locking (MVCC).
- The **null bitmap** records which columns are empty, so NULL values take no bytes.

Without the label, every reader would need to ask a central authority about every row.

**4. Problem: the label can be bigger than the contents**

Like mailing a single stamp in a large padded envelope:

```text
Table with two int columns (8 B of data per row)

  data          8 B
  header       24 B
  slot pointer  4 B
  ─────────────────
  total        36 B   → 78% of the space is overhead

10,000,000 rows:  80 MB of data  →  ~360 MB on disk
```

That's why time-series and columnar engines drop per-row headers entirely.

**5. Problem: padding**

Columns are aligned to 2/4/8-byte boundaries, like packing a box badly with gaps between items.
Order matters:

```text
(bool, bigint, bool, bigint)  →  1 + 7 pad + 8 + 1 + 7 pad + 8 = 32 B
(bigint, bigint, bool, bool)  →  8 + 8 + 1 + 1                = 18 B
```

Put wide columns first, then narrower ones — pack big items first, fill the gaps with small ones.

**6. Updates create new parcels**

In Postgres, `UPDATE` doesn't rewrite the parcel. It stamps the old one "replaced by txn 311"
(`xmax=311`) and sends a **new parcel** (`xmin=311`). Old parcels stay in the warehouse until
VACUUM clears them — see the MVCC card.

**7. Remember it like this**

```text
Row = parcel with a label
 │
 ├── label (header): who created it, who deleted it, which fields are empty
 ├── contents (data): your columns
 │
 ├── tiny rows          → label bigger than contents → overhead
 ├── badly ordered cols → gaps                       → padding
 └── update             → new parcel, old one kept   → versions → VACUUM
```

**Technical details — Tuple (row)**

- *Without it:* no unit to insert, lock, or version.
- *Why this shape:* header (visibility info xmin/xmax, null bitmap, flags) + data. The header is
  what makes MVCC and NULLs possible without touching other rows.
- *Problems it creates:* overhead. A Postgres row has a ~23-byte header + a 4-byte slot pointer,
  so a row of two integers takes ~36 bytes, not 8. At billions of narrow rows this dominates →
  column stores and time-series engines strip per-row headers.

#### TID (tuple ID)

A **TID** is best understood as **a street address with an apartment number**.

**1. Picture a city 🏢**

To send a letter to someone you don't write their full description ("tall, brown hair, likes
tea"). You write an address: **building 2,104, apartment 3**. Short, exact, and the mail carrier
goes straight there.

A TID is exactly that: **(page number, slot number)**.

```text
TID (2104, 3)
      │    └── slot 3 inside the page    = apartment
      └─────── page 2,104 of the table   = building
```

**2. Why is it needed?**

An index must say **where** the row lives. Two options:

```text
Option A: copy the whole row into every index
  index on id    → {42, 'Ana', 31, 'ana@…', …}
  index on email → {'ana@…', 42, 'Ana', 31, …}
  → every update rewrites every copy; indexes as big as the table

Option B: store the address (6 bytes in Postgres)
  index on id    → 42      → (2104, 3)
  index on email → 'ana@…' → (2104, 3)
  → small indexes; the row exists once
```

Databases choose B: like an address book storing addresses, not photocopies of people.

**3. Why an apartment number, not "the 3rd meter from the door"?**

Because the slot is an **indirection**. The slot directory at the top of the page says where
each row actually starts:

```text
Page 2104
slot directory: [1 → byte 8000] [2 → byte 7900] [3 → byte 7800]
                                                   │
                        page compaction moves row 3 to byte 7850
                                                   ▼
slot directory: [1 → byte 8000] [2 → byte 7900] [3 → byte 7850]

TID (2104, 3) is still correct — no index had to change.
```

The building manager can move tenants between rooms, as long as the apartment number on the
mailbox still leads to them.

**4. Problem: moving to another building**

If an `UPDATE` makes the row not fit on its page, the new version goes to **another page**, so it
gets a **new TID**. Every index now needs a new entry:

```text
UPDATE users SET bio = '…long text…' WHERE id = 42;
old version: (2104, 3)   →  new version: (5001, 7)

table with 6 indexes → 6 extra index inserts (+ their WAL)
```

Like moving to another building: every friend's address book must be updated.

**The Postgres escape hatch — HOT updates.** If the new version fits on the **same page** and no
indexed column changed, Postgres leaves a forwarding note on the old slot (a *HOT chain*) and
touches **no index**. Tip: `fillfactor = 90` leaves 10% of each page free so updates can stay "in
the same building".

**5. The alternative: address by name, not location**

InnoDB's secondary indexes store the **primary key** instead of a physical address, like
addressing mail to "Ana, customer #42" and having the front desk look up the room:

```text
Postgres:  email index → TID (2104, 3)   → 1 hop,  but row moves change indexes
InnoDB:    email index → PK 42 → PK tree → 2 tree walks, but row moves don't matter
```

**6. Remember it like this**

```text
TID = building + apartment number
 │
 ├── lets indexes point to the row instead of copying it
 ├── slot = apartment number → row can move inside the page freely
 │
 ├── row moves to another page → new TID → every index updated
 │       └── HOT update: stay on the same page → no index update
 └── alternative: point by primary key (InnoDB) → stable, but an extra lookup
```

**Technical details — TID (tuple ID)**

- *Without it:* each index would store a copy of the whole row (every update rewrites every copy)
  or the primary key (every secondary lookup becomes a second tree walk — which is exactly what
  InnoDB does, by choice).
- *Why this size:* 6 bytes in Postgres (4-byte page number + 2-byte slot) — small enough that
  indexes stay compact.
- *Why the slot (not a byte offset):* the slot is an indirection *inside* the page, so the row can
  be moved or compacted within the page without changing its TID.
- *Problems it creates:* if the row moves to *another* page (a Postgres update that doesn't fit),
  its TID changes and every index gets a new entry — the source of index write amplification
  (HOT updates avoid it when the new version fits on the same page and no indexed column changed).

#### Buffer pool

The **buffer pool** is best understood as **your kitchen counter, while the disk is the pantry in
the basement**.

**1. Picture cooking 🍳**

Your counter holds ~30 items. The basement pantry holds 1,000. Grabbing something from the
counter takes 1 second; a trip to the basement takes 80 seconds. You'd never cook a meal by
walking downstairs for every pinch of salt — you bring what you need up and keep it on the counter.

```text
Kitchen counter (small, fast)          Basement pantry (huge, slow)
┌──────────────────────────┐           ┌───────────────────────────────┐
│ salt  oil  onions  eggs  │  ◄──────  │ 1,000 items                   │
│ (what you're using now)  │   80 s    │                               │
└──────────────────────────┘           └───────────────────────────────┘
      grab: 1 s

Buffer pool: 256 MB = 32,768 frames    Disk: ~150,000 pages (table + index)
      hit: ~1 µs                             miss: ~80 µs (NVMe)
```

**2. Why is it needed?**

Without it, every page access is a disk read. A login touches 4 pages; 20,000 logins/s would be
80,000 random disk reads per second, and each login would spend 320 µs waiting on I/O instead of
~4 µs.

**3. How it works**

```text
Buffer pool (32,768 frames)
┌─────────┬─────────┬─────────┬─────────┬──────
│ page 17 │ p. 2104 │ root    │ (free)  │ …
│ pin = 1 │ pin = 0 │ pin = 3 │         │
│ clean   │ DIRTY   │ clean   │         │
│ used ✓  │ used ✗  │ used ✓  │         │
└─────────┴─────────┴─────────┴─────────┴──────
page table (hash map): {17 → frame 0, 2104 → frame 1, root → frame 2}

fetch_page(2104):
  in page table?  yes → pin it, return it                     (hit,  ~1 µs)
                  no  → find a victim frame, read from disk   (miss, ~80 µs)
```

- **pin count** = "someone is holding this item right now, don't put it away".
- **dirty flag** = "I changed this item; the pantry copy is out of date".
- **used bit** = "touched recently" (for eviction).

**4. The counter is full — what goes back down? (eviction)**

The **clock** algorithm: every item has a "used recently" sticker. A hand walks around the
counter: if an item has a sticker, remove the sticker and move on; the first item without a
sticker (and not pinned) goes back to the pantry. If it's dirty, write it first.

```text
         ┌─► [salt ✓] → remove ✓, skip
 hand ───┤   [oil  ✓] → remove ✓, skip
         └─► [eggs  ] → no sticker, not pinned → evict
```

Why not perfect LRU? LRU must reorder a shared list on **every** hit (a lock on every read).
Clock just sets a bit — much cheaper at 100,000 reads/s.

**5. Why the database manages it, not the OS (the landlord)**

- **Scan pollution.** A report that reads all 125,000 pages once would push every hot item off
  the counter. The DB knows it's a scan and gives it a small separate tray (Postgres uses a
  256 KB ring buffer for large sequential scans).
- **Write order.** A dirty page may go back to the pantry only after its WAL record is safe
  (§2.2). The OS doesn't know that rule.
- **Pins.** The OS could evict a page in the middle of an index split.

**6. Problem: cold start**

After you move house, the counter is empty. The first hour of cooking is all basement trips.
After a DB restart or failover, latency is high until the hot pages are loaded again. Tools like
`pg_prewarm` reload them on purpose.

**7. How big should the counter be?**

Big enough for what you **actually cook with** (the hot working set), not the whole pantry.
Beyond that, extra space gives no benefit.

```text
Postgres  shared_buffers ≈ 25% of RAM   (the OS cache is a second counter: double buffering)
InnoDB    buffer pool    ≈ 70–80% of RAM (bypasses the OS cache with O_DIRECT)
```

**8. Remember it like this**

```text
Buffer pool = kitchen counter; disk = basement pantry
 │
 ├── hit  = item on the counter (~1 µs)
 ├── miss = trip downstairs (~80 µs)
 │
 ├── counter full → clock sweep evicts an unused, unpinned item
 ├── dirty item   → must be written back before its space is reused
 ├── big scan     → separate small tray, so it doesn't evict everything
 ├── restart      → empty counter → slow until warm
 └── size it for the working set, not for the whole database
```

**Technical details — Buffer pool**

- *Without it:* every page access is a disk read (~80 µs NVMe, ~1 ms cloud disk) instead of
  ~100 ns in RAM. A lookup touching 4 pages at 20,000 queries/s would need 80,000 random reads/s.
- *Why the DB owns it (not just the OS):* it must control eviction (one big scan shouldn't evict
  the hot set), write order (WAL before page), and pinning (don't evict a page someone is reading).
- *Why this size:* as large as the **hot working set**, not the whole database. Postgres
  `shared_buffers` ≈ 25% of RAM because it also relies on the OS cache (double buffering); InnoDB
  ≈ 70–80% because it bypasses the OS cache with `O_DIRECT`.
- *Problems it creates:* **cold start** after restart (latency spike until it warms up; tools like
  `pg_prewarm`), a shared structure that needs careful latching, and a "cliff" when the working
  set grows past it.
- *Alternatives:* mmap (gives up control), fully in-memory engines (no pool at all).

#### Hit and miss (hit ratio)

The **hit ratio** is best understood as **train punctuality on your daily commute**.

**1. Picture a commute 🚆**

You commute 250 days a year. The railway boasts "99% on time!". Next year it's "90% on time". A
9-point drop sounds small. But count the days you're **late**:

```text
99% on time  →  1% late  → 2.5 late days a year
90% on time  → 10% late  →  25 late days a year   ← 10× worse
```

You don't feel the on-time days. You feel the late ones.

**2. In the database**

Every page request is a trip. A **hit** means the page was already in the buffer pool (the train
was on time, ~1 µs). A **miss** means a disk read (the train was late, ~80 µs, 80× slower).

**3. The math**

```text
average page latency = hit% × 1 µs + miss% × 80 µs

99% hits:  0.99 × 1 + 0.01 × 80 = 0.99 + 0.8  ≈  1.8 µs
90% hits:  0.90 × 1 + 0.10 × 80 = 0.90 + 8.0  ≈  8.9 µs   → 5× slower on average
disk reads per second:                                     → 10× more
```

That's why OLTP systems aim for **> 99%**, and why a "small" drop is a big incident.

**4. Problem: the average hides the painful part**

A 99% average can hide one table that is **always** cold, like a railway that's punctual
everywhere except on your line. If your checkout query hits that table, the average tells you
nothing. Measure per table and per index (`pg_statio_user_tables`, `pg_statio_user_indexes`).

**5. How to improve it**

```text
more RAM for the pool          → bigger counter
smaller working set            → indexes instead of scans; archive or partition old data
protect it from big scans      → run reports on a replica or a columnar copy
```

**6. Remember it like this**

```text
Hit ratio = train punctuality
 │
 ├── hit  = on time  (~1 µs)
 ├── miss = late     (~80 µs)
 │
 ├── what hurts is the miss rate: 1% → 10% = 10× more disk reads
 ├── averages hide a cold table → measure per table
 └── fix: bigger pool, smaller working set, keep scans away
```

**Technical details — Hit and miss (hit ratio)**

- *Why track it:* it's the biggest single driver of read latency.
- *Why the percentage misleads:* what matters is the *miss* rate. 99% → 90% hits sounds like a 9%
  change, but misses go from 1% to 10% → **10× more disk reads**. OLTP systems aim for >99%.
- *Problems it creates:* a high average can hide a cold table that one important query always misses.

#### Dirty page

A **dirty page** is best understood as **a shopping list on the fridge**.

**1. Picture running out of things at home 🛒**

Monday you run out of milk, Tuesday eggs, Wednesday coffee… You could drive to the store every
time: 20 trips a week. Instead you write each item on the list on the fridge and go **once** on
Saturday, buying all 20 items in one trip.

**2. In the database**

`UPDATE` changes the page **in RAM** and marks it *dirty* ("the disk copy is out of date"). The
page is written to disk later, in one go.

```text
RAM (buffer pool)                    Disk
┌───────────────────────┐            ┌───────────────────────┐
│ page 2104  DIRTY      │            │ page 2104  (old)      │
│ id=42 name='Ana B.'   │   later    │ id=42 name='Ana'      │
│ id=43 …  (changed)    │  ───────►  │ id=43 …               │
└───────────────────────┘            └───────────────────────┘
```

**3. Why: write absorption**

If 30 users on the same page update their rows before the page is flushed (calculation L):

```text
write every change immediately:  31 × 8 KB = 248 KB of random writes
dirty page, flush once:           1 × 8 KB =   8 KB
```

One trip instead of 31.

**4. Problem 1: the list can be lost**

If the fridge list burns (power cut), you forget what you needed. Dirty pages vanish with RAM.
That's why the database **also** keeps a durable note: every change is first written to the WAL.
It's like texting each item to yourself. If the list is lost, you rebuild it from your messages.

**5. Problem 2: forced trips at a bad moment**

If the list gets so long that the fridge is full, you have to go shopping **right now**, in the
middle of cooking. When the buffer pool is full of dirty pages, loading a new page first requires
writing a dirty one out, and the query waits. The **background writer** is a helper who does
small shopping trips during the week, so there's always a clean frame free.

**6. Problem 3: the Saturday rush**

A **checkpoint** must flush *all* dirty pages, which is one huge shopping trip causing an I/O
spike. Databases spread it over the whole interval (`checkpoint_completion_target = 0.9`), like
shopping a little each day instead of everything at once.

**7. Remember it like this**

```text
Dirty page = item on the shopping list, not yet bought
 │
 ├── change in RAM now, write to disk later
 ├── many changes → one write              → write absorption
 │
 ├── RAM lost on crash                     → WAL is required
 ├── pool full of dirty pages              → queries wait → background writer
 └── checkpoint flushes all of them        → spread it out to avoid spikes
```

**Technical details — Dirty page**

- *Without it (write-through):* every update writes its page to disk immediately → random writes on
  the commit path, and a hot page updated 1,000×/s is written 1,000×/s.
- *How it fixes it:* change in RAM, mark dirty, write once later → **write absorption**.
- *Problems it creates:* dirty pages die with a crash → you *need* the WAL. A dirty page must be
  written before its frame can be reused, so a pool full of dirty pages makes reads wait on writes
  → background writers flush ahead of time. Checkpoints must flush them all → I/O bursts.

#### WAL (write-ahead log)

The **WAL** is best understood as **a bank teller's journal kept in a fire safe**.

**1. Picture a bank before computers 🏦**

The bank has 10,000 customer passbooks in filing cabinets spread across the building. A customer
deposits $100. Two ways to do it:

```text
Option A: walk to the right cabinet, find the passbook, update it, walk back
          → for every single transaction, all day

Option B: write one line in the journal on your desk
          "#5001  account 42  +$100"
          say "done", and update the passbooks in the evening in one pass
```

**2. Why option B wins**

The journal is **right on the desk**, and you only ever add a line at the end (sequential). The
passbooks are **all over the building** (random). In a database:

```text
WAL record:  ~150 B appended to the end of one file   (sequential, cheap)
data pages:  8 KB each, scattered across the disk     (random, expensive)
```

**3. Durability: the journal lives in the fire safe**

At the end of each transaction the journal page is locked in the safe (fsync). If the building
burns at 3 pm, the passbooks may be ashes or out of date, but the journal survives. Next morning:

```text
time →
 09:00  checkpoint: all passbooks correct up to journal line #5000
 09:01  #5001  acct 42  +100
 09:02  #5002  acct 7   −50
 …
 15:00  🔥 fire (crash)

 recovery: take the passbooks as of 09:00 → replay #5001 … #N → exact state at 15:00
```

**4. The golden rule: journal first, passbook second**

Imagine the teller updates the passbook first and the fire starts before the journal line is
written. Now there's a passbook change nobody can explain. Was it part of a transfer that never
finished? You can't tell. Hence: **the WAL record must be durable before the page it describes
may be written**, and before the customer hears "done".

**5. Cost 1: everything is written twice**

Once in the journal, once in the passbook. Write-heavy tables can produce more WAL than data.

**6. Cost 2: a journal nobody archives fills the safe**

Old journal pages are kept until they're no longer needed: until the next checkpoint, until
they're archived, and until every replica has received them. If an archiver breaks or a replica
disconnects while its **replication slot** still holds the WAL, the safe fills up. The disk runs
out, and the database stops.

**7. Cost 3: "done" can't be faster than locking the safe**

Commit latency ≥ fsync latency of the WAL device (20 µs – 2 ms). **Group commit** = when 50
customers are waiting, put all 50 lines in the safe in one trip.

**8. Bonus: the journal is useful for more than fires**

```text
same WAL stream ──┬─► crash recovery           (replay after a fire)
                  ├─► replicas                  (branches get photocopies of the journal)
                  ├─► point-in-time recovery    (keep all old journals → rebuild any moment)
                  └─► change-data-capture       (Debezium / logical decoding → other systems)
```

**9. Remember it like this**

```text
WAL = teller's journal in the fire safe
 │
 ├── append one small line (sequential) instead of updating pages (random)
 ├── fsync the journal at commit → durable
 ├── rule: journal line before the page, and before "OK"
 │
 ├── written twice                    → extra I/O
 ├── unarchived / stuck slot          → disk full → DB stops
 ├── commit ≥ fsync latency           → group commit
 └── same log → recovery, replicas, PITR, CDC
```

**Technical details — WAL (write-ahead log)**

- *Without it:* either force every touched page at commit (slow, and a torn page still corrupts
  data) or accept losing data on crash.
- *How it fixes it:* one small sequential append per change, one fsync per commit (shared by group
  commit). Pages become a cache that can always be rebuilt from the log.
- *Why this shape:* append-only, because sequential writes are the fastest thing any disk does.
  Split into **segments** (16 MB by default in Postgres) so old parts can be archived or deleted
  as whole files.
- *Problems it creates:* every change is written twice (log + page). WAL volume can exceed the data
  size on write-heavy tables. If an archiver or a replication slot stops consuming, WAL piles up
  until **the disk fills and the DB stops**. The commit latency can never be lower than the log
  device's fsync latency.
- *Bonus:* the same stream powers replicas, point-in-time recovery and change-data-capture.

#### fsync

**fsync** is best understood as **the difference between dropping a letter in a mailbox and
sending it by registered mail with a signed receipt**.

**1. Picture sending an important contract ✉️**

```text
Mailbox:          drop it in → you walk away → feels done
                  but it sits in the box until pickup; if the box is destroyed tonight,
                  the letter is gone — and nobody tells you

Registered mail:  wait at the counter until you get a signed receipt
                  slow, costs more — but now you have proof it arrived
```

**2. In the computer**

```text
app ── write() ──► OS page cache (RAM)          ← write() returns here: "done"
                        │                          (NOT safe: power cut = gone)
                        │ kernel flushes "later", often after ~5–30 s
                        ▼
app ── fsync() ──► disk controller ──► flash     ← fsync() returns here: SAFE
```

`write()` = the mailbox. `fsync()` = the signed receipt.

**3. Why only for the commit record?**

A signed receipt costs **20–50 µs** on a datacenter NVMe and **0.5–2 ms** on a cloud disk. You
wouldn't send every postcard by registered mail. The database fsyncs only what makes a commit
durable: the WAL up to the commit record. Data pages are written lazily and are covered by
checkpoints.

**4. Problem: couriers who lie**

Some consumer SSDs have a volatile cache. They report "delivered" while the letter is still in
the van. Power cut → gone, even though fsync said OK. Enterprise drives have **power-loss
protection** (capacitors): the van has a backup generator that finishes the delivery.

**5. Problem: the courier who throws the letter away ("fsyncgate", 2018)**

```text
you:     fsync()        courier: "delivery FAILED"
you:     fsync() again  courier: "delivered!"   ← because he threw the letter away
```

On Linux, after a failed fsync the kernel could drop the dirty data and mark it clean, so a
*retry* reported success. Postgres learned this in 2018, and now **crashes and recovers from the
WAL** instead of retrying.

**6. Remember it like this**

```text
fsync = registered mail with a signed receipt
 │
 ├── write()  → in the OS cache (mailbox)  → lost on power cut
 ├── fsync()  → on durable storage         → safe
 │
 ├── slow (µs–ms) → use only for the WAL at commit
 ├── cheap SSDs may lie → use drives with power-loss protection
 └── a failed fsync can't be retried safely → crash + WAL recovery
```

**Technical details — fsync**

- *Without it:* `write()` returns once bytes are in the OS page cache. A power cut erases that
  cache — the "committed" transaction is gone.
- *Why only at commit, only on the log:* it costs ~20–50 µs on datacenter NVMe with power-loss
  protection and ~0.5–2 ms on cloud network disks, so it's done as rarely as correctness allows.
- *Problems it creates:* latency floor for commits. Consumer SSDs without power-loss protection
  may acknowledge before data is really safe. Error handling is subtle: in 2018 ("fsyncgate")
  Postgres learned that after a failed fsync, Linux could drop the dirty pages and a *retried*
  fsync would report success. Postgres now crashes and recovers from WAL instead of retrying.

#### LSN (log sequence number)

An **LSN** is best understood as **numbered cheques plus a "processed up to" stamp in the ledger**.

**1. Picture an accountant updating a ledger 📒**

Cheques are numbered 1, 2, 3, … Each ledger page has a stamp in the corner:
*"cheques applied up to no. 4,800"*. The accountant picks up a pile of cheques:

```text
cheque #4,750  → stamp says 4,800 → already applied → SKIP
cheque #4,900  → stamp says 4,800 → not applied yet → APPLY, update stamp to 4,900
```

Even if the same pile is processed twice, no cheque is ever counted twice.

**2. In the database**

- Every WAL record has an **LSN**: its byte position in the log (looks like `0/16B3748`).
- Every page header stores **page_lsn**: the LSN of the last change applied to that page.

```text
Page 2104 header: page_lsn = 4,800

WAL records during recovery:
  LSN 4,750  "set slot 3 name='Ana'"     → 4,750 ≤ 4,800 → skip (already on the page)
  LSN 4,900  "set slot 3 name='Ana B.'"  → 4,900 > 4,800 → apply, page_lsn = 4,900
```

**3. Why is it needed?**

Without it, recovery can't know whether a page already contains a change. Replaying blindly could
apply it twice:

```text
"insert row 42"     applied twice → duplicate row
"balance += 100"    applied twice → +$200 instead of +$100
```

With LSNs, replay is **idempotent**: repeating it changes nothing. That's also why a crash
*during* recovery is harmless. Just start recovery again.

**4. Other jobs the LSN does**

```text
replication lag   = primary's current LSN − replica's replayed LSN   (bytes behind)
the WAL rule      = a page may be written to disk only if page_lsn ≤ WAL flushed LSN
point-in-time     = "restore up to LSN X" (or the timestamp that maps to it)
```

**5. Why 64 bits?**

It's a byte offset into the log. Even at 1 GB/s of WAL, 2⁶⁴ bytes lasts about **585 years**, so it
never wraps around in practice. The cost is 8 bytes per page.

**6. Remember it like this**

```text
LSN = cheque number;  page_lsn = "processed up to" stamp
 │
 ├── record LSN ≤ page_lsn → skip;  > page_lsn → apply
 ├── makes replay idempotent → recovery can be repeated safely
 ├── measures replica lag in bytes
 └── enforces "WAL before page"
```

**Technical details — LSN (log sequence number)**

- *Without it:* after a crash you can't tell whether a page on disk already contains a change, so
  re-applying could apply it twice (e.g. insert the same row twice).
- *How it fixes it:* each WAL record has a position; each page stores the LSN of the last change
  applied to it. Redo applies a record only if `page_lsn < record_lsn` → replay is **idempotent**.
- *Why this size:* 64 bits (a byte offset into the log) — it never wraps in practice.
- *Also used for:* measuring replication lag (primary LSN − replica LSN = bytes behind) and
  enforcing the WAL rule (a page can be flushed only if `page_lsn ≤ flushed_lsn`).

#### Checkpoint

A **checkpoint** is best understood as **saving your progress in a video game**.

**1. Picture a long video game 🎮**

No saves: the game crashes on level 9 and you start again from level 1. Autosave every 5
minutes: a crash costs you at most 5 minutes of play.

**2. In the database**

A checkpoint = write all dirty pages to disk, then record *"everything up to LSN X is on disk"*.
After a crash, recovery starts replaying from X, not from the beginning of time.

```text
WAL  ─────────────────────────────────────────────────────────────►
      ▲ checkpoint           ▲ checkpoint                    ▲ crash
      LSN 1,000              LSN X = 5,000                    LSN 6,200
                             └── replay only 5,000 → 6,200 ───┘
      └── WAL before 5,000 can be recycled or archived
```

**3. Why is it needed?**

- **Bounds recovery time**: you only replay since the last save.
- **Lets old WAL be deleted**: like an accountant closing the books for the month, after which
  that month's receipts can go to the archive.

**4. The trade-off: saving pauses the game**

Each checkpoint writes many dirty pages (an I/O burst). Also, the **first** change to each page
after a checkpoint logs a full 8 KB page image (torn-write protection), so WAL volume jumps right
after every checkpoint.

With 5 MB/s of WAL and a replay speed of 100–500 MB/s (calculation M):

| Checkpoint every | WAL to replay (worst case) | Recovery time | Checkpoint I/O + extra WAL |
|---|---|---|---|
| 1 min | 300 MB | ~1–3 s | high |
| **5 min** (Postgres default) | 1.5 GB | ~3–15 s | moderate |
| 30 min | 9 GB | ~20–90 s | low |

**5. Autosave in the background**

Postgres doesn't flush everything at once. It spreads the writes over ~90% of the interval
(`checkpoint_completion_target = 0.9`), like a game saving quietly in the background instead of
freezing the screen.

**6. Remember it like this**

```text
Checkpoint = save game
 │
 ├── flush dirty pages, record "safe up to LSN X"
 ├── recovery replays only from X
 ├── WAL older than X can be recycled
 │
 ├── save often  → fast recovery, more I/O, more WAL
 ├── save rarely → less I/O, slow recovery, more WAL on disk
 └── spread the writes to avoid I/O spikes
```

**Technical details — Checkpoint**

- *Without it:* recovery would replay the WAL from the very beginning, and no WAL could ever be
  deleted.
- *How it fixes it:* periodically flush all dirty pages, then record "everything before LSN X is
  on disk". Recovery starts at X; older WAL can be recycled.
- *Why this interval:* Postgres defaults to every **5 minutes** (`checkpoint_timeout`) or when
  **1 GB** of WAL accumulates (`max_wal_size`). Shorter → faster recovery but more flush I/O and
  more full-page images in WAL. Longer → less I/O, but recovery replays more and needs more WAL
  disk. Writes are spread across the interval (`checkpoint_completion_target`) to avoid a spike.
- *Problems it creates:* I/O bursts and WAL volume spikes right after each checkpoint.

#### Transaction

A **transaction** is best understood as **buying a house through escrow**.

**1. Picture buying a house 🏠**

You pay $300,000; the seller hands over the keys. Two dangers:

```text
you paid, but got no keys      ✗
you got keys, seller got no money  ✗
```

Escrow fixes it: an agent holds both the money and the deed. At closing **both move at once**, or
the deal is cancelled and everything goes back.

**2. In the database**

```sql
BEGIN;
UPDATE accounts SET balance = balance - 100 WHERE id = 1;   -- debit Ana
UPDATE accounts SET balance = balance + 100 WHERE id = 2;   -- credit Bob
COMMIT;
```

```text
without a transaction:  debit ✓ → 💥 crash → credit never happens → $100 vanished
with a transaction:     debit ✓ → 💥 crash → recovery rolls back the debit → nothing happened
```

**3. ACID, mapped onto escrow**

| Letter | Escrow | Database mechanism |
|---|---|---|
| **A**tomicity | both sides move, or neither | WAL + undo / invisible versions |
| **C**onsistency | the deal can't close if it breaks the rules (no negative balance) | constraints, checks, foreign keys |
| **I**solation | other buyers don't see a half-closed deal | MVCC + locks |
| **D**urability | the signed deed is filed at the registry | WAL fsync at commit |

**4. Problem 1: a long escrow blocks others**

While escrow is open, the house is held for you. A transaction left open for an hour (e.g. an app
that ran `BEGIN` and waits for user input, shown as *idle in transaction*) holds its row locks and
its old snapshot. Others wait, and VACUUM can't clean up anything newer than that snapshot.

**5. Problem 2: deals get cancelled → retry**

Under stricter isolation (`SERIALIZABLE`), or after a deadlock, the database cancels one
transaction. The app **must retry**, just as a buyer restarts a deal that fell through. Code
without retry logic will surface random errors under load.

**6. Problem 3: paperwork per deal**

Every commit costs an fsync. A million single-row transactions = a million fsyncs; the same
million rows in batches of 1,000 = 1,000 fsyncs.

**7. Remember it like this**

```text
Transaction = escrow
 │
 ├── all changes happen together, or none do
 ├── ACID: all-or-nothing, rules hold, others don't see half-done work, survives crash
 │
 ├── long transaction   → holds locks + old snapshot → blocks others and VACUUM
 ├── aborts happen      → the app must retry
 └── one fsync per commit → batch small writes
```

**Technical details — Transaction**

- *Without it:* a crash or error between "debit A" and "credit B" leaves money missing, and every
  application has to write its own cleanup logic.
- *How it fixes it:* all-or-nothing (atomicity via WAL/undo), isolation from others (MVCC/locks),
  durability at commit (fsync).
- *Problems it creates:* long transactions hold locks (blocking others) and old snapshots
  (blocking cleanup). Under stricter isolation some transactions abort and **must be retried by
  the app**. Each commit pays an fsync, so millions of tiny transactions are slower than batches.

#### MVCC (multi-version concurrency control)

**MVCC** is best understood as **newspaper editions** (or Wikipedia's page history).

**1. Picture a newspaper 📰**

Readers read **this morning's edition** while journalists write **tomorrow's**. Nobody waits for
anybody: readers don't block writers, writers don't block readers.

Compare with a single shared **whiteboard** (plain locking): while someone erases and rewrites
it, readers must wait; while someone copies it down, the writer must wait.

**2. In the database**

`UPDATE` doesn't overwrite the row. It creates a **new version** and marks the old one as replaced:

```text
row id = 42
  v1: name='Ana'      xmin=107   xmax=311   ← visible to snapshots taken before 311 committed
  v2: name='Ana B.'   xmin=311   xmax=0     ← visible to snapshots taken after 311 committed
```

**3. Snapshots: which edition do I get?**

Each transaction gets a **snapshot**: "which transactions had committed when I started".

```text
10:00  report starts (snapshot: 311 not committed)   → sees 'Ana' for the whole report
10:01  txn 311 renames to 'Ana B.' and commits
10:02  new login (snapshot: 311 committed)           → sees 'Ana B.'
       the report, still running, still sees 'Ana' → consistent, and no one waited
```

(A snapshot lasts for one statement under the default READ COMMITTED, and for the whole
transaction under REPEATABLE READ / SERIALIZABLE. A long report query is one statement, so it
keeps one snapshot either way.)

**4. Problem 1: old editions pile up (bloat)**

Every update leaves an old version behind. **VACUUM** is the recycling truck that removes
versions no snapshot can see anymore. If it can't keep up, a table with 10 GB of live data can
take 80 GB on disk.

**5. Problem 2: one slow reader keeps everything**

The library can't recycle any edition someone might still be reading. One transaction open for 6
hours means **6 hours of old versions of every updated row** stay on disk, for every table.

**6. Problem 3: write skew**

```text
Rule: at least one doctor must be on call. Alice and Bob are both on call.
Alice's txn: reads "2 on call" → sets Alice off call
Bob's txn:   reads "2 on call" → sets Bob off call     (both read the old edition)
both commit → 0 doctors on call ✗
```

Each saw a consistent edition, but the combination breaks the rule. Fix: `SERIALIZABLE`
isolation, or lock the rows you read (`SELECT … FOR UPDATE`).

**7. Problem 4: the edition counter wraps around (Postgres)**

Transaction IDs are 32-bit, compared in a circle, so only ~2 billion are "in the past" at any
time. Old rows must be **frozen** by VACUUM ("so old it's visible to everyone") before the counter
comes around. Otherwise Postgres stops accepting writes to protect the data.

**8. Remember it like this**

```text
MVCC = newspaper editions
 │
 ├── writers create new versions; readers read their snapshot's edition
 ├── readers never block writers, writers never block readers
 │
 ├── old versions pile up          → VACUUM / purge
 ├── long transaction              → nothing can be cleaned
 ├── snapshot isolation            → write skew → SERIALIZABLE or FOR UPDATE
 └── 32-bit xid (Postgres)         → freeze old rows before wraparound
```

**Technical details — MVCC (multi-version concurrency control)**

- *Without it (plain locking):* a reader must lock rows so a writer can't change them mid-read. A
  10-minute report then blocks every update to the rows it touches.
- *How it fixes it:* writers create new versions; each reader sees the versions that were committed
  when its snapshot started. Readers and writers don't block each other.
- *Problems it creates:* **bloat** — old versions stay until VACUUM/purge removes them, and one
  long-open transaction prevents that for everyone. **Write skew** under snapshot isolation.
  **Transaction ID wraparound** in Postgres: xids are 32-bit (~2 billion usable), so old rows
  must be "frozen" by VACUUM or the DB stops accepting writes to protect itself.
- *Alternatives:* 2PL (readers lock), OCC (validate at commit), SSI (MVCC + conflict tracking).

#### Lock vs latch

**Locks and latches** are best understood as **booking a meeting room vs holding a door open**.

**1. Picture an office 🚪**

```text
Meeting room booking (LOCK)              Holding a door (LATCH)
─────────────────────────                ─────────────────────────
on the calendar, visible to everyone     nobody records it
held for the whole meeting (hours)       held for a second
can conflict: two people want            only a problem when a crowd
the same room                            needs the same door at once
```

**2. In the database**

| | Lock | Latch |
|---|---|---|
| Protects | logical data: "row 42", "table users" | a memory structure: the bytes of page 2,104, a hash bucket |
| Held by | a **transaction** | a **thread** |
| Duration | ms → hours (until commit) | ns → µs |
| Modes | many (shared, exclusive, intent, …) | read / write |
| Deadlocks | possible → detected, a victim is aborted | avoided by always taking them in a fixed order |
| Where you see it | `pg_locks`, lock waits | CPU profiles, `LWLock` wait events |

**3. Why two different mechanisms?**

- Using the room-booking system to hold a door would be absurdly slow. A lock manager does
  hash-table work and bookkeeping on every acquire, and the database takes latches millions of
  times per second.
- Holding a door for a 2-hour meeting would block the whole building. A latch held for a whole
  transaction would freeze every other thread touching that page.

**4. Lock problem: deadlock**

```text
Alice books Room A, then wants Room B.
Bob   books Room B, then wants Room A.   → both wait forever

txn 1: UPDATE row 1 … then UPDATE row 2
txn 2: UPDATE row 2 … then UPDATE row 1  → deadlock
```

The database's detector notices (Postgres checks after `deadlock_timeout` = 1 s) and aborts one
of them. The fix in code: always update rows in the same order (e.g. by id).

**5. Latch problem: the crowded door (hot spot)**

With ever-increasing ids, **every insert** goes to the same right-most B+Tree leaf, so every
thread queues at the same door. That's fine at thousands of inserts/s; at hundreds of thousands
it becomes the bottleneck. The fix: spread the traffic (hash partitioning, several
"doors").

**6. Remember it like this**

```text
Lock  = meeting-room booking (transaction, long, logical data, can deadlock)
Latch = holding a door       (thread, µs, memory structure, ordered → no deadlock)
 │
 ├── locks   → waiting, deadlocks → consistent update order, short transactions
 └── latches → hot-spot contention → spread the load
```

**Technical details — Lock vs latch**

- *Why two mechanisms:* two different dangers at two timescales. A **lock** protects a logical
  row from another *transaction* (held for ms to minutes, can deadlock → needs a deadlock
  detector). A **latch** protects a data structure in memory from another *thread* while it's being
  changed (held for µs, deadlocks avoided by always taking them in a fixed order). Using a heavy
  lock for a µs job is too slow; using a latch for a transaction-long job breaks isolation.
- *Problems they create:* locks → waiting and deadlocks. Latches → **hot-spot contention**. For
  example, with an ever-increasing key every insert hits the same right-most B+Tree leaf, so
  threads queue on its latch. (That's the flip side of "sequential keys are good for cache
  locality" — both are true; at very high insert rates, hash-sharding or partitioning spreads it out.)

#### Optimizer

The **optimizer** is best understood as **a navigation app like Google Maps or Waze**.

**1. Picture planning a drive 🗺️**

You type the destination. There are dozens of possible routes. The app uses **traffic data** to
estimate each route's time and shows you the fastest. You never say *which* roads to take, only
*where* you want to go.

**2. In the database**

SQL is the destination; a **plan** is a route.

```sql
SELECT o.*
FROM orders o JOIN users u ON u.id = o.user_id
WHERE u.country = 'AM' AND o.created_at > now() - interval '1 day';
```

```text
Route A: filter users by country → for each, look up their recent orders via index
Route B: take yesterday's orders  → for each, look up its user via index
Route C: scan both tables fully   → hash join
join method: nested loop? hash join? merge join?
```

**3. Traffic data = statistics**

```text
users:  10,000,000 rows,  country='AM' ≈ 0.5%      → 50,000 users
orders: 200,000,000 rows, last day ≈ 0.1%           → 200,000 orders

estimated cost:  Route A  ≈ 85,000   Route B  ≈ 1,200   Route C ≈ 2,400,000
                                      ▲ picked
```

`ANALYZE` refreshes these statistics (row counts, histograms, most common values, distinct counts).

**4. Problem 1: stale traffic data**

You bulk-load 5M rows into a table the optimizer still thinks has 1,000 rows. It picks a nested
loop "because the table is tiny", and a 50 ms query takes 30 s. It's the app confidently
sending you onto a road that closed yesterday. Fix: `ANALYZE` after big loads (autovacuum does it
eventually, but maybe not soon enough).

**5. Problem 2: correlated columns**

```text
WHERE city = 'Yerevan' AND country = 'AM'
city = 'Yerevan' ≈ 0.3% of users,  country = 'AM' ≈ 0.5% of users
optimizer assumes independent:  0.3% × 0.5% = 0.0015% → ~150 rows
reality: everyone in Yerevan is in AM   → 0.3%          → ~30,000 rows (200× more)
```

Like assuming two roads have independent traffic when one feeds into the other. Fix: extended
statistics (`CREATE STATISTICS`).

**6. Problem 3: too many stops**

For n tables the number of join orders grows like n!: 5 tables → 120, 12 tables → 479 million.
Like a route with 12 stops, the planner stops trying every order and uses a heuristic (Postgres
switches to genetic search at 12 tables, `geqo_threshold`).

**7. Why not a simple rule like "always use the index"?**

Because the fastest route depends on traffic. For 1 row, the index wins (4 pages vs 125,000).
For 40% of the table, the index would do ~4M random reads and a sequential scan wins easily.

**8. Remember it like this**

```text
Optimizer = navigation app
 │
 ├── SQL = destination, plan = route, statistics = traffic data
 ├── estimates cost of each route, picks the cheapest
 │
 ├── stale stats         → wrong route → ANALYZE
 ├── correlated columns  → wrong estimate → extended statistics
 ├── many tables         → too many routes → heuristics
 └── check with EXPLAIN (ANALYZE): estimated rows vs actual rows
```

**Technical details — Optimizer**

- *Without it:* the programmer writes the plan by hand, and it silently becomes wrong when the table
  grows from 1,000 to 100M rows.
- *How it fixes it:* enumerate plans, estimate each one's cost from statistics (row counts,
  histograms, distinct values), and pick the cheapest.
- *Why cost-based and not "always use the index":* for a query returning 40% of a table, an index
  scan does ~1 random read per row and is slower than reading the whole table sequentially.
- *Problems it creates:* estimates can be wrong (correlated columns, stale stats, skew), and a small
  estimate change can **flip the plan** → a query that took 5 ms takes 30 s with no code change.
  Planning time grows fast with join count (Postgres switches to a genetic search at 12 tables,
  `geqo_threshold`).

#### Index

An **index** is best understood as **the index at the back of a textbook**.

**1. Picture a 1,000-page textbook 📚**

You want everything about "WAL". Without an index you read all 1,000 pages. With the index at the
back: find "W", then "WAL → pages 214, 380", then open those two pages.

**2. In the database**

```text
SELECT * FROM users WHERE id = 42;

without an index:  read all ~125,000 pages      (~1 GB)
with a B+Tree:     root → internal → leaf → row  (4 pages, ~32 KB)
```

**3. Why a sorted tree? Like a phone book with tabs**

```text
                    ROOT  [ 1 … 3.3M | 3.3M … 6.6M | 6.6M … 10M ]      ← letter tabs
                           │
            INTERNAL [ 1…8K | 8K…16K | … ]  (~400 children each)       ← page headers
                           │
            LEAF  [ 41 → (2103,9) | 42 → (2104,3) | 43 → (2104,4) … ]   ← the entries
                                     │
                              table page 2104, slot 3  → the row
```

~400 keys per page means 3 levels cover 64 million keys. Because the keys are sorted, the same
tree also answers `BETWEEN`, `ORDER BY id` and `MIN(id)`.

**4. Cost 1: every edit updates the index**

When the author adds a paragraph, every index entry after it must be updated. Every
`INSERT`/`UPDATE`/`DELETE` also changes every index on the table:

```text
INSERT into users with 6 indexes = 1 table write + 6 index writes (+ WAL for all 7)
```

**5. Cost 2: space**

An index on `users.id` alone is ~200 MB (25,000 leaf pages) for a 1 GB table. Six indexes can be
bigger than the table itself, and they compete for the buffer pool.

**6. Cost 3: unused indexes are dead weight**

A book with an index entry for every word would be twice as thick and slow to revise. Find
indexes nobody uses (`pg_stat_user_indexes.idx_scan = 0`) and drop them.

**7. Different kinds of index, different kinds of "back of the book"**

| Index | Book analogy | Good for |
|---|---|---|
| B+Tree | the alphabetical index | `=`, ranges, sorting — the default |
| Hash | a coat-check ticket: exact number → exact hook | `=` only |
| GIN | a concordance: every word → every page it appears on | full-text, JSONB, arrays |
| BRIN | "chapter 5 covers the years 2020–2021" | huge tables naturally ordered by time |
| HNSW | a "books similar to this one" shelf | vector / similarity search |

**8. Remember it like this**

```text
Index = the index at the back of the book
 │
 ├── find rows without reading the whole table
 ├── B+Tree: sorted, ~400 keys/page, 3–4 levels, ranges + ORDER BY
 │
 ├── every write pays for every index
 ├── takes space and buffer-pool memory
 └── unused index = pure cost → drop it
```

**Technical details — Index**

- *Without it:* finding one row among 10M means reading ~125,000 pages.
- *Why a B+Tree by default:* ~400 keys per page → 3–4 levels for billions of rows, the top levels
  stay cached, and sorted order serves ranges and `ORDER BY` too.
- *Problems it creates:* each index is extra work on **every** insert/update/delete, extra space,
  and it bloats. Unused indexes are pure cost, so audit them (`pg_stat_user_indexes`). Too many
  indexes also give the optimizer more ways to pick badly.

#### Replica

A **replica** is best understood as **a branch office that receives every change from head office
by fax**.

**1. Picture a company with a branch office 🏢 → 🏢**

Head office makes every decision and faxes each one to the branch. The branch applies each fax to
its own copy of the files. Customers can ask the branch questions, and if head office burns down,
the branch can take over.

**2. In the database**

```text
PRIMARY (head office)                         REPLICA (branch)
 writes WAL ── stream of WAL records ──────►  replays WAL continuously
 (all writes)                                 (read-only queries)
                                              = crash recovery that never ends
```

**3. Why is it needed?**

- **Read scaling**: 20,000 reads/s can be spread across the primary and replicas.
- **High availability**: if the primary dies, promote a replica in seconds.

**4. Problem 1: fax delay (replication lag)**

```text
t = 0 ms    user posts a comment        → written on the primary
t = 50 ms   page reloads, read replica  → replica is 200 ms behind → comment missing!
```

The branch quoted yesterday's price. Fixes: send "read your own writes" traffic to the primary,
or wait until the replica has replayed past the commit's LSN.

**5. Problem 2: head office burns before the last fax was sent**

With **async** replication the primary says "OK" before the replica has the change. If it dies
right then, the promoted replica never saw those commits, and they're **lost** (RPO > 0).

**6. Problem 3: waiting for "fax received" slows every decision**

With **sync** replication the primary waits for the replica's acknowledgement before "OK". No
loss, but every commit pays a network round trip: ~0.5 ms in the same zone, 30–100 ms across
regions.

**7. Problem 4: the branch clerk is still reading a document the fax says to shred**

A long query on the replica needs old row versions that the replayed WAL wants to remove. Postgres
either **cancels the query** or **delays replay** (more lag). The knobs are
`max_standby_streaming_delay` and `hot_standby_feedback`. The latter makes the primary keep the
versions, which causes bloat there instead.

**8. Remember it like this**

```text
Replica = branch office receiving faxes (WAL)
 │
 ├── scales reads, survives primary failure
 │
 ├── lag                 → stale reads → read-your-writes on primary
 ├── async failover      → recent commits lost
 ├── sync replication    → no loss, +1 round trip per commit
 └── long replica query  → cancelled or delays replay
```

**Technical details — Replica**

- *Without it:* one machine = one point of failure, and reads are capped at what one machine can
  serve.
- *How it works:* the primary streams its WAL; the replica replays it (crash recovery that never
  ends).
- *Problems it creates:* **replication lag** → stale reads (a user doesn't see the comment they just
  posted). Async failover can **lose** the last acknowledged commits. Long queries on a replica can
  conflict with replayed cleanup (Postgres cancels the query or delays replay). Sync replication
  fixes loss but adds a network round trip to every commit.

#### Amplification (read, write, space)

**Amplification** is best understood as **three everyday annoyances of moving stuff around**.

**1. Picture three annoyances 🔩📖🗄️**

```text
READ amplification:   you need ONE screw → drive to the warehouse → carry back a box of 500
WRITE amplification:  fix ONE typo on page 12 of a printed book → reprint the page,
                      the table of contents and the index
SPACE amplification:  keep every old draft "just in case" → the cupboard is full
```

**2. In the database, with numbers from this chapter**

```text
Read amplification  = bytes read / bytes needed
  one row via a page:        8,192 B / 100 B          ≈ 82×
  one row via index + page:  4 pages × 8 KB / 100 B   ≈ 330×   (if nothing is cached)

Write amplification = bytes written to disk / bytes the user changed
  UPDATE name (≈10 B changed):
    WAL record ~150 B + page 8 KB (+ 8 KB full-page image after a checkpoint)
    ≈ 8–16 KB / 10 B ≈ 800–1,600×  (less if many updates share one page flush)
  LSM tree: each byte is rewritten once per level during compaction → ~10–30×

Space amplification = bytes on disk / live data
  bloated MVCC table:  80 GB / 10 GB = 8×
  LSM with obsolete versions: ~1.1–2×
```

**3. The rule: RUM — you can't win all three**

You can make at most **two** of **R**ead cost, **U**pdate cost and **M**emory/space cost small:

```text
                 Read cost low
                      /\
                     /  \
          B+Tree ── /    \
                   /  ✗   \        ✗ = "all three low" does not exist
                  /________\
   Update cost low          Space low
        LSM                 compression, columnar
```

Like "fast, cheap, good: pick two".

**4. How to use it**

For any design, ask: **which amplification does it lower, and which does it raise?**

```text
add an index        → read ↓   write ↑   space ↑
B+Tree → LSM        → write ↓  read ↑    (space depends on compaction)
compression         → space ↓  CPU ↑     (reads of compressed blocks cost CPU)
bigger pages        → fewer I/Os for scans, read amp ↑ for point lookups
```

**5. Remember it like this**

```text
Amplification = extra work per byte you actually wanted
 │
 ├── read  → carry a whole box for one screw
 ├── write → reprint the book for one typo
 ├── space → keep every old draft
 │
 └── RUM: pick two of read / update / space → every design is a trade
```

**Technical details — Amplification (read, write, space)**

- *Why the concept exists:* it's the common currency for comparing designs. *Read amplification*
  = bytes read ÷ bytes wanted (read a 16 KB page for a 100 B row = 160×). *Write amplification* =
  bytes written to disk ÷ bytes changed (WAL + page + index pages; LSM compaction rewrites data
  10–30×). *Space amplification* = bytes on disk ÷ live data (MVCC bloat, obsolete LSM versions).
- *Why it matters:* **RUM conjecture** — you can make two of read cost, update cost and memory/space
  cost small, never all three. B+Tree favours reads, LSM favours writes, compression favours space.
  Every design choice in §10 and §11 is a move along this triangle.

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

## 3. The Build Order: Phase 0 to Phase 17

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

## 11. What If the Architecture Changes? The change matrix

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
