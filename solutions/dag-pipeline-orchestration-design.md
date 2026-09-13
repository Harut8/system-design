# DAG-Based Pipeline Orchestration Engine: Design Document

> Solution to [`tasks/dag-pipeline-orchestration.md`](../tasks/dag-pipeline-orchestration.md).

---

## Table of Contents

1. [Requirements Clarification](#1-requirements-clarification)
2. [Capacity Estimates](#2-capacity-estimates)
3. [High-Level Architecture](#3-high-level-architecture)
4. [DAG Definition and Parsing](#4-dag-definition-and-parsing)
5. [The Scheduler Loop](#5-the-scheduler-loop)
6. [Task Instance State Machine](#6-task-instance-state-machine)
7. [Dependency Resolution and Trigger Rules](#7-dependency-resolution-and-trigger-rules)
8. [Executor Architecture](#8-executor-architecture)
9. [KubernetesExecutor Deep Dive](#9-kubernetesexecutor-deep-dive)
10. [DBOS-Style Transactional Execution](#10-dbos-style-transactional-execution)
11. [XCom and Data Passing](#11-xcom-and-data-passing)
12. [Backfill, Catchup, and Logical Date Semantics](#12-backfill-catchup-and-logical-date-semantics)
13. [Sensors and Deferrable Operators](#13-sensors-and-deferrable-operators)
14. [Dynamic DAGs and Mapped Tasks](#14-dynamic-dags-and-mapped-tasks)
15. [Dataset-Driven Scheduling](#15-dataset-driven-scheduling)
16. [Pools, Priority, and Concurrency Control](#16-pools-priority-and-concurrency-control)
17. [Metadata Database Design](#17-metadata-database-design)
18. [Metadata DB at Scale: Growth, Bottlenecks, and Archival](#18-metadata-db-at-scale-growth-bottlenecks-and-archival)
19. [Observability and Debugging](#19-observability-and-debugging)
20. [Failure Walkthroughs](#20-failure-walkthroughs)
21. [Trade-offs](#21-trade-offs)
22. [Evolution Path](#22-evolution-path)

---

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|---|---|---|
| **Scope** | Does the engine execute task code, or just dispatch? | The engine **dispatches** task instances to an executor. The executor runs the code (in a subprocess, Celery worker, or K8s pod). The engine owns scheduling, dependency resolution, state tracking, and metadata — not the business logic inside tasks. |
| **Persistence** | Is the metadata DB opinionated? | **PostgreSQL** is the primary target. MySQL is supported for backward compatibility. The metadata DB stores DAG definitions, run state, task instance state, XCom values, connections, pools, and logs. It is the single source of truth for orchestration state. |
| **DAG lifetime** | Are DAGs long-running? | No. A DAG run represents a bounded execution: process today's data, train this week's model, run this hour's quality checks. Each run starts, executes its task graph, and completes. Long-running processes (subscription lifecycle, order fulfillment) belong in a Temporal-style engine, not here. |
| **Scale model** | What is the bottleneck? | The scheduler loop. Everything flows through it: DAG parsing, run creation, dependency evaluation, task dispatch. A slow scheduler means delayed pipelines. The design must make the scheduler loop fast and parallelizable. |
| **Executor** | Can users bring their own executor? | Yes. The executor is behind a pluggable interface. The engine ships with LocalExecutor, CeleryExecutor, KubernetesExecutor, and a DBOS-style TransactionalExecutor. Custom executors implement the same interface. |
| **Multi-tenancy** | Is this multi-tenant? | DAGs are namespaced by `owner` and `tags`. Hard multi-tenancy (separate metadata DBs, executor pools per tenant) is an enterprise feature. The base design supports soft isolation via pools, priority, and RBAC. |

### Key Assumptions

1. **The scheduler is the brain; executors are the hands.** The scheduler decides what runs when. Executors know nothing about dependencies, schedules, or DAG structure — they receive "run this task" and report back "succeeded/failed."
2. **DAG files are code, not configuration.** Parsing a DAG file means executing Python. This is powerful (dynamic DAG generation, conditional logic, imports) but dangerous (import errors, infinite loops, resource abuse). The parser must be sandboxed.
3. **The metadata DB is the source of truth for all orchestration state.** Task instance states, XCom values, run status — everything is in the DB. If the scheduler crashes, it can reconstruct the full state of every run by querying the DB.
4. **Backfill is not a special case; it is the normal case run retroactively.** The engine must treat historical runs identically to scheduled runs, except for the trigger mechanism.
5. **At-least-once execution is the default guarantee.** Tasks may run more than once (scheduler crash after dispatch but before state update). Tasks that require exactly-once must use the DBOS transactional executor or application-level idempotency.

### What We Are Explicitly Not Building

- **Not** a durable execution engine. If you need deterministic replay, saga compensation, or month-long stateful workflows, use Temporal (see [`solutions/workflow-orchestration-design.md`](workflow-orchestration-design.md)).
- **Not** a streaming system. This is batch-oriented: each task runs to completion. For streaming, use Flink or Kafka Streams.
- **Not** a notebook execution environment. Tasks are code deployed to a shared volume, not interactive notebooks (though tasks can invoke notebook execution as a side-effect).

---

## 2. Capacity Estimates

### Core Scale Numbers

| Quantity | Value | Derivation |
|---|---|---|
| Total DAGs | 10,000 | Stated NFR |
| DAGs with active schedules | ~3,000 | ~30% are paused, experimental, or deprecated |
| Task instances scheduled/hour | 100,000 | Stated NFR |
| Avg tasks per DAG | ~15 | Simple: 5-10, complex: 50-200 |
| Avg DAG runs/day per active DAG | ~4 | Mix of hourly (24), daily (1), weekly (0.14) |
| Total DAG runs/day | ~12,000 | 3,000 active × 4 avg |
| Total task instances/day | ~180,000 | 12,000 runs × 15 tasks avg |
| Concurrent running task instances | ~2,000 | 100K/hour ÷ 60 min × 1.2 min avg duration |
| Metadata DB writes/sec | ~150 | 180K TI state transitions/day ÷ 86,400 × 3 transitions per TI + overhead |

### Storage Sizing

```
Metadata DB:
  Task instances (90-day retention):
    Records:          180K/day × 90 days = 16.2M rows
    Avg row size:     800 bytes
    Data:             16.2M × 800B = ~13 GB
    With indexes:     ~30 GB

  DAG runs (90-day retention):
    Records:          12K/day × 90 days = 1.08M rows
    Avg row size:     500 bytes
    Data:             ~540 MB

  XCom (90-day retention):
    Records:          ~50% of TIs push XCom = 8.1M rows
    Avg value size:   2 KB (metadata DB backend, small values only)
    Data:             8.1M × 2 KB = ~16 GB
    NOTE: This is why XCom in the metadata DB is a problem at scale

  Task logs:
    Log entries:      180K/day × 50 KB avg = 9 GB/day
    90-day retention: ~810 GB (stored in object storage, not metadata DB)

  Total metadata DB: ~60 GB (manageable for PostgreSQL)
```

### Scheduler Performance Budget

```
Scheduler loop iteration (one "heartbeat"):
  Parse DAG files:          0 ms (async, separate process pool)
  Create pending DAG runs:  ~50 ms (query schedules, insert runs)
  Evaluate task instances:  ~200 ms (query all queued TIs, check deps)
  Dispatch to executor:     ~50 ms (enqueue to Celery / create K8s pods)
  Process executor events:  ~100 ms (state callbacks from completed tasks)
  Total:                    ~400 ms target (well under the 5s dispatch NFR)

  At 2,000 concurrent TIs:
    State checks/heartbeat: 2,000 (one per running TI, batched)
    DB queries/heartbeat:   ~20 (batched reads + writes)
```

---

## 3. High-Level Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │               Web Server                      │
                    │  (REST API, UI, Authentication, RBAC)         │
                    └──────────┬───────────────────────────────────┘
                               │
                    ┌──────────┴───────────────────────────────────┐
                    │            Metadata Database                  │
                    │         (PostgreSQL / MySQL)                  │
                    │                                               │
                    │  ┌──────────┐ ┌──────────┐ ┌──────────────┐  │
                    │  │ dag_run   │ │ task_    │ │ xcom         │  │
                    │  │          │ │ instance │ │              │  │
                    │  └──────────┘ └──────────┘ └──────────────┘  │
                    │  ┌──────────┐ ┌──────────┐ ┌──────────────┐  │
                    │  │ dag      │ │ pool     │ │ connection   │  │
                    │  │ (serial.)│ │          │ │              │  │
                    │  └──────────┘ └──────────┘ └──────────────┘  │
                    └──────────┬──────────────────┬────────────────┘
                               │                  │
              ┌────────────────┘                  └───────────────┐
              │                                                   │
    ┌─────────┴──────────┐                          ┌─────────────┴──────┐
    │     Scheduler       │                          │     Triggerer       │
    │  (DAG Processor     │                          │  (Async event loop │
    │   Pool + Scheduling │                          │   for deferrable   │
    │   Loop)             │                          │   operators)       │
    │                     │                          └────────────────────┘
    │  ┌───────────────┐  │
    │  │ DAG Processor │  │       ┌───────────────────────────────────┐
    │  │ Pool (N procs)│──┼──────▶│         DAG Files Volume          │
    │  └───────────────┘  │       │  (Shared filesystem / Git-sync)   │
    │                     │       └───────────────────────────────────┘
    │  ┌───────────────┐  │
    │  │ Scheduling    │  │
    │  │ Loop          │──┼──────────┐
    │  └───────────────┘  │          │
    └─────────────────────┘          │
                                     │  Dispatch
              ┌──────────────────────┘
              │
              ▼
    ┌───────────────────────────────────────────────────────────┐
    │                    Executor (pluggable)                    │
    │                                                           │
    │  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐  │
    │  │ Local        │  │ Celery       │  │ Kubernetes     │  │
    │  │ Executor     │  │ Executor     │  │ Executor       │  │
    │  │ (subprocess) │  │ (Redis/RMQ)  │  │ (pod-per-task) │  │
    │  └──────┬───────┘  └──────┬───────┘  └──────┬─────────┘  │
    │         │                 │                  │            │
    └─────────┼─────────────────┼──────────────────┼────────────┘
              │                 │                  │
              ▼                 ▼                  ▼
    ┌──────────┐     ┌──────────────┐     ┌──────────────────┐
    │ Local    │     │ Celery       │     │ K8s Pods         │
    │ Process  │     │ Workers      │     │ (one per task    │
    │          │     │ (fleet)      │     │  instance)       │
    └──────────┘     └──────────────┘     └──────────────────┘
```

### Component Responsibilities

| Component | Responsibility |
|---|---|
| **Web Server** | REST API, Web UI (DAG graph, task logs, Gantt chart), authentication, RBAC. Stateless — reads from metadata DB. Horizontally scalable. |
| **Scheduler** | The brain. Two sub-systems: (1) **DAG Processor Pool** — parses DAG files in isolated subprocesses, writes serialized DAGs to DB. (2) **Scheduling Loop** — creates DAG runs, evaluates task dependencies, dispatches runnable tasks to executor. |
| **Triggerer** | Async event loop that monitors deferrable triggers (external conditions). When a trigger fires, it wakes the deferred task instance. Runs as a separate deployment. |
| **Executor** | Pluggable task runner. Receives "execute this task instance" from the scheduler, runs it, reports back success/failure. Each executor type has different scale, isolation, and lifecycle characteristics. |
| **Metadata DB** | PostgreSQL. Single source of truth for all orchestration state. Schema is designed for the scheduler's query patterns: "give me all runnable task instances across all active DAG runs." |
| **DAG Files Volume** | Shared filesystem (NFS, EFS, PVC) or Git-sync sidecar. Contains the Python DAG files. Must be accessible to scheduler (for parsing) and workers (for task execution). |
| **Log Storage** | Task execution logs. Short-term in local filesystem, long-term in object storage (S3, GCS). Web server reads from both for display. |

---

## 4. DAG Definition and Parsing

### How Users Define DAGs

```python
from datetime import datetime, timedelta
from airflow.decorators import dag, task
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.operators.postgres import PostgresOperator

@dag(
    dag_id="daily_etl_pipeline",
    schedule="0 6 * * *",          # 6 AM UTC daily
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
        "execution_timeout": timedelta(hours=1),
        "pool": "etl_pool",
    },
    tags=["etl", "warehouse"],
)
def daily_etl_pipeline():

    @task()
    def extract_orders():
        """Pull today's orders from the source DB."""
        # ... returns a list of order dicts
        return orders

    @task()
    def extract_customers():
        """Pull customer updates."""
        return customers

    @task()
    def transform(orders, customers):
        """Join and clean data."""
        # orders and customers are XCom values from upstream tasks
        return transformed_data

    @task()
    def load(data):
        """Write to warehouse."""
        write_to_warehouse(data)

    # Define dependency graph
    orders = extract_orders()
    customers = extract_customers()
    transformed = transform(orders, customers)
    load(transformed)

daily_etl_pipeline()
```

### What Happens at Parse Time

```
┌──────────────────────────────────────────────────────────────────┐
│  DAG File Processing Pipeline                                     │
│                                                                   │
│  1. Scheduler's DAG Processor Pool spawns a subprocess            │
│                                                                   │
│  2. Subprocess imports the DAG file:                              │
│     exec(compile(source, filename, 'exec'), namespace)            │
│     → Python executes the file, which calls @dag decorator        │
│     → Decorator builds a DAG object with task nodes + edges       │
│                                                                   │
│  3. DAG validation:                                               │
│     a. Check for cycles (topological sort)                        │
│     b. Validate task_ids are unique within the DAG                │
│     c. Validate pool references exist                             │
│     d. Validate schedule expression is parseable                  │
│     e. Check start_date is set                                    │
│                                                                   │
│  4. Serialize the DAG:                                            │
│     DAG object → JSON/Protobuf → stored in metadata DB            │
│     (dag table: dag_id, schedule, is_paused, fileloc,             │
│      owners, tags, serialized_dag BLOB)                           │
│                                                                   │
│  5. Record parse metrics:                                         │
│     - Parse duration                                              │
│     - Number of tasks                                             │
│     - Import errors (stored in import_error table)                │
│                                                                   │
│  Parse errors in one DAG file do NOT affect other DAGs.            │
│  Each file is parsed in isolation.                                │
└──────────────────────────────────────────────────────────────────┘
```

### The DAG Processor Pool

The scheduler does NOT parse DAG files in the main scheduling loop. DAG parsing is offloaded to a pool of **worker subprocesses** for isolation and performance:

```
┌──────────────────────────────────────────────────────────────────┐
│  DAG Processor Pool                                               │
│                                                                   │
│  Scheduler process:                                               │
│    - Maintains a list of DAG file paths                           │
│    - Assigns files to processor subprocesses round-robin          │
│    - Each file is re-parsed every min_file_process_interval       │
│      (default 30s)                                                │
│                                                                   │
│  Processor subprocess #1:    ┌──────────────────────────────┐     │
│    dag_file_a.py ──parse──▶  │ DAG object → serialize       │     │
│    dag_file_b.py ──parse──▶  │ → write to metadata DB       │     │
│    dag_file_c.py ──parse──▶  │ → report errors to DB        │     │
│                              └──────────────────────────────┘     │
│                                                                   │
│  Processor subprocess #2:                                         │
│    dag_file_d.py ──parse──▶  ...                                  │
│    dag_file_e.py ──parse──▶  ...                                  │
│                                                                   │
│  Config:                                                          │
│    parsing_processes = 4        (number of parser subprocesses)    │
│    min_file_process_interval = 30s  (re-parse interval per file)  │
│    dag_dir_list_interval = 300s     (rescan for new DAG files)    │
│    dagbag_import_timeout = 30s      (timeout per file import)     │
│                                                                   │
│  At 10,000 DAG files with 4 processors:                           │
│    Files per processor: 2,500                                     │
│    At 100ms avg parse time: 2,500 × 0.1s = 250s per full scan    │
│    With 30s interval: a file is re-parsed every ~250s             │
│    → 4-minute lag for DAG changes to take effect (acceptable)     │
│                                                                   │
│  Optimization: track file modification time (mtime).              │
│  Only re-parse files whose mtime changed since last parse.        │
│  Reduces per-cycle parse count from 10,000 to ~50 (changed files) │
└──────────────────────────────────────────────────────────────────┘
```

### Why Parsing Is Dangerous

DAG files are Python code. Importing them executes arbitrary code. Risks:

| Risk | What Happens | Mitigation |
|---|---|---|
| Infinite loop at import time | Parser subprocess hangs | `dagbag_import_timeout` kills the subprocess after 30s |
| Import error (missing module) | DAG is not loaded; other DAGs unaffected | Error recorded in `import_error` table, shown in UI |
| Heavy computation at import | Slow parse, blocks the processor | Parse in subprocess, enforce timeout, log duration |
| Database/network calls at import | Parse depends on external system availability | Discouraged in docs; parse timeout catches runaway calls |
| Memory leak in DAG code | Subprocess memory grows | Subprocess is recycled after N parses |

---

## 5. The Scheduler Loop

The scheduler loop is the heartbeat of the orchestration engine. It runs continuously, evaluating what needs to happen next.

### Scheduler Loop Pseudocode

```python
class Scheduler:
    def run(self):
        while True:
            self._create_dag_runs()
            self._schedule_task_instances()
            self._process_executor_events()
            self._check_timeouts()
            sleep(self.scheduler_heartbeat_sec)  # default: 5s

    def _create_dag_runs(self):
        """Create DAG runs for DAGs whose schedule has elapsed."""
        active_dags = db.query("""
            SELECT dag_id, schedule_interval, last_run_logical_date,
                   max_active_runs, next_dagrun_create_after
            FROM dag
            WHERE is_paused = FALSE
              AND next_dagrun_create_after <= NOW()
        """)

        for dag in active_dags:
            active_runs = db.count(
                "dag_run WHERE dag_id = %s AND state = 'running'",
                dag.dag_id
            )
            if active_runs >= dag.max_active_runs:
                continue

            logical_date = dag.compute_next_logical_date()
            db.insert("dag_run", {
                "dag_id": dag.dag_id,
                "run_id": f"scheduled__{logical_date.isoformat()}",
                "logical_date": logical_date,
                "state": "queued",
                "run_type": "scheduled",
            })
            # Create task instances for all tasks in this DAG
            for task in dag.tasks:
                db.insert("task_instance", {
                    "dag_id": dag.dag_id,
                    "task_id": task.task_id,
                    "run_id": run_id,
                    "state": None,  # "no_status" — not yet evaluated
                })
            # Update next schedule
            dag.update_next_dagrun_create_after(logical_date)

    def _schedule_task_instances(self):
        """Find task instances that are ready to run and dispatch them."""
        # Query: all task instances in "scheduled" or None state
        # whose upstream dependencies are satisfied
        schedulable = db.query("""
            SELECT ti.dag_id, ti.task_id, ti.run_id, ti.state,
                   ti.pool, ti.priority_weight
            FROM task_instance ti
            JOIN dag_run dr ON ti.dag_id = dr.dag_id AND ti.run_id = dr.run_id
            WHERE dr.state = 'running'
              AND ti.state IN (NULL, 'scheduled', 'up_for_retry')
            ORDER BY ti.priority_weight DESC, ti.logical_date ASC
        """)

        for ti in schedulable:
            # Check dependencies (upstream task states)
            if not self._are_dependencies_met(ti):
                if ti.state is None:
                    ti.state = "upstream_failed"  # or keep as None
                continue

            # Check pool slot availability
            pool = pools[ti.pool]
            if pool.open_slots <= 0:
                continue  # wait for a slot

            # Check concurrency limits
            if not self._check_concurrency(ti):
                continue

            # Dispatch to executor
            ti.state = "queued"
            db.update(ti)
            self.executor.queue_task_instance(ti)

    def _process_executor_events(self):
        """Process callbacks from the executor (task completed/failed)."""
        events = self.executor.get_event_buffer()
        for task_key, state, info in events:
            ti = db.get_task_instance(task_key)
            if state == State.SUCCESS:
                ti.state = "success"
                ti.end_date = now()
            elif state == State.FAILED:
                if ti.try_number < ti.max_retries:
                    ti.state = "up_for_retry"
                    ti.next_retry_at = now() + ti.retry_delay
                else:
                    ti.state = "failed"
                    ti.end_date = now()
            db.update(ti)
            # Check if all TIs in the run are done → update DAG run state
            self._update_dag_run_state(ti.dag_id, ti.run_id)

    def _check_timeouts(self):
        """Kill task instances that exceeded their execution_timeout."""
        timed_out = db.query("""
            SELECT * FROM task_instance
            WHERE state = 'running'
              AND start_date + execution_timeout < NOW()
        """)
        for ti in timed_out:
            self.executor.terminate(ti)
            ti.state = "failed"
            ti.end_date = now()
            ti.error = "Task exceeded execution_timeout"
            db.update(ti)
```

### The Scheduling Loop's Critical Path

```
┌─────────────────────────────────────────────────────────────────┐
│  One Scheduler Heartbeat (~400ms target)                         │
│                                                                  │
│  1. Create DAG Runs [~50ms]                                      │
│     Query: SELECT from dag WHERE next_dagrun_create_after <= NOW()│
│     For each eligible DAG: INSERT dag_run + task_instances        │
│     Batched: single query for eligible DAGs, bulk insert for TIs │
│                                                                  │
│  2. Evaluate Task Instances [~200ms]                             │
│     Query: SELECT from task_instance WHERE state ∈ schedulable   │
│     For each TI:                                                 │
│       a. Check upstream dependency states (single query, cached) │
│       b. Check pool slot availability (in-memory counter)        │
│       c. Check concurrency limits (in-memory counters)           │
│       d. If all pass → mark as "queued", dispatch to executor    │
│     Batched: bulk UPDATE for state transitions                   │
│                                                                  │
│  3. Process Executor Events [~100ms]                             │
│     Read completed/failed events from executor callback queue    │
│     Update task_instance states                                  │
│     Update dag_run states (if all TIs done)                      │
│                                                                  │
│  4. Check Timeouts [~50ms]                                       │
│     Query: running TIs past execution_timeout                    │
│     Issue kill commands to executor                              │
│                                                                  │
│  Total DB queries per heartbeat: ~20 (heavily batched)           │
│  Target heartbeat interval: 5 seconds                            │
│  Worst-case task dispatch latency: ~5 seconds (one heartbeat)    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Scaling the Scheduler

For 10,000+ DAGs, a single scheduler process is a bottleneck. Solutions:

**HA Scheduler (Active-Active):**

Multiple scheduler instances run concurrently, each processing a subset of DAGs. They use row-level locking to prevent double-processing:

```sql
-- Each scheduler instance claims a batch of DAG runs to evaluate
SELECT * FROM dag_run
WHERE state = 'running'
  AND dag_id IN (SELECT dag_id FROM dag WHERE is_paused = FALSE)
ORDER BY logical_date
LIMIT 100
FOR UPDATE SKIP LOCKED;
```

`FOR UPDATE SKIP LOCKED` ensures two scheduler instances never process the same DAG run simultaneously. Each scheduler processes ~100 runs per heartbeat, and 3 schedulers together cover 300 runs per heartbeat.

**DAG Sharding:**

Assign DAGs to scheduler instances via consistent hashing:

```
scheduler_instance = hash(dag_id) % num_schedulers
```

Each scheduler only evaluates its assigned DAGs. This eliminates lock contention entirely but requires rebalancing when a scheduler dies.

---

## 6. Task Instance State Machine

### States and Transitions

```
                                 ┌──────────────────┐
                                 │   no_status       │
                                 │   (initial)       │
                                 └────────┬──────────┘
                                          │
                            Scheduler evaluates dependencies
                                          │
                         ┌────────────────┼────────────────┐
                         │                │                │
                         ▼                ▼                ▼
                  ┌─────────────┐  ┌────────────┐  ┌──────────────┐
                  │ scheduled    │  │ upstream_  │  │ skipped       │
                  │ (deps met,  │  │ failed     │  │ (branch not  │
                  │  ready)     │  │            │  │  taken)       │
                  └──────┬──────┘  └────────────┘  └──────────────┘
                         │
                   Pool slot available +
                   concurrency limit OK
                         │
                         ▼
                  ┌─────────────┐
                  │ queued       │
                  │ (in executor │
                  │  queue)     │
                  └──────┬──────┘
                         │
                   Executor picks up task
                         │
                         ▼
                  ┌─────────────┐
                  │ running      │
                  │              │
                  └──┬──────┬───┘
                     │      │
              Success│      │Failure
                     │      │
                     ▼      ▼
              ┌────────┐ ┌─────────────────┐
              │success │ │ up_for_retry    │──── retries remain
              └────────┘ │ (wait backoff)  │
                         └────────┬────────┘
                                  │
                            Retry after delay
                                  │
                                  ▼
                         ┌─────────────┐
                         │ scheduled    │  (re-enters the loop)
                         └─────────────┘
                                  │
                            Max retries exceeded
                                  │
                                  ▼
                         ┌─────────────┐
                         │ failed       │
                         └─────────────┘

  Special transitions:
    Any state → removed     (task removed from DAG definition)
    Any state → restarting  (cleared for rerun by user)
    running → deferred      (deferrable operator yields to Triggerer)
    deferred → scheduled    (trigger fires, task re-enters queue)
```

### State Definitions

| State | Meaning |
|---|---|
| `no_status` | Task instance created but not yet evaluated by scheduler |
| `scheduled` | Dependencies met; waiting for pool slot / concurrency budget |
| `queued` | Sent to executor; waiting for worker to pick up |
| `running` | Executing on a worker |
| `success` | Completed successfully |
| `failed` | Failed after exhausting all retries |
| `up_for_retry` | Failed, waiting for retry delay to elapse |
| `upstream_failed` | An upstream dependency failed (trigger rule dependent) |
| `skipped` | Skipped by a branching operator or short-circuit |
| `deferred` | Yielded to Triggerer, waiting for async event |
| `removed` | Task no longer exists in DAG definition |
| `restarting` | User cleared this TI for re-execution |

---

## 7. Dependency Resolution and Trigger Rules

### How the Scheduler Evaluates Dependencies

For each task instance in a DAG run, the scheduler checks the states of all upstream task instances (within the same run) and applies the task's **trigger rule** to decide if the task should proceed:

```python
def are_dependencies_met(ti: TaskInstance, dag: DAG) -> bool:
    upstream_task_ids = dag.get_upstream(ti.task_id)
    upstream_states = db.query("""
        SELECT task_id, state FROM task_instance
        WHERE dag_id = %s AND run_id = %s AND task_id = ANY(%s)
    """, [ti.dag_id, ti.run_id, upstream_task_ids])

    # Apply trigger rule
    return evaluate_trigger_rule(ti.trigger_rule, upstream_states)


def evaluate_trigger_rule(rule, upstream_states):
    successes = sum(1 for s in upstream_states if s == 'success')
    failures  = sum(1 for s in upstream_states if s == 'failed')
    upfailed  = sum(1 for s in upstream_states if s == 'upstream_failed')
    skipped   = sum(1 for s in upstream_states if s == 'skipped')
    done      = sum(1 for s in upstream_states if s in TERMINAL_STATES)
    total     = len(upstream_states)

    if total == 0:
        return True  # no upstream → always runnable (root task)

    match rule:
        case "all_success":
            return successes == total
        case "all_failed":
            return failures == total
        case "all_done":
            return done == total
        case "one_success":
            return successes >= 1
        case "one_failed":
            return failures >= 1
        case "none_failed":
            return failures == 0 and done == total
        case "none_skipped":
            return skipped == 0 and done == total
        case "always":
            return True
```

### Trigger Rule Scenarios

```
DAG:  extract_A ──┐
                   ├──▶ transform ──▶ load
      extract_B ──┘

Scenario 1: transform.trigger_rule = "all_success" (default)
  extract_A: success
  extract_B: success
  → transform: RUNS (both upstream succeeded)

  extract_A: success
  extract_B: failed
  → transform: upstream_failed (not all upstream succeeded)

Scenario 2: transform.trigger_rule = "one_success"
  extract_A: success
  extract_B: failed
  → transform: RUNS (at least one upstream succeeded)

Scenario 3: transform.trigger_rule = "all_done"
  extract_A: success
  extract_B: failed
  → transform: RUNS (all upstream are in terminal state, regardless of outcome)

Scenario 4: transform.trigger_rule = "none_failed"
  extract_A: success
  extract_B: skipped
  → transform: RUNS (no upstream failed — skipped is not a failure)
```

### Cross-DAG Dependencies

Some tasks depend on tasks in other DAGs (e.g., "run quality checks only after the ETL DAG finishes"). This is handled via **ExternalTaskSensor**:

```python
wait_for_etl = ExternalTaskSensor(
    task_id="wait_for_etl",
    external_dag_id="daily_etl_pipeline",
    external_task_id="load",
    execution_date_fn=lambda dt: dt,  # same logical date
    timeout=3600,
    poke_interval=60,
)
```

The sensor polls the metadata DB for the external task's state. This creates implicit coupling between DAGs — a change to the upstream DAG's schedule can silently break the dependency. Dataset-driven scheduling (Section 15) is the modern solution.

---

## 8. Executor Architecture

### The Executor Interface

```python
class BaseExecutor(ABC):
    @abstractmethod
    def start(self):
        """Initialize the executor (connect to broker, start watchers)."""

    @abstractmethod
    def queue_task_instance(self, ti: TaskInstance):
        """Queue a task instance for execution."""

    @abstractmethod
    def get_event_buffer(self) -> list[tuple[TaskInstanceKey, State, str]]:
        """Return completed/failed task events since last call."""

    @abstractmethod
    def terminate(self, ti: TaskInstance):
        """Kill a running task instance (timeout, cancellation)."""

    @abstractmethod
    def end(self):
        """Shut down the executor."""
```

### Executor Comparison

| Dimension | LocalExecutor | CeleryExecutor | KubernetesExecutor |
|---|---|---|---|
| **How it runs tasks** | `fork()` a subprocess on the scheduler host | Sends task to Celery worker via Redis/RabbitMQ broker | Creates a K8s pod per task instance |
| **Concurrency** | Limited by scheduler host CPU/memory | Limited by worker fleet size | Limited by K8s cluster capacity |
| **Isolation** | Process-level (shared host) | Worker-level (separate hosts) | Pod-level (container isolation, resource limits) |
| **Scale ceiling** | ~32 concurrent tasks | ~1,000 concurrent tasks | ~5,000+ concurrent pods |
| **Startup latency** | ~10ms (fork) | ~50ms (Celery dispatch) | ~10-30s (pod scheduling + image pull) |
| **Resource control** | None (shares scheduler resources) | Worker-level | Per-task CPU/memory requests and limits |
| **Failure blast radius** | Task crash can affect scheduler stability | Task crash affects only that worker | Pod crash is fully isolated |
| **When to use** | Dev/test, small deployments (<50 tasks concurrent) | Medium scale, shared worker fleet | Large scale, heterogeneous resource needs, strict isolation |

### CeleryExecutor Flow

```
┌───────────────┐    Celery task     ┌──────────────┐     Worker picks up
│   Scheduler    │──── message ────▶│ Redis/RabbitMQ│────── message ────▶
│                │                  │   Broker      │
│  queue_task_   │                  └──────────────┘     ┌──────────────┐
│  instance(ti)  │                                       │ Celery Worker │
│                │                                       │              │
│                │◀── result callback (state) ──────────│  execute(ti) │
└───────────────┘                                       └──────────────┘

Details:
  1. Scheduler serializes: (dag_id, task_id, run_id, logical_date, try_number)
  2. Sends as Celery task to queue named by ti.queue (default: "default")
  3. Worker deserializes, loads DAG from DAG files, instantiates task
  4. Worker executes task.execute(context)
  5. Worker updates task_instance state in metadata DB directly
  6. Worker sends result event back to scheduler via result backend

Failure modes:
  - Broker down → tasks are not dispatched → scheduler retries on next heartbeat
  - Worker crashes mid-task → Celery's visibility timeout re-queues the message
    → BUT metadata DB already says "running" → reaper detects stale "running" tasks
  - Worker completes but result callback lost → scheduler sees "running" forever
    → Reaper picks it up after timeout
```

### LocalExecutor Flow

```python
class LocalExecutor(BaseExecutor):
    def __init__(self, parallelism=32):
        self.parallelism = parallelism
        self.manager = multiprocessing.Manager()
        self.result_queue = self.manager.Queue()

    def queue_task_instance(self, ti):
        proc = multiprocessing.Process(
            target=self._execute_in_subprocess,
            args=(ti.key, ti.command)
        )
        proc.start()
        self.running[ti.key] = proc

    def _execute_in_subprocess(self, key, command):
        try:
            # Re-import DAG, instantiate task, call execute()
            result = execute_task_command(command)
            self.result_queue.put((key, State.SUCCESS, ""))
        except Exception as e:
            self.result_queue.put((key, State.FAILED, str(e)))

    def get_event_buffer(self):
        events = []
        while not self.result_queue.empty():
            events.append(self.result_queue.get_nowait())
        return events
```

---

## 9. KubernetesExecutor Deep Dive

### Pod-Per-Task Model

The KubernetesExecutor creates a dedicated Kubernetes pod for each task instance. This provides:

- **Resource isolation**: each task gets its own CPU/memory limits
- **Heterogeneous execution**: different tasks can use different images, GPUs, node selectors
- **Auto-scaling**: cluster autoscaler provisions nodes as pods are scheduled
- **Clean environment**: no state leakage between tasks

```
┌───────────────────────────────────────────────────────────────┐
│  KubernetesExecutor Flow                                       │
│                                                                │
│  1. Scheduler calls queue_task_instance(ti)                    │
│                                                                │
│  2. Executor builds a Pod spec:                                │
│     apiVersion: v1                                             │
│     kind: Pod                                                  │
│     metadata:                                                  │
│       name: daily-etl-extract-orders-2025-03-15-abc123         │
│       labels:                                                  │
│         dag_id: daily_etl_pipeline                             │
│         task_id: extract_orders                                │
│         run_id: scheduled__2025-03-15                          │
│     spec:                                                      │
│       containers:                                              │
│         - name: base                                           │
│           image: my-airflow:2.9.0                              │
│           command: ["airflow", "tasks", "run",                 │
│                     "daily_etl_pipeline", "extract_orders",    │
│                     "scheduled__2025-03-15"]                   │
│           resources:                                           │
│             requests: { cpu: "500m", memory: "1Gi" }           │
│             limits:   { cpu: "2",    memory: "4Gi" }           │
│           volumeMounts:                                        │
│             - name: dags                                       │
│               mountPath: /opt/airflow/dags                     │
│       volumes:                                                 │
│         - name: dags                                           │
│           persistentVolumeClaim:                                │
│             claimName: airflow-dags                             │
│       restartPolicy: Never                                     │
│                                                                │
│  3. Executor calls K8s API: create_namespaced_pod(pod_spec)    │
│                                                                │
│  4. K8s schedules pod → pod runs → task executes               │
│                                                                │
│  5. Executor's watcher (K8s watch API) detects pod termination:│
│     - Pod succeeded (exit code 0) → report SUCCESS             │
│     - Pod failed (exit code != 0) → report FAILED              │
│     - Pod OOMKilled → report FAILED + error "OOMKilled"        │
│     - Pod evicted → report FAILED + error "Evicted"            │
│                                                                │
│  6. Executor collects pod logs (kubectl logs <pod>)            │
│     → streams to log storage                                   │
│                                                                │
│  7. Executor deletes the pod (cleanup)                         │
│     OR: leaves pod for debugging if delete_worker_pods=False   │
│                                                                │
└───────────────────────────────────────────────────────────────┘
```

### Pod Lifecycle Challenges

| Challenge | What Happens | Mitigation |
|---|---|---|
| **Image pull latency** | First task on a node pulls the image (30s-2min) | Pre-pull images via DaemonSet; use `imagePullPolicy: IfNotPresent` |
| **Pod scheduling delay** | No available node → pod Pending | Cluster autoscaler provisions node (2-5 min); use priority classes |
| **OOMKilled** | Task exceeds memory limit → killed by kubelet | Set memory limits based on profiling; report OOMKilled as a distinct failure |
| **Spot/preemptible eviction** | Node is reclaimed → pod evicted | Use `terminationGracePeriodSeconds`; mark as retry-eligible |
| **Log loss** | Pod deleted before logs collected | Collect logs before deletion; use sidecar log shipping |
| **Zombie pods** | Executor crashes → pods orphaned (no one watching) | Startup reconciliation: on restart, find all pods with the executor's labels, reconcile with metadata DB state |

### Startup Reconciliation

When the scheduler/executor restarts, it must reconcile:

```python
def reconcile_on_startup(self):
    # Find all pods we created that are still running
    pods = k8s.list_namespaced_pod(
        namespace="airflow",
        label_selector="executor=kubernetes,dag_id"
    )

    for pod in pods:
        ti_key = extract_ti_key(pod.metadata.labels)
        db_state = db.get_task_instance_state(ti_key)

        if pod.status.phase == "Succeeded" and db_state == "running":
            # Pod finished but we missed the callback → update DB
            db.update_task_instance(ti_key, state="success")
            k8s.delete_pod(pod)

        elif pod.status.phase == "Failed" and db_state == "running":
            db.update_task_instance(ti_key, state="failed")
            k8s.delete_pod(pod)

        elif pod.status.phase == "Running" and db_state == "running":
            # Still running — resume watching
            self.watch(pod)

        elif db_state in ("success", "failed"):
            # DB already has terminal state — delete orphaned pod
            k8s.delete_pod(pod)
```

---

## 10. DBOS-Style Transactional Execution

### The Problem with At-Least-Once

Standard executors provide at-least-once execution. The scheduler dispatches a task, the worker executes it, and the worker reports the result. But:

```
Timeline:
  t=0:   Scheduler dispatches task "charge_customer"
  t=1:   Worker executes: calls payment API → customer is charged $50
  t=2:   Worker crashes BEFORE reporting success to metadata DB
  t=3:   Scheduler sees task still "running" → times out → retries
  t=4:   New worker executes: calls payment API → customer charged AGAIN
  Result: customer charged $100 instead of $50
```

Traditional fix: make the task idempotent (use an idempotency key with the payment API). This works but pushes the complexity to every task author.

### DBOS: The Database IS the Execution Engine

DBOS (Database-Oriented Operating System) takes a radical approach: **every step of task execution is wrapped in a database transaction**. The database is not just metadata — it is the execution log, the state machine, and the idempotency guarantee.

```python
# DBOS-style task execution
from dbos import DBOS, step, workflow

@workflow
def charge_and_notify(order_id: str, amount: float):
    # Step 1: Charge the customer
    # This step's execution is recorded in the DB BEFORE the side-effect
    tx_id = charge_customer(order_id, amount)

    # Step 2: Send notification
    send_notification(order_id, tx_id)

@step
def charge_customer(order_id: str, amount: float) -> str:
    """Each @step is wrapped in a DB transaction that records:
    - function name, inputs, output
    - execution status (started, completed, failed)
    - idempotency key (function_name + inputs hash)

    If this step was already executed (recorded in DB), the recorded
    result is returned WITHOUT re-executing the function.
    """
    response = payment_api.charge(order_id, amount)
    return response.transaction_id

@step
def send_notification(order_id: str, tx_id: str):
    email_api.send(order_id, f"Payment {tx_id} confirmed")
```

### How Transactional Execution Achieves Exactly-Once

```
┌──────────────────────────────────────────────────────────────────┐
│  DBOS Execution Model                                            │
│                                                                   │
│  For each @step:                                                  │
│                                                                   │
│  1. BEGIN TRANSACTION                                             │
│     INSERT INTO step_executions (                                 │
│       workflow_id, step_name, inputs_hash, status                 │
│     ) VALUES (..., 'started')                                     │
│     ON CONFLICT (workflow_id, step_name, inputs_hash) DO NOTHING  │
│     COMMIT                                                        │
│                                                                   │
│  2. Check if step already completed:                              │
│     SELECT output FROM step_executions                             │
│     WHERE workflow_id = X AND step_name = Y AND status = 'done'   │
│                                                                   │
│     IF found → return recorded output (skip execution)            │
│     IF not → proceed to execute                                   │
│                                                                   │
│  3. Execute the step function (the actual side-effect)            │
│                                                                   │
│  4. BEGIN TRANSACTION                                             │
│     UPDATE step_executions SET                                    │
│       status = 'done', output = <serialized result>               │
│     WHERE workflow_id = X AND step_name = Y                       │
│     COMMIT                                                        │
│                                                                   │
│  If the worker crashes between steps 3 and 4:                     │
│    - Step is recorded as 'started' but not 'done'                 │
│    - On retry, step 2 finds 'started' → re-executes step 3       │
│    - The side-effect may happen twice (payment charged twice)     │
│    - BUT: the step function should use the inputs_hash as an      │
│      idempotency key with the external API                        │
│                                                                   │
│  If the worker crashes between steps 1 and 3:                     │
│    - Step is recorded as 'started', never executed                │
│    - On retry, step 2 finds 'started' → executes step 3          │
│    - No double-execution                                          │
│                                                                   │
│  Key insight: the DB transaction makes the state update atomic    │
│  with the execution record. There is no window where the step     │
│  executed but the system doesn't know about it.                   │
│                                                                   │
│  For PURE DATABASE operations (INSERT/UPDATE/DELETE):             │
│    The step's side-effect IS a DB operation → wrap it in the      │
│    SAME transaction as the execution record → truly atomic         │
│    → exactly-once, no idempotency key needed                      │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

### DBOS vs Traditional Executor

| Dimension | Traditional (Celery/K8s) | DBOS Transactional |
|---|---|---|
| Execution guarantee | At-least-once | Exactly-once for DB ops; at-least-once for external calls (with idempotency) |
| State tracking | Metadata DB updated after execution | State transitions are atomic with execution |
| Ghost runs | Possible (worker crashed after side-effect) | Eliminated for DB ops |
| Overhead | Low (one DB write per state change) | Higher (DB transaction per step) |
| Scale | Thousands of concurrent tasks | Hundreds (DB transaction throughput is the limit) |
| Use case | General-purpose pipelines | Financial, compliance, data-critical pipelines |

### When to Use DBOS-Style

- Payment processing pipelines where double-charging is unacceptable
- Regulatory reporting where every step must be auditable and non-duplicatable
- Data pipelines where idempotency is hard to achieve at the application level
- Any pipeline where "just retry" has costly consequences

---

## 11. XCom and Data Passing

### How XCom Works

XCom (cross-communication) is the mechanism for passing data between tasks within a DAG run.

```python
# Task A pushes a value
@task()
def extract():
    data = query_source_db()
    return data  # TaskFlow: return value auto-pushed as XCom

# Task B pulls the value
@task()
def transform(data):  # TaskFlow: parameter auto-pulled from XCom
    return clean(data)

# Under the hood (explicit XCom API):
def extract_classic(ti, **context):
    data = query_source_db()
    ti.xcom_push(key="extracted_data", value=data)

def transform_classic(ti, **context):
    data = ti.xcom_pull(task_ids="extract", key="extracted_data")
    result = clean(data)
    ti.xcom_push(key="transformed_data", value=result)
```

### XCom Storage: The Metadata DB Problem

By default, XCom values are stored in the metadata DB:

```sql
CREATE TABLE xcom (
    dag_id       VARCHAR(250) NOT NULL,
    task_id      VARCHAR(250) NOT NULL,
    run_id       VARCHAR(250) NOT NULL,
    key          VARCHAR(512) NOT NULL,
    value        BYTEA,              -- serialized value (pickle or JSON)
    timestamp    TIMESTAMPTZ NOT NULL,
    map_index    INTEGER DEFAULT -1,  -- for mapped tasks
    PRIMARY KEY (dag_id, task_id, run_id, key, map_index)
);
```

**Why this is a problem at scale:**

```
10,000 DAGs × 15 tasks × 4 runs/day × 50% push XCom = 300,000 XCom rows/day
Average value size: 2 KB
Daily XCom data: 300K × 2 KB = 600 MB/day
90-day retention: 54 GB of XCom data in metadata DB

Problems:
  1. Large BYTEA values cause table bloat (same MVCC issue as job tables)
  2. XCom reads block on DB I/O (task B waits for DB read to get task A's result)
  3. VACUUM pressure from XCom cleanup
  4. Backup size grows linearly with XCom data
```

### External XCom Backend

The solution: store XCom values in object storage, and only store a reference in the metadata DB.

```python
class S3XComBackend(BaseXCom):
    BUCKET = "airflow-xcom"

    @staticmethod
    def serialize_value(value, key, dag_id, task_id, run_id, map_index):
        s3_key = f"{dag_id}/{run_id}/{task_id}/{key}/{map_index}"
        serialized = json.dumps(value).encode()
        s3.put_object(Bucket=BUCKET, Key=s3_key, Body=serialized)
        return s3_key  # store only the reference in metadata DB

    @staticmethod
    def deserialize_value(s3_key):
        obj = s3.get_object(Bucket=BUCKET, Key=s3_key)
        return json.loads(obj["Body"].read())
```

```
XCom flow with external backend:
  Task A completes → serialize result → upload to S3
                   → store S3 key in xcom table (< 200 bytes)

  Task B starts → query xcom table → get S3 key
               → download from S3 → deserialize → use

XCom table row: ~200 bytes (reference only)
S3 object: 2 KB - 500 MB (no metadata DB impact)
```

### TaskFlow API: Implicit XCom

The TaskFlow API (`@task` decorator) infers XCom edges from function signatures:

```python
@task()
def extract() -> dict:
    return {"orders": [...]}

@task()
def transform(data: dict) -> dict:
    return clean(data)

# When you write:
data = extract()
result = transform(data)

# The decorator framework:
# 1. Sees that transform(data) receives extract()'s return value
# 2. Automatically inserts XCom push in extract (key="return_value")
# 3. Automatically inserts XCom pull in transform (from extract's return_value)
# 4. Creates a dependency edge: extract >> transform
```

This eliminates explicit `xcom_push`/`xcom_pull` calls and makes the data flow visible in the code.

---

## 12. Backfill, Catchup, and Logical Date Semantics

### Logical Date vs. Wall-Clock Time

This is the single most confusing concept in DAG orchestration. Every DAG run has two times:

```
Logical Date (formerly execution_date):
  The start of the data interval this run represents.
  A daily DAG scheduled at "0 6 * * *" with start_date 2025-01-01:
    Run 1: logical_date = 2025-01-01 00:00:00
            data_interval = [2025-01-01 00:00:00, 2025-01-02 00:00:00)
            Actually runs at: 2025-01-02 06:00:00 (after the interval ends)

    Run 2: logical_date = 2025-01-02 00:00:00
            data_interval = [2025-01-02 00:00:00, 2025-01-03 00:00:00)
            Actually runs at: 2025-01-03 06:00:00

Why this matters:
  A task that queries "SELECT * FROM orders WHERE date = {{ ds }}"
  uses the LOGICAL date, not the wall-clock time. This means:
    - The run on 2025-01-03 at 06:00 processes data for 2025-01-02
    - Backfilling March 2025 creates runs with logical dates in March,
      even though the backfill runs in September 2025
    - The query always processes the correct date's data, regardless
      of when the run actually executes
```

### Catchup Behavior

When a DAG is paused and then unpaused (or first deployed with a past `start_date`):

```
DAG: daily_etl
  schedule: daily
  start_date: 2025-01-01
  catchup: True

Scenario: DAG deployed on 2025-03-10

With catchup=True:
  Scheduler creates runs for EVERY missed interval:
    Run 1: logical_date=2025-01-01 → processes Jan 1 data
    Run 2: logical_date=2025-01-02 → processes Jan 2 data
    ...
    Run 68: logical_date=2025-03-09 → processes Mar 9 data
    Run 69: logical_date=2025-03-10 → processes Mar 10 data (current)

  68 backlog runs execute (subject to max_active_runs concurrency limit).
  This is correct for ETL: you want all historical data processed.

With catchup=False:
  Scheduler creates only the LATEST run:
    Run 1: logical_date=2025-03-10 → processes Mar 10 data

  Past intervals are skipped. Use this for alerting, notifications,
  or dashboards where historical runs are meaningless.
```

### Backfill: Explicit Historical Execution

```
CLI: airflow dags backfill daily_etl \
       --start-date 2025-03-01 \
       --end-date 2025-03-31

API: POST /api/v1/backfills
     { "dag_id": "daily_etl",
       "from_date": "2025-03-01",
       "to_date": "2025-03-31" }

What happens:
  1. Engine creates 31 DAG runs (one per day in March)
  2. Each run has logical_date set to the interval start
  3. Runs execute respecting:
     a. Dependency order (within each run)
     b. max_active_runs (across runs, default 1 for backfill safety)
     c. Pool slot limits
  4. Tasks use {{ ds }} template → process the correct date's data
  5. If a task fails, the run pauses; operator can fix and retry

Parallelized backfill:
  Set max_active_runs=8 → 8 days process simultaneously
  365 daily runs / 8 concurrent = ~46 cycles
  At ~10 min per cycle: ~7.5 hours for a year of backfill
  (well within the 2-hour NFR for 365 runs if tasks are fast)
```

### Clear and Rerun

Users can selectively clear task instances to trigger re-execution:

```
Scenario: The "transform" task had a bug on March 15-20.
          Upstream "extract" data is fine. Only transform + load need rerun.

CLI: airflow tasks clear daily_etl \
       --start-date 2025-03-15 \
       --end-date 2025-03-20 \
       --task-regex "transform|load" \
       --downstream  # also clear tasks downstream of matched tasks

What happens:
  1. Task instances for transform and load on March 15-20 are set to None state
  2. Scheduler re-evaluates them on next heartbeat
  3. extract is already "success" → dependencies are met → transform runs
  4. transform completes → load runs
  5. Only the broken tasks re-execute; upstream data is not re-fetched
```

---

## 13. Sensors and Deferrable Operators

### Sensors: Blocking Wait for External Conditions

A sensor is a special task that repeatedly checks for a condition and blocks until it's met:

```python
class S3KeySensor(BaseSensor):
    def __init__(self, bucket, key, poke_interval=60, timeout=3600, **kwargs):
        super().__init__(poke_interval=poke_interval, timeout=timeout, **kwargs)
        self.bucket = bucket
        self.key = key

    def poke(self, context) -> bool:
        """Called every poke_interval seconds. Return True when ready."""
        return s3.head_object(Bucket=self.bucket, Key=self.key) is not None
```

### The Sensor Deadlock Problem

Sensors consume a worker slot while waiting. If all slots are consumed by sensors, no actual work can run:

```
Pool "default": 10 slots

Running tasks:
  5 sensors (waiting for external files, each could take hours)
  5 actual data tasks

New task needs to run but pool is full.
If all 5 data tasks finish:
  5 sensors still hold 5 slots
  5 new sensors start → 10 sensors, 0 work slots
  → DEADLOCK: no work can ever run

This is a real production incident pattern.
```

**Mitigation 1:** Dedicated sensor pool

```python
wait_for_file = S3KeySensor(
    task_id="wait_for_file",
    bucket="data-lake",
    key="orders/{{ ds }}/part-0000.parquet",
    pool="sensor_pool",  # separate pool with its own slots
)
```

**Mitigation 2:** Sensor mode `reschedule` (not `poke`)

```python
wait_for_file = S3KeySensor(
    task_id="wait_for_file",
    bucket="data-lake",
    key="orders/{{ ds }}/part-0000.parquet",
    mode="reschedule",  # release slot between pokes
    poke_interval=300,
)
```

In `reschedule` mode, the sensor runs, checks the condition, and if not met, sets itself to `up_for_reschedule` and releases the worker slot. The scheduler re-schedules it after `poke_interval`. This uses slots only during the brief `poke()` call, not during the wait.

**Mitigation 3:** Deferrable operators (the modern solution)

### Deferrable Operators and the Triggerer

Deferrable operators solve the sensor deadlock problem by yielding execution entirely to an async event loop:

```python
class S3KeySensorAsync(BaseSensorOperator):
    def execute(self, context):
        # Don't wait here — defer to Triggerer
        self.defer(
            trigger=S3KeyTrigger(bucket=self.bucket, key=self.key),
            method_name="execute_complete",
        )

    def execute_complete(self, context, event):
        # Called when the trigger fires
        if event["status"] == "success":
            return  # sensor is satisfied, downstream can proceed
        raise AirflowException(f"Trigger failed: {event}")
```

```
┌──────────────────────────────────────────────────────────────────┐
│  Deferrable Operator Flow                                         │
│                                                                   │
│  1. Scheduler dispatches task instance to executor                │
│  2. Worker starts executing the operator                          │
│  3. Operator calls self.defer(trigger=..., method_name=...)       │
│  4. Worker:                                                       │
│     a. Saves trigger info to metadata DB (deferred_event table)   │
│     b. Sets task_instance state to "deferred"                     │
│     c. EXITS — releases the worker slot                           │
│                                                                   │
│  5. Triggerer process picks up the deferred trigger:              │
│     ┌──────────────────────────────────────────┐                  │
│     │  Triggerer (asyncio event loop)           │                  │
│     │                                           │                  │
│     │  async def run_trigger(trigger):          │                  │
│     │    while True:                            │                  │
│     │      if await trigger.check_condition():  │                  │
│     │        yield TriggerEvent(status="success")│                 │
│     │        return                             │                  │
│     │      await asyncio.sleep(poke_interval)   │                  │
│     │                                           │                  │
│     │  One event loop handles 1000+ triggers    │                  │
│     │  concurrently with minimal resources      │                  │
│     └──────────────────────────────────────────┘                  │
│                                                                   │
│  6. Trigger fires → Triggerer writes event to deferred_event table│
│  7. Scheduler sees the event → transitions TI to "scheduled"     │
│  8. Executor re-runs the operator → calls execute_complete()      │
│                                                                   │
│  Result: 1000 sensors consume ~0 worker slots (only the brief     │
│  execute and execute_complete calls use a slot)                   │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

### Triggerer Architecture

```
The Triggerer is a separate process (deployment in K8s):

  - Runs a Python asyncio event loop
  - Polls the metadata DB for new deferred triggers
  - Runs each trigger as an async coroutine
  - Single Triggerer instance handles 1000+ concurrent triggers
  - Multiple Triggerer instances for HA (each claims triggers via SKIP LOCKED)

Performance:
  1,000 concurrent sensors:
    Traditional (poke mode):  1,000 worker slots consumed
    Deferrable:               ~0 worker slots, 1 Triggerer with 1,000 coroutines
                              Memory: ~100 MB (asyncio is lightweight)
                              CPU: minimal (mostly sleeping between pokes)
```

---

## 14. Dynamic DAGs and Mapped Tasks

### The Problem: Static vs. Dynamic Graphs

A DAG's structure is normally fixed at parse time. But some workflows need to:

- Process a variable number of files (fan-out)
- Run the same task for each partition/table (parameterized)
- Create tasks conditionally based on runtime data

### Mapped Tasks (Dynamic Task Expansion)

```python
@dag(schedule="@daily")
def process_files():

    @task()
    def list_files() -> list[str]:
        """Returns a list of file paths to process."""
        return s3.list_objects("bucket/incoming/{{ ds }}/")
        # e.g., returns ["file1.csv", "file2.csv", "file3.csv"]

    @task()
    def process_file(file_path: str) -> dict:
        """Process a single file. Runs once per file."""
        data = read(file_path)
        return transform(data)

    @task()
    def aggregate(results: list[dict]):
        """Aggregate all processed results."""
        combined = merge(results)
        write_to_warehouse(combined)

    files = list_files()
    processed = process_file.expand(file_path=files)  # MAPPED: creates N instances
    aggregate(processed)

process_files()
```

### How Mapped Tasks Work

```
Parse time:
  DAG has 3 tasks: list_files, process_file, aggregate
  process_file is marked as "mapped" (expansion deferred to runtime)
  The scheduler doesn't know how many instances it will have

Runtime:
  list_files runs → returns ["file1.csv", "file2.csv", "file3.csv"]
  XCom value is stored with 3 elements

  Scheduler sees process_file is mapped:
    → reads upstream XCom (list_files return value)
    → counts elements: 3
    → creates 3 task instances:
        process_file[0] (map_index=0, file_path="file1.csv")
        process_file[1] (map_index=1, file_path="file2.csv")
        process_file[2] (map_index=2, file_path="file3.csv")
    → dispatches all 3 (subject to concurrency limits)

  All 3 complete → aggregate runs with all results

Next run (different day):
  list_files returns 7 files → 7 instances of process_file
  DAG shape is different from previous run
```

### Mapped Task Instance Storage

```sql
-- task_instance table includes map_index
CREATE TABLE task_instance (
    dag_id      VARCHAR(250) NOT NULL,
    task_id     VARCHAR(250) NOT NULL,
    run_id      VARCHAR(250) NOT NULL,
    map_index   INTEGER      NOT NULL DEFAULT -1,  -- -1 for non-mapped tasks
    state       VARCHAR(20),
    -- ...
    PRIMARY KEY (dag_id, task_id, run_id, map_index)
);

-- For process_file with 3 files:
-- (daily_etl, process_file, run_1, 0) → state=success
-- (daily_etl, process_file, run_1, 1) → state=success
-- (daily_etl, process_file, run_1, 2) → state=failed  ← only this one retries
```

### DAG-Time vs. Runtime Expansion

| Type | When | Use Case |
|---|---|---|
| **DAG-time** (static) | At parse time, DAG factory generates tasks | Generating one task per table in a known schema |
| **Runtime** (mapped) | At runtime, after upstream produces a list | Processing a variable number of files, partitions, API pages |

```python
# DAG-time expansion (static — task count fixed at parse time)
with DAG("process_tables") as dag:
    tables = ["users", "orders", "products"]  # known at import
    for table in tables:
        PythonOperator(
            task_id=f"sync_{table}",
            python_callable=sync_table,
            op_args=[table],
        )

# Runtime expansion (dynamic — task count determined at runtime)
@task()
def get_tables() -> list[str]:
    return query_information_schema()

sync_table.expand(table=get_tables())  # N instances created at runtime
```

---

## 15. Dataset-Driven Scheduling

### Beyond Cron: Data-Aware Triggers

Traditional scheduling: "run this DAG at 6 AM every day." But what if the upstream data isn't ready at 6 AM? You add sensors. Sensors waste resources.

Dataset-driven scheduling: "run this DAG when the upstream data is updated."

```python
# Producer DAG: declares it updates a dataset
orders_dataset = Dataset("s3://warehouse/orders/")

@dag(schedule="@daily")
def etl_orders():
    @task(outlets=[orders_dataset])  # marks this task as a dataset producer
    def load_orders():
        data = extract_from_source()
        write_to_s3("s3://warehouse/orders/", data)

# Consumer DAG: triggered when the dataset is updated
@dag(schedule=[orders_dataset])  # triggered by dataset update, not cron
def downstream_analytics():
    @task()
    def build_report():
        data = read_from_s3("s3://warehouse/orders/")
        generate_report(data)
```

### How Dataset Triggers Work

```
1. etl_orders DAG runs → load_orders task completes successfully
2. Task has outlets=[orders_dataset]
   → Scheduler records a "dataset event" in the dataset_event table:
     (uri="s3://warehouse/orders/", source_dag="etl_orders",
      source_task="load_orders", timestamp=NOW())
3. Scheduler checks: which DAGs are triggered by this dataset?
   → downstream_analytics is triggered by [orders_dataset]
4. All triggering datasets have been updated since the last run
   → Scheduler creates a DAG run for downstream_analytics
   → run_type = "dataset_triggered"
```

### Multi-Dataset Triggers

```python
orders = Dataset("s3://warehouse/orders/")
customers = Dataset("s3://warehouse/customers/")

@dag(schedule=[orders, customers])  # both must be updated
def joined_analytics():
    ...

# Trigger logic:
# Run is created only when BOTH orders AND customers have new events
# since the last run of joined_analytics.
```

---

## 16. Pools, Priority, and Concurrency Control

### Pool-Based Resource Management

```sql
CREATE TABLE slot_pool (
    pool_name        VARCHAR(256) PRIMARY KEY,
    slots            INTEGER NOT NULL,       -- total slots
    description      TEXT,
    include_deferred BOOLEAN DEFAULT FALSE   -- count deferred TIs against limit?
);

-- Default pools
INSERT INTO slot_pool VALUES ('default_pool', 128, 'Default pool');
INSERT INTO slot_pool VALUES ('etl_pool', 32, 'ETL database connections');
INSERT INTO slot_pool VALUES ('api_pool', 10, 'Rate-limited API calls');
```

### Scheduling with Pools and Priority

```
Scheduler evaluation order:

  1. Gather all task instances in schedulable states
     (scheduled, up_for_retry)

  2. Sort by priority_weight DESC, logical_date ASC
     → highest priority, oldest runs first

  3. For each TI in sorted order:
     a. Check upstream dependencies → skip if not met
     b. Check pool: open_slots = pool.slots - running_count[pool]
        → skip if open_slots <= 0
     c. Check dag.max_active_tasks → skip if at limit
     d. Check task.max_active_tis_per_dag → skip if at limit
     e. Check global parallelism → skip if at limit
     f. All checks pass → dispatch to executor, decrement pool counter

  This is a greedy algorithm: highest-priority tasks consume pool
  slots first. Lower-priority tasks may starve if the pool is saturated.
```

### Priority Weight Calculation

```
Default priority_weight = 1

priority_weight_method:
  "downstream" (default): weight = 1 + sum(downstream_task_weights)
    → Tasks with more downstream dependents get higher priority
    → Ensures critical-path tasks are scheduled first

  "upstream": weight = 1 + sum(upstream_task_weights)
    → Tasks later in the pipeline get higher priority

  "absolute": weight = task.priority_weight (user-set)

Example DAG:
  A(weight=1) → B(weight=1) → C(weight=1)
                ↗
  D(weight=1) →

  With method="downstream":
    A: 1 + weight(B) + weight(C) = 3
    D: 1 + weight(B) + weight(C) = 3
    B: 1 + weight(C) = 2
    C: 1
  → A and D scheduled before B, B before C (critical path first)
```

---

## 17. Metadata Database Design

### Core Schema

```sql
-- DAG definition (serialized from DAG files)
CREATE TABLE dag (
    dag_id                    VARCHAR(250) PRIMARY KEY,
    is_paused                 BOOLEAN NOT NULL DEFAULT TRUE,
    is_active                 BOOLEAN NOT NULL DEFAULT TRUE,
    fileloc                   VARCHAR(2000),
    owners                    VARCHAR(2000),
    description               TEXT,
    schedule_interval         TEXT,
    timetable_description     TEXT,
    tags                      JSONB,
    max_active_runs           INTEGER NOT NULL DEFAULT 16,
    max_active_tasks          INTEGER NOT NULL DEFAULT 16,
    has_import_errors         BOOLEAN NOT NULL DEFAULT FALSE,
    next_dagrun_create_after  TIMESTAMPTZ,
    last_parsed_time          TIMESTAMPTZ,
    processor_subdir          VARCHAR(2000)
);

-- DAG run (one per schedule interval per DAG)
CREATE TABLE dag_run (
    id               BIGSERIAL PRIMARY KEY,
    dag_id           VARCHAR(250) NOT NULL REFERENCES dag(dag_id),
    run_id           VARCHAR(250) NOT NULL,
    logical_date     TIMESTAMPTZ NOT NULL,
    start_date       TIMESTAMPTZ,
    end_date         TIMESTAMPTZ,
    state            VARCHAR(50),     -- queued, running, success, failed
    run_type         VARCHAR(50),     -- scheduled, manual, backfill, dataset_triggered
    conf             JSONB,           -- runtime configuration override
    data_interval_start TIMESTAMPTZ,
    data_interval_end   TIMESTAMPTZ,
    creating_job_id  INTEGER,
    UNIQUE (dag_id, run_id),
    UNIQUE (dag_id, logical_date)
);

-- Task instance (one per task per DAG run)
CREATE TABLE task_instance (
    dag_id           VARCHAR(250) NOT NULL,
    task_id          VARCHAR(250) NOT NULL,
    run_id           VARCHAR(250) NOT NULL,
    map_index        INTEGER NOT NULL DEFAULT -1,
    start_date       TIMESTAMPTZ,
    end_date         TIMESTAMPTZ,
    duration         FLOAT,
    state            VARCHAR(20),
    try_number       INTEGER NOT NULL DEFAULT 0,
    max_tries        INTEGER NOT NULL DEFAULT 0,
    hostname         VARCHAR(1000),
    unixname         VARCHAR(1000),
    job_id           INTEGER,
    pool             VARCHAR(256) NOT NULL DEFAULT 'default_pool',
    pool_slots       INTEGER NOT NULL DEFAULT 1,
    queue            VARCHAR(256),
    priority_weight  INTEGER,
    operator         VARCHAR(1000),
    queued_when      TIMESTAMPTZ,
    queued_by_job_id INTEGER,
    pid              INTEGER,
    executor_config  JSONB,
    next_method      VARCHAR(1000),       -- for deferrable operators
    next_kwargs      JSONB,               -- for deferrable operators
    trigger_id       INTEGER,             -- FK to trigger table
    PRIMARY KEY (dag_id, task_id, run_id, map_index),
    FOREIGN KEY (dag_id, run_id) REFERENCES dag_run(dag_id, run_id)
);

-- XCom (task output / cross-communication)
CREATE TABLE xcom (
    dag_id           VARCHAR(250) NOT NULL,
    task_id          VARCHAR(250) NOT NULL,
    run_id           VARCHAR(250) NOT NULL,
    key              VARCHAR(512) NOT NULL DEFAULT 'return_value',
    value            BYTEA,
    timestamp        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    map_index        INTEGER NOT NULL DEFAULT -1,
    PRIMARY KEY (dag_id, task_id, run_id, key, map_index)
);

-- Pools
CREATE TABLE slot_pool (
    id          SERIAL PRIMARY KEY,
    pool_name   VARCHAR(256) UNIQUE NOT NULL,
    slots       INTEGER NOT NULL DEFAULT 128,
    description TEXT,
    include_deferred BOOLEAN DEFAULT FALSE
);

-- Dataset events (for dataset-driven scheduling)
CREATE TABLE dataset_event (
    id              BIGSERIAL PRIMARY KEY,
    dataset_uri     VARCHAR(3000) NOT NULL,
    source_dag_id   VARCHAR(250),
    source_task_id  VARCHAR(250),
    source_run_id   VARCHAR(250),
    source_map_index INTEGER DEFAULT -1,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    extra           JSONB
);

-- Deferred triggers (for deferrable operators)
CREATE TABLE trigger (
    id              SERIAL PRIMARY KEY,
    classpath       VARCHAR(1000) NOT NULL,
    kwargs          JSONB NOT NULL,
    created_date    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    triggerer_id    INTEGER             -- which triggerer instance owns this
);

-- Import errors (DAG file parse failures)
CREATE TABLE import_error (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    filename        VARCHAR(1024),
    stacktrace      TEXT
);

-- Connections (external system credentials)
CREATE TABLE connection (
    id              SERIAL PRIMARY KEY,
    conn_id         VARCHAR(250) UNIQUE NOT NULL,
    conn_type       VARCHAR(500) NOT NULL,
    host            VARCHAR(500),
    schema          VARCHAR(500),
    login           VARCHAR(500),
    password        VARCHAR(5000),    -- encrypted at rest (Fernet)
    port            INTEGER,
    extra            JSONB,
    description     TEXT
);
```

### Critical Indexes

```sql
-- The scheduler's primary query: "give me schedulable task instances"
CREATE INDEX idx_ti_state_run ON task_instance (state, dag_id, run_id)
    WHERE state IN ('scheduled', 'queued', 'up_for_retry', 'running');

-- Pool slot counting
CREATE INDEX idx_ti_pool_state ON task_instance (pool, state)
    WHERE state IN ('running', 'queued');

-- DAG run scheduling: "which DAGs need new runs?"
CREATE INDEX idx_dag_next_run ON dag (next_dagrun_create_after)
    WHERE is_paused = FALSE AND is_active = TRUE;

-- DAG run state tracking
CREATE INDEX idx_dagrun_state ON dag_run (dag_id, state);

-- XCom reads
CREATE INDEX idx_xcom_lookup ON xcom (dag_id, task_id, run_id, key);

-- Dataset event lookups
CREATE INDEX idx_dataset_event ON dataset_event (dataset_uri, timestamp DESC);
```

---

## 18. Metadata DB at Scale: Growth, Bottlenecks, and Archival

### What Causes the Metadata DB to Become the Bottleneck

```
Source 1: task_instance table growth
  180K rows/day × 365 days = 65M rows/year
  Each row has 3-5 state transitions (UPDATE):
    no_status → scheduled → queued → running → success
    = 4 UPDATEs per row = 4 dead tuples per row
  Dead tuples/year: 65M × 4 = 260M dead tuples
  Same MVCC/VACUUM issue as job scheduler tables

Source 2: XCom bloat
  Large XCom values (DataFrames serialized as pickle/JSON) stored as BYTEA
  VACUUM must process these large rows

Source 3: Scheduler query contention
  The scheduling loop runs every 5 seconds
  Each iteration: ~20 queries touching task_instance + dag_run
  Multiple scheduler instances competing for the same rows
  FOR UPDATE SKIP LOCKED helps but adds lock management overhead

Source 4: Index bloat
  Partial indexes on state columns: constantly changing values
  Same index bloat death spiral as job scheduler (Section 6 of job-scheduler)
```

### Archival Strategy

```
┌──────────────────────────────────────────────────────────────────┐
│  Metadata DB Archival                                             │
│                                                                   │
│  Hot data (metadata DB):                                          │
│    dag_run:       last 30 days                                    │
│    task_instance: last 30 days                                    │
│    xcom:          last 30 days                                    │
│                                                                   │
│  Warm archive (separate PostgreSQL instance or table):            │
│    dag_run:       30-90 days                                      │
│    task_instance: 30-90 days                                      │
│    xcom:          purged (only references kept)                   │
│                                                                   │
│  Cold archive (object storage):                                   │
│    dag_run:       90+ days (queryable via Athena/Presto)          │
│    task_instance: 90+ days                                        │
│    xcom:          external backend (already in S3)                │
│                                                                   │
│  Archival process (runs daily):                                   │
│    1. COPY old dag_run + task_instance to archive table/DB        │
│    2. DELETE from hot tables WHERE end_date < NOW() - 30 days     │
│    3. VACUUM hot tables                                           │
│    OR: use table partitioning (partition by month)                │
│       → DROP PARTITION instead of DELETE (zero bloat)             │
│                                                                   │
│  Recommended: partition task_instance and dag_run by month.        │
│  DROP PARTITION for cleanup. Same pattern as job scheduler.       │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

### DBOS Difference: The DB Is the Execution Engine

In a traditional orchestrator, the metadata DB stores state. In DBOS, the DB stores state AND execution records AND idempotency guarantees:

```
Traditional:
  Metadata DB = state tracking (dag_run + task_instance states)
  Execution = happens on workers, outside any DB transaction
  Gap: worker crashes between execution and state update → inconsistency

DBOS:
  Database = state tracking + execution log + idempotency
  Each step is a DB transaction that atomically records:
    - The step's inputs and outputs
    - The execution status
    - Any DB side-effects (INSERT/UPDATE in the same transaction)
  Gap: none for DB operations; reduced for external calls (idempotency key)

Trade-off: DBOS scales with PostgreSQL write throughput (~20-30K txn/sec),
which limits it to ~10K task steps/sec. For most data pipelines, this is
sufficient. For high-throughput event processing, use Celery/K8s executor.
```

---

## 19. Observability and Debugging

### Key Metrics

| Metric | Type | Labels | Alert Threshold |
|---|---|---|---|
| `scheduler_heartbeat_duration_sec` | Histogram | — | P99 > 10s: scheduler too slow |
| `dag_processing_duration_sec` | Histogram | dag_id | P99 > 30s: DAG file too complex |
| `dagrun_duration_sec` | Histogram | dag_id, run_type | — |
| `task_instance_duration_sec` | Histogram | dag_id, task_id | — |
| `task_instance_state_total` | Counter | dag_id, state | failed rate > 5%: pipeline unhealthy |
| `pool_open_slots` | Gauge | pool_name | 0 for > 5 min: pool exhausted |
| `scheduler_tasks_running` | Gauge | — | > parallelism × 0.9: nearing limit |
| `executor_queue_depth` | Gauge | — | > 100: executor backed up |
| `dagrun_queued_duration_sec` | Histogram | dag_id | P99 > 60s: scheduling delay |
| `xcom_size_bytes` | Histogram | dag_id, task_id | > 10 MB: should use external backend |
| `dag_import_errors_total` | Gauge | — | > 0: broken DAG files |
| `triggerer_running_triggers` | Gauge | — | — |
| `k8s_pod_pending_duration_sec` | Histogram | dag_id | P99 > 60s: cluster capacity issue |

### Debugging Common Issues

**"My DAG isn't running"** — Troubleshooting tree:

```
1. Is the DAG paused?
   → Check dag.is_paused in metadata DB or UI

2. Does it have import errors?
   → Check import_error table → fix Python syntax/import issue

3. Is max_active_runs reached?
   → Check: SELECT COUNT(*) FROM dag_run WHERE dag_id=X AND state='running'

4. Is the scheduler running?
   → Check scheduler heartbeat metric → restart if down

5. Are pool slots available?
   → Check: SELECT pool_name, slots, (slots - running) as open FROM slot_pool

6. Is the task stuck in "scheduled" but never "queued"?
   → Pool exhaustion, concurrency limit, or executor not processing

7. Is the task stuck in "queued" but never "running"?
   → Executor issue: Celery broker down, K8s pods not scheduling
```

---

## 20. Failure Walkthroughs

### Scenario 1: Scheduler Crashes Mid-Heartbeat

```
Timeline:
  t=0:    Scheduler heartbeat starts
  t=1:    Scheduler creates 5 new DAG runs
  t=2:    Scheduler updates 20 task instances to "queued"
  t=3:    Scheduler dispatches 15 tasks to Celery
  t=3.5:  Scheduler CRASHES (OOM, pod eviction)

What happens:
  - 5 DAG runs are in DB with state "queued" → valid, will be picked up
  - 20 task instances in "queued" state but only 15 dispatched
  - 5 task instances: "queued" in DB but NOT in Celery queue
    → these are "orphaned queued" tasks

Recovery (on scheduler restart):
  t=10:   New scheduler starts (or standby takes over)
  t=11:   Scheduler heartbeat runs:
          → Finds 5 TIs in "queued" state that are NOT in executor's known tasks
          → Re-dispatches them to executor
          → Finds 15 TIs in "queued" that ARE being executed by Celery
          → Waits for executor events

No data loss. Brief delay (seconds to minutes depending on restart time).
```

### Scenario 2: Worker Crashes During Task Execution

```
Timeline:
  t=0:    Celery worker starts executing "transform" task
  t=5:    Worker crashes (OOM, segfault)

What happens:
  - task_instance state in DB: "running" (set before execution started)
  - Celery: task message is acked (removed from broker queue)
  - No completion callback arrives at scheduler

Detection:
  Option A: Scheduler timeout
    - task has execution_timeout = 1 hour
    - After 1 hour, scheduler marks it "failed"
    - If retries remain → "up_for_retry" → reschedule

  Option B: Celery visibility timeout
    - If task was NOT acked → Celery re-delivers after visibility_timeout
    - This creates a double-execution risk (mitigate with idempotency)

  Option C: KubernetesExecutor
    - K8s watch detects pod in "Failed" state immediately
    - Executor reports failure to scheduler within seconds
    - Much faster detection than Celery timeout
```

### Scenario 3: Metadata Database Overload

```
Timeline:
  t=0:    Large backfill starts: 365 DAG runs, 15 tasks each = 5,475 TIs
  t=1:    Scheduler creates all TIs in DB (bulk INSERT)
  t=2:    Scheduler evaluates all TIs every heartbeat:
          SELECT from task_instance WHERE dag_id=X AND state IN (...)
          → returns 5,475 rows
  t=3:    DB CPU at 90% — every heartbeat takes 10 seconds
  t=4:    Normal scheduled DAGs are delayed (scheduler too busy with backfill)

Mitigation:
  1. max_active_runs = 8 for the backfilled DAG
     → only 8 × 15 = 120 TIs evaluated per heartbeat (not 5,475)
  2. Backfill in batches (100 runs at a time, not 365)
  3. Separate scheduler instance for backfill operations
  4. DB query optimization: index on (dag_id, state) + LIMIT
```

---

## 21. Trade-offs

### DAG-Based vs. Code-Based Workflows

| Dimension | DAG Orchestrator (Airflow/Prefect) | Durable Execution (Temporal) |
|---|---|---|
| **Best for** | Scheduled data pipelines with explicit dependencies | Long-running business processes with complex control flow |
| **Definition** | Graph of tasks with edges | Code with SDK primitives (activities, timers, signals) |
| **Scheduling** | Calendar intervals, datasets, cron | Event-driven, timer-based, signal-based |
| **Backfill** | First-class operation | Not applicable (replay ≠ backfill) |
| **Data passing** | XCom, result backends | Activity results in event history |
| **Failure recovery** | Retry the task, clear-and-rerun | Replay workflow from event history |
| **Exactly-once** | Not by default (DBOS adds it) | Via deterministic replay |
| **Duration** | Minutes to hours per run | Minutes to months per workflow |
| **UI** | DAG graph, Gantt chart, task logs | Workflow event timeline |

**Use the DAG orchestrator when:** you have scheduled batch workloads with explicit task dependencies, need backfill, and tasks are independent units (each task's side-effects don't need transactional coordination with other tasks).

**Use the durable execution engine when:** you have long-running stateful processes, need saga compensation, have human-in-the-loop approvals, or require exactly-once side-effect execution.

### Executor Choice

| Dimension | LocalExecutor | CeleryExecutor | KubernetesExecutor | DBOS Transactional |
|---|---|---|---|---|
| **When** | Dev, <50 tasks | Medium scale | Large scale, isolation | Exactly-once needed |
| **Startup latency** | ~10ms | ~50ms | ~10-30s | ~5ms |
| **Isolation** | None | Worker | Container | Transaction |
| **Scale** | 1 host | Worker fleet | K8s cluster | DB throughput |
| **Operational cost** | None | Broker + workers | K8s cluster | PostgreSQL |

### Metadata DB: PostgreSQL vs. Purpose-Built

| Dimension | PostgreSQL (current) | Purpose-built (hypothetical) |
|---|---|---|
| **Reliability** | Battle-tested, ACID, backups | Needs proving |
| **Scheduler queries** | SQL, well-optimized | Custom query engine |
| **Scale ceiling** | ~100K TIs/day comfortable | Higher |
| **VACUUM pressure** | Real problem at scale | Not applicable |
| **Operational cost** | Low (managed PostgreSQL) | High (new system) |
| **DBOS compatibility** | Natural (DB IS the engine) | Would need adapter |

**Recommendation:** PostgreSQL with partitioning and archival. Only consider alternatives at >1M TIs/day sustained — and even then, optimize PostgreSQL first (partitioning, archival, read replicas, separate hot/cold tables).

---

## 22. Evolution Path

### Phase 1: MVP (Months 1-3)

- [ ] Core engine: scheduler loop, DAG parsing, task state machine
- [ ] LocalExecutor and CeleryExecutor
- [ ] PostgreSQL metadata DB with core schema
- [ ] XCom (metadata DB backend)
- [ ] Cron and timedelta scheduling
- [ ] Basic Web UI: DAG list, task instance states, logs
- [ ] REST API for DAGs, runs, and task instances
- [ ] Pool-based concurrency control
- [ ] Retry policies with exponential backoff

### Phase 2: Production Hardening (Months 4-6)

- [ ] KubernetesExecutor with pod-per-task
- [ ] Deferrable operators and Triggerer
- [ ] External XCom backend (S3/GCS)
- [ ] Dataset-driven scheduling
- [ ] Mapped tasks (dynamic fan-out)
- [ ] HA scheduler (multiple instances with SKIP LOCKED)
- [ ] Backfill API and CLI
- [ ] Connection management with Fernet encryption
- [ ] DAG serialization and DB-stored DAGs
- [ ] Prometheus metrics export

### Phase 3: Scale (Months 7-12)

- [ ] Metadata DB partitioning and archival
- [ ] DBOS-style transactional executor
- [ ] Fine-grained RBAC (per-DAG permissions)
- [ ] Multi-team / soft multi-tenancy with tags and pools
- [ ] Task log archival to object storage
- [ ] DAG versioning and audit log
- [ ] Lineage tracking (dataset → task → dataset)
- [ ] Performance: sub-5s dispatch latency at 100K TIs/hour

### Phase 4: Enterprise (Months 12+)

- [ ] Hard multi-tenancy (isolated metadata DBs per tenant)
- [ ] Federated scheduling (multiple clusters, cross-cluster dependencies)
- [ ] Cost attribution per DAG/team
- [ ] SLA monitoring and alerting (DAG must complete by time X)
- [ ] Visual DAG editor (low-code, generates Python)
- [ ] Plugin marketplace (custom operators, hooks, triggers)
- [ ] Managed cloud offering with per-run billing

---

## Appendix: Why This Is Not a Job Scheduler, and Not Temporal

### vs. Job Scheduler

A job scheduler fires individual tasks. A DAG orchestrator fires **graphs of tasks with data dependencies**. The scheduler cannot express "run task B only after task A succeeds and pass A's output to B." The DAG orchestrator's scheduler loop, dependency resolution, backfill, and XCom have no equivalent in a job scheduler.

### vs. Temporal

A Temporal workflow is **code that runs for the lifetime of a business process** (hours to months). State is maintained via event-sourced history and deterministic replay. Side-effects are recorded so they are never re-executed.

A DAG orchestrator run is **bounded**: it processes one interval's data and completes. State is task instance statuses in a metadata DB, not an event log. There is no replay — if a task fails, it retries from scratch (or the user clears and reruns). Backfill creates new runs for historical intervals — Temporal has no equivalent.

The two complement each other: use the DAG orchestrator to schedule and coordinate your data pipelines. Use Temporal for the long-running business processes (order fulfillment, subscription management) that those pipelines feed into.
