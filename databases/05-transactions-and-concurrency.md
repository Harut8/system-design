# Database Transactions & Concurrency Control: A Deep Dive

A comprehensive, staff-engineer-level reference covering ACID internals, every concurrency anomaly, isolation levels across major databases, locking protocols, MVCC implementations, distributed transactions, practical concurrency patterns, and the engine-specific lock internals (PostgreSQL row/table lock modes, MultiXacts, InnoDB gap and next-key locks) behind most production lock incidents.

---

## Table of Contents

0. [Mental Model: Key Terms With Everyday Analogies](#0-mental-model-key-terms-with-everyday-analogies)
1. [ACID Deep Dive](#1-acid-deep-dive)
2. [Concurrency Anomalies (All of Them)](#2-concurrency-anomalies-all-of-them)
3. [Isolation Levels](#3-isolation-levels)
4. [Concurrency Control Mechanisms](#4-concurrency-control-mechanisms)
5. [Lock Manager Implementation](#5-lock-manager-implementation)
6. [Distributed Transactions](#6-distributed-transactions)
7. [Transaction Implementation Details](#7-transaction-implementation-details)
8. [Practical Concurrency Patterns](#8-practical-concurrency-patterns)
9. [Advanced Locking Internals](#9-advanced-locking-internals)
   - [9.1 PG row-lock modes: FOR KEY SHARE / FOR NO KEY UPDATE](#91-postgresql-row-lock-modes-for-key-share-and-for-no-key-update) · [9.2 Where row locks live](#92-where-row-locks-physically-live) · [9.3 MultiXacts](#93-multixacts-when-several-transactions-lock-one-row) · [9.4 Table lock modes](#94-postgresql-table-level-lock-modes) · [9.5 Lock queue pile-up & safe DDL](#95-the-lock-queue-pile-up-how-a-1-ms-alter-table-causes-an-outage) · [9.6 Timeouts](#96-where-timeouts-apply) · [9.7 EvalPlanQual](#97-read-committed-write-semantics-evalplanqual-and-semi-consistent-reads) · [9.8 InnoDB gap / next-key / insert-intention](#98-innodb-lock-types-record-gap-next-key-insert-intention-auto-inc) · [9.9 Write skew without SERIALIZABLE](#99-preventing-write-skew-and-phantoms-without-serializable) · [9.10 U locks & lock ordering](#910-lock-conversion-u-locks-and-lock-ordering) · [9.11 Hot rows](#911-hot-rows-when-one-row-is-the-bottleneck) · [9.12 Subtransactions](#912-subtransactions-the-hidden-scalability-cliff-postgresql)

---

## 0. Mental Model: Key Terms With Everyday Analogies

Read this table first. Each row gives the one-line meaning, a real-world picture to hang it on, why the thing exists, and what it costs. Every later section is detail on one of these rows. (Same format as [MENTAL_MODEL.md](MENTAL_MODEL.md).)

**The whole chapter in one picture: a busy shared office.** Rows are documents on desks, tables are rooms, a transaction is one employee's errand that must finish completely or be undone. Locks are "in use" signs; MVCC means readers get a photocopy so they never wait for the person editing the original.

| Term | Plain meaning | Everyday analogy | Why we need it | Problems it creates |
|---|---|---|---|---|
| Transaction | changes that all happen or none happen | a bank transfer: debit and credit together | Crashes mid-change would leave half-done data | Long ones hold locks/snapshots; apps must retry aborts |
| Undo log / rollback | before-images used to reverse changes | Ctrl-Z history | Atomicity: an abort must restore every changed row | Undo space grows with long transactions |
| Isolation level | how much concurrent transactions can see of each other | how thick the walls between cubicles are | Full isolation is expensive; weaker levels are faster | Each weaker level allows specific anomalies (Section 2) |
| Snapshot | the set of committed data a statement/transaction is allowed to see | a photo of the whiteboard taken when you walked in | Readers get a stable view without blocking writers | Old versions must be kept until no photo needs them (bloat) |
| Dirty read | reading uncommitted data | reading a colleague's unsent draft email | -- (it's the anomaly) | You act on data that may be rolled back |
| Lost update | two read-modify-writes, one overwrites the other | two people editing the same spreadsheet cell offline; last save wins | -- | Silent data loss; InnoDB RR does NOT prevent it |
| Write skew | two transactions check a shared rule, then change *different* rows | two on-call doctors each see "2 on call" and both go home | -- | Invisible to row locks; needs constraints, a shared lock row, or SERIALIZABLE (9.9) |
| Phantom | new rows appear in a repeated range query | a new guest walks in after you counted the room | -- | Needs predicate/gap locks or snapshots |
| 2PL | acquire locks, never release until done | collect every key you need before returning any | Guarantees serializability with locks | Blocking, deadlocks |
| MVCC | keep old row versions for readers | readers get a photocopy while the editor works on the original | Readers and writers stop blocking each other | Version cleanup (VACUUM/purge), write skew under SI |
| SSI | snapshot isolation + a referee tracking read/write dependencies | a referee who cancels one move if the game could not have been played turn by turn | Serializable without read locks | False-positive aborts; everyone must retry |
| Deadlock | a cycle of transactions waiting for each other | two cars nose-to-nose on a one-lane bridge | -- | One transaction is killed (40P01) and must retry |
| Lock ordering | always take locks in the same global order | always pick up the lower-numbered chopstick first | Prevents most deadlocks by construction | Needs discipline across all code paths |
| Row-lock modes (PG) | `FOR UPDATE` / `NO KEY UPDATE` / `SHARE` / `KEY SHARE` | demolish / repaint / inspect / mail carrier needs the address | Let FK checks run in parallel with normal updates (9.1) | ORMs default to the strongest mode |
| MultiXact | a shared lock list stored when several txns lock one row | a sign-up sheet on the door, reprinted for every new name | `xmax` can hold only one XID | O(N²) growth on hot rows, its own wraparound (9.3) |
| Table lock modes | 8 relation-level modes from ACCESS SHARE to ACCESS EXCLUSIVE | shop signs: open / restocking / stocktake / closed for renovation | DDL must not change a table under a running query | ACCESS EXCLUSIVE blocks even plain `SELECT` (9.4) |
| Lock queue pile-up | queued DDL makes every later query wait | a wide load waiting at a single-lane bridge jams the whole road | Queue order prevents starvation | Tiny DDL behind a long query = outage; use `lock_timeout` (9.5) |
| Gap / next-key lock | InnoDB locks on the *space between* index records | traffic cones across empty parking spaces | Stop phantom inserts without predicate locks | Insert deadlocks, locking far more than you read (9.8) |
| Insert intention | InnoDB "I'm about to insert here" signal | a driver signalling for a parking spot | Inserts into one gap can proceed in parallel | Blocked by any gap lock |
| EvalPlanQual | RC re-checks `WHERE` on the newest version after waiting | re-reading the price tag at the checkout | Lets RC updates see the latest committed data safely | Rows that newly match are never seen (9.7) |
| SKIP LOCKED | skip rows another transaction has locked | a deli counter: take the next ticket nobody is serving | Parallel queue workers without contention | Deliberately inconsistent view; only for queues |
| Advisory lock | app-defined lock on a number | a talking stick | Mutual exclusion for things that aren't rows | Only works if every code path uses it |
| Hot row | one row every transaction updates | a shop with a single cash register | -- | Throughput = 1 / lock hold time (9.11) |
| Subtransaction | savepoint / exception block inside a transaction | lines in a 64-line pocket notebook | Partial rollback | >64 overflows into a shared archive all sessions must visit (9.12) |
| 2PC | prepare everywhere, then commit everywhere | a wedding: "do you?" "I do" → "I now pronounce you" | Atomic commit across nodes | Blocks if the coordinator dies after prepare |
| Saga | chain of local transactions with compensations | a trip booking with cancellation policies | Cross-service workflows without distributed locks | No isolation; compensations must be designed |
| Idempotency key | client-chosen ID that makes retries safe | a receipt number: the second time, the cashier just reprints | Network timeouts make "did it commit?" unknown | Key storage, retention, payload mismatch handling |

---

## 1. ACID Deep Dive

ACID is not a single mechanism -- it is four separate guarantees, each implemented by distinct subsystems inside the database engine. Understanding the implementation of each property is critical when debugging production anomalies or choosing between databases.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          ACID Properties                                │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Atomicity ──────► Undo logs, compensation log records                  │
│  Consistency ────► Constraints, triggers, application invariants         │
│  Isolation ──────► Concurrency control (locks, MVCC, OCC)               │
│  Durability ─────► WAL, fsync, replicas, battery-backed cache           │
│                                                                         │
└──────────────────────────────────────────────────────────────────────────┘
```

### 1.1 Atomicity: Undo Logs & Compensation

Atomicity means a transaction is all-or-nothing. If any part fails, the entire transaction is rolled back as if it never happened.

**Implementation: Undo Logs**

Before modifying any data page, the database writes the *before-image* (the original value) into an undo log. If the transaction aborts, the database walks the undo log backwards and restores every page to its original state.

```
Transaction T1: UPDATE accounts SET balance = 500 WHERE id = 42;
                (old balance was 1000)

Undo Log Entry:
┌─────────────────────────────────────────────────────────┐
│  LSN: 10047                                             │
│  TxID: T1                                               │
│  Table: accounts                                        │
│  Row: id=42                                             │
│  Operation: UPDATE                                      │
│  Before-Image: {balance: 1000}                          │
│  After-Image:  {balance: 500}                           │
│  Prev-LSN: 10031  (previous log record for T1)          │
└─────────────────────────────────────────────────────────┘
```

**Rollback Process:**

```
ABORT T1:
  1. Read undo log for T1 from tail (most recent entry first)
  2. For each undo record:
     a. Apply the before-image to the data page
     b. Write a Compensation Log Record (CLR) to the WAL
  3. Write "T1 ABORT" record to WAL
  4. Release all locks held by T1

CLR (Compensation Log Record):
┌─────────────────────────────────────────────────────────┐
│  LSN: 10052                                             │
│  TxID: T1                                               │
│  Type: CLR (Compensation)                               │
│  Undo-of: LSN 10047                                     │
│  Operation: Restore accounts.id=42.balance = 1000       │
│  Undo-Next-LSN: 10031  (next record to undo if needed)  │
└─────────────────────────────────────────────────────────┘
```

CLRs are critical: they are **redo-only** records. If the system crashes during a rollback, the recovery process sees the CLR and knows that particular undo has already been applied. Without CLRs, the database could get stuck in an infinite loop of undo-crash-redo-undo.

**PostgreSQL vs InnoDB Approach:**

| Aspect | PostgreSQL | InnoDB (MySQL) |
|--------|-----------|----------------|
| Undo storage | Old row versions stored inline in heap (dead tuples) | Separate undo log segments in undo tablespace |
| Rollback | No tuple is touched: the XID is marked ABORTED in pg_xact, so its new tuples become invisible and its `xmax` marks are ignored | Restore from undo log segment |
| Space reclamation | VACUUM must clean dead tuples | Purge thread reclaims undo segments |
| Crash recovery undo | Minimal (dead tuples are just ignored) | Must replay undo log for uncommitted txns |

### 1.2 Consistency: Constraint Enforcement

Consistency means a transaction brings the database from one valid state to another. This is partly the database's responsibility (constraint enforcement) and partly the application's (business logic).

**Database-Enforced Constraints:**

```sql
-- PRIMARY KEY: uniqueness + NOT NULL
CREATE TABLE orders (
    id          BIGINT PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id),
    amount      DECIMAL(10,2) CHECK (amount > 0),
    status      VARCHAR(20) DEFAULT 'pending'
                CHECK (status IN ('pending','confirmed','shipped','delivered')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- UNIQUE constraint: PostgreSQL checks each row immediately unless the
-- constraint is DEFERRABLE (then at statement end, or at commit if deferred)
ALTER TABLE orders ADD CONSTRAINT uq_order_ref UNIQUE (user_id, created_at);
```

**Constraint Check Timing:**

```
┌─────────────────────────────────────────────────────────────────┐
│  Constraint Checking Modes                                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  IMMEDIATE (default):                                            │
│    Checked after each DML statement within the transaction.      │
│    Violation → statement fails. PostgreSQL then aborts the       │
│    whole txn (unless a SAVEPOINT is used); Oracle/MySQL/         │
│    SQL Server roll back only the failed statement.               │
│                                                                  │
│  DEFERRED:                                                       │
│    Checked once, at COMMIT time.                                 │
│    Violation → entire transaction aborts.                        │
│    Useful for circular references or bulk loads.                 │
│                                                                  │
│  Example:                                                        │
│    -- only valid if fk_order_user is declared DEFERRABLE         │
│    SET CONSTRAINTS fk_order_user DEFERRED;                       │
│    INSERT INTO orders (user_id, ...) VALUES (999, ...);          │
│    INSERT INTO users (id, ...) VALUES (999, ...);                │
│    COMMIT;  -- FK checked here, passes because user exists       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**Triggers for Complex Invariants:**

```sql
-- Ensure an account balance never goes negative
CREATE OR REPLACE FUNCTION check_balance()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.balance < 0 THEN
        RAISE EXCEPTION 'Balance cannot be negative for account %', NEW.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_check_balance
    BEFORE UPDATE ON accounts
    FOR EACH ROW EXECUTE FUNCTION check_balance();
```

**Important nuance:** Consistency is the only ACID property that is *not* purely a database-level guarantee. The database can enforce schema constraints, but application-level invariants (e.g., "total money in the system is conserved") require correct transaction logic in application code.

### 1.3 Isolation: The Hardest Property

Isolation determines what concurrent transactions can see of each other's work. It is by far the most complex ACID property because it sits at the intersection of correctness and performance. Providing full isolation (serializability) is expensive; weaker isolation improves throughput but introduces anomalies.

This property is so important that Sections 2, 3, and 4 of this document are entirely devoted to it.

**The Core Tension:**

```
          Correctness                         Performance
              ▲                                   ▲
              │                                   │
  SERIALIZABLE│───────────────────────────────────│ Lowest throughput
              │                                   │
  SNAPSHOT    │───────────────────────────────────│
  ISOLATION   │                                   │
              │                                   │
  REPEATABLE  │───────────────────────────────────│
  READ        │                                   │
              │                                   │
  READ        │───────────────────────────────────│
  COMMITTED   │                                   │
              │                                   │
  READ        │───────────────────────────────────│ Highest throughput
  UNCOMMITTED │                                   │
              └───────────────────────────────────┘
```

### 1.4 Durability: WAL, fsync, and Replicas

Durability guarantees that once a transaction is committed, it survives any subsequent failure (power loss, crash, disk failure).

**The Durability Stack:**

```
┌─────────────────────────────────────────────────────────────────┐
│                     Durability Layers                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Layer 1: WAL (Write-Ahead Log)                                  │
│    - Log records written BEFORE data pages modified              │
│    - fsync() forces WAL to stable storage                        │
│    - Group commit: batch multiple txns into one fsync            │
│                                                                  │
│  Layer 2: Checkpoints                                            │
│    - Periodically flush dirty pages to data files                │
│    - Write checkpoint record to WAL                              │
│    - Allows WAL truncation (bounded recovery time)               │
│                                                                  │
│  Layer 3: Replication                                            │
│    - Synchronous replication: commit waits for replica ACK       │
│    - Semi-synchronous: at least one replica must ACK             │
│    - Asynchronous: fire-and-forget (risk of data loss)           │
│                                                                  │
│  Layer 4: Storage Hardware                                       │
│    - Battery-backed write cache (BBU/BBWC)                       │
│    - UPS for power loss protection                               │
│    - RAID for disk failure protection                            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**The fsync Trap:**

Many systems claim durability but violate it through incorrect fsync usage:

```
WRONG (common in early systems):
  write() to WAL file  →  return "committed" to client
  Problem: write() only reaches OS page cache, not disk.
           Power failure loses the data.

CORRECT:
  write() to WAL file  →  fsync() or fdatasync()  →  return "committed"
  fsync() forces OS to flush to physical storage.

PostgreSQL settings:
  wal_sync_method = fdatasync   (default on Linux)
  fsync = on                    (NEVER turn this off in production)
  synchronous_commit = on       (can be turned off for speed at cost of
                                 up to 3 × wal_writer_delay of data loss)
```

**Group Commit Optimization:**

```
Without group commit:          With group commit:
  T1: write + fsync              T1: write ─┐
  T2: write + fsync              T2: write ──┼──► single fsync
  T3: write + fsync              T3: write ─┘
  = 3 fsyncs (~30ms)            = 1 fsync (~10ms)
```

---

## 2. Concurrency Anomalies (All of Them)

A concurrency anomaly occurs when the interleaved execution of concurrent transactions produces a result that could not have been produced by any serial (one-at-a-time) execution. Understanding every anomaly is essential for choosing the correct isolation level.

### 2.1 Dirty Read

A transaction reads data written by another transaction that has not yet committed. If that other transaction aborts, the reader has seen data that never officially existed.

```
Timeline:
  T1                              T2
  ──────────────────────────────  ──────────────────────────────
  BEGIN
  UPDATE accounts
    SET balance = 500
    WHERE id = 1;
    (was 1000)
                                  BEGIN
                                  SELECT balance FROM accounts
                                    WHERE id = 1;
                                  → Returns 500  ← DIRTY READ!
  ROLLBACK
  (balance restored to 1000)
                                  -- T2 now has stale/invalid data
                                  -- It saw balance=500 which never
                                  -- actually committed
                                  COMMIT
```

```sql
-- Real-world example: reporting on uncommitted data
-- Session 1:
BEGIN;
UPDATE inventory SET quantity = 0 WHERE product_id = 42;
-- hasn't committed yet, maybe doing other checks...

-- Session 2 (READ UNCOMMITTED):
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SELECT SUM(quantity * price) AS total_value FROM inventory;
-- Reports a lower total because it sees product 42 with quantity=0
-- even though Session 1 might ROLLBACK

-- Session 1:
ROLLBACK;  -- oops, the quantity was never actually 0
```

**Impact:** Dirty reads can lead to incorrect business decisions (reporting), cascading errors (acting on phantom data), and constraint violations at the application level.

**Prevented by:** READ COMMITTED and above.

### 2.2 Non-Repeatable Read (Fuzzy Read)

A transaction reads the same row twice and gets different values because another committed transaction modified the row between the two reads.

```
Timeline:
  T1                              T2
  ──────────────────────────────  ──────────────────────────────
  BEGIN
  SELECT balance FROM accounts
    WHERE id = 1;
  → Returns 1000
                                  BEGIN
                                  UPDATE accounts
                                    SET balance = 500
                                    WHERE id = 1;
                                  COMMIT
  SELECT balance FROM accounts
    WHERE id = 1;
  → Returns 500  ← DIFFERENT!
  -- Same query, same txn, different result
  COMMIT
```

```sql
-- Real-world example: rate calculation with stale base
-- Session 1:
BEGIN;
SELECT rate FROM exchange_rates WHERE pair = 'USD/EUR';
-- Returns 0.92

-- Session 2:
BEGIN;
UPDATE exchange_rates SET rate = 0.95 WHERE pair = 'USD/EUR';
COMMIT;

-- Session 1 (continued):
-- Converts $1000 using the old rate...
SELECT amount * 0.92 AS converted FROM transfers WHERE id = 100;
-- But then re-reads the rate for logging:
SELECT rate FROM exchange_rates WHERE pair = 'USD/EUR';
-- Returns 0.95 -- inconsistent with the rate actually used!
COMMIT;
```

**Prevented by:** REPEATABLE READ and above.

### 2.3 Phantom Read

A transaction re-executes a range query and finds new rows that satisfy the predicate, inserted by another committed transaction.

```
Timeline:
  T1                              T2
  ──────────────────────────────  ──────────────────────────────
  BEGIN
  SELECT * FROM employees
    WHERE dept = 'eng';
  → Returns 3 rows (Alice, Bob, Carol)
                                  BEGIN
                                  INSERT INTO employees
                                    (name, dept)
                                    VALUES ('Dave', 'eng');
                                  COMMIT
  SELECT * FROM employees
    WHERE dept = 'eng';
  → Returns 4 rows  ← PHANTOM!
  -- Dave appeared out of nowhere
  COMMIT
```

```sql
-- Real-world example: check-then-insert race
-- Session 1:
BEGIN;
SELECT COUNT(*) FROM reservations
  WHERE room_id = 101 AND date = '2025-03-15';
-- Returns 0, room appears available

-- Session 2:
BEGIN;
INSERT INTO reservations (room_id, date, guest)
  VALUES (101, '2025-03-15', 'Smith');
COMMIT;

-- Session 1 (continued):
INSERT INTO reservations (room_id, date, guest)
  VALUES (101, '2025-03-15', 'Jones');
COMMIT;
-- DOUBLE BOOKING! Both sessions saw 0 reservations
```

**Note:** Non-repeatable read is about a row being *modified*; phantom read is about rows being *added or removed* from a result set. The distinction matters because they require different mechanisms to prevent: row locks vs predicate/gap locks.

**Prevented by:** SERIALIZABLE (and, in practice, REPEATABLE READ in PostgreSQL via its snapshot and in InnoDB via snapshot reads + gap locks; see 3.3).

### 2.4 Lost Update

Two transactions read the same row, then both update it based on what they read. One update overwrites the other, and the first update is silently lost.

```
Timeline:
  T1                              T2
  ──────────────────────────────  ──────────────────────────────
  BEGIN
  SELECT balance FROM accounts
    WHERE id = 1;
  → Returns 1000
                                  BEGIN
                                  SELECT balance FROM accounts
                                    WHERE id = 1;
                                  → Returns 1000
  -- Deposit $200
  UPDATE accounts
    SET balance = 1200         -- 1000 + 200
    WHERE id = 1;
                                  -- Deposit $300
                                  UPDATE accounts
                                    SET balance = 1300  -- 1000 + 300
                                    WHERE id = 1;
  COMMIT
                                  COMMIT
  -- Final balance: 1300
  -- Expected: 1500 (1000 + 200 + 300)
  -- T1's deposit of $200 is LOST!
```

```sql
-- Real-world example: inventory decrement
-- Session 1:
BEGIN;
SELECT quantity FROM inventory WHERE product_id = 42;
-- Returns 10

-- Session 2:
BEGIN;
SELECT quantity FROM inventory WHERE product_id = 42;
-- Returns 10

-- Session 1:
UPDATE inventory SET quantity = 9 WHERE product_id = 42;  -- 10 - 1
COMMIT;

-- Session 2:
UPDATE inventory SET quantity = 8 WHERE product_id = 42;  -- 10 - 2
COMMIT;
-- Final: 8. Expected: 7 (10 - 1 - 2). One decrement is LOST.

-- FIX: use atomic update
UPDATE inventory SET quantity = quantity - 1 WHERE product_id = 42;
```

**Prevented by:** Using `SELECT ... FOR UPDATE`, or atomic updates (`SET x = x + 1`), or REPEATABLE READ and above (depending on database).

### 2.5 Write Skew

The most subtle anomaly. Two transactions each read a set of rows, check a condition based on what they read, then each write to *different* rows in a way that violates the condition if both commits succeed.

```
Timeline (hospital on-call constraint: at least 1 doctor on call):

  T1 (Dr. Alice)                  T2 (Dr. Bob)
  ──────────────────────────────  ──────────────────────────────
  BEGIN                           BEGIN
  SELECT COUNT(*) FROM doctors
    WHERE on_call = true;
  → Returns 2 (Alice & Bob)
                                  SELECT COUNT(*) FROM doctors
                                    WHERE on_call = true;
                                  → Returns 2 (Alice & Bob)

  -- "2 on call, safe for me
  --  to leave"
  UPDATE doctors
    SET on_call = false
    WHERE name = 'Alice';
                                  -- "2 on call, safe for me
                                  --  to leave"
                                  UPDATE doctors
                                    SET on_call = false
                                    WHERE name = 'Bob';
  COMMIT
                                  COMMIT

  -- RESULT: 0 doctors on call!
  -- Constraint violated. Neither transaction saw the other's write
  -- because they wrote to DIFFERENT rows.
```

```sql
-- Real-world example: meeting room double-booking (write skew variant)
-- Constraint: no overlapping bookings for the same room

-- Session 1:
BEGIN;
SELECT COUNT(*) FROM bookings
  WHERE room_id = 5
    AND start_time < '14:00' AND end_time > '13:00';
-- Returns 0, no conflict

-- Session 2:
BEGIN;
SELECT COUNT(*) FROM bookings
  WHERE room_id = 5
    AND start_time < '14:00' AND end_time > '13:00';
-- Returns 0, no conflict

-- Session 1:
INSERT INTO bookings (room_id, start_time, end_time, user_id)
  VALUES (5, '13:00', '14:00', 101);
COMMIT;

-- Session 2:
INSERT INTO bookings (room_id, start_time, end_time, user_id)
  VALUES (5, '13:30', '14:30', 102);
COMMIT;
-- OVERLAP! Both checked, both found no conflict, both inserted.
```

**Why write skew is dangerous:** It is not caught by row-level locks because the two transactions modify *different* rows. It requires predicate locking or serializable isolation.

**Prevented by:** SERIALIZABLE only (not even REPEATABLE READ or SNAPSHOT ISOLATION).

### 2.6 Read Skew

A transaction reads two *related* items at different points in time and sees an inconsistent pair because another transaction modified one between the reads.

```
Timeline (constraint: x + y = 100):

  T1                              T2
  ──────────────────────────────  ──────────────────────────────
  BEGIN
  SELECT x FROM t;
  → Returns 50  (x=50, y=50)
                                  BEGIN
                                  UPDATE t SET x = 25;
                                  UPDATE t SET y = 75;
                                  COMMIT
                                  (x=25, y=75, still sums to 100)
  SELECT y FROM t;
  → Returns 75
  -- T1 sees x=50, y=75
  -- Sum = 125, which is WRONG
  -- Inconsistent snapshot!
  COMMIT
```

```sql
-- Real-world example: backup reads inconsistent state
-- Session 1 (backup process, READ COMMITTED):
BEGIN;
SELECT * FROM accounts WHERE id BETWEEN 1 AND 1000;
-- Reads account 500: balance = $1000

-- Session 2 (transfer):
BEGIN;
UPDATE accounts SET balance = balance - 200 WHERE id = 500;
UPDATE accounts SET balance = balance + 200 WHERE id = 1500;
COMMIT;

-- Session 1 (continued):
SELECT * FROM accounts WHERE id BETWEEN 1001 AND 2000;
-- Reads account 1500: balance = $1200 (after transfer)
-- Backup has: account 500 = $1000 AND account 1500 = $1200
-- Total money in backup is $200 more than reality!
```

**Prevented by:** REPEATABLE READ / SNAPSHOT ISOLATION and above.

### 2.7 Serialization Anomaly

Any result that could not have been produced by some serial ordering of the committed transactions. Write skew and read skew are specific types of serialization anomalies, but there are others.

```
Timeline (constraint: rows are numbered sequentially):

  T1                              T2
  ──────────────────────────────  ──────────────────────────────
  BEGIN                           BEGIN
  INSERT INTO t VALUES
    (SELECT MAX(id)+1 FROM t);
  -- Reads max=5, inserts id=6
                                  INSERT INTO t VALUES
                                    (SELECT MAX(id)+1 FROM t);
                                  -- Also reads max=5, inserts id=6
  COMMIT
                                  COMMIT
  -- DUPLICATE id=6!
  -- No serial order produces this result.
  -- If T1 ran first: T1 inserts 6, T2 inserts 7 (correct)
  -- If T2 ran first: T2 inserts 6, T1 inserts 7 (correct)
```

```sql
-- Classic example: mutual dependency
-- Session 1:
BEGIN;
INSERT INTO t1 SELECT COUNT(*) FROM t2;

-- Session 2:
BEGIN;
INSERT INTO t2 SELECT COUNT(*) FROM t1;

-- If T1 first: t2 has 0 rows → T1 inserts 0, then T2 reads 1 row, inserts 1
-- If T2 first: t1 has 0 rows → T2 inserts 0, then T1 reads 1 row, inserts 1
-- With SI:    Both read 0, both insert 0 → neither serial order gives (0,0)
```

### 2.8 Anomaly Summary Table

| Anomaly | Description | Prevented Starting At |
|---------|-------------|----------------------|
| Dirty Read | Read uncommitted data from another txn | READ COMMITTED |
| Non-Repeatable Read | Same row, two reads, different values | REPEATABLE READ |
| Phantom Read | Range query returns new rows on re-execution | SERIALIZABLE* |
| Lost Update | Two read-modify-write cycles, one silently lost | REPEATABLE READ** |
| Write Skew | Two txns read overlapping set, write disjoint set, break invariant | SERIALIZABLE |
| Read Skew | Two reads of related data see inconsistent state | REPEATABLE READ / SI |
| Serialization Anomaly | Any result impossible under serial execution | SERIALIZABLE |

\* PostgreSQL's REPEATABLE READ (snapshot) and InnoDB's REPEATABLE READ (snapshot + gap locks) also prevent phantoms in most cases.
\** Depends on the database implementation: PostgreSQL's REPEATABLE READ (SI) detects lost updates and aborts with `40001` (first-updater-wins). InnoDB's REPEATABLE READ does **not**: a plain `SELECT` reads the snapshot, the later `UPDATE` reads the *latest committed* version and silently overwrites -- the lost update happens. InnoDB only prevents it if the read is a locking read (`FOR UPDATE`) or the update is atomic (`SET x = x + 1`).

### 2.9 Beyond the Classic List

Two more anomalies that the SQL standard's table leaves out but that matter in practice:

**Dirty write (P0).** T2 overwrites a row T1 wrote but hasn't committed. If T1 then rolls back, what should the row contain? Every real database prevents this at *every* isolation level, including READ UNCOMMITTED, by holding write locks until commit. It's the reason even the weakest level still has writers blocking writers.

**Read-only transaction anomaly (Fekete, O'Neil & O'Neil, 2004).** Under snapshot isolation, even a transaction that only *reads* can observe a state no serial order produces:

```
Accounts: checking = 0, savings = 0. Rule: withdrawing from checking when
checking + savings < 0 after the withdrawal costs a $1 overdraft fee.

T1 (withdraw 10 from checking):  reads checking=0, savings=0   (snapshot)
T2 (deposit 20 to savings):      savings = 20; COMMIT
T3 (read-only report):           reads checking=0, savings=20  → "total 20"
T1: sees 0 + 0 - 10 < 0 → charges fee: checking = -11; COMMIT

Final: checking=-11, savings=20 → T1 is serialized BEFORE T2 (it didn't see
the deposit). But T3 already reported T2's deposit without T1's
withdrawal, which is only possible if T2 ran BEFORE T1. Contradiction.
```

Without T3 the history is serializable (T1, T2). The read-only report is what makes it anomalous -- which is why PostgreSQL SSI tracks read-only transactions too, and why `SERIALIZABLE READ ONLY DEFERRABLE` exists: it waits for a snapshot where this cannot happen.

For the precise, implementation-independent definitions (G0 dirty write, G1 dirty/aborted reads, G2 anti-dependency cycles), see Adya's thesis in Appendix B.

---

## 3. Isolation Levels

### 3.1 READ UNCOMMITTED

The weakest level. Transactions can see uncommitted changes from other transactions (dirty reads). Almost never used in practice.

```sql
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;

-- In PostgreSQL, READ UNCOMMITTED is treated as READ COMMITTED.
-- PostgreSQL does not actually implement dirty reads.
-- In SQL Server, it is real and is sometimes used with NOLOCK hints
-- for reporting queries that tolerate stale data.
```

**Use case:** Very rare. Sometimes used in SQL Server for approximate analytics where absolute precision is not required and blocking must be avoided at all costs.

### 3.2 READ COMMITTED

Each statement sees only data committed before the *statement* began. Different statements within the same transaction can see different snapshots.

```
READ COMMITTED behavior:

  T1                              T2
  ──────────────────────────────  ──────────────────────────────
  BEGIN
  SELECT * FROM t WHERE x = 1;
  → Sees rows as of this moment
                                  BEGIN
                                  INSERT INTO t VALUES (1, 'new');
                                  COMMIT
  SELECT * FROM t WHERE x = 1;
  → Sees the new row! (new snapshot per statement)
  COMMIT
```

This is the **default in PostgreSQL and Oracle**.

**How PostgreSQL implements READ COMMITTED:**

Each SQL statement acquires a new snapshot at the start of the statement. The snapshot records the XID bounds (`xmin`, `xmax`) and the list of XIDs still *in progress* at that point; an XID counts as committed-before-the-snapshot if it is below `xmax`, not in that list, and marked committed in pg_xact. Rows whose `xmin` (creating transaction) is committed in that sense are visible; rows whose `xmax` (deleting transaction) is committed in that sense are invisible.

### 3.3 REPEATABLE READ

The transaction sees a consistent snapshot taken at the start of the *transaction* (not each statement). All reads within the transaction see the same data.

```
REPEATABLE READ behavior:

  T1                              T2
  ──────────────────────────────  ──────────────────────────────
  BEGIN  ← snapshot taken here
  SELECT * FROM t WHERE x = 1;
  → Returns {A, B}
                                  BEGIN
                                  INSERT INTO t VALUES (1, 'C');
                                  COMMIT
  SELECT * FROM t WHERE x = 1;
  → Still returns {A, B}  (snapshot hasn't changed)
  COMMIT
```

This is the **default in MySQL/InnoDB**.

**Critical difference between databases:**

| Database | REPEATABLE READ Implementation | Phantoms? | Write Skew? |
|----------|-------------------------------|-----------|-------------|
| PostgreSQL | Snapshot Isolation (MVCC) | Prevented (snapshot) | ALLOWED |
| MySQL/InnoDB | MVCC snapshot for plain reads + next-key locks for locking reads/writes | Plain reads: prevented (snapshot). Locking reads: prevented (next-key locks). **Mixing the two: allowed** (see 9.8) | ALLOWED with plain `SELECT`; prevented only if the check uses `FOR SHARE`/`FOR UPDATE` |
| SQL Server | Lock-based (S locks held to commit) | ALLOWED | Mostly prevented: S locks on read rows turn the race into a deadlock (one victim aborts) |

In PostgreSQL, REPEATABLE READ is really Snapshot Isolation. It does not use locks for reads, so it cannot prevent write skew. In InnoDB, plain `SELECT` statements at REPEATABLE READ are *consistent non-locking reads* -- they take **no locks at all**, so they cannot prevent write skew or lost updates either. Next-key locks (row lock + gap lock) apply only to locking reads (`FOR SHARE`/`FOR UPDATE`), `UPDATE`, and `DELETE`. InnoDB also has **no write-write conflict check** at commit: an `UPDATE` simply operates on the latest committed row version, even if that version is newer than the transaction's snapshot.

### 3.4 SERIALIZABLE

The strongest standard level. The result of any set of concurrent transactions is equivalent to some serial ordering. All anomalies are prevented.

**PostgreSQL: Serializable Snapshot Isolation (SSI)**

PostgreSQL implements SERIALIZABLE using SSI, which is based on Snapshot Isolation plus detection of dangerous read-write conflicts (rw-antidependencies).

```
SSI Conflict Detection:

  T1 reads X → T2 writes X   (T1 has rw-antidependency on T2 for X)
  T2 reads Y → T1 writes Y   (T2 has rw-antidependency on T1 for Y)

  This forms a "dangerous structure" (cycle of length 2 in the
  serialization graph). SSI detects this and aborts one transaction.

  ┌──────────┐   rw-antidep    ┌──────────┐
  │    T1    │ ───────────────► │    T2    │
  │          │ ◄─────────────── │          │
  └──────────┘   rw-antidep    └──────────┘

  SSI aborts one of {T1, T2} with:
  ERROR: could not serialize access due to read/write dependencies
         among transactions
```

**MySQL/InnoDB: Lock-based SERIALIZABLE**

InnoDB implements SERIALIZABLE by implicitly converting all `SELECT` statements to `SELECT ... FOR SHARE` (shared locks on every row read). This prevents concurrent writes to any row read by the transaction.

```sql
-- InnoDB SERIALIZABLE: implicit locking
SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;
BEGIN;
SELECT * FROM accounts WHERE id = 1;
-- Internally: SELECT * FROM accounts WHERE id = 1 FOR SHARE;
-- Shared lock placed on row id=1
-- Any concurrent UPDATE/DELETE on id=1 will block until this txn commits
```

### 3.5 SNAPSHOT ISOLATION (SI)

Not in the SQL standard, but widely implemented. Each transaction sees a consistent snapshot of the database as of the transaction's start time. Writes are checked for conflicts at commit time (first-committer-wins).

```
Snapshot Isolation: First-Committer-Wins Rule

  T1                              T2
  ──────────────────────────────  ──────────────────────────────
  BEGIN (snapshot at time=100)     BEGIN (snapshot at time=101)
  UPDATE t SET x = 10
    WHERE id = 1;
                                  UPDATE t SET x = 20
                                    WHERE id = 1;
                                  -- BLOCKS (waiting for T1)
  COMMIT (succeeds, T1 is first
          committer for id=1)
                                  -- T2 now detects conflict:
                                  -- Row id=1 was modified after
                                  -- T2's snapshot. ABORT!
                                  ERROR: could not serialize access
                                  due to concurrent update
```

**First-committer-wins vs first-updater-wins:** The textbook SI rule (Berenson et al.) checks for write-write conflicts at commit. PostgreSQL, Oracle, and most real engines implement the *first-updater-wins* variant shown above: the second writer blocks on the row lock at `UPDATE` time, and when the first writer commits, the second gets the error immediately (if the first aborts, the second proceeds). Same guarantees, but the conflict surfaces earlier and costs less wasted work.

**SI vs SERIALIZABLE:**

SI prevents dirty reads, non-repeatable reads, phantoms, lost updates, and read skew. It does **not** prevent write skew or all serialization anomalies. This is why some databases (PostgreSQL) offer SSI as a level above SI.

**Databases and their actual isolation implementations:**

| Database | "REPEATABLE READ" is actually | "SERIALIZABLE" is actually |
|----------|------------------------------|---------------------------|
| PostgreSQL | Snapshot Isolation | SSI (Snapshot + conflict detection) |
| MySQL/InnoDB | REPEATABLE READ with gap locks | Lock-based serializability |
| Oracle | N/A (only RC and SERIALIZABLE) | Snapshot Isolation (!) |
| SQL Server | Lock-based RR (SI is a separate `SNAPSHOT` level) | Lock-based (key-range locks) |
| CockroachDB | N/A | SSI |

**Important:** Oracle's "SERIALIZABLE" is actually Snapshot Isolation and does NOT prevent write skew. This is a well-known discrepancy that has been documented in academic papers.

### 3.6 Serializable Snapshot Isolation (SSI)

SSI adds write-skew detection on top of Snapshot Isolation. It was first described by Cahill, Röhm, and Fekete (SIGMOD 2008) and implemented in PostgreSQL 9.1.

**How SSI works:**

1. Run using Snapshot Isolation (no read locks, readers never block writers).
2. Track rw-antidependencies: when T1 reads a row that T2 later writes (or vice versa).
3. As conflicts are detected (and again at commit), check for "dangerous structures" -- two consecutive rw-antidependency edges T1 → T2 → T3 through a "pivot" T2 (T1 may equal T3), the shape every SI cycle must contain.
4. If found, abort one transaction.

```
SSI Tracking Structures in PostgreSQL:

  SIREAD locks (predicate locks):
  ┌──────────────────────────────────────────────────┐
  │  These are NOT real locks. They never block.      │
  │  They are markers that say "T1 read this data."   │
  │                                                   │
  │  Granularities:                                   │
  │    - Tuple-level (row)                            │
  │    - Page-level (if too many tuple locks)          │
  │    - Relation-level (if too many page locks)       │
  │                                                   │
  │  Stored in a shared memory structure.              │
  │  Cleaned up after transactions complete.           │
  └──────────────────────────────────────────────────┘

  rw-conflict list:
  ┌──────────────────────────────────────────────────┐
  │  Directed edges: T_reader → T_writer              │
  │  When T_writer modifies data that T_reader read   │
  │  (from T_reader's snapshot), an edge is added.    │
  │                                                   │
  │  Dangerous structure detected when:               │
  │    T1 → T2 → T3 and T3 committed first           │
  │    (pivot: T2 has both in and out edges)           │
  └──────────────────────────────────────────────────┘
```

**SSI Performance:**

SSI typically adds 5-10% overhead compared to SI for read-heavy workloads. For write-heavy workloads with high contention, the abort rate can be significant. The key advantage over lock-based serializability is that reads never block writes, providing much better throughput for mixed workloads.

### 3.7 Why Different Databases Chose Different Defaults

| Database | Default Level | Rationale |
|----------|--------------|-----------|
| PostgreSQL | READ COMMITTED | Conservative default; avoids blocking. Most web apps work fine with RC. Developers opt in to stronger isolation when needed. |
| MySQL/InnoDB | REPEATABLE READ | Historical: InnoDB's gap locking made RR cheap. Also, MySQL's binlog-based replication required RR for STATEMENT-based replication to work correctly. |
| Oracle | READ COMMITTED | Performance-oriented default. Oracle's undo-based MVCC makes RC very efficient. Their "SERIALIZABLE" is actually SI, reflecting a design philosophy that favors throughput. |
| SQL Server | READ COMMITTED | Cheapest useful lock-based level (note: the SQL standard's own default is SERIALIZABLE). Offers READ_COMMITTED_SNAPSHOT and SNAPSHOT as opt-in alternatives that don't block reads. |
| CockroachDB | SERIALIZABLE | For years the only level (READ COMMITTED is available as an opt-in since v24.1). Designed for correctness-first in distributed systems. The cost of debugging anomalies in distributed systems is too high. |

### 3.8 Complete Anomaly Prevention Matrix

```
┌──────────────────┬───────────┬────────────┬──────────┬──────────┬──────────┬──────────┬──────────┐
│ Isolation Level  │  Dirty    │ Non-Repeat │ Phantom  │  Lost    │  Write   │  Read    │ Serial.  │
│                  │  Read     │ Read       │ Read     │  Update  │  Skew    │  Skew    │ Anomaly  │
├──────────────────┼───────────┼────────────┼──────────┼──────────┼──────────┼──────────┼──────────┤
│ READ UNCOMMITTED │ Possible  │ Possible   │ Possible │ Possible │ Possible │ Possible │ Possible │
│ READ COMMITTED   │ Prevented │ Possible   │ Possible │ Possible │ Possible │ Possible │ Possible │
│ REPEATABLE READ  │ Prevented │ Prevented  │ Possible*│ Depends**│ Possible │ Prevented│ Possible │
│ SNAPSHOT ISOL.   │ Prevented │ Prevented  │ Prevented│ Prevented│ Possible │ Prevented│ Possible │
│ SERIALIZABLE     │ Prevented │ Prevented  │ Prevented│ Prevented│ Prevented│ Prevented│ Prevented│
└──────────────────┴───────────┴────────────┴──────────┴──────────┴──────────┴──────────┴──────────┘

*  InnoDB's REPEATABLE READ prevents phantoms for plain reads (snapshot) and
   locking reads (gap locks); PostgreSQL's RR prevents them via the snapshot
** PostgreSQL's RR (SI) aborts the second writer (40001). InnoDB's RR does NOT
   detect lost updates for read-then-write with a plain SELECT (see 2.8)
```

---

## 4. Concurrency Control Mechanisms

### 4.1 Two-Phase Locking (2PL)

The oldest and most well-understood concurrency control protocol. It guarantees conflict-serializability by dividing each transaction into two phases.

**The Two Phases:**

```
┌────────────────────────────────────────────────────────────────────────┐
│                     Two-Phase Locking Protocol                         │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  Growing Phase          │ Lock Point          │ Shrinking Phase         │
│  ───────────────────────┼─────────────────────┼───────────────────────  │
│  Acquire locks          │ Last lock acquired  │ Release locks           │
│  No lock released       │                     │ No lock acquired        │
│                                                                        │
│  ┌───┐ ┌───┐ ┌───┐                      ┌───┐ ┌───┐ ┌───┐             │
│  │ S │ │ X │ │ S │     LOCK              │-S │ │-S │ │-X │             │
│  │ L1│ │ L2│ │ L3│     POINT             │ L1│ │ L3│ │ L2│             │
│  └───┘ └───┘ └───┘                      └───┘ └───┘ └───┘             │
│  ◄──────────────────────►◄──────────────────────────────►              │
│      No releases here        No acquisitions here                      │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

**2PL Variants:**

```
Basic 2PL:
  Growing ──► Lock Point ──► Shrinking ──► End
  Problem: cascading aborts (released data might be read by others)

Strict 2PL (S2PL):
  Growing ──► Lock Point ──► Hold ALL exclusive (X) locks until commit/abort
  Releases shared locks during shrinking phase.
  Prevents cascading aborts for writes.

Rigorous 2PL (SS2PL):
  Growing ──► Lock Point ──► Hold ALL locks (S and X) until commit/abort
  No shrinking phase at all; all locks released at once.
  Used by most real databases. Simplifies implementation.
  Guarantees strict serializability.
```

**Lock Types:**

| Lock Type | Abbreviation | Purpose | Compatible With |
|-----------|-------------|---------|-----------------|
| Shared | S | Read access | S, IS |
| Exclusive | X | Write access | Nothing |
| Intent Shared | IS | Intent to acquire S lock on descendant | IS, IX, S, SIX |
| Intent Exclusive | IX | Intent to acquire X lock on descendant | IS, IX |
| Shared + Intent Exclusive | SIX | Hold S on this level, intent X on descendant | IS |

**Lock Compatibility Matrix:**

```
          ┌─────┬─────┬─────┬─────┬─────┐
          │  S  │  X  │ IS  │ IX  │ SIX │
    ┌─────┼─────┼─────┼─────┼─────┼─────┤
    │  S  │  Y  │  N  │  Y  │  N  │  N  │
    │  X  │  N  │  N  │  N  │  N  │  N  │
    │ IS  │  Y  │  N  │  Y  │  Y  │  Y  │
    │ IX  │  N  │  N  │  Y  │  Y  │  N  │
    │ SIX │  N  │  N  │  Y  │  N  │  N  │
    └─────┴─────┴─────┴─────┴─────┴─────┘
    Y = Compatible (both can be held simultaneously)
    N = Conflict (requester must wait)
```

**Lock Granularity and Escalation:**

```
Lock Hierarchy:
                    ┌──────────┐
                    │ DATABASE │  (coarsest)
                    └─────┬────┘
                          │
                    ┌─────┴────┐
                    │  TABLE   │
                    └─────┬────┘
                          │
                    ┌─────┴────┐
                    │   PAGE   │
                    └─────┬────┘
                          │
                    ┌─────┴────┐
                    │   ROW    │  (finest)
                    └──────────┘

Intent locks allow the lock manager to quickly determine if
a coarse-grained lock conflicts with any fine-grained lock
WITHOUT scanning all fine-grained locks.

Example:
  T1 holds row-level X lock on row 42 in table employees.
  The lock manager also holds IX on the page, IX on the table.

  T2 wants table-level S lock on employees.
  Lock manager checks: S conflicts with IX? Yes! → T2 waits.
  No need to scan all row locks.

Lock Escalation:
  When a transaction holds too many fine-grained locks (e.g., >5000 row
  locks on a table), the database ESCALATES to a coarser granularity:

  5000 row locks → 1 table lock
  Pros: Less memory for lock manager
  Cons: Reduced concurrency (blocks entire table)

  SQL Server escalates at ~5000 locks per table by default.
  PostgreSQL does NOT escalate row locks: they live in the tuple header
    (xmax + infomask bits), not in the lock table, so they cost no shared
    memory. (Only SSI predicate locks escalate: tuple → page → relation.)
  InnoDB does NOT escalate: row locks are a bitmap per (transaction, page)
    in the lock_sys hash table -- one lock_t covers every locked row on a
    page, so thousands of row locks cost a few KB.
  See Section 9.2 for where each engine physically stores row locks.
```

**Deadlock Detection:**

```
Wait-For Graph:

  T1 waits for T2 (T2 holds lock on row A)
  T2 waits for T3 (T3 holds lock on row B)
  T3 waits for T1 (T1 holds lock on row C)

  ┌────┐     ┌────┐     ┌────┐
  │ T1 │────►│ T2 │────►│ T3 │
  │    │◄────────────────│    │
  └────┘                 └────┘

  Cycle detected! → DEADLOCK
  Resolution: Abort the "youngest" transaction (lowest cost to redo)
              or the transaction that has done the least work.

  PostgreSQL: A waiter sleeps for deadlock_timeout (default 1s), THEN runs
              the detector once. Most lock waits resolve before that, so
              the (expensive) graph walk is usually skipped.
  InnoDB: Checks on every lock wait (immediate detection). Under extreme
          contention on hot rows this check itself becomes the bottleneck;
          innodb_deadlock_detect=OFF + a short innodb_lock_wait_timeout is
          the documented escape hatch.
  Oracle: The waiting session checks after a short (~3s) enqueue wait
          (RAC: the LMD background process finds global deadlocks). It
          rolls back only the victim's current STATEMENT (ORA-00060),
          not the whole transaction.
```

**Deadlock Prevention (alternative to detection):**

```
Wait-Die (non-preemptive, older waits, younger dies):
  If T_old wants lock held by T_young → T_old WAITS
  If T_young wants lock held by T_old → T_young DIES (abort + restart)
  No cycles possible: older transactions never abort for younger ones.

Wound-Wait (preemptive, older wounds younger):
  If T_old wants lock held by T_young → T_young is WOUNDED (aborted)
  If T_young wants lock held by T_old → T_young WAITS
  No cycles possible: younger transactions never preempt older ones.

  Wound-Wait tends to cause fewer total aborts than Wait-Die because
  it kills transactions that have done less work.
```

### 4.2 Multi-Version Concurrency Control (MVCC)

MVCC is the dominant concurrency control mechanism in modern databases. The core insight: instead of blocking readers with locks, maintain multiple versions of each row. Readers see an old version consistent with their snapshot; writers create new versions.

```
Core MVCC Principle:

  Readers NEVER block Writers.
  Writers NEVER block Readers.
  Writers only block other Writers (to the same row).

  ┌────────────────────────────────────────────────────────────────┐
  │  Physical Row Storage (conceptual)                             │
  │                                                                │
  │  Row id=1:                                                     │
  │    Version 3: {balance: 800}  created by T103, current         │
  │        ↓                                                       │
  │    Version 2: {balance: 1000} created by T101, superseded      │
  │        ↓                                                       │
  │    Version 1: {balance: 500}  created by T99, superseded       │
  │                                                                │
  │  Transaction T105 (snapshot at T102):                           │
  │    Sees Version 2 (T101 committed before T102)                 │
  │    Does NOT see Version 3 (T103 committed after T102)          │
  │                                                                │
  │  Transaction T108 (snapshot at T106):                           │
  │    Sees Version 3 (T103 committed before T106)                 │
  │                                                                │
  └────────────────────────────────────────────────────────────────┘
```

#### 4.2.1 PostgreSQL MVCC

PostgreSQL stores all row versions (tuples) directly in the table heap. Each tuple has metadata fields:

```
PostgreSQL Tuple Header:
┌─────────────────────────────────────────────────────────────┐
│  xmin    │ Transaction ID that created this tuple version    │
│  xmax    │ Transaction ID that deleted/updated this tuple,   │
│          │ OR that merely row-LOCKED it (FOR UPDATE/SHARE),  │
│          │ OR a MultiXactId if several txns hold locks       │
│          │ (0 if never deleted or locked)                    │
│  cmin    │ Command ID within xmin's transaction              │
│  cmax    │ Command ID within xmax's transaction              │
│  ctid    │ Physical location (page, offset) of next version  │
│  infomask│ Bit flags: committed, aborted, frozen, etc.       │
└─────────────────────────────────────────────────────────────┘
```

**Visibility Check Algorithm:**

```python
def is_visible(tuple, snapshot):
    """
    Simplified PostgreSQL visibility check.
    snapshot.xmin = oldest active txn at snapshot time
    snapshot.xmax = next txn ID at snapshot time
    snapshot.active = set of txn IDs active at snapshot time
    """
    # Was the creating transaction committed before our snapshot?
    if tuple.xmin not in snapshot.active and tuple.xmin < snapshot.xmax:
        if not is_committed(tuple.xmin):
            return False  # creator aborted
        # tuple was created before our snapshot
    else:
        return False  # creator is still active or started after us

    # Was the tuple deleted?
    if tuple.xmax == 0:
        return True  # not deleted

    if tuple.xmax not in snapshot.active and tuple.xmax < snapshot.xmax:
        if is_committed(tuple.xmax):
            return False  # deleted before our snapshot
    # else: deleter is still active or started after us → tuple still visible
    # (Real code first checks HEAP_XMAX_LOCK_ONLY: if xmax is only a row
    #  locker -- SELECT FOR UPDATE / FOR KEY SHARE -- the tuple is visible
    #  regardless. See Section 9.2.)

    return True
```

**pg_xact (formerly clog):**

PostgreSQL maintains a commit log (`pg_xact`) -- a bitmap where each transaction ID maps to a 2-bit status: `IN_PROGRESS`, `COMMITTED`, `ABORTED`, `SUB_COMMITTED`. This is consulted during visibility checks.

```
pg_xact structure:
┌───────────────────────────────────────────────────────┐
│  TxID  │  Status bits                                 │
│  100   │  COMMITTED     (01)                          │
│  101   │  COMMITTED     (01)                          │
│  102   │  ABORTED       (10)                          │
│  103   │  IN_PROGRESS   (00)                          │
│  104   │  SUB_COMMITTED (11)  (subxact, parent open)  │
│  ...                                                  │
└───────────────────────────────────────────────────────┘
  Stored in 8KB pages under pg_xact/ directory (4 xids per byte,
  32K xids per page). Cached in a shared-memory SLRU buffer.

  Hint bits: after the first lookup, the reader sets HEAP_XMIN_COMMITTED /
  HEAP_XMAX_INVALID etc. in the tuple's infomask so later readers skip
  pg_xact. Side effect: a plain SELECT can DIRTY pages (and, with
  checksums/wal_log_hints, write WAL) -- "why is my read query writing?".
```

**Visibility Map:**

```
Visibility Map (VM): all-visible bit per heap page (shown below;
                     9.6+ adds a second, all-frozen bit per page)
┌─────────────────────────────────────────────────────────┐
│  Page 0: 1  (all tuples visible to all active txns)     │
│  Page 1: 0  (has some dead/invisible tuples)            │
│  Page 2: 1                                              │
│  Page 3: 0                                              │
│  ...                                                    │
└─────────────────────────────────────────────────────────┘

Used by:
  - Index-only scans: if page is all-visible, no need to
    check the heap (huge performance win).
  - VACUUM: can skip all-visible pages.
```

**VACUUM (Garbage Collection):**

```
VACUUM Process:
  1. Scan heap pages (skip all-visible pages via visibility map)
  2. For each dead tuple (xmax committed and no active txn needs it):
     a. Remove index entries pointing to dead tuple
     b. Mark heap space as reusable (add to free space map)
  3. Update visibility map
  4. Optionally freeze old tuples (9.4+: set the HEAP_XMIN_FROZEN infomask
     bits; older releases overwrote xmin with FrozenTransactionId)
     to prevent transaction ID wraparound

VACUUM FULL:
  Rewrites the entire table to reclaim space back to the OS.
  Requires exclusive table lock. Use rarely.

Autovacuum:
  Background workers triggered when dead tuple count exceeds threshold.
  Default: autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor * n_live_tuples
  Example: 50 + 0.2 * 10000 = 2050 dead tuples triggers autovacuum

Common problem: Long-running transactions prevent VACUUM from reclaiming
tuples because those old snapshots might still need to see them.
This causes TABLE BLOAT -- the table grows and grows with dead tuples.
```

#### 4.2.2 InnoDB MVCC

InnoDB stores only the *latest* version in the clustered index (primary key B-tree). Old versions are reconstructed from undo log segments.

```
InnoDB Version Chain:

  Clustered Index (Primary Key B-tree):
  ┌──────────────────────────────────────────┐
  │  Row id=1: {balance: 800}                │
  │  DB_TRX_ID: 103 (last modifier)          │
  │  DB_ROLL_PTR: → undo log segment         │
  └───────────────────┬──────────────────────┘
                      │ (roll pointer)
                      ▼
  Undo Log Segment:
  ┌──────────────────────────────────────────┐
  │  Previous version: {balance: 1000}       │
  │  TRX_ID: 101                             │
  │  ROLL_PTR: → older undo record           │
  └───────────────────┬──────────────────────┘
                      │
                      ▼
  ┌──────────────────────────────────────────┐
  │  Even older version: {balance: 500}      │
  │  TRX_ID: 99                              │
  │  ROLL_PTR: NULL (oldest version)         │
  └──────────────────────────────────────────┘
```

**Read View:**

When a transaction starts (or each statement in READ COMMITTED), InnoDB creates a Read View:

```
InnoDB Read View:
┌────────────────────────────────────────────────┐
│  m_low_limit_id:  105   (next TRX_ID to be    │
│                          assigned)             │
│  m_up_limit_id:   102   (oldest active TRX_ID)│
│  m_ids:          [102, 103] (active TRX_IDs)  │
│  m_creator_trx_id: 104  (this transaction)    │
└────────────────────────────────────────────────┘

Visibility rule:
  If row.DB_TRX_ID < m_up_limit_id → VISIBLE (committed before snapshot)
  If row.DB_TRX_ID >= m_low_limit_id → NOT VISIBLE (started after snapshot)
  If row.DB_TRX_ID in m_ids → NOT VISIBLE (was active at snapshot time)
  Otherwise → VISIBLE
```

**Purge Thread:**

InnoDB's equivalent of PostgreSQL's VACUUM. The purge thread removes undo log records that no Read View needs anymore.

```
Purge Process:
  1. Find the oldest active Read View (oldest_view_trx_id)
  2. Any undo record with TRX_ID < oldest_view_trx_id can be purged
  3. Remove the undo record and associated index entries for
     delete-marked rows

Problem: Long-running transactions block purge, causing undo log growth.
Monitor: SHOW ENGINE INNODB STATUS → "History list length"
  - Normal: < 1000
  - Concerning: > 10000
  - Critical: > 100000 (undo tablespace growing rapidly)
```

#### 4.2.3 Oracle MVCC

Oracle uses an undo tablespace and System Change Numbers (SCN) instead of transaction IDs.

```
Oracle SCN-based MVCC:
  - Every committed change gets a monotonically increasing SCN
  - Each query/transaction records its snapshot SCN
  - To read a block, Oracle checks if the block's SCN > snapshot SCN
  - If yes, Oracle reconstructs an older version from undo tablespace

"Snapshot too old" error (ORA-01555):
  Occurs when undo records needed for reconstruction have been
  overwritten. Common with long-running queries and small undo
  tablespace.
```

#### 4.2.4 MVCC Overhead

| Problem | PostgreSQL | InnoDB | Oracle |
|---------|-----------|---------|--------|
| Table bloat | Dead tuples accumulate in heap | No (only latest in B-tree) | No (undo is separate) |
| Undo space growth | N/A | Undo log segments grow | Undo tablespace grows |
| GC mechanism | VACUUM (autovacuum) | Purge thread | Automatic undo management |
| GC trigger | Dead tuple threshold | Background continuous | Undo retention period |
| Long txn impact | Prevents dead tuple cleanup | Prevents undo purge | ORA-01555 risk |
| Index overhead | Dead index entries | Mostly clean indexes | Clean indexes |

### 4.3 Optimistic Concurrency Control (OCC)

OCC assumes conflicts are rare. Transactions execute without acquiring locks, then validate at commit time.

```
OCC Three Phases:

  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
  │  READ PHASE  │───►│  VALIDATION  │───►│ WRITE PHASE  │
  │              │    │   PHASE      │    │              │
  │ Read from DB │    │ Check for    │    │ Apply writes │
  │ Buffer writes│    │ conflicts    │    │ to DB        │
  │ in local     │    │ with other   │    │              │
  │ workspace    │    │ committed    │    │              │
  │              │    │ txns         │    │              │
  └──────────────┘    └──────────────┘    └──────────────┘
                          │
                     Conflict? ──Yes──► ABORT + RESTART
```

**Backward Validation:**

```
Backward Validation:
  For each transaction T_j that committed during T_i's read phase:
    Check: WriteSet(T_j) ∩ ReadSet(T_i) = ∅ ?
    If not empty → ABORT T_i (it read stale data)

  Example:
    T1 starts at time 10, reads rows A, B, C
    T2 commits at time 12, wrote row B
    T1 tries to commit at time 15:
      WriteSet(T2) = {B}
      ReadSet(T1) = {A, B, C}
      Intersection = {B} ≠ ∅
      → T1 must ABORT
```

**Forward Validation:**

```
Forward Validation:
  For each transaction T_j currently in its read phase:
    Check: WriteSet(T_i) ∩ ReadSet(T_j) = ∅ ?
    If not empty → either ABORT T_i or ABORT T_j

  More flexible than backward validation but requires knowing
  the read sets of active transactions.
```

**When OCC Works Well:**

| Scenario | OCC Suitability | Why |
|----------|----------------|-----|
| Low contention (few conflicts) | Excellent | Rarely aborts, no lock overhead |
| Read-heavy workload | Good | Reads have zero overhead |
| High contention | Poor | Frequent aborts waste work |
| Long transactions | Poor | Higher chance of conflict, more wasted work |
| Short transactions | Good | Less time to accumulate conflicts |

**Real-world usage:** Google's Percolator (built for incremental web indexing on Bigtable) uses snapshot isolation with optimistic, lock-at-commit writes; TiDB's original transaction model is Percolator-based (TiDB now defaults to *pessimistic* mode because OCC abort rates surprised MySQL-migrated apps). Spanner, by contrast, uses pessimistic 2PL with wound-wait for read-write transactions. Application-level OCC (a `version` column checked in `UPDATE ... WHERE version = :v`) is the most common form in practice.

### 4.4 Timestamp Ordering

Each transaction gets a unique timestamp at start. The protocol ensures that the execution is equivalent to running transactions in timestamp order.

**Basic Timestamp Ordering (BTO):**

```
Rules:
  Each data item X maintains:
    W-TS(X) = timestamp of last transaction that wrote X
    R-TS(X) = timestamp of last transaction that read X

  Transaction T with timestamp TS(T):

  READ X:
    If TS(T) < W-TS(X) → ABORT T
      (T is trying to read a value that was overwritten by a newer txn)
    Else → Allow read, set R-TS(X) = max(R-TS(X), TS(T))

  WRITE X:
    If TS(T) < R-TS(X) → ABORT T
      (T is trying to overwrite a value that a newer txn already read)
    If TS(T) < W-TS(X) → ABORT T (or use Thomas Write Rule)
      (T is trying to overwrite a value written by a newer txn)
    Else → Allow write, set W-TS(X) = TS(T)
```

**Thomas Write Rule:**

```
Thomas Write Rule (optimization):
  If TS(T) < W-TS(X):
    Instead of aborting, simply SKIP the write.
    Rationale: a newer transaction has already written X, so T's
    write is obsolete and would be overwritten anyway.

  This allows more transactions to succeed but sacrifices
  conflict-serializability. The result is view-serializable.
```

**Multi-Version Timestamp Ordering (MVTO):**

```
MVTO combines MVCC with timestamp ordering:
  Each write creates a new version with the writer's timestamp.
  Reads select the version with the largest timestamp ≤ TS(T).

  Version chain for item X:
    X_50: written by T50
    X_80: written by T80
    X_110: written by T110

  T95 reads X → gets X_80 (largest version ≤ 95)
  T120 reads X → gets X_110

  Writes only fail if a later transaction has already read an
  older version that this write would invalidate.
```

---

## 5. Lock Manager Implementation

The lock manager is one of the most performance-critical subsystems in a database engine. It must handle millions of lock requests per second with minimal latency.

### 5.1 Lock Table Structure

```
Lock Table (Hash Table):
┌─────────────────────────────────────────────────────────────────┐
│                                                                  │
│  Hash function: hash(resource_id) → bucket                       │
│                                                                  │
│  Bucket 0: ──► [Lock Entry: Table 'orders']                      │
│                   Grant Group: T1(S), T3(S)                      │
│                   Wait Queue: T5(X) → T7(X)                      │
│                                                                  │
│  Bucket 1: ──► [Lock Entry: Row orders.id=42]                    │
│                   Grant Group: T2(X)                              │
│                   Wait Queue: T4(S) → T6(S) → T8(X)             │
│                                                                  │
│  Bucket 2: ──► NULL                                              │
│                                                                  │
│  Bucket 3: ──► [Lock Entry: Table 'users'] ──►                   │
│                   Grant Group: T1(IS), T3(IX)                    │
│                   Wait Queue: (empty)                             │
│                [Lock Entry: Row users.id=7]                       │
│                   Grant Group: T3(X)                              │
│                   Wait Queue: T9(S)                               │
│                                                                  │
│  ...                                                             │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 5.2 Lock Request Processing

```
LOCK_REQUEST(txn T, resource R, mode M):
  1. hash_bucket = hash(R) mod num_buckets
  2. Acquire latch on hash_bucket  ← NOTE: latch, not lock!
  3. Search bucket chain for lock entry matching R
  4. If no entry exists:
     a. Create new lock entry for R
     b. Grant lock to T in mode M
     c. Release latch
     d. Return GRANTED
  5. If entry exists:
     a. Check compatibility: is M compatible with all granted modes?
     b. Check wait queue: is the wait queue empty?
        (even if compatible, must wait if others are already waiting
         to prevent starvation)
     c. If compatible AND no waiters:
        Grant lock to T in mode M
        Release latch
        Return GRANTED
     d. Else:
        Add T to wait queue with requested mode M
        Release latch
        Suspend T (block the thread/connection)
        Return WAITING
```

### 5.3 Lock Request Queue and Fairness

```
Lock Entry for Row orders.id=42:

  Granted Group:
  ┌──────────┬──────────┐
  │ T1 (S)   │ T2 (S)   │  ← Multiple shared locks can coexist
  └──────────┴──────────┘

  Wait Queue (FIFO):
  ┌──────────┐   ┌──────────┐   ┌──────────┐
  │ T3 (X)   │──►│ T4 (S)   │──►│ T5 (S)   │
  └──────────┘   └──────────┘   └──────────┘

  When T1 and T2 release their S locks:
    T3 gets X lock (first in queue)
    T4 and T5 must still wait (X blocks S)

  When T3 releases X lock:
    T4 and T5 can BOTH be granted S locks (compatible)
    This is called "group mode grant" or "batch wakeup"
```

### 5.4 Latch vs Lock: A Critical Distinction

```
┌─────────────────────────────┬──────────────────────────────────┐
│           LATCH              │             LOCK                 │
├─────────────────────────────┼──────────────────────────────────┤
│ Protects: in-memory data    │ Protects: logical data           │
│ structures (B-tree nodes,   │ (rows, tables, key ranges)       │
│ buffer pool pages, hash     │                                  │
│ buckets)                    │                                  │
├─────────────────────────────┼──────────────────────────────────┤
│ Duration: nanoseconds to    │ Duration: milliseconds to        │
│ microseconds                │ seconds (entire transaction)     │
├─────────────────────────────┼──────────────────────────────────┤
│ Implementation: CPU atomic  │ Implementation: lock manager     │
│ instructions (CAS, XCHG),  │ hash table, wait queues,         │
│ spin locks, mutexes         │ deadlock detection                │
├─────────────────────────────┼──────────────────────────────────┤
│ Deadlock handling: coding   │ Deadlock handling: wait-for      │
│ discipline (acquire in      │ graph, timeouts, abort + retry   │
│ fixed order), no detection  │                                  │
├─────────────────────────────┼──────────────────────────────────┤
│ Visible to user: NO         │ Visible to user: YES             │
│ (internal implementation    │ (pg_locks, SHOW ENGINE INNODB    │
│ detail)                     │ STATUS, lock wait timeouts)      │
├─────────────────────────────┼──────────────────────────────────┤
│ Modes: shared, exclusive    │ Modes: S, X, IS, IX, SIX,       │
│ (sometimes just exclusive)  │ key-range locks, predicate locks │
├─────────────────────────────┼──────────────────────────────────┤
│ WAL interaction: NOT logged │ WAL interaction: generally NOT   │
│ (latches are never recovered│ logged either -- after a crash   │
│ after crash)                │ all in-flight txns are rolled    │
│                             │ back, so their locks vanish.     │
│                             │ Exceptions: PREPAREd (2PC) txns  │
│                             │ re-acquire locks on recovery; PG │
│                             │ logs AccessExclusiveLocks so hot │
│                             │ standbys can replay them.        │
└─────────────────────────────┴──────────────────────────────────┘
```

**Why the distinction matters in practice:**

When someone says "the database is experiencing lock contention," you need to determine whether it is:

1. **Lock contention** (logical locks) -- visible via `pg_locks`, fix by reducing transaction duration, reordering operations, or changing isolation level.
2. **Latch contention** (internal) -- visible via `perf` or database-specific instrumentation (e.g., `pg_stat_activity` wait events). Fix by reducing hotspot access patterns, partitioning data, or upgrading hardware.

```sql
-- PostgreSQL: view current locks
SELECT pid, locktype, relation::regclass, mode, granted, waitstart
FROM pg_locks
WHERE NOT granted
ORDER BY waitstart;

-- PostgreSQL: wait events (latch contention shows as LWLock waits)
SELECT pid, wait_event_type, wait_event, state, query
FROM pg_stat_activity
WHERE wait_event IS NOT NULL;
```

### 5.5 Intent Locks and Hierarchical Locking

```
Hierarchical Locking Example:

  Transaction T1: UPDATE employees SET salary = 100000 WHERE id = 42;

  Lock acquisition order:
    1. IS lock on DATABASE         (intent: I'll read something in this DB)
       ... actually IX since we're updating ...
    2. IX lock on TABLE employees  (intent: I'll write something in this table)
    3. X  lock on ROW id=42        (exclusive: I'm writing this specific row)

  Transaction T2: LOCK TABLE employees IN EXCLUSIVE MODE;

  Lock acquisition:
    1. IX lock on DATABASE
    2. X lock on TABLE employees → BLOCKED by T1's IX lock!

  Without intent locks, T2 would have to scan every row lock
  in the employees table to determine if any conflicts exist.
  Intent locks provide an O(1) check at each level.
```

---

## 6. Distributed Transactions

### 6.1 Two-Phase Commit (2PC)

The classic protocol for atomic commits across multiple nodes.

```
Two-Phase Commit Protocol:

  Coordinator                  Participant A         Participant B
  ────────────────────────     ────────────────      ────────────────
  Phase 1: PREPARE
  ──────────────────
  Send PREPARE ──────────────► Receive PREPARE
                               Write redo/undo logs
                               Acquire all locks
                               ◄──────────────────── VOTE YES/NO
  Send PREPARE ────────────────────────────────────► Receive PREPARE
                                                     Write redo/undo logs
                                                     Acquire all locks
                               ◄──────────────────── VOTE YES/NO

  Phase 2: COMMIT/ABORT
  ──────────────────────
  If all YES:
    Write COMMIT to log
    Send COMMIT ─────────────► Apply changes
                               Release locks
                               ACK
    Send COMMIT ────────────────────────────────────► Apply changes
                                                     Release locks
                                                     ACK
  If any NO:
    Write ABORT to log
    Send ABORT ──────────────► Rollback
                               Release locks
    Send ABORT ─────────────────────────────────────► Rollback
                                                     Release locks
```

**2PC Problems:**

```
Problem 1: Blocking
  If the coordinator crashes after sending PREPARE but before
  sending COMMIT/ABORT:
    Participants are STUCK. They have voted YES, hold locks,
    and cannot unilaterally decide to commit or abort.
    They must wait for coordinator recovery.

    ┌──────────┐                ┌──────────┐
    │Coordinator│     PREPARE    │Participant│
    │  (crashed)│───────────────►│ (stuck!) │
    │     X     │                │ Voted YES │
    │           │  ← no COMMIT   │ Locks held│
    │           │     or ABORT   │ Waiting...│
    └──────────┘                └──────────┘

Problem 2: Latency
  Minimum 2 round trips (4 messages per participant).
  All participants hold locks during entire protocol.
  With cross-datacenter transactions, this can be 100-200ms.

Problem 3: Coordinator is a single point of failure
  If coordinator fails permanently, participants may be stuck
  indefinitely (data is locked, unavailable).
```

**2PC in Practice:**

```sql
-- PostgreSQL: prepared transactions (built-in 2PC support)
-- Set max_prepared_transactions > 0 in postgresql.conf

-- Participant node:
BEGIN;
UPDATE accounts SET balance = balance - 100 WHERE id = 1;
PREPARE TRANSACTION 'transfer_001';
-- Transaction is now in prepared state, survives crashes

-- Later, coordinator decides:
COMMIT PREPARED 'transfer_001';
-- or
ROLLBACK PREPARED 'transfer_001';

-- Monitor orphaned prepared transactions:
SELECT * FROM pg_prepared_xacts;
-- These HOLD LOCKS and PREVENT VACUUM. Clean them up!
```

### 6.2 Three-Phase Commit (3PC)

Adds a PRE-COMMIT phase to avoid the blocking problem of 2PC.

```
Three-Phase Commit:

  Phase 1: CAN-COMMIT?
    Coordinator → Participants: "Can you commit?"
    Participants → Coordinator: "Yes" or "No"

  Phase 2: PRE-COMMIT
    If all Yes:
      Coordinator → Participants: "Pre-commit" (prepare to commit)
      Participants → Coordinator: ACK
    If any No:
      Coordinator → Participants: ABORT

  Phase 3: DO-COMMIT
    Coordinator → Participants: "Do-commit"
    Participants commit and ACK

  If coordinator fails after Phase 2:
    Participants know everyone voted Yes (they received pre-commit).
    A new coordinator can safely decide to COMMIT.
    (In 2PC, participants wouldn't know if ALL voted Yes.)

  Tradeoff: 3 round trips instead of 2, higher latency.
  Rarely used in practice: network partitions still cause issues.
```

### 6.3 Saga Pattern

For long-running distributed transactions where holding locks across services is impractical. Instead of ACID, sagas provide eventual consistency through compensating actions.

```
Saga: Sequence of local transactions with compensating actions.

  Book Flight ──► Book Hotel ──► Charge Payment ──► Send Confirmation
       │              │               │
       ▼              ▼               ▼
  Cancel Flight  Cancel Hotel    Refund Payment    (compensating actions)

  If "Charge Payment" fails:
    1. Refund Payment (no-op, it failed)
    2. Cancel Hotel (compensating action)
    3. Cancel Flight (compensating action)
    → Run compensations in reverse order

  Choreography (event-driven):
  ┌────────┐  FlightBooked  ┌────────┐  HotelBooked  ┌─────────┐
  │ Flight │───────────────►│ Hotel  │───────────────►│ Payment │
  │ Svc    │                │ Svc    │                │ Svc     │
  │        │◄───────────────│        │◄───────────────│         │
  └────────┘  CancelFlight  └────────┘  CancelHotel   └─────────┘
                              (on failure)

  Orchestration (central coordinator):
  ┌─────────────┐
  │ Saga        │───► Book Flight ───► Book Hotel ───► Charge Payment
  │ Orchestrator│◄── result ◄── result ◄── result
  │             │
  │  On failure:│───► Cancel Hotel ───► Cancel Flight
  └─────────────┘
```

**Saga Guarantees:**

| Property | ACID Transaction | Saga |
|----------|-----------------|------|
| Atomicity | All-or-nothing | "All-or-compensate" (ACD) |
| Consistency | Strong | Eventual |
| Isolation | Full | None (intermediate states visible) |
| Durability | Yes | Yes (each step is durable) |

**The Isolation Problem with Sagas:**

Sagas have no isolation between steps. Other transactions can see intermediate states (e.g., flight booked but hotel not yet booked). Mitigation strategies include semantic locks (marking resources as "pending"), commutative operations, and reordering steps to reduce anomaly impact.

### 6.4 Calvin: Deterministic Database Protocol

Calvin (Yale, 2012) takes a radically different approach: if all nodes agree on the order of transactions *before* executing them, you don't need 2PC at all.

```
Calvin Architecture:

  ┌───────────────────────────────────────────────────┐
  │              Sequencer Layer                       │
  │  Collects transactions, batches them (10ms epochs)│
  │  Replicates batch to all replicas via Paxos/Raft  │
  │  ALL replicas agree on the SAME batch order        │
  └───────────────────────┬───────────────────────────┘
                          │ (deterministic order)
  ┌───────────────────────▼───────────────────────────┐
  │              Scheduler Layer                       │
  │  Analyzes read/write sets of each transaction      │
  │  Determines which transactions conflict            │
  │  Executes non-conflicting transactions in parallel │
  └───────────────────────┬───────────────────────────┘
                          │
  ┌───────────────────────▼───────────────────────────┐
  │              Storage Layer                         │
  │  Executes transactions deterministically           │
  │  Same input + same order = same output on all nodes│
  └───────────────────────────────────────────────────┘

Key insight: No 2PC needed! Since all nodes execute the same
transactions in the same order, they all reach the same state.
Replicas are always consistent.

Limitation: Transactions must declare their read/write sets upfront.
Interactive transactions (read → think → write) are difficult.
```

**FaunaDB (later Fauna; the service shut down in 2025) and other deterministic databases were inspired by Calvin.**

### 6.5 Spanner: TrueTime and External Consistency

Google Spanner achieves externally consistent distributed transactions using GPS/atomic clock-synchronized timestamps (TrueTime).

```
TrueTime API:
  TT.now() returns an interval [earliest, latest]
  Guarantee: actual time is within the interval
  Typical uncertainty: epsilon ≈ 1-7ms

  ┌────────────────────────────────────────────────────────────┐
  │  TrueTime: TT.now() = [t - epsilon, t + epsilon]          │
  │                                                            │
  │  Real time: ──────────────●──────────────────────          │
  │                       actual time                          │
  │                                                            │
  │  TrueTime:  ─────[earliest───●───latest]─────────          │
  │                          guaranteed to                     │
  │                          contain actual                    │
  │                                                            │
  └────────────────────────────────────────────────────────────┘

Spanner Commit Protocol:
  1. Acquire locks (2PL for read-write transactions)
  2. Choose commit timestamp s = TT.now().latest
  3. WAIT until TT.now().earliest > s  (commit-wait)
     This guarantees that s is in the past for ALL nodes.
     Wait time ≈ 2 * epsilon ≈ 2-14ms
  4. Apply changes with timestamp s
  5. Release locks

External Consistency:
  If T1 commits before T2 starts (in real time),
  then T1's commit timestamp < T2's commit timestamp.
  This is STRONGER than serializability.
```

### 6.6 CockroachDB: Hybrid-Logical Clocks

CockroachDB achieves serializable isolation in a distributed setting without specialized hardware, using Hybrid-Logical Clocks (HLCs).

```
Hybrid-Logical Clock:
  HLC = (physical_time, logical_counter)

  physical_time: wall clock (NTP-synchronized, ~100ms uncertainty)
  logical_counter: incremented when events have same physical_time

  Guarantees:
    - If event A happens-before event B, then HLC(A) < HLC(B)
    - HLC is always close to real time (bounded drift)

  ┌────────────────────────────────────────────────────────────┐
  │  CockroachDB Transaction Protocol:                         │
  │                                                            │
  │  1. Transaction starts with provisional timestamp           │
  │  2. Reads encounter values with higher timestamps?          │
  │     → Push transaction timestamp forward (timestamp restart)│
  │  3. If pushed timestamp causes read-set to change:          │
  │     → Restart the transaction (serialization failure)       │
  │  4. Writes go to a staging area (write intents)             │
  │  5. At commit: parallel consensus on each range             │
  │     (no single coordinator, uses parallel commits)          │
  │                                                            │
  │  Clock Skew Handling:                                       │
  │  - max_clock_offset (default 500ms)                        │
  │  - Transactions that span the uncertainty window may need   │
  │    to wait or restart                                       │
  │  - Nodes that drift > max_clock_offset are terminated       │
  └────────────────────────────────────────────────────────────┘
```

**Comparison of Distributed Transaction Approaches:**

| Approach | Isolation | Latency | Clock Requirement | Interactive Txns |
|----------|-----------|---------|-------------------|-----------------|
| 2PC | N/A -- atomic commit only; isolation comes from each node's local CC | 2 RTT + lock hold | None | Yes |
| 3PC | N/A (same as 2PC) | 3 RTT | None | Yes |
| Saga | None (eventual) | 1 RTT per step | None | N/A |
| Calvin | Serializable | 1 RTT (batch) | None | Limited |
| Spanner (TrueTime) | External Consistency | 1 RTT + commit-wait | GPS/Atomic | Yes |
| CockroachDB (HLC) | Serializable | 1-2 RTT | NTP | Yes |

---

## 7. Transaction Implementation Details

### 7.1 Transaction ID Assignment

```
PostgreSQL XID (32-bit):
  ┌──────────────────────────────────────────────────────────────┐
  │  XIDs are 32-bit unsigned integers, mod 2^32.                │
  │  Wrap-around occurs at ~4 billion transactions; comparison   │
  │  is circular, so only ~2.1 billion (2^31) XIDs are ever      │
  │  "in the past" -- that is the real limit on tuple age.       │
  │                                                              │
  │  "Freeze" mechanism:                                         │
  │    Old tuples are marked frozen (9.4+: infomask bits; older  │
  │    releases overwrote xmin with FrozenTransactionId = 2).    │
  │    Frozen tuples are visible to ALL transactions.            │
  │    VACUUM is responsible for freezing old tuples.            │
  │                                                              │
  │  If VACUUM falls behind → transaction ID wraparound →        │
  │    database refuses to assign new XIDs (no writes) until a   │
  │    VACUUM freezes old tuples -- to prevent data corruption!  │
  │                                                              │
  │  On-disk tuple xmin/xmax are STILL 32-bit in every release.  │
  │  FullTransactionId (32-bit epoch + 32-bit xid) exists only   │
  │  in memory/some catalogs. Wraparound is a live risk: monitor │
  │  age(datfrozenxid) and mxid_age(datminmxid) (Section 9.3).   │
  │  Emergency guards: PG14+ vacuum_failsafe_age (default 1.6B)  │
  │  makes VACUUM skip index cleanup to freeze faster.           │
  └──────────────────────────────────────────────────────────────┘

InnoDB Transaction ID (48-bit):
  Up to 281 trillion unique transaction IDs.
  At 1000 TPS, this lasts ~8,900 years. No wraparound concern.

  Transaction IDs are stored in:
    - Each row: DB_TRX_ID (6 bytes)
    - Undo log headers
    - Redo log records
```

### 7.2 Savepoints and Partial Rollback

```sql
-- Savepoints allow rolling back part of a transaction
BEGIN;

INSERT INTO orders (id, amount) VALUES (1, 100);
SAVEPOINT sp1;

INSERT INTO orders (id, amount) VALUES (2, 200);
SAVEPOINT sp2;

INSERT INTO orders (id, amount) VALUES (3, -50);
-- Oops, negative amount violates constraint
ROLLBACK TO sp2;
-- Order 3 is rolled back, but orders 1 and 2 remain

INSERT INTO orders (id, amount) VALUES (3, 50);
COMMIT;
-- Final result: orders 1, 2, 3 all committed
```

**Internal Implementation:**

```
Savepoint creates a subtransaction with its own sub-XID.
Undo log is partitioned by savepoint markers.

Undo Log for the above example:
  ┌──────────────────────────────────────────┐
  │ XID 100: INSERT orders id=1             │
  │ ─── SAVEPOINT sp1 (sub-XID 101) ───     │
  │ XID 101: INSERT orders id=2             │
  │ ─── SAVEPOINT sp2 (sub-XID 102) ───     │
  │ XID 102: INSERT orders id=3 (amount=-50)│ ← rolled back
  │ ─── ROLLBACK TO sp2 ───                 │
  │ XID 103: INSERT orders id=3 (amount=50) │
  │ ─── COMMIT ───                          │
  └──────────────────────────────────────────┘

PostgreSQL: subtransaction XIDs are tracked in pg_subtrans.
Heavy use of savepoints (e.g., in ORMs that wrap every statement
in a savepoint) can cause performance issues with many sub-XIDs.
```

### 7.3 Nested Transactions

True nested transactions (where inner transactions can independently commit or abort) are rare in production databases.

```
True Nested Transactions (theoretical):

  T_outer: BEGIN
    T_inner1: BEGIN
      UPDATE A ...
      COMMIT  ← inner commit is provisional
    T_inner2: BEGIN
      UPDATE B ...
      ABORT   ← only inner2's changes are rolled back
    T_outer: COMMIT  ← inner1's changes become permanent
                       inner2's changes remain rolled back

Most databases simulate this with savepoints:
  PostgreSQL: SAVEPOINT is the closest equivalent
  SQL Server: Supports SAVE TRANSACTION (like savepoint)
  Oracle: Savepoints only; no true nested transactions
  MySQL: Savepoints; AUTOCOMMIT complicates things
```

### 7.4 Long-Running Transactions: Problems and Solutions

Long-running transactions are one of the most common causes of production database issues.

```
Problems caused by long-running transactions:

  1. Lock Contention
     Long txn holds locks → other txns wait → connection pool exhausted
     ┌──────────────────────────────────────────┐
     │ T1 (long): holds X lock on row A         │
     │ T2: wants row A → WAITING                │
     │ T3: wants row A → WAITING                │
     │ T4: wants row A → WAITING                │
     │ ...                                      │
     │ Connection pool: 95% blocked on T1       │
     └──────────────────────────────────────────┘

  2. MVCC Bloat (PostgreSQL)
     Long txn's snapshot prevents VACUUM from cleaning dead tuples.
     Table size grows unboundedly.

  3. Undo Log Growth (InnoDB)
     History list length grows, purge thread can't keep up.
     Undo tablespace grows, read performance degrades (longer
     version chains to traverse).

  4. Replication Lag
     Long txns on replicas hold old snapshots, preventing
     replay of newer WAL records.

  5. Log Retention (engine-specific)
     SQL Server: an open txn blocks transaction-log truncation, so the
     log grows. PostgreSQL: an open txn does NOT pin WAL (only
     replication slots / wal_keep_size do), but a logical slot must keep
     WAL back to the oldest txn still running when it decodes.
```

**Solutions:**

```sql
-- 1. Set statement and transaction timeouts
SET statement_timeout = '30s';          -- PostgreSQL
SET idle_in_transaction_session_timeout = '60s';  -- PostgreSQL
SET innodb_lock_wait_timeout = 50;      -- MySQL: row-lock wait only (seconds)
SET max_execution_time = 30000;         -- MySQL: SELECT timeout (ms)

-- 2. Monitor long transactions
-- PostgreSQL:
SELECT pid, now() - xact_start AS duration, state, query
FROM pg_stat_activity
WHERE state != 'idle'
  AND xact_start < now() - interval '1 minute'
ORDER BY duration DESC;

-- MySQL:
SELECT * FROM information_schema.innodb_trx
WHERE TIME_TO_SEC(TIMEDIFF(NOW(), trx_started)) > 60;

-- 3. Break long operations into batches
-- Instead of:
DELETE FROM logs WHERE created_at < '2024-01-01';  -- might lock millions of rows

-- Do:
DO $$
DECLARE
    batch_size INT := 10000;
    deleted INT;
BEGIN
    LOOP
        DELETE FROM logs
        WHERE ctid IN (
            SELECT ctid FROM logs
            WHERE created_at < '2024-01-01'
            LIMIT batch_size
        );
        GET DIAGNOSTICS deleted = ROW_COUNT;
        EXIT WHEN deleted = 0;
        COMMIT;  -- release locks between batches (PG11+: allowed in DO/procedures
                 -- only when NOT called inside an outer transaction block)
    END LOOP;
END $$;
```

### 7.5 Connection Pooling and Transaction Management

```
Connection Pool Transaction Lifecycle:

  Application                Connection Pool           Database
  ───────────                ───────────────           ────────
  getConnection() ──────────► Assign idle conn ──────►
  BEGIN ─────────────────────────────────────────────► BEGIN
  SELECT ... ────────────────────────────────────────► SELECT ...
  UPDATE ... ────────────────────────────────────────► UPDATE ...
  COMMIT ────────────────────────────────────────────► COMMIT
  releaseConnection() ─────► Return conn to pool

  DANGER: Forgetting to COMMIT or ROLLBACK before releasing:
    Application releases connection with open transaction.
    Pool assigns it to another user.
    New user's queries run inside the OLD transaction!

  PgBouncer Transaction Pooling Mode:
    Connection is assigned for the duration of a transaction.
    Between transactions, the connection can serve different clients.
    Limitation: No session-level state (SQL-level PREPARE,
    temp tables, SET commands) persists across transactions.
    (Protocol-level prepared statements work since PgBouncer 1.21
    via max_prepared_statements.)

  ┌─────────────────────────────────────────────────────────────┐
  │  Pooling Modes:                                             │
  │                                                             │
  │  Session pooling:   1 app session = 1 DB connection         │
  │                     (safest, worst utilization)             │
  │                                                             │
  │  Transaction pooling: 1 transaction = 1 DB connection       │
  │                       (good utilization, no session state)  │
  │                                                             │
  │  Statement pooling: 1 statement = 1 DB connection           │
  │                     (best utilization, no multi-statement   │
  │                     transactions, rarely used)              │
  └─────────────────────────────────────────────────────────────┘
```

### 7.6 Advisory Locks

Application-defined locks managed by the database but not tied to any table or row.

```sql
-- PostgreSQL Advisory Locks
-- Session-level (held until session ends or explicitly released):
SELECT pg_advisory_lock(12345);      -- blocks if another session holds it
SELECT pg_advisory_unlock(12345);

-- Transaction-level (released at COMMIT/ROLLBACK):
SELECT pg_advisory_xact_lock(12345);

-- Try (non-blocking):
SELECT pg_try_advisory_lock(12345);  -- returns true/false immediately

-- Common use case: distributed cron job locking
-- Only one instance should run the daily report:
DO $$
BEGIN
    IF pg_try_advisory_lock(hashtext('daily_report')) THEN
        -- Run the report
        PERFORM generate_daily_report();
        PERFORM pg_advisory_unlock(hashtext('daily_report'));
    ELSE
        RAISE NOTICE 'Another instance is running the daily report';
    END IF;
END $$;

-- Common use case: application-level mutex
-- Prevent two users from editing the same document:
SELECT pg_advisory_lock(hashtext('doc_edit'), document_id);
-- ... edit document ...
SELECT pg_advisory_unlock(hashtext('doc_edit'), document_id);
```

---

## 8. Practical Concurrency Patterns

### 8.1 SELECT FOR UPDATE

Acquire an exclusive lock on selected rows. Other transactions trying to SELECT FOR UPDATE, UPDATE, or DELETE those rows will block until the lock is released.

```sql
-- Pattern: check-then-act with exclusive lock
BEGIN;
SELECT * FROM inventory
  WHERE product_id = 42 AND quantity >= 1
  FOR UPDATE;
-- If a row is returned, we have an exclusive lock on it.
-- No other transaction can modify it until we commit.

UPDATE inventory
  SET quantity = quantity - 1
  WHERE product_id = 42;

INSERT INTO order_items (order_id, product_id) VALUES (100, 42);
COMMIT;
```

```
Timeline with FOR UPDATE:

  T1                              T2
  ──────────────────────────────  ──────────────────────────────
  BEGIN
  SELECT * FROM inventory
    WHERE product_id = 42
    FOR UPDATE;
  → Returns {qty: 10}, lock acquired
                                  BEGIN
                                  SELECT * FROM inventory
                                    WHERE product_id = 42
                                    FOR UPDATE;
                                  → BLOCKS (waiting for T1)
  UPDATE inventory
    SET quantity = 9
    WHERE product_id = 42;
  COMMIT (lock released)
                                  → Returns {qty: 9}, lock acquired
                                  UPDATE inventory
                                    SET quantity = 8
                                    WHERE product_id = 42;
                                  COMMIT
  -- Final: qty = 8 (correct! no lost update)
```

**What T2 actually sees depends on isolation level.** At READ COMMITTED, PostgreSQL does *not* re-run T2's query from scratch; it re-fetches the newest version of the row it was waiting on and re-evaluates the `WHERE` clause against it (EvalPlanQual, Section 9.7) -- so T2 sees `qty: 9`. At REPEATABLE READ / SERIALIZABLE, T2 instead fails with `40001 could not serialize access due to concurrent update` and must retry. InnoDB locking reads always read the latest committed version, at any isolation level.

**Prefer `FOR NO KEY UPDATE` in PostgreSQL** when you will not change the primary key / unique columns (the normal read-modify-write case). `FOR UPDATE` also blocks concurrent `INSERT`s into child tables whose foreign key references this row; `FOR NO KEY UPDATE` does not. See Section 9.1.

### 8.2 SELECT FOR SHARE

Acquire a shared lock. Multiple transactions can hold shared locks on the same rows. Prevents other transactions from UPDATE or DELETE, but allows concurrent reads.

```sql
-- Pattern: ensure referenced data doesn't change during transaction
BEGIN;
SELECT * FROM users WHERE id = 42 FOR SHARE;
-- User row is locked for share. Nobody can UPDATE or DELETE it.
-- But other transactions CAN also SELECT ... FOR SHARE.

INSERT INTO orders (user_id, amount)
  VALUES (42, 99.99);
-- We know user 42 still exists and hasn't been modified.
COMMIT;
```

**FOR UPDATE vs FOR SHARE:**

| Feature | FOR UPDATE | FOR SHARE |
|---------|-----------|-----------|
| Lock type | Exclusive (X) | Shared (S) |
| Concurrent FOR UPDATE | Blocks | Blocks |
| Concurrent FOR SHARE | Blocks | Allowed |
| Concurrent plain SELECT | Allowed (MVCC) | Allowed (MVCC) |
| Concurrent UPDATE/DELETE | Blocks | Blocks |
| Use case | Read-modify-write | Protect referenced data |

This two-mode picture is the MySQL/SQL-standard view. **PostgreSQL has four row-lock modes** -- `FOR UPDATE`, `FOR NO KEY UPDATE`, `FOR SHARE`, `FOR KEY SHARE` -- and for the "make sure the parent still exists" use case above, `FOR KEY SHARE` is the right tool (it's what PostgreSQL's own foreign-key checks use): it blocks `DELETE` and key changes but still lets others `UPDATE` non-key columns of `users`. Full matrix in Section 9.1.

**Caveat for FOR SHARE as a write-skew fix:** two transactions can both take `FOR SHARE` on the same row, then both try to `UPDATE` it → each waits for the other's shared lock → deadlock. If you intend to write, lock with `FOR UPDATE` / `FOR NO KEY UPDATE` from the start (the classic "lock upgrade" deadlock; SQL Server solves it with U locks, Section 9.10).

### 8.3 SKIP LOCKED (Queue-Like Patterns)

Skip rows that are already locked by other transactions. Essential for implementing work queues in the database.

```sql
-- Pattern: database-backed job queue
-- Multiple workers process jobs concurrently without stepping on each other

-- Worker process:
BEGIN;
SELECT id, payload FROM jobs
  WHERE status = 'pending'
  ORDER BY created_at
  LIMIT 1
  FOR UPDATE SKIP LOCKED;
-- Returns the next pending job that ISN'T being processed by another worker.
-- If all pending jobs are locked, returns empty result set (not blocking).

-- Process the job...
UPDATE jobs SET status = 'processing', worker_id = 'worker-3' WHERE id = :job_id;
-- ... do work ...
UPDATE jobs SET status = 'completed' WHERE id = :job_id;
COMMIT;
```

```
SKIP LOCKED in Action:

  Job Queue:  [Job1: locked by W1] [Job2: locked by W2] [Job3: free] [Job4: free]

  Worker W3: SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1;
             Skips Job1 (locked) ──► Skips Job2 (locked) ──► Returns Job3, locks it

  Worker W4: SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1;
             Skips Job1,2,3 (locked) ──► Returns Job4, locks it

  All four workers process different jobs in parallel, zero contention.
```

**Production notes for SKIP LOCKED queues:**

- Setting `status = 'processing'` inside the *same* transaction that holds the lock is invisible to other sessions until commit -- the row lock is what actually claims the job. That's fine for short jobs.
- For long jobs, don't hold a transaction open for minutes (it pins VACUUM and a pooled connection). Use a **claim-and-lease** pattern: a short transaction that `UPDATE jobs SET status='processing', locked_until = now() + interval '5 min' WHERE id = (SELECT id ... FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *`, commit, do the work, then mark done. A reaper re-queues rows whose lease expired.
- Index the predicate: `CREATE INDEX ON jobs (created_at) WHERE status = 'pending'`. Without it, every worker scans (and skips) the same locked rows.
- SKIP LOCKED returns an intentionally *inconsistent* view of the table. Use it for queues, never for reads that must be complete.

**Why SKIP LOCKED is superior to polling with application locks:**

```
Without SKIP LOCKED (anti-pattern):
  1. Application checks Redis/memcached for a "claim" on the job
  2. Race condition between check and claim
  3. Need distributed locking (Redlock, etc.)
  4. If worker crashes, lock might not be released

With SKIP LOCKED:
  1. Single atomic SQL statement
  2. No race conditions
  3. If worker crashes, PostgreSQL automatically releases the lock
  4. No external dependencies
```

### 8.4 NOWAIT

Instead of blocking when a lock cannot be acquired immediately, raise an error. Useful for fail-fast patterns.

```sql
-- Pattern: try to lock, fail fast if someone else has it
BEGIN;
SELECT * FROM accounts WHERE id = 42 FOR UPDATE NOWAIT;
-- If the row is locked by another transaction:
-- ERROR: could not obtain lock on row in relation "accounts"
-- SQLSTATE: 55P03 (lock_not_available)

-- Application catches the error and retries or returns immediately:
-- "Account is being modified by another transaction. Try again."
```

```sql
-- Combined with timeout for more flexible control:
SET lock_timeout = '5s';  -- wait up to 5 seconds before failing
BEGIN;
SELECT * FROM accounts WHERE id = 42 FOR UPDATE;
-- Blocks for up to 5 seconds, then raises error if still locked
```

### 8.5 Retry Logic for Serialization Failures

When using SERIALIZABLE or REPEATABLE READ (in PostgreSQL), transactions may be aborted due to serialization conflicts. Applications MUST implement retry logic.

```python
import psycopg2
import time
import random

def execute_with_retry(conn_params, operation, max_retries=5):
    """
    Execute a database operation with retry logic for serialization failures.
    Uses exponential backoff with jitter.
    """
    for attempt in range(max_retries):
        conn = psycopg2.connect(**conn_params)
        try:
            conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_SERIALIZABLE
            )
            with conn.cursor() as cur:
                operation(cur)
            conn.commit()
            return  # Success
        except (psycopg2.errors.SerializationFailure,   # 40001
                psycopg2.errors.DeadlockDetected):      # 40P01
            conn.rollback()
            if attempt == max_retries - 1:
                raise  # Final attempt failed
            # Exponential backoff with jitter
            delay = min(0.1 * (2 ** attempt), 5.0)
            jitter = random.uniform(0, delay * 0.5)
            time.sleep(delay + jitter)
        except Exception:
            conn.rollback()
            raise  # Non-retriable error
        finally:
            conn.close()

# Usage:
def transfer_funds(cur):
    cur.execute("SELECT balance FROM accounts WHERE id = 1")
    balance_from = cur.fetchone()[0]
    cur.execute("SELECT balance FROM accounts WHERE id = 2")
    balance_to = cur.fetchone()[0]

    if balance_from < 100:
        raise ValueError("Insufficient funds")

    cur.execute("UPDATE accounts SET balance = balance - 100 WHERE id = 1")
    cur.execute("UPDATE accounts SET balance = balance + 100 WHERE id = 2")

execute_with_retry(conn_params, transfer_funds)
```

**Key rules for retry logic:**

```
1. ALWAYS retry the ENTIRE transaction, not just the failed statement.
   The snapshot is stale; re-reading is required.

2. Use exponential backoff with jitter to avoid thundering herd.
   Without jitter, all retries happen at the same time → conflict again.

3. Set a maximum retry count. If it keeps failing, the conflict
   pattern may require application redesign.

4. Only retry on serialization failures (SQLSTATE 40001) and
   deadlock errors (SQLSTATE 40P01). Do NOT retry on other errors.

5. PostgreSQL error codes for retriable errors:
   40001 - serialization_failure
   40P01 - deadlock_detected

6. Keep transactions SHORT to minimize conflict probability.

7. Never retry a transaction that had external side effects (sent an
   email, called a payment API). Keep side effects outside the retried
   block, or make them idempotent (Section 8.6).

8. A COMMIT that fails with a network error has UNKNOWN outcome -- it may
   have committed. Blind retry can double-apply; this is exactly what
   idempotency keys solve.
```

### 8.6 Idempotency Keys

Ensure that retrying an operation (due to network timeout, serialization failure, etc.) doesn't apply the effect twice.

```sql
-- Idempotency key table. Scope keys per user: a client-generated key
-- must not collide with (or reveal) another user's request.
CREATE TABLE idempotency_keys (
    user_id     BIGINT NOT NULL,
    key         UUID   NOT NULL,
    response    JSONB,                       -- NULL while in flight
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);

-- Pattern: idempotent payment processing
CREATE OR REPLACE FUNCTION process_payment(
    p_idempotency_key UUID,
    p_user_id BIGINT,
    p_amount DECIMAL
) RETURNS JSONB AS $$
DECLARE
    v_existing JSONB;
    v_result JSONB;
BEGIN
    -- 1. CLAIM the key first. The unique index serializes concurrent
    --    duplicates: the second INSERT blocks on the first's uncommitted
    --    index entry, then sees the conflict once the first commits.
    INSERT INTO idempotency_keys (user_id, key)
    VALUES (p_user_id, p_idempotency_key)
    ON CONFLICT DO NOTHING;

    IF NOT FOUND THEN
        -- Duplicate request: return the stored response.
        SELECT response INTO v_existing
        FROM idempotency_keys
        WHERE user_id = p_user_id AND key = p_idempotency_key;
        RETURN v_existing;
    END IF;

    -- 2. Process the payment (same transaction as the claim, so a
    --    failure rolls back both and the key can be retried).
    UPDATE accounts SET balance = balance - p_amount
    WHERE id = p_user_id AND balance >= p_amount;

    IF NOT FOUND THEN
        v_result := '{"status": "insufficient_funds"}'::jsonb;
    ELSE
        INSERT INTO transactions (user_id, amount, type)
        VALUES (p_user_id, p_amount, 'debit');
        v_result := '{"status": "success"}'::jsonb;
    END IF;

    -- 3. Store the result for future duplicate requests.
    UPDATE idempotency_keys SET response = v_result
    WHERE user_id = p_user_id AND key = p_idempotency_key;

    RETURN v_result;
END;
$$ LANGUAGE plpgsql;
```

**Why claim-first:** the naive "`SELECT` key → if missing, process → `INSERT` key" version has a check-then-act race. Two concurrent retries both see "missing", both debit, and the second one's `INSERT` hits a unique violation -- which rolls back its debit, so money is safe, but the client gets a 500 instead of the cached response. Claiming first turns the unique index into the lock. Also store a hash of the request body with the key and reject a reused key with a different payload (`422`), and expire keys after a retention window (e.g. 24h) -- they are personal-data-adjacent and should not live forever (GDPR storage limitation).

```
Idempotency Flow:

  Client ──► API Server ──► Database
  Request with idempotency_key=abc123

  First attempt:
    1. Check idempotency_keys for abc123 → not found
    2. Process payment → success
    3. Store result in idempotency_keys
    4. Return success to client
    5. Network timeout! Client doesn't receive response.

  Retry (same idempotency_key=abc123):
    1. Check idempotency_keys for abc123 → FOUND
    2. Return cached result → success
    3. Client receives response.
    Payment processed exactly once despite two requests.
```

### 8.7 Putting It All Together: Common Patterns Decision Table

| Scenario | Pattern | Why |
|----------|---------|-----|
| Decrement inventory | `UPDATE ... SET qty = qty - 1 WHERE qty > 0` | Atomic, no read-then-write race |
| Transfer money between accounts | `SELECT FOR UPDATE` both rows, then UPDATE | Prevents lost update, holds locks on both |
| Job queue / task processing | `SELECT FOR UPDATE SKIP LOCKED` | Zero-contention parallel processing |
| Check availability, then book | SERIALIZABLE isolation | Prevents write skew / phantom booking |
| Upsert (insert or update) | `INSERT ... ON CONFLICT DO UPDATE` | Atomic upsert, no race condition |
| Distributed cron (leader election) | Advisory locks | Lightweight, no table contention |
| API payment processing | Idempotency keys | Safe to retry on network failure |
| Optimistic UI with conflict detection | Version column + `WHERE version = :expected` | Application-level OCC |
| Read-heavy, tolerate slight staleness | `SET TRANSACTION READ ONLY` at READ COMMITTED | Enables read-only optimizations |
| Long analytical query on live DB | Read replica or `pg_export_snapshot()` | No impact on OLTP workload |
| Read-modify-write of a parent row (PG) | `SELECT ... FOR NO KEY UPDATE` | Doesn't block concurrent FK inserts into child tables (9.1) |
| No overlapping reservations | `EXCLUDE USING gist (...)` constraint | Race-free at any isolation level (9.9) |
| Lock several rows | One `SELECT ... WHERE id IN (...) ORDER BY id FOR UPDATE` | Consistent lock order prevents deadlocks (9.10) |
| Schema migration on a busy table | `SET lock_timeout` + retry; `CONCURRENTLY` / `NOT VALID` | Avoids the lock queue pile-up (9.5) |

### 8.8 Anti-Patterns to Avoid

```
Anti-Pattern 1: SELECT then UPDATE without locking
  ❌ SELECT balance FROM accounts WHERE id = 1;
     -- application computes new_balance = balance - amount
     UPDATE accounts SET balance = :new_balance WHERE id = 1;
  ✅ UPDATE accounts SET balance = balance - :amount WHERE id = 1;
  ✅ SELECT ... FOR UPDATE, then UPDATE

Anti-Pattern 2: Long transaction holding locks
  ❌ BEGIN;
     SELECT ... FOR UPDATE;
     -- call external API (2 seconds)
     -- process results
     UPDATE ...;
     COMMIT;
  ✅ Call external API BEFORE the transaction.
     BEGIN;
     SELECT ... FOR UPDATE;
     UPDATE ...;
     COMMIT;

Anti-Pattern 3: Using SERIALIZABLE everywhere "to be safe"
  ❌ All transactions at SERIALIZABLE
  ✅ Use the MINIMUM isolation level that prevents the specific
     anomaly you're concerned about. SERIALIZABLE has higher
     abort rates and requires retry logic everywhere.

Anti-Pattern 4: Ignoring serialization failures
  ❌ try:
         execute(sql)
     except:
         log("error")
         return error_response
  ✅ Distinguish retriable errors (40001, 40P01) from non-retriable.
     Retry with backoff for serialization failures.

Anti-Pattern 5: Not setting lock_timeout or statement_timeout
  ❌ No timeouts (default in PostgreSQL: infinite wait)
  ✅ SET lock_timeout = '10s';
     SET statement_timeout = '30s';
     SET idle_in_transaction_session_timeout = '60s';
```

## 9. Advanced Locking Internals

Sections 4-5 describe locking in textbook terms (S, X, intent locks, one lock table). Real engines diverge sharply from that model, and most production lock incidents come from the divergence: foreign keys that block updates, `ALTER TABLE` that takes the site down, gap-lock deadlocks on inserts, `SELECT` statements that write WAL. This section covers what each engine actually does.

### 9.1 PostgreSQL Row-Lock Modes: FOR KEY SHARE and FOR NO KEY UPDATE

> **Everyday analogy: an apartment building and its mail carrier.**
> The row is an apartment; its primary key is the **street address**. Child rows (orders pointing at a user) are **letters addressed to that apartment**.
>
> 1. `FOR UPDATE` = *"I'm demolishing or re-numbering the apartment."* Nobody else may do anything, not even deliver mail.
> 2. `FOR NO KEY UPDATE` = *"I'm repainting inside; the address stays the same."* Other painters wait, but mail can still be delivered.
> 3. `FOR SHARE` = *"Building inspector: nothing inside may change while I look."*
> 4. `FOR KEY SHARE` = *the mail carrier:* *"I just need this address to keep existing until I've dropped the letter."* Painting inside is fine.
>
> ```
> Remember it like this
> ├─ the key is the ADDRESS, not the furniture
> ├─ FK checks are mail carriers → FOR KEY SHARE
> ├─ normal UPDATE is repainting → FOR NO KEY UPDATE (carriers not blocked)
> └─ DELETE / change the id is demolition → FOR UPDATE (blocks carriers)
> ```

PostgreSQL has **four** row-level lock modes, not two. The extra two exist to solve one specific problem: foreign keys.

```sql
SELECT ... FOR UPDATE;          -- strongest: I will delete this row or change its key
SELECT ... FOR NO KEY UPDATE;   -- I will update non-key columns
SELECT ... FOR SHARE;           -- nobody may change this row at all
SELECT ... FOR KEY SHARE;       -- weakest: nobody may delete it or change its key
```

A **"key"** here means any column set covered by a unique index that a foreign key could reference (no partial or expression indexes) -- typically the primary key.

**Conflict matrix (X = conflicts):**

```
                        Lock already held by another transaction
Requested            │ KEY SHARE │ SHARE │ NO KEY UPDATE │ UPDATE │
─────────────────────┼───────────┼───────┼───────────────┼────────┤
FOR KEY SHARE        │           │       │               │   X    │
FOR SHARE            │           │       │       X       │   X    │
FOR NO KEY UPDATE    │           │   X   │       X       │   X    │
FOR UPDATE           │     X     │   X   │       X       │   X    │
```

**Which statements take which lock implicitly:**

| Statement | Row lock taken |
|-----------|----------------|
| `UPDATE` that does not modify any key column | `FOR NO KEY UPDATE` |
| `UPDATE` that modifies a key column | `FOR UPDATE` |
| `DELETE` | `FOR UPDATE` |
| FK check on `INSERT`/`UPDATE` of a child row (locks the parent row) | `FOR KEY SHARE` |
| `SELECT ... FOR <mode>` | That mode |

**Why this exists -- the pre-9.3 foreign key disaster:**

```
Before PostgreSQL 9.3, FK checks took FOR SHARE on the parent row.

  T1 (child insert)                   T2 (parent update)
  ──────────────────────────────────  ──────────────────────────────────
  BEGIN;
  INSERT INTO orders (user_id, ...)
    VALUES (42, ...);
  -- FK check: SELECT 1 FROM users
  --   WHERE id = 42 FOR SHARE
                                      BEGIN;
                                      UPDATE users SET last_login = now()
                                        WHERE id = 42;
                                      -- BLOCKS: UPDATE vs SHARE conflict
                                      -- Updating last_login cannot break
                                      -- the FK, yet it waits.

Since 9.3:
  FK check takes FOR KEY SHARE; the UPDATE takes FOR NO KEY UPDATE.
  They are compatible → no wait. Only DELETE or changing users.id blocks.
```

**Practical rules:**

1. **Application read-modify-write: use `FOR NO KEY UPDATE`, not `FOR UPDATE`.** Most ORMs emit `FOR UPDATE` by default. On a parent table with busy children (e.g., `accounts` ← `transactions`), `FOR UPDATE` blocks every concurrent child insert for the duration of your transaction. `FOR NO KEY UPDATE` gives you the same protection against concurrent writers of that row without blocking FK checks.
2. **Checking existence of a referenced row: use `FOR KEY SHARE`.** It guarantees the row won't be deleted or re-keyed, while allowing normal updates.
3. **MySQL/InnoDB has no equivalent.** FK checks take a shared record lock (S) on the parent row, which conflicts with any X lock on it -- including an `UPDATE` of an unrelated column. Hot parent rows + child inserts are a classic InnoDB deadlock source.

### 9.2 Where Row Locks Physically Live

> **Everyday analogy: where do you put the "occupied" sign?**
> - **PostgreSQL** writes the name of the occupant *on the door itself* (the tuple header). Unlimited doors can carry names, but writing a name means touching the door: a paint job (page write + WAL) even if you only wanted to reserve it.
> - **InnoDB** keeps a **seating chart per floor** at the front desk: one sheet (bitmap) per page lists which seats are taken. Cheap, compact.
> - **Oracle** has a small **sign-in sheet on each floor's wall** (ITL slots); if the sheet is full, newcomers wait for a slot.
> - **SQL Server** gives out a **physical key card per lock** from a central desk; when the desk runs low on cards, it hands you the whole floor instead (escalation).
>
> ```
> Remember it like this
> ├─ PG: name on the door → no lock memory, but locking = writing
> ├─ InnoDB: seating chart per page → cheap, implicit for fresh inserts
> ├─ Oracle: sign-in sheet in the block → full sheet = ITL waits
> └─ SQL Server: key cards from a desk → too many cards = escalation
> ```

The "lock table" diagram in 5.1 is a useful model, but only SQL Server stores row locks that way. The storage location explains each engine's scaling behavior:

| Engine | Where a row lock is stored | Consequences |
|--------|---------------------------|--------------|
| **PostgreSQL** | In the tuple header: `xmax` = locker's XID, plus infomask bits (`HEAP_XMAX_LOCK_ONLY`, `HEAP_XMAX_KEYSHR_LOCK`, `HEAP_XMAX_EXCL_LOCK`, `HEAP_XMAX_IS_MULTI`) | Unlimited row locks, zero lock-table memory, no escalation. But **locking a row writes the page**: dirties the buffer, emits a WAL record, can trigger a full-page image. `SELECT ... FOR UPDATE` on 1M rows writes 1M tuple headers. Impossible on a read-only standby. |
| **InnoDB** | `lock_t` structs in the `lock_sys` hash, one per (transaction, page, mode), with a **bitmap** indexed by heap number of rows on the page. Fresh inserts use **implicit locks** (no struct at all -- the row's `DB_TRX_ID` of an active transaction *is* the lock). | Thousands of row locks cost a few KB, so no escalation. Implicit locks are converted to explicit ones only when someone else asks for a conflicting lock. |
| **Oracle** | A lock byte in the row header pointing at an **ITL** (Interested Transaction List) slot in the block header, which holds the XID. | No lock memory at all. Waiters enqueue on the holder's transaction (`enq: TX - row lock contention`). A block with too few free ITL slots causes `enq: TX - allocate ITL entry` waits (tune `INITRANS`). |
| **SQL Server** | Lock manager memory (~100 bytes per lock). | Memory pressure → **lock escalation** to table level at ~5,000 locks per statement per object. Tune with `ALTER TABLE ... SET (LOCK_ESCALATION = AUTO | TABLE | DISABLE)`. |

**How a PostgreSQL row-lock wait actually works:**

Because row locks aren't in the lock table, a waiter can't queue *on the row*. Instead:

```
T1 holds a row lock (its XID is in the tuple's xmax).

T2 wants the row:
  1. Acquire a heavyweight TUPLE lock on (relation, page, offset)
     → establishes T2 as "next in line" for this row
  2. Wait on T1's TRANSACTIONID lock (every txn holds an exclusive lock
     on its own XID until it ends)
  3. When T1 ends: re-check the tuple, set xmax = T2, release tuple lock

T3 arrives while T2 is waiting:
  1. Tries the TUPLE lock → held by T2 → T3 waits on the tuple lock

What pg_locks shows:
  T2: locktype = transactionid, transactionid = T1, granted = false
  T3: locktype = tuple,        (rel, page, tuple), granted = false
```

This is why row-lock contention appears in `pg_locks` as `transactionid` waits, and why `pg_blocking_pids()` (Appendix A) is the easiest way to read it.

**Other PostgreSQL lock types worth recognizing in `pg_locks`:**

| `locktype` | What it protects | When you see it waiting |
|------------|------------------|-------------------------|
| `relation` | Table/index (8 modes, 9.4) | DDL vs DML, lock queue pile-ups |
| `transactionid` | A transaction's lifetime | Row-lock waits; unique-index insert waits on an uncommitted duplicate |
| `tuple` | Queue position for one row | Second+ waiter on a hot row |
| `virtualxid` | A transaction's lifetime (before it has an XID) | `CREATE INDEX CONCURRENTLY` waiting for *every* older transaction -- one idle-in-transaction session stalls it forever |
| `extend` | Adding pages to a relation | Many concurrent bulk inserts into one table |
| `spectoken` | Speculative insertion | `INSERT ... ON CONFLICT` racing on the same key |
| `advisory` | Application-defined | Section 7.6 |
| `object` | Non-relation catalog objects | e.g. concurrent `DROP`/`ALTER` of the same type or schema |

### 9.3 MultiXacts: When Several Transactions Lock One Row

> **Everyday analogy: a sign-up sheet that must be retyped for every new name.**
> One name fits on the door (`xmax`). When a second person wants to share the room, PostgreSQL puts up a **sign-up sheet** (MultiXact) and writes the sheet's number on the door. The rule is that sheets are never edited: each new name means **printing a fresh sheet with all old names plus the new one**. 100 people signing up = 100 sheets with 1, 2, ..., 100 names = ~5,000 names printed. The sheets are numbered with a counter that eventually wraps around, so old sheets must be archived (frozen) by VACUUM.
>
> ```
> Remember it like this
> ├─ one locker → XID on the door; many lockers → sheet number on the door
> ├─ sheets are immutable → N lockers cost O(N²) entries
> ├─ hot parent row + many FK inserts = the sheet printer never stops
> └─ sheet numbers wrap too → watch mxid_age(), not only age()
> ```

`xmax` holds one XID. What if two transactions both hold `FOR KEY SHARE` on the same parent row (two concurrent child inserts), or one holds `FOR KEY SHARE` while another does a `NO KEY UPDATE`? PostgreSQL stores a **MultiXactId** in `xmax` instead, with `HEAP_XMAX_IS_MULTI` set.

```
Tuple header:  xmax = MultiXactId 7001  (HEAP_XMAX_IS_MULTI)
                         │
                         ▼
pg_multixact/offsets:  7001 → offset 55200
pg_multixact/members:  55200: { xid 900: ForKeyShare,
                                 xid 901: ForKeyShare,
                                 xid 905: NoKeyUpdate }
```

**MultiXacts are immutable.** Adding a locker creates a *new* MultiXact containing all previous members plus the new one, and rewrites `xmax`. So N concurrent lockers of one row generate N MultiXacts with 1, 2, ..., N members -- **O(N²) member entries**.

**Where it hurts in production:**

1. **Hot parent rows.** A `tenants` or `accounts` row referenced by high-rate child inserts (`events.tenant_id → tenants.id`) gets `FOR KEY SHARE` from every insert. Symptoms: `LWLock: MultiXactOffsetSLRU` / `MultiXactMemberSLRU` (PG13+ names) wait events, the `pg_multixact/` directory growing, and the parent row's page being rewritten constantly.
2. **MultiXact wraparound.** MultiXactIds are 32-bit, and so is the members address space. They need freezing exactly like XIDs, governed by `autovacuum_multixact_freeze_max_age` (default 400M). Member-space exhaustion can force emergency anti-wraparound vacuums even when the MultiXactId count looks fine.
3. **Savepoints + row locks.** Locking a row in a subtransaction when the parent transaction already locked or updated it records both XIDs → a MultiXact from a single session (9.12).

**Monitoring and mitigation:**

```sql
-- Wraparound headroom for both counters
SELECT datname,
       age(datfrozenxid)       AS xid_age,
       mxid_age(datminmxid)    AS multixact_age
FROM pg_database ORDER BY multixact_age DESC;

-- SLRU cache health (PG13+): high blks_read = cache thrashing
-- (PG17+ names; PG13-16 used 'MultiXactOffset', 'MultiXactMember', 'Subtrans')
SELECT name, blks_hit, blks_read FROM pg_stat_slru
WHERE name IN ('multixact_offset', 'multixact_member', 'subtransaction');

-- Who holds locks on rows of a table (needs the pgrowlocks extension)
CREATE EXTENSION IF NOT EXISTS pgrowlocks;
SELECT * FROM pgrowlocks('tenants');   -- shows multi = true + member xids/modes
```

Mitigations, in order of preference: avoid FK checks against a single hot row (e.g., don't FK high-volume event tables to a tenants table, or validate asynchronously); keep transactions that insert children short; on PG17+ enlarge `multixact_offset_buffers` / `multixact_member_buffers`; make sure autovacuum keeps up on tables with high `mxid_age`.

### 9.4 PostgreSQL Table-Level Lock Modes

> **Everyday analogy: a shop's door signs.** Customers browsing (`SELECT`) only need the shop to be open. Staff restocking shelves (`INSERT/UPDATE`) can work alongside customers. Stocktaking (`CREATE INDEX`, non-concurrent) says *"browse all you like, but nobody moves stock."* A renovation (`ALTER TABLE`, `DROP`, `TRUNCATE` → ACCESS EXCLUSIVE) **closes the shop entirely**.
>
> ```
> Remember it like this
> ├─ ACCESS SHARE = browsing          → blocked only by "closed for renovation"
> ├─ ROW EXCLUSIVE = restocking       → normal writes, run together
> ├─ SHARE (CREATE INDEX) = stocktake → readers OK, writers wait
> └─ ACCESS EXCLUSIVE = renovation    → everyone out
> ```

Every statement takes a table-level ("relation") lock, including plain `SELECT`. There are eight modes; the names are historical and misleading (`ROW EXCLUSIVE` is a *table* lock).

```
Requested \ Held   │ AS  RS  RE  SUE  S   SRE  E   AE
───────────────────┼──────────────────────────────────
ACCESS SHARE  (AS) │                               X
ROW SHARE     (RS) │                           X   X
ROW EXCL.     (RE) │                  X   X    X   X
SHARE UPD EX (SUE) │             X    X   X    X   X
SHARE          (S) │         X   X        X    X   X
SHARE ROW EX (SRE) │         X   X    X   X    X   X
EXCLUSIVE      (E) │     X   X   X    X   X    X   X
ACCESS EXCL.  (AE) │ X   X   X   X    X   X    X   X
```

| Mode | Taken by | Blocks plain `SELECT`? | Blocks writes? |
|------|----------|------|------|
| ACCESS SHARE | `SELECT` | No | No |
| ROW SHARE | `SELECT ... FOR UPDATE/NO KEY UPDATE/SHARE/KEY SHARE` | No | No |
| ROW EXCLUSIVE | `INSERT`, `UPDATE`, `DELETE`, `MERGE` | No | No |
| SHARE UPDATE EXCLUSIVE | `VACUUM` (non-FULL), `ANALYZE`, `CREATE INDEX CONCURRENTLY`, `REINDEX CONCURRENTLY`, `ALTER TABLE ... VALIDATE CONSTRAINT`, `... SET STATISTICS`, `ATTACH PARTITION` (on parent) | No | No (self-conflicting: one at a time) |
| SHARE | `CREATE INDEX` (non-concurrent) | No | **Yes** |
| SHARE ROW EXCLUSIVE | `CREATE TRIGGER`, `ALTER TABLE ... ADD FOREIGN KEY` (on both tables) | No | **Yes** |
| EXCLUSIVE | `REFRESH MATERIALIZED VIEW CONCURRENTLY` | No | **Yes** |
| ACCESS EXCLUSIVE | `DROP`, `TRUNCATE`, `VACUUM FULL`, `CLUSTER`, `REINDEX` (non-concurrent), `REFRESH MATERIALIZED VIEW`, most `ALTER TABLE` (`ADD COLUMN`, `ALTER TYPE`, `SET NOT NULL`...), `LOCK TABLE` (default) | **Yes** | **Yes** |

Rule of thumb: **only ACCESS EXCLUSIVE blocks readers.** Everything from SHARE upward (SHARE, SHARE ROW EXCLUSIVE, EXCLUSIVE, ACCESS EXCLUSIVE) blocks writers.

### 9.5 The Lock Queue Pile-Up (How a 1 ms ALTER TABLE Causes an Outage)

> **Everyday analogy: a single-lane bridge with a strict "wait your turn" line.**
> A slow tractor (a 5-minute report) is crossing. A wide load (`ALTER TABLE`, needs the whole bridge) arrives and waits. Every car behind it -- even ones that could have squeezed past the tractor -- must also wait, because nobody may jump the wide load. The wide load would cross in one second, but the whole road is jammed for five minutes.
> `lock_timeout` is the wide-load driver saying *"if I can't go within 3 seconds, I'll pull over and try again later"* -- the line keeps moving.
>
> ```
> Remember it like this
> ├─ queue order is strict: new requests wait behind the QUEUED lock
> ├─ a fast DDL behind a slow query = everyone waits for the slow query
> ├─ always: SET lock_timeout + retry with jitter
> └─ prefer CONCURRENTLY / NOT VALID + VALIDATE / INSTANT variants
> ```

Lock requests on a relation are granted in **queue order**: a new request that is compatible with the *held* locks still waits if it conflicts with a lock *already queued* (Section 5.2, anti-starvation). Combine that with ACCESS EXCLUSIVE:

```
t=0   T1: long analytics SELECT on orders      → holds ACCESS SHARE (runs 5 min)
t=1   T2: ALTER TABLE orders ADD COLUMN note text;
          wants ACCESS EXCLUSIVE → conflicts with T1 → QUEUED
          (the ALTER itself would take ~1 ms -- it's metadata only)
t=2   T3..T500: ordinary SELECT/INSERT on orders
          want ACCESS SHARE / ROW EXCLUSIVE
          compatible with T1, but conflict with QUEUED AE → QUEUED
          → every query on orders now waits for T1 to finish
          → connection pool exhausted → site down
```

**The fix: never run DDL without `lock_timeout`, and retry.**

```sql
-- Migration session
SET lock_timeout = '3s';          -- give up quickly instead of blocking the queue
SET statement_timeout = '15min';  -- for the actual work, if it rewrites
ALTER TABLE orders ADD COLUMN note text;
-- On 55P03 (lock_not_available): sleep with jitter, retry N times.
```

**Zero-downtime migration toolkit (PostgreSQL):**

| Goal | Unsafe | Safe |
|------|--------|------|
| Add index | `CREATE INDEX` (SHARE: blocks writes for the whole build) | `CREATE INDEX CONCURRENTLY` (SUE). Can't run in a transaction block; on failure leaves an `INVALID` index -- drop and retry. Waits for all older transactions (`virtualxid`) twice. |
| Add FK | `ADD FOREIGN KEY` (SRE on both tables + full validation scan) | `ADD ... NOT VALID` (brief lock, no scan), then `VALIDATE CONSTRAINT` (SUE -- writes continue) |
| Add CHECK / NOT NULL | `ADD CHECK (...)` / `SET NOT NULL` (AE + full scan) | `ADD CHECK (col IS NOT NULL) NOT VALID` → `VALIDATE` → PG12+: `SET NOT NULL` skips the scan when a valid CHECK proves it |
| Add column with default | PG ≤10: rewrites table under AE | PG11+: non-volatile default is metadata-only (still takes AE briefly -- still needs `lock_timeout`) |
| Change column type | `ALTER COLUMN TYPE` (AE + full rewrite, unless binary-coercible) | New column → dual-write → backfill in batches → swap |

**Fast-path locks and the partition trap.** Weak relation locks (AS, RS, RE) don't touch the shared lock table: each backend records up to 16 of them in its own `PGPROC` fast-path slots. A query that touches more than 16 relations -- **each partition and each of its indexes counts** -- spills into the shared lock table, which is split into 16 partitions guarded by LWLocks. High-QPS queries on a partitioned table without plan-time pruning then serialize on `LWLock: LockManager`. PostgreSQL 18 sizes the fast-path array from `max_locks_per_transaction`; on older versions, keep pruning effective and index counts low. Separately, the shared lock table has room for `max_locks_per_transaction × (max_connections + max_prepared_transactions)` locks -- exceeding it gives `out of shared memory, HINT: You might need to increase max_locks_per_transaction` (common with `pg_dump` or transactions touching thousands of partitions).

**MySQL's counterpart: metadata locks (MDL).** Every statement takes a shared MDL on the tables it touches for the **whole transaction**. DDL needs an exclusive MDL → same pile-up, visible as `Waiting for table metadata lock` in `SHOW PROCESSLIST`. The default `lock_wait_timeout` is **one year**; set it to a few seconds in migration sessions. Prefer `ALGORITHM=INSTANT` (8.0+: add/drop column) or `ALGORITHM=INPLACE, LOCK=NONE`, and tools like `gh-ost` / `pt-online-schema-change` for rebuilds. Inspect with `performance_schema.metadata_locks`.

### 9.6 Where Timeouts Apply

> **Everyday analogy: kitchen timers.** `lock_timeout` = how long you'll wait in line; `statement_timeout` = how long one dish may cook; `idle_in_transaction_session_timeout` = how long you may hold a table while not ordering; `transaction_timeout` = the maximum length of the whole dinner. Without timers, one forgotten customer can hold a table all night.

| Setting | Engine | Bounds | Default |
|---------|--------|--------|---------|
| `lock_timeout` | PG | Any single lock wait (row, table, advisory) | 0 (forever) |
| `statement_timeout` | PG | One statement's total runtime | 0 |
| `idle_in_transaction_session_timeout` | PG | Time idle *inside* an open transaction | 0 |
| `transaction_timeout` | PG17+ | Whole transaction | 0 |
| `deadlock_timeout` | PG | Wait before running the deadlock detector; also the `log_lock_waits` threshold | 1s |
| `innodb_lock_wait_timeout` | MySQL | InnoDB row-lock waits only | 50s |
| `lock_wait_timeout` | MySQL | Metadata-lock waits (DDL, `LOCK TABLES`) | 31,536,000s (1 year) |
| `max_execution_time` | MySQL | `SELECT` statements only (ms) | 0 |

Set them per role so batch jobs and web requests get different budgets: `ALTER ROLE web_app SET lock_timeout = '2s';`. Turn on `log_lock_waits = on` in PostgreSQL -- it logs every wait longer than `deadlock_timeout` with the blocking PIDs, which is the cheapest lock-contention telemetry available.

### 9.7 READ COMMITTED Write Semantics: EvalPlanQual and Semi-Consistent Reads

> **Everyday analogy: re-reading the price tag at the checkout.**
> You picked a jacket because the tag said "€80, fits my €80 budget". At the till, the cashier is busy with the customer before you, who is changing the same jacket's price. When it's your turn, you **look at the tag again** (EPQ re-checks the `WHERE` on the newest version). If it now says €120, you put it back. But you never go back to the shop floor to look for *other* jackets that became cheaper in the meantime -- rows that newly match are never seen.
>
> ```
> Remember it like this
> ├─ READ COMMITTED: wait for the row, re-check WHERE on its newest version
> ├─ still matches → update it; no longer matches → skip it
> ├─ rows that START matching mid-statement are never found
> └─ REPEATABLE READ / SERIALIZABLE: no re-check, you get 40001 instead
> ```

At READ COMMITTED, what happens when an `UPDATE` finds a row that a concurrent transaction is modifying? PostgreSQL waits, then runs **EvalPlanQual (EPQ)**: it fetches the *newest committed version* of that one row and re-evaluates the `WHERE` clause against it. If it still matches, the update proceeds on the new version; if not, the row is skipped.

**When EPQ gives the right answer:**

```
accounts: id=1, balance=100

T1: UPDATE accounts SET balance = balance - 80
      WHERE id = 1 AND balance >= 80;         -- balance → 20 (uncommitted)
T2: same statement                             -- blocks on row id=1
T1: COMMIT
T2: EPQ re-checks new version: 20 >= 80? No → 0 rows updated.   ✓ no overdraft
```

This is why the atomic conditional `UPDATE` in 8.7 is safe at READ COMMITTED.

**When EPQ surprises you** (example from the PostgreSQL docs):

```
website: rows with hits = 9 and hits = 10

T1: UPDATE website SET hits = hits + 1;        -- 9→10, 10→11 (uncommitted)
T2: DELETE FROM website WHERE hits = 10;
      row (hits=9 in T2's snapshot):  doesn't match → skipped, never re-checked
      row (hits=10 in T2's snapshot): matches → waits for T1
T1: COMMIT
T2: EPQ re-checks: now 11 ≠ 10 → skipped
    → DELETE 0, although a row with hits=10 existed both before AND after T1.
```

**EPQ rules to remember:**

- Only rows found in the statement's *original* snapshot are re-checked. Rows that start matching the predicate because of a concurrent update or insert are **never seen** -- a single `UPDATE ... WHERE <predicate>` is not atomic with respect to that predicate.
- Only the locked target rows are re-fetched; rows from other joined tables keep their snapshot values.
- At REPEATABLE READ / SERIALIZABLE, PostgreSQL does not do EPQ; it raises `40001 could not serialize access due to concurrent update`.

**InnoDB's equivalent -- semi-consistent read:** at READ COMMITTED, an `UPDATE` that hits a row locked by another transaction reads the latest *committed* version to decide whether it matches the `WHERE`. If not, it skips the row without waiting; if so, it waits for the lock and re-reads. Combined with InnoDB releasing locks on non-matching rows at RC, this is why RC dramatically reduces lock waits and deadlocks in MySQL.

### 9.8 InnoDB Lock Types: Record, Gap, Next-Key, Insert Intention, AUTO-INC

> **Everyday analogy: a car park with numbered spaces.** Records are **parked cars**; the empty stretches between them are **gaps**.
> - **Record lock** = a clamp on one car.
> - **Gap lock** = cones across the empty stretch between two cars: *"nobody parks here."* Two people can put cones on the same stretch -- cones don't fight each other; they only stop *new cars*.
> - **Next-key lock** = a clamp on a car **plus** cones on the stretch in front of it.
> - **Insert intention** = a driver signalling *"I'm about to park in that stretch."* Several drivers can signal for different spots in the same stretch, but any cones there stop them.
> - The famous deadlock: two people each put cones on the same stretch, then each tries to park there. Each waits for the other's cones.
>
> ```
> Remember it like this
> ├─ InnoDB locks what it SCANS, not what it returns → index your WHERE
> ├─ gap locks only block INSERTS, never each other
> ├─ "SELECT FOR UPDATE, then INSERT if missing" = cone deadlock
> └─ READ COMMITTED removes most cones (gap locks)
> ```

InnoDB locks **index records**, not rows -- a statement locks every index record it *scans*, not just the ones it returns. A `FOR UPDATE` on an unindexed column locks every row of the table (and every gap) at REPEATABLE READ.

Example index on `id`: records `10, 20, 30`, plus the `supremum` pseudo-record after the last one. Gaps: `(-∞,10) (10,20) (20,30) (30,+∞)`.

| Lock | Covers | `data_locks.LOCK_MODE` | Taken by (at RR) |
|------|--------|------------------------|------------------|
| **Record lock** | One index record | `X,REC_NOT_GAP` / `S,REC_NOT_GAP` | Unique-index equality search that finds a row: `WHERE id = 20 FOR UPDATE` |
| **Gap lock** | The open interval before a record | `X,GAP` / `S,GAP` | Equality search that finds nothing: `WHERE id = 25 FOR UPDATE` locks gap (20,30) |
| **Next-key lock** | Record + the gap before it: `(10,20]` | `X` / `S` | Range scans and non-unique index searches: `WHERE id BETWEEN 15 AND 25 FOR UPDATE` → `(10,20]` and a gap/next-key on 30 |
| **Insert intention** | A point inside a gap | `X,GAP,INSERT_INTENTION` | Every `INSERT`, momentarily, before inserting into a gap |
| **AUTO-INC** | The table's auto-increment counter | (table lock) | `INSERT` into tables with `AUTO_INCREMENT`, depending on `innodb_autoinc_lock_mode` |

**Compatibility (for conflicting S/X modes; S vs S never conflicts):**

```
                          Held by another transaction
Requested          │ Gap │ Insert Intention │ Record │ Next-Key │
───────────────────┼─────┼──────────────────┼────────┼──────────┤
Gap                │  ✓  │        ✓         │   ✓    │    ✓     │
Insert Intention   │  ✗  │        ✓         │   ✓    │    ✗     │
Record             │  ✓  │        ✓         │   ✗    │    ✗     │
Next-Key           │  ✓  │        ✓         │   ✗    │    ✗     │
```

Two facts drive almost every InnoDB lock incident:

- **Gap locks never conflict with each other** (even X,GAP vs X,GAP). They exist only to stop *inserts*.
- **Insert intention conflicts with any gap lock**, but not with other insert intentions (two inserts of different keys into the same gap proceed in parallel).

**Classic deadlock 1: "lock-then-insert" upsert**

```
Index: 10, 20, 30.   Both sessions: "if id doesn't exist, insert it."

T1: SELECT * FROM t WHERE id = 25 FOR UPDATE;   -- empty; X,GAP on (20,30)
T2: SELECT * FROM t WHERE id = 26 FOR UPDATE;   -- empty; X,GAP on (20,30) ✓ compatible
T1: INSERT INTO t (id) VALUES (25);             -- insert intention vs T2's gap → WAIT
T2: INSERT INTO t (id) VALUES (26);             -- insert intention vs T1's gap → DEADLOCK

Fix: INSERT ... ON DUPLICATE KEY UPDATE (or INSERT first and handle the
duplicate-key error), or run at READ COMMITTED where these gap locks aren't taken.
```

**Classic deadlock 2: duplicate-key on concurrent inserts**

On a duplicate-key error, InnoDB puts a **shared** lock on the existing record (for the duplicate check). Three sessions insert the same key: S1 succeeds (holds X), S2 and S3 hit the duplicate and queue for S locks. S1 rolls back → S2 and S3 both get S → both now try to take X to insert → deadlock. Seen in practice with "insert, catch duplicate, retry" loops under high concurrency.

**READ COMMITTED turns gap locking off** for searches and index scans. Gap locks remain only for foreign-key and duplicate-key checks. Locks on rows that don't match the `WHERE` are released after evaluation. This is why many large MySQL shops run at RC (with row-based binlog).

**The InnoDB RR "phantom that isn't supposed to exist":** plain `SELECT` reads the snapshot; `UPDATE`/`DELETE`/locking reads read the *latest committed* data. Mixing them breaks the snapshot illusion:

```
T1: BEGIN;  SELECT COUNT(*) FROM t WHERE c = 1;     -- 2 (snapshot)
T2: INSERT INTO t (c) VALUES (1); COMMIT;
T1: SELECT COUNT(*) FROM t WHERE c = 1;             -- 2 (still snapshot: fine)
T1: UPDATE t SET c = 2 WHERE c = 1;                 -- "3 rows affected"  ← current read
T1: SELECT COUNT(*) FROM t WHERE c = 2;             -- 3: T2's row now visible,
                                                    --    because T1 modified it
```

At PostgreSQL REPEATABLE READ, T1's `UPDATE` uses the same snapshot as its `SELECT`, so it updates 2 rows and T2's row is untouched. Neither behavior violates the SQL standard; they are different designs, and code ported between the two can change behavior silently.

**AUTO-INC lock modes (`innodb_autoinc_lock_mode`):**

| Mode | Behavior | Trade-off |
|------|----------|-----------|
| 0 traditional | Table-level AUTO-INC lock held to **end of statement** | Consecutive IDs, serializes all inserts |
| 1 consecutive (default ≤5.7) | Lightweight mutex for simple inserts; table lock only for bulk inserts of unknown size (`INSERT ... SELECT`) | Consecutive within a statement |
| 2 interleaved (default 8.0+) | Mutex only, never a statement-long lock | Fastest; IDs in one bulk insert may interleave with others; requires row-based binlog |

In every mode, rolled-back inserts leave **gaps**: auto-increment values are not transactional. Never use them as "no gaps" invoice numbers.

**Inspecting InnoDB locks:**

```sql
SELECT engine_transaction_id AS trx, object_name, index_name,
       lock_type, lock_mode, lock_status, lock_data
FROM performance_schema.data_locks;
-- lock_data = 'supremum pseudo-record' → gap lock past the last row

SELECT * FROM sys.innodb_lock_waits\G       -- who waits for whom, with KILL hints
SHOW ENGINE INNODB STATUS\G                  -- "LATEST DETECTED DEADLOCK" section
```

### 9.9 Preventing Write Skew and Phantoms Without SERIALIZABLE

> **Everyday analogy: two doctors and the on-call roster.**
> Alice and Bob each glance at the roster ("two of us on call, I can leave"), and each crosses out *their own* line. Nobody touched the same line, so row locks never fired, and now nobody is on call.
> The fixes map one-to-one:
> 1. **Constraint** = the hospital's software refuses the change if it would leave zero doctors.
> 2. **Materialized conflict** = there is one physical **roster clipboard**; you must hold it while you check and edit.
> 3. **Advisory lock** = a "talking stick" everyone agrees to hold before editing the roster -- works only if everyone follows the rule.
> 4. **SERIALIZABLE** = a referee who watches who read what and cancels one change if the combination could not have happened one-at-a-time.
>
> ```
> Remember it like this
> ├─ write skew = different rows, shared assumption
> ├─ best: express the rule as a constraint (UNIQUE / EXCLUDE)
> ├─ else: make everyone grab the same row ("the clipboard")
> └─ or: SERIALIZABLE everywhere + retry
> ```

Row locks can't protect rows that don't exist yet (Section 2.5). Four production techniques, in rough order of preference:

**1. Let a constraint do it.** Constraints are checked by the index, which serializes concurrent inserts at every isolation level.

```sql
-- No overlapping bookings per room -- the meeting-room example from 2.5
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE bookings ADD CONSTRAINT no_overlap
  EXCLUDE USING gist (room_id WITH =, tstzrange(start_time, end_time) WITH &&);
-- The second concurrent insert waits for the first, then fails with
-- SQLSTATE 23P01 (exclusion_violation).

-- "At most one active subscription per user": partial unique index
CREATE UNIQUE INDEX one_active_sub ON subscriptions (user_id)
  WHERE status = 'active';
```

**2. Materialize the conflict.** Pick an existing row that represents the predicate and lock it, so the two transactions collide on a row.

```sql
-- On-call example: lock the shift row before counting doctors on it
BEGIN;
SELECT 1 FROM shifts WHERE id = :shift_id FOR NO KEY UPDATE;  -- serializes per shift
SELECT count(*) FROM doctors WHERE shift_id = :shift_id AND on_call;
UPDATE doctors SET on_call = false WHERE id = :me;
COMMIT;
```

**3. Advisory lock on the predicate** when there's no natural row to lock:

```sql
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('room:5:2025-03-15', 0));
-- check + insert; released automatically at COMMIT/ROLLBACK
COMMIT;
```

Every code path that writes must take the same lock -- it is a convention, not an enforced constraint.

**4. SERIALIZABLE with retries.** The most general option; no lock design needed.

| Technique | Isolation needed | Enforced by DB? | Cost |
|-----------|------------------|-----------------|------|
| Unique / exclusion constraint | Any | Yes, always | Index maintenance; only for invariants expressible as a constraint |
| Materialized conflict (`FOR NO KEY UPDATE`) | READ COMMITTED | Only if every writer does it | Serializes per locked row |
| Advisory lock | READ COMMITTED | Only if every writer does it | Hash collisions serialize unrelated work (harmless) |
| SERIALIZABLE (SSI) | SERIALIZABLE | Yes | False-positive aborts; every caller must retry |

**Making PostgreSQL SSI work well in practice:**

- **All** transactions touching the data must run at SERIALIZABLE. A READ COMMITTED writer is invisible to SSI's conflict tracking, and the guarantee silently disappears.
- SIREAD locks (`mode = 'SIReadLock'` in `pg_locks`) are taken on tuples and **index pages** for index scans, but on the **whole relation** for sequential scans. A seq scan makes any concurrent write to that table a potential conflict → false-positive aborts. SERIALIZABLE works best when queries are index-driven.
- Promotion thresholds (tuple → page → relation) are `max_pred_locks_per_transaction` (64), `max_pred_locks_per_relation` (-2 → half of that), and `max_pred_locks_per_page` (2). Raise them if you see many relation-level SIReadLocks and high abort rates.
- SIREAD locks outlive the transaction until all overlapping transactions finish, so long transactions increase aborts for everyone.
- Declare read-only work `READ ONLY` (lets PostgreSQL drop predicate locks early); use `READ ONLY DEFERRABLE` for long reports -- they never abort.
- Hot standbys can't run SERIALIZABLE (the max there is REPEATABLE READ).
- PostgreSQL chooses the abort victim so that an immediate retry won't fail on the *same* conflict -- blind retry loops do converge.

### 9.10 Lock Conversion, U Locks, and Lock Ordering

> **Everyday analogy: two people, one pen, one notebook.** Both are *reading* the notebook (shared locks). Both decide to *write* and each waits for the other to stop reading -- forever. SQL Server's **U lock** is a "next to write" badge: only one reader may hold it, so the other waits *before* reading.
> **Lock ordering** is the dining-philosophers fix: always pick up the lower-numbered chopstick first, and nobody can end up holding one while waiting for the other.

**Conversion (upgrade) deadlock:** both transactions read with a shared lock, then both try to upgrade to exclusive.

```
T1: S lock on row A      T2: S lock on row A     (compatible)
T1: wants X on A → waits for T2's S
T2: wants X on A → waits for T1's S              → DEADLOCK
```

- **SQL Server** has an **Update (U) lock** for exactly this: U is compatible with S but not with another U or X, so only one transaction at a time can be "reading with intent to write". `UPDATE` takes U while searching, then converts to X; apps can request it with `WITH (UPDLOCK)`.
- **PostgreSQL/InnoDB** have no U lock; the equivalent is to take the exclusive lock at read time (`FOR UPDATE` / `FOR NO KEY UPDATE`), not `FOR SHARE`.

**SQL Server key-range locks** (`RangeS-S`, `RangeS-U`, `RangeI-N`, `RangeX-X`) are its analog of next-key/gap locks, used only at SERIALIZABLE. Its row-versioning levels -- `READ_COMMITTED_SNAPSHOT` (RC reads a version instead of taking S locks) and `SNAPSHOT` (SI with update-conflict error 3960) -- store versions in tempdb, or in the database itself with Accelerated Database Recovery (2019+).

**Lock ordering eliminates most deadlocks.** Acquire multiple row locks in one statement, in a deterministic order:

```sql
-- Transfer between accounts :a and :b -- always lock the lower id first
SELECT id, balance FROM accounts
WHERE id IN (:a, :b)
ORDER BY id
FOR NO KEY UPDATE;
-- PostgreSQL locks rows as they come out of the sort, i.e. in id order.
```

Two separate `SELECT ... FOR UPDATE` statements in "from, to" order deadlock as soon as A→B and B→A transfers run concurrently.

### 9.11 Hot Rows: When One Row Is the Bottleneck

> **Everyday analogy: a single cash register.** However big the shop, if every customer must pay at register #1, throughput is one customer per payment time. Speed up each payment (shorter lock hold), open more registers (sharded counters), give each customer a receipt and total up later (append-only ledger), or pre-bag the goods so people grab a bag without queueing (pre-sliced inventory + SKIP LOCKED).

Row locks are held until commit, so a single hot row caps throughput at roughly `1 / (lock hold time)`. At 2 ms per transaction that's ~500 updates/s on that row, no matter how big the server is.

| Technique | How | When |
|-----------|-----|------|
| Shrink the hold time | One statement: `UPDATE ... SET n = n - 1 WHERE id = :id AND n > 0 RETURNING n`; no app round-trips while holding the lock; commit immediately | Always first |
| Sharded counter | N rows per counter (`counter_id, slot`); increment a random slot; `SUM` on read | Likes, view counts, rate limits |
| Append-only ledger | Insert a `ledger_entry` row per change; derive balance by `SUM` + periodic snapshot row | Balances, wallets (also gives an audit trail) |
| Pre-sliced inventory | One row per unit (or per batch of units); claim with `FOR UPDATE SKIP LOCKED LIMIT 1` | Flash sales, ticketing, seat maps |
| Single-writer queue | Funnel updates for the hot key through one worker that batches them | Extreme contention on one key |

### 9.12 Subtransactions: The Hidden Scalability Cliff (PostgreSQL)

> **Everyday analogy: a pocket notebook with 64 lines.** Every savepoint writes a line in your pocket notebook, and anyone checking "is this row visible?" glances at it -- instant. On line 65 the notebook is full, so you start filing entries in the **archive room downstairs** (`pg_subtrans`). Now *everyone in the building*, for every visibility check that might involve you, has to walk to the archive room and queue at its one door. One transaction's habit slows the whole building -- replicas worst of all.
>
> ```
> Remember it like this
> ├─ SAVEPOINT / EXCEPTION block / ORM nested atomic = one notebook line
> ├─ > 64 lines in one transaction = "suboverflowed"
> ├─ overflow makes EVERY session read pg_subtrans → SubtransSLRU waits
> └─ fix: no EXCEPTION blocks in write loops, no per-statement savepoints
> ```

Subtransactions come from `SAVEPOINT`, **every PL/pgSQL `BEGIN ... EXCEPTION` block**, and many drivers/ORMs (pgjdbc `autosave`, Django nested `atomic()`, Rails `requires_new`). Each one that writes gets its own XID.

```
Each backend's PGPROC caches up to 64 subtransaction XIDs.

  ≤ 64 subxacts:  snapshots list them directly → visibility check is cheap
  > 64 subxacts:  backend marked "suboverflowed"
                  → EVERY snapshot taken anywhere while this txn is open
                    must consult pg_subtrans (an SLRU) to map subxid → parent
                  → cluster-wide contention on LWLock: SubtransSLRU
                    (PG13+ name; formerly SubtransControlLock)
                  → replicas are hit hardest
```

Also costly: a row locked or updated in a subtransaction after the parent locked it creates a MultiXact (9.3).

**Guidance:**

- Don't put `EXCEPTION` blocks inside loops that write; validate first, or handle errors outside the loop.
- Avoid driver modes that wrap every statement in a savepoint in high-throughput paths.
- Diagnose with `SELECT * FROM pg_stat_get_backend_subxact(<backend_id>)` (PG16+: `subxact_count`, `subxact_overflowed`) and `pg_stat_slru` (`name = 'subtransaction'` on PG17+, `'Subtrans'` before). PG17 adds `subtransaction_buffers` to enlarge the cache.

---

## Appendix A: Quick Reference

### PostgreSQL Transaction Configuration

```sql
-- Show current isolation level
SHOW transaction_isolation;

-- Set for current transaction
SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;
BEGIN ISOLATION LEVEL REPEATABLE READ;

-- Set default for session
SET default_transaction_isolation = 'read committed';

-- Read-only transaction (enables optimizations)
SET TRANSACTION READ ONLY;
BEGIN READ ONLY;

-- Deferrable (SERIALIZABLE READ ONLY DEFERRABLE):
-- Waits until a safe snapshot is available, then runs without
-- risk of serialization failure. Ideal for long analytical queries.
BEGIN ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE;
```

### MySQL/InnoDB Transaction Configuration

```sql
-- Show current isolation level
SELECT @@transaction_isolation;

-- Set for next transaction
SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;

-- Set for session
SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED;

-- Set globally
SET GLOBAL TRANSACTION ISOLATION LEVEL READ COMMITTED;

-- InnoDB lock wait timeout (default: 50 seconds)
SET innodb_lock_wait_timeout = 10;

-- Deadlock detection (default: ON; global-only variable)
SET GLOBAL innodb_deadlock_detect = ON;
-- Log every deadlock to the error log, not just the latest one
SET GLOBAL innodb_print_all_deadlocks = ON;

-- View current locks (MySQL 8.0+)
SELECT * FROM performance_schema.data_locks;
SELECT * FROM performance_schema.data_lock_waits;
```

### Monitoring Queries

```sql
-- PostgreSQL 9.6+: who blocks whom (simplest, handles all lock types
-- including row locks that surface as transactionid waits)
SELECT pid,
       pg_blocking_pids(pid)      AS blocked_by,
       wait_event_type, wait_event,
       now() - query_start        AS waiting_for,
       left(query, 80)            AS query
FROM pg_stat_activity
WHERE cardinality(pg_blocking_pids(pid)) > 0
ORDER BY waiting_for DESC;

-- PostgreSQL: the same via a raw pg_locks self-join (pre-9.6 style)
SELECT
    blocked.pid AS blocked_pid,
    blocked.query AS blocked_query,
    blocking.pid AS blocking_pid,
    blocking.query AS blocking_query,
    now() - blocked.query_start AS blocked_duration
FROM pg_stat_activity blocked
JOIN pg_locks bl ON bl.pid = blocked.pid AND NOT bl.granted
JOIN pg_locks gl ON gl.locktype = bl.locktype
    AND gl.database IS NOT DISTINCT FROM bl.database
    AND gl.relation IS NOT DISTINCT FROM bl.relation
    AND gl.page IS NOT DISTINCT FROM bl.page
    AND gl.tuple IS NOT DISTINCT FROM bl.tuple
    AND gl.virtualxid IS NOT DISTINCT FROM bl.virtualxid
    AND gl.transactionid IS NOT DISTINCT FROM bl.transactionid
    AND gl.classid IS NOT DISTINCT FROM bl.classid
    AND gl.objid IS NOT DISTINCT FROM bl.objid
    AND gl.objsubid IS NOT DISTINCT FROM bl.objsubid
    AND gl.pid != bl.pid
    AND gl.granted
JOIN pg_stat_activity blocking ON blocking.pid = gl.pid
ORDER BY blocked_duration DESC;

-- PostgreSQL: table bloat estimate
SELECT
    schemaname,
    relname,
    pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
    n_dead_tup,
    n_live_tup,
    ROUND(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 1) AS dead_pct,
    last_autovacuum
FROM pg_stat_user_tables
WHERE n_dead_tup > 1000
ORDER BY n_dead_tup DESC;
```

---

## Appendix B: Further Reading

| Resource | What It Covers |
|----------|---------------|
| *Designing Data-Intensive Applications* (Kleppmann) | Chapter 7: Transactions -- best practical overview |
| *Transaction Processing* (Gray & Reuter) | The definitive academic reference on transactions |
| *A Critique of ANSI SQL Isolation Levels* (Berenson et al., 1995) | Formalizes snapshot isolation and its anomalies |
| *Serializable Snapshot Isolation in PostgreSQL* (Ports & Grittner, 2012) | How PostgreSQL implemented SSI |
| *Calvin: Fast Distributed Transactions for Partitioned Database Systems* (2012) | Deterministic transaction protocol |
| *Spanner: Google's Globally-Distributed Database* (Corbett et al., 2012) | TrueTime and external consistency |
| *An Empirical Evaluation of In-Memory MVCC* (Wu et al., 2017) | Performance comparison of MVCC variants |
| PostgreSQL documentation: Chapter 13 (Concurrency Control) | Authoritative reference for PostgreSQL specifics |
| MySQL documentation: InnoDB Locking and Transaction Model | Authoritative reference for InnoDB specifics |
| PostgreSQL source: `src/backend/access/heap/README.tuplock` | Row-lock modes, MultiXacts, and why FOR KEY SHARE exists |
| PostgreSQL source: `src/backend/storage/lmgr/README` | Heavyweight lock manager, fast-path locks, deadlock detector |
| *A Read-Only Transaction Anomaly Under Snapshot Isolation* (Fekete, O'Neil, O'Neil, 2004) | Why read-only transactions are not automatically safe under SI |
| *Weak Consistency: A Generalized Theory...* (Adya, 1999) | G0/G1/G2 phenomena -- the precise definitions behind isolation levels |
| Jepsen analyses (jepsen.io) | Empirical isolation bugs in real databases |
