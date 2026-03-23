# Database Transactions & Concurrency Control: A Deep Dive

A comprehensive, staff-engineer-level reference covering ACID internals, every concurrency anomaly, isolation levels across major databases, locking protocols, MVCC implementations, distributed transactions, and practical concurrency patterns used in production systems.

---

## Table of Contents

1. [ACID Deep Dive](#1-acid-deep-dive)
2. [Concurrency Anomalies (All of Them)](#2-concurrency-anomalies-all-of-them)
3. [Isolation Levels](#3-isolation-levels)
4. [Concurrency Control Mechanisms](#4-concurrency-control-mechanisms)
5. [Lock Manager Implementation](#5-lock-manager-implementation)
6. [Distributed Transactions](#6-distributed-transactions)
7. [Transaction Implementation Details](#7-transaction-implementation-details)
8. [Practical Concurrency Patterns](#8-practical-concurrency-patterns)

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
| Rollback | Mark old tuple as live, new tuple as dead | Restore from undo log segment |
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

-- UNIQUE constraint checked at statement end (or deferred to commit)
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
│    Violation → statement fails, transaction can continue.        │
│                                                                  │
│  DEFERRED:                                                       │
│    Checked once, at COMMIT time.                                 │
│    Violation → entire transaction aborts.                        │
│    Useful for circular references or bulk loads.                 │
│                                                                  │
│  Example:                                                        │
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
                                 up to wal_writer_delay ms of data loss)
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

**Prevented by:** SERIALIZABLE (and REPEATABLE READ in InnoDB, which uses gap locks).

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

\* InnoDB's REPEATABLE READ uses gap locks, which also prevent phantoms in most cases.
\** Depends on the database implementation: PostgreSQL's REPEATABLE READ (SI) detects lost updates; InnoDB prevents them via row locks.

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

Each SQL statement acquires a new snapshot at the start of the statement. The snapshot contains the list of all transaction IDs (XIDs) that are committed at that point. Rows with `xmin` (creating transaction) in the committed set are visible; rows with `xmax` (deleting transaction) in the committed set are invisible.

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
| MySQL/InnoDB | Gap locks + MVCC | Prevented (gap locks) | Prevented (by locks) |
| SQL Server | Lock-based (by default) | ALLOWED | Prevented (by locks) |

In PostgreSQL, REPEATABLE READ is really Snapshot Isolation. It does not use locks for reads, so it cannot prevent write skew. In InnoDB, REPEATABLE READ uses next-key locks (row lock + gap lock), which prevent both phantoms and some forms of write skew but can cause more lock contention and deadlocks.

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

**SI vs SERIALIZABLE:**

SI prevents dirty reads, non-repeatable reads, phantoms, lost updates, and read skew. It does **not** prevent write skew or all serialization anomalies. This is why some databases (PostgreSQL) offer SSI as a level above SI.

**Databases and their actual isolation implementations:**

| Database | "REPEATABLE READ" is actually | "SERIALIZABLE" is actually |
|----------|------------------------------|---------------------------|
| PostgreSQL | Snapshot Isolation | SSI (Snapshot + conflict detection) |
| MySQL/InnoDB | REPEATABLE READ with gap locks | Lock-based serializability |
| Oracle | N/A (only RC and SERIALIZABLE) | Snapshot Isolation (!) |
| SQL Server | Lock-based RR (or SI if enabled) | Lock-based (or SI + conflict) |
| CockroachDB | N/A | SSI |

**Important:** Oracle's "SERIALIZABLE" is actually Snapshot Isolation and does NOT prevent write skew. This is a well-known discrepancy that has been documented in academic papers.

### 3.6 Serializable Snapshot Isolation (SSI)

SSI adds write-skew detection on top of Snapshot Isolation. It was first described by Cahill, Ronsher, and Fekete (2008) and implemented in PostgreSQL 9.1.

**How SSI works:**

1. Run using Snapshot Isolation (no read locks, readers never block writers).
2. Track rw-antidependencies: when T1 reads a row that T2 later writes (or vice versa).
3. At commit time, check for "dangerous structures" -- cycles of two consecutive rw-antidependencies.
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
  │    T1 → T2 → T3 and T3 committed before T1       │
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
| SQL Server | READ COMMITTED | Follows the SQL standard's recommendation. Offers SNAPSHOT as an opt-in alternative that doesn't block reads. |
| CockroachDB | SERIALIZABLE | Only offers SERIALIZABLE. Designed for correctness-first in distributed systems. The cost of debugging anomalies in distributed systems is too high. |

### 3.8 Complete Anomaly Prevention Matrix

```
┌──────────────────┬───────────┬────────────┬──────────┬──────────┬──────────┬──────────┬──────────┐
│ Isolation Level  │  Dirty    │ Non-Repeat │ Phantom  │  Lost    │  Write   │  Read    │ Serial.  │
│                  │  Read     │ Read       │ Read     │  Update  │  Skew    │  Skew    │ Anomaly  │
├──────────────────┼───────────┼────────────┼──────────┼──────────┼──────────┼──────────┼──────────┤
│ READ UNCOMMITTED │ Possible  │ Possible   │ Possible │ Possible │ Possible │ Possible │ Possible │
│ READ COMMITTED   │ Prevented │ Possible   │ Possible │ Possible │ Possible │ Possible │ Possible │
│ REPEATABLE READ  │ Prevented │ Prevented  │ Possible*│ Prev.**  │ Possible │ Prevented│ Possible │
│ SNAPSHOT ISOL.   │ Prevented │ Prevented  │ Prevented│ Prevented│ Possible │ Prevented│ Possible │
│ SERIALIZABLE     │ Prevented │ Prevented  │ Prevented│ Prevented│ Prevented│ Prevented│ Prevented│
└──────────────────┴───────────┴────────────┴──────────┴──────────┴──────────┴──────────┴──────────┘

* InnoDB's REPEATABLE READ prevents phantoms via gap locks
** InnoDB prevents lost updates via row locks; PostgreSQL's SI detects them at commit
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
│  │ S │ │ X │ │ S │     LOCK              │-S │ │-X │ │-S │             │
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
  PostgreSQL does NOT escalate; it has no page-level locks for data.
  InnoDB does NOT escalate; row locks are stored in the index structure.
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

  PostgreSQL: Checks for deadlocks every deadlock_timeout (default 1s).
  InnoDB: Checks on every lock wait (immediate detection).
  Oracle: Uses a background process that periodically checks.
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
│  xmax    │ Transaction ID that deleted/updated this tuple    │
│          │ (0 if tuple is still live)                        │
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

    return True
```

**pg_xact (formerly clog):**

PostgreSQL maintains a commit log (`pg_xact`) -- a bitmap where each transaction ID maps to a 2-bit status: `IN_PROGRESS`, `COMMITTED`, `ABORTED`, `SUB_COMMITTED`. This is consulted during visibility checks.

```
pg_xact structure:
┌───────────────────────────────────────────────────────┐
│  TxID  │  Status bits                                 │
│  100   │  COMMITTED  (11)                             │
│  101   │  COMMITTED  (11)                             │
│  102   │  ABORTED    (10)                             │
│  103   │  IN_PROGRESS(00)                             │
│  104   │  COMMITTED  (11)                             │
│  ...                                                  │
└───────────────────────────────────────────────────────┘
  Stored in 8KB pages under pg_xact/ directory.
  Cached in shared memory for fast lookups.
```

**Visibility Map:**

```
Visibility Map (VM): one bit per heap page
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
  4. Optionally freeze old tuples (set xmin to FrozenTransactionId)
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
│  m_up_limit_id:   100   (oldest active TRX_ID)│
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

**Real-world usage:** Google's Percolator (used in Google Spanner) and TiDB use OCC-style concurrency control for distributed transactions.

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
│ WAL interaction: NOT logged │ WAL interaction: logged           │
│ (latches are never recovered│ (locks are re-acquired during    │
│ after crash)                │ crash recovery)                  │
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

**FaunaDB (now Fauna) and deterministic databases are inspired by Calvin.**

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
| 2PC | Serializable | 2 RTT + lock hold | None | Yes |
| 3PC | Serializable | 3 RTT | None | Yes |
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
  │  Wrap-around occurs at ~4 billion transactions.              │
  │                                                              │
  │  "Freeze" mechanism:                                         │
  │    Old XIDs are replaced with FrozenTransactionId (2)        │
  │    Frozen tuples are visible to ALL transactions.            │
  │    VACUUM is responsible for freezing old tuples.            │
  │                                                              │
  │  If VACUUM falls behind → transaction ID wraparound →        │
  │    database shuts down to prevent data corruption!           │
  │                                                              │
  │  PostgreSQL 14+: 64-bit XIDs (no more wraparound panic)     │
  │  But internal storage still uses 32-bit epoch + 32-bit xid  │
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

  5. Checkpoint Pressure
     Long txns keep WAL segments pinned, preventing recycling.
     WAL disk usage grows.
```

**Solutions:**

```sql
-- 1. Set statement and transaction timeouts
SET statement_timeout = '30s';          -- PostgreSQL
SET idle_in_transaction_session_timeout = '60s';  -- PostgreSQL
SET innodb_lock_wait_timeout = 50;      -- MySQL (seconds)

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
        COMMIT;  -- release locks between batches (requires procedure in PG14+)
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
    Limitation: No session-level state (prepared statements,
    temp tables, SET commands) persists across transactions.

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
        except psycopg2.errors.SerializationFailure:
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
```

### 8.6 Idempotency Keys

Ensure that retrying an operation (due to network timeout, serialization failure, etc.) doesn't apply the effect twice.

```sql
-- Idempotency key table
CREATE TABLE idempotency_keys (
    key         UUID PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    response    JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_idempotency_user UNIQUE (key, user_id)
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
    -- Check if this request was already processed
    SELECT response INTO v_existing
    FROM idempotency_keys
    WHERE key = p_idempotency_key AND user_id = p_user_id;

    IF v_existing IS NOT NULL THEN
        -- Already processed, return cached response
        RETURN v_existing;
    END IF;

    -- Process the payment
    UPDATE accounts SET balance = balance - p_amount
    WHERE id = p_user_id AND balance >= p_amount;

    IF NOT FOUND THEN
        v_result := '{"status": "insufficient_funds"}'::jsonb;
    ELSE
        INSERT INTO transactions (user_id, amount, type)
        VALUES (p_user_id, p_amount, 'debit');
        v_result := '{"status": "success"}'::jsonb;
    END IF;

    -- Store the result for future duplicate requests
    INSERT INTO idempotency_keys (key, user_id, response)
    VALUES (p_idempotency_key, p_user_id, v_result);

    RETURN v_result;
END;
$$ LANGUAGE plpgsql;
```

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

-- Deadlock detection (default: ON)
SET innodb_deadlock_detect = ON;

-- View current locks (MySQL 8.0+)
SELECT * FROM performance_schema.data_locks;
SELECT * FROM performance_schema.data_lock_waits;
```

### Monitoring Queries

```sql
-- PostgreSQL: find blocked queries
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
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname || '.' || tablename)) AS total_size,
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
