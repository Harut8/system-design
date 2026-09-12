# Job Scheduler with PostgreSQL: A Deep Dive

## Executive Summary

Building a job scheduler on PostgreSQL sounds deceptively simple — `INSERT` a row, `SELECT ... FOR UPDATE SKIP LOCKED`, process, `UPDATE` to done. At 100 jobs/minute this works. At 10,000 jobs/second, PostgreSQL's MVCC model, VACUUM mechanics, and index maintenance conspire to degrade performance catastrophically. This document dissects every failure mode and provides production-grade solutions.

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Schema Design & Table Strategy](#3-schema-design--table-strategy)
4. [PostgreSQL MVCC Deep Dive: Why Job Tables Bloat](#4-postgresql-mvcc-deep-dive-why-job-tables-bloat)
5. [VACUUM Mechanics & Operational Failures](#5-vacuum-mechanics--operational-failures)
6. [The Ordered Scan Problem](#6-the-ordered-scan-problem)
7. [LISTEN/NOTIFY: Promise and Pitfalls](#7-listennotify-promise-and-pitfalls)
8. [Heartbeat, Lease & Zombie Workers](#8-heartbeat-lease--zombie-workers)
9. [Partitioning: DROP PARTITION vs DELETE](#9-partitioning-drop-partition-vs-delete)
10. [Table Strategy: Single vs Per-Tenant vs Partitioned](#10-table-strategy-single-vs-per-tenant-vs-partitioned)
11. [Multi-Tenancy & Noisy Neighbor Prevention](#11-multi-tenancy--noisy-neighbor-prevention)
12. [Production Architecture](#12-production-architecture)
13. [Monitoring & Operational Runbook](#13-monitoring--operational-runbook)
14. [Decision Framework](#14-decision-framework)
15. [Data Model Gaps: What the Naive Schema Misses](#15-data-model-gaps-what-the-naive-schema-misses)
16. [Claim Protocol: Subtleties](#16-claim-protocol-subtleties)
17. [Failure & Retry Semantics](#17-failure--retry-semantics)
18. [Scheduling: Cron, Catch-Up & Time](#18-scheduling-cron-catch-up--time)
19. [Job Semantics: Ordering, Dependencies & Cancellation](#19-job-semantics-ordering-dependencies--cancellation)
20. [Producer Side: Transactional Enqueue & Outbox](#20-producer-side-transactional-enqueue--outbox)
21. [Worker Runtime](#21-worker-runtime)
22. [Advanced PostgreSQL Operations](#22-advanced-postgresql-operations)
23. [Observability Gaps](#23-observability-gaps)
24. [Security & Compliance](#24-security--compliance)
25. [Scaling: Capacity Planning & Exit Path](#25-scaling-capacity-planning--exit-path)
26. [Developer Experience & Testing](#26-developer-experience--testing)

---

## 1. Requirements

### Functional

| Feature | Specification |
|---------|---------------|
| Job submission | Payload + priority + scheduled_at + tenant_id |
| Execution | Exactly-once via claim + lease |
| Retry | Configurable max_attempts + exponential backoff |
| Scheduling | One-time delayed + recurring cron |
| Multi-tenancy | 1,000+ tenants, fair scheduling |
| History | Queryable job history, 90-day retention |

### Non-Functional

```
Enqueue throughput:     10,000 jobs/sec
Dequeue throughput:     5,000 jobs/sec
Claim latency p99:     < 50ms
Availability:          99.95%
Job retention:         7 days hot, 90 days archive
Recovery:              Orphaned jobs requeued within 60s
```

### Constraints

- PostgreSQL is the **only** persistent store — no Redis, no Kafka, no SQS
- Workers are stateless and horizontally scalable
- Job payloads < 64 KB

---

## 2. High-Level Architecture

```
                         ┌──────────────────────────────────────────┐
                         │              API Gateway                 │
                         │         (Rate Limit, Auth, Tenant ID)    │
                         └──────────┬───────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
                    ▼               ▼               ▼
             ┌──────────┐   ┌──────────┐   ┌──────────────┐
             │ Enqueue  │   │  Query   │   │   Admin      │
             │ Service  │   │  Service │   │   Service    │
             └────┬─────┘   └────┬─────┘   └──────┬───────┘
                  │              │                 │
                  ▼              ▼                 ▼
         ┌────────────────────────────────────────────────┐
         │              PostgreSQL Primary                 │
         │  ┌────────────┐  ┌──────────┐  ┌────────────┐  │
         │  │ jobs_queue  │  │ jobs_    │  │ jobs_      │  │
         │  │ (hot,       │  │ archive  │  │ recurring  │  │
         │  │  partitioned)│  │          │  │            │  │
         │  └─────┬──────┘  └──────────┘  └────────────┘  │
         │        │  LISTEN/NOTIFY                         │
         └────────┼────────────────────────────────────────┘
                  │
      ┌───────────┼───────────────┐
      │           │               │
      ▼           ▼               ▼
 ┌─────────┐ ┌─────────┐   ┌─────────┐
 │ Worker 1│ │ Worker 2│   │ Worker N│
 │ (claim  │ │ (claim  │   │ (claim  │
 │  + exec)│ │  + exec)│   │  + exec)│
 └─────────┘ └─────────┘   └─────────┘
```

### Core Flow

```
1. Client POSTs a job → Enqueue Service INSERTs into jobs_queue
2. PostgreSQL fires NOTIFY on channel "jobs_ready"
3. Workers receive notification → attempt to claim via SELECT FOR UPDATE SKIP LOCKED
4. Worker executes job, sends heartbeats, UPDATEs status to completed/failed
5. Reaper process detects expired leases → requeues orphaned jobs
6. Archiver moves completed jobs to jobs_archive partition (or drops old partitions)
```

---

## 3. Schema Design & Table Strategy

### Core Schema

```sql
CREATE TABLE jobs_queue (
    id              BIGINT GENERATED ALWAYS AS IDENTITY,
    idempotency_key UUID,
    tenant_id       INTEGER        NOT NULL,
    queue_name      TEXT           NOT NULL DEFAULT 'default',
    priority        SMALLINT       NOT NULL DEFAULT 0,
    status          TEXT           NOT NULL DEFAULT 'pending',
    payload         JSONB          NOT NULL,
    
    -- Scheduling
    scheduled_at    TIMESTAMPTZ    NOT NULL DEFAULT now(),
    
    -- Execution tracking
    attempt         SMALLINT       NOT NULL DEFAULT 0,
    max_attempts    SMALLINT       NOT NULL DEFAULT 3,
    locked_by       TEXT,                        -- worker_id
    locked_at       TIMESTAMPTZ,
    lease_expires   TIMESTAMPTZ,
    
    -- Lifecycle
    created_at      TIMESTAMPTZ    NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    failed_at       TIMESTAMPTZ,
    error_message   TEXT,
    
    CONSTRAINT pk_jobs PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
```

### Why These Columns

| Column | Purpose |
|--------|---------|
| `idempotency_key` | Client dedup — unique partial index on `(idempotency_key) WHERE idempotency_key IS NOT NULL` |
| `lease_expires` | Heartbeat-based lease — worker must renew before this timestamp |
| `locked_by` | Worker identification for debugging orphaned jobs |
| `status` | State machine: `pending → running → completed/failed/dead` |
| `queue_name` | Logical separation within a tenant (email, webhook, export) |
| `created_at` in PK | Required for range partitioning on `created_at` |

### Critical Indexes

```sql
-- The "fetch next job" index — this is the most important index in the system
CREATE INDEX idx_jobs_fetchable ON jobs_queue (
    queue_name, priority DESC, scheduled_at ASC
)
WHERE status = 'pending' AND scheduled_at <= now();

-- Lease expiration scan (reaper)
CREATE INDEX idx_jobs_lease_expired ON jobs_queue (lease_expires)
WHERE status = 'running';

-- Tenant query
CREATE INDEX idx_jobs_tenant_status ON jobs_queue (tenant_id, status, created_at DESC);

-- Idempotency enforcement
CREATE UNIQUE INDEX idx_jobs_idempotency ON jobs_queue (idempotency_key)
WHERE idempotency_key IS NOT NULL AND status IN ('pending', 'running');
```

---

## 4. PostgreSQL MVCC Deep Dive: Why Job Tables Bloat

### How MVCC Works

PostgreSQL does **not** update rows in place. Every `UPDATE` creates a **new tuple version** and marks the old one as dead. This is the foundation of MVCC (Multi-Version Concurrency Control).

```
┌────────────────────────────────────────────────────────────────────┐
│                    PostgreSQL Tuple Header                         │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  xmin = 100    ← Transaction ID that INSERTED this tuple           │
│  xmax = 0      ← 0 means "not deleted" (still live)               │
│  ctid = (0,1)  ← Physical location: page 0, slot 1                │
│  infomask      ← Hint bits (committed? aborted? frozen?)           │
│  t_data        ← Actual row data (payload)                         │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### Tuple Lifecycle in a Job Table

```
Step 1: INSERT (xid = 100)
  Tuple A: xmin=100, xmax=0, status='pending'

Step 2: UPDATE to 'running' (xid = 200)
  Tuple A: xmin=100, xmax=200, status='pending'     ← DEAD (invisible to new txns)
  Tuple B: xmin=200, xmax=0,   status='running'     ← LIVE

Step 3: UPDATE to 'completed' (xid = 300)
  Tuple B: xmin=200, xmax=300, status='running'     ← DEAD
  Tuple C: xmin=300, xmax=0,   status='completed'   ← LIVE

Result: 1 logical row → 3 physical tuples → 2 are dead → BLOAT
```

### Why This Is Catastrophic for Job Tables

A typical job goes through 2-4 status transitions:

```
pending → running → completed       (2 UPDATEs = 3 tuple versions, 2 dead)
pending → running → failed → pending → running → completed  (5 UPDATEs = 6 versions, 5 dead)
```

At 5,000 jobs/sec with an average of 3 updates per job:

```
Dead tuples generated: 5,000 × 2 = 10,000/sec = 864 million/day
Dead tuple size:       ~500 bytes × 864M = ~400 GB/day of dead data
```

This dead data sits in heap pages and index entries until VACUUM removes it.

### Visibility Check Cost

Every `SELECT` must check if each tuple is visible to the current transaction:

```
Is xmin committed?  → Check pg_xact (clog) or hint bits
Is xmax committed?  → Check pg_xact (clog) or hint bits
Is xmin < my snapshot's xmin?
Is xmax > my snapshot's xmin or in-progress?
```

When a heap page has 100 tuples but 90 are dead, the sequential scan still reads all 100 and evaluates visibility for each. This means a table with 1 million live rows and 10 million dead rows scans like it has 11 million rows.

### HOT Updates (Heap-Only Tuples)

PostgreSQL has an optimization for updates that don't change indexed columns. A HOT update stores the new tuple on the **same heap page** and chains them via `ctid`:

```
Page 0:
  Slot 1: xmin=100, xmax=200, ctid=(0,2), status='pending'    ← redirects to slot 2
  Slot 2: xmin=200, xmax=0,   ctid=(0,2), status='running'    ← current version
```

**HOT requirements:**
1. No indexed column changes
2. New tuple fits on the same page

**For job tables:** The `status` column is almost always indexed (for the fetchable index). This means `pending → running` **breaks HOT** because it changes a column in the partial index condition. This is one reason job tables bloat far worse than typical OLTP tables.

**Mitigation — Separate the hot path from indexes:**

```sql
-- Instead of indexing on status directly, use a boolean or separate column
ALTER TABLE jobs_queue ADD COLUMN is_fetchable BOOLEAN 
    GENERATED ALWAYS AS (status = 'pending' AND scheduled_at <= now()) STORED;

-- Index only the boolean
CREATE INDEX idx_jobs_ready ON jobs_queue (priority DESC, scheduled_at)
WHERE is_fetchable = true;
```

But generated columns re-evaluate on every update, which still breaks HOT. The real solution is structural: **move completed jobs out of the hot table entirely** (see Section 9).

---

## 5. VACUUM Mechanics & Operational Failures

### How VACUUM Works

```
┌─────────────────────────────────────────────────────────────────────┐
│                    VACUUM Pipeline                                   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  1. Scan Visibility Map                                              │
│     → Skip all-visible pages (bitmap: 1 bit per page)                │
│                                                                      │
│  2. Scan Heap Pages                                                  │
│     → Identify dead tuples (xmax committed, no snapshot sees them)   │
│     → Collect TIDs of dead tuples (limited by maintenance_work_mem)  │
│                                                                      │
│  3. Sort TIDs by index order                                         │
│                                                                      │
│  4. Index Vacuum Pass (for EACH index)                               │
│     → Walk entire index, remove entries pointing to dead TIDs        │
│     → This is O(index_size), NOT O(dead_tuples)                      │
│                                                                      │
│  5. Heap Vacuum Pass                                                 │
│     → Remove dead tuples from heap pages                             │
│     → Set pages as all-visible in visibility map                     │
│     → Potentially truncate trailing empty pages (if any)             │
│                                                                      │
│  6. Update pg_class (relpages, reltuples, relallvisible)             │
│     → Planner uses these for cost estimates                          │
│                                                                      │
│  7. Freeze old tuples (if near wraparound)                           │
│     → Rewrite xmin to FrozenTransactionId                            │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Why VACUUM Falls Behind on Job Tables

**Problem 1: Index Vacuum is O(index_size)**

If you have 5 indexes on a 100M-row table, and 1M dead tuples need cleaning, VACUUM must walk all 5 indexes (each potentially gigabytes) to find and remove dead pointers. The heap cleanup is fast; the index passes dominate.

```
Index vacuum cost:
  5 indexes × 10 GB each = 50 GB of index scans per VACUUM cycle
  At 500 MB/s SSD read speed = 100 seconds just for I/O
  Plus CPU for B-tree traversal = 2-5 minutes total
```

**Problem 2: maintenance_work_mem limits batch size**

VACUUM collects dead TIDs in memory, limited by `maintenance_work_mem` (default 64MB). Each TID is 6 bytes, so 64MB holds ~11 million TIDs. If the table has 50M dead tuples, VACUUM makes ~5 passes, and **each pass does a full index scan**:

```
5 passes × 5 indexes × 10 GB = 250 GB of index I/O per VACUUM
```

**Fix:** Set `maintenance_work_mem` to 1-2 GB for job table VACUUM operations.

**Problem 3: Long-running transactions block VACUUM**

VACUUM cannot remove a dead tuple if **any** open transaction might need to see it. A single long-running query (a reporting query, an analytics export, a forgotten `BEGIN` in pgAdmin) pins the `xmin horizon` and prevents ALL dead tuples newer than that transaction from being reclaimed.

```
Timeline:
  t=0:   BEGIN (txid 1000) — reporting query starts
  t=1s:  Job table processes 5,000 jobs (10,000 dead tuples)
  t=60s: Job table now has 600,000 dead tuples
         VACUUM runs — cannot remove ANY because txid 1000 is still open
  t=300s: 3 million dead tuples accumulated
          Table is 80% dead tuples
          Index scans degrade to sequential scans
          QUERY FINALLY FINISHES — but VACUUM is now 5 min behind
```

**Fix:** Monitor `pg_stat_activity` for long transactions. Set `idle_in_transaction_session_timeout`. Use read replicas for reporting queries.

```sql
-- Find the oldest transaction blocking VACUUM
SELECT pid, age(backend_xid) AS xid_age,
       now() - xact_start AS duration,
       query
FROM pg_stat_activity
WHERE backend_xid IS NOT NULL
ORDER BY xid_age DESC
LIMIT 5;
```

**Problem 4: Transaction ID Wraparound**

PostgreSQL uses 32-bit transaction IDs (4.2 billion values, but effectively ~2 billion usable due to modular comparison). At 10,000 transactions/sec:

```
2 billion / 10,000 = 200,000 seconds ≈ 2.3 days to wraparound
```

When approaching wraparound, PostgreSQL runs **aggressive anti-wraparound VACUUM**, which scans **every page** (ignoring the visibility map) and can block writes for hours. At extreme risk, PostgreSQL **shuts down entirely** and refuses to process transactions.

**Fix:**

```sql
-- Monitor wraparound distance
SELECT datname,
       age(datfrozenxid) AS xid_age,
       2147483647 - age(datfrozenxid) AS remaining
FROM pg_database
ORDER BY xid_age DESC;

-- Alert when remaining < 500 million
```

### Autovacuum Tuning for Job Tables

```sql
-- Per-table autovacuum settings — critical for job tables
ALTER TABLE jobs_queue SET (
    autovacuum_vacuum_threshold = 1000,           -- trigger after 1K dead tuples (default 50)
    autovacuum_vacuum_scale_factor = 0.01,         -- trigger at 1% dead (default 20%)
    autovacuum_vacuum_cost_delay = 2,              -- less throttling (default 20ms)
    autovacuum_vacuum_cost_limit = 1000,           -- more aggressive (default 200)
    autovacuum_analyze_threshold = 5000,
    autovacuum_analyze_scale_factor = 0.02,
    fillfactor = 70                                -- leave room for HOT updates
);
```

**Why `fillfactor = 70`:** Leave 30% free space per page so HOT updates (when possible) can store the new tuple version on the same page. This trades storage for reduced bloat.

---

## 6. The Ordered Scan Problem

### The Core Query

Every job scheduler based on PostgreSQL uses some variant of:

```sql
SELECT id, payload, tenant_id
FROM jobs_queue
WHERE status = 'pending'
  AND scheduled_at <= now()
  AND queue_name = 'email'
ORDER BY priority DESC, scheduled_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

This looks innocent. At scale, it becomes the system's bottleneck.

### Why It Degrades

**Phase 1: Clean table (works perfectly)**

```
Index idx_jobs_fetchable has 1,000 entries
→ B-tree scan finds first matching row instantly
→ Locks it with FOR UPDATE
→ SKIP LOCKED is a no-op (no contention)
→ Execution: < 1ms
```

**Phase 2: Under load (starts degrading)**

```
10 workers query simultaneously
→ Worker 1 locks row at position 1
→ Worker 2 hits locked row → SKIP LOCKED → advance to position 2
→ Worker 3 → position 3
→ ...
→ Worker 10 → position 10
→ Still fast, but doing 10× the index traversal
```

**Phase 3: Dead tuple accumulation (severe degradation)**

```
Index has 1 million entries, but 900K point to dead tuples
→ Index scan returns TID → heap fetch → tuple is dead → skip → next TID
→ Repeat 900K times to find 1 live row
→ "Index scan" becomes worse than sequential scan
→ Execution: 500ms - 5 seconds
```

This is the **index bloat death spiral**: dead tuples in the heap cause index entries to point to invisible rows. The index itself isn't wrong — it correctly points to tuples that exist — but those tuples fail the visibility check. VACUUM cleans both, but until it does, every index scan pays the cost.

**Phase 4: Planner abandons index (catastrophic)**

```
pg_class.reltuples = 10,000,000 (stale from last ANALYZE)
Actual live rows = 100,000
Planner estimates: "Seq scan 10M rows vs index scan + 10M heap fetches"
→ Planner chooses seq scan (correctly, based on stale stats)
→ SELECT with LIMIT 1 now scans millions of rows
→ Execution: 10+ seconds
→ Workers time out → jobs pile up → more bloat → feedback loop
```

### Solutions to the Ordered Scan Problem

**Solution 1: Separate Ready Queue Table**

Instead of querying the main table, maintain a lightweight "ready queue":

```sql
CREATE UNLOGGED TABLE jobs_ready (
    job_id      BIGINT PRIMARY KEY,
    queue_name  TEXT     NOT NULL,
    priority    SMALLINT NOT NULL DEFAULT 0,
    scheduled_at TIMESTAMPTZ NOT NULL,
    tenant_id   INTEGER  NOT NULL
);

CREATE INDEX idx_ready_fetch ON jobs_ready (queue_name, priority DESC, scheduled_at ASC);
```

```
Enqueue flow:
  1. INSERT into jobs_queue (permanent, WAL-logged)
  2. INSERT into jobs_ready (lightweight, UNLOGGED)

Claim flow:
  1. DELETE FROM jobs_ready WHERE id = (
       SELECT job_id FROM jobs_ready
       WHERE queue_name = 'email'
       ORDER BY priority DESC, scheduled_at
       LIMIT 1
       FOR UPDATE SKIP LOCKED
     ) RETURNING job_id;
  2. UPDATE jobs_queue SET status = 'running' WHERE id = returned_job_id;
```

**Why this works:**
- `jobs_ready` only contains pending jobs — no dead tuples from status transitions
- `DELETE` removes the row entirely (no bloat cycle)
- `UNLOGGED` means no WAL overhead (we can rebuild from `jobs_queue` on crash)
- The table stays tiny — only pending jobs exist in it

**Trade-off:** On crash, `UNLOGGED` tables are truncated. Recovery process must scan `jobs_queue` for `status = 'pending'` jobs and re-populate `jobs_ready`. This adds recovery time but is acceptable since the source of truth (`jobs_queue`) is WAL-logged.

**Solution 2: Advisory Locks Instead of FOR UPDATE**

```sql
-- Worker tries to claim a specific job using advisory lock
SELECT id, payload FROM jobs_queue
WHERE status = 'pending'
  AND scheduled_at <= now()
  AND queue_name = 'email'
  AND pg_try_advisory_xact_lock(id)  -- non-blocking lock attempt
ORDER BY priority DESC, scheduled_at
LIMIT 1;
```

**Advantages:**
- `pg_try_advisory_xact_lock` doesn't create row-level lock entries
- Failed lock attempts are instant (no blocking, no SKIP LOCKED scan)
- Advisory locks live in shared memory, not on the heap

**Disadvantages:**
- Advisory locks are database-wide, not table-scoped — ID collisions across tables
- Lock namespace management adds complexity
- Must ensure lock is released (use `pg_try_advisory_xact_lock` tied to transaction)

**Solution 3: Hash-Based Worker Assignment**

Eliminate contention by pre-assigning job ranges to workers:

```sql
-- Each worker has a stable ID (0 to N-1)
-- Jobs are assigned to workers via hash

-- Worker 3 of 10 only claims jobs assigned to it:
SELECT id, payload FROM jobs_queue
WHERE status = 'pending'
  AND scheduled_at <= now()
  AND queue_name = 'email'
  AND (id % 10) = 3            -- hash assignment
ORDER BY priority DESC, scheduled_at
LIMIT 5
FOR UPDATE SKIP LOCKED;
```

**Advantages:**
- Zero contention — each worker queries a disjoint set
- `FOR UPDATE SKIP LOCKED` almost never skips (only races with the reaper)
- Can create partial indexes per hash bucket for even faster scans

**Disadvantages:**
- Uneven load distribution (some hash buckets have more jobs)
- Adding/removing workers requires rehashing (similar to consistent hashing)
- Priority ordering is only within a worker's hash bucket

**Solution 4: Batch Claim**

Instead of claiming 1 job at a time (10 workers × 10 queries/sec = 100 queries/sec), claim in batches:

```sql
UPDATE jobs_queue
SET status = 'running',
    locked_by = 'worker-3',
    locked_at = now(),
    lease_expires = now() + interval '5 minutes'
WHERE id IN (
    SELECT id FROM jobs_queue
    WHERE status = 'pending'
      AND scheduled_at <= now()
      AND queue_name = 'email'
    ORDER BY priority DESC, scheduled_at
    LIMIT 20                       -- claim 20 jobs at once
    FOR UPDATE SKIP LOCKED
)
RETURNING id, payload;
```

**Impact:**
- 10 workers claiming 20 jobs each = 200 claims in 10 queries (instead of 200 queries)
- Reduces contention by 20× on the index scan
- Worker processes batch locally (no DB round-trips per job)

---

## 7. LISTEN/NOTIFY: Promise and Pitfalls

### How LISTEN/NOTIFY Works Internally

```
┌─────────────────────────────────────────────────────────────────────┐
│                    LISTEN/NOTIFY Internals                           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  Producer:                                                           │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ NOTIFY jobs_ready, '{"queue":"email","tenant":42}'           │    │
│  │                                                              │    │
│  │ 1. Message written to shared memory ring buffer              │    │
│  │    (async_queue in shared memory, 8 KB default)              │    │
│  │ 2. Signal sent to all backends listening on 'jobs_ready'     │    │
│  │ 3. Message delivered when listener's transaction COMMITs     │    │
│  │    (NOTIFY inside a transaction is deferred)                 │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                      │
│  Consumer:                                                           │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ LISTEN jobs_ready;                                           │    │
│  │                                                              │    │
│  │ 1. Backend registered in shared notification list            │    │
│  │ 2. On signal: copy message from ring buffer to connection    │    │
│  │ 3. Client reads notification via socket                      │    │
│  │ 4. Notification removed from queue when ALL listeners have   │    │
│  │    consumed it (or slow consumer's queue is full → dropped)  │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Failure Mode 1: Not Persistent

LISTEN/NOTIFY lives entirely in shared memory. It has **no WAL, no disk persistence, no replay**.

```
Scenario:
  t=0: Worker connects and LISTENs on 'jobs_ready'
  t=1: PostgreSQL crashes and restarts
  t=2: 500 jobs were inserted between t=0 and t=1
  t=3: Worker reconnects, LISTENs again
  → Those 500 notifications are GONE — worker never sees them
```

**Fix:** LISTEN/NOTIFY is an optimization hint, never the source of truth. Always combine with polling:

```python
class HybridJobConsumer:
    def __init__(self, dsn, poll_interval=5.0):
        self.listen_conn = psycopg.connect(dsn, autocommit=True)
        self.work_conn = psycopg.connect(dsn)
        self.poll_interval = poll_interval

    def run(self):
        self.listen_conn.execute("LISTEN jobs_ready")
        
        while True:
            # Try NOTIFY-driven wake first
            if self.listen_conn.notifies(timeout=self.poll_interval):
                # Drain all pending notifications
                for notify in self.listen_conn.notifies(timeout=0):
                    pass  # consume but don't act on payload
            
            # Always poll regardless — NOTIFY is just a faster wakeup
            self.claim_and_process_jobs()
    
    def claim_and_process_jobs(self):
        with self.work_conn.transaction():
            rows = self.work_conn.execute("""
                UPDATE jobs_queue
                SET status = 'running', locked_by = %s,
                    lease_expires = now() + interval '5 minutes'
                WHERE id IN (
                    SELECT id FROM jobs_queue
                    WHERE status = 'pending' AND scheduled_at <= now()
                    ORDER BY priority DESC, scheduled_at
                    LIMIT 10
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, payload
            """, [self.worker_id]).fetchall()
        
        for job_id, payload in rows:
            self.execute_job(job_id, payload)
```

**The pattern:** NOTIFY reduces latency (instant wakeup instead of 5-second poll). Polling guarantees correctness (no missed jobs). The combination gives you both.

### Failure Mode 2: Queue Overflow

The NOTIFY queue in shared memory is bounded. When a slow consumer can't keep up:

```
WARNING: too many notifications in the NOTIFY queue
```

At overflow, PostgreSQL starts **dropping notifications for that listener**. There is no backpressure or retry — they're simply gone.

**Fix:** The hybrid approach above handles this implicitly. The polling fallback picks up anything missed.

### Failure Mode 3: Connection Loss

LISTEN is tied to a specific connection. If the connection drops:

```
Scenario:
  PgBouncer in transaction mode → LISTEN doesn't work at all
      (LISTEN requires session-level state, which transaction-mode poolers discard)
  
  Direct connection drops → must re-LISTEN after reconnect
  
  Between drop and reconnect → notifications are lost
```

**Fix:** 
- Use a **dedicated connection** for LISTEN (not from a pool)
- Use PgBouncer in **session mode** for LISTEN connections only
- Monitor connection health and reconnect immediately

### Failure Mode 4: Thundering Herd

NOTIFY is broadcast to **all** listeners on a channel. With 100 workers all listening on `jobs_ready`:

```
1 job is inserted → NOTIFY fires → 100 workers wake up simultaneously
→ 100 concurrent SELECT FOR UPDATE SKIP LOCKED
→ 1 worker gets the job, 99 did useless work
→ 99 wasted database round-trips + lock contention
```

**Mitigations:**

```python
# 1. Per-worker channels (fan-out from enqueue service)
NOTIFY jobs_ready_worker_3, '{"job_id": 12345}'

# 2. Jittered wakeup
import random
await asyncio.sleep(random.uniform(0, 0.1))  # 0-100ms jitter before claiming

# 3. Batch NOTIFY (one notification per batch, not per job)
# Accumulate for 50ms, then send one NOTIFY with count
NOTIFY jobs_ready, '{"count": 47, "queue": "email"}'
```

---

## 8. Heartbeat, Lease & Zombie Workers

### The Problem

When a worker claims a job, it must eventually complete it. But workers can:
- Crash mid-execution (OOM kill, segfault, hardware failure)
- Hang (infinite loop, deadlock, blocked I/O)
- Lose network connectivity to the database
- Experience GC pauses (JVM stop-the-world, Python GC)

Without a mechanism to detect failure, the job stays in `running` forever — a **stuck job**.

### Approach 1: Fixed Lease (Timeout-Based)

```sql
-- Worker claims with a lease
UPDATE jobs_queue
SET status = 'running',
    locked_by = 'worker-3',
    lease_expires = now() + interval '5 minutes'
WHERE id = 12345;

-- Reaper query (runs every 30 seconds)
UPDATE jobs_queue
SET status = 'pending',
    locked_by = NULL,
    lease_expires = NULL,
    attempt = attempt + 1
WHERE status = 'running'
  AND lease_expires < now()
  AND attempt < max_attempts
RETURNING id;
```

**The Lease Problem: Byzantine Faults**

```
Timeline:
  t=0:     Worker-3 claims job-100, lease_expires = t+300s
  t=290s:  Worker-3 is mid-execution (HTTP call to external API)
  t=300s:  Lease expires — Reaper requeues job-100
  t=301s:  Worker-5 claims job-100, starts executing
  t=305s:  Worker-3's HTTP call returns — it believes it still owns job-100
  t=306s:  Worker-3 UPDATEs job-100 to 'completed'
           Worker-5 is also executing job-100
           → DOUBLE EXECUTION
```

**This is Martin Kleppmann's fencing token problem.** A lease gives no guarantee after expiration — the holder doesn't know the lease expired.

### Approach 2: Heartbeat + Lease (Renewable)

```sql
-- Worker renews lease every 30 seconds
UPDATE jobs_queue
SET lease_expires = now() + interval '5 minutes'
WHERE id = 12345
  AND locked_by = 'worker-3'
  AND status = 'running';
```

**Better, but still vulnerable:**

```
Timeline:
  t=0:     Worker-3 claims job-100, starts heartbeat thread
  t=60s:   Worker-3 sends heartbeat (lease_expires = t+300s)
  t=65s:   Worker-3 hits full GC pause (20 seconds for JVM)
  t=85s:   GC ends, heartbeat thread resumes — lease is still valid
           BUT: all in-flight work may be inconsistent (half-written state)

Worse:
  t=60s:   Worker-3 sends heartbeat
  t=61s:   Network partition — worker can't reach DB
  t=361s:  Lease expires — reaper requeues
  t=362s:  Network heals — worker-3 thinks it still owns the job
```

### Approach 3: Fencing Tokens

The proper solution from distributed systems literature:

```sql
-- Add a monotonic token column
ALTER TABLE jobs_queue ADD COLUMN fence_token BIGINT;

-- Claim with fencing token
UPDATE jobs_queue
SET status = 'running',
    locked_by = 'worker-3',
    lease_expires = now() + interval '5 minutes',
    fence_token = nextval('fence_token_seq')
WHERE id = 12345
RETURNING fence_token;  -- returns e.g. 98765
```

```python
class FencedWorker:
    def execute_job(self, job_id, fence_token, payload):
        result = do_work(payload)
        
        # Complete ONLY if our fence token is still current
        rows = self.db.execute("""
            UPDATE jobs_queue
            SET status = 'completed', completed_at = now()
            WHERE id = %s AND fence_token = %s AND status = 'running'
        """, [job_id, fence_token])
        
        if rows.rowcount == 0:
            # Someone else reclaimed this job — discard our result
            log.warn(f"Job {job_id} was reclaimed, discarding result")
            self.rollback_side_effects(payload, result)
```

**The fence token ensures:** if the reaper reclaims the job and another worker gets it with a new (higher) fence token, the original worker's completion UPDATE is a no-op because the fence_token doesn't match.

### Approach 4: Database Connection = Liveness

Use PostgreSQL advisory locks tied to the session:

```sql
-- Worker claims using session-level advisory lock
SELECT pg_advisory_lock(job_id);

-- If the worker's connection drops, PostgreSQL automatically releases the lock
-- Reaper doesn't need to check leases — just check if advisory lock is held:
SELECT NOT pg_try_advisory_lock(job_id) AS is_locked;
```

**Advantage:** No heartbeat, no lease, no race condition. Connection death = lock release = instant detection.

**Disadvantage:** Advisory locks consume shared memory. 10,000 concurrent locks per worker × 100 workers = 1 million advisory locks — this can exhaust `max_locks_per_transaction`.

### Recommended Approach: Heartbeat + Fence Token + Idempotent Execution

```
┌──────────────────────────────────────────────────────────────────┐
│                   Recommended Lease Strategy                      │
├──────────────────────────────────────────────────────────────────┤
│                                                                   │
│  1. Claim: UPDATE with lease_expires + fence_token                │
│  2. Heartbeat: Renew lease every 30s (lease = 5 min)              │
│     → Heartbeat thread is independent of worker thread            │
│     → If heartbeat fails 3× consecutive → worker self-terminates  │
│  3. Complete: UPDATE WHERE fence_token = X (CAS operation)        │
│  4. Reaper: Runs every 30s, requeues jobs with expired leases     │
│  5. Side effects: Must be idempotent (use job_id as idempotency   │
│     key for external calls)                                       │
│                                                                   │
│  Lease duration = 5 × heartbeat interval                          │
│  (Survives 4 missed heartbeats before reaper acts)                │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

---

## 9. Partitioning: DROP PARTITION vs DELETE

### The Problem With DELETE

When you delete completed jobs to reclaim space:

```sql
DELETE FROM jobs_queue WHERE status = 'completed' AND completed_at < now() - interval '7 days';
```

**What actually happens:**

```
1. Each DELETE marks tuples with xmax (creates dead tuples — does NOT free space)
2. Deleting 10 million rows = 10 million dead tuples
3. All indexes now have 10 million dangling entries
4. VACUUM must clean up: O(index_size) per index
5. Even after VACUUM, the file on disk doesn't shrink (pages are reused, not returned to OS)
6. To actually shrink: VACUUM FULL (rewrites entire table, takes exclusive lock)
```

For a job table doing 5,000 completions/sec:

```
Daily completed rows:   ~400 million
Weekly DELETE:          400M × 7 = 2.8 billion rows to delete
VACUUM after DELETE:    Hours of I/O (multiple index passes)
Space reclaimed:        0 bytes (without VACUUM FULL)
```

### DROP PARTITION: The Solution

```sql
-- Create partitioned table by day
CREATE TABLE jobs_queue (
    id          BIGINT GENERATED ALWAYS AS IDENTITY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT        NOT NULL DEFAULT 'pending',
    -- ... other columns ...
    CONSTRAINT pk_jobs PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- Create daily partitions
CREATE TABLE jobs_queue_2025_01_15 PARTITION OF jobs_queue
    FOR VALUES FROM ('2025-01-15') TO ('2025-01-16');

CREATE TABLE jobs_queue_2025_01_16 PARTITION OF jobs_queue
    FOR VALUES FROM ('2025-01-16') TO ('2025-01-17');
```

**Dropping a partition:**

```sql
-- Move old partition to archive (instant metadata operation)
ALTER TABLE jobs_queue DETACH PARTITION jobs_queue_2025_01_08;

-- Optionally archive to a separate table
ALTER TABLE jobs_queue_2025_01_08 RENAME TO jobs_archive_2025_01_08;

-- Or drop entirely (instant, no VACUUM needed)
DROP TABLE jobs_queue_2025_01_08;
```

### DROP vs DELETE Performance Comparison

| Operation | DELETE 10M rows | DROP PARTITION |
|-----------|----------------|----------------|
| Execution time | 10-30 minutes | < 1 second |
| WAL generated | ~10 GB | ~0 bytes |
| Dead tuples created | 10 million | 0 |
| VACUUM work needed | Hours | None |
| Index cleanup | O(index_size) × N indexes | None |
| Space reclaimed immediately | No (need VACUUM FULL) | Yes (files deleted) |
| Lock required | Row-level locks during DELETE | ACCESS EXCLUSIVE briefly |
| Replication lag | Significant | Minimal |

### DETACH PARTITION CONCURRENTLY

PostgreSQL 14+ supports non-blocking detach:

```sql
-- Does not block reads/writes on the parent table
ALTER TABLE jobs_queue DETACH PARTITION jobs_queue_2025_01_08 CONCURRENTLY;
```

This runs in two transactions internally:
1. Marks partition as "being detached" (brief lock)
2. Waits for all queries using the partition to finish
3. Removes partition from the parent (brief lock)

**Caveat:** If the session is interrupted mid-detach, the partition is stuck in a half-detached state. Fix with `ALTER TABLE ... DETACH PARTITION ... FINALIZE`.

### Partition Maintenance Automation

```sql
-- Function to create future partitions
CREATE OR REPLACE FUNCTION create_daily_partition(target_date DATE)
RETURNS VOID AS $$
DECLARE
    partition_name TEXT;
    start_date DATE;
    end_date DATE;
BEGIN
    partition_name := 'jobs_queue_' || to_char(target_date, 'YYYY_MM_DD');
    start_date := target_date;
    end_date := target_date + 1;
    
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF jobs_queue
         FOR VALUES FROM (%L) TO (%L)',
        partition_name, start_date, end_date
    );
END;
$$ LANGUAGE plpgsql;

-- Create partitions 7 days ahead (run daily via cron or pg_cron)
SELECT create_daily_partition(current_date + i)
FROM generate_series(0, 7) AS i;

-- Drop partitions older than retention
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN 
        SELECT inhrelid::regclass AS partition_name
        FROM pg_inherits
        JOIN pg_class ON pg_class.oid = inhrelid
        WHERE inhparent = 'jobs_queue'::regclass
          AND pg_class.relname < 'jobs_queue_' || to_char(current_date - 7, 'YYYY_MM_DD')
    LOOP
        EXECUTE 'ALTER TABLE jobs_queue DETACH PARTITION ' || r.partition_name;
        EXECUTE 'DROP TABLE ' || r.partition_name;
    END LOOP;
END $$;
```

---

## 10. Table Strategy: Single vs Per-Tenant vs Partitioned

### Option 1: Single Table (All Tenants, No Partitions)

```sql
CREATE TABLE jobs_queue (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id   INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    -- ...
);
CREATE INDEX idx_jobs_fetch ON jobs_queue (status, priority DESC, scheduled_at)
WHERE status = 'pending';
```

| Pros | Cons |
|------|------|
| Simplest schema | Single VACUUM bottleneck |
| Easy to query across tenants | One noisy tenant bloats entire table |
| Single connection pool | No per-tenant DROP PARTITION |
| Works at low scale (<1K jobs/sec) | Index bloat affects all tenants |
| | DELETE-based cleanup only |

**When to use:** Prototypes, low-scale systems, < 100 tenants with similar workloads.

### Option 2: Table Per Tenant

```sql
-- Dynamically create per-tenant tables
CREATE TABLE jobs_queue_tenant_42 (LIKE jobs_queue INCLUDING ALL);
CREATE TABLE jobs_queue_tenant_43 (LIKE jobs_queue INCLUDING ALL);
-- ... for each tenant
```

| Pros | Cons |
|------|------|
| Perfect isolation | Schema management nightmare at 1,000+ tenants |
| Per-tenant VACUUM | 1,000 tables × 5 indexes = 5,000 objects for planner |
| No noisy neighbor | Cannot query across tenants easily |
| Independent DROP TABLE | Connection pool per tenant (or dynamic routing) |
| Tenant-specific tuning | Autovacuum workers spread thin (default 3 workers) |

**The Autovacuum Problem:**

```
Default: autovacuum_max_workers = 3
With 1,000 tenant tables, each needing VACUUM:
→ 3 workers serving 1,000 tables = each table vacuumed every ~333 cycles
→ If each VACUUM takes 5 seconds: 1,000 × 5 = 5,000s between vacuums per table
→ Dead tuples accumulate for ~80 minutes between cleanups
```

**When to use:** Strong regulatory isolation requirements, very different workloads per tenant, < 50 tenants.

### Option 3: Partitioned Table (Recommended)

```sql
-- Time-based partitioning (best for job tables)
CREATE TABLE jobs_queue (
    id          BIGINT GENERATED ALWAYS AS IDENTITY,
    tenant_id   INTEGER     NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT        NOT NULL DEFAULT 'pending',
    -- ...
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- Daily partitions
CREATE TABLE jobs_queue_20250115 PARTITION OF jobs_queue
    FOR VALUES FROM ('2025-01-15') TO ('2025-01-16');
```

**Sub-partitioning by tenant (for large tenants):**

```sql
-- Two-level: time → tenant hash
CREATE TABLE jobs_queue (
    id          BIGINT GENERATED ALWAYS AS IDENTITY,
    tenant_id   INTEGER     NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at, tenant_id)
) PARTITION BY RANGE (created_at);

CREATE TABLE jobs_queue_20250115 PARTITION OF jobs_queue
    FOR VALUES FROM ('2025-01-15') TO ('2025-01-16')
    PARTITION BY HASH (tenant_id);

CREATE TABLE jobs_queue_20250115_p0 PARTITION OF jobs_queue_20250115
    FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE jobs_queue_20250115_p1 PARTITION OF jobs_queue_20250115
    FOR VALUES WITH (MODULUS 4, REMAINDER 1);
-- ...
```

| Pros | Cons |
|------|------|
| DROP PARTITION for cleanup | Partition key must be in PK |
| VACUUM scoped to partition | Cross-partition queries need merge |
| Partition pruning in queries | Cannot have global unique index (except on PK) |
| Balanced autovacuum load | More complex partition management |
| Tenant sub-partitioning optional | Query plans more complex |

### Option 4: Hot/Cold Table Split (Pragmatic Recommendation)

```
┌──────────────────────────────────────────────────────────────────────┐
│              Hot/Cold Architecture (Recommended)                      │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  jobs_active (UNPARTITIONED)                                          │
│  ├── Only pending + running jobs                                      │
│  ├── Small table (thousands to low millions of rows)                  │
│  ├── Hot indexes stay in memory                                       │
│  ├── VACUUM is fast (small table)                                     │
│  └── Ordered scan problem is minimized                                │
│                                                                       │
│  jobs_completed (PARTITIONED BY RANGE on completed_at)                │
│  ├── Write-once (INSERT only, no UPDATEs)                             │
│  ├── DROP PARTITION for retention                                     │
│  ├── Append-only = no bloat                                           │
│  └── Query for history/analytics only                                 │
│                                                                       │
│  Flow:                                                                │
│  1. INSERT into jobs_active (status = 'pending')                      │
│  2. UPDATE status to 'running' (in jobs_active)                       │
│  3. On completion: DELETE from jobs_active,                            │
│                    INSERT into jobs_completed                          │
│  4. DROP old jobs_completed partitions for retention                   │
│                                                                       │
└──────────────────────────────────────────────────────────────────────┘
```

```sql
-- Hot table: only active jobs
CREATE TABLE jobs_active (
    id              BIGINT PRIMARY KEY,
    tenant_id       INTEGER     NOT NULL,
    queue_name      TEXT        NOT NULL DEFAULT 'default',
    priority        SMALLINT    NOT NULL DEFAULT 0,
    status          TEXT        NOT NULL DEFAULT 'pending',
    payload         JSONB       NOT NULL,
    scheduled_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempt         SMALLINT    NOT NULL DEFAULT 0,
    max_attempts    SMALLINT    NOT NULL DEFAULT 3,
    locked_by       TEXT,
    lease_expires   TIMESTAMPTZ,
    fence_token     BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cold table: completed/failed jobs (append-only, partitioned)
CREATE TABLE jobs_completed (
    id              BIGINT      NOT NULL,
    tenant_id       INTEGER     NOT NULL,
    queue_name      TEXT        NOT NULL,
    status          TEXT        NOT NULL,   -- 'completed', 'failed', 'dead'
    payload         JSONB       NOT NULL,
    result          JSONB,
    attempt         SMALLINT    NOT NULL,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    completed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, completed_at)
) PARTITION BY RANGE (completed_at);
```

**Why this is the best approach:**

1. `jobs_active` stays small — at 5,000 jobs/sec with 10s avg execution time, it holds ~50,000 rows. Indexes fit in RAM. VACUUM finishes in milliseconds.
2. `jobs_completed` is append-only — no UPDATEs, no dead tuples, no bloat. Partitioned by time for instant cleanup.
3. The ordered scan problem is eliminated — scanning 50K live rows with a btree index is trivial.

### Partition Pruning and the Ordered Scan

When a time-partitioned table receives an `ORDER BY priority, scheduled_at LIMIT 1` query, PostgreSQL must decide which partitions to scan:

```sql
-- Without a time filter, PostgreSQL must check ALL partitions:
SELECT * FROM jobs_queue
WHERE status = 'pending'
ORDER BY priority DESC, scheduled_at
LIMIT 1;

-- Execution plan (BAD):
Limit
  → Merge Append
    → Sort (jobs_queue_20250115)
    → Sort (jobs_queue_20250116)
    → Sort (jobs_queue_20250117)
    ... (one sort per partition, then merge)
```

Each partition is sorted independently, then merged. With 30 daily partitions, that's 30 parallel sort operations that are merged. The planner may estimate this as more expensive than a sequential scan.

**Fix — Add the partition key to the query:**

```sql
SELECT * FROM jobs_queue
WHERE status = 'pending'
  AND created_at >= now() - interval '1 day'   -- partition pruning hint
ORDER BY priority DESC, scheduled_at
LIMIT 1;
```

Now PostgreSQL prunes to 1-2 partitions. But this requires knowing that pending jobs are always recent — a scheduled job created a week ago for today would be missed.

**This is why Hot/Cold split wins:** `jobs_active` is unpartitioned, so partition pruning doesn't apply. The table is small enough that any query plan is fast.

---

## 11. Multi-Tenancy & Noisy Neighbor Prevention

### The Noisy Neighbor Problem

```
Tenant A: submits 100 jobs/sec (normal)
Tenant B: submits 50,000 jobs/sec (burst — marketing campaign)

Without isolation:
  - Tenant B's jobs fill the queue
  - Workers spend 99% of time on Tenant B's jobs
  - Tenant A's jobs starve (hours of delay)
  - Tenant B's burst causes table bloat affecting everyone
```

### Strategy 1: Weighted Fair Queuing

```sql
-- Workers round-robin across tenants using weighted selection
WITH tenant_weights AS (
    SELECT tenant_id,
           quota_weight,                -- configured per tenant (1-100)
           COUNT(*) FILTER (WHERE status = 'running') AS running_jobs
    FROM jobs_active
    JOIN tenant_config USING (tenant_id)
    GROUP BY tenant_id, quota_weight
),
selected_tenant AS (
    SELECT tenant_id FROM tenant_weights
    WHERE running_jobs < max_concurrent_jobs   -- per-tenant concurrency limit
    ORDER BY (running_jobs::float / NULLIF(quota_weight, 0))  -- least-served tenant first
    LIMIT 1
)
SELECT j.id, j.payload
FROM jobs_active j
JOIN selected_tenant st USING (tenant_id)
WHERE j.status = 'pending' AND j.scheduled_at <= now()
ORDER BY j.priority DESC, j.scheduled_at
LIMIT 10
FOR UPDATE SKIP LOCKED;
```

### Strategy 2: Per-Tenant Concurrency Limits

```sql
CREATE TABLE tenant_config (
    tenant_id              INTEGER PRIMARY KEY,
    max_concurrent_jobs    INTEGER NOT NULL DEFAULT 100,
    max_enqueue_per_minute INTEGER NOT NULL DEFAULT 1000,
    quota_weight           INTEGER NOT NULL DEFAULT 10,
    priority_boost         SMALLINT NOT NULL DEFAULT 0
);

-- Enforce at claim time
WITH running_count AS (
    SELECT COUNT(*) AS cnt
    FROM jobs_active
    WHERE tenant_id = 42 AND status = 'running'
)
UPDATE jobs_active
SET status = 'running', locked_by = 'worker-3'
WHERE id IN (
    SELECT id FROM jobs_active
    WHERE tenant_id = 42
      AND status = 'pending'
      AND (SELECT cnt FROM running_count) < (
          SELECT max_concurrent_jobs FROM tenant_config WHERE tenant_id = 42
      )
    ORDER BY priority DESC, scheduled_at
    LIMIT 5
    FOR UPDATE SKIP LOCKED
)
RETURNING id, payload;
```

### Strategy 3: Logical Queue Separation

Instead of one query across all tenants, workers cycle through tenant queues:

```python
class FairSchedulerWorker:
    def __init__(self):
        self.tenant_cursor = 0
    
    def claim_batch(self):
        # Get active tenants with pending work
        tenants = db.execute("""
            SELECT DISTINCT tenant_id FROM jobs_active
            WHERE status = 'pending' AND scheduled_at <= now()
            ORDER BY tenant_id
        """).fetchall()
        
        if not tenants:
            return []
        
        # Round-robin: pick next tenant
        self.tenant_cursor = (self.tenant_cursor + 1) % len(tenants)
        tenant_id = tenants[self.tenant_cursor]
        
        # Claim from that tenant only
        return db.execute("""
            UPDATE jobs_active
            SET status = 'running', locked_by = %s,
                lease_expires = now() + interval '5 minutes'
            WHERE id IN (
                SELECT id FROM jobs_active
                WHERE tenant_id = %s AND status = 'pending' AND scheduled_at <= now()
                ORDER BY priority DESC, scheduled_at
                LIMIT 10
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, payload
        """, [self.worker_id, tenant_id]).fetchall()
```

### Strategy 4: Rate Limiting at Enqueue

Prevent the burst from entering the database at all:

```sql
-- Sliding window rate limit using a helper table
CREATE TABLE tenant_rate_limit (
    tenant_id   INTEGER NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, window_start)
);

-- At enqueue time (in application code):
WITH rate_check AS (
    INSERT INTO tenant_rate_limit (tenant_id, window_start, count)
    VALUES (42, date_trunc('minute', now()), 1)
    ON CONFLICT (tenant_id, window_start)
    DO UPDATE SET count = tenant_rate_limit.count + 1
    RETURNING count
)
INSERT INTO jobs_active (tenant_id, payload, queue_name)
SELECT 42, '{"type":"email"}', 'default'
WHERE (SELECT count FROM rate_check) <= (
    SELECT max_enqueue_per_minute FROM tenant_config WHERE tenant_id = 42
);
-- Returns 0 rows if rate limit exceeded
```

---

## 12. Production Architecture

### Complete Claim Flow (Pseudocode)

```python
class ProductionWorker:
    def __init__(self, worker_id: str, dsn: str):
        self.worker_id = worker_id
        self.work_conn = connect(dsn)
        self.listen_conn = connect(dsn, autocommit=True)
        self.listen_conn.execute("LISTEN jobs_ready")
        self.heartbeat_interval = 30   # seconds
        self.lease_duration = 300      # seconds (5 min)
        self.batch_size = 10
    
    def run_loop(self):
        while True:
            # Hybrid wake: NOTIFY or poll timeout
            notified = self.listen_conn.notifies(timeout=5.0)
            
            jobs = self.claim_batch()
            if not jobs:
                continue
            
            for job_id, fence_token, payload in jobs:
                # Each job gets its own heartbeat tracker
                self.execute_with_heartbeat(job_id, fence_token, payload)
    
    def claim_batch(self) -> list:
        with self.work_conn.transaction():
            return self.work_conn.execute("""
                UPDATE jobs_active
                SET status = 'running',
                    locked_by = %(worker)s,
                    locked_at = now(),
                    lease_expires = now() + make_interval(secs := %(lease)s),
                    fence_token = nextval('fence_token_seq'),
                    started_at = now(),
                    attempt = attempt + 1
                WHERE id IN (
                    SELECT id FROM jobs_active
                    WHERE status = 'pending'
                      AND scheduled_at <= now()
                    ORDER BY priority DESC, scheduled_at
                    LIMIT %(batch)s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, fence_token, payload
            """, {
                "worker": self.worker_id,
                "lease": self.lease_duration,
                "batch": self.batch_size
            }).fetchall()
    
    def execute_with_heartbeat(self, job_id, fence_token, payload):
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, fence_token, heartbeat_stop)
        )
        heartbeat_thread.start()
        
        try:
            result = self.do_work(payload)
            self.complete_job(job_id, fence_token, result)
        except Exception as e:
            self.fail_job(job_id, fence_token, str(e))
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join()
    
    def _heartbeat_loop(self, job_id, fence_token, stop_event):
        consecutive_failures = 0
        while not stop_event.wait(timeout=self.heartbeat_interval):
            try:
                rows = self.work_conn.execute("""
                    UPDATE jobs_active
                    SET lease_expires = now() + make_interval(secs := %(lease)s)
                    WHERE id = %(id)s
                      AND fence_token = %(token)s
                      AND status = 'running'
                """, {"id": job_id, "token": fence_token, "lease": self.lease_duration})
                
                if rows.rowcount == 0:
                    # Job was reclaimed — abort execution
                    log.error(f"Lost ownership of job {job_id}")
                    os._exit(1)  # hard exit — cannot safely continue
                
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    log.error("Cannot reach DB for heartbeat — self-terminating")
                    os._exit(1)
    
    def complete_job(self, job_id, fence_token, result):
        with self.work_conn.transaction():
            # Atomically: remove from active, insert into completed
            deleted = self.work_conn.execute("""
                DELETE FROM jobs_active
                WHERE id = %(id)s AND fence_token = %(token)s AND status = 'running'
                RETURNING *
            """, {"id": job_id, "token": fence_token}).fetchone()
            
            if deleted:
                self.work_conn.execute("""
                    INSERT INTO jobs_completed
                    (id, tenant_id, queue_name, status, payload, result,
                     attempt, created_at, completed_at)
                    VALUES (%(id)s, %(tenant)s, %(queue)s, 'completed', %(payload)s,
                            %(result)s, %(attempt)s, %(created)s, now())
                """, {
                    "id": deleted.id, "tenant": deleted.tenant_id,
                    "queue": deleted.queue_name, "payload": deleted.payload,
                    "result": Json(result), "attempt": deleted.attempt,
                    "created": deleted.created_at
                })
```

### Reaper Process

```python
class ReaperProcess:
    """Runs on a single node (leader-elected or cron-based)."""
    
    def reap_expired_leases(self):
        requeued = self.db.execute("""
            UPDATE jobs_active
            SET status = 'pending',
                locked_by = NULL,
                locked_at = NULL,
                lease_expires = NULL,
                fence_token = NULL,
                scheduled_at = now() + make_interval(
                    secs := power(2, LEAST(attempt, 8))  -- exponential backoff, cap at 256s
                )
            WHERE status = 'running'
              AND lease_expires < now()
              AND attempt < max_attempts
            RETURNING id, tenant_id, attempt
        """).fetchall()
        
        for job in requeued:
            log.warn(f"Requeued job {job.id} (tenant={job.tenant_id}, attempt={job.attempt})")
            metrics.counter("jobs.requeued", tags={"tenant": job.tenant_id})
        
        # Move exhausted jobs to dead
        dead = self.db.execute("""
            DELETE FROM jobs_active
            WHERE status = 'running'
              AND lease_expires < now()
              AND attempt >= max_attempts
            RETURNING *
        """).fetchall()
        
        for job in dead:
            self.db.execute("""
                INSERT INTO jobs_completed
                (id, tenant_id, queue_name, status, payload, error_message,
                 attempt, created_at, completed_at)
                VALUES (%s, %s, %s, 'dead', %s, 'Max attempts exceeded',
                        %s, %s, now())
            """, [job.id, job.tenant_id, job.queue_name, job.payload,
                  job.attempt, job.created_at])
            
            metrics.counter("jobs.dead", tags={"tenant": job.tenant_id})
```

---

## 13. Monitoring & Operational Runbook

### Critical Metrics

```sql
-- 1. Queue depth per tenant (alert if growing)
SELECT tenant_id, queue_name, COUNT(*) AS pending_jobs,
       MIN(scheduled_at) AS oldest_pending
FROM jobs_active
WHERE status = 'pending'
GROUP BY tenant_id, queue_name;

-- 2. Dead tuple ratio (alert if > 20%)
SELECT schemaname, relname,
       n_live_tup, n_dead_tup,
       ROUND(n_dead_tup::numeric / NULLIF(n_live_tup + n_dead_tup, 0) * 100, 1) AS dead_pct,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
WHERE relname LIKE 'jobs_%'
ORDER BY dead_pct DESC;

-- 3. Table bloat estimate
SELECT pg_size_pretty(pg_total_relation_size('jobs_active')) AS total_size,
       pg_size_pretty(pg_table_size('jobs_active')) AS heap_size,
       pg_size_pretty(pg_indexes_size('jobs_active')) AS index_size;

-- 4. Transaction ID age (alert if > 500M)
SELECT datname, age(datfrozenxid) AS xid_age
FROM pg_database ORDER BY xid_age DESC;

-- 5. Lock contention on jobs table
SELECT relation::regclass, mode, COUNT(*)
FROM pg_locks
WHERE relation = 'jobs_active'::regclass
GROUP BY relation, mode;

-- 6. VACUUM progress
SELECT relid::regclass AS table_name,
       phase, heap_blks_total, heap_blks_scanned, heap_blks_vacuumed,
       index_vacuum_count, max_dead_tuples, num_dead_tuples
FROM pg_stat_progress_vacuum;
```

### Alert Thresholds

| Metric | Warning | Critical |
|--------|---------|----------|
| Queue depth growth rate | > 100 jobs/min | > 1,000 jobs/min |
| Dead tuple ratio | > 20% | > 50% |
| Table bloat ratio | > 2× live data | > 5× live data |
| XID age | > 500M | > 1B |
| Job claim latency p99 | > 100ms | > 500ms |
| Orphaned jobs (expired leases) | > 10 | > 100 |
| VACUUM run duration | > 10 min | > 60 min |
| Oldest pending job age | > 5 min | > 30 min |

### Emergency Procedures

**Bloat Emergency: Table has grown 10× expected size**

```sql
-- Option 1: pg_repack (online, no exclusive lock)
-- Install extension first
CREATE EXTENSION pg_repack;
-- Run from command line (not inside a transaction)
-- $ pg_repack --table jobs_active --no-order -j 4 dbname

-- Option 2: VACUUM FULL (exclusive lock — downtime required)
VACUUM FULL jobs_active;

-- Option 3: Create new table and swap (online, application-level)
CREATE TABLE jobs_active_new (LIKE jobs_active INCLUDING ALL);
INSERT INTO jobs_active_new SELECT * FROM jobs_active WHERE status IN ('pending', 'running');
-- Swap in application code, then drop old table
```

**VACUUM Blocked by Long Transaction**

```sql
-- Find and terminate the blocker
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state = 'idle in transaction'
  AND now() - xact_start > interval '10 minutes';
```

---

## 14. Decision Framework

### When to Use What

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Decision Tree                                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  Jobs/sec < 100?                                                     │
│  └─ YES → Single table, no partitions, simple polling                │
│  └─ NO ↓                                                             │
│                                                                      │
│  Jobs/sec < 1,000?                                                   │
│  └─ YES → Single partitioned table (by day) + aggressive autovacuum  │
│  └─ NO ↓                                                             │
│                                                                      │
│  Jobs/sec < 10,000?                                                  │
│  └─ YES → Hot/Cold split (jobs_active + jobs_completed partitioned)  │
│           + LISTEN/NOTIFY hybrid + batch claiming                    │
│  └─ NO ↓                                                             │
│                                                                      │
│  Jobs/sec > 10,000?                                                  │
│  └─ PostgreSQL is the wrong tool. Use:                               │
│     • Redis + Lua for queue operations                               │
│     • Kafka for durable event streaming                              │
│     • Purpose-built: Temporal, AWS SQS, Google Cloud Tasks           │
│     • PostgreSQL only as the metadata/state store                    │
│                                                                      │
│  Multi-tenant?                                                       │
│  └─ < 50 tenants with strict isolation → Table per tenant            │
│  └─ 50-10,000 tenants → Row-level tenancy + fair scheduling         │
│  └─ Regulated (healthcare, finance) → Schema per tenant              │
│                                                                      │
│  Need instant job dispatch?                                          │
│  └─ YES → LISTEN/NOTIFY + polling fallback (hybrid)                  │
│  └─ NO  → Polling only (simpler, fewer failure modes)                │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Trade-off Summary

| Dimension | Simple Approach | Production Approach | Trade-off |
|-----------|----------------|---------------------|-----------|
| Table design | Single table | Hot/cold split | Complexity vs performance |
| Cleanup | DELETE + VACUUM | DROP PARTITION | Simplicity vs zero-bloat |
| Job dispatch | Polling | LISTEN/NOTIFY + poll | Latency vs reliability |
| Locking | FOR UPDATE | SKIP LOCKED + fence token | Safety vs throughput |
| Heartbeat | Fixed timeout | Renewable lease + fence | Simplicity vs correctness |
| Multi-tenancy | Row-level filter | Weighted fair queue | Code complexity vs fairness |
| Index strategy | Broad index | Partial index on active | Maintenance vs scan speed |

---

## 15. Data Model Gaps: What the Naive Schema Misses

### State Machine: Use an Enum, Not Free-Form Text

The schema in Section 3 uses `TEXT` for status. In production, use a proper enum:

```sql
CREATE TYPE job_status AS ENUM (
    'pending',      -- waiting to be claimed
    'running',      -- claimed by a worker
    'completed',    -- finished successfully
    'failed',       -- handler returned an error, will retry
    'retryable',    -- explicitly marked for retry (snooze/defer)
    'dead',         -- exhausted all attempts
    'cancelled'     -- cancelled by user or system
);

ALTER TABLE jobs_active ALTER COLUMN status TYPE job_status USING status::job_status;
```

Enum values are stored as 4-byte integers internally — faster comparison than text, and PostgreSQL enforces valid values at the type level.

### Attempts History Table

The main table only stores `attempt` count and `error_message` for the last failure. For debugging, you need the full history:

```sql
CREATE TABLE job_attempts (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id      BIGINT      NOT NULL,
    attempt     SMALLINT    NOT NULL,
    worker_id   TEXT        NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status      TEXT        NOT NULL,  -- 'completed', 'failed', 'timeout'
    error_class TEXT,                  -- e.g. 'TimeoutError', 'HTTPError'
    error_message TEXT,
    stack_trace TEXT,
    duration_ms INTEGER GENERATED ALWAYS AS (
        EXTRACT(MILLISECONDS FROM (finished_at - started_at))
    ) STORED,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_attempts_job ON job_attempts (job_id, attempt);
```

This table is append-only — no bloat from updates. Partition by `created_at` for retention.

### Job Type Registry

Scattered string identifiers (`"send_email"`, `"sync_user"`) across the codebase are a maintenance hazard. Define typed jobs:

```python
from dataclasses import dataclass
from abc import ABC, abstractmethod

@dataclass(frozen=True)
class JobType:
    name: str
    queue: str
    max_attempts: int = 3
    timeout_seconds: int = 300
    priority: int = 0

class JobHandler(ABC):
    job_type: JobType
    
    @abstractmethod
    async def execute(self, payload: dict) -> dict:
        ...

class SendEmailHandler(JobHandler):
    job_type = JobType(
        name="send_email",
        queue="email",
        max_attempts=5,
        timeout_seconds=30,
        priority=10
    )
    
    async def execute(self, payload: dict) -> dict:
        # Handler validates payload shape, not the enqueue path
        ...

# Registry maps type name → handler class
JOB_REGISTRY: dict[str, type[JobHandler]] = {}

def register(handler_cls: type[JobHandler]) -> type[JobHandler]:
    JOB_REGISTRY[handler_cls.job_type.name] = handler_cls
    return handler_cls
```

The registry ensures: (1) every job name maps to exactly one handler, (2) queue/timeout/retry defaults are per-type not per-call, (3) new workers fail fast if a handler is missing.

### Payload Versioning

When job formats change during a rolling deploy, old workers must not crash on new payloads:

```sql
ALTER TABLE jobs_active ADD COLUMN payload_version SMALLINT NOT NULL DEFAULT 1;
```

```python
class SendEmailHandler(JobHandler):
    async def execute(self, payload: dict, version: int) -> dict:
        if version == 1:
            recipient = payload["email"]
        elif version == 2:
            recipient = payload["recipient"]["address"]  # new format
        else:
            raise UnsupportedVersionError(version)
```

### Payload Validation at Enqueue Time

Validate payload structure when the job is submitted, not when a worker picks it up. A malformed payload discovered at execution time wastes a claim cycle and an attempt:

```python
from pydantic import BaseModel

class SendEmailPayload(BaseModel):
    recipient: str
    subject: str
    template_id: str

PAYLOAD_SCHEMAS: dict[str, type[BaseModel]] = {
    "send_email": SendEmailPayload,
}

def enqueue_job(job_type: str, payload: dict):
    schema = PAYLOAD_SCHEMAS.get(job_type)
    if schema:
        schema.model_validate(payload)  # raises ValidationError if invalid
    
    db.execute("INSERT INTO jobs_active ...")
```

---

## 16. Claim Protocol: Subtleties

### Short Claim Transaction

The most critical correctness rule: **processing happens strictly outside the transaction that claims the job.**

```python
# WRONG — holds a transaction open for the entire job duration
with db.transaction():
    job = db.execute("SELECT ... FOR UPDATE SKIP LOCKED").fetchone()
    result = call_external_api(job.payload)  # could take 30 seconds
    db.execute("UPDATE ... SET status = 'completed'")
# Transaction held 30 seconds → blocks VACUUM, holds row lock, wastes connection

# RIGHT — claim in one transaction, process outside
with db.transaction():
    job = db.execute("""
        UPDATE jobs_active SET status = 'running', locked_by = %s, ...
        WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)
        RETURNING *
    """).fetchone()

# Transaction committed — row is now 'running', lock released
result = call_external_api(job.payload)

with db.transaction():
    db.execute("UPDATE ... SET status = 'completed' WHERE fence_token = %s")
```

A long claim transaction is the single most common cause of VACUUM stalls in PostgreSQL job queues.

### Predicate Shape: Equality vs `= ANY`

```sql
-- GOOD: equality predicate → index scan in sorted order
WHERE queue_name = 'email'
ORDER BY priority DESC, scheduled_at
LIMIT 1

-- BAD: = ANY → planner may choose BitmapOr, which destroys sort ordering
WHERE queue_name = ANY(ARRAY['email', 'webhook', 'export'])
ORDER BY priority DESC, scheduled_at
LIMIT 1
-- Result: PostgreSQL cannot use the index to deliver rows in order
-- → explicit Sort node → reads ALL matching rows → sorts → returns 1
```

If a worker processes multiple queues, run one claim query per queue, not one query with `= ANY`.

### Batch Claiming: Head-of-Line Blocking

When Worker-3 claims a batch of 20 jobs, the lease covers all 20. If job #1 takes 4 minutes and jobs #2-20 each take 1 second:

```
t=0:     Claim 20 jobs, lease_expires = t+300s
t=0-240: Processing job #1 (slow HTTP call)
t=240:   Jobs #2-20 have been sitting claimed but unprocessed for 4 minutes
         Other workers could have processed them already
```

**Mitigations:**
- Use a local worker queue: claim a batch but process items concurrently with a thread pool
- Set batch size relative to expected processing time: `batch = target_prefetch_seconds / avg_job_duration`
- Heartbeat per job, not per batch — each job has its own lease timer

### Work Stealing

When hash-based sharding assigns jobs to workers, a slow or crashed worker's partition goes unprocessed. Work stealing lets idle workers reclaim from overloaded partitions:

```python
class StealingWorker:
    def claim_batch(self):
        # First: try own partition
        jobs = self.claim_from_partition(self.partition_id)
        if jobs:
            return jobs
        
        # Second: steal from other partitions (only if own is empty)
        for partition in self.other_partitions():
            jobs = self.claim_from_partition(partition)
            if jobs:
                metrics.counter("jobs.stolen", tags={"from": partition})
                return jobs
        
        return []
```

### Claim Fairness

Without explicit fairness, a fast worker (low-latency network, fast CPU) claims disproportionately more jobs via `SKIP LOCKED` because it arrives at the index scan first more often:

```
Worker-1 (fast, 2ms RTT):  claims 80% of jobs
Worker-2 (slow, 20ms RTT): claims 20% of jobs
```

**Fix:** Add jitter between claim attempts, or use hash-based assignment (Section 6, Solution 3) which eliminates contention entirely.

---

## 17. Failure & Retry Semantics

### Poison Pill Protection

A poison pill is a job whose payload crashes the worker process (OOM, segfault, infinite loop). The danger: if you increment `attempt` on completion, a poison pill restarts the worker forever without ever counting an attempt.

```
Loop:
  Worker claims job-99 (attempt stays at 0) →
  Worker crashes during execution →
  Reaper requeues job-99 (attempt still 0) →
  Another worker claims job-99 →
  Crash → Requeue → Claim → Crash → forever
```

**Fix:** Increment `attempt` at claim time, not at completion:

```sql
UPDATE jobs_active
SET status = 'running',
    attempt = attempt + 1,  -- increment HERE, before execution
    locked_by = 'worker-3',
    lease_expires = now() + interval '5 minutes'
WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)
RETURNING *;
```

Now even if the worker crashes without updating the row, the attempt count is already incremented. After `max_attempts` crashes, the job moves to dead.

### Retryable vs Non-Retryable Errors

Not all failures deserve retry. A 400 Bad Request will fail the same way every time:

```python
class RetryPolicy:
    RETRYABLE = {
        "TimeoutError", "ConnectionError", "HTTPError_5xx",
        "DatabaseUnavailable", "RateLimitExceeded"
    }
    NON_RETRYABLE = {
        "ValidationError", "HTTPError_4xx", "AuthenticationError",
        "PayloadTooLarge"
    }

    @staticmethod
    def should_retry(error: Exception) -> bool:
        error_class = type(error).__name__
        if error_class in RetryPolicy.NON_RETRYABLE:
            return False
        if error_class in RetryPolicy.RETRYABLE:
            return True
        return True  # unknown errors default to retryable
```

Non-retryable errors go straight to `dead` status regardless of remaining attempts.

### Exponential Backoff with Full Jitter

The reaper in Section 12 uses `power(2, attempt)` but has no jitter. Without jitter, all retried jobs for the same attempt number become runnable at the same instant, causing a thundering herd:

```sql
-- With full jitter: uniform random between 0 and exponential ceiling
scheduled_at = now() + make_interval(
    secs := random() * power(2, LEAST(attempt, 8))  -- 0 to 256s, random
)
```

Full jitter (as opposed to equal jitter or decorrelated jitter) gives the best spread for reducing correlated retries.

### Dead Letter Queue Replay / Redrive

Jobs in `dead` status need tooling to inspect and retry:

```sql
-- Redrive: move dead jobs back to pending with attempt reset
UPDATE jobs_active
SET status = 'pending',
    attempt = 0,
    locked_by = NULL,
    lease_expires = NULL,
    fence_token = NULL,
    scheduled_at = now()
WHERE id = ANY($1)           -- array of job IDs
  AND status = 'dead'
RETURNING id;

-- Bulk redrive with filter (e.g., all dead jobs for a specific error)
INSERT INTO jobs_active (tenant_id, queue_name, priority, payload, scheduled_at, max_attempts)
SELECT tenant_id, queue_name, priority, payload, now(), max_attempts
FROM jobs_completed
WHERE status = 'dead'
  AND error_message LIKE '%TimeoutError%'
  AND completed_at > now() - interval '24 hours';
```

### Circuit Breaker per Handler/Queue

When an external API goes down, every job that calls it will fail and retry, amplifying load on both the queue and the failing service:

```python
from datetime import datetime, timedelta

class CircuitBreaker:
    def __init__(self, failure_threshold=5, recovery_timeout=60):
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.last_failure: datetime | None = None
        self.state = "closed"  # closed, open, half-open
    
    def record_failure(self):
        self.failure_count += 1
        self.last_failure = datetime.utcnow()
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
    
    def allow_request(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if datetime.utcnow() - self.last_failure > timedelta(seconds=self.recovery_timeout):
                self.state = "half-open"
                return True  # allow one probe
            return False
        return True  # half-open: allow

# Per-queue circuit breakers
breakers: dict[str, CircuitBreaker] = {}

def claim_batch(queue_name: str):
    breaker = breakers.setdefault(queue_name, CircuitBreaker())
    if not breaker.allow_request():
        log.info(f"Circuit open for queue {queue_name}, skipping")
        return []
    # ... proceed with claim
```

When the circuit is open, the worker stops claiming from that queue entirely — no wasted attempts, no amplifying the outage.

### Snooze / Defer

A handler can say "give this back in 10 minutes" without counting as a failure:

```python
class SnoozeError(Exception):
    def __init__(self, delay_seconds: int):
        self.delay_seconds = delay_seconds

class Worker:
    def execute_job(self, job_id, fence_token, payload):
        try:
            result = handler.execute(payload)
            self.complete_job(job_id, fence_token, result)
        except SnoozeError as e:
            # Don't increment attempt — this isn't a failure
            db.execute("""
                UPDATE jobs_active
                SET status = 'pending',
                    locked_by = NULL,
                    lease_expires = NULL,
                    scheduled_at = now() + make_interval(secs := %s)
                WHERE id = %s AND fence_token = %s
            """, [e.delay_seconds, job_id, fence_token])
        except Exception as e:
            self.fail_job(job_id, fence_token, str(e))
```

### Error Fingerprinting

A DLQ with 50,000 entries is useless without grouping. Fingerprint errors so operators see patterns:

```python
import hashlib

def error_fingerprint(error_class: str, error_message: str, stack_trace: str) -> str:
    # Normalize: strip line numbers and memory addresses from stack
    normalized = re.sub(r'line \d+', 'line N', stack_trace)
    normalized = re.sub(r'0x[0-9a-f]+', '0xADDR', normalized)
    return hashlib.md5(f"{error_class}:{normalized}".encode()).hexdigest()[:12]
```

```sql
ALTER TABLE job_attempts ADD COLUMN error_fingerprint TEXT;
CREATE INDEX idx_attempts_fingerprint ON job_attempts (error_fingerprint, created_at DESC);

-- Group DLQ by error pattern
SELECT error_fingerprint, error_class, COUNT(*) AS occurrences,
       MIN(created_at) AS first_seen, MAX(created_at) AS last_seen,
       (array_agg(error_message ORDER BY created_at DESC))[1] AS latest_message
FROM job_attempts
WHERE status = 'failed'
  AND created_at > now() - interval '24 hours'
GROUP BY error_fingerprint, error_class
ORDER BY occurrences DESC;
```

---

## 18. Scheduling: Cron, Catch-Up & Time

### Cron Jobs: Deduplication Across Workers

Multiple workers must not each spawn the same cron job at the same time slot. Use a unique constraint on `(job_type, scheduled_slot)`:

```sql
CREATE TABLE cron_schedules (
    id              SERIAL PRIMARY KEY,
    job_type        TEXT        NOT NULL,
    cron_expression TEXT        NOT NULL,
    tenant_id       INTEGER     NOT NULL,
    payload         JSONB       NOT NULL DEFAULT '{}',
    enabled         BOOLEAN     NOT NULL DEFAULT true,
    last_run_at     TIMESTAMPTZ,
    next_run_at     TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A single scheduler process (leader-elected) runs every minute:
WITH due_crons AS (
    SELECT id, job_type, tenant_id, payload, next_run_at
    FROM cron_schedules
    WHERE enabled = true AND next_run_at <= now()
    FOR UPDATE SKIP LOCKED
)
INSERT INTO jobs_active (queue_name, tenant_id, payload, scheduled_at, idempotency_key)
SELECT job_type, tenant_id, payload, next_run_at,
       md5(job_type || tenant_id || next_run_at::text)::uuid  -- dedup key per time slot
FROM due_crons
ON CONFLICT (idempotency_key) WHERE status IN ('pending', 'running')
DO NOTHING;

-- Update next_run_at based on cron expression
UPDATE cron_schedules
SET last_run_at = next_run_at,
    next_run_at = cron_next(cron_expression, next_run_at)  -- requires pg_cron or app-level
WHERE id IN (SELECT id FROM due_crons);
```

### Catch-Up Policy: What Happens After Downtime?

If the scheduler was down for 3 hours and a cron job runs every 15 minutes, do you run 12 catch-up instances or just 1?

```python
class CatchUpPolicy:
    SKIP = "skip"         # only run the latest missed slot
    CATCH_UP = "catch_up" # run every missed slot sequentially
    COLLAPSE = "collapse" # run once with metadata about missed slots

def schedule_catchup(cron: CronSchedule, policy: str):
    missed_slots = compute_missed_slots(cron.last_run_at, now(), cron.expression)
    
    if policy == CatchUpPolicy.SKIP:
        enqueue(cron, scheduled_at=now())  # just run once
    elif policy == CatchUpPolicy.CATCH_UP:
        for slot in missed_slots:
            enqueue(cron, scheduled_at=slot)  # one job per missed slot
    elif policy == CatchUpPolicy.COLLAPSE:
        enqueue(cron, scheduled_at=now(), payload={
            **cron.payload,
            "missed_slots": len(missed_slots),
            "first_missed": missed_slots[0],
            "last_missed": missed_slots[-1]
        })
```

### Priority Starvation

Numeric priority (`0-100`) causes starvation: high-priority jobs always jump the queue, so a steady stream of priority-90 jobs means priority-10 jobs never run.

**Prefer weighted queues over numeric priority.** Instead of one queue sorted by priority, use separate queues with weighted claim ratios:

```python
QUEUE_WEIGHTS = {
    "critical": 5,    # 50% of claims
    "default":  3,    # 30% of claims
    "bulk":     2,    # 20% of claims
}

def weighted_claim(worker):
    # Weighted random selection
    queues = list(QUEUE_WEIGHTS.keys())
    weights = list(QUEUE_WEIGHTS.values())
    selected_queue = random.choices(queues, weights=weights, k=1)[0]
    return claim_from_queue(selected_queue)
```

If you must use numeric priority, add **priority aging** to prevent starvation:

```sql
-- Effective priority increases with age
SELECT *, priority + EXTRACT(EPOCH FROM (now() - created_at)) / 60 AS effective_priority
FROM jobs_active
WHERE status = 'pending'
ORDER BY effective_priority DESC
LIMIT 10
FOR UPDATE SKIP LOCKED;
```

A low-priority job (priority=10) created 100 minutes ago has effective_priority = 110, surpassing a high-priority job (priority=100) created just now.

### SKIP LOCKED Breaks FIFO

`FOR UPDATE SKIP LOCKED` gives no ordering guarantee across concurrent workers:

```
Queue (ordered): [A, B, C, D, E]
Worker-1 locks A → Worker-2 skips A, locks B → Worker-1 finishes A fast
Worker-1 locks C → Worker-2 still processing B
Result processing order: A, C, B (NOT A, B, C)
```

For strict ordering (e.g., events for the same user must be processed in order), use **job groups**:

```sql
ALTER TABLE jobs_active ADD COLUMN group_key TEXT;
CREATE INDEX idx_jobs_group ON jobs_active (group_key, created_at) WHERE status = 'pending';

-- Only claim from groups with no running job
SELECT j.* FROM jobs_active j
WHERE j.status = 'pending'
  AND NOT EXISTS (
      SELECT 1 FROM jobs_active j2
      WHERE j2.group_key = j.group_key AND j2.status = 'running'
  )
ORDER BY j.created_at
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

### Job Expiration / TTL

Some jobs are pointless after a deadline:

```sql
ALTER TABLE jobs_active ADD COLUMN expires_at TIMESTAMPTZ;

-- Reaper also expires stale pending jobs
UPDATE jobs_active
SET status = 'cancelled'
WHERE status = 'pending'
  AND expires_at IS NOT NULL
  AND expires_at < now()
RETURNING id;
```

### Timezone / DST Handling for Cron

Store cron schedules in the **tenant's timezone**, but convert to UTC for `next_run_at`:

```sql
ALTER TABLE cron_schedules ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC';

-- When computing next_run_at:
-- 1. Parse cron expression in tenant's timezone
-- 2. Convert result to UTC for storage
-- This handles DST transitions correctly
-- (a "daily at 2am" schedule that falls in DST gap is skipped or doubled, policy-dependent)
```

### Server Time Everywhere

Never trust the worker's clock for scheduling decisions:

```sql
-- ALWAYS use server time
WHERE scheduled_at <= now()                    -- Postgres server's clock
SET lease_expires = now() + interval '5 min'   -- Postgres server's clock

-- NEVER
WHERE scheduled_at <= '2025-01-15T10:00:00Z'   -- worker's clock, might be skewed
SET lease_expires = $1                          -- worker-computed timestamp
```

---

## 19. Job Semantics: Ordering, Dependencies & Cancellation

### Job Dependencies / Workflows (DAGs)

When jobs have dependencies (A must complete before B starts):

```sql
CREATE TABLE job_dependencies (
    job_id     BIGINT NOT NULL REFERENCES jobs_active(id),
    depends_on BIGINT NOT NULL,  -- references jobs_active or jobs_completed
    PRIMARY KEY (job_id, depends_on)
);

-- Job becomes claimable only when all dependencies are completed
-- Modify the claim query:
SELECT j.* FROM jobs_active j
WHERE j.status = 'pending'
  AND NOT EXISTS (
      SELECT 1 FROM job_dependencies d
      LEFT JOIN jobs_completed c ON c.id = d.depends_on AND c.status = 'completed'
      WHERE d.job_id = j.id AND c.id IS NULL
  )
ORDER BY j.priority DESC, j.scheduled_at
LIMIT 10
FOR UPDATE SKIP LOCKED;
```

For fan-out/fan-in (batch completion callbacks):

```sql
CREATE TABLE job_batches (
    batch_id    UUID PRIMARY KEY,
    total_jobs  INTEGER NOT NULL,
    completed   INTEGER NOT NULL DEFAULT 0,
    callback_payload JSONB  -- job to enqueue when all finish
);

-- On each job completion, atomically increment:
UPDATE job_batches
SET completed = completed + 1
WHERE batch_id = $1
RETURNING completed, total_jobs;
-- If completed == total_jobs → enqueue the callback job
```

**When you need DAGs, strongly consider Temporal.** PostgreSQL-native DAGs are fragile — cycle detection, partial failure recovery, and visualization are all hard problems that Temporal solves out of the box.

### Cancellation

```sql
-- Cancel pending job: simple status update
UPDATE jobs_active
SET status = 'cancelled'
WHERE id = $1 AND status = 'pending'
RETURNING id;

-- Cancel running job: set a flag that the worker checks
ALTER TABLE jobs_active ADD COLUMN cancel_requested BOOLEAN NOT NULL DEFAULT false;

UPDATE jobs_active
SET cancel_requested = true
WHERE id = $1 AND status = 'running'
RETURNING id;
```

The worker must cooperatively check `cancel_requested` during long operations:

```python
class CancellableWorker:
    def execute_job(self, job_id, fence_token, payload):
        for chunk in process_in_chunks(payload):
            if self.is_cancelled(job_id, fence_token):
                self.complete_job(job_id, fence_token, status='cancelled')
                return
            process_chunk(chunk)
```

### Debounce / Throttle / Singleton

**Singleton:** Only one instance of a job type can be pending/running at a time:

```sql
-- Enforced via partial unique index
CREATE UNIQUE INDEX idx_singleton ON jobs_active (queue_name, tenant_id)
WHERE status IN ('pending', 'running')
  AND singleton = true;
```

**Debounce:** Replace a pending job with a newer version (e.g., sync_user after rapid edits):

```sql
-- Use the idempotency_key as the job key
-- ON CONFLICT replace the payload and reset scheduled_at
INSERT INTO jobs_active (queue_name, tenant_id, payload, idempotency_key, scheduled_at)
VALUES ('sync_user', 42, '{"user_id": 99}', 'sync-user-99', now() + interval '5 seconds')
ON CONFLICT (idempotency_key) WHERE status IN ('pending', 'running')
DO UPDATE SET payload = EXCLUDED.payload,
              scheduled_at = EXCLUDED.scheduled_at
WHERE jobs_active.status = 'pending';  -- don't replace running jobs
```

### Bulk Enqueue

N individual `INSERT` statements for N jobs is N round-trips. Use multi-row insert or `COPY`:

```sql
-- Multi-row INSERT (up to ~1000 rows per statement)
INSERT INTO jobs_active (tenant_id, queue_name, payload, scheduled_at)
VALUES
    (42, 'email', '{"to":"a@b.com"}', now()),
    (42, 'email', '{"to":"c@d.com"}', now()),
    -- ... up to 1000 rows
;

-- COPY for very large batches (100K+ jobs)
COPY jobs_active (tenant_id, queue_name, payload, scheduled_at)
FROM STDIN WITH (FORMAT csv);
```

---

## 20. Producer Side: Transactional Enqueue & Outbox

### Transactional Enqueue

**This is the single biggest reason to use PostgreSQL as a job queue.** When the job and the business data live in the same database, you get atomicity for free:

```python
async def place_order(order: Order):
    async with db.transaction():
        # Business logic and job enqueue in the SAME transaction
        await db.execute("INSERT INTO orders (...) VALUES (...)")
        await db.execute("""
            INSERT INTO jobs_active (queue_name, tenant_id, payload)
            VALUES ('send_confirmation', %s, %s)
        """, [order.tenant_id, json.dumps({"order_id": order.id})])
    
    # If the transaction commits → both the order AND the job exist
    # If it rolls back → neither exists
    # No "order created but email never sent" bug
```

With Redis/SQS/Kafka, you can't do this — you either write the job first (risk: order fails, orphaned job) or the order first (risk: job enqueue fails, user never gets an email). The workaround is the outbox pattern.

### Outbox Pattern

When some events must go to an external system (Kafka, SQS) while others stay in-database:

```sql
CREATE TABLE outbox (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type  TEXT        NOT NULL,
    payload     JSONB       NOT NULL,
    destination TEXT        NOT NULL,  -- 'kafka', 'sqs', 'webhook'
    published   BOOLEAN     NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Business transaction writes to outbox atomically
BEGIN;
INSERT INTO orders (...) VALUES (...);
INSERT INTO outbox (event_type, payload, destination)
VALUES ('order_placed', '{"order_id": 123}', 'kafka');
COMMIT;

-- Separate publisher process reads and publishes
UPDATE outbox SET published = true
WHERE id IN (
    SELECT id FROM outbox
    WHERE published = false
    ORDER BY id
    LIMIT 100
    FOR UPDATE SKIP LOCKED
)
RETURNING *;
-- Publish each to Kafka/SQS, then commit the UPDATE
```

### Producer Backpressure

When the system is saturated, reject enqueue to prevent unbounded growth:

```python
async def enqueue_with_backpressure(job):
    pending_count = await db.fetchval(
        "SELECT COUNT(*) FROM jobs_active WHERE queue_name = $1 AND status = 'pending'",
        job.queue_name
    )
    
    if pending_count > MAX_QUEUE_DEPTH:  # e.g., 100,000
        raise QueueFullError(f"Queue {job.queue_name} has {pending_count} pending jobs")
    
    await db.execute("INSERT INTO jobs_active ...")
```

---

## 21. Worker Runtime

### Bounded Concurrency

A worker should never consume unlimited database connections:

```python
import asyncio

class BoundedWorker:
    def __init__(self, concurrency=10, dsn: str = ""):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.pool = asyncpg.create_pool(dsn, min_size=concurrency + 2, max_size=concurrency + 2)
        # +2: one for heartbeat, one for LISTEN

    async def run_loop(self):
        while True:
            async with self.semaphore:
                job = await self.claim_one()
                if job:
                    asyncio.create_task(self.process(job))
```

### Pool Sizing vs `max_connections`

```
PostgreSQL default max_connections = 100

Workers:            10 instances
Concurrency/worker: 20 jobs
Connections/worker: 20 (work) + 1 (heartbeat) + 1 (LISTEN) = 22
Total connections:  10 × 22 = 220 > 100 → CONNECTION REFUSED

Add: enqueue services, query services, admin, monitoring
Total: 220 + 30 = 250 connections needed
```

**Solutions:**
1. `max_connections = 300` (increases shared memory usage)
2. PgBouncer in transaction mode for work connections (session mode for LISTEN)
3. Reduce per-worker concurrency

### Graceful Shutdown / Drain

On SIGTERM, the worker must:
1. Stop claiming new jobs
2. Wait for in-flight jobs to complete (with a deadline)
3. Return uncompleted jobs to `pending`

```python
import signal

class GracefulWorker:
    def __init__(self):
        self.draining = False
        self.in_flight: set[int] = set()
        signal.signal(signal.SIGTERM, self._handle_sigterm)
    
    def _handle_sigterm(self, signum, frame):
        log.info("SIGTERM received, draining...")
        self.draining = True
    
    async def run_loop(self):
        while not self.draining:
            jobs = await self.claim_batch()
            for job in jobs:
                self.in_flight.add(job.id)
                asyncio.create_task(self._process_and_track(job))
        
        # Drain: wait for in-flight jobs with timeout
        deadline = time.monotonic() + 30  # 30s drain timeout
        while self.in_flight and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        
        # Return any still-running jobs to pending
        if self.in_flight:
            log.warn(f"Returning {len(self.in_flight)} jobs to pending")
            await db.execute("""
                UPDATE jobs_active
                SET status = 'pending', locked_by = NULL, lease_expires = NULL
                WHERE id = ANY($1) AND locked_by = $2
            """, [list(self.in_flight), self.worker_id])
```

### Handler Isolation

One crashing handler must not take down the entire worker process:

```python
async def safe_execute(self, job):
    try:
        handler = JOB_REGISTRY[job.queue_name]
        async with asyncio.timeout(handler.job_type.timeout_seconds):
            result = await handler.execute(job.payload)
        return result
    except asyncio.TimeoutError:
        raise JobTimeoutError(f"Job {job.id} exceeded {handler.job_type.timeout_seconds}s")
    except MemoryError:
        log.critical("OOM in handler, recycling worker")
        os._exit(1)  # let the supervisor restart us
    except Exception as e:
        # Handler crashed — job fails, worker continues
        raise
```

### Middleware Chain

Cross-cutting concerns (logging, tracing, metrics) as composable middleware:

```python
class LoggingMiddleware:
    async def __call__(self, job, next_handler):
        log.info("job.start", job_id=job.id, queue=job.queue_name, attempt=job.attempt)
        start = time.monotonic()
        try:
            result = await next_handler(job)
            log.info("job.complete", job_id=job.id, duration_ms=(time.monotonic()-start)*1000)
            return result
        except Exception as e:
            log.error("job.failed", job_id=job.id, error=str(e))
            raise

class TracingMiddleware:
    async def __call__(self, job, next_handler):
        trace_ctx = job.payload.get("_trace_context")
        with tracer.start_span("job.execute", parent=trace_ctx):
            return await next_handler(job)

class MetricsMiddleware:
    async def __call__(self, job, next_handler):
        with metrics.timer("job.duration", tags={"queue": job.queue_name}):
            return await next_handler(job)

# Compose: Logging → Tracing → Metrics → Handler
pipeline = compose(LoggingMiddleware(), TracingMiddleware(), MetricsMiddleware())
```

### Adaptive Polling

Don't use a fixed poll interval. Use a greedy loop when work exists, back off when empty:

```python
class AdaptivePoller:
    def __init__(self):
        self.min_interval = 0.01   # 10ms when busy
        self.max_interval = 5.0    # 5s when idle
        self.current = self.min_interval
    
    async def run_loop(self):
        while True:
            jobs = await self.claim_batch()
            if jobs:
                self.current = self.min_interval  # reset to aggressive
                await self.process(jobs)
            else:
                await asyncio.sleep(self.current)
                self.current = min(self.current * 2, self.max_interval)  # exponential backoff
```

---

## 22. Advanced PostgreSQL Operations

### TOAST Awareness

PostgreSQL stores large column values (> ~2KB) out-of-line in a TOAST table. For job payloads, this means:

```sql
-- Check if payloads are being TOASTed
SELECT pg_column_size(payload) AS size,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY pg_column_size(payload)) AS p95_size
FROM jobs_active;
```

If p95 payload size > 2KB, every claim query that reads the payload column does an extra TOAST table lookup. The mitigation:

```
Option 1: Don't SELECT payload in the claim query; fetch it in a second query
Option 2: Store large payloads in object storage (S3), carry only a reference
Option 3: Split the table (hot/cold already helps — jobs_ready has no payload)
```

### WAL Impact

The job queue can dominate the cluster's WAL production:

```
Per job lifecycle: INSERT + 2-3 UPDATEs + DELETE = 5 WAL records
At 5,000 jobs/sec: 25,000 WAL records/sec
Average WAL record size: ~200 bytes
WAL throughput: 5 MB/sec just from the job queue

Add indexes: each index update adds another WAL record
5 indexes × 5 operations × 200 bytes = 5,000 bytes/job = 25 MB/sec
```

This affects replication lag (streaming replication must replay all this WAL) and backup storage.

**Mitigations:**
- `UNLOGGED` for the ready queue table (no WAL at all — acceptable for derived data)
- Minimize number of indexes on the hot table
- `wal_level = replica` not `logical` (unless you need CDC)
- Dedicated tablespace on fast storage for WAL

### Replication Slots Block VACUUM

Streaming replication with `hot_standby_feedback = on` or unused replication slots pin the xmin horizon just like long-running transactions:

```sql
-- Check if replication slots are blocking VACUUM
SELECT slot_name, slot_type, active,
       age(xmin) AS xmin_age,
       age(catalog_xmin) AS catalog_xmin_age
FROM pg_replication_slots;

-- An inactive slot with growing xmin_age is a VACUUM blocker
-- Drop it if no longer needed:
SELECT pg_drop_replication_slot('unused_slot');
```

### Separate Database for the Queue

At scale, the job queue's write volume, VACUUM pressure, and connection consumption can interfere with the application's OLTP workload:

```
Shared instance:
  Application writes: 2,000 TPS (orders, users, products)
  Queue writes:       25,000 TPS (job lifecycle)
  VACUUM pressure:    Dominated by queue
  max_connections:     Split between app and workers
  Autovacuum workers:  Shared between app tables and queue tables

Separate instance:
  App DB:   2,000 TPS, autovacuum tuned for OLTP, connections for app
  Queue DB: 25,000 TPS, autovacuum tuned for queue, connections for workers
  Trade-off: Lose transactional enqueue (need outbox pattern)
```

**Decision:** Keep them together as long as you can (transactional enqueue is too valuable). Split when WAL throughput or VACUUM contention becomes the bottleneck. The outbox pattern bridges the gap.

### Know the Ceiling

PostgreSQL queues run on the **primary only**. Read replicas don't help for the claim path (`FOR UPDATE SKIP LOCKED` requires write access). This means:

```
Max throughput = single PostgreSQL primary's write capacity
Typical ceiling: 10,000-50,000 claims/sec (hardware-dependent)
```

Beyond this, you need either:
- Horizontal sharding (multiple PostgreSQL primaries, each owning a subset of queues)
- A purpose-built queue (Redis, SQS, Kafka)

### REINDEX CONCURRENTLY

B-tree indexes on job tables fragment over time even with VACUUM. Schedule periodic rebuilds:

```sql
-- Non-blocking index rebuild (PostgreSQL 12+)
REINDEX INDEX CONCURRENTLY idx_jobs_fetchable;

-- Or use pg_repack for the whole table
-- $ pg_repack --table jobs_active --only-indexes dbname
```

River ships a reindexer as a built-in maintenance service that runs on a schedule.

### Online Migrations

Rolling deployments mean old and new workers run simultaneously. Schema changes must be backwards-compatible:

```
Expand/Contract pattern:
1. EXPAND: Add new column (nullable, with default)
   ALTER TABLE jobs_active ADD COLUMN new_field TEXT DEFAULT 'v1';
   
2. MIGRATE: Backfill existing rows
   UPDATE jobs_active SET new_field = compute(old_field) WHERE new_field IS NULL;
   
3. Deploy new code that writes both old and new columns
   
4. CONTRACT: Drop old column once all workers use the new one
   ALTER TABLE jobs_active DROP COLUMN old_field;
```

Never rename a column or change its type in a single deploy — old workers will crash.

---

## 23. Observability Gaps

### `oldest_pending_age` Is the Primary Metric

Queue depth is misleading. 10,000 pending jobs could be fine (workers will clear them in seconds) or catastrophic (workers are down). **`oldest_pending_age` tells you the truth:**

```sql
SELECT queue_name,
       now() - MIN(scheduled_at) FILTER (WHERE status = 'pending' AND scheduled_at <= now())
           AS oldest_pending_age,
       COUNT(*) FILTER (WHERE status = 'pending') AS pending_count,
       COUNT(*) FILTER (WHERE status = 'running') AS running_count
FROM jobs_active
GROUP BY queue_name;
```

Alert on `oldest_pending_age > 5 minutes` — not on depth.

### Autoscaling

Scale workers based on `oldest_pending_age`, not CPU or queue depth:

```
oldest_pending_age > 2 min → scale up workers
oldest_pending_age < 30s for 5 min → scale down workers
```

### Handler Duration Histogram

Track per-handler execution time to detect degradation:

```python
# Emit as a histogram metric
metrics.histogram("job.handler.duration_ms",
                  value=duration_ms,
                  tags={"handler": job.queue_name, "status": "success"})
```

Alert on p99 handler duration increasing — a slow external API will cascade into queue buildup.

### Lease Expiry Rate

A proxy for worker crashes. If lease expiry rate spikes, workers are dying:

```sql
-- Track reaper activity as a time-series metric
SELECT COUNT(*) AS expired_leases
FROM jobs_active
WHERE status = 'running'
  AND lease_expires < now();
```

### Distributed Tracing

Carry trace context through the job payload so you can trace a request from HTTP → enqueue → worker:

```python
# At enqueue time
trace_id = get_current_trace_id()
payload["_trace_context"] = {"trace_id": trace_id, "span_id": get_current_span_id()}

# At execution time
trace_ctx = payload.pop("_trace_context", None)
with tracer.start_span("job.execute", parent=trace_ctx, attributes={"job_id": job.id}):
    handler.execute(payload)
```

### Admin UI / Pause-Resume

An operational necessity — not a nice-to-have:

```sql
-- Pause a queue: workers skip it during claim
CREATE TABLE queue_config (
    queue_name TEXT PRIMARY KEY,
    paused     BOOLEAN NOT NULL DEFAULT false,
    paused_at  TIMESTAMPTZ,
    paused_by  TEXT
);

-- Workers check before claiming:
SELECT paused FROM queue_config WHERE queue_name = $1;
-- If paused, skip this queue entirely
```

Oban Web is the reference implementation for what a good admin UI looks like: job inspection, retry, cancel, queue pause, real-time metrics.

---

## 24. Security & Compliance

### Payloads Land in WAL and Backups

Every `INSERT` and `UPDATE` to `jobs_active` writes the full row (including payload) to the WAL. The WAL is streamed to replicas and archived to backup storage. If payloads contain PII:

```
Data flow: INSERT payload → WAL → streaming replication → replica
                                → WAL archive → S3 backup (retained 30 days)
                                → pg_basebackup → backup server
```

**PII in payloads means PII in backups, replicas, and WAL archives.** This has GDPR implications.

### Encrypt Sensitive Payload Fields

```python
from cryptography.fernet import Fernet

# Application-layer encryption for sensitive fields
def encrypt_payload(payload: dict, sensitive_keys: set[str]) -> dict:
    encrypted = payload.copy()
    for key in sensitive_keys & payload.keys():
        encrypted[key] = fernet.encrypt(json.dumps(payload[key]).encode()).decode()
        encrypted[f"_{key}_encrypted"] = True
    return encrypted

# At enqueue:
enqueue(job_type="send_email", payload=encrypt_payload(
    {"recipient": "user@example.com", "body": "Your order..."},
    sensitive_keys={"recipient", "body"}
))
```

### Secrets Out of Payloads

Never store API keys, tokens, or credentials in job payloads:

```python
# WRONG
enqueue(payload={"api_key": "sk-live-abc123", "action": "charge"})

# RIGHT — store a reference, resolve at execution time
enqueue(payload={"credential_ref": "stripe_api_key", "action": "charge"})

# Handler fetches the secret from a vault at runtime
class ChargeHandler(JobHandler):
    async def execute(self, payload):
        api_key = await vault.get_secret(payload["credential_ref"])
        stripe.api_key = api_key
        ...
```

### Least Privilege Database Roles

```sql
-- Enqueue service: can only INSERT into jobs_active
CREATE ROLE job_producer;
GRANT INSERT ON jobs_active TO job_producer;
GRANT USAGE ON SEQUENCE jobs_active_id_seq TO job_producer;

-- Worker: can UPDATE/DELETE jobs_active, INSERT into jobs_completed
CREATE ROLE job_worker;
GRANT SELECT, UPDATE, DELETE ON jobs_active TO job_worker;
GRANT INSERT ON jobs_completed TO job_worker;
GRANT USAGE ON SEQUENCE fence_token_seq TO job_worker;

-- Admin: full access (for DLQ replay, cancellation, queue management)
CREATE ROLE job_admin;
GRANT ALL ON jobs_active, jobs_completed, cron_schedules, queue_config TO job_admin;

-- Read-only: for dashboards and monitoring
CREATE ROLE job_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO job_readonly;
```

### Row-Level Security for Multi-Tenancy

```sql
ALTER TABLE jobs_active ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON jobs_active
    USING (tenant_id = current_setting('app.tenant_id')::integer);

-- Set tenant context on each connection
SET app.tenant_id = '42';
-- Now all queries automatically filter by tenant_id = 42
```

### GDPR Right to Erasure

When a user requests deletion, you must purge their data from job payloads — but keep the audit record:

```sql
-- Scrub PII from completed jobs, keep the metadata
UPDATE jobs_completed
SET payload = '{"scrubbed": true}'::jsonb,
    result = NULL,
    error_message = NULL
WHERE tenant_id = $1
  AND payload->>'user_id' = $2;

-- Also scrub from attempts history
UPDATE job_attempts
SET error_message = '[SCRUBBED]',
    stack_trace = NULL
WHERE job_id IN (
    SELECT id FROM jobs_completed
    WHERE tenant_id = $1 AND payload->>'user_id' = $2
);
```

### RBAC on Admin Operations

Not every engineer should be able to replay DLQ jobs or cancel running jobs:

```python
ADMIN_PERMISSIONS = {
    "job.cancel":     ["admin", "oncall"],
    "job.retry":      ["admin", "oncall"],
    "job.redrive":    ["admin"],
    "queue.pause":    ["admin", "oncall"],
    "queue.purge":    ["admin"],
}

def require_permission(action: str, user: User):
    allowed_roles = ADMIN_PERMISSIONS.get(action, [])
    if not any(role in user.roles for role in allowed_roles):
        raise PermissionDenied(f"Action {action} requires one of {allowed_roles}")
```

### Audit Log

```sql
CREATE TABLE admin_audit_log (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor       TEXT        NOT NULL,
    action      TEXT        NOT NULL,
    target_type TEXT        NOT NULL,  -- 'job', 'queue', 'cron'
    target_id   TEXT        NOT NULL,
    details     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Example: admin replays a dead job
INSERT INTO admin_audit_log (actor, action, target_type, target_id, details)
VALUES ('admin@company.com', 'redrive', 'job', '12345',
        '{"from_status": "dead", "reason": "external API recovered"}');
```

---

## 25. Scaling: Capacity Planning & Exit Path

### Capacity Planning Formula

```
Per job: INSERT(1) + UPDATE to running(1) + heartbeats(N) + UPDATE/DELETE to complete(1) + INSERT to completed(1)
       = 4 + N writes per job

At 5,000 jobs/sec with avg 2 heartbeats per job:
  Write TPS = 5,000 × 6 = 30,000 TPS

WAL per write ≈ 200 bytes (tuple + index updates)
WAL throughput = 30,000 × 200 = 6 MB/sec

With 5 indexes, each write generates ~5 additional WAL entries:
Actual WAL ≈ 30 MB/sec

Replication lag = WAL throughput / replication bandwidth
```

### Global Concurrency Limits

Per-tenant limits are not enough. Some resources have cluster-wide limits:

```sql
-- "No more than 20 concurrent report-generation jobs across all workers"
-- Use a dedicated limiter table

CREATE TABLE global_concurrency (
    resource_key TEXT PRIMARY KEY,
    max_slots    INTEGER NOT NULL,
    used_slots   INTEGER NOT NULL DEFAULT 0
);

INSERT INTO global_concurrency VALUES ('report_generation', 20, 0);

-- At claim time (inside the claim transaction):
UPDATE global_concurrency
SET used_slots = used_slots + 1
WHERE resource_key = 'report_generation'
  AND used_slots < max_slots
RETURNING used_slots;
-- If 0 rows returned → limit reached, skip this job type

-- At completion time:
UPDATE global_concurrency
SET used_slots = GREATEST(used_slots - 1, 0)
WHERE resource_key = 'report_generation';
```

### Queue-to-Pool Mapping

Different queues should run on different worker deployments:

```
Deployment 1 (email-workers):
  - Queues: [email, notification]
  - Concurrency: 50
  - Scaling: based on email queue oldest_pending_age

Deployment 2 (report-workers):
  - Queues: [report, export]
  - Concurrency: 5 (reports are CPU-heavy)
  - Scaling: based on report queue oldest_pending_age

Deployment 3 (webhook-workers):
  - Queues: [webhook]
  - Concurrency: 100 (I/O bound, high fan-out)
  - Scaling: based on webhook queue oldest_pending_age
```

This prevents a CPU-heavy report from blocking email delivery.

### Rate Limiting Against External Resources

Check rate limits **before** claiming, not after:

```python
class RateLimitedWorker:
    def __init__(self):
        self.rate_limiters = {
            "stripe_api": TokenBucket(rate=100, capacity=100),   # 100 req/sec
            "sendgrid_api": TokenBucket(rate=500, capacity=500), # 500 req/sec
        }
    
    async def claim_batch(self, queue_name: str):
        resource = QUEUE_TO_RESOURCE.get(queue_name)
        if resource and not self.rate_limiters[resource].try_acquire():
            return []  # don't claim — we can't execute anyway
        
        return await super().claim_batch(queue_name)
```

### Exit Path: When PostgreSQL Isn't Enough

Signs it's time to move:

```
1. Claim latency p99 > 200ms despite all optimizations
2. WAL throughput > 100 MB/sec (approaching disk limits)
3. autovacuum can't keep up (dead_tup ratio chronically > 30%)
4. max_connections exhausted even with PgBouncer
5. Replication lag chronically > 5 seconds due to WAL volume
```

Migration strategy:

```
Phase 1: Add Redis/SQS as a "fast lane" for high-volume, low-importance jobs
         PostgreSQL keeps critical jobs (transactional enqueue)
         
Phase 2: Move all claim operations to Redis (BRPOPLPUSH)
         PostgreSQL becomes the state store (status, history, retry)
         
Phase 3: Full migration to SQS/Kafka
         PostgreSQL stores only job metadata and audit history
```

**Key rule:** Never delete the PostgreSQL state store. Even at full scale, you need a relational store for job history queries, tenant analytics, and admin operations.

---

## 26. Developer Experience & Testing

### Testing Primitives

```python
class InlineExecutor:
    """Run jobs synchronously in tests — no background workers needed."""
    
    async def enqueue_and_execute(self, job_type: str, payload: dict) -> dict:
        handler = JOB_REGISTRY[job_type]
        return await handler.execute(payload)

class FakeClock:
    """Control time in tests for scheduled jobs."""
    
    def __init__(self, start: datetime):
        self._now = start
    
    def advance(self, seconds: int):
        self._now += timedelta(seconds=seconds)
    
    def now(self) -> datetime:
        return self._now

# Test assertions
class JobAssertions:
    @staticmethod
    async def assert_enqueued(queue_name: str, count: int = 1, payload_match: dict = None):
        jobs = await db.fetch(
            "SELECT * FROM jobs_active WHERE queue_name = $1 AND status = 'pending'",
            queue_name
        )
        assert len(jobs) == count
        if payload_match:
            for key, value in payload_match.items():
                assert jobs[0]["payload"][key] == value
```

### Fault Injection

The concept map's reference to `SIGKILL` the worker, drop the DB connection, hang the external API — these are the right chaos tests:

```python
# Test: worker crash mid-execution
@pytest.mark.chaos
async def test_worker_crash_requeue():
    job_id = await enqueue("slow_job", {"sleep": 60})
    worker = spawn_worker()
    
    await wait_until(lambda: get_job_status(job_id) == "running")
    worker.kill(signal.SIGKILL)  # hard kill, no cleanup
    
    # Reaper should requeue within lease_duration
    await wait_until(
        lambda: get_job_status(job_id) == "pending",
        timeout=LEASE_DURATION + REAPER_INTERVAL + 10
    )

# Test: DB connection drop during heartbeat
@pytest.mark.chaos
async def test_heartbeat_failure_self_terminates():
    job_id = await enqueue("long_job", {})
    worker = spawn_worker()
    
    await wait_until(lambda: get_job_status(job_id) == "running")
    drop_pg_connections(worker.pid)  # iptables or pg_terminate_backend
    
    # Worker should self-terminate after 3 failed heartbeats
    await wait_until(lambda: not worker.is_alive(), timeout=HEARTBEAT_INTERVAL * 4)
```

### Load/Benchmark Harness

```sql
-- EXPLAIN the claim path under load
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT id FROM jobs_active
WHERE status = 'pending' AND scheduled_at <= now()
ORDER BY priority DESC, scheduled_at
LIMIT 10
FOR UPDATE SKIP LOCKED;

-- Key things to check:
-- 1. Is it using the partial index? (Index Scan using idx_jobs_fetchable)
-- 2. How many heap fetches vs rows returned? (Buffers: shared hit, shared read)
-- 3. Execution time (should be < 10ms for healthy table)
```

### Incident Runbook

| Symptom | Likely Cause | Action |
|---------|-------------|--------|
| Queue depth growing, workers idle | Workers can't connect to DB | Check `max_connections`, PgBouncer |
| Claim latency > 500ms | Index bloat / dead tuples | Check `n_dead_tup`, run `REINDEX CONCURRENTLY` |
| DLQ growing rapidly | External API down | Check circuit breaker, pause affected queue |
| VACUUM running for hours | Long-running transaction blocking | `pg_stat_activity`, kill idle-in-transaction |
| Table size 10× expected | VACUUM not keeping up | Check autovacuum settings, `pg_repack` |
| XID age approaching 1B | Anti-wraparound VACUUM can't finish | Emergency VACUUM, increase `autovacuum_freeze_max_age` |
| Replication lag > 30s | Queue WAL overwhelming replica | Reduce indexes, consider `UNLOGGED` for ready queue |
| Jobs processed out of order | `SKIP LOCKED` inherent behavior | Use job groups for ordering, or accept it |

---

## Production Checklist (Complete)

### MVP — Can't Launch Without

```
[ ] FOR UPDATE SKIP LOCKED claim protocol
[ ] Short claim transaction (processing outside the transaction)
[ ] Lease with fence token on every claim
[ ] Reaper process for expired leases
[ ] max_attempts with exponential backoff + full jitter
[ ] Retryable vs non-retryable error classification
[ ] Dead letter state (dead) with visibility
[ ] Poison pill protection (increment attempt at claim time)
[ ] Partial index on the claim path
[ ] Payload validation at enqueue time
[ ] Job type registry (typed, not string identifiers)
[ ] Transactional enqueue (the reason you picked PostgreSQL)
[ ] Per-table autovacuum tuning (scale_factor=0, cost_delay=0)
[ ] Bloat monitoring (n_dead_tup, index sizes)
[ ] xmin horizon monitoring (idle_in_transaction_session_timeout)
[ ] statement_timeout on all connections
[ ] Bounded worker concurrency
[ ] Graceful shutdown / drain on SIGTERM
[ ] Pool sizing vs max_connections math
[ ] Polling interval ≤ 5 seconds
[ ] oldest_pending_age metric per queue (THE primary metric)
[ ] Queue depth by state/queue/tenant
[ ] Throughput + success/failure/DLQ rate metrics
[ ] Structured logging (job_id, attempt, worker_id)
[ ] Alerts and SLOs (on age, DLQ growth, autovacuum lag)
[ ] Server time everywhere (now(), never worker clock)
[ ] Least-privilege DB roles (producer, worker, admin)
[ ] Secrets out of payloads (store references, not credentials)
[ ] PII awareness (payloads land in WAL and backups)
[ ] RBAC on admin operations (cancel, retry, purge)
[ ] Online migrations (expand/contract)
[ ] Containerized worker deployments
[ ] CI: lint, tests, migrations, security scan
[ ] Testing primitives (inline mode, fake clock, assertions)
[ ] One-command local startup
```

### Growth — Needed as You Scale

```
[ ] Hot/cold table split (jobs_active + jobs_completed)
[ ] Attempts history table (per-attempt worker_id, error, stack trace)
[ ] Payload versioning (old workers handle new formats)
[ ] LISTEN/NOTIFY + polling hybrid (latency optimization)
[ ] NOTIFY debounce (one per batch, not per job)
[ ] Thundering herd mitigation (jitter before claim)
[ ] PgBouncer compatibility (session mode for LISTEN)
[ ] Batch claiming (10-20 jobs per claim)
[ ] Heartbeat (lease extension for long jobs)
[ ] Cooperative cancellation (AbortSignal / context into handler)
[ ] Cron / recurring jobs with dedup (unique key per time slot)
[ ] Catch-up policy for missed cron runs
[ ] Weighted queues over numeric priority
[ ] Job expiration / TTL
[ ] Timezone / DST handling for cron
[ ] Uniqueness / deduplication (idempotency_key with partial unique index)
[ ] Debounce / singleton patterns
[ ] Cancellation (pending + running)
[ ] Bulk enqueue (multi-row INSERT or COPY)
[ ] DLQ replay / redrive tooling
[ ] Circuit breaker per handler/queue
[ ] Error fingerprinting
[ ] Snooze / defer
[ ] Outbox pattern (if events go to Kafka/SQS)
[ ] Enqueue-time rate limiting and quotas
[ ] DROP PARTITION retention (not DELETE)
[ ] Partition creation automated 7 days ahead
[ ] Index maintenance (REINDEX CONCURRENTLY on schedule)
[ ] TOAST awareness (p95 of pg_column_size(payload))
[ ] fillfactor tuning
[ ] Lock monitoring (pg_locks)
[ ] Dedicated heartbeat and LISTEN connections
[ ] Handler isolation (one crash doesn't kill the worker)
[ ] Middleware chain (logging, tracing, metrics)
[ ] Adaptive polling (greedy when busy, backoff when idle)
[ ] Sleep until next run_at
[ ] Claim latency p50/p99 metric
[ ] Handler duration histogram
[ ] Lease expiry rate metric
[ ] Distributed tracing (trace context in payload)
[ ] Admin UI / CLI (inspect, retry, cancel, pause)
[ ] Pause / resume queue (incident kill switch)
[ ] Per-tenant concurrency cap
[ ] Weighted fair scheduling
[ ] Noisy neighbor isolation (large tenants get own queues)
[ ] Row-Level Security for multi-tenancy
[ ] Audit log of admin actions
[ ] GDPR retention + right to erasure (payload scrubbing)
[ ] Fault injection / chaos testing
[ ] Load/benchmark harness with EXPLAIN ANALYZE
[ ] Incident runbook
```

### Scale — Maturity Only

```
[ ] Bucket sharding + work stealing
[ ] Progress-based heartbeat (only extend on real progress)
[ ] Worker registry / presence table
[ ] Priority aging (prevent starvation)
[ ] Job dependencies / DAGs (evaluate Temporal at this point)
[ ] Ordering guarantees / job groups
[ ] Retry budget / load shedding
[ ] Producer backpressure (reject enqueue when saturated)
[ ] WAL / full-page write awareness
[ ] Separate database/instance for the queue
[ ] Know the ceiling (primary-only, replicas don't help claims)
[ ] Global concurrency limits (cluster-wide resource caps)
[ ] Queue → pool mapping (separate deployments per queue type)
[ ] Autoscaling on oldest_pending_age
[ ] Capacity planning (jobs/s × writes per job = WAL load)
[ ] End-to-end backpressure
[ ] Exit path planning (when to move to Redis/SQS/Kafka)
```
