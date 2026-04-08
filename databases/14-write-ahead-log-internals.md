# Write-Ahead Log (WAL) Internals: A Deep Dive

The Write-Ahead Log is the single most critical component for database durability and crash recovery. Every OLTP database -- PostgreSQL, MySQL/InnoDB, SQLite, Oracle, SQL Server -- relies on some form of WAL to guarantee that committed transactions survive crashes. This document goes deep into the internals: the full ARIES recovery algorithm, checkpoint mechanics, LSN arithmetic, WAL buffer management, segment lifecycle, and the modern architectural innovations that have reimagined what a WAL can be.

Prerequisites: familiarity with storage engine fundamentals (pages, buffer pool, fsync) covered in `01-storage-engine-fundamentals.md` and basic transaction concepts from `05-transactions-and-concurrency.md`.

---

## Table of Contents

1. [Why WAL Exists](#1-why-wal-exists)
2. [LSN Arithmetic and the WAL Address Space](#2-lsn-arithmetic-and-the-wal-address-space)
3. [WAL Record Anatomy](#3-wal-record-anatomy)
4. [The WAL Buffer](#4-the-wal-buffer)
5. [ARIES: The Full Recovery Algorithm](#5-aries-the-full-recovery-algorithm)
6. [Checkpoints](#6-checkpoints)
7. [Compensation Log Records (CLRs) and Nested Aborts](#7-compensation-log-records-clrs-and-nested-aborts)
8. [WAL Segment Management and Lifecycle](#8-wal-segment-management-and-lifecycle)
9. [WAL Compression](#9-wal-compression)
10. [Alternatives to WAL](#10-alternatives-to-wal)
11. [WAL in LSM-Tree Engines](#11-wal-in-lsm-tree-engines)
12. [WAL-Based Replication](#12-wal-based-replication)
13. [Modern WAL Architectures](#13-modern-wal-architectures)
14. [WAL Performance Tuning and Monitoring](#14-wal-performance-tuning-and-monitoring)
15. [Real-World Failure Modes](#15-real-world-failure-modes)

---

## 1. Why WAL Exists

### The Fundamental Problem

Databases modify pages in a buffer pool (RAM). Flushing every modified page to disk on every transaction commit would be catastrophically slow because data pages are scattered randomly across the disk. A single transaction might touch pages in 10 different locations -- that is 10 random writes.

The WAL solves this with one insight: **convert random writes into a single sequential write**.

```
THE PROBLEM WAL SOLVES:

  Without WAL (force every page on commit):

    Transaction modifies pages 7, 42, 189, 3001
    COMMIT → must flush all 4 pages to their on-disk locations

    ┌──────────┐     Random I/O to 4 locations
    │ Buffer   │ ──► Page 7    (seek 2ms)
    │ Pool     │ ──► Page 42   (seek 2ms)
    │          │ ──► Page 189  (seek 2ms)
    │          │ ──► Page 3001 (seek 2ms)
    └──────────┘     Total: ~8ms + write time

  With WAL (no-force, steal):

    Transaction modifies pages 7, 42, 189, 3001
    COMMIT → append 4 WAL records sequentially, single fsync

    ┌──────────┐     Sequential I/O to WAL
    │ Buffer   │ ──► WAL: [rec1][rec2][rec3][rec4]  (one fsync, ~0.1ms NVMe)
    │ Pool     │
    │          │     Dirty pages flushed later by background writer
    └──────────┘     (at its own pace, in batch, optimized order)
```

### The No-Force / Steal Policy

WAL enables the most performant buffer pool policy:

```
BUFFER POOL POLICIES:

                    Force                        No-Force
                    (flush on commit)            (flush later)
  ┌─────────────────────────────────────────────────────────────┐
  │                                                              │
  │  No-Steal       Simplest. No undo needed,   No-Force +      │
  │  (don't evict   no redo needed.             No-Steal.        │
  │   uncommitted   But: must hold ALL dirty    Still needs redo  │
  │   pages)        pages in memory until        but not undo.   │
  │                 commit. Not practical for                     │
  │                 large transactions.                           │
  │                                                              │
  │  Steal          Force + Steal. Needs undo   ARIES approach.  │
  │  (can evict     (page on disk may have      Needs both undo  │
  │   uncommitted   uncommitted data) but no     AND redo.       │
  │   pages)        redo. Unusual combination.  Most performant.  │
  │                                                              │
  └─────────────────────────────────────────────────────────────┘

  ARIES uses No-Force + Steal:
    - No-Force: don't flush data pages on commit (only WAL). Fast commits.
    - Steal: can evict dirty pages with uncommitted changes. Flexible memory.
    - Consequence: need REDO (committed changes might not be on data pages)
                   need UNDO (uncommitted changes might be on data pages)
    - WAL records carry both before-images (for undo) and after-images (for redo).
```

### The Three WAL Rules (Recap)

These rules are covered in `01-storage-engine-fundamentals.md` but are restated here because everything in this document builds on them:

```
RULE 1 — Write-Ahead:
  Before flushing dirty page P to disk,
  all WAL records with LSN ≤ page_LSN(P) must be on stable storage.

RULE 2 — Commit:
  A transaction is committed only after its COMMIT record
  reaches stable storage (fsync'd WAL).

RULE 3 — Redo:
  WAL records carry enough information to redo the operation
  on the affected page, restoring the after-image.

  (ARIES adds): WAL records also carry enough information to UNDO
  the operation, restoring the before-image.
```

---

## 2. LSN Arithmetic and the WAL Address Space

### What Is an LSN?

The **Log Sequence Number (LSN)** is a monotonically increasing identifier for each WAL record. It serves as both a unique ID and a physical address into the WAL stream. In most implementations, the LSN is the byte offset from the start of the WAL.

```
WAL AS A BYTE STREAM:

  Byte offset:   0         128        300       512       700
                  │          │          │         │         │
                  ▼          ▼          ▼         ▼         ▼
  WAL stream:   [Record 1 ][Record 2  ][Rec 3  ][Record 4][Rec 5...]
                  LSN=0      LSN=128    LSN=300  LSN=512   LSN=700

  PostgreSQL:
    LSN format: 64-bit value, displayed as "segment/offset"
    Example: 0/1A3E7F00 → segment 0, offset 0x1A3E7F00
    Each WAL segment = 16 MB (configurable at initdb with --wal-segsize)
    LSN increases across segments: segment boundary at every 16 MB

  InnoDB:
    LSN format: 64-bit byte offset into the redo log circular buffer
    Example: LSN = 38204736
    Wraps around when the circular redo log files are reused
```

### The Key LSN Pointers

Every WAL-based system maintains several critical LSN pointers. Understanding their relationships is essential for reasoning about crash recovery.

```
LSN POINTERS:

  ┌─────────────────────────────────────────────────────────────────────┐
  │                        WAL STREAM                                    │
  │                                                                      │
  │  ◄──── already recycled ────►◄────── active WAL ──────────────────► │
  │                               │                                      │
  │              checkpoint        │         flushed       written        │
  │              LSN               │         LSN           LSN           │
  │               │                │          │              │            │
  │               ▼                ▼          ▼              ▼            │
  │  ─────────[CKPT]─────────[records]────[records]─────[records]──►    │
  │                                                          │           │
  │                                               insert LSN │           │
  │                                               (next write position)  │
  └─────────────────────────────────────────────────────────────────────┘

  1. pageLSN (per page):
     The LSN of the most recent WAL record that modified this page.
     Stored in the page header. Used during recovery to determine
     whether a redo record needs to be applied:
       if WAL_record.LSN ≤ page.pageLSN → skip (already applied)
       if WAL_record.LSN > page.pageLSN → must redo

  2. flushedLSN (global):
     The LSN up to which the WAL has been fsync'd to disk.
     All records with LSN ≤ flushedLSN are durable.
     The WAL rule requires: page flush only if pageLSN ≤ flushedLSN.

  3. checkpointLSN (global):
     The LSN of the last completed checkpoint record.
     Recovery starts scanning from here (not from the beginning of WAL).

  4. redoLSN (per checkpoint):
     The earliest LSN that might need to be redone after a crash.
     Equals the minimum recLSN across all dirty pages at checkpoint time.
     Recovery REDO phase starts scanning from redoLSN.

  5. recLSN (per dirty page, in the dirty page table):
     The LSN of the first WAL record that dirtied this page since
     it was last flushed. This is the earliest point from which
     redo might be needed for this specific page.

  6. lastLSN (per transaction, in the transaction table):
     The LSN of the most recent WAL record written by this transaction.
     Used to walk the undo chain backwards during abort/recovery.

  7. prevLSN (per WAL record):
     The LSN of the previous WAL record for the same transaction.
     Forms a backward-linked list for undo traversal.
```

### LSN Comparison During Recovery

```
REDO DECISION FOR A SINGLE WAL RECORD:

  Given WAL record R with R.LSN and R.pageID:

  1. Is R.pageID in the dirty page table?
     NO  → skip (page was flushed before checkpoint or never dirtied)
     YES → continue

  2. Is R.LSN < dirty_page_table[R.pageID].recLSN?
     YES → skip (this record was applied before the page was last flushed)
     NO  → continue

  3. Read page P from disk. Is R.LSN ≤ P.pageLSN?
     YES → skip (page on disk already has this change)
     NO  → REDO this record (apply R's changes to P)

  This three-level filtering avoids unnecessary disk reads:
    - Level 1: dirty page table eliminates clean pages (no I/O)
    - Level 2: recLSN eliminates old records (no I/O)
    - Level 3: pageLSN on the actual page (requires I/O, but only for true candidates)
```

---

## 3. WAL Record Anatomy

### Record Types

```
WAL RECORD TYPES (ARIES-based systems):

  ┌──────────────────┬─────────────────────────────────────────────────┐
  │ Type              │ Purpose                                         │
  ├──────────────────┼─────────────────────────────────────────────────┤
  │ UPDATE           │ Page modification. Carries before-image (undo)  │
  │                  │ and after-image (redo). The core record type.   │
  ├──────────────────┼─────────────────────────────────────────────────┤
  │ INSERT           │ New tuple added to a page. After-image only.    │
  │                  │ Undo = delete the tuple.                        │
  ├──────────────────┼─────────────────────────────────────────────────┤
  │ DELETE           │ Tuple removed. Before-image for undo.           │
  │                  │ Redo = remove tuple. Undo = re-insert.          │
  ├──────────────────┼─────────────────────────────────────────────────┤
  │ COMMIT           │ Transaction committed. No page data.            │
  │                  │ Once this is on stable storage, txn is durable. │
  ├──────────────────┼─────────────────────────────────────────────────┤
  │ ABORT            │ Transaction aborted. Triggers undo.             │
  ├──────────────────┼─────────────────────────────────────────────────┤
  │ CLR              │ Compensation Log Record. Written during undo.   │
  │ (Compensation)   │ Records the undo action so it won't be undone   │
  │                  │ again if we crash during recovery. See §7.      │
  ├──────────────────┼─────────────────────────────────────────────────┤
  │ BEGIN_CHECKPOINT │ Start of a fuzzy checkpoint.                    │
  │ END_CHECKPOINT   │ End of checkpoint. Contains dirty page table    │
  │                  │ and active transaction table snapshots.         │
  ├──────────────────┼─────────────────────────────────────────────────┤
  │ PREPARE          │ 2PC: transaction has voted YES but not yet      │
  │                  │ committed. Must survive crash for coordinator.  │
  └──────────────────┴─────────────────────────────────────────────────┘
```

### Physical vs. Logical vs. Physiological Records

This is covered briefly in `01-storage-engine-fundamentals.md`. Here is the deeper treatment with implementation trade-offs:

```
LOGGING GRANULARITY SPECTRUM:

  ◄── Physical ──────────────── Physiological ──────────────── Logical ──►

  PHYSICAL:
    Record: "Page 42, offset 128, change bytes [old] → [new]"
    Redo: memcpy(page+128, new_bytes, len)
    Undo: memcpy(page+128, old_bytes, len)

    ✓ Trivially idempotent (can apply repeatedly, same result)
    ✓ Recovery is just byte copying -- no logic needed
    ✗ Large records (full byte diffs)
    ✗ Breaks if page is reorganized (e.g., compaction within page)

  PHYSIOLOGICAL (PostgreSQL, most modern systems):
    Record: "Page 42, operation=heap_insert, data=(tuple bytes)"
    Redo: call heap_insert(page_42, tuple_bytes)
    Undo: call heap_delete(page_42, item_pointer)

    ✓ Compact records (operation + minimal data)
    ✓ Page-level idempotent (pageLSN check ensures apply-once)
    ✓ Tolerates within-page reorganization
    ✗ Redo requires the operation's code to be available
    ✗ Not idempotent at the byte level -- needs LSN guard

  LOGICAL:
    Record: "INSERT INTO users VALUES (42, 'Alice')"
    Redo: re-execute the SQL statement
    Undo: DELETE FROM users WHERE id = 42

    ✓ Very compact records
    ✓ Works across schema changes
    ✗ Redo is slow (full SQL execution)
    ✗ NOT idempotent without additional machinery
    ✗ Concurrent modifications can change which page is affected
    ✗ Not used for page-level WAL (used in logical replication only)

WHY PHYSIOLOGICAL WINS:
  The key insight is that physiological logging is physical at the page
  level (guaranteeing idempotency via pageLSN) but logical within the
  page (keeping records compact and tolerating page-internal changes).

  PostgreSQL WAL record for a heap insert:
  ┌────────────┬──────────┬───────────┬────────────────────────────┐
  │ Header     │ Block    │ Operation │ Tuple data                  │
  │ (LSN,      │ Ref      │ ID        │ (the actual row bytes,      │
  │  TxnID,    │ (rel,    │ (e.g.,    │  minus columns derivable    │
  │  prevLSN)  │  fork,   │  XLOG_    │  from the schema)           │
  │            │  blkno)  │  HEAP_    │                             │
  │            │          │  INSERT)  │                             │
  └────────────┴──────────┴───────────┴────────────────────────────┘
```

---

## 4. The WAL Buffer

### Architecture

The WAL buffer is a region of shared memory that sits between transaction execution and the WAL files on disk. WAL records are first written into this buffer, then periodically flushed to disk.

```
WAL BUFFER ARCHITECTURE:

  Transaction threads          WAL Buffer (shared memory)          Disk
  ────────────────────         ──────────────────────────          ──────

  Txn 1: INSERT ──────┐       ┌──────────────────────────┐
                       │       │  [slot 0] WAL rec (T1)    │
  Txn 2: UPDATE ──────┼──►    │  [slot 1] WAL rec (T2)    │ ──fsync──► WAL
  Txn 3: DELETE ──────┤       │  [slot 2] WAL rec (T3)    │            file
  Txn 4: UPDATE ──────┘       │  [slot 3] WAL rec (T1)    │
                               │  [slot 4] (empty)         │
                               │  ...                       │
                               └──────────────────────────┘
                                     ▲            ▲
                                     │            │
                               insertPos     flushPos

  PostgreSQL specifics:
    - WAL buffer = wal_buffers (default: -1, auto-sized to 1/32 of shared_buffers,
      min 64KB, max one WAL segment = 16MB)
    - Organized as a circular buffer of 8KB WAL pages
    - WAL insertion uses lightweight locks (since PG 9.4: lock-free with atomics)
    - Multiple backends can insert concurrently into different WAL pages

  InnoDB specifics:
    - Log buffer = innodb_log_buffer_size (default 16MB)
    - Single circular buffer
    - Written by user threads, flushed by log writer thread (since 8.0.11)
    - Group commit uses a "link_buf" lock-free data structure
```

### Concurrent WAL Insertion (PostgreSQL)

```
LOCK-FREE WAL INSERTION (PostgreSQL ≥ 9.4):

  Problem: multiple backends writing WAL simultaneously.
  Old approach: single WAL insert lock → bottleneck at high concurrency.

  New approach: reserve space with atomic fetch-and-add.

  Backend 1:                         Backend 2:
  ─────────                          ─────────
  1. Calculate record size: 96B      1. Calculate record size: 128B
  2. atomic FAA(&insertPos, 96)      2. atomic FAA(&insertPos, 128)
     → gets position 1000               → gets position 1096
  3. Copy record to WAL buffer       3. Copy record to WAL buffer
     at offset 1000                     at offset 1096
  4. Mark slot as "written"          4. Mark slot as "written"

  The flush process must wait until all slots up to the flush point
  are marked "written" (no holes). A hole means a backend reserved
  space but hasn't finished copying yet.

  ┌───────────────────────────────────────────────────────────────┐
  │ WAL Buffer:                                                    │
  │                                                                │
  │ [written][written][written][HOLE][written][written][empty]...  │
  │                            ▲                                   │
  │                            │                                   │
  │                   Cannot flush past here until                  │
  │                   this backend finishes copying                 │
  └───────────────────────────────────────────────────────────────┘
```

### When the WAL Buffer Is Flushed

```
WAL BUFFER FLUSH TRIGGERS:

  1. Transaction COMMIT
     The committing backend (or a dedicated WAL writer) flushes
     all WAL up to and including the COMMIT record's LSN.

  2. WAL buffer full
     When insertPos wraps around and catches up to flushPos,
     backends must flush before they can insert more records.

  3. Background WAL writer
     PostgreSQL: walwriter process wakes every wal_writer_delay (200ms default)
       and flushes whatever has accumulated.
     InnoDB: log_writer thread runs continuously, batching flushes.

  4. Buffer pool page eviction
     Before the buffer pool can flush a dirty page with pageLSN = X,
     the WAL must be flushed up to LSN X (the WAL rule).
     If flushedLSN < X, the eviction triggers a WAL flush first.

  5. Checkpoint
     A checkpoint implies all WAL up to the checkpoint LSN is durable.

  Flush I/O pattern:
    WAL flush is always sequential (append to end of WAL file).
    This is why WAL is so fast: one sequential write + one fsync.
    On NVMe: sequential write + fsync ≈ 10-50 μs.
    On HDD:  sequential write + fsync ≈ 2-10 ms (rotational latency).
```

---

## 5. ARIES: The Full Recovery Algorithm

ARIES (Algorithm for Recovery and Isolation Exploiting Semantics) is the gold-standard recovery algorithm, published by Mohan et al. at IBM in 1992. PostgreSQL, InnoDB, SQL Server, and DB2 all implement variants of ARIES. The algorithm has three phases: **Analysis**, **Redo**, **Undo**.

### Pre-Crash State Example

```
SCENARIO BEFORE CRASH:

  Transactions:
    T1: UPDATE page 5 (LSN=10), UPDATE page 8 (LSN=20), COMMIT (LSN=30)
    T2: UPDATE page 3 (LSN=15), UPDATE page 7 (LSN=25) -- no commit
    T3: UPDATE page 5 (LSN=35), UPDATE page 9 (LSN=40) -- no commit

  Last checkpoint at LSN=12:
    Dirty Page Table at checkpoint:
      Page 5: recLSN=10
    Active Transaction Table at checkpoint:
      T1: lastLSN=10, status=active

  WAL on disk:
  ┌────┬─────────┬──────────────────────────────────┐
  │LSN │ Txn     │ Record                            │
  ├────┼─────────┼──────────────────────────────────┤
  │ 10 │ T1      │ UPDATE page 5, prev=nil           │
  │ 12 │ --      │ END_CHECKPOINT (dirty pages, txns) │
  │ 15 │ T2      │ UPDATE page 3, prev=nil           │
  │ 20 │ T1      │ UPDATE page 8, prev=10            │
  │ 25 │ T2      │ UPDATE page 7, prev=15            │
  │ 30 │ T1      │ COMMIT, prev=20                   │
  │ 35 │ T3      │ UPDATE page 5, prev=nil           │
  │ 40 │ T3      │ UPDATE page 9, prev=35            │
  └────┴─────────┴──────────────────────────────────┘

  Pages on disk (some flushed, some not):
    Page 3: pageLSN=0   (T2's update at LSN=15 NOT flushed)
    Page 5: pageLSN=10  (T1's update flushed, T3's at LSN=35 NOT flushed)
    Page 7: pageLSN=0   (T2's update at LSN=25 NOT flushed)
    Page 8: pageLSN=20  (T1's update flushed)
    Page 9: pageLSN=0   (T3's update at LSN=40 NOT flushed)

  ═══════════════════  CRASH  ═══════════════════
```

### Phase 1: Analysis

The Analysis phase scans the WAL forward from the last checkpoint to determine:
- Which transactions were active at crash time (need undo)
- Which pages might be dirty (need redo)

```
ANALYSIS PHASE:

  Start: read the last checkpoint record (LSN=12).
  Initialize from checkpoint:
    Dirty Page Table (DPT): {page 5: recLSN=10}
    Active Transaction Table (ATT): {T1: lastLSN=10, status=active}

  Scan forward from LSN=12:

  LSN=15 (T2 UPDATE page 3):
    ATT: add T2 (lastLSN=15, status=active)
    DPT: add page 3 (recLSN=15)  -- first time dirtied since checkpoint

  LSN=20 (T1 UPDATE page 8):
    ATT: T1.lastLSN = 20
    DPT: add page 8 (recLSN=20)

  LSN=25 (T2 UPDATE page 7):
    ATT: T2.lastLSN = 25
    DPT: add page 7 (recLSN=25)

  LSN=30 (T1 COMMIT):
    ATT: remove T1 (committed, no undo needed)
    DPT: unchanged

  LSN=35 (T3 UPDATE page 5):
    ATT: add T3 (lastLSN=35, status=active)
    DPT: page 5 already present, recLSN stays 10

  LSN=40 (T3 UPDATE page 9):
    ATT: T3.lastLSN = 40
    DPT: add page 9 (recLSN=40)

  ═══ End of WAL ═══

  ANALYSIS RESULTS:
  ┌──────────────────────────────────────────────┐
  │ Active Transaction Table (need UNDO):         │
  │   T2: lastLSN=25 (was active, uncommitted)    │
  │   T3: lastLSN=40 (was active, uncommitted)    │
  │                                                │
  │ Dirty Page Table (might need REDO):            │
  │   Page 3: recLSN=15                            │
  │   Page 5: recLSN=10                            │
  │   Page 7: recLSN=25                            │
  │   Page 8: recLSN=20                            │
  │   Page 9: recLSN=40                            │
  │                                                │
  │ redoLSN = min(recLSN) = 10                     │
  │ (REDO phase will start scanning from LSN=10)   │
  └──────────────────────────────────────────────┘
```

### Phase 2: Redo ("Repeating History")

ARIES redoes ALL actions (even those of uncommitted transactions) to bring the database to the exact state it was in at the moment of crash. This is called **repeating history**.

```
REDO PHASE:

  Scan forward from redoLSN=10.
  For each WAL record, apply the three-level filter:

  LSN=10 (T1 UPDATE page 5):
    Page 5 in DPT? YES (recLSN=10)
    LSN=10 ≥ recLSN=10? YES
    Read page 5 from disk: pageLSN=10
    LSN=10 ≤ pageLSN=10? YES → SKIP (already on disk)

  LSN=15 (T2 UPDATE page 3):
    Page 3 in DPT? YES (recLSN=15)
    LSN=15 ≥ recLSN=15? YES
    Read page 3 from disk: pageLSN=0
    LSN=15 ≤ pageLSN=0? NO → REDO ✓
    Apply T2's changes to page 3, set pageLSN=15

  LSN=20 (T1 UPDATE page 8):
    Page 8 in DPT? YES (recLSN=20)
    LSN=20 ≥ recLSN=20? YES
    Read page 8 from disk: pageLSN=20
    LSN=20 ≤ pageLSN=20? YES → SKIP (already on disk)

  LSN=25 (T2 UPDATE page 7):
    Page 7 in DPT? YES (recLSN=25)
    LSN=25 ≥ recLSN=25? YES
    Read page 7 from disk: pageLSN=0
    LSN=25 ≤ pageLSN=0? NO → REDO ✓
    Apply T2's changes to page 7, set pageLSN=25

  LSN=30 (T1 COMMIT):
    Not a page modification → skip

  LSN=35 (T3 UPDATE page 5):
    Page 5 in DPT? YES (recLSN=10)
    LSN=35 ≥ recLSN=10? YES
    Read page 5 (already in buffer from LSN=10 check): pageLSN=10
    LSN=35 ≤ pageLSN=10? NO → REDO ✓
    Apply T3's changes to page 5, set pageLSN=35

  LSN=40 (T3 UPDATE page 9):
    Page 9 in DPT? YES (recLSN=40)
    LSN=40 ≥ recLSN=40? YES
    Read page 9 from disk: pageLSN=0
    LSN=40 ≤ pageLSN=0? NO → REDO ✓
    Apply T3's changes to page 9, set pageLSN=40

  ═══ End of WAL ═══

  AFTER REDO: database is in the exact pre-crash state.
    Page 3: has T2's uncommitted changes (pageLSN=15)
    Page 5: has T3's uncommitted changes (pageLSN=35)
    Page 7: has T2's uncommitted changes (pageLSN=25)
    Page 8: has T1's committed changes (pageLSN=20)
    Page 9: has T3's uncommitted changes (pageLSN=40)
```

### Phase 3: Undo

The Undo phase rolls back all uncommitted transactions, restoring the database to a consistent state.

```
UNDO PHASE:

  Loser transactions (from Analysis): T2 (lastLSN=25), T3 (lastLSN=40)

  Build a priority queue (max-heap by LSN) of records to undo:
    ToUndo = {(T3, LSN=40), (T2, LSN=25)}

  Iteration 1: pop (T3, LSN=40) — UPDATE page 9
    Read WAL record at LSN=40
    Undo: restore page 9 to its before-image
    Write CLR: "undid LSN=40, undoNextLSN=35" (points to T3's prev record)
    Push (T3, LSN=35) into ToUndo

    ToUndo = {(T3, LSN=35), (T2, LSN=25)}

  Iteration 2: pop (T3, LSN=35) — UPDATE page 5
    Read WAL record at LSN=35
    Undo: restore page 5 to its before-image
    Write CLR: "undid LSN=35, undoNextLSN=nil" (T3 has no more records)
    T3 fully undone. Write ABORT record for T3.

    ToUndo = {(T2, LSN=25)}

  Iteration 3: pop (T2, LSN=25) — UPDATE page 7
    Read WAL record at LSN=25
    Undo: restore page 7 to its before-image
    Write CLR: "undid LSN=25, undoNextLSN=15"
    Push (T2, LSN=15) into ToUndo

    ToUndo = {(T2, LSN=15)}

  Iteration 4: pop (T2, LSN=15) — UPDATE page 3
    Read WAL record at LSN=15
    Undo: restore page 3 to its before-image
    Write CLR: "undid LSN=15, undoNextLSN=nil"
    T2 fully undone. Write ABORT record for T2.

    ToUndo = {} (empty → done)

  ═══ RECOVERY COMPLETE ═══

  Final state:
    Page 3: T2's changes undone (restored to original)
    Page 5: T3's changes undone (has T1's committed change only)
    Page 7: T2's changes undone (restored to original)
    Page 8: T1's committed changes preserved
    Page 9: T3's changes undone (restored to original)
```

### Why Redo ALL (Including Losers)?

```
WHY "REPEAT HISTORY"?

  Q: Why redo uncommitted transactions just to undo them?
  A: Three critical reasons:

  1. STEAL policy pages:
     Uncommitted data might have been flushed to disk before the crash.
     Some pages have the uncommitted changes, some don't.
     To get a CONSISTENT state to undo from, we must first redo
     everything to the pre-crash state.

  2. Logical undo correctness:
     Some undo operations are logical (e.g., "remove this index entry").
     They require the page to be in the exact state it was in at crash time.
     Without redo, the page might be in a half-updated state where the
     logical undo doesn't make sense.

  3. CLR correctness:
     If we crash during undo and have to recover again, the CLRs written
     during the first undo attempt must be redone. Repeating history
     handles this naturally.
```

---

## 6. Checkpoints

### Why Checkpoints Exist

Without checkpoints, recovery would need to replay the entire WAL from the beginning of time. Checkpoints bound recovery time by establishing a known-good starting point.

```
CHECKPOINT IMPACT ON RECOVERY:

  Without checkpoints:
    WAL: [rec][rec][rec]...[rec][rec][rec]...[rec][rec]  CRASH
         ◄──────── replay ALL of this ───────────────►
         Could be hours of WAL for a busy database.

  With checkpoints:
    WAL: [rec]...[CKPT]...[rec]...[CKPT]...[rec][rec]  CRASH
                                    ▲
                                    └── start replay here
         Only replay WAL since last checkpoint.
         Typically seconds to minutes.
```

### Fuzzy Checkpoints (Non-Blocking)

Modern databases use **fuzzy checkpoints** that do not stop transaction processing. This is critical for production availability.

```
FUZZY CHECKPOINT ALGORITHM:

  1. Write BEGIN_CHECKPOINT record to WAL
     (transactions continue running normally)

  2. Snapshot the dirty page table (DPT) and
     active transaction table (ATT) from shared memory

  3. Write END_CHECKPOINT record containing:
     - Copy of DPT (which pages are dirty and their recLSN)
     - Copy of ATT (which transactions are active and their lastLSN)
     - The LSN of the BEGIN_CHECKPOINT record

  4. Update the "master record" (or control file) to point to
     this checkpoint's END_CHECKPOINT LSN

  Important: NO pages are flushed during the checkpoint record writing.
  The checkpoint merely records the current state. Pages continue to be
  flushed by the background writer at its own pace.

  ┌──────────────────────────────────────────────────────────────────┐
  │ Timeline during fuzzy checkpoint:                                 │
  │                                                                   │
  │  T1: ───[update]──────[update]────[commit]──────────────────     │
  │  T2: ────────[update]───────────────[update]────────────────     │
  │  T3: ──────────────[begin]────[update]──────[update]────────     │
  │                                                                   │
  │  CKPT:        [BEGIN]──(snapshot DPT+ATT)──[END]                 │
  │                   ▲                           ▲                   │
  │                   │                           │                   │
  │            checkpoint starts           checkpoint ends            │
  │            (no blocking)               (no blocking)              │
  │                                                                   │
  │  Transactions T1, T2, T3 continue without interruption.          │
  └──────────────────────────────────────────────────────────────────┘

  Because transactions run during the checkpoint, the DPT/ATT snapshot
  may be slightly stale by the time END_CHECKPOINT is written.
  This is fine -- the Analysis phase corrects any staleness by
  scanning WAL records after the checkpoint.
```

### Sharp (Consistent) Checkpoints

```
SHARP CHECKPOINT (rarely used in production):

  1. Stop all transactions (acquire exclusive lock)
  2. Flush ALL dirty pages to disk
  3. Write CHECKPOINT record
  4. Release lock, resume transactions

  ✓ Recovery starts from the checkpoint -- no redo needed for
    anything before it. Extremely simple recovery.
  ✗ Blocks all transactions during the flush. For a database with
    GB of dirty pages, this can take minutes. Unacceptable in production.

  Used by: SQLite (in non-WAL mode), some embedded databases.
  NOT used by: PostgreSQL, InnoDB, Oracle, SQL Server.
```

### PostgreSQL Checkpoint Internals

```
POSTGRESQL CHECKPOINT PROCESS:

  Triggered by:
    - checkpoint_timeout (default 5 min) elapsed since last checkpoint
    - max_wal_size (default 1 GB) of WAL generated since last checkpoint
    - Manual: CHECKPOINT command
    - pg_start_backup() (for base backups)
    - Server shutdown (immediate checkpoint)

  The checkpointer process:
    1. Writes BEGIN_CHECKPOINT WAL record
    2. Collects list of all dirty buffers from shared_buffers
    3. Sorts dirty buffers by file and block number (minimize random I/O)
    4. Flushes dirty buffers over a spread-out period:
       checkpoint_completion_target (default 0.9) controls pacing.
       With 5-min timeout and 0.9 target: spread flushes over 4.5 min.
    5. Writes END_CHECKPOINT WAL record
    6. Updates pg_control with new checkpoint location
    7. Recycles old WAL segments (segments before the checkpoint)

  ┌─────────────────────────────────────────────────────────────────┐
  │ Checkpoint Spread:                                               │
  │                                                                  │
  │  I/O rate │                                                      │
  │           │     ┌───────────────────────────────┐                │
  │           │     │  Spread flushes evenly         │                │
  │           │     │  over 90% of checkpoint         │                │
  │           │     │  interval (4.5 minutes)         │                │
  │           └─────┴───────────────────────────────┴────► time      │
  │           0 min                              4.5 min  5 min      │
  │                                                       ▲          │
  │                                                  next checkpoint  │
  │                                                                  │
  │  Without spread (checkpoint_completion_target = 0):              │
  │  I/O rate │                                                      │
  │           │ ██                                                    │
  │           │ ██  ← I/O spike: flush everything immediately        │
  │           │ ██                                                    │
  │           └─██───────────────────────────────────────► time      │
  │                                                                  │
  └─────────────────────────────────────────────────────────────────┘
```

### InnoDB Checkpoint Internals

```
INNODB CHECKPOINT MECHANISM:

  InnoDB uses a more continuous approach called "fuzzy checkpointing"
  combined with an adaptive flushing algorithm.

  Key concepts:
    - Redo log is a FIXED-SIZE circular buffer (set of files)
    - Checkpoint LSN advances as dirty pages are flushed
    - If checkpoint LSN falls too far behind the write LSN,
      InnoDB must aggressively flush pages or stall writes

  ┌──────────────────────────────────────────────────────────────┐
  │ InnoDB Redo Log Circular Buffer:                              │
  │                                                               │
  │  ┌───────────────────────────────────────────────────┐       │
  │  │  [flushed/reusable] ║ [active redo] ║ [free space]│       │
  │  └───────────────────────────────────────────────────┘       │
  │                         ▲               ▲                     │
  │                    checkpoint_lsn    current_lsn              │
  │                                                               │
  │  Distance between checkpoint_lsn and current_lsn is the      │
  │  "checkpoint age". If it approaches the total redo log size,  │
  │  we must flush pages or stop accepting writes.                │
  │                                                               │
  │  Adaptive flushing:                                           │
  │    InnoDB monitors the checkpoint age and adjusts page        │
  │    flush rate to keep checkpoint age < 75% of redo log size.  │
  │    If age > ~75%: async flushing increases aggressively       │
  │    If age > ~90%: sync flushing (writes may stall)            │
  │                                                               │
  │  innodb_redo_log_capacity (MySQL 8.0.30+):                    │
  │    Total redo log size. Default: 100MB → set to 1-4 GB        │
  │    for write-heavy workloads.                                 │
  │                                                               │
  │  Pre-8.0.30:                                                  │
  │    innodb_log_file_size × innodb_log_files_in_group           │
  │    Typical: 1 GB × 2 = 2 GB total redo log space             │
  └──────────────────────────────────────────────────────────────┘
```

---

## 7. Compensation Log Records (CLRs) and Nested Aborts

### The Problem: Crash During Undo

What if the database crashes while undoing a transaction during recovery? Without CLRs, we would undo the same operations again -- but some undo operations are not idempotent (e.g., decrementing a counter).

```
CRASH DURING UNDO (without CLRs):

  Original: T1 does A=10→20, B=5→15, then crashes (no commit)

  Recovery attempt 1:
    Redo: apply A=10→20, B=5→15
    Undo: undo B=15→5 ... CRASH during undo of A

  Recovery attempt 2:
    Redo: apply A=10→20, B=5→15, undo B=15→5
    Undo: undo B=5→???  ← WRONG! B was already undone!

  Without CLRs, the undo of B happens twice. If the undo is
  "subtract 10 from B" rather than "set B to 5", the second undo
  produces B = -5. Corrupted.
```

### CLR Solution

```
COMPENSATION LOG RECORDS:

  A CLR is a special WAL record that says: "I undid record at LSN X."
  CLRs are REDO-ONLY -- they are never undone themselves.
  Each CLR has an undoNextLSN that points to the NEXT record to undo,
  skipping over the record that was just undone.

  CLR structure:
  ┌──────────┬──────────┬──────────┬─────────────┬──────────────────┐
  │ LSN      │ TxnID    │ prevLSN  │ undoNextLSN │ redo data         │
  │          │          │          │ (skip to)   │ (undo action)     │
  └──────────┴──────────┴──────────┴─────────────┴──────────────────┘

  CRASH-SAFE UNDO WITH CLRs:

  Original WAL (T1):
    LSN=10: UPDATE A (before=10, after=20), prevLSN=nil
    LSN=20: UPDATE B (before=5, after=15), prevLSN=10

  Recovery attempt 1:
    Redo phase: apply both records
    Undo phase:
      Undo LSN=20: set B=5, write CLR(LSN=30, undoNextLSN=10)
      ... CRASH ...

  Recovery attempt 2:
    Redo phase: apply LSN=10, LSN=20, AND LSN=30 (the CLR)
    After redo: A=20, B=5 (CLR already undid B)
    Undo phase:
      T1's lastLSN = 30 (the CLR)
      CLR at LSN=30 has undoNextLSN=10
      → skip to LSN=10, undo it: set A=10, write CLR(LSN=40, undoNextLSN=nil)
      T1 fully undone.

  The CLR chain:
    LSN=20 (update B) ←── CLR at LSN=30 says "B is undone, skip to 10"
    LSN=10 (update A) ←── CLR at LSN=40 says "A is undone, skip to nil"

  ┌──────────────────────────────────────────────────────────────────┐
  │ WAL record chain with CLRs:                                      │
  │                                                                   │
  │  Forward (redo) ──►                                               │
  │  [LSN=10: A=20] → [LSN=20: B=15] → [LSN=30: CLR] → [LSN=40: CLR]│
  │       ▲                                  │                │       │
  │       │              undoNextLSN ─────────┘                │       │
  │       │                                                    │       │
  │       └──────────── undoNextLSN ──────────────────────────┘       │
  │                                                                   │
  │  ◄── Backward (undo) follows undoNextLSN pointers,               │
  │       skipping already-undone records.                             │
  └──────────────────────────────────────────────────────────────────┘
```

### CLRs Enable Bounded Undo Work

```
KEY PROPERTY OF CLRs:

  No matter how many times the system crashes during recovery,
  the total undo work NEVER increases.

  Each crash-and-restart redoes the CLRs from previous attempts
  (cheap -- they're just applied during the redo phase), then
  the undo phase picks up where it left off.

  Worst case: N crashes during undo of a transaction with M operations.
  Total redo work grows (by the CLRs), but total undo work = M.
  Each restart undoes at most the remaining operations.

  Without CLRs: each crash could repeat all undo work.
  With CLRs: progress is always preserved.
```

---

## 8. WAL Segment Management and Lifecycle

### WAL File Organization

```
WAL SEGMENT FILES (PostgreSQL):

  WAL is stored as a sequence of fixed-size segment files.

  Default segment size: 16 MB (configurable at initdb: --wal-segsize)
  File naming: 24-hex-digit name = TimelineID (8) + segment high (8) + segment low (8)

  Example:
    pg_wal/
    ├── 000000010000000000000001    ← segment 1 (bytes 0 - 16MB)
    ├── 000000010000000000000002    ← segment 2 (bytes 16MB - 32MB)
    ├── 000000010000000000000003    ← segment 3 (bytes 32MB - 48MB)
    ├── 000000010000000000000004    ← segment 4 (active, being written)
    └── archive_status/
        ├── 000000010000000000000001.done   ← archived
        └── 000000010000000000000002.done   ← archived

  Segment lifecycle:
  ┌─────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
  │ Created │ ──► │ Active   │ ──► │ Full     │ ──► │ Archived │
  │ (empty) │     │ (being   │     │ (ready   │     │ (copied  │
  │         │     │  written)│     │  for     │     │  to      │
  │         │     │          │     │  archive)│     │  backup) │
  └─────────┘     └──────────┘     └──────────┘     └────┬─────┘
                                                          │
                                              ┌───────────┴──────────┐
                                              ▼                      ▼
                                        ┌──────────┐          ┌──────────┐
                                        │ Recycled │          │ Removed  │
                                        │ (reused  │          │ (deleted │
                                        │  as new  │          │  if past │
                                        │  segment)│          │  retention)│
                                        └──────────┘          └──────────┘

  PostgreSQL WAL retention controls:
    wal_keep_size:     Minimum WAL to retain (for replication slots)
    max_wal_size:      Triggers checkpoint when WAL grows past this
    min_wal_size:      Minimum WAL to keep (pre-allocated for performance)
    archive_command:   Shell command to copy WAL segment to archive
    archive_library:   (PG 15+) Shared library for archiving
```

### WAL Recycling

```
WAL SEGMENT RECYCLING:

  Creating and deleting files has overhead (filesystem metadata updates).
  PostgreSQL recycles WAL segments by renaming old ones instead of
  creating new files.

  Before recycling:
    pg_wal/000000010000000000000005  ← oldest, no longer needed
    pg_wal/000000010000000000000009  ← next segment to be written

  Instead of:
    rm 000000010000000000000005
    create 000000010000000000000009

  PostgreSQL does:
    rename 000000010000000000000005 → 000000010000000000000009

  Benefits:
    - Avoids filesystem allocation overhead
    - File is already the right size (16 MB, pre-allocated)
    - On CoW filesystems (ZFS, btrfs), avoids fragmentation

  The file is zeroed/overwritten as new WAL records fill it.
```

### InnoDB Redo Log Files

```
INNODB REDO LOG FILES:

  Pre-MySQL 8.0.30:
    Fixed number of fixed-size files in a circular arrangement:
    ib_logfile0  (innodb_log_file_size bytes)
    ib_logfile1  (innodb_log_file_size bytes)

    Write pointer cycles: logfile0 → logfile1 → logfile0 → ...

  MySQL 8.0.30+:
    Dynamic redo log files in #innodb_redo/ directory:
    #ib_redo10    (active)
    #ib_redo11    (active)
    #ib_redo12    (spare)

    innodb_redo_log_capacity controls total size.
    Files are created and removed dynamically.

  Circular buffer management:
    ┌───────────────────────────────────────────────────┐
    │         Redo Log Circular Buffer                    │
    │                                                     │
    │  ┌─────────┬───────────────────┬─────────┐         │
    │  │ reusable│ active (needs     │ free    │         │
    │  │ (behind │  recovery)        │ (ahead  │         │
    │  │  ckpt)  │                   │  of     │         │
    │  │         │                   │  write) │         │
    │  └────┬────┴────┬──────────────┴────┬────┘         │
    │       │         │                   │               │
    │   checkpoint   tail              head               │
    │   LSN          LSN               LSN                │
    │                                                     │
    │  If head approaches tail (buffer full):             │
    │    → InnoDB MUST flush dirty pages to advance       │
    │      the checkpoint and free space                  │
    │    → If can't flush fast enough: WRITE STALL        │
    └───────────────────────────────────────────────────┘
```

---

## 9. WAL Compression

### Why Compress WAL?

```
WAL COMPRESSION MOTIVATION:

  A busy OLTP database can generate 100+ MB/s of WAL.

  Problem 1: Disk space
    At 100 MB/s, that's 8.4 TB/day of WAL data.
    Even with segment recycling, archive storage is expensive.

  Problem 2: Replication bandwidth
    Streaming 100 MB/s to each replica. With 3 replicas = 300 MB/s
    network bandwidth consumed just for WAL shipping.

  Problem 3: Backup/restore time
    Restoring from base backup + WAL archive: replaying TB of WAL.

  Compression ratios for WAL:
    Typical: 3:1 to 8:1 (WAL is highly compressible -- repetitive
    structure, similar record layouts, correlated data)
```

### Implementation Approaches

```
WAL COMPRESSION APPROACHES:

  1. FULL-PAGE IMAGE COMPRESSION (PostgreSQL 15+):
     wal_compression = zstd | lz4 | pglz | on (pglz)

     Only compresses full-page images (FPW), not regular WAL records.
     FPW can be 50-80% of WAL volume after a checkpoint.

     Impact: 50-70% reduction in total WAL volume.
     CPU cost: minimal with lz4/zstd (hardware-accelerated).

  2. REDO LOG COMPRESSION (Oracle):
     RMAN backup compression applies to archived redo logs.
     Redo log itself is uncompressed on disk.

  3. WAL ARCHIVE COMPRESSION:
     archive_command = 'gzip -c %p > /archive/%f.gz'
     Or use pg_basebackup with --compress.
     Compression happens after WAL is written, before archiving.

  4. TRANSPARENT FILESYSTEM COMPRESSION:
     ZFS compression=lz4 on the WAL directory.
     Transparent to the database, but may interact poorly with
     fsync semantics (CoW + compression + fsync is complex).

  Trade-offs:
  ┌────────────────────┬──────────┬──────────┬───────────────────┐
  │ Approach            │ CPU cost │ Savings  │ Recovery impact    │
  ├────────────────────┼──────────┼──────────┼───────────────────┤
  │ FPW compression    │ Low      │ 50-70%   │ Decompress during  │
  │ (in-engine)        │          │          │ redo (fast)        │
  ├────────────────────┼──────────┼──────────┼───────────────────┤
  │ Archive compression│ Medium   │ 60-80%   │ Decompress before  │
  │ (post-write)       │          │          │ replay (adds time) │
  ├────────────────────┼──────────┼──────────┼───────────────────┤
  │ FS-level           │ Low      │ 40-60%   │ Transparent but    │
  │ (ZFS/btrfs)        │          │          │ fsync caveats      │
  └────────────────────┴──────────┴──────────┴───────────────────┘
```

---

## 10. Alternatives to WAL

### Shadow Paging

```
SHADOW PAGING (Copy-on-Write):

  Instead of logging changes, maintain TWO copies of each page.
  Modifications go to the "shadow" copy. On commit, atomically
  swap the pointer from the old page to the shadow page.

  ┌──────────────┐        ┌──────────────┐
  │ Page Table   │        │ Page Table   │
  │ (current)    │        │ (after swap) │
  ├──────────────┤        ├──────────────┤
  │ P0 → disk 10│        │ P0 → disk 10│ (unchanged)
  │ P1 → disk 20│───────►│ P1 → disk 55│ (swapped!)
  │ P2 → disk 30│        │ P2 → disk 30│ (unchanged)
  └──────────────┘        └──────────────┘
                                    ▲
              old page at disk 20   │   new page at disk 55
              (becomes free)        │   (was the shadow copy)
                                    │
                             Atomic pointer swap
                             (single write to page table root)

  Used by:
    - LMDB (Lightning Memory-Mapped Database): full CoW B-tree
    - SQLite rollback mode (not WAL mode): shadow pages in journal
    - CouchDB: append-only B-tree with CoW

  Advantages:
    ✓ No WAL needed -- no redo log to manage
    ✓ Instant recovery (no replay, just use the last committed root)
    ✓ Natural snapshot isolation (old root = old snapshot)
    ✓ Simpler implementation than ARIES

  Disadvantages:
    ✗ Write amplification: every modified page is written in full,
      even for a 1-byte change. No incremental logging.
    ✗ Random I/O on commit: shadow pages are scattered on disk.
      WAL commit is one sequential write; shadow paging is N random writes.
    ✗ Page table update must be atomic: limits to single-root structures.
    ✗ Garbage collection: old page versions must be reclaimed.
    ✗ Fragmentation: pages get scattered over time (no in-place updates).

  LMDB's approach (CoW B-tree):
    - Two copies of the B-tree root page (meta pages): meta0, meta1
    - Alternate which meta page is "active" on each commit
    - Commit = write new pages + update inactive meta page + fsync
    - Recovery = read both meta pages, use the one with higher txn ID
    - If crash during write: inactive meta page is still valid
    - Zero WAL, zero recovery time, but high write amplification
```

### Command Logging

```
COMMAND LOGGING (VoltDB):

  Instead of logging the effects of operations (data changes),
  log the operations themselves (the commands/stored procedures).

  Traditional WAL:
    Record: "Page 42, offset 128, changed bytes from X to Y"
    Replay: apply byte changes to the page

  Command log:
    Record: "CALL transfer_funds(account_A=100, account_B=200, amount=50)"
    Replay: re-execute the stored procedure

  VoltDB implementation:
    - All transactions are deterministic stored procedures
    - Log only the procedure name + parameters + partition info
    - Recovery: re-execute all logged commands in order
    - Extremely compact log (tiny records, just the invocation)

  ┌──────────────────────────────────────────────────────────────┐
  │ Command Log Record:                                           │
  │ [TxnID][Timestamp][ProcedureName][Params...][PartitionID]    │
  │ Typically 50-200 bytes vs. kilobytes for data-change WAL.    │
  └──────────────────────────────────────────────────────────────┘

  Advantages:
    ✓ Extremely compact (10-100x smaller than WAL)
    ✓ Very fast to write (tiny records)
    ✓ Natural for deterministic execution engines

  Disadvantages:
    ✗ Recovery is SLOW: must re-execute every command, not just apply bytes
    ✗ Requires deterministic execution (same inputs → same outputs)
    ✗ Non-deterministic functions (NOW(), RANDOM()) break this
    ✗ Recovery time proportional to command count, not data volume
    ✗ Cannot do fine-grained page-level recovery (all or nothing)
```

---

## 11. WAL in LSM-Tree Engines

LSM-tree engines (RocksDB, LevelDB, Cassandra, HBase) use WAL differently from B-tree engines. The WAL protects the MemTable, not data pages.

```
LSM WAL vs. B-TREE WAL:

  B-tree WAL (PostgreSQL, InnoDB):
  ────────────────────────────────
    Protects: modified data pages in the buffer pool
    Contains: page-level redo/undo records
    Recovery: replay WAL to reconstruct dirty page state
    Checkpoint: flush dirty pages, advance checkpoint LSN
    Lifetime: records live until checkpoint advances past them

  LSM WAL (RocksDB, LevelDB):
  ────────────────────────────
    Protects: MemTable (in-memory sorted structure)
    Contains: key-value operations (put, delete, merge)
    Recovery: replay WAL to rebuild MemTable from scratch
    Checkpoint: MemTable flush to SSTable = implicit checkpoint
    Lifetime: entire WAL deleted when its MemTable is flushed

  Key difference: LSM WAL is simpler because there are no dirty pages
  to track. The MemTable is either entirely in memory or entirely lost.
  Recovery = replay the entire WAL file(s), rebuilding the MemTable.

  ┌─────────────────────────────────────────────────────────────────┐
  │ LSM WAL lifecycle:                                               │
  │                                                                  │
  │  Write arrives:                                                  │
  │    1. Append to WAL file #N (sequential I/O)                     │
  │    2. Insert into active MemTable #N                             │
  │                                                                  │
  │  MemTable full:                                                  │
  │    1. Switch to new MemTable #N+1 and WAL #N+1                   │
  │    2. Old MemTable #N becomes immutable                          │
  │    3. Background thread flushes MemTable #N to SSTable           │
  │    4. Once flush completes: delete WAL #N                        │
  │                                                                  │
  │  Crash recovery:                                                 │
  │    1. Identify WAL files whose MemTables weren't flushed         │
  │    2. Replay each WAL in sequence order                          │
  │    3. Rebuilt MemTables become the new active/immutable set       │
  │    4. Resume normal operation                                    │
  │                                                                  │
  │  No ARIES, no undo, no dirty page table, no checkpoint records.  │
  │  The WAL IS the checkpoint -- flush the MemTable and delete WAL. │
  └─────────────────────────────────────────────────────────────────┘
```

### RocksDB WAL Configuration

```
ROCKSDB WAL OPTIONS:

  WAL directory:
    db_options.wal_dir = "/fast-nvme/wal"    # Separate disk for WAL
    (Separate from data directory for I/O isolation)

  WAL sync behavior:
    WriteOptions.sync = true       # fsync every write (safest, slowest)
    WriteOptions.sync = false      # OS buffers WAL writes (default)

    db_options.wal_bytes_per_sync = 0    # 0 = only sync on memtable flush
                                          # >0 = periodic sync every N bytes

  WAL size management:
    db_options.max_total_wal_size = 0    # 0 = auto (4x write_buffer_size)
    When total WAL exceeds this: force flush oldest MemTable to free WAL.

  WAL recovery mode:
    db_options.wal_recovery_mode:
      kTolerateCorruptedTailRecords  (default) – tolerate torn last record
      kAbsoluteConsistency           – fail if any corruption
      kPointInTimeRecovery           – recover up to first corruption
      kSkipAnyCorruptedRecords       – skip corrupted, recover rest

  Disabling WAL (dangerous but fast):
    WriteOptions.disableWAL = true
    Use case: bulk loading, ephemeral caches
    Data in MemTable lost on crash.
```

---

## 12. WAL-Based Replication

### Physical Replication (Streaming WAL)

```
WAL STREAMING REPLICATION:

  The same WAL used for crash recovery doubles as the replication stream.
  The primary ships WAL records to replicas, which replay them.

  ┌──────────┐    WAL stream    ┌──────────┐
  │ Primary  │ ────────────────►│ Replica  │
  │          │                  │          │
  │ WAL      │  (TCP, streaming │ WAL      │
  │ writer   │   or file-based) │ receiver │
  │    │     │                  │    │     │
  │    ▼     │                  │    ▼     │
  │ WAL file │                  │ WAL file │
  │    │     │                  │    │     │
  │    ▼     │                  │    ▼     │
  │ Data     │                  │ Data     │ (recovery process
  │ pages    │                  │ pages    │  applies WAL to pages)
  └──────────┘                  └──────────┘

  PostgreSQL streaming replication:
    Primary: walsender process streams WAL from pg_wal/
    Replica: walreceiver process writes WAL to local pg_wal/
             startup process (recovery) applies WAL to data pages

  Synchronous commit levels (synchronous_commit):
    off:            Don't wait for WAL fsync. Fastest, up to
                    wal_writer_delay ms data loss on crash.

    local:          Wait for local WAL fsync. No remote guarantee.

    remote_write:   Wait for replica to receive and write WAL to OS
                    cache (not fsync). Replica crash can lose data.

    on (default):   Wait for local fsync. If synchronous_standby_names
                    is set, also waits for replica to fsync WAL.

    remote_apply:   Wait for replica to apply (redo) the WAL records.
                    Strongest: reads on replica see committed data.

  ┌──────────────────────────────────────────────────────────────────┐
  │ Replication Latency vs. Durability:                               │
  │                                                                   │
  │  Durability │  remote_apply                                       │
  │             │  on (+ sync standby)                                │
  │             │  remote_write                                       │
  │             │  local                                              │
  │             │  off                                                │
  │             └──────────────────────────────── Commit latency      │
  │                low ◄──────────────────────► high                  │
  └──────────────────────────────────────────────────────────────────┘
```

### Logical vs. Physical WAL Replication

```
PHYSICAL vs. LOGICAL REPLICATION:

  Physical (WAL streaming):
    Ships raw WAL records (page-level changes).
    Replica must have identical page layout (same PG version, same OS).
    Full database replication only (can't filter tables).
    Used for: HA failover, read replicas.

  Logical (Logical decoding of WAL):
    Decodes WAL records into logical changes (INSERT/UPDATE/DELETE on rows).
    Replica can be different PG version, different schema, even different DBMS.
    Can filter by table, transform data.
    Used for: cross-version upgrades, CDC, selective replication.

  PostgreSQL logical replication flow:
    1. WAL records written normally (physiological format)
    2. Logical decoding plugin (e.g., pgoutput) reads WAL
    3. Decodes page-level changes into row-level changes
    4. Streams row changes to subscriber via replication protocol
    5. Subscriber applies row changes using normal SQL execution

  ┌──────────────────────────────────────────────────────────┐
  │ WAL record:                                               │
  │   "Page (1663,16384,12345), block 42, heap_insert,       │
  │    offset 12, data: \x00420041006C696365..."              │
  │                          │                                │
  │                   logical decoding                        │
  │                          │                                │
  │                          ▼                                │
  │ Logical change:                                           │
  │   INSERT INTO users (id, name) VALUES (42, 'Alice')      │
  └──────────────────────────────────────────────────────────┘
```

---

## 13. Modern WAL Architectures

### Aurora: "The Log Is the Database"

```
AMAZON AURORA ARCHITECTURE:

  Traditional database:
    Compute node writes data pages AND WAL to EBS storage.
    Problem: 4/6 I/O operations per write (data page + WAL + replicas).

  Aurora's insight:
    ONLY ship WAL records to storage. Never write data pages over the network.
    Storage nodes reconstruct pages from WAL records on demand.

  ┌───────────────┐        ┌──────────────────────────────────┐
  │ Compute (PG)  │  WAL   │ Aurora Storage (6 replicas)       │
  │               │ records│                                    │
  │ Buffer Pool   │───────►│  Storage Node 1: WAL → page cache │
  │ WAL Buffer    │        │  Storage Node 2: WAL → page cache │
  │               │        │  Storage Node 3: WAL → page cache │
  │ (No data page │        │  Storage Node 4: WAL → page cache │
  │  writes to    │        │  Storage Node 5: WAL → page cache │
  │  network!)    │        │  Storage Node 6: WAL → page cache │
  └───────────────┘        └──────────────────────────────────┘

  Key design points:

  1. Write path:
     Compute → ships only WAL records (not data pages) to 6 storage nodes
     Commit when 4/6 storage nodes ACK the WAL write (quorum)
     Network traffic reduced by ~7x vs. traditional MySQL replication

  2. Storage nodes:
     Receive WAL, apply it to page cache in background
     When compute requests a page: materialize from WAL if not cached
     "Coalesce WAL records into page" = essentially continuous redo

  3. Crash recovery:
     Traditional: replay minutes of WAL (serial, slow)
     Aurora: storage nodes already have WAL applied (continuous redo)
     Recovery = just re-establish connections. ~10-30 seconds.

  4. Read replicas:
     Share the same storage layer (zero-lag data access)
     Compute ships WAL to replicas for buffer cache invalidation only
     Replica lag: typically <20ms (just cache invalidation)

  The insight: in a distributed storage system, the WAL is the
  only source of truth needed. Data pages are just a cached
  materialization of the WAL.
```

### Neon: Disaggregated WAL Storage

```
NEON ARCHITECTURE (open-source serverless Postgres):

  Separates compute, WAL, and page storage into independent services.

  ┌──────────┐     WAL     ┌──────────────┐     WAL     ┌─────────────┐
  │ Compute  │ ──────────► │ WAL Service  │ ──────────► │ Page Server │
  │ (Postgres│             │ (safekeepers)│             │             │
  │  process)│             │ Paxos-based  │             │ Materializes│
  │          │ ◄────────── │ durable WAL  │             │ pages from  │
  │          │  page reads │ storage      │             │ WAL on      │
  │          │             └──────────────┘             │ demand      │
  │          │ ◄───────────────────────────────────────│             │
  │          │             page responses               └─────────────┘
  └──────────┘

  WAL Service (Safekeepers):
    - 3 safekeepers form a Paxos group
    - WAL is durably committed when 2/3 safekeepers ACK
    - Provides the durability guarantee (replaces local WAL files)

  Page Server:
    - Receives WAL from safekeepers
    - Maintains a page cache: materializes pages by replaying WAL
    - Responds to compute's page read requests
    - Stores page images + WAL in a tiered storage (local + S3)

  Compute:
    - Stateless PostgreSQL instance
    - No local storage for data pages
    - Fetches pages from page server on demand
    - Can be started/stopped independently (serverless)

  Benefits:
    - Compute scales independently from storage
    - Instant database branching (copy-on-write of WAL history)
    - Time-travel queries (page server stores full WAL history)
    - Serverless: zero compute cost when idle
```

### WarpStream / Shared-Log Architectures

```
SHARED LOG PATTERN (increasingly common):

  The WAL is extracted into a shared, durable log service that
  multiple consumers can read from. This pattern appears in:

  - Apache Kafka (the log IS the database for event streaming)
  - FoundationDB (uses a shared WAL for all storage servers)
  - CockroachDB Raft log (distributed WAL via consensus)
  - TiKV Raft log (per-region WAL)

  ┌──────────┐   ┌──────────┐   ┌──────────┐
  │ Writer 1 │   │ Writer 2 │   │ Writer 3 │
  └────┬─────┘   └────┬─────┘   └────┬─────┘
       │              │              │
       ▼              ▼              ▼
  ┌────────────────────────────────────────┐
  │          Shared Durable Log             │
  │     (Kafka, Paxos, Raft, or custom)    │
  │                                         │
  │  [rec1][rec2][rec3][rec4][rec5][rec6]  │
  └──────────────┬───────────┬─────────────┘
                 │           │
                 ▼           ▼
           ┌──────────┐ ┌──────────┐
           │ Consumer │ │ Consumer │
           │ (index)  │ │ (cache)  │
           └──────────┘ └──────────┘

  This inverts the traditional model:
    Traditional: each database instance owns its WAL
    Shared log:  WAL is a shared service, databases are consumers

  Benefits:
    - Multiple materialized views from one log
    - Replay log to rebuild any consumer from scratch
    - Natural audit trail and event sourcing
    - Decouple write durability from read serving
```

---

## 14. WAL Performance Tuning and Monitoring

### The WAL Write Bottleneck

```
WAL PERFORMANCE BOTTLENECK ANALYSIS:

  For high-throughput OLTP, WAL fsync is often the #1 bottleneck.

  Anatomy of a commit:
    1. Write WAL records to WAL buffer:     ~1 μs  (memory copy)
    2. Write WAL buffer to OS page cache:   ~5 μs  (write() syscall)
    3. fsync WAL to disk:                   ~10-50 μs (NVMe)
                                            ~2-10 ms  (HDD)
    4. Return "committed" to client

  Step 3 dominates. On NVMe, maximum single-threaded commit rate:
    1 / 30μs = ~33,000 commits/sec

  With group commit (batching 10 commits per fsync):
    10 / 30μs = ~333,000 commits/sec

  Maximum NVMe sequential write bandwidth for WAL:
    ~1-3 GB/s (far more than most workloads generate)

  The bottleneck is fsync LATENCY, not THROUGHPUT.
```

### PostgreSQL WAL Tuning

```
POSTGRESQL WAL CONFIGURATION:

  ┌──────────────────────────┬────────────────────────────────────────┐
  │ Parameter                 │ Recommendation                         │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ wal_level                │ replica (default since PG 10)          │
  │                          │ logical (if using logical replication) │
  │                          │ minimal (only if no replication/PITR)  │
  │                          │ Higher levels = more WAL volume.       │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ wal_buffers              │ -1 (auto, default). Usually fine.      │
  │                          │ 16MB max. Increase only if seeing      │
  │                          │ WAL buffer contention in pg_stat_wal.  │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ wal_compression          │ lz4 or zstd (PG 15+). Reduces WAL     │
  │                          │ volume 50-70%. Minimal CPU overhead.   │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ max_wal_size             │ 2-8 GB (trigger checkpoint when WAL    │
  │                          │ exceeds this). Larger = longer recovery│
  │                          │ but less frequent checkpoints.         │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ checkpoint_timeout       │ 10-30 min for write-heavy workloads.   │
  │                          │ Default 5 min often too frequent.      │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ checkpoint_completion    │ 0.9 (default). Spread checkpoint I/O   │
  │ _target                  │ over 90% of the interval.              │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ commit_delay             │ 0 (default). Set to 10-100 μs if       │
  │                          │ commit_siblings is regularly exceeded  │
  │                          │ and you want more group commit benefit.│
  ├──────────────────────────┼────────────────────────────────────────┤
  │ synchronous_commit       │ on (default, safest).                  │
  │                          │ off: ~3x more commits/sec but up to   │
  │                          │ wal_writer_delay (200ms) data loss.   │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ wal_sync_method          │ fdatasync (default on Linux). Use      │
  │                          │ open_datasync on some systems.         │
  │                          │ pg_test_fsync to benchmark options.    │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ full_page_writes         │ on (default, NEVER disable unless     │
  │                          │ storage guarantees atomic 8KB writes,  │
  │                          │ e.g., ZFS with matching recordsize).   │
  └──────────────────────────┴────────────────────────────────────────┘
```

### Monitoring WAL Performance

```
POSTGRESQL WAL MONITORING:

  pg_stat_wal (PG 14+):
  ┌─────────────────────┬──────────────────────────────────────────┐
  │ Column               │ What it tells you                        │
  ├─────────────────────┼──────────────────────────────────────────┤
  │ wal_records          │ Total WAL records generated               │
  │ wal_fpi              │ Full-page images written (FPW)           │
  │ wal_bytes            │ Total WAL bytes generated                │
  │ wal_buffers_full     │ Times WAL buffer was full (back-pressure)│
  │ wal_write            │ Times WAL written to OS (write() calls)  │
  │ wal_sync             │ Times WAL fsync'd                        │
  │ wal_write_time       │ Total time in WAL writes (ms)            │
  │ wal_sync_time        │ Total time in WAL fsyncs (ms)            │
  └─────────────────────┴──────────────────────────────────────────┘

  Key metrics to watch:
    wal_buffers_full > 0:  WAL buffer is too small or writes too bursty
    wal_sync_time / wal_sync: average fsync latency
    wal_fpi / wal_records: FPW ratio (high = consider wal_compression)

  pg_stat_bgwriter:
    checkpoints_timed:     Checkpoints triggered by timeout (normal)
    checkpoints_req:       Checkpoints triggered by max_wal_size (bad)
    If checkpoints_req >> checkpoints_timed:
      → Increase max_wal_size or checkpoint_timeout

  Current WAL position:
    SELECT pg_current_wal_lsn();                -- current write position
    SELECT pg_current_wal_insert_lsn();         -- current insert position
    SELECT pg_current_wal_flush_lsn();          -- current flush position
    SELECT pg_wal_lsn_diff(lsn1, lsn2);        -- byte difference between LSNs

  WAL generation rate:
    SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0') /
           extract(epoch from (now() - pg_postmaster_start_time()))
           AS wal_bytes_per_second;

  Replication lag in WAL bytes:
    SELECT client_addr,
           pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS lag_bytes
    FROM pg_stat_replication;
```

### Dedicated WAL Disk

```
DEDICATED WAL DISK:

  For write-heavy workloads, placing WAL on a separate disk from data
  eliminates I/O contention between WAL writes (sequential) and
  data page writes/reads (random).

  Setup (PostgreSQL):
    1. Create a symlink or use tablespace:
       mv $PGDATA/pg_wal /fast-nvme-disk/pg_wal
       ln -s /fast-nvme-disk/pg_wal $PGDATA/pg_wal

    2. Or at initdb:
       initdb --waldir=/fast-nvme-disk/pg_wal

  Setup (RocksDB):
    db_options.wal_dir = "/fast-nvme-disk/rocksdb-wal"

  Disk recommendations for WAL:
    - NVMe SSD with power-loss protection (PLP)
    - DO NOT use consumer SSDs without PLP for WAL
      (they may lie about fsync completion)
    - Optane/Intel P5800X: ideal for WAL (4μs write latency)
    - Separate from data disk to avoid I/O contention
    - RAID-1 for WAL disk if not using synchronous replication

  Impact: WAL fsync latency drops from ~50μs (shared NVMe) to
  ~10-20μs (dedicated NVMe with no contention).
  Commit throughput can double.
```

---

## 15. Real-World Failure Modes

### WAL Corruption

```
WAL CORRUPTION SCENARIOS:

  1. Torn WAL record (most common):
     Power loss during WAL write. Last record is partially written.

     Detection: CRC checksum in WAL record header.
     Recovery: discard the torn tail record.
       - If it was a COMMIT record: transaction is NOT committed
         (client should have received an error or timeout)
       - If it was a data record: transaction was uncommitted anyway

  2. Silent bit corruption in WAL file:
     Disk firmware bug, bit rot, cosmic ray.

     Detection: CRC checksum mismatch during recovery replay.
     Recovery: if the corrupted record is after the last committed
       transaction, discard it. If it's in the middle of committed
       data → CATASTROPHIC. Need to restore from backup.

  3. Missing WAL segment:
     Operator error (deleted WAL file), archive failure.

     Detection: gap in LSN sequence during recovery.
     Recovery: impossible without the missing segment.
       Need to fall back to last full backup + available WAL.

  4. WAL disk full:
     The WAL partition runs out of space.

     PostgreSQL behavior: PANIC and shut down immediately.
     This is intentional: better to crash cleanly than to
     silently lose committed data by failing to write WAL.

     Prevention:
       - Monitor disk space on WAL partition
       - Set max_wal_size appropriately
       - Use wal_keep_size carefully (can prevent WAL recycling)
       - Monitor replication slots (inactive slots prevent WAL cleanup)

  5. fsync failure:
     The disk reports fsync success but data isn't durable.

     PostgreSQL (before PG 12): retried fsync, which on some
       Linux kernels silently succeeds even though data was lost.
       Led to the famous "fsync gate" (2018).

     PostgreSQL (PG 12+): if fsync fails, PANIC immediately.
       Do not retry. Assume data is corrupt. Operator must
       restore from backup or verify data integrity.

  ┌──────────────────────────────────────────────────────────────┐
  │ THE FSYNC GATE (2018):                                        │
  │                                                               │
  │ Linux kernel behavior:                                        │
  │   1. write() to file → data in page cache, page marked dirty │
  │   2. fsync() fails (I/O error) → page marked clean anyway!   │
  │   3. Next fsync() succeeds → but data was NEVER written!     │
  │                                                               │
  │ PostgreSQL's old behavior:                                    │
  │   1. fsync() fails → log warning, retry                      │
  │   2. Retry fsync() succeeds → assume data is safe            │
  │   3. BUT: kernel cleared the dirty flag, data is LOST         │
  │                                                               │
  │ Fix (PG 12):                                                  │
  │   Any fsync failure → immediate PANIC → don't trust anything  │
  │                                                               │
  │ Lesson: fsync error handling is one of the hardest problems   │
  │ in database engineering. The kernel, filesystem, disk firmware,│
  │ and database must all cooperate correctly.                    │
  └──────────────────────────────────────────────────────────────┘
```

### Recovery Time Estimation

```
RECOVERY TIME ESTIMATION:

  Recovery time ≈ (WAL since last checkpoint) / (redo throughput)

  Redo throughput depends on:
    - I/O pattern: mostly random reads (fetching pages to redo)
    - NVMe SSD: ~200-500 MB/s effective redo throughput
    - HDD: ~10-50 MB/s effective redo throughput

  Example:
    checkpoint_timeout = 5 min
    WAL generation rate = 20 MB/s
    WAL since checkpoint = 5 min × 20 MB/s = 6 GB
    Redo throughput (NVMe) = 300 MB/s

    Recovery time ≈ 6 GB / 300 MB/s = 20 seconds
    Plus analysis phase: ~2-5 seconds (WAL scan, no I/O)
    Plus undo phase: ~1-10 seconds (depends on uncommitted txns)
    Total: ~25-35 seconds

  To reduce recovery time:
    - More frequent checkpoints (lower checkpoint_timeout / max_wal_size)
      Trade-off: more background I/O, more FPW in WAL
    - Faster storage (NVMe > SATA SSD > HDD)
    - Parallel recovery (PG 15+ can parallelize redo of different tables)

  Aurora/Neon: recovery time ≈ 10-30 seconds regardless of WAL volume,
    because storage nodes continuously apply WAL (no redo phase needed).
```

---

## Summary: WAL Design Decision Space

```
WAL DESIGN CHOICES AND TRADE-OFFS:

  ┌──────────────────────┬───────────────────────┬──────────────────────┐
  │ Dimension             │ Options                │ Used by               │
  ├──────────────────────┼───────────────────────┼──────────────────────┤
  │ Logging granularity  │ Physical              │ (rare)               │
  │                      │ Physiological         │ PostgreSQL, InnoDB   │
  │                      │ Logical               │ Logical replication  │
  ├──────────────────────┼───────────────────────┼──────────────────────┤
  │ Recovery algorithm   │ ARIES (redo + undo)   │ PG, InnoDB, SQL Svr  │
  │                      │ Redo-only (no steal)  │ Some in-memory DBs   │
  │                      │ No recovery (no WAL)  │ LMDB (shadow paging) │
  ├──────────────────────┼───────────────────────┼──────────────────────┤
  │ Checkpoint style     │ Fuzzy (non-blocking)  │ PG, InnoDB, Oracle   │
  │                      │ Sharp (blocking)      │ SQLite rollback mode │
  │                      │ Continuous             │ InnoDB adaptive flush│
  ├──────────────────────┼───────────────────────┼──────────────────────┤
  │ WAL file management  │ Segment-based         │ PostgreSQL           │
  │                      │ Circular buffer       │ InnoDB               │
  │                      │ Per-MemTable WAL      │ RocksDB, LevelDB     │
  ├──────────────────────┼───────────────────────┼──────────────────────┤
  │ Torn page protection │ Full-page writes      │ PostgreSQL           │
  │                      │ Double-write buffer   │ InnoDB               │
  │                      │ Atomic page writes    │ (hardware guarantee) │
  │                      │ Shadow paging (CoW)   │ LMDB, ZFS            │
  ├──────────────────────┼───────────────────────┼──────────────────────┤
  │ Commit durability    │ fsync every commit    │ PG (sync_commit=on)  │
  │                      │ Group commit          │ PG, InnoDB           │
  │                      │ Async (batched fsync) │ PG (sync_commit=off) │
  │                      │ No fsync              │ Testing/ephemeral    │
  ├──────────────────────┼───────────────────────┼──────────────────────┤
  │ WAL architecture     │ Local (embedded)      │ Traditional DBMS     │
  │                      │ Disaggregated         │ Aurora, Neon         │
  │                      │ Shared log            │ Kafka, FoundationDB  │
  └──────────────────────┴───────────────────────┴──────────────────────┘
```