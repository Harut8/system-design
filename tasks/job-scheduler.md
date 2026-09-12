## System Design Task: Job Scheduler with PostgreSQL

### Problem Statement

Design a **production-grade distributed job scheduler** backed by **PostgreSQL** that can reliably enqueue, schedule, and execute millions of jobs per day across a fleet of worker nodes.

The system must handle **high-throughput job ingestion**, provide **exactly-once execution guarantees**, and remain performant under sustained load without degrading the database — addressing PostgreSQL-specific challenges like **table bloat, VACUUM pressure, and MVCC overhead**.

This scheduler will serve as the backbone for async task processing across multiple tenants, so it must be **multi-tenant, observable, and operationally resilient**.

---

### Functional Requirements

Your system must support:

1. **Job Submission**

   * Submit jobs with a payload, priority, scheduled time, and tenant ID
   * Support immediate execution and future-scheduled jobs
   * Idempotent submission via client-provided deduplication keys

2. **Job Execution**

   * Workers poll for and claim jobs with **exactly-once semantics**
   * Support configurable retry policies (max attempts, backoff strategy)
   * Jobs can have a maximum execution timeout

3. **Job Lifecycle Management**

   * States: `pending` → `running` → `completed` / `failed` / `dead`
   * Support cancellation of pending/running jobs
   * Queryable job history per tenant

4. **Scheduling**

   * One-time delayed jobs (execute at time T)
   * Recurring/cron jobs (execute every N minutes/hours)
   * Priority-based ordering within a queue

5. **Multi-Tenancy**

   * Tenant-level isolation for job queues
   * Per-tenant rate limiting and quota enforcement
   * Fair scheduling across tenants (no single tenant starves others)

6. **APIs**

   ```
   POST   /api/v1/jobs                    — Submit a job
   GET    /api/v1/jobs/{id}               — Get job status
   DELETE /api/v1/jobs/{id}               — Cancel a job
   GET    /api/v1/jobs?tenant=X&status=Y  — List jobs with filters
   POST   /api/v1/jobs/{id}/retry         — Manually retry a failed job
   ```

---

### Non-Functional Requirements

| Requirement     | Target                                                  |
|-----------------|----------------------------------------------------------|
| Throughput      | 10,000+ jobs/sec enqueue, 5,000+ jobs/sec dequeue       |
| Latency         | Job claim p99 < 50ms                                     |
| Availability    | 99.95% uptime                                            |
| Consistency     | Exactly-once execution (at-least-once with idempotency)  |
| Retention       | Hot data: 7 days, Archive: 90 days                       |
| Multi-tenancy   | 1,000+ tenants, fair scheduling                          |
| Recovery        | Automatic requeue of orphaned jobs within 60 seconds      |

---

### Deep Dive Areas (Required)

You must address **all** of the following PostgreSQL-specific challenges:

1. **Table Bloat & MVCC (xmin/xmax)**
   * How does PostgreSQL's MVCC model cause bloat in a high-churn job table?
   * What is the role of `xmin` and `xmax` in tuple visibility?
   * How does `VACUUM` reclaim dead tuples, and why can it fall behind?
   * What is transaction ID wraparound and how do you prevent it?

2. **LISTEN/NOTIFY vs Polling**
   * How does PostgreSQL's `LISTEN/NOTIFY` work internally?
   * What are its failure modes (connection loss, buffer overflow, no persistence)?
   * When should you use it vs polling, and can you combine both?

3. **Heartbeat & Lease Problems**
   * How do workers prove they are alive (heartbeat vs lease)?
   * What happens during GC pauses, network partitions, or clock skew?
   * How do you avoid the "zombie worker" problem (worker thinks it owns a job, but lease expired)?

4. **Partitioning Strategy**
   * `DROP PARTITION` vs `DELETE` — performance and bloat implications
   * Partitioned tables vs separate tables per tenant vs single table
   * How does partition pruning affect the ordered scan problem?
   * Index behavior across partitions (local vs global indexes)

5. **The Ordered Scan Problem**
   * Why does `SELECT ... ORDER BY priority, scheduled_at LIMIT 1 FOR UPDATE SKIP LOCKED` degrade?
   * How does index bloat and dead tuple accumulation affect this scan?
   * What are the alternatives (hash-based sharding, separate priority queues, materialized ready queue)?

6. **Multi-Tenancy Isolation**
   * Schema-per-tenant vs row-level tenancy vs queue-per-tenant
   * How to prevent a noisy neighbor from starving others
   * Tenant-aware connection pooling and resource limits

---

### Constraints & Assumptions

* PostgreSQL is the **only** persistent store (no Redis, no external queue)
* Workers are **stateless** and horizontally scalable
* Network partitions between workers and DB are possible
* Clock skew between workers is bounded (NTP, < 1 second)
* Job payloads are < 64 KB (larger payloads stored in object storage, job carries a reference)

---

### Evaluation Criteria

| Criteria                         | Weight |
|----------------------------------|--------|
| PostgreSQL internals depth       | 25%    |
| Bloat/VACUUM strategy            | 20%    |
| Correctness (exactly-once, leases) | 20% |
| Partitioning & scan optimization | 15%   |
| Multi-tenancy design             | 10%   |
| Operational readiness            | 10%   |

---

### Reference Systems

Study these for inspiration:

* **Graphile Worker** — `SKIP LOCKED`-based, lightweight
* **pgboss** — Node.js, partition-based archival
* **Temporal** — Durable execution, lease-based ownership
* **Que** — Ruby, advisory lock approach
* **River** — Go, `LISTEN/NOTIFY` + polling hybrid
