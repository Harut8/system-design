# Appendix A — SurrealKV: a versioned LSM engine with radix-tree memtables and a commit ring

> **Why this appendix exists.** [Chapter 13](13-lsm-trees-and-compaction.md) teaches the LSM family
> through LevelDB and RocksDB: skiplist memtable, WAL, leveled compaction, Bloom filters, write
> stalls. SurrealKV 1.0.0 (October 2026) is a new member of that family that changes three parts of
> the standard design at once, and its whitepaper is unusually explicit about what each change costs.
> That makes it the best available worked example of the trade-off space chapter 13 describes:
>
> - the memtable is an **adaptive radix tree in a fixed arena**, not a skiplist;
> - commits pass through a **lock-free commit ring** for conflict validation, and a **single flusher
>   task** turns whatever accumulated into one group — one log append, at most one `fsync`;
> - **versioning** (keep every version of every key, read as of a time) rides on the structures that
>   are already there: no time index, no separate history store.
>
> Read this after chapter 13 §2–§6 and chapter 14 §11. It is a design-and-evidence reading, not a
> product page: every claim below is labelled **(paper)** when the whitepaper states it from the
> structure of the code, **(measured)** when it comes from their benchmark, and **(analysis)** when
> it is this appendix reasoning about what the design implies. The distinction matters, because the
> paper's own conclusion is that most of its design claims have *not* been measured.
>
> **Where the neighbouring detail lives:** LSM architecture, memtables, read path, compaction and
> write stalls in [`13-lsm-trees-and-compaction.md`](13-lsm-trees-and-compaction.md); WAL record
> framing, group commit and LSM WALs in
> [`14-write-ahead-log-internals.md`](14-write-ahead-log-internals.md); snapshot isolation, write
> skew and first-committer-wins in
> [`05-transactions-and-concurrency.md`](05-transactions-and-concurrency.md); B+tree and
> memory-mapped engines (LMDB, libmdbx, redb) in
> [`01-storage-engine-fundamentals.md`](01-storage-engine-fundamentals.md) and
> [`00-os-and-hardware-internals.md`](00-os-and-hardware-internals.md); lock-free structures and the
> cost of a shared latch in [`17-latches-and-locks-internals.md`](17-latches-and-locks-internals.md).
>
> Labs for this appendix: [LABS.md](LABS.md), section *Appendix A — SurrealKV*.

## Contents

1. [What it is, in one page](#1-what-it-is-in-one-page)
2. [Architecture](#2-architecture)
3. [Transactions: snapshots, the commit ring and first-committer-wins](#3-transactions-snapshots-the-commit-ring-and-first-committer-wins)
4. [The commit pipeline and the two durability modes](#4-the-commit-pipeline-and-the-two-durability-modes)
5. [The memtable: an adaptive radix tree in a fixed arena](#5-the-memtable-an-adaptive-radix-tree-in-a-fixed-arena)
6. [Back-pressure: the write stall gate](#6-back-pressure-the-write-stall-gate)
7. [The read path](#7-the-read-path)
8. [Table format, cache and compaction](#8-table-format-cache-and-compaction)
9. [Value separation, and the one hazard in it](#9-value-separation-and-the-one-hazard-in-it)
10. [Versioned trees: a timestamp in the value](#10-versioned-trees-a-timestamp-in-the-value)
11. [Recovery, integrity, checkpoints](#11-recovery-integrity-checkpoints)
12. [How it is verified, and what the tests miss](#12-how-it-is-verified-and-what-the-tests-miss)
13. [The cost model](#13-the-cost-model)
14. [The measurements, and how to read them](#14-the-measurements-and-how-to-read-them)
15. [Design lineage: LevelDB, RocksDB, Pebble](#15-design-lineage-leveldb-rocksdb-pebble)
16. [Limitations worth weighing before you rely on it](#16-limitations-worth-weighing-before-you-rely-on-it)
17. [What to steal for your own engine](#17-what-to-steal-for-your-own-engine)
18. [Interview questions](#18-interview-questions)
19. [Sources](#19-sources)

---

## 1. What it is, in one page

SurrealKV is an embedded, transactional key-value storage engine in Rust, written by SurrealDB as
their own alternative to RocksDB. Structurally it is a leveled LSM-tree with snapshot-isolated
transactions — the Bigtable/LevelDB arrangement of chapter 13 §2: a write-ahead log, an in-memory
memtable, immutable sorted tables in levels, and a manifest that records which table belongs to
which level. Three things set it apart from RocksDB, Pebble, Fjall and SlateDB **(paper, abstract)**:

| Standard LSM design (ch. 13) | SurrealKV 1.0.0 |
| --- | --- |
| Memtable is a skiplist | Memtable is a concurrent **adaptive radix tree** (ART) in a **fixed arena** |
| Commit group written by an *elected leader thread*, applied concurrently | Commit group assembled by **one standing flusher task**: numbers it, appends it, applies it, publishes it |
| MVCC history, if any, needs its own index or a separate store | **Versioning rides on the sequence numbers and compaction rules already there**: the time is 8 bytes inside the value |

Everything else is familiar: 56-bit sequence numbers order all writes, a reader pins a sequence
number and sees the newest version at or below it, tables carry a partitioned Bloom filter and a
partitioned index behind a shared block cache, and large values can optionally be kept out of the
tree in a WiscKey-style value log.

The defaults are worth memorising, because every number later in this appendix is relative to them
**(paper, Appendix B)**:

| Setting | Default |
| --- | --- |
| Memtable size (= arena size) | 100 MiB |
| Levels | 6 |
| Block size, restart interval | 64 KiB, every 16 entries |
| Index partition size | 16 KiB |
| Bloom filter | 10 bits/key, 7 probes, over user keys |
| Compression | **off** (Snappy and Zstd level 3 available, per level) |
| Block cache | 32 MiB, byte-weighted, sharded |
| Level-0 compaction trigger | 4 tables **or** 256 MiB |
| Level base and multiplier | 256 MiB at L1, ×10 per level |
| Compaction output file size | ~64 MiB |
| Write stall thresholds | 2 immutable memtables, 12 level-0 tables |
| Commit ring | 65,536 slots, 32,768 admission permits |
| Commit group limit | 4 MiB of batch bytes |
| Value separation | **off**; threshold 1 KiB; value-log files of 256 MiB; value checksums **off** |
| Versioning | **off**; retention 0 (= keep for ever) when on |
| Durability | `Eventual` (write to the OS, no `fsync`) |
| Log recovery mode | trim a proven torn tail, refuse other damage |

Two defaults deserve a flag immediately **(analysis)**. Compression off and a 32 MiB block cache are
*benchmark-shaped* defaults, not production-shaped ones: a 32 MiB cache against a 64 KiB block size
holds about 500 blocks. And `Eventual` durability means a committed transaction survives a process
crash but **not** a machine crash, with nothing syncing the log on a timer — the opposite of the
default most engineers assume from Postgres (`synchronous_commit = on`, ch. 14 §14).

---

## 2. Architecture

```
SURREALKV: THE PARTS
=====================

TRANSACTION           buffered write set  +  snapshot = published sequence number
                                  │
                                  ▼ commit
COMMIT PIPELINE   ┌───────────────────────────────────────────────────────────┐
                  │  write stall gate → admission permit → commit ring slot   │
                  │                      (65,536 slots, 32,768 permits)       │
                  │                              │ validate (concurrent)      │
                  │                              ▼                            │
                  │                      ONE FLUSHER TASK                     │
                  │        gather group (≤4 MiB) → number → WAL append        │
                  │        → ≤1 fsync → apply to memtable → publish           │
                  └───────────────────────────────────────────────────────────┘
                                  │
MEMORY            ┌───────────────▼────────────────┐   ┌────────────────────┐
                  │ ACTIVE MEMTABLE                │   │ IMMUTABLE MEMTABLES│
                  │ adaptive radix tree, 100 MiB   │──▶│ oldest flushed     │
                  │ fixed arena, version chains    │   │ first, one at a    │
                  └────────────────────────────────┘   │ time               │
                                                       └─────────┬──────────┘
                                  ┌──────────────────┐           │ flush
                                  │   BLOCK CACHE    │           ▼
                                  │ 32 MiB, sharded, │   ┌────────────────────┐
                                  │ byte-weighted:   │◀──│ SSTABLES, L0..L5   │
                                  │ index parts,     │   │ L0 overlapping;    │
                                  │ filter parts,    │   │ L1 256 MiB, ×10    │
                                  │ data blocks,     │   └────────────────────┘
                                  │ vlog values      │
                                  └──────────────────┘   ┌────────────────────┐
STORAGE   write-ahead log (32 KiB blocks,                │ VALUE LOG (opt.)   │
          one segment per memtable generation)           │ 256 MiB files      │
          manifest (one file, replaced atomically)       └────────────────────┘
```

Two background tasks do all the deferred work **(paper §3)**: one flushes sealed memtables to level 0
(oldest first, one at a time), and one compacts levels (one compaction at a time). Neither is paced;
there is no I/O rate limiter of the kind RocksDB offers.

---

## 3. Transactions: snapshots, the commit ring and first-committer-wins

### 3.1 What a snapshot is

Every key operation gets a **56-bit sequence number**, assigned by the flusher in commit order. A
transaction's snapshot is simply "the published sequence number when I began", and a read returns the
newest version of a key at or below it **(paper §4.1)**. This is the MVCC-by-sequence-number model of
chapter 05 §4, with the cheapest possible reader state: a reader pins *one integer*. There is no
reader slot table (LMDB), no per-key visibility bookkeeping, no refcount on versions.

A transaction that can read registers its snapshot in a tracker of **64 sharded sets**, which
compaction consults to decide which old versions must survive. A write-only transaction cannot read,
takes no snapshot, and registers nothing.

### 3.2 Conflict detection without a lock table

Isolation is **snapshot isolation with first-committer-wins on written keys** (ch. 05 §3). Reads can
be promoted into the check explicitly with `get_for_update`, which is the usual SI escape hatch for
write skew. At commit:

1. The committer claims the next slot of the **commit ring** with one atomic increment.
2. It publishes its **sorted key list** and a **4,096-bit Bloom filter** of its keys into that slot.
3. It validates against every slot claimed since its own window began, skipping aborted slots and
   those already visible in its snapshot. Two entries conflict if their key sets intersect — decided
   by the Bloom filter first, then by an exact search of the sorted keys.

This is optimistic concurrency control (ch. 18) implemented as a ring of published write sets instead
of a lock manager. The price list is explicit **(paper, Table 7)**:

- validation cost grows with **the number of commits that ran during your transaction**, not with the
  size of the database;
- **an undecided entry counts as a conflict**, so an earlier commit that later aborts can abort a
  later transaction for nothing (a false abort);
- a long-open read-write transaction holds back retirement of ring entries, and **the resulting
  overflow is not bounded** — this is listed by the paper itself as a limitation.

Two sharp edges for anyone writing a client **(paper §4.1)**:

- **Scans and plain reads are not validated.** Write skew across different keys is possible, and a
  range delete is validated **only on its start key**. There is no serializable mode.
- **A commit is terminal.** Whether it succeeds or fails, the transaction is closed; a conflict means
  the application must build a new transaction, not retry the old one.

### 3.3 Worked example (from the paper, §4.5)

Three transactions begin at published sequence 100. T1 writes `a` and `b`; T2 writes `c`; T3 writes
`b`. They commit in order T1, T2, T3.

- T2's keys don't intersect T1's → passes validation.
- T3 shares `b` with T1, whose entry is not visible in T3's snapshot → **aborts with a write
  conflict**; its slot is marked aborted.
- The flusher finds T1 and T2 decided, takes them as one group (skipping the aborted slot), numbers
  the three key operations 101, 102, 103, appends **both records in one call**, syncs once if either
  asked for `Immediate`, applies them to the memtable, stores **103** as the published sequence
  number and wakes both committers.
- A transaction beginning now has snapshot 103 and sees all three writes; one that began at 100 sees
  none of them. A retried T3 begins at 103, reads T1's `b`, and can commit.

---

## 4. The commit pipeline and the two durability modes

```
COMMIT PIPELINE: WHO DOES WHAT
================================

COMMITTERS (many at once, nothing awaits between 1 and 4)
  1 stall gate        2 admission         3 validate          4 hand off
  pace or wait        one of 32,768       claim a ring slot,  give the batch to
  (§6)                permits             check the window    the flusher, wait

FLUSHER (one task, one group at a time)
  5 gather group      6 number + log      7 apply + publish
  every decided       assign sequence     insert into the memtable,
  entry after the     numbers, ONE        store the new published
  last handled, in    append, ≤1 fsync    sequence, mark entries
  ring order, ≤4 MiB  if any member       visible, advance the ring's
                      asked for it        completed prefix, wake committers
```

Notes that matter **(paper §4.2)**:

- **No linger timer.** A group is whatever accumulated while the previous group was being written.
  Compare RocksDB's and Postgres's group-commit delays (ch. 14 §14): SurrealKV's group size is purely
  a function of arrival rate and of how long the previous append took. Under low concurrency the
  group is one commit; under high concurrency it is up to 4 MiB of batches.
- **From the permit to the verdict nothing awaits**, so a cancelled commit can never leave a claimed
  slot undecided (which would otherwise abort everyone behind it, §3.2).
- **The commit returns when the flusher finishes the group** — not when the memtable is flushed to a
  table, and not after any compaction.
- **Only the flusher writes the active memtable**, so applying a group is single-threaded by
  construction. The pipeline's concurrency lives in validation and in reads.
- Partial failure: if a group fails before anything is applied, the whole group fails and the log is
  cut back to its pre-group length. If it fails *after* part of it was applied, **the database stops**
  and recovery decides the outcome at the next open.

### 4.1 The two durability modes

| Mode | What happens before the commit returns | Survives |
| --- | --- | --- |
| `Eventual` (default) | Group written to the OS; **no `fsync`**, and nothing syncs the log on a timer | Process crash, not machine crash |
| `Immediate` | **One `fsync` for the whole group**, before the group is applied and acknowledged | Machine crash (commit is durable before it is visible) |

Durability is chosen **per transaction**, with one consequence to internalise: one `fsync` serves
every commit in the group, so **an `Eventual` commit that shares a group with an `Immediate` one is
synced too** **(paper §4.3)**. The converse is the real cost: one slow `fsync` delays every commit in
its group.

### 4.2 The five invariants

The paper states five invariants that carry the consistency argument **(paper §4.4)**; they are a
good checklist for any MVCC engine you write yourself:

1. **Read order at begin.** A transaction that can write registers its pin in the active-transaction
   tracker, reads its commit window, and *only then* reads the published sequence number. The order
   puts every commit at or below the window inside its snapshot.
2. **Publish after apply, in order.** The published sequence number advances only after a group is
   fully applied; then entries are marked visible and the ring's completed prefix advances. A reader
   therefore never sees part of a group.
3. **Values before the log.** Separated values reach the value log (flushed or synced to match the
   group) *before* the log record that points to them, and a log segment is not sealed before the
   value log is synced. A log record never points at a value that is not there.
4. **Flush ordering.** A flush adds the new level-0 table and removes the memtable from the immutable
   queue while holding both the manifest's and the queue's write locks, so a reader that misses the
   memtable finds the table.
5. **Compaction reads the horizon first.** The compactor reads the published sequence number, then
   the registered snapshots, and never the log sequence number. A restore may lower the published
   number, which only makes compaction keep *more*.

Invariant 5 has a documented hole, and the paper says so: a transaction that has taken its sequence
number but not yet registered its snapshot **can be missed** by a concurrent compaction.

### 4.3 Against RocksDB and Pebble

| | RocksDB | Pebble | SurrealKV |
| --- | --- | --- | --- |
| Who orders commits | Leader of a batch group | A commit mutex, held while a batch is numbered and logged | One flusher task, in ring order after validation |
| Log write | Leader writes the whole group | Each batch on its own, copied into the log buffer | Whole group in **one append** |
| Memtable apply | Concurrent, by the group's batches | Concurrent, outside the mutex | **Sequential, by the flusher** |
| Visibility | Visible sequence bumped once per group | Published in order through a commit queue | Published once per group, in ring order |
| Waits | Leader and followers wait on one another | One queue entry per commit | One hand-off and one completion per commit |
| Sync | Optional | Optional | One `fsync` per group if any member asked |

Pebble's design notes criticise the RocksDB pipeline for copying batch contents into the group and
for its thread synchronisation points. SurrealKV keeps the group (so one append and one `fsync` serve
a burst) and replaces leader/follower waits with a single hand-off — **but gives up the concurrent
memtable apply**, which is exactly why RocksDB wins its own benchmark when no `fsync` is in play
(§14).

---

## 5. The memtable: an adaptive radix tree in a fixed arena

Chapter 13 §2 presents the memtable as a skiplist, because that is what LevelDB, RocksDB and Pebble
use: ordered, lock-free-ish insertion, O(log n) with a big constant and one cache miss per level
traversed. SurrealKV uses a **concurrent adaptive radix tree** (ART, Leis et al. 2013) from the
`artmap` crate, with **per-key version chains**, and allocates nodes, leaves and versions from a
**fixed arena whose size is the memtable size** **(paper §5)**.

Why this is interesting, in chapter-13 terms **(analysis)**:

- **Lookup cost depends on key length, not on key count.** A radix tree descends one node per key
  byte chunk; a skiplist descends ~log₂(n) levels, each a pointer chase to a random address. For the
  short keys an embedded engine sees, ART's path is shorter *and* more predictable.
- **Adaptive node sizes (4/16/48/256 children) keep nodes cache-line friendly**, which is the whole
  point of the original paper; a skiplist's tower nodes are scattered by construction (ch. 00 §cache
  hierarchy).
- **Point reads benefit; ordered scans benefit less.** A radix tree is ordered, but iteration walks a
  trie rather than a linked list at level 0 — the scan numbers in §14 are consistent with that.
- **Version chains per key are the right shape for versioning** (§10): all versions of a key hang off
  one leaf, so `history` is a walk of one chain, not a range scan over encoded `key|seq` tuples the
  way a RocksDB-style engine would do it.

Arena behaviour, which is the operational half of the choice:

- When the next group does not fit, the memtable is **sealed**, joins the immutable queue, and is
  replaced. The arena is never grown.
- **A batch too large for any memtable is written straight to a level-0 table** — compare RocksDB
  (the memtable grows to take it, and the notes warn this can exhaust memory and repeat on restart)
  and Pebble (the sorted batch joins the immutable list as an extra layer).
- A background task flushes immutable memtables oldest first, one at a time, writing each as a
  level-0 table, updating the manifest atomically, and deleting the log segments the flush made
  obsolete.
- A failed flush short of fatal is retried with backoff **50 ms → 2 s**; an I/O error while writing
  the table is **fatal and stops the database**.

One honest caveat the paper lists under limitations: **the memtable size budgets the arena, not the
bytes of keys and values**, and no memory limit is enforced engine-wide.

### 5.1 The write-ahead log

The log is a sequence of **32 KiB blocks** with a **7-byte record header** holding a CRC-32 checksum,
a length and a type — recognisably LevelDB's log format (ch. 14 §11), with these properties
**(paper §5)**:

- **One committed transaction is one record**, so it is recovered whole or not at all;
- a **group is appended in one call**;
- **each memtable generation has its own log segment**, so a flush can delete whole segments;
- separated values reach the value log before the log record that points to them.

---

## 6. Back-pressure: the write stall gate

The gate checks two counts before every commit **(paper §5)**:

```
IMMUTABLE MEMTABLES          LEVEL-0 TABLES
  0 ─ 1   proceed              0 ──── 8    proceed
  2+      WAIT (hard stall)    9 ─── 11    delay 100 µs per table above 8
                              12+         WAIT (hard stall)
```

With the default settings, **the memtable count reaches its hard limit before any level-0 pacing
applies to it** — i.e. in practice the stall you hit first is "two sealed memtables waiting to be
flushed", not the gentle level-0 delay. This is the chapter 13 §6 write-stall problem with smaller
numbers and no rate limiter: SurrealKV does not pace background I/O at all, where RocksDB offers an
optional (fixed or auto-tuned) rate limit on flush and compaction I/O, and Pebble paces flushes to
the rate of user writes and compactions to an estimate of compaction debt **(paper §9)**.

**(analysis)** The combination — unpaced background I/O, one compaction at a time, a hard stall at 2
immutable memtables — predicts a sawtooth under sustained writes rather than a smooth slowdown. The
paper agrees that this is untested: "behaviour under sustained writes, where the stall gate and
compaction interact" is on its own list of measurements not run.

---

## 7. The read path

```
WHERE A POINT READ LOOKS (first match at or below the snapshot wins)
=====================================================================

 1. the transaction's own write set          ← sees its own writes
 2. active memtable                          ← radix-tree lookup
 3. immutable memtables, newest first        ← m lookups
 4. level 0: range check + Bloom, newest first   ← z probes
 5. levels 1..L: binary search, ONE table per level
       └─ Bloom filter partition → top-level index (in memory)
          → one index partition → one data block (via cache)
          → seek inside the block by restart points
 6. if the value's 2-byte header says "pointer": read the VALUE LOG (also cached)

BEFORE all of that: collect the largest range-delete sequence covering the key
from the active memtable, every immutable memtable, and every table's metadata.
```

Details with consequences **(paper §6)**:

- **The range-delete horizon is cheap only while nobody uses range deletes.** Each table carries an
  atomic flag, so the check is free when no range deletes exist — but once *any* table has them, the
  check costs a pass over **every** table on every point read. Chapter 13 §4's tombstone-graveyard
  problem, in a new shape.
- **A synchronous read holds the level manifest's read lock across its table reads.** So a manifest
  swap (a flush or compaction installing its output) waits behind readers. The asynchronous variant
  collects candidate tables under the lock, releases it, then offloads only the data-block read.
  This is a reader-writer latch on the hot path — chapter 17's subject exactly.
- **Reads are plain positional reads on the calling thread.** No memory mapping, no `io_uring`, no
  kernel async I/O.
- **Scans merge with a linear-scan cursor, not a heap.** Every source is positioned, and each step
  picks the smallest key by comparing **all** sources. Each overlapping table in a deeper level is
  its own source; there is no per-level iterator. A backward step re-scans from the previous restart
  point of the block and copies the key and value, and a block entered by a scan or point read is
  copied into the iterator.

**(analysis)** That last point is the structural explanation for the scan results in §14: an O(s)
comparison per step instead of O(log s), plus per-step copying, against LMDB walking a single
memory-mapped B+tree with zero-copy reads. The paper notes the costs are "consistent with" the scan
results but says it has not measured their share.

---

## 8. Table format, cache and compaction

### 8.1 The table

```
SURREALKV TABLE LAYOUT
========================
  ┌──────────────────────────────┐
  │ data blocks (~64 KiB each)   │  restart point every 16 entries,
  │  ...                         │  prefix compression between them
  ├──────────────────────────────┤
  │ filter partitions            │  Bloom, 10 bits/key, 7 probes, user keys
  ├──────────────────────────────┤
  │ filter index                 │
  ├──────────────────────────────┤
  │ index partitions (~16 KiB)   │  fetched on demand, through the cache
  ├──────────────────────────────┤
  │ top-level index              │  read into memory when the table opens
  ├──────────────────────────────┤
  │ metadata                     │
  ├──────────────────────────────┤
  │ footer (fixed 50 bytes)      │
  └──────────────────────────────┘

  Every block ends in: [ compression byte | masked CRC-32 ]
  CRC-32 is the IEEE polynomial, NOT CRC-32C, verified on every read from
  disk and not re-checked in the cache.
```

Compare chapter 13 §7's LevelDB table and §6's RocksDB table: the shape is the same, the
**partitioning of both index and filter in step** is the RocksDB `partitioned_index_and_filters`
idea, and the payoff is that an open table costs only a small top-level index in memory instead of
its whole index and filter **(paper, Table 7)**. The price: a cold partition costs an extra read.

### 8.2 The cache

One **byte-weighted, sharded cache of 32 MiB** (default) holds index partitions, filter partitions,
data blocks and value-log values. One quirk the paper admits: **scans and point reads cache the same
block under different keys**, so a block can be cached twice.

### 8.3 Compaction

Leveled, with LevelDB's shape and RocksDB's scoring **(paper §7)**:

| Decision | Rule |
| --- | --- |
| Level-0 trigger | 4 tables **or** 256 MiB |
| Level targets | 256 MiB at L1, ×10 per level, 6 levels |
| Which level | Highest score |
| Which table in it | Largest, **weighted up by its share of deletions** |
| Inputs | Every selected input is rewritten |
| Outputs | ~64 MiB, cut only between user keys |
| Parallelism | **One compaction at a time** |
| On failure | Partial outputs removed (unless the manifest write is uncertain), retried with 50 ms → 2 s backoff; writes continue until level 0 hits its stall limit; **corruption in an input stops the database** |

What a compaction keeps: every version a registered snapshot can read, everything above the
published sequence number, and the newest published version of each key. In an **unversioned** tree
it drops hard delete markers **only when writing the last level** — more conservative than LevelDB,
which drops a delete marker as soon as no deeper level overlaps its key. That costs space and buys
simplicity.

---

## 9. Value separation, and the one hazard in it

Value separation is the WiscKey idea from chapter 13 §10: compaction rewrites large values over and
over, so keep them in a separate log and store a pointer. SurrealKV's version **(paper §7)**:

- **off by default**; when on, a value longer than **1 KiB** goes to the value log and the tree stores
  a **25-byte pointer**;
- value-log files rotate at **256 MiB**;
- a commit group **pins** the files its values go into until the group is applied;
- **value checksums are off by default**.

Reclamation is where to pay attention: a file is deleted only when it lies **wholly below the
smallest file referenced by any live table or unflushed memtable**. When no table points into the log
at all, every file but the active one is deleted. There is **no rewriting garbage collector**.

**(analysis + paper, Table 7)** So space is reclaimed strictly as a *prefix* of files, and **one
long-lived large value pins every file after it**. A workload where a few big values are written early
and never updated — a config blob, a tenant's base document — can hold hundreds of gigabytes of dead
value log alive indefinitely. This is the engine's sharpest space-amplification edge, and the paper
names it as one of two trade-offs that "deserve a second look", with a rewriting collector listed as
an open question.

---

## 10. Versioned trees: a timestamp in the value

A tree can be opened **versioned**, with a retention in nanoseconds (0 = keep every version for
ever). A versioned tree keeps the history of every key and can answer reads as of a time. The design
claim is that this needs **no new structure** **(paper §8)**:

| Need | How it is met |
| --- | --- |
| A time per version | **8-byte stamp inside the value** (commit time, or one the caller gives with `set_at`; 0 means commit time) |
| Ordering of versions | **The sequence numbers that already order everything** |
| History storage | **Per-key version chains in the memtable, ordinary records in tables** |
| Deletes | A soft delete is a tombstone that is *itself a version*: the stamp alone plus a 2-byte header (10 bytes) |
| Hiding older history | **Barriers**: a hard delete, a range delete and a replace hide whatever was written before them |
| Keeping history across compaction | **The same rule compaction already uses for snapshots** |

Reading:

- `get_at(key, t)` returns the version with the newest time **at or before** `t`, taking the newest
  *write* among versions sharing one time. It reads the versions of the key the snapshot can see once,
  oldest write first, holding only the best so far.
- `history(range)` lists versions newest-time-first, forwards or backwards, with options for
  tombstones, a time range and a limit — and **holds one key's versions in memory** while returning
  them.
- Both honour the transaction's own pending writes and range deletes.
- A transaction writes **one version per key per time**: the last of its writes to that key, whatever
  savepoints came between.

What compaction keeps, by setting **(paper, Table 3)**:

| Setting | Kept beyond what readers can see |
| --- | --- |
| Unversioned | Nothing — the newest version, plus what each snapshot reads |
| Versioned, retention 0 | Every version that no barrier hides |
| Versioned, retention `r` | Versions written at or after `T − r`, plus, **for every reader**, the newest version before that time |

`T` is read once, when the compaction starts. A barrier above the last level is kept as a rule, since
tables the compaction did not read may hold what it hides; at the last level a barrier is kept only
while a version below it is kept. A version written while the tree was *not* versioned has no stamp,
and gets stamped with the time it reads at if it survives to the last level.

### 10.1 The three traps

**(paper §8, §16)** — these are the parts to say out loud before designing on top of it:

1. **Time can go backwards.** The tree is ordered by sequence number; the time is a *property of the
   value*. A write made later can carry an earlier time. Reading by time is a **rule over the versions
   of a key**, not a seek to a position — so it is not the "as-of" semantics of a
   system-versioned SQL table (ch. 05 §7), and a careless `set_at` can make history non-monotonic.
2. **A read by time reads every version of its key** that the snapshot can see. Cost grows with the
   history kept, and `history` holds one key's versions in memory. There is no time index to prune it.
3. **Versions written while versioning was off read at the time of the versions before them**, not at
   the time they were actually written. Turning versioning on later does not retroactively date the
   old data.

Cost of the feature when on: 8 bytes per version, 10 for a soft delete; compaction that applies a
retention decides over the versions of each key and allocates buffers that grow with them. **None of
this is measured — the paper's benchmark runs with versioning off, and it states that no benchmark of
a versioned tree exists.**

---

## 11. Recovery, integrity, checkpoints

### 11.1 Open

**(paper §12)** Opening a tree **refuses a directory that holds data but no usable manifest**, so an
open never silently creates an empty database — a good default that the "data directory mounted
empty" class of outage (ch. 14 §15) argues for.

The manifest is a **single file** listing table identifiers per level, replaced atomically by: write
a temporary file → `fsync` it → rename → `fsync` the directory. It ends in a **16-byte trailer**
holding its length and a checksum, verified at open (a manifest without one, written by an earlier
release, is accepted and gains one on the next write). This is the one place SurrealKV simplifies
LevelDB *downward*: LevelDB's manifest is an append-only log of version edits; SurrealKV's is a file
that is rewritten.

Then: **every table the manifest lists is opened** (LevelDB's notes prefer to open tables lazily),
the log is replayed from the manifest's log number with one memtable per segment, and the engine
continues in a **fresh segment** so a damaged segment is never appended to. Tables the manifest does
not list, value-log files below the smallest referenced one, and stale temporary manifests are
deleted.

### 11.2 What happens on each failure

| Failure | Behaviour |
| --- | --- |
| Data but no manifest; manifest fails to decode or fails its checksum | Open is **refused** |
| Torn record at the end of the newest log segment, nothing intact after it | Segment copied to a **quarantine** file (last three kept), then truncated in place; earlier acknowledged commits survive |
| Damage with intact records after it, or in an older segment | **Default: refused**, naming the segment, the offset and how many intact records are at stake. *Salvage* mode keeps the valid prefix and discards the rest after quarantine. *Strict* mode refuses everything and touches nothing |
| Failed `fsync` of the log | The segment is **poisoned** and the next group replaces it — **a commit that returned an error can still take effect at the next open** |
| Failed memtable flush | Retried with 50 ms → 2 s backoff if short of fatal; stalled writers released with an error. An I/O error writing the table is **fatal** |
| Failed compaction | Partial outputs removed, retried with backoff; writes continue until level 0 stalls. **Corruption in an input stops the database** |

**(analysis)** Two rows deserve underlining. "A commit that returned an error can still take effect
at the next open" means the client-visible outcome of a failed `fsync` is *indeterminate* — the
classic "`fsync` failure is not retryable" problem (ch. 14 §15) surfaced honestly in the API
contract. And "damage in the middle of a segment stops the open" makes `Eventual` durability riskier
than it first sounds: a machine crash with unsynced data is exactly the case that produces damage
whose position you do not control.

### 11.3 Integrity and encryption

Table blocks carry a masked CRC-32 checked on every read from disk (not re-checked in cache); log
records carry a CRC-32 checked at recovery; the manifest carries a checksum; an offline command reads
every block of every table of all six kinds and reports failures. **There is no background
scrubber.**

A database created with a key manager is encrypted at rest — manifest, tables, value log and log
sealed with **AES-256-GCM or XChaCha20-Poly1305** under per-component keys derived from a root key,
with every block bound to its file and position so a moved or edited block is refused. Encryption is
chosen **at creation only** and cannot be added later; it does not hide file names, sizes or access
timing, and the on-disk formats are not frozen.

### 11.4 Checkpoints

A checkpoint flushes the memtables, **hard-links** every table into the destination, copies the
manifest and the value log, writes an empty log directory, syncs, and writes its metadata file
**last** so an interrupted checkpoint does not look complete. It refuses a destination that is, lies
inside, or contains the database directory, and writes one destination at a time.

A restore validates the whole checkpoint — manifest and every table it lists — **before removing
anything**, writes a marker before the first removal and clears it after everything is synced, and
opening a directory that still holds the marker fails. It waits for running compactions and fences
in-flight commits. **A restore is not atomic, but an interrupted one is detectable.**

---

## 12. How it is verified, and what the tests miss

This section is the most transferable part of the paper if you are building an engine yourself
**(paper §13)**.

**What exists:**

- A **seeded differential test** drives the engine and an in-memory reference model with the same
  actions (begin, set, delete, delete range, savepoints, rollback, reads, commit, range scans, log
  flush, restart). Any divergence in a read, a scan, a commit verdict, or the full state after a
  restart fails the run. CI runs **500 seeds × 5,000 steps** over a pool of 30 keys with up to 12
  open transactions.
- A **second randomised model for versioned trees**, checking `get`, `get_at`, `history`, scans and
  iterator walks against a model written from the versioning rules, across transactions, savepoints,
  flushes, deep compactions, crashes, writes made without versioning, old snapshots, retention and
  two-level trees. The longer run does 300 sequences.
- **Fault suites**: unsynced files come back empty (the file-system power-loss contract), the log is
  torn at many byte offsets across restarts in every recovery mode, a crash image is cut at any byte
  of a grouped commit, failures are injected into `fsync`, manifest replacement and value-log writes,
  checkpoints must refuse overlapping destinations, database directories are copied at each step of a
  compaction/level change/checkpoint/restore and reopened, restores race against commits and flushes,
  and **every byte of a manifest is bit-flipped and each alteration must be refused**.
- The **compaction retention rule** is checked against a model of readers over **500,000+ small
  cases** of sets, deletes and range deletes.

**What is missing, in the paper's own words:**

- It is **not deterministic simulation testing** (FoundationDB-style): time, files and scheduling are
  real, no fault is injected in the differential test, and its "restart" is a graceful close and
  reopen.
- The differential test runs the **default configuration**, so the value log, compression and
  versioning are **not exercised** by it; it generates no soft deletes, replaces, locked reads or
  backward iteration; and **its commits are never concurrent**.
- Property-based tests cover only the encryption layer; one randomised test exercises the log reader;
  **there is no coverage-guided fuzzing**.

**(analysis)** Read those two lists together and one gap stands out: the commit ring is the engine's
most novel and most concurrency-sensitive component, and the differential model — the test that would
catch a wrong commit verdict — **never runs two commits at once**. Everything protecting the ring's
correctness is argument (the five invariants) plus the crash suites, not differential testing under
contention. That is the question to ask in an evaluation.

---

## 13. The cost model

The paper's cost table, which it is careful to describe as "the loops in the code, not measurements".
`m` = immutable memtables, `z` = level-0 tables passing the key range and filter, `L` = levels,
`w` = commits in a validation window, `s` = sources of a scan **(paper, Table 6)**:

| Operation | Cost grows with |
| --- | --- |
| Point read | `m` memtable lookups; `z` level-0 probes; one table per level for `L` levels, each a filter probe plus at most one index partition and one data block (cached or read); one more read for a separated value |
| Scan | `s` sources, each positioned, **and a comparison with every source at each step** |
| Commit: validate | `w` range tests and filter probes, plus a binary search of the commit's keys on each hit |
| Commit: flush a group | The group's bytes, up to 4 MiB: one append, at most one `fsync`, and a sequential insert of every key |
| Memtable flush | The memtable's size: one level-0 table |
| Compaction | Every selected input is rewritten; with a fanout of 10, a key is rewritten at each level it reaches (ch. 13 §5) |
| Read by time | **Every version of the key the snapshot can see**, in every memtable and table, read once; `history` holds one key's versions in memory |
| Compaction, versioned | As above, plus a decision over the versions of each key, with buffers that grow with them |

### 13.1 The decisions and their prices

The paper's most useful table, reproduced in condensed form **(paper, Table 7)**:

| Decision | What it spares | What it costs |
| --- | --- | --- |
| One flusher groups concurrent commits | One append and ≤1 `fsync` per burst; ordered sequence numbers with no committer coordination | Memtable apply is single-threaded; one slow `fsync` delays the whole group |
| Validate against a ring, not a lock table | No per-key lock or state: one atomic increment plus a window scan | Validation grows with concurrent commits; a long-open write transaction lets the ring overflow **unbounded** |
| Arena memtable | Bump allocation for nodes, leaves, versions; readers never block | Fixed size; rotation when the next group does not fit |
| Reads pin only a sequence number | No read lock, no per-key reader bookkeeping | A synchronous read holds the manifest read lock across its table reads, so a manifest swap waits |
| Partitioned index and filter behind one cache | A table costs a small top-level index in memory | A cold partition costs an extra read; a scan and a point read cache the same block twice |
| Value separation (when on) | Compaction moves keys and pointers, not large values | One extra read per large value; space reclaimed only as a prefix of files |
| Time stamped in the value | A time index, and any change to unversioned records | 8 bytes per version; a read by time reads every version; a later write can carry an earlier time |

---

## 14. The measurements, and how to read them

### 14.1 Method

**(paper §14.1)** `crud-bench` on one bare-metal machine: AMD Ryzen Threadripper 9970X (32 cores, 64
threads, up to 5.48 GHz), 128 GB DDR5, 4 TB PCIe 4.0 NVMe, Ubuntu 24.04 / kernel 6.8.0-146,
powersave governor, THP in madvise mode, 8 GB swap, mimalloc. **500,000 keys, 48 worker threads, 128
clients, and every figure is a single run.**

SurrealKV in the harness runs with a **256 MiB memtable** (not the 100 MiB default), 64 KiB blocks, a
cache sized from machine memory, **value separation on** and **versioning off**. Comparators:
`surrealdb-rocksdb` 0.24.0-surreal.5 wrapping **RocksDB 11.0.0** (optimistic transactions, 256 MiB
write buffer, level compaction, blob files for values ≥4 KiB), `slatedb` 0.15.0 **on local disk, not
object storage**, `fjall` 3.1.10, `redb` 4.3.0, `heed` 0.22.1 over LMDB, `libmdbx` 0.6.6. LevelDB and
Pebble are not measured. Every operation — including reads — is a transaction.

The "synchronous durability" flag means different things per engine, which the paper tabulates
honestly: SurrealKV `Immediate` (one `fsync` per group) vs RocksDB's sync-on-every-write vs
SlateDB's commit-awaits-durability, and so on. ReDB, Fjall, LMDB and libmdbx were measured **only
without** synchronous durability, because with it their runs were too slow to finish.

### 14.2 Throughput, with synchronous durability (ops/s)

| Engine | Read | Create | Update | Delete | Scan |
| --- | --- | --- | --- | --- | --- |
| **SurrealKV** | 6,804,864 | 229,376 | 221,341 | 264,089 | 905,132 |
| RocksDB | 855,851 | 8,568 | 36,263 | 32,043 | 852,661 |
| SlateDB | 92,303 | 8,257 | 8,122 | 8,242 | 17,985 |

### 14.3 Throughput, without synchronous durability (ops/s)

| Engine | Read | Create | Update | Delete | Scan |
| --- | --- | --- | --- | --- | --- |
| **SurrealKV** | 6,992,767 | 321,715 | 274,172 | 307,034 | 896,366 |
| RocksDB | 846,293 | **603,452** | **608,494** | **739,357** | 851,848 |
| SlateDB | 96,397 | 6,314 | 6,352 | 6,380 | 26,187 |
| Fjall | 992,874 | 62,608 | 64,059 | 2,676 | 132,698 |
| ReDB | 779,262 | 35,171 | 35,805 | 65,686 | 561,837 |
| LMDB | 1,461,261 | 127,045 | 163,099 | 157,965 | **1,192,772** |
| libmdbx | 613,904 | 24,378 | 5,026 | 11,521 | 384,128 |

Ratios (SurrealKV ÷ comparator; below 1 means the comparator wins):

| Against | Read | Create | Update | Delete | Scan |
| --- | --- | --- | --- | --- | --- |
| *With sync* — RocksDB | 8.0× | 26.8× | 6.1× | 8.2× | 1.1× |
| *With sync* — SlateDB | 73.7× | 27.8× | 27.3× | 32.0× | 50.3× |
| *No sync* — RocksDB | 8.3× | **0.53×** | **0.45×** | **0.42×** | 1.1× |
| *No sync* — LMDB | 4.8× | 2.5× | 1.7× | 1.9× | **0.75×** |
| *No sync* — ReDB | 9.0× | 9.1× | 7.7× | 4.7× | 1.6× |
| *No sync* — Fjall | 7.0× | 5.1× | 4.3× | 115× | 6.8× |
| *No sync* — libmdbx | 11.4× | 13.2× | 54.6× | 26.7× | 2.3× |
| *No sync* — SlateDB | 72.5× | 51.0× | 43.2× | 48.1× | 34.2× |

### 14.4 Scans (ops/s, no sync) and memory

`Limit` = first 100 pairs, `Keys` = keys only, `Skip` = discard 5,000 then return 100, `Count` = visit
every key.

| Engine | Limit | Keys | Skip | Keys, skip | Count |
| --- | --- | --- | --- | --- | --- |
| **SurrealKV** | 896,366 | **2,313,942** | 96,365 | 98,612 | 402 |
| RocksDB | 851,848 | 851,580 | 35,539 | 35,902 | 159 |
| LMDB | **1,192,772** | 1,195,166 | **549,220** | **552,090** | **902** |
| libmdbx | 384,128 | 260,769 | 183,361 | 185,073 | 848 |
| ReDB | 561,837 | 571,629 | 44,438 | 44,462 | 249 |
| Fjall | 132,698 | 151,861 | 4,335 | 4,383 | 42.4 |
| SlateDB | 26,187 | 26,266 | 7,460 | 7,477 | 87 |

Process memory, sampled over the whole no-sync run: SurrealKV has the **lowest resting footprint**
(221 MiB, below ReDB's 257 MiB and SlateDB's 265 MiB) and a peak of 1.79 GiB — above RocksDB's 1.02
GiB, LMDB's 826 MiB and Fjall's 1.71 GiB, below ReDB's 2.17 GiB and SlateDB's 2.73 GiB. libmdbx in
`UtterlyNoSync` mode is an outlier at 40.98 GiB resting / 117.71 GiB peak, with a file that grew past
160 GB; the paper says it has not established why.

### 14.5 How to read all of that

The paper's own threats-to-validity section is unusually candid, and the honest summary is short:

- **Every figure is one run.** Run-to-run variation is not measured, so small differences (the 1.1×
  scan gap against RocksDB, for instance) carry no ordering information.
- **The dataset fits in RAM by a wide margin** — 500,000 small keys on a 128 GB machine, with a 256
  MiB memtable and a cache sized from machine memory. **(analysis)** This means the read ratios are
  largely a comparison of *in-memory index plus per-operation transaction overhead* — ART in an arena
  vs skiplist vs memory-mapped B+tree — and say "little about reads that miss the cache and reach the
  disk", in the paper's words. Do not carry 8.3× into a working set larger than memory.
- **The synchronous-durability win is a group-commit win.** 26.8× on creates compares "one `fsync`
  per group of up to 4 MiB, with 128 clients committing concurrently" against an adapter that syncs
  **every write**. That is the amortisation chapter 14 §14 describes, measured at the concurrency
  where it pays most; the paper explicitly has not measured how much of the difference grouping
  accounts for. (It also flags, without explaining, that RocksDB's synchronous creates — 8,568 — are
  a fraction of its synchronous updates — 36,263.)
- **Without `fsync` to amortise, SurrealKV loses to RocksDB by 1.9–2.4× on writes**, and the design
  says why: there is nothing left to share, and one flusher still applies every group to the
  memtable, so insertion runs on one core. Whether that is the actual limit is unmeasured.
- **The no-sync configurations are not equivalent across engines**, so that whole table measures
  speed and not safety. SurrealKV's `Eventual` survives a process crash; ReDB's `None` does not
  persist until a later durable commit; LMDB and libmdbx document that their no-sync flags let a
  machine crash corrupt the database or roll it back arbitrarily. Nobody tested what survives.
- **Scans are the honest loss.** LMDB leads on whole-entry limit scans (1.3×), skip scans (5.7×) and
  counts (2.2×); libmdbx leads on skip scans (1.9×) and counts (2.1×). SurrealKV leads LMDB only on
  key-only limit scans (1.9×). §7's linear-scan cursor and per-step copying are the structural
  reason.
- **Moving `Immediate` → `Eventual` buys only 1.4× creates, 1.2× updates, 1.2× deletes** (from
  separate runs) — a small gap that is itself evidence that grouping is doing its job.
- **Eight measurements the paper names and did not run**: throughput as cores are added; the effect
  of group size; value separation on vs off; sustained writes (stall gate × compaction); datasets
  larger than memory; mixed read/write workloads and long transactions; versioned vs unversioned
  trees; and **ablations that remove one design decision at a time**. There is also no latency
  distribution under load.

**(analysis)** So the defensible claim from this evaluation is narrow and still interesting: *on one
machine, for a cache-resident working set, SurrealKV's point reads and its concurrent durable writes
are far ahead of RocksDB's, its unsynced writes are behind, and its scans are behind single-tree
engines.* Everything about scaling, larger-than-memory data and versioning is open.

### 14.6 Reproducing it

The harness is open source **(paper, Appendix A)**:

```bash
CRUD_BENCH_CONFIG=config/memory.toml \
  ./run.sh -d surrealkv --no-build --optimised -s 500000 -c 128 -t 48
# add -sync for the synchronous-durability configuration
```

---

## 15. Design lineage: LevelDB, RocksDB, Pebble

**(paper §9)** What it shares with LevelDB: the leveled shape, an overlapping level 0 with a
four-table trigger, and one log segment per memtable generation. With Pebble: a fixed-size arena, so
no batch can grow a memtable without bound.

Where it differs from LevelDB:

| | LevelDB | SurrealKV |
| --- | --- | --- |
| Table size | ~2 MB | ~64 MiB |
| Level 1 target | 10 MB (10^L MB per level) | 256 MiB (×10 per level) |
| Memtable / log | One log; at ~4 MB it becomes a table | 100 MiB arena; one segment per generation |
| Compaction choice | One file of level L, round-robin through the key space | Highest-score level, largest table, weighted by deletions |
| Delete markers | Dropped once no deeper level overlaps the key | Hard delete markers dropped **only at the last level** |
| Manifest | An append-only log of edits + `CURRENT` | **One file, replaced atomically**, with a checksum trailer |
| Tables at open | Opened lazily (preferred by the notes) | **Every table the manifest lists** |
| Slowing writers | Proposed as level 0 fills | 100 µs per level-0 table above 8, stall at 12 |

Where it differs from RocksDB and Pebble: **ART memtable** instead of skiplist; an oversized batch
goes **straight to a level-0 table** (RocksDB grows the memtable; Pebble adds the sorted batch as a
layer); **no background pacing at all** (RocksDB has an optional rate limiter, Pebble paces flushes
and compactions against compaction debt); and throttling by a fixed per-table delay rather than
RocksDB's approach of artificial delays as thresholds approach.

**(analysis)** Two of SurrealKV's divergences are *more conservative* than the references — the
last-level-only delete-marker rule (space for simplicity) and opening every table at startup
(front-loaded work, slower opens on a large database, predictable reads afterwards). It is worth
noticing that a 2026 engine chose "raise every size and simplify the manifest" over LevelDB's
incremental edits: bigger files mean fewer of them and burstier compactions, which LevelDB's own
notes name as the trade.

---

## 16. Limitations worth weighing before you rely on it

Condensed from the paper's own list **(paper §16)** — this is the section to read first in any
engine's paper:

- **Snapshot isolation only.** No serializable mode; scans are not validated; write skew is possible;
  a range delete is validated only on its start key.
- **Versioned trees**: a later write can carry an earlier time; a read by time reads every version of
  its key; `history` holds one key's versions in memory; versions written while unversioned read at
  the time of the versions before them; **no benchmark of a versioned tree exists**.
- **Single flusher.** Memtable apply and the group's log append both run on one task.
- **Single compaction.** One at a time, unpaced.
- **Open write transactions** hold back commit-ring retirement, and the overflow is **not bounded**.
- **Value log**: prefix-only reclamation, checksums off by default.
- **Recovery**: damage in the middle of a segment, or in an older segment, stops the open; the only
  way past is salvage mode, which discards everything after the valid prefix.
- **Checkpoints**: a checkpoint flushes first and copies no log; a restore is not atomic and waits for
  running compactions.
- **Encryption**: chosen at creation only, cannot be added later; does not hide file names, sizes or
  access timing; on-disk formats are not frozen.
- **Memory**: the memtable size budgets the arena, not the bytes of keys and values; no engine-wide
  memory limit.
- **Platforms**: builds for WebAssembly, but a tree does not persist in browser storage yet.

Open questions the authors name: a concurrent memtable apply, a rewriting value-log collector,
parallel compaction, a serializable mode, deterministic simulation testing, and the missing ablations.

---

## 17. What to steal for your own engine

If you are doing LABS.md's capstone **C1 — A crash-safe key-value engine**, this paper is a template
for three things worth copying:

1. **A single-writer commit pipeline is easier to get right than a leader election.** One standing
   task that numbers, logs, applies and publishes gives you ordered visibility with no
   leader/follower handshake, and one `fsync` per burst for free. Measure the ceiling it imposes
   (single-core memtable insert) before you decide it is too slow — SurrealKV's own numbers show the
   ceiling only matters when `fsync` is out of the picture.
2. **Published write sets in a ring beat a lock table for OCC**, as long as you accept that validation
   cost scales with concurrent commit volume and that undecided entries must count as conflicts.
   Write down the overflow bound; SurrealKV did not, and it became a limitation.
3. **Versioning can be free in structure if your versions are already ordered.** Put the time in the
   value, keep versions ordered by sequence number, and let compaction's snapshot rule decide
   retention. The price is that time becomes a *property*, not an *order* — and every "as-of" read
   degrades with history depth, because there is no time index.

And three things to do differently:

1. **Give the value log a rewriting collector**, or accept unbounded space amplification from one
   long-lived large value.
2. **Make the differential test run concurrent commits.** The novel component deserves the strongest
   test, not the weakest.
3. **Pace background I/O.** Unpaced flush and compaction plus a hard stall at two immutable memtables
   is a latency cliff waiting for a sustained write phase (ch. 13 §6).

---

## 18. Interview questions

**Q1. Why would a radix tree beat a skiplist as a memtable, and where would it not?**
Lookup cost depends on key length rather than key count, adaptive nodes (4/16/48/256 children) stay
cache-friendly, and per-key version chains make history a chain walk. It helps point reads most. It
does not obviously help ordered scans, which walk a trie instead of a level-0 linked list, and the
arena it needs is fixed, so the memtable rotates instead of growing.

**Q2. SurrealKV's synchronous creates are 26.8× RocksDB's, but its unsynced creates are 0.53×. What
single mechanism explains both?**
Group commit by a single flusher. One `fsync` serves a whole group, so with 128 concurrent clients the
durable path amortises the only expensive operation. With no `fsync` there is nothing to amortise, and
the same design hands you a single-threaded memtable apply, which RocksDB's concurrent apply beats.

**Q3. A transaction commits and gets an error because the log `fsync` failed. Can the write still
appear after a restart?**
Yes. The segment is poisoned and the next group replaces it, and the paper states that a commit that
returned an error can still take effect at the next open. Clients must treat a commit error as an
indeterminate outcome and re-read, not as a definite "did not happen".

**Q4. Why can an aborted transaction abort another one?**
Validation treats a ring entry whose owner has not yet decided as a conflict. An earlier commit that
later aborts can therefore abort a later transaction for nothing — a false abort, the price of not
waiting on undecided predecessors.

**Q5. You enable value separation for 4 KB documents. Two months later the data directory is 400 GB
for 20 GB of live data. Why?**
Value-log files are reclaimed only as a prefix: a file is deleted only when it lies wholly below the
smallest file any live table or unflushed memtable references. One long-lived value pins every later
file, and there is no rewriting collector.

**Q6. Why are SurrealKV's scans behind LMDB's, by design rather than by accident?**
LMDB walks one memory-mapped copy-on-write B+tree with zero-copy reads. SurrealKV merges `s` sources
(memtables, every overlapping table in every level) with a cursor that compares **all** sources at
each step, copies blocks into its iterator, and re-scans from the previous restart point on a backward
step.

**Q7. What does "range deletes are cheap until someone uses them" mean here?**
Each table carries an atomic flag, so collecting the range-delete horizon costs nothing while no table
has range deletes. Once any table does, every point read pays a pass over every table's metadata.

**Q8. The engine keeps every version with versioning on. Why doesn't it need a time index?**
Versions are ordered by the sequence numbers that order everything else, and the time is 8 bytes
inside the value. `get_at` is a rule applied over the versions of a key, not a seek. The cost is that
a read by time reads **every** version of the key its snapshot can see.

**Q9. Which part of the design is least covered by the test suite, and why does that matter?**
The commit ring. The differential test against the reference model never runs concurrent commits, so
wrong commit verdicts under contention are covered by argument (the five invariants) and crash suites
rather than by differential testing — and the ring is the engine's most novel component.

**Q10. The paper reports 8.3× faster point reads than RocksDB. What would you need to know before
believing that for your workload?**
Whether your working set fits in memory. 500,000 keys on a 128 GB machine with a 256 MiB memtable and
a machine-sized cache measures an in-memory index plus transaction overhead; the paper says the
figures say little about reads that miss the cache. Also: single run, one machine, value separation
on, versioning off.

---

## 19. Sources

- Farhan Khan, Tobie Morgan Hitchcock, Stu Schwartz. *SurrealKV: A Versioned LSM Engine with
  Radix-Tree Memtables and a Commit Ring.* SurrealDB, October 2026 (describes SurrealKV 1.0.0) —
  <https://surrealdb.com/static/whitepapers/surrealkv.pdf>. Every number and mechanism above is from
  this paper; section references in the text are its sections.
- Patrick O'Neil, Edward Cheng, Dieter Gawlick, Elizabeth O'Neil. *The Log-Structured Merge-Tree
  (LSM-Tree).* Acta Informatica 33(4), 1996 — <https://doi.org/10.1007/s002360050048>.
- Fay Chang et al. *Bigtable: A Distributed Storage System for Structured Data.* OSDI '06 —
  <https://www.usenix.org/conference/osdi-06/bigtable-distributed-storage-system-structured-data>.
- *LevelDB implementation notes* (`doc/impl.md`) —
  <https://github.com/google/leveldb/blob/main/doc/impl.md>.
- Siying Dong, Andrew Kryczka, Yanqin Jin, Michael Stumm. *Evolution of Development Priorities in
  Key-Value Stores Serving Large-Scale Applications: The RocksDB Experience.* FAST '21 —
  <https://www.usenix.org/conference/fast21/presentation/dong>.
- *Pebble vs RocksDB: Implementation differences* (`docs/rocksdb.md`, "Commit Pipeline") —
  <https://github.com/cockroachdb/pebble/blob/master/docs/rocksdb.md>.
- Viktor Leis, Alfons Kemper, Thomas Neumann. *The Adaptive Radix Tree: ARTful Indexing for
  Main-Memory Databases.* ICDE 2013 — <https://doi.org/10.1109/ICDE.2013.6544812>.
- Lanyue Lu et al. *WiscKey: Separating Keys from Values in SSD-Conscious Storage.* FAST '16 —
  <https://www.usenix.org/conference/fast16/technical-sessions/presentation/lu>.
- Hal Berenson et al. *A Critique of ANSI SQL Isolation Levels.* SIGMOD '95 —
  <https://doi.org/10.1145/223784.223785>.
- Howard Chu. *MDB: A Memory-Mapped Database and Backend for OpenLDAP.* LDAPCon 2011 —
  <https://www.openldap.org/pub/hyc/mdb-paper.pdf>.
- Jingyu Zhou et al. *FoundationDB: A Distributed Unbundled Transactional Key Value Store.*
  SIGMOD '21 (deterministic simulation testing, §4) — <https://doi.org/10.1145/3448016.3457559>.
- The engines measured: <https://slatedb.io>, <https://fjall-rs.github.io>, <https://www.redb.org>,
  <https://sourcecraft.dev/dqdkfa/libmdbx>.
