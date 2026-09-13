## System Design Task: DAG-Based Pipeline Orchestration Engine

### Problem Statement

Design a **DAG-based pipeline orchestration engine** — in the spirit of
**Apache Airflow, Prefect, Dagster, and DBOS** — that enables teams to
author, schedule, and monitor **directed acyclic graphs (DAGs) of dependent
tasks** that run on pluggable executors, with first-class support for
**backfill, data passing, dynamic task generation, and exactly-once
transactional execution**.

A **job scheduler** fires individual tasks on a schedule. A **Temporal-style
workflow engine** provides durable execution through event sourcing and
deterministic replay. A **DAG pipeline orchestrator** is a third, distinct
system: it models work as a **graph of tasks with explicit dependency edges**,
schedules that graph on calendar intervals, resolves which tasks are runnable
at each moment, dispatches them to heterogeneous executors (containers,
Kubernetes pods, Celery workers, serverless functions), passes data between
tasks, and provides operators with **full lifecycle visibility**: run history,
task logs, data lineage, and the ability to retry, backfill, or
clear-and-rerun any historical window.

**Why this is its own design problem:**

* **Scheduler complexity** is unique. The scheduler must evaluate thousands of
  DAGs, each with their own `schedule_interval`, `start_date`, catchup
  policy, and concurrency limits. It must create **DAG runs** for each
  logical execution date, resolve which task instances within each run are
  runnable (all upstream dependencies satisfied), and dispatch them — all
  while respecting pool slots, priority weights, and cross-DAG dependencies.
  This is a constraint-satisfaction problem, not a simple cron.

* **Backfill and idempotent re-execution** are first-class operations. Users
  routinely say "reprocess all of March 2025" — the engine must create DAG
  runs for every interval in that window, execute them respecting dependency
  order, and allow partial clearing (rerun only the failing tasks, keeping
  upstream results). This requires a deep understanding of logical execution
  dates vs. wall-clock time.

* **Executor heterogeneity** means the engine must dispatch to local
  processes, Celery/Redis workers, Kubernetes pods, or cloud-native
  serverless runtimes — each with different lifecycle semantics (container
  startup, pod scheduling, cold start), failure modes (OOMKilled, preemption,
  spot eviction), and resource profiles (CPU, GPU, memory).

* **Data passing between tasks** (XCom in Airflow, return values in Prefect,
  assets in Dagster) introduces a hidden data plane. Naively storing task
  results in the metadata database bloats it; using external backends
  (S3, GCS) introduces consistency windows and garbage collection.

* **Dynamic DAGs and TaskFlow** allow tasks to generate downstream tasks at
  runtime (fan-out), creating DAG structures that are unknown at parse time.
  The scheduler and UI must handle DAGs whose shape changes between runs.

* **DBOS-style transactional execution** offers an alternative paradigm:
  instead of retry-based idempotency, wrap each task step in a database
  transaction so that side-effects and state updates are atomic. This
  eliminates ghost runs, double-execution, and the need for external
  idempotency keys — but constrains the execution model.

This engine will serve as the backbone for **ETL/ELT pipelines, ML training
workflows, data quality checks, report generation, event-driven data
processing, and CI/CD pipelines** — any workload where tasks have explicit
data or ordering dependencies and must be scheduled, monitored, and
re-executed at scale.

---

### Functional Requirements

1. **DAG Definition**

   * Authors define DAGs as Python code using decorators (`@dag`, `@task`)
     or explicit operator instantiation. No YAML — code is the source of
     truth for both control flow and business logic.
   * Each DAG has: `dag_id` (unique string), `schedule` (cron expression,
     timedelta, timetable object, or dataset trigger), `start_date`,
     `end_date`, `catchup` (boolean), `max_active_runs`, `default_args`
     (retry, timeout, pool, priority), `tags`, and `owner`.
   * Tasks within a DAG declare dependencies via `>>` / `<<` operators or
     `set_upstream()` / `set_downstream()`. The engine validates the graph
     is acyclic at parse time.
   * Support **TaskFlow API**: `@task`-decorated Python functions where
     return values automatically become inputs to downstream tasks
     (implicit XCom).

2. **Scheduler**

   * The scheduler is a long-running process that:
     a. Parses DAG files periodically to discover new/changed DAGs.
     b. Creates **DAG runs** for each DAG whose schedule interval has
        elapsed (or whose dataset trigger has fired).
     c. For each DAG run, evaluates **task instances** and transitions
        them through states based on dependency rules.
     d. Dispatches runnable task instances to the executor.
   * Support for **trigger rules**: `all_success` (default), `all_failed`,
     `all_done`, `one_success`, `one_failed`, `none_failed`,
     `none_skipped`.
   * Support for **sensors**: tasks that poll for an external condition
     (file exists, partition available, API returns 200) and block the
     downstream until the condition is met — with configurable `poke_interval`
     and `timeout`.
   * Support for **deferrable operators / triggers**: sensors that yield
     execution back to the scheduler (freeing the worker slot) and are
     re-awoken by an async trigger component when the condition is met.

3. **Executor Architecture**

   * The executor is a pluggable component that runs task instances:
     - **LocalExecutor**: multiprocessing on the scheduler host
     - **CeleryExecutor**: distributes to a Celery worker fleet via
       Redis/RabbitMQ broker
     - **KubernetesExecutor**: launches a pod per task instance
     - **DBOS-style TransactionalExecutor**: wraps execution in a database
       transaction for exactly-once guarantees
   * Each executor reports task state transitions (running, success, failed)
     back to the scheduler.
   * Executors must handle: task timeout enforcement, resource limits (CPU,
     memory), log streaming, and graceful shutdown.

4. **Data Passing (XCom / Task Results)**

   * Tasks can push key-value results (`xcom_push`) and downstream tasks
     can pull them (`xcom_pull` or via TaskFlow return values).
   * XCom values are serialized and stored in a configurable backend:
     metadata database (small values), S3/GCS (large values), or a custom
     backend.
   * Size limits: metadata DB backend caps at 48 KB per value; external
     backends have no practical limit.
   * XCom values are scoped to `(dag_id, task_id, run_id, key)` and
     are immutable once written.

5. **Backfill and Historical Execution**

   * `backfill(dag_id, start_date, end_date)` creates DAG runs for every
     interval in the window and executes them in dependency order.
   * **Clear and rerun**: mark specific task instances (or all tasks in a
     date range) as `cleared` to trigger re-execution while preserving
     upstream results.
   * **Logical date** (formerly `execution_date`): each DAG run is
     identified by its logical date (the start of the interval it
     represents), not the wall-clock time it ran. A daily DAG run for
     "2025-03-15" processes data for March 15, regardless of when it
     actually executes.

6. **Pools, Priority, and Concurrency**

   * **Pools**: named resource pools with a fixed number of slots.
     Tasks assigned to a pool compete for slots. Used to limit
     concurrency to external systems (e.g., max 5 concurrent connections
     to a production DB).
   * **Priority weight**: integer weight per task. Higher-priority tasks
     are dispatched first when competing for executor capacity.
   * **Concurrency limits**: per-DAG (`max_active_runs`,
     `max_active_tasks`), per-task (`max_active_tis_per_dag`), and
     global (`parallelism`).

7. **Dataset-Driven Scheduling**

   * A DAG can declare that it **produces** a dataset (e.g.,
     `Dataset("s3://warehouse/orders/")`) when a task completes.
   * Other DAGs can declare they are **triggered by** one or more
     datasets. When all triggering datasets are updated, the downstream
     DAG run is created.
   * This enables event-driven, data-aware scheduling without cron.

8. **Observability**

   * **Web UI**: DAG list, graph view (dependency visualization), Gantt
     chart (task execution timeline), task logs (streamed), XCom
     inspector, run history.
   * **API**: full REST API for programmatic access to DAGs, runs, task
     instances, logs, XCom, pools, and connections.
   * **Lineage**: which dataset was produced by which task in which run.

9. **APIs**

   ```
   GET    /api/v1/dags                            — List DAGs
   GET    /api/v1/dags/{dag_id}                   — DAG detail
   POST   /api/v1/dags/{dag_id}/dagRuns            — Trigger a DAG run
   GET    /api/v1/dags/{dag_id}/dagRuns             — List runs
   GET    /api/v1/dags/{dag_id}/dagRuns/{run_id}/taskInstances — Task instances
   POST   /api/v1/dags/{dag_id}/clearTaskInstances  — Clear for rerun
   POST   /api/v1/backfills                         — Trigger backfill
   GET    /api/v1/pools                             — List pools
   GET    /api/v1/dags/{dag_id}/dagRuns/{run_id}/taskInstances/{task_id}/xcomEntries — XCom
   GET    /api/v1/dags/{dag_id}/dagRuns/{run_id}/taskInstances/{task_id}/logs — Logs
   ```

---

### Non-Functional Requirements

| Requirement         | Target                                                           |
|---------------------|------------------------------------------------------------------|
| DAG capacity        | 10,000+ DAGs, 100,000+ task instances scheduled per hour         |
| Scheduler latency   | DAG file parsing loop < 30 seconds                               |
| Task dispatch       | Runnable task → executor dispatch P99 < 5 seconds                |
| Availability        | 99.9% for the scheduler and web UI                               |
| Metadata DB         | Supports 500M+ task instance records (with retention/archival)   |
| Backfill            | Backfill 365 daily runs in < 2 hours (parallelized)              |
| Executor scale      | KubernetesExecutor: 1,000+ concurrent pods                      |
| Log retention       | Task logs retained 90 days, archived to object storage           |
| Recovery            | Scheduler restart recovers in-progress runs within 60 seconds    |

---

### Deep Dive Areas (Required)

You must address **all** of the following areas with depth:

1. **Scheduler Loop and DAG Parsing**
   * How does the scheduler discover and parse DAG files?
   * What is the scheduler loop: how does it create DAG runs, evaluate
     task dependencies, and transition task instance states?
   * How do you handle 10,000 DAGs without the scheduler loop taking
     minutes?
   * What happens when a DAG file has a syntax error — does it break
     other DAGs?
   * How does the DagBag / DAG processor pool work?

2. **Task Instance State Machine and Dependency Resolution**
   * What are the task instance states and transitions?
   * How does the scheduler determine which task instances are runnable?
   * How do trigger rules work (all_success vs. all_done vs. one_success)?
   * What happens when an upstream task is skipped — does the downstream
     run?

3. **Executor Design and Pluggability**
   * How does the executor interface work?
   * Compare LocalExecutor, CeleryExecutor, KubernetesExecutor in depth.
   * How does KubernetesExecutor handle pod lifecycle: creation, log
     collection, timeout, OOMKilled detection, cleanup?
   * What is the DBOS transactional executor model and how does it
     achieve exactly-once execution?

4. **Data Passing: XCom, Result Backends, and the Serialization Problem**
   * How are task results passed between tasks?
   * What are the failure modes of storing XCom in the metadata DB?
   * How do external XCom backends (S3, GCS) work?
   * How does TaskFlow API infer XCom edges from function signatures?

5. **Backfill, Catchup, and Logical Date Semantics**
   * What is the difference between `execution_date` / `logical_date`
     and the actual run time?
   * How does catchup work when a DAG is paused for a week?
   * How does backfill create and schedule runs for a historical window?
   * What are the gotchas with re-running tasks that depend on
     `{{ ds }}` (the templated logical date)?

6. **Sensor and Deferrable Operator Architecture**
   * How do sensors block a DAG run waiting for an external condition?
   * What is the "sensor deadlock" problem (all pool slots consumed
     by waiting sensors)?
   * How do deferrable operators and the Triggerer solve this?
   * What is the Triggerer architecture (asyncio event loop, triggers,
     deferred events)?

7. **Metadata Database Design and Scale**
   * What is the schema for DAGs, DAG runs, task instances, XCom,
     pools, connections, and logs?
   * How does the metadata DB grow and what causes it to become the
     bottleneck?
   * What archival and retention strategies keep it performant?
   * How does DBOS use the database differently (as the execution
     engine, not just metadata)?

8. **Dynamic DAGs, Mapped Tasks, and Runtime Graph Expansion**
   * How do dynamic tasks (`.expand()` / mapped tasks) work?
   * How does the scheduler handle a DAG whose task count changes
     between runs?
   * What is the difference between DAG-time expansion and runtime
     expansion?

---

### Constraints & Assumptions

* DAG definitions are Python files deployed to a shared filesystem or
  Git-synced volume accessible to the scheduler and workers.
* The metadata database is PostgreSQL (or MySQL) — the engine does not
  use NoSQL for metadata.
* Workers are stateless. Task state lives in the metadata DB and the
  executor's infrastructure (Kubernetes API, Celery broker).
* Clock skew between scheduler, workers, and DB is bounded (NTP, < 1 second).
* Task payloads (XCom) for the DB backend are < 48 KB. Larger artifacts
  go through an external backend.
* The deployment target is Kubernetes, with the scheduler, webserver, and
  triggerer as separate deployments.

---

### Evaluation Criteria

| Criteria                                    | Weight |
|---------------------------------------------|--------|
| Scheduler loop + DAG parsing architecture   | 20%    |
| Task state machine + dependency resolution  | 15%    |
| Executor design + KubernetesExecutor depth  | 15%    |
| XCom / data passing architecture            | 10%    |
| Backfill + logical date semantics           | 10%    |
| Sensor / deferrable operator design         | 10%    |
| Metadata DB schema + scale                  | 10%    |
| Dynamic DAGs / mapped tasks                 | 5%     |
| Operational readiness (monitoring, failure)  | 5%     |

---

### What Sets This Apart from a Job Scheduler and a Workflow Engine

| Dimension              | Job Scheduler                          | DAG Pipeline Orchestrator                  | Durable Workflow Engine (Temporal)         |
|------------------------|----------------------------------------|--------------------------------------------|--------------------------------------------|
| Unit of work           | Single task/job                        | DAG of tasks with dependency edges         | Multi-step stateful code process           |
| Definition             | Job type + payload + schedule          | Python DAG with operators/tasks            | Code with SDK primitives                   |
| Dependencies           | None                                   | Explicit graph edges, trigger rules        | Implicit in code flow                      |
| Scheduling model       | Cron or one-shot per job               | Calendar intervals, datasets, timetables   | Timer-based, signal-driven                 |
| Data passing           | None                                   | XCom, result backends, datasets            | Activity results in event history          |
| Backfill               | Not applicable                         | First-class: reprocess any date range      | Not applicable (replay ≠ backfill)         |
| Execution model        | Worker claims + executes               | Executor dispatches (proc, Celery, K8s)    | Worker replays + executes activities       |
| Failure recovery       | Retry the job                          | Retry the task, clear-and-rerun the DAG    | Replay the workflow from event history     |
| Side-effect guarantee  | At-least-once (retries)                | At-least-once (or exactly-once via DBOS)   | Exactly-once via deterministic replay      |
| Graph shape            | N/A                                    | Known at parse time (or dynamic at runtime)| Implicit in code (not a graph)             |
| Primary use case       | Async task processing                  | Data pipelines, ETL, ML workflows          | Business processes, sagas, long-running tx |

---

### Reference Systems

Study these for inspiration:

* **Apache Airflow** — The canonical open-source DAG orchestrator
* **Prefect** — Python-native, dynamic DAGs, hybrid execution
* **Dagster** — Software-defined assets, type-checked data passing
* **DBOS** — Transactional workflow execution backed by PostgreSQL
* **Luigi** — Spotify's predecessor, target-based (file existence)
* **Mage** — Notebook-first pipeline builder
* **Argo Workflows** — Kubernetes-native DAG execution (YAML-based)
* **Flyte** — Type-safe, container-native ML pipelines
