# Concurrency Control Theory, Scheduling & Database Threading Models

A staff-engineer-level deep dive into the theoretical foundations and practical implementations of concurrency control in database systems. Covers formal serializability theory (conflict and view serializability, precedence graphs), scheduling mechanisms, deep MVCC implementation internals (version storage strategies, garbage collection, visibility algorithms), pessimistic vs optimistic locking protocols with deadlock prevention (Wound-Wait, Wait-Die), lock-free database architectures beyond data structures, database threading models (single-threaded, thread-per-connection, thread-pool, coroutine-based), and async I/O semantics (io_uring, epoll, kqueue) in production databases.

**Relationship to other topics:** Topic 05 covers ACID, isolation levels, and practical concurrency patterns. Topic 17 covers low-level latch/lock mechanics and hardware primitives. This document covers the *theory*, *scheduling*, *MVCC internals*, *threading architectures*, and *async I/O* that those topics do not address.

---

## Table of Contents

1. [Formal Serializability Theory](#1-formal-serializability-theory)
2. [Schedule Classification and Analysis](#2-schedule-classification-and-analysis)
3. [Concurrency Control Protocols: Deep Theory](#3-concurrency-control-protocols-deep-theory)
4. [MVCC Implementation Internals](#4-mvcc-implementation-internals)
5. [Pessimistic vs Optimistic Locking: Complete Analysis](#5-pessimistic-vs-optimistic-locking-complete-analysis)
6. [Deadlock Handling: Prevention, Detection, Resolution](#6-deadlock-handling-prevention-detection-resolution)
7. [Lock-Free Database Architectures](#7-lock-free-database-architectures)
8. [Database Threading Models](#8-database-threading-models)
9. [Async I/O Semantics in Databases](#9-async-io-semantics-in-databases)
10. [Deterministic Databases](#10-deterministic-databases)
11. [Production Concurrency Control Comparison](#11-production-concurrency-control-comparison)

---

## 1. Formal Serializability Theory

Serializability is the gold standard of correctness for concurrent transaction execution. A concurrent execution is correct if and only if its outcome is equivalent to *some* serial execution of the same transactions. This section formalizes that intuition.

### 1.1 Transactions as Operations on Data Items

A transaction T_i is a sequence of read and write operations on data items, ending with either commit or abort:

```
T_i = { r_i(X), w_i(Y), r_i(Z), ..., c_i | a_i }

Where:
  r_i(X)  = Transaction i reads data item X
  w_i(X)  = Transaction i writes data item X
  c_i     = Transaction i commits
  a_i     = Transaction i aborts
```

**Data items** can be at any granularity: a row, a page, a table, or even a predicate range. The theory works regardless of granularity.

### 1.2 Schedules

A **schedule** S is an ordering of the operations of a set of transactions that preserves each transaction's internal ordering.

```
Given T1 = { r1(A), w1(A), c1 }
      T2 = { r2(A), w2(A), c2 }

Serial schedule (T1 then T2):
  S1: r1(A) w1(A) c1 r2(A) w2(A) c2

Serial schedule (T2 then T1):
  S2: r2(A) w2(A) c2 r1(A) w1(A) c1

Interleaved (concurrent) schedule:
  S3: r1(A) r2(A) w1(A) w2(A) c1 c2
       │     │      │      │
       └─────┴──────┴──────┘
       Operations from T1 and T2 are interleaved
       but each transaction's internal order is preserved:
       r1 before w1, r2 before w2
```

### 1.3 Conflicting Operations

Two operations **conflict** if and only if ALL three conditions hold:

1. They belong to **different transactions**
2. They access the **same data item**
3. At least one of them is a **write**

```
Conflict types (for operations on data item X):

  ┌─────────────┬──────────┬──────────┐
  │             │  r_j(X)  │  w_j(X)  │
  ├─────────────┼──────────┼──────────┤
  │  r_i(X)     │  NO      │  YES     │  Read-Write conflict (RW)
  │             │ conflict │ conflict │
  ├─────────────┼──────────┼──────────┤
  │  w_i(X)     │  YES     │  YES     │  Write-Read (WR) and
  │             │ conflict │ conflict │  Write-Write (WW)
  └─────────────┴──────────┴──────────┘

  Non-conflicting: r_i(X) and r_j(X) — two reads never conflict
  Non-conflicting: w_i(X) and r_j(Y) — different data items never conflict
```

**Why conflicts matter:** Non-conflicting operations can be swapped in order without changing the final result. Conflicting operations *cannot* be reordered — doing so changes what values are read or what the final state is.

### 1.4 Conflict Equivalence

Two schedules S and S' are **conflict equivalent** if:
1. They involve the same set of transactions
2. They have the same set of operations
3. Every pair of conflicting operations is ordered the same way in both schedules

```
Example:
  S3: r1(A) r2(A) w1(A) c1 w2(A) c2
  S1: r1(A) w1(A) c1 r2(A) w2(A) c2  (serial: T1 then T2)

  Conflicting pairs in S3:
    (r1(A), w2(A)): r1 before w2  ✓ same in S1
    (r2(A), w1(A)): r2 before w1  ✗ DIFFERENT in S1 (w1 before r2)
    (w1(A), w2(A)): w1 before w2  ✓ same in S1

  S3 is NOT conflict equivalent to S1 (the r2/w1 pair differs).

  Check S2 (serial: T2 then T1):
    (r1(A), w2(A)): r1 before w2 in S3, but w2 before r1 in S2  ✗

  S3 is not conflict equivalent to any serial schedule
  → S3 is NOT conflict serializable
```

### 1.5 Conflict Serializability

A schedule S is **conflict serializable** if it is conflict equivalent to some serial schedule.

**Testing Conflict Serializability: The Precedence Graph (Serialization Graph)**

Build a directed graph where:
- Each transaction T_i is a node
- Add edge T_i → T_j if there exists a conflicting pair where T_i's operation comes before T_j's operation in the schedule

```
Algorithm: Build Precedence Graph

  For schedule S with transactions T1, T2, ..., Tn:
  1. Create node for each Ti
  2. For each pair of conflicting operations (op_i, op_j) where i ≠ j:
     If op_i appears before op_j in S:
       Add edge Ti → Tj (if not already present)
  3. Check for cycles in the graph

  THEOREM: S is conflict serializable ⟺ the precedence graph is acyclic (DAG)

  If acyclic: any topological sort gives an equivalent serial order
```

**Worked example:**

```
Schedule S: r1(A) r2(B) w1(B) r3(A) w2(A) w3(B) c1 c2 c3

Step 1: Identify all conflicting pairs

  Data item A:
    r1(A) vs w2(A): r1 before w2  → edge T1 → T2
    r3(A) vs w2(A): r3 before w2  → edge T3 → T2
    (r1(A) and r3(A) don't conflict — both reads)

  Data item B:
    r2(B) vs w1(B): r2 before w1  → edge T2 → T1
    r2(B) vs w3(B): r2 before w3  → edge T2 → T3
    w1(B) vs w3(B): w1 before w3  → edge T1 → T3

Step 2: Precedence graph:

    T1 → T2  (from r1(A) before w2(A))
    T2 → T1  (from r2(B) before w1(B))
    T2 → T3  (from r2(B) before w3(B))
    T1 → T3  (from w1(B) before w3(B))
    T3 → T2  (from r3(A) before w2(A))

    Edges: T1↔T2 (bidirectional = CYCLE), T2↔T3 (bidirectional = CYCLE)

Step 3: Cycle detected: T1 → T2 → T1  and  T2 → T3 → T2

  RESULT: Schedule S is NOT conflict serializable
```

### 1.6 View Serializability

View serializability is a **weaker** (more permissive) condition than conflict serializability. More schedules are view serializable than conflict serializable.

Two schedules S and S' are **view equivalent** if:

1. **Initial reads:** If T_i reads the initial value of data item X in S, then T_i also reads the initial value of X in S'
2. **Updated reads:** If T_i reads a value of X written by T_j in S, then T_i also reads the value written by T_j in S'
3. **Final writes:** If T_i performs the final write on X in S, then T_i also performs the final write on X in S'

```
View Serializability vs Conflict Serializability:

  ┌─────────────────────────────────────────────────────────────┐
  │                    ALL SCHEDULES                             │
  │  ┌──────────────────────────────────────────────────────┐   │
  │  │              VIEW SERIALIZABLE                        │   │
  │  │  ┌───────────────────────────────────────────────┐   │   │
  │  │  │          CONFLICT SERIALIZABLE                 │   │   │
  │  │  │  ┌────────────────────────────────────────┐   │   │   │
  │  │  │  │           SERIAL SCHEDULES              │   │   │   │
  │  │  │  └────────────────────────────────────────┘   │   │   │
  │  │  └───────────────────────────────────────────────┘   │   │
  │  │                                                       │   │
  │  │  The gap between view and conflict serializable       │   │
  │  │  contains "blind write" schedules where the extra     │   │
  │  │  flexibility rarely matters in practice.              │   │
  │  └──────────────────────────────────────────────────────┘   │
  │                                                              │
  │  Non-serializable schedules live outside both circles.      │
  └─────────────────────────────────────────────────────────────┘

  Key difference: Testing conflict serializability is polynomial (cycle detection).
  Testing view serializability is NP-complete. This is why databases use
  conflict serializability as the practical standard.
```

**Blind writes — the gap between view and conflict serializability:**

```
The gap exists for schedules involving "blind writes" — writes that occur
without a preceding read. Example:

  S: r1(A) w2(A) w1(A) w3(A) c1 c2 c3

  Precedence graph:
    r1(A) before w2(A): T1 → T2
    w2(A) before w1(A): T2 → T1  ← CYCLE!
  So S is NOT conflict serializable.

  But consider serial schedule T2, T1, T3:
    - Initial reads: T1 reads A initially in S. In serial(T2,T1,T3),
      T1 reads A after T2 writes it — DIFFERENT. Not view equivalent.

  In practice, the gap is rarely exploited because:
    1. Testing for it is NP-complete
    2. The extra schedules it permits involve blind writes
    3. No practical database implements view serializability checking
```

### 1.7 Why Databases Use Conflict Serializability

```
┌─────────────────────────────────────────────────────────────────┐
│  PRACTICAL DECISION MATRIX                                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Criterion           │ Conflict Serial. │ View Serial.          │
│  ────────────────────┼──────────────────┼──────────────────     │
│  Test complexity      │ O(V + E) — DAG   │ NP-complete           │
│  Implementation       │ 2PL, TO, SSI     │ No practical algo     │
│  Permissiveness       │ Slightly less    │ Slightly more         │
│  Production databases │ ALL of them      │ None                  │
│  Covers real workloads│ 99.9%+           │ Extra 0.1% not useful │
│                                                                  │
│  VERDICT: Conflict serializability wins on every practical      │
│  criterion. The tiny set of extra schedules permitted by view   │
│  serializability is not worth the NP-complete testing cost.     │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. Schedule Classification and Analysis

Beyond serializability, schedule theory classifies schedules by their recoverability properties. A schedule that is serializable but not recoverable can still lead to data corruption after a crash.

### 2.1 Recoverable Schedules

A schedule is **recoverable** if, whenever T_i reads a value written by T_j, T_j commits before T_i commits.

```
RECOVERABLE:
  w1(A) r2(A) c1 c2
  └──────────┘
  T2 reads T1's write, and T1 commits (c1) before T2 commits (c2). ✓

NOT RECOVERABLE (irrecoverable):
  w1(A) r2(A) c2 c1
  └──────────┘
  T2 reads T1's write, but T2 commits BEFORE T1.
  If T1 now aborts, T2 has already committed with invalid data!
  The database cannot fix this — T2's commit is durable.

  ┌─────────────────────────────────────────────────────────┐
  │  Irrecoverable schedules are FORBIDDEN in any correct   │
  │  database implementation. Every real database ensures    │
  │  recoverability, usually via stronger conditions.       │
  └─────────────────────────────────────────────────────────┘
```

### 2.2 Cascadeless (Avoid Cascading Aborts) Schedules

A schedule is **cascadeless** if every transaction only reads values written by committed transactions.

```
CASCADING ABORT PROBLEM:

  w1(A) r2(A) r3(B) w2(B) r3(A)
                              ↑
                              T3 reads A — which version?

  Now suppose T1 aborts:
    - T2 read T1's write on A → T2 must abort (dirty read)
    - T3 read T2's write on B → T3 must also abort!

  This is a cascading abort: T1's abort triggers T2's abort,
  which triggers T3's abort.

  ┌─────────────────────────────────────────────────────────┐
  │  CASCADE CHAIN                                           │
  │                                                          │
  │  T1 aborts ──► T2 must abort ──► T3 must abort ──► ...  │
  │                                                          │
  │  In the worst case, ONE abort can cause N transactions   │
  │  to roll back, wasting all their work.                  │
  └─────────────────────────────────────────────────────────┘

CASCADELESS (prevents this):
  Transactions never read uncommitted data.
  If T2 wants to read A written by T1, T2 must wait until T1 commits.
  This is exactly what READ COMMITTED isolation level provides.
```

### 2.3 Strict Schedules

A schedule is **strict** if no transaction reads or writes a data item X until the last transaction that wrote X has committed or aborted.

```
STRICT (strongest practical condition):

  A transaction must not read OR WRITE a data item until the
  previous writer has committed or aborted.

  Why "no overwrite" matters:
    w1(A) w2(A) abort1

    After T1 aborts, what value should A have?
    - The before-image of T1's write? But T2 has already overwritten it.
    - T2's value? But T2 wrote on top of T1's uncommitted value.

    With strictness: T2 would have waited for T1 to commit/abort
    before writing A, avoiding this problem entirely.

Schedule Hierarchy:

  ┌──────────────────────────────────────────────────────────────┐
  │                     ALL SCHEDULES                             │
  │  ┌───────────────────────────────────────────────────────┐   │
  │  │                RECOVERABLE                             │   │
  │  │  ┌────────────────────────────────────────────────┐   │   │
  │  │  │            CASCADELESS (ACA)                    │   │   │
  │  │  │  ┌─────────────────────────────────────────┐   │   │   │
  │  │  │  │              STRICT                      │   │   │   │
  │  │  │  │  ┌──────────────────────────────────┐   │   │   │   │
  │  │  │  │  │           SERIAL                  │   │   │   │   │
  │  │  │  │  └──────────────────────────────────┘   │   │   │   │
  │  │  │  └─────────────────────────────────────────┘   │   │   │
  │  │  └────────────────────────────────────────────────┘   │   │
  │  └───────────────────────────────────────────────────────┘   │
  │                                                               │
  │  Most databases enforce STRICT + CONFLICT SERIALIZABLE       │
  │  at their highest isolation level (SERIALIZABLE).            │
  └──────────────────────────────────────────────────────────────┘
```

### 2.4 Schedule Analysis: Putting It All Together

```
Given a schedule, the complete analysis checklist:

  1. CONFLICT SERIALIZABLE?
     Build precedence graph → check for cycles
     If acyclic → topological sort gives equivalent serial order

  2. RECOVERABLE?
     For every read r_j(X) that reads a value written by w_i(X):
     Does c_i appear before c_j? If yes for all → recoverable

  3. CASCADELESS?
     Does every transaction only read committed values?
     i.e., for every r_j(X) reading w_i(X), does c_i appear before r_j(X)?

  4. STRICT?
     For every data item X written by w_i(X), no other transaction
     reads or writes X until c_i or a_i appears?

Example analysis:
  S: r1(A) w1(A) r2(A) w2(A) c1 c2

  Conflict serializable?
    Conflicts: (w1(A), r2(A)): T1→T2, (w1(A), w2(A)): T1→T2
    Graph: T1 → T2 (acyclic) → YES, equivalent to serial T1,T2

  Recoverable?
    T2 reads value written by T1 (r2(A) reads w1(A))
    c1 appears before c2 → YES

  Cascadeless?
    r2(A) reads w1(A). Does c1 appear before r2(A)?
    S: r1(A) w1(A) r2(A) w2(A) c1 c2
    c1 is AFTER r2(A) → NO, not cascadeless
    (T2 reads uncommitted data from T1)

  Strict?
    Since not even cascadeless → NO
```

---

## 3. Concurrency Control Protocols: Deep Theory

This section covers the *theoretical foundations* of concurrency control protocols. Topic 05 introduced 2PL, MVCC, and OCC at a practical level. Here we formalize *why* they work.

### 3.1 Two-Phase Locking (2PL): Proof of Correctness

The **Two-Phase Locking Protocol** states: A transaction must acquire all its locks before releasing any lock. This divides the transaction into two phases:

```
┌────────────────────────────────────────────────────────────────┐
│                                                                 │
│  Number of                                                      │
│  locks held     ╱╲                                              │
│       ▲        ╱  ╲                                             │
│       │       ╱    ╲                                            │
│       │      ╱      ╲                                           │
│       │     ╱        ╲                                          │
│       │    ╱          ╲                                         │
│       │   ╱            ╲                                        │
│       │  ╱              ╲                                       │
│       │ ╱                ╲                                      │
│       │╱                  ╲                                     │
│       └────────┬───────────┬──────────────────────► time        │
│           GROWING      SHRINKING                                │
│           PHASE        PHASE                                    │
│                    ▲                                             │
│                    │                                             │
│               LOCK POINT                                        │
│        (moment of last lock acquisition)                        │
│                                                                 │
│  THEOREM: Any schedule produced by 2PL is conflict              │
│  serializable. The serialization order equals the order         │
│  of lock points.                                                │
└────────────────────────────────────────────────────────────────┘
```

**Proof sketch (by contradiction):**

```
Assume 2PL produces a non-conflict-serializable schedule S.
Then the precedence graph of S has a cycle: T1 → T2 → ... → Tn → T1.

Edge Ti → Tj means Ti has a conflicting operation before Tj's on some data item X.
Since they conflict and access the same X, they must both hold a lock on X.
Ti held the lock first (its operation came first), so Ti released it before Tj acquired it.

Since Ti released a lock on X, Ti is in its SHRINKING phase at that point.
Since Tj acquired a lock on X after Ti released it, Tj's lock point is after
Ti's release → Tj's lock point is after Ti's lock point.

Following the cycle: lockpoint(T1) < lockpoint(T2) < ... < lockpoint(Tn) < lockpoint(T1)
This is a contradiction: lockpoint(T1) < lockpoint(T1).

Therefore, 2PL cannot produce a cycle → 2PL guarantees conflict serializability. QED
```

### 3.2 2PL Variants

```
┌────────────────────────────────────────────────────────────────────────┐
│  2PL VARIANT COMPARISON                                                │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  BASIC 2PL:                                                            │
│    Growing phase → Shrinking phase                                     │
│    ✓ Conflict serializable                                             │
│    ✗ Not strict (can release locks before commit)                      │
│    ✗ Cascading aborts possible                                         │
│    ✗ Not used in practice                                              │
│                                                                        │
│  STRICT 2PL (S2PL):                                                   │
│    Growing phase → Hold ALL exclusive (write) locks until commit/abort │
│    Shared locks may be released after lock point                       │
│    ✓ Conflict serializable                                             │
│    ✓ Strict (no cascading aborts for writes)                           │
│    ✓ Used by most databases (MySQL/InnoDB, SQL Server)                 │
│                                                                        │
│  RIGOROUS 2PL (SS2PL / Strong Strict 2PL):                            │
│    Hold ALL locks (shared AND exclusive) until commit/abort            │
│    ✓ Conflict serializable                                             │
│    ✓ Strict + cascadeless                                              │
│    ✓ Serialization order = commit order (simplifies reasoning)         │
│    ✓ Used by PostgreSQL's SERIALIZABLE (pre-SSI, with predicate locks)│
│    ✗ Lower concurrency (holds shared locks longer)                     │
│                                                                        │
│  Lock release timeline:                                                │
│                                                                        │
│  Basic 2PL:    [───grow───][──shrink──]                                │
│  Strict 2PL:   [───grow───][─hold X locks────] commit → release all   │
│  Rigorous 2PL: [───grow───][─hold ALL locks──] commit → release all   │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

### 3.3 Timestamp Ordering (TO) Protocol: Formal Rules

Each transaction T_i gets a timestamp TS(T_i) at birth. Each data item X tracks:
- **W-timestamp(X):** Timestamp of the last transaction that successfully wrote X
- **R-timestamp(X):** Timestamp of the last transaction that successfully read X

```
BASIC TIMESTAMP ORDERING RULES:

  When T_i wants to READ X:
  ┌─────────────────────────────────────────────────────────────┐
  │  If TS(T_i) < W-timestamp(X):                               │
  │    T_i is trying to read a value that was already            │
  │    overwritten by a younger transaction.                     │
  │    → REJECT: Abort and restart T_i with a new timestamp     │
  │                                                              │
  │  If TS(T_i) ≥ W-timestamp(X):                               │
  │    Read is valid.                                            │
  │    → ALLOW: Execute read, set R-timestamp(X) =              │
  │             max(R-timestamp(X), TS(T_i))                     │
  └─────────────────────────────────────────────────────────────┘

  When T_i wants to WRITE X:
  ┌─────────────────────────────────────────────────────────────┐
  │  If TS(T_i) < R-timestamp(X):                               │
  │    A younger transaction already read the old value of X.   │
  │    T_i's write would invalidate that read.                  │
  │    → REJECT: Abort and restart T_i                          │
  │                                                              │
  │  If TS(T_i) < W-timestamp(X):                               │
  │    A younger transaction already wrote X.                   │
  │    → With THOMAS WRITE RULE: skip this write (it's obsolete)│
  │    → Without: Abort and restart T_i                          │
  │                                                              │
  │  Otherwise:                                                  │
  │    → ALLOW: Execute write, set W-timestamp(X) = TS(T_i)    │
  └─────────────────────────────────────────────────────────────┘

THOMAS WRITE RULE optimization:
  If T_i wants to write X but TS(T_i) < W-timestamp(X), the write
  is "obsolete" because a newer transaction already wrote X. Instead
  of aborting, simply skip the write. This is safe because no future
  transaction will ever see T_i's value anyway.

  This allows more schedules to succeed but makes the resulting
  schedule potentially NOT conflict serializable (though still
  view serializable).
```

### 3.4 Multi-Version Timestamp Ordering

```
Instead of maintaining a single version of each data item, keep
multiple versions. Each write creates a new version rather than
overwriting the old one.

Data item X versions: X1, X2, X3, ...
Each version Xk has:
  - W-timestamp(Xk): timestamp of the transaction that created it
  - R-timestamp(Xk): largest timestamp of any transaction that read it
  - Value: the actual data

READ by T_i:
  Find the version Xk with the largest W-timestamp(Xk) ≤ TS(T_i)
  → T_i reads the most recent version that existed "before" it
  → Reads NEVER fail (no aborts needed for reads!)
  Set R-timestamp(Xk) = max(R-timestamp(Xk), TS(T_i))

WRITE by T_i:
  Find version Xk such that W-timestamp(Xk) is the largest ≤ TS(T_i)
  If R-timestamp(Xk) > TS(T_i):
    → A younger transaction already read Xk
    → T_i's new version would be between Xk and the reader's timestamp
    → ABORT T_i (writing would invalidate the reader's snapshot)
  Else:
    → Create new version with W-timestamp = TS(T_i)

  ┌──────────────────────────────────────────────────────────┐
  │  KEY INSIGHT: Multi-version schemes separate the         │
  │  read path from the write path. Readers never block      │
  │  writers, and writers never block readers. This is        │
  │  the theoretical foundation of MVCC.                     │
  └──────────────────────────────────────────────────────────┘
```

### 3.5 Serializable Snapshot Isolation (SSI): The Theory

SSI is the modern approach used by PostgreSQL (9.1+) and CockroachDB. It builds on Snapshot Isolation (SI) and detects a specific pattern called **dangerous structures** to prevent the anomalies SI allows.

```
Snapshot Isolation allows exactly ONE class of anomaly: WRITE SKEW.
Write skew occurs when two transactions read overlapping data, make
decisions based on what they read, and write to different items.

SSI theory (Cahill, Röhm, Fekete, 2008):

Every non-serializable execution under SI contains a structure called
a "dangerous structure" — two consecutive rw-antidependency edges
in the serialization graph where both source transactions are
concurrent:

  T1 ──rw──► T2 ──rw──► T3
  where T1 and T2 are concurrent, and T2 and T3 are concurrent
  (either T1=T3 is possible, making it a cycle of length 2)

  rw-antidependency: T_i reads X, then T_j writes X
  (T_j's write makes T_i's read "stale")

SSI Detection:
┌──────────────────────────────────────────────────────────────┐
│  For each transaction T, track:                               │
│    - inConflict:  was T the TARGET of an rw-antidependency   │
│                   from a concurrent transaction?              │
│    - outConflict: was T the SOURCE of an rw-antidependency   │
│                   toward a concurrent transaction?            │
│                                                               │
│  If any T has BOTH inConflict AND outConflict → it sits in   │
│  the middle of a dangerous structure → ABORT T                │
│                                                               │
│  This is conservative: it may abort transactions that would   │
│  have been serializable. But it never allows non-serializable │
│  executions.                                                  │
│                                                               │
│  FALSE POSITIVE RATE: typically 5-20% extra aborts compared  │
│  to a theoretically perfect serializable scheduler.          │
│  But the benefit is: readers never block, no lock overhead.  │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. MVCC Implementation Internals

Topic 05 introduced MVCC conceptually. This section dives deep into how MVCC is actually implemented: where versions are stored, how visibility is determined, and how old versions are garbage collected.

### 4.1 Version Storage Strategies

There are three fundamental approaches to storing multiple versions:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STRATEGY 1: APPEND-ONLY STORAGE (PostgreSQL)                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Every UPDATE creates a NEW tuple in the same heap.                    │
│  The old tuple remains in place (becomes a "dead tuple").              │
│                                                                         │
│  Heap page:                                                             │
│  ┌────────────────────────────────────────────────────────────┐        │
│  │  Tuple v1 (xmin=100, xmax=105)  ──► DEAD (old version)    │        │
│  │  Tuple v2 (xmin=105, xmax=110)  ──► DEAD (old version)    │        │
│  │  Tuple v3 (xmin=110, xmax=∞)    ──► LIVE (current)        │        │
│  │  Tuple v4 (different row)        ──► LIVE                  │        │
│  └────────────────────────────────────────────────────────────┘        │
│                                                                         │
│  Version chain traversal: follow t_ctid pointers                       │
│    v1.t_ctid → v2 → v3 (found live version)                           │
│                                                                         │
│  Chain ordering options:                                                │
│    Oldest-to-newest (O2N): PostgreSQL                                  │
│      + Natural append order                                             │
│      - Must traverse entire chain to find latest version               │
│      - Index points to oldest version → long chains hurt reads         │
│                                                                         │
│    Newest-to-first (N2O): alternative approach                         │
│      + Index always points to newest (most commonly needed) version    │
│      + Short traversal for recent reads                                │
│      - UPDATE must modify index to point to new head                   │
│      - More index maintenance overhead                                 │
│                                                                         │
│  Pros:                                                                  │
│    + Simple implementation                                              │
│    + No separate undo storage to manage                                │
│    + Good for workloads with few updates (OLAP)                        │
│                                                                         │
│  Cons:                                                                  │
│    - Table bloat: dead tuples waste space until VACUUM                 │
│    - VACUUM overhead: must scan heap, remove dead tuples, update FSM   │
│    - Index bloat: every version may have index entries                 │
│    - Long version chains slow down reads                               │
│                                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│  STRATEGY 2: DELTA STORAGE (MySQL/InnoDB, Oracle)                      │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  The main table always contains the LATEST version.                    │
│  Old versions are reconstructed from "undo logs" / "rollback segments" │
│                                                                         │
│  Main table (clustered index):                                          │
│  ┌────────────────────────────────────────────┐                        │
│  │  Row (current version, DB_ROLL_PTR → undo) │                        │
│  └──────────────────────┬─────────────────────┘                        │
│                          │ DB_ROLL_PTR                                  │
│                          ▼                                              │
│  Undo log:                                                              │
│  ┌──────────────────────────────┐                                      │
│  │  Delta: {balance: 1000→500}  │ ← what changed                      │
│  │  prev_undo_ptr ──────────────┼──┐                                   │
│  └──────────────────────────────┘  │                                   │
│                                     ▼                                   │
│  ┌──────────────────────────────┐                                      │
│  │  Delta: {balance: 1500→1000} │ ← even older change                 │
│  │  prev_undo_ptr → NULL        │                                      │
│  └──────────────────────────────┘                                      │
│                                                                         │
│  To read an old version:                                                │
│    1. Start from current version in main table                         │
│    2. Follow DB_ROLL_PTR chain                                         │
│    3. Apply deltas in reverse to reconstruct desired version           │
│                                                                         │
│  Pros:                                                                  │
│    + Main table always has latest version → fast for current reads     │
│    + Index entries point to current version (no index bloat)           │
│    + Undo segments are circular → space reuse is natural               │
│                                                                         │
│  Cons:                                                                  │
│    - Reconstructing old versions is CPU-intensive (apply N deltas)     │
│    - Long-running transactions prevent undo purge → undo growth       │
│    - "History list length" in InnoDB can grow unbounded                │
│                                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│  STRATEGY 3: SEPARATE VERSION STORE (SQL Server, Hekaton)              │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Versions stored in a dedicated version store (tempdb in SQL Server).  │
│  Main table has current version with pointer to version store.         │
│                                                                         │
│  Main table:                                                            │
│  ┌────────────────────────────────────────────┐                        │
│  │  Row (current, version_ptr → version store)│                        │
│  └──────────────────────┬─────────────────────┘                        │
│                          │                                              │
│                          ▼                                              │
│  Version store (tempdb):                                                │
│  ┌──────────────────────────────┐                                      │
│  │  Full copy of previous row   │                                      │
│  │  version_ptr ────────────────┼──┐                                   │
│  └──────────────────────────────┘  │                                   │
│                                     ▼                                   │
│  ┌──────────────────────────────┐                                      │
│  │  Full copy of even older row │                                      │
│  │  version_ptr → NULL          │                                      │
│  └──────────────────────────────┘                                      │
│                                                                         │
│  Pros:                                                                  │
│    + Clean separation of concerns                                      │
│    + No table bloat (versions are elsewhere)                           │
│    + Full row copies → no delta reconstruction needed                  │
│                                                                         │
│  Cons:                                                                  │
│    - Version store can pressure tempdb (SQL Server known issue)        │
│    - Extra I/O to access versions in separate storage                  │
│    - Memory overhead for in-memory version stores                      │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Transaction Visibility: Snapshot Algorithms

The core question in MVCC: **which version of a row should this transaction see?**

```
POSTGRESQL VISIBILITY RULES (tuple-level):

Every tuple has:
  xmin:  Transaction ID that created this tuple (INSERT or UPDATE-new)
  xmax:  Transaction ID that deleted/updated this tuple (0 if live)
  t_infomask: hint bits (committed, aborted, etc.)

Visibility check for transaction T with snapshot S:

  CanSeeThisTuple(tuple, snapshot):
    1. Is xmin committed?
       - Check pg_xact (commit log / clog)
       - If xmin is aborted → tuple was never valid → INVISIBLE
       - If xmin is in-progress and xmin ≠ my_xid → INVISIBLE

    2. Is xmin visible in my snapshot?
       - If xmin ≥ snapshot.xmax → started after my snapshot → INVISIBLE
       - If xmin is in snapshot.xip[] (in-progress list) → INVISIBLE

    3. Is xmax set (tuple was deleted/updated)?
       - If xmax = 0 → tuple is live → VISIBLE
       - If xmax is aborted → deletion didn't happen → VISIBLE
       - If xmax is committed AND visible in my snapshot → INVISIBLE
       - If xmax is in-progress or not in snapshot → VISIBLE

  ┌──────────────────────────────────────────────────────────────┐
  │  SNAPSHOT STRUCTURE (PostgreSQL):                             │
  │                                                               │
  │  typedef struct SnapshotData {                                │
  │    TransactionId xmin;    // lowest active txid at snapshot   │
  │    TransactionId xmax;    // first unassigned txid at snap    │
  │    TransactionId *xip;    // array of in-progress txids      │
  │    uint32 xcnt;           // count of in-progress txids      │
  │  };                                                           │
  │                                                               │
  │  A txid is visible if:                                        │
  │    txid < xmin                     → definitely committed    │
  │    txid ≥ xmax                     → definitely not visible  │
  │    xmin ≤ txid < xmax AND          → check xip array:        │
  │      txid NOT IN xip               → committed, visible      │
  │      txid IN xip                   → was in-progress, hidden │
  └──────────────────────────────────────────────────────────────┘
```

```
INNODB VISIBILITY (ReadView):

InnoDB uses a "ReadView" structure similar to PostgreSQL's snapshot:

  struct ReadView {
    trx_id_t m_low_limit_id;    // txids ≥ this are invisible
    trx_id_t m_up_limit_id;     // txids < this are visible
    trx_id_t m_creator_trx_id;  // my own transaction ID
    ids_t    m_ids;              // sorted list of active txids at creation
  };

  Visibility check for row with DB_TRX_ID = trx_id:
    if (trx_id == m_creator_trx_id)  → VISIBLE (my own changes)
    if (trx_id < m_up_limit_id)      → VISIBLE (committed before snapshot)
    if (trx_id >= m_low_limit_id)    → INVISIBLE (started after snapshot)
    if (trx_id IN m_ids)             → INVISIBLE (was active at snapshot)
    else                              → VISIBLE (committed between bounds)

  READ COMMITTED:   new ReadView for EVERY statement
  REPEATABLE READ:  one ReadView for the entire transaction
  SERIALIZABLE:     same as REPEATABLE READ + gap locks (InnoDB approach)
```

### 4.3 MVCC Garbage Collection (Version Cleanup)

Old versions that no transaction can ever see again must be cleaned up. This is one of the hardest problems in MVCC implementation.

```
WHEN IS A VERSION SAFE TO REMOVE?

  A version V of data item X is safe to remove when:
    1. V is not the current version (there's a newer committed version)
    2. No active transaction has a snapshot old enough to see V
    3. No future transaction will need V (all possible readers have committed)

  Formally: remove version with W-timestamp(V) < min(all active snapshot timestamps)

  ┌──────────────────────────────────────────────────────────────┐
  │  THE LONG-RUNNING TRANSACTION PROBLEM                        │
  │                                                               │
  │  If ONE transaction holds a snapshot from timestamp 100,     │
  │  and the current timestamp is 10,000, then ALL versions      │
  │  from timestamp 100 to 10,000 must be retained.             │
  │                                                               │
  │  This is the #1 operational problem with MVCC databases.     │
  │  A single forgotten transaction can cause:                   │
  │    - PostgreSQL: massive table bloat (dead tuples)           │
  │    - InnoDB: undo log growth (history list length >> 0)      │
  │    - SQL Server: tempdb version store explosion              │
  └──────────────────────────────────────────────────────────────┘
```

**PostgreSQL VACUUM internals:**

```
VACUUM process for table T:

  1. DETERMINE HORIZON
     oldestXmin = min(xmin of all active snapshots across all backends)
     Any tuple with xmax < oldestXmin AND xmax is committed → dead

  2. SCAN HEAP (sequential scan of all pages)
     For each page:
       For each tuple:
         if tuple is DEAD (xmax committed and xmax < oldestXmin):
           Mark tuple's space as reusable in FSM (Free Space Map)
           Remove index entries pointing to this tuple

  3. UPDATE VISIBILITY MAP
     Pages where ALL tuples are visible to all → mark as "all-visible"
     All-visible pages can be skipped by index-only scans

  4. UPDATE FREE SPACE MAP
     Record how much free space each page now has
     Future INSERTs will use FSM to find pages with space

  5. FREEZE OLD TUPLES
     Tuples with xmin older than vacuum_freeze_min_age:
       Set xmin to FrozenTransactionId (special value = 2)
       This tuple is visible to ALL transactions forever
       Prevents transaction ID wraparound (32-bit txid space)

AUTOVACUUM triggers:
  ┌────────────────────────────────────────────────────────────┐
  │  Trigger when:                                              │
  │  dead_tuples > autovacuum_vacuum_threshold +                │
  │                autovacuum_vacuum_scale_factor * n_live_tuples│
  │                                                              │
  │  Default: 50 + 0.2 * n_live_tuples                         │
  │  For a table with 1M rows: triggers at 200,050 dead tuples │
  │                                                              │
  │  CRITICAL PARAMETERS:                                       │
  │    autovacuum_vacuum_cost_delay = 2ms (throttling)          │
  │    autovacuum_vacuum_cost_limit = 200 (I/O budget)          │
  │    autovacuum_max_workers = 3 (parallel vacuums)            │
  │    autovacuum_naptime = 1min (check frequency)              │
  └────────────────────────────────────────────────────────────┘
```

**InnoDB purge system:**

```
InnoDB PURGE THREAD:

  1. PURGE COORDINATOR checks purge_sys->purge_trx_no
     (oldest ReadView's m_low_limit_id across all connections)

  2. Walk history list in undo log:
     For each undo record older than purge horizon:
       - Remove the old version from undo log
       - If DELETE: remove the "delete-marked" row from clustered index
       - Clean up secondary index entries pointing to purged versions

  3. Free undo log segments that are fully purged

  MONITORING:
    SHOW ENGINE INNODB STATUS;
    → "History list length: 12345"

    If this number keeps growing → purge cannot keep up
    Usually caused by: long-running transaction holding old ReadView

  ┌────────────────────────────────────────────────────────────┐
  │  InnoDB Purge Configuration:                                │
  │    innodb_purge_threads = 4      (parallel purge workers)  │
  │    innodb_purge_batch_size = 300 (undo records per batch)  │
  │    innodb_max_purge_lag = 0      (0=unlimited, else delay  │
  │                                   DML when lag exceeds)    │
  │    innodb_max_purge_lag_delay = 0 (max delay in microsec)  │
  └────────────────────────────────────────────────────────────┘
```

### 4.4 MVCC and Indexes: The Hidden Complexity

```
THE INDEX PROBLEM:
  When a row has multiple versions, what do index entries point to?

  POSTGRESQL:
  ┌──────────────────────────────────────────────────────────────┐
  │  Indexes point to PHYSICAL tuple locations (TID = page,offset)│
  │  Every version of a row may have its own index entry.        │
  │                                                               │
  │  Index entry → heap page → tuple (check visibility)          │
  │                                                               │
  │  Consequence: UPDATE creates new index entries in ALL indexes │
  │  on the table, even if the indexed columns didn't change!    │
  │                                                               │
  │  HOT (Heap-Only Tuple) optimization:                         │
  │    If UPDATE doesn't change any indexed column AND new tuple  │
  │    fits on the same heap page → no new index entries needed.  │
  │    Old index entry → old tuple → t_ctid → new tuple (same pg)│
  │                                                               │
  │    HOT avoids index bloat for UPDATE-heavy workloads where   │
  │    indexed columns are stable (e.g., updating a status flag  │
  │    on a table indexed by id).                                 │
  └──────────────────────────────────────────────────────────────┘

  INNODB:
  ┌──────────────────────────────────────────────────────────────┐
  │  Clustered index (primary key) always has latest version.    │
  │  Secondary indexes have a "min trx id" to help with         │
  │  visibility but may still need to consult undo log.         │
  │                                                               │
  │  Secondary index lookup:                                      │
  │    sec_index → primary key value → clustered index lookup    │
  │    → if row's DB_TRX_ID is not visible, follow undo chain   │
  │                                                               │
  │  "Change buffer" (formerly insert buffer):                    │
  │    Secondary index changes are buffered and merged lazily.   │
  │    Reduces random I/O for non-unique secondary index updates.│
  └──────────────────────────────────────────────────────────────┘
```

### 4.5 MVCC Anomalies and Limitations

```
MVCC DOES NOT AUTOMATICALLY MEAN SERIALIZABLE!

Standard MVCC (Snapshot Isolation) allows:

  1. WRITE SKEW:
     T1 reads X,Y. T2 reads X,Y. T1 writes X. T2 writes Y.
     Both make decisions based on stale reads of the other's write target.

     Classic example: on-call doctors
       Constraint: at least one doctor must be on-call
       T1: reads 2 doctors on-call, removes self → 1 on-call
       T2: reads 2 doctors on-call, removes self → 1 on-call
       Both commit: 0 doctors on-call! Constraint violated.

  2. READ-ONLY TRANSACTION ANOMALY:
     Even a read-only transaction can observe a non-serializable state
     under basic SI. (Formally proven by Fekete et al.)

     T1: writes X=1 (was 0)
     T2: reads X=0 (snapshot before T1), writes Y=1
     T3 (read-only): reads X=1 (sees T1), reads Y=0 (doesn't see T2)
     → T3 sees a state that could not exist in any serial execution

  Solutions:
  ┌─────────────────────────────────────────────────────────────┐
  │  Database          │ How they achieve serializability       │
  ├────────────────────┼───────────────────────────────────────┤
  │  PostgreSQL 9.1+   │ SSI (detect dangerous structures)     │
  │  MySQL/InnoDB      │ 2PL + gap locks (not MVCC-based)     │
  │  SQL Server        │ SI + manual UPDLOCK hints             │
  │  CockroachDB       │ SSI (like PostgreSQL)                 │
  │  Oracle            │ SI only (no true serializable)        │
  │  TiDB              │ Percolator-based optimistic txns      │
  └─────────────────────────────────────────────────────────────┘
```

---

## 5. Pessimistic vs Optimistic Locking: Complete Analysis

Topic 05 introduced these concepts. This section provides the complete theoretical framework, implementation details, and decision criteria.

### 5.1 Pessimistic Concurrency Control (PCC)

Pessimistic control assumes conflicts are **likely** and prevents them proactively by acquiring locks before accessing data.

```
PESSIMISTIC EXECUTION MODEL:

  Transaction T wants to read/write data item X:

  ┌──────────────────────────────────────────────────────────────┐
  │  1. REQUEST lock on X from Lock Manager                      │
  │     ├── Lock available? → GRANT, proceed to step 2          │
  │     └── Lock held by another txn in conflicting mode?       │
  │         ├── WAIT in lock queue (block this thread)           │
  │         └── If deadlock detected → ABORT one transaction    │
  │                                                               │
  │  2. ACCESS data item X (read or write)                       │
  │                                                               │
  │  3. HOLD lock until end of transaction (S2PL)                │
  │                                                               │
  │  4. At COMMIT/ABORT: release all locks                       │
  └──────────────────────────────────────────────────────────────┘

  Lock compatibility matrix:
            IS    IX    S     SIX   X
    IS      Y     Y     Y     Y     N
    IX      Y     Y     N     N     N
    S       Y     N     Y     N     N
    SIX     Y     N     N     N     N
    X       N     N     N     N     N
```

### 5.2 Optimistic Concurrency Control (OCC)

Optimistic control assumes conflicts are **rare** and validates at commit time. The original OCC protocol (Kung & Robinson, 1981) has three phases:

```
OPTIMISTIC EXECUTION MODEL (three-phase):

  ┌──────────────────────────────────────────────────────────────┐
  │                                                               │
  │  PHASE 1: READ PHASE                                         │
  │    - Read data items into a local workspace (read set)       │
  │    - Write to local copies only (write set)                  │
  │    - NO locks acquired, NO validation, NO blocking           │
  │    - Other transactions are completely unaware of this txn   │
  │                                                               │
  │  PHASE 2: VALIDATION PHASE                                   │
  │    - Check if this transaction's execution conflicts with    │
  │      any concurrent transaction that has already validated   │
  │    - This phase is typically serialized (one txn at a time)  │
  │    - If validation FAILS → ABORT and restart                 │
  │                                                               │
  │  PHASE 3: WRITE PHASE                                        │
  │    - If validation succeeded, apply writes from local        │
  │      workspace to the actual database                        │
  │    - This is the only time real data is modified             │
  │                                                               │
  │  Timeline:                                                    │
  │  ───────────────────────────────────────────────────►         │
  │  [────── READ ──────][── VALIDATE ──][── WRITE ──]           │
  │  no locks, no blocks   serialized     apply changes          │
  └──────────────────────────────────────────────────────────────┘
```

**OCC Validation rules (Kung-Robinson):**

```
Transaction T_i validates after T_j (T_j has a smaller timestamp).
T_i passes validation if ONE of these conditions holds:

  RULE 1: T_j completed all 3 phases before T_i started its read phase.
    (No overlap at all → trivially safe)

  RULE 2: T_j completed its write phase before T_i started its write phase,
    AND T_j's write set ∩ T_i's read set = empty
    (T_i didn't read anything T_j wrote → T_j's writes don't affect T_i)

  RULE 3: T_j completed its read phase before T_i completed its read phase,
    AND T_j's write set ∩ T_i's read set = empty
    AND T_j's write set ∩ T_i's write set = empty
    (No overlap in what was read or written → independent)

  ┌────────────────────────────────────────────────────────────┐
  │  If NONE of these rules can be satisfied → ABORT T_i      │
  │                                                             │
  │  The abort rate depends on the workload:                   │
  │    - Read-heavy, low contention: < 1% aborts → OCC wins   │
  │    - Write-heavy, high contention: 30%+ aborts → OCC bad  │
  └────────────────────────────────────────────────────────────┘
```

### 5.3 Head-to-Head Comparison

```
┌────────────────────────────────────────────────────────────────────────┐
│  PESSIMISTIC vs OPTIMISTIC: DECISION FRAMEWORK                        │
├──────────────────────┬───────────────────────┬────────────────────────┤
│  Criterion           │ Pessimistic (2PL)      │ Optimistic (OCC)      │
├──────────────────────┼───────────────────────┼────────────────────────┤
│  Conflict assumption │ Conflicts are likely   │ Conflicts are rare    │
│  When work is wasted │ Never (waits instead)  │ On abort (redo all)   │
│  Blocking            │ Yes (lock waits)       │ No (until validate)   │
│  Deadlocks           │ Possible (need detect) │ Impossible            │
│  Starvation          │ Possible (priority)    │ Possible (repeated    │
│                      │                        │ abort of same txn)    │
│  Overhead per txn    │ Lock acquire/release   │ Track read/write sets │
│  Abort cost          │ Low (rare aborts)      │ High (redo entire txn)│
│  Best for            │ High-contention OLTP   │ Read-heavy, low-write │
│  Worst for           │ Read-heavy (lock thrsh)│ Write-heavy hotspots  │
│  Throughput at low   │ Lower (lock overhead)  │ Higher (no lock cost) │
│  contention          │                        │                        │
│  Throughput at high  │ Higher (no wasted work)│ Lower (abort storms)  │
│  contention          │                        │                        │
├──────────────────────┼───────────────────────┼────────────────────────┤
│  Used by             │ MySQL/InnoDB           │ Hekaton (SQL Server)  │
│                      │ SQL Server             │ CockroachDB           │
│                      │ PostgreSQL (non-SSI)   │ Google Percolator     │
│                      │ Oracle                 │ TiDB                  │
│                      │ DB2                    │ FoundationDB          │
│                      │                        │ Silo (research)       │
└──────────────────────┴───────────────────────┴────────────────────────┘
```

### 5.4 Hybrid Approaches: What Production Databases Actually Do

```
Almost no production database uses PURE pessimistic or PURE optimistic
concurrency control. Real databases mix strategies:

POSTGRESQL:
  - MVCC for reads (optimistic — readers never block)
  - Locks for writes (pessimistic — row-level exclusive locks)
  - SSI for SERIALIZABLE (optimistic — detect conflicts at commit)
  → Hybrid: optimistic reads + pessimistic writes + optimistic serializable

MYSQL/INNODB:
  - MVCC for consistent reads (optimistic — uses ReadView)
  - 2PL for writes (pessimistic — row locks + gap locks)
  - SERIALIZABLE = REPEATABLE READ with auto S-lock on all reads
  → Hybrid: optimistic reads + pessimistic writes + pessimistic serializable

COCKROACHDB:
  - MVCC for all reads (optimistic — timestamp-based)
  - Optimistic for writes (detect conflicts at commit via write intents)
  - SSI for serializable (like PostgreSQL)
  - But: pessimistic locking available via SELECT FOR UPDATE
  → Mostly optimistic with pessimistic escape hatches

HEKATON (SQL Server in-memory OLTP):
  - Fully optimistic — no locks at all
  - Validation at commit time using version chain timestamps
  - SERIALIZABLE uses range validation (phantom detection)
  → Pure optimistic (unique among major databases)
```

---

## 6. Deadlock Handling: Prevention, Detection, Resolution

Pessimistic concurrency control can lead to deadlocks. Databases handle this through three strategies: prevention, detection, and avoidance.

### 6.1 The Deadlock Problem

```
DEADLOCK: Two or more transactions are each waiting for a lock held
by another transaction in the group, forming a cycle.

Simplest case (two transactions):

  T1 holds lock on A, wants lock on B
  T2 holds lock on B, wants lock on A

  T1: LOCK(A) ─────────────── WAIT(B) ──── blocked!
  T2: ──────── LOCK(B) ────── WAIT(A) ──── blocked!

  ┌───────────────────────────────────────┐
  │  Wait-For Graph:                       │
  │                                        │
  │    T1 ────waits-for────► T2            │
  │    ▲                      │            │
  │    └──────waits-for───────┘            │
  │                                        │
  │    CYCLE detected → DEADLOCK!         │
  └───────────────────────────────────────┘

Complex case (three transactions):

  T1 holds A, wants B
  T2 holds B, wants C
  T3 holds C, wants A

    T1 → T2 → T3 → T1  (cycle of length 3)
```

### 6.2 Deadlock Prevention: Wound-Wait and Wait-Die

Prevention protocols assign timestamps to transactions and use them to break potential cycles proactively. No deadlock detection is needed.

```
WAIT-DIE PROTOCOL (non-preemptive):

  When T_i requests a lock held by T_j:
    If TS(T_i) < TS(T_j):  [T_i is OLDER than T_j]
      T_i WAITS for T_j to release the lock
      (Old waits for young — "elders have patience")

    If TS(T_i) > TS(T_j):  [T_i is YOUNGER than T_j]
      T_i DIES (aborts) and restarts with SAME timestamp
      (Young must defer to old — "youth must yield")

  ┌─────────────────────────────────────────────────────────┐
  │  Why this prevents deadlocks:                            │
  │                                                          │
  │  The "waits-for" edges always go from older to younger: │
  │    older → younger (waits)                               │
  │    younger → older (dies, no edge)                       │
  │                                                          │
  │  Since timestamps are totally ordered and edges only go  │
  │  in one direction (old→young), cycles are impossible.   │
  │                                                          │
  │  Downside: younger transactions may abort repeatedly     │
  │  if they keep conflicting with older transactions.       │
  │  Keeping the same timestamp on restart ensures they      │
  │  eventually become "old enough" to proceed.             │
  └─────────────────────────────────────────────────────────┘

WOUND-WAIT PROTOCOL (preemptive):

  When T_i requests a lock held by T_j:
    If TS(T_i) < TS(T_j):  [T_i is OLDER than T_j]
      T_i WOUNDS T_j: T_j is aborted (preempted), T_i gets the lock
      (Old takes priority — "elders take what they need")

    If TS(T_i) > TS(T_j):  [T_i is YOUNGER than T_j]
      T_i WAITS for T_j
      (Young waits for old — "youth must be patient")

  ┌─────────────────────────────────────────────────────────┐
  │  Why this prevents deadlocks:                            │
  │                                                          │
  │  "waits-for" edges only go from younger to older:       │
  │    younger → older (waits)                               │
  │    older → younger (wounds, no edge — younger is killed)│
  │                                                          │
  │  Unidirectional edges → no cycles → no deadlocks.       │
  │                                                          │
  │  Advantage over Wait-Die: Wound-Wait aborts younger     │
  │  transactions that have done LESS work, so less work is  │
  │  wasted. It's generally preferred in practice.          │
  └─────────────────────────────────────────────────────────┘

COMPARISON:

  ┌──────────────────┬───────────────────┬───────────────────┐
  │  Criterion        │ Wait-Die          │ Wound-Wait        │
  ├──────────────────┼───────────────────┼───────────────────┤
  │  Who gets aborted │ Young requester   │ Young holder      │
  │  Who waits        │ Old requester     │ Young requester   │
  │  Preemptive?      │ No                │ Yes               │
  │  Work wasted      │ More (young may   │ Less (young has   │
  │                   │ have done a lot)  │ done less work)   │
  │  Starvation-free? │ Yes (same ts)     │ Yes (same ts)     │
  │  Used by          │ Some distributed  │ Google Spanner,   │
  │                   │ databases         │ CockroachDB       │
  └──────────────────┴───────────────────┴───────────────────┘
```

### 6.3 Deadlock Detection: Wait-For Graph

Most traditional databases (PostgreSQL, MySQL, SQL Server, Oracle) use **deadlock detection** rather than prevention.

```
DEADLOCK DETECTION ALGORITHM:

  1. Maintain a Wait-For Graph (WFG):
     - Nodes = active transactions
     - Edge T_i → T_j means T_i is waiting for a lock held by T_j

  2. Periodically (or on every lock wait), check WFG for cycles:
     - Run DFS/BFS cycle detection
     - PostgreSQL: checks on every lock wait (immediately)
     - MySQL/InnoDB: dedicated background thread, checks every
       innodb_deadlock_detect_interval (default: immediately on wait)
     - SQL Server: dedicated monitor thread, runs every 5 seconds
       (or immediately under high lock pressure)

  3. When cycle found → choose a VICTIM transaction to abort

  VICTIM SELECTION criteria:
  ┌─────────────────────────────────────────────────────────────┐
  │  Factor                │ Goal                               │
  ├────────────────────────┼────────────────────────────────────┤
  │  Work done so far      │ Abort txn with least work invested│
  │  Number of locks held  │ Abort txn holding fewest locks    │
  │  Remaining work        │ Abort txn closest to completion   │
  │  Number of rollbacks   │ Avoid re-aborting same txn        │
  │  Priority              │ Abort lower-priority transaction  │
  │  Cycle involvement     │ Break the most cycles possible    │
  └─────────────────────────────────────────────────────────────┘

  PostgreSQL: aborts the transaction that caused the deadlock
              (the one that just tried to acquire the lock)

  MySQL/InnoDB: aborts the transaction with the fewest undo log
                records (proxy for "least work done").
                Can be overridden: SET innodb_deadlock_detect = OFF;
                (then relies on innodb_lock_wait_timeout instead)

  SQL Server: choose victim based on DEADLOCK_PRIORITY setting
              (LOW, NORMAL, HIGH, or numeric -10 to 10)
              Lowest priority gets killed. If tied, least log used.
```

### 6.4 Deadlock Avoidance: Timeout-Based

```
TIMEOUT-BASED DEADLOCK HANDLING:

  The simplest approach: if a transaction waits for a lock longer
  than a timeout, assume deadlock and abort it.

  ┌────────────────────────────────────────────────────────────┐
  │  Database            │ Default lock wait timeout           │
  ├──────────────────────┼────────────────────────────────────┤
  │  PostgreSQL           │ lock_timeout = 0 (disabled)        │
  │                      │ deadlock_timeout = 1s (then detect) │
  │  MySQL/InnoDB        │ innodb_lock_wait_timeout = 50s      │
  │  SQL Server          │ @@LOCK_TIMEOUT = -1 (infinite)      │
  │  Oracle              │ No timeout (detect immediately)     │
  │  CockroachDB         │ 2.5s (uses push-based resolution)  │
  └────────────────────────────────────────────────────────────┘

  PostgreSQL's approach is actually hybrid:
    1. Transaction requests lock → blocks
    2. After deadlock_timeout (1s) → run deadlock detection
    3. If deadlock found → abort victim
    4. If no deadlock → keep waiting (or until lock_timeout)

  TRADEOFF:
    Short timeout → false positives (abort txns that aren't deadlocked)
    Long timeout → wasted time waiting before discovering real deadlocks
    Detection → accurate but has CPU cost of graph traversal
```

### 6.5 Distributed Deadlock Detection

```
In a distributed database, the Wait-For Graph spans multiple nodes.
Local cycle detection at each node is insufficient — the cycle may
involve transactions on different nodes.

CENTRALIZED DETECTION:
  One coordinator collects all local WFGs, merges them, checks for cycles.
  Problem: coordinator bottleneck, network delays cause phantom deadlocks.

DISTRIBUTED DETECTION (path pushing):
  Each node sends its local WFG edges to other nodes.
  Nodes extend paths and detect cycles locally.
  Problem: high message overhead, phantom deadlocks from stale data.

TIMEOUT (what most distributed DBs actually do):
  CockroachDB, Spanner, TiDB: primarily use timeouts + priority-based
  resolution rather than distributed deadlock detection.

  CockroachDB's approach:
    1. Transactions have priorities (random, but adjustable)
    2. When T_i blocks on T_j:
       If priority(T_i) > priority(T_j):
         T_i "pushes" T_j's timestamp forward (for reads)
         or aborts T_j (for writes)
       Else:
         T_i waits, with timeout
    3. Aborted transaction restarts with higher priority
       → prevents starvation

  This avoids distributed deadlock detection entirely,
  at the cost of occasionally aborting non-deadlocked transactions.
```
