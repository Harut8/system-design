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

### Production Checklist

```
[ ] Hot/Cold table split (jobs_active + jobs_completed)
[ ] jobs_completed partitioned by day with automated DROP PARTITION
[ ] LISTEN/NOTIFY + polling hybrid for job dispatch
[ ] Batch claiming (10-20 jobs per claim query)
[ ] Fence tokens on every claim for exactly-once safety
[ ] Heartbeat thread with self-termination on failure
[ ] Reaper process for expired leases (runs every 30s)
[ ] Per-tenant concurrency limits
[ ] Autovacuum tuned per table (scale_factor = 0.01, cost_delay = 2)
[ ] maintenance_work_mem = 1GB for job table VACUUM
[ ] idle_in_transaction_session_timeout = 5min
[ ] Monitoring: dead tuple ratio, XID age, queue depth, claim latency
[ ] Alerting on VACUUM lag and bloat ratio
[ ] pg_repack available for emergency compaction
[ ] Partition creation automated 7 days ahead
[ ] Read replicas for reporting/analytics queries
```
