## System Design Task: Temporal-Style Workflow Orchestration Engine

### Problem Statement

Design a **durable workflow orchestration engine** — in the spirit of
**Temporal, Cadence, and Azure Durable Functions** — that enables developers
to write complex, long-running business processes as **ordinary code** while
the platform guarantees **durability, fault tolerance, and exactly-once
execution semantics**, even across process crashes, machine failures, and
deployments that span days, weeks, or months.

A **job scheduler** fires individual tasks on a schedule and retries them on
failure. A **workflow orchestration engine** is a categorically different
system: it models **multi-step, stateful business processes** — where steps
have data dependencies, branching logic, parallel fan-out/fan-in, human
approval gates, saga compensations, long-running timers (wait 30 days, then
send a reminder), child workflows, and versioned code evolution — and
guarantees that the entire process executes to completion or is compensated,
no matter how many failures occur along the way.

The critical insight is **durable execution**: the engine transparently
persists every side-effect (activity result, timer expiry, signal receipt)
so that when a workflow worker crashes mid-execution, the engine can
**replay** the workflow's deterministic code from the beginning, feeding it
the recorded history, and the workflow resumes exactly where it left off —
without re-executing any side-effect that already succeeded. This is not
retry; this is deterministic replay of execution history.

**Why this is hard:**

* **Replay correctness** demands that workflow code is deterministic. Any
  non-determinism (random numbers, system clock reads, unordered map
  iteration) breaks replay. The engine must detect and flag violations.
* **History size** grows with every event. A workflow running for 6 months
  with thousands of activities accumulates a history that is expensive to
  replay from scratch. Continue-as-new, checkpointing, and history
  compaction are required.
* **Versioning** is uniquely hard: you deploy a new version of workflow code,
  but thousands of in-flight workflows were started with the old version.
  You need a **patching / versioning mechanism** that lets old histories
  replay correctly on new code.
* **Saga compensation** must unwind a partially completed workflow when a
  late step fails — but compensation itself can fail, creating nested
  failure scenarios.
* **Timer durability** means a `sleep(30 days)` must survive process restarts,
  machine failures, and even cluster migrations — without any worker holding
  state in memory for 30 days.
* **Task routing** (sticky execution, worker-specific task queues, rate
  limiting) adds scheduling complexity beyond a simple task queue.

This engine will serve as the backbone for business-critical processes:
**order fulfillment, payment processing, subscription lifecycle, onboarding
flows, data pipeline orchestration, and CI/CD pipelines** — any process
that is too important to fail silently and too complex for a single
retryable job.

---

### Functional Requirements

1. **Workflow Definition**

   * Developers write workflows as code (Go, Python, TypeScript, Java) using
     an SDK — not YAML, not a DAG DSL. The SDK provides async primitives:
     `execute_activity()`, `sleep()`, `wait_for_signal()`,
     `start_child_workflow()`, `side_effect()`.
   * Workflow code must be **deterministic**: no I/O, no random, no
     system clock. All non-deterministic operations go through activities
     or `side_effect()`.
   * Workflows are identified by a **workflow type** (the function name) and
     a **workflow ID** (client-provided, unique per namespace). Starting a
     workflow with a duplicate ID is rejected or follows a configurable
     ID-reuse policy (allow if previous completed, terminate previous, etc.).

2. **Activity Execution**

   * Activities are the unit of side-effect: calling an API, writing to a
     database, sending an email, charging a credit card. They run in
     activity workers, not in the workflow worker.
   * Activities have configurable **retry policies**: initial interval,
     backoff coefficient, maximum interval, maximum attempts, non-retryable
     error types.
   * Activities have a **start-to-close timeout** (max execution time per
     attempt) and a **schedule-to-close timeout** (max wall-clock time
     including all retries).
   * Activities can **heartbeat**: long-running activities periodically
     report progress. If a heartbeat is missed, the engine declares the
     activity timed out and reschedules it. The heartbeat carries a
     payload so the new attempt can resume where the previous left off.
   * **Local activities**: lightweight activities that execute in the
     workflow worker process (no task queue dispatch) for low-latency,
     non-critical side-effects.

3. **Durable Timers**

   * `workflow.sleep(duration)` creates a durable timer persisted in the
     engine. The workflow worker does not hold any state during the sleep.
   * Timers survive worker restarts, deployments, and cluster failovers.
   * Timer granularity: 1 second minimum, up to years.
   * Support for **cron workflows**: a workflow that re-executes on a cron
     schedule, each run starting fresh with access to the previous run's
     result.

4. **Signals and Queries**

   * **Signals**: external events sent to a running workflow
     (`workflow.signal("approve", payload)`). The workflow code can block
     waiting for a signal. Signals are durable — if the workflow is not
     currently being processed, the signal is enqueued and delivered on the
     next replay.
   * **Queries**: read-only requests to inspect a running workflow's state
     without affecting its execution or history. Queries execute against the
     workflow's in-memory state (populated by replaying history).
   * **Updates** (Temporal-style): a mutation + return-value operation that
     validates and processes input within the workflow, producing a durable
     result — unlike a signal (fire-and-forget) or query (read-only,
     non-durable).

5. **Child Workflows and Workflow Composition**

   * A workflow can start child workflows that run independently with their
     own history. The parent can wait for the child's result or fire-and-forget.
   * A parent workflow's failure can be configured to **terminate**,
     **abandon**, or **request cancellation** of child workflows.
   * **Continue-as-new**: when a workflow's history grows too large, the
     workflow completes and restarts itself with a fresh history, carrying
     forward a summary payload. This is essential for long-running or
     polling workflows.

6. **Saga / Compensation**

   * The SDK provides a **saga pattern** for distributed transactions: each
     step registers a compensation function. If a later step fails, the
     engine executes compensations in reverse order.
   * Compensations are activities and follow the same retry/timeout policies.
   * **Partial compensation failures** are tracked: the workflow surfaces
     which compensations succeeded and which failed, enabling manual
     intervention.

7. **Visibility and Search**

   * List and filter workflows by status, type, start time, execution time,
     and **custom search attributes** (key-value pairs set by the workflow
     code, indexed for efficient queries).
   * **Full history retrieval**: download the complete event history of any
     workflow for debugging or auditing.

8. **Namespace Isolation**

   * Workflows are organized into **namespaces** (multi-tenancy boundary).
   * Each namespace has independent: workflow ID uniqueness, retention
     policies, search attribute schemas, and quotas.
   * Cross-namespace communication is explicitly disallowed (a workflow in
     namespace A cannot start a child in namespace B).

9. **APIs**

   ```
   POST   /api/v1/namespaces/{ns}/workflows                — Start a workflow
   GET    /api/v1/namespaces/{ns}/workflows/{id}            — Get workflow status
   POST   /api/v1/namespaces/{ns}/workflows/{id}/signal     — Send a signal
   POST   /api/v1/namespaces/{ns}/workflows/{id}/query      — Query workflow state
   POST   /api/v1/namespaces/{ns}/workflows/{id}/cancel     — Request cancellation
   POST   /api/v1/namespaces/{ns}/workflows/{id}/terminate  — Force terminate
   GET    /api/v1/namespaces/{ns}/workflows/{id}/history    — Get event history
   GET    /api/v1/namespaces/{ns}/workflows?query=...       — Search workflows
   ```

---

### Non-Functional Requirements

| Requirement         | Target                                                           |
|---------------------|------------------------------------------------------------------|
| Throughput          | 10,000 workflow starts/sec, 50,000 activity completions/sec      |
| Latency             | Workflow task schedule-to-start P99 < 200ms                      |
| Availability        | 99.99% for the orchestration engine (4-nines)                    |
| Durability          | Zero lost workflow state — every persisted event is durable      |
| History size        | Support workflows with up to 50,000 events per execution         |
| Timer durability    | Timers accurate to ±1 second, surviving cluster restarts         |
| Retention           | Completed workflows retained for 30 days (configurable/namespace)|
| Namespace scale     | 1,000+ namespaces, 10M+ concurrent open workflows               |
| Recovery            | Shard failover < 30 seconds                                      |

---

### Deep Dive Areas (Required)

You must address **all** of the following areas with depth:

1. **Event Sourcing and Deterministic Replay**
   * How is workflow state reconstructed from its event history?
   * What happens when replay encounters a non-determinism error?
   * How do you prevent workflow code from accidentally doing I/O?
   * What is the performance cost of replaying a 10,000-event history, and
     how do you mitigate it (caching, sticky execution, checkpoints)?

2. **History Management and Continue-as-New**
   * How does event history grow, and what are the memory/storage costs?
   * When and how does `continue_as_new` trigger?
   * How do you handle a workflow that has been running for 6 months with
     100,000 activity completions?

3. **Workflow Versioning and Patching**
   * A workflow started on code v1 is now running on workers with code v2.
     The histories are incompatible. How do you handle this?
   * What is the patching API (`workflow.patched("my-change-id")`) and how
     does it work during replay vs. first execution?
   * How do you safely deprecate old code paths?

4. **Task Queue Design and Worker Routing**
   * How are workflow tasks and activity tasks dispatched to workers?
   * What is sticky execution and why does it matter for replay performance?
   * How do you handle worker-specific task queues (e.g., activities that
     must run on a GPU worker or in a specific region)?
   * Rate limiting: per-task-queue, per-namespace, and global.

5. **Timer and Schedule Infrastructure**
   * How are millions of durable timers stored and fired efficiently?
   * What data structure supports efficient "what fires in the next second"
     queries at scale?
   * How do you handle timer skew during shard failover?

6. **Saga Compensation and Failure Semantics**
   * Walk through a payment workflow where step 3 of 5 fails: what happens
     to steps 1 and 2's compensations?
   * What if a compensation itself fails?
   * How does cancellation propagate through a workflow with child workflows?

7. **Persistence Layer Design**
   * What is the schema for storing workflow execution state, event history,
     activity results, timers, and visibility data?
   * How is the data sharded (by workflow ID, by namespace, by shard key)?
   * What database(s) are used and why (Cassandra, MySQL, PostgreSQL)?
   * How does the transfer queue + timer queue + visibility store work?

8. **Multi-Tenancy, Sharding, and Cluster Topology**
   * How are workflows assigned to shards, and shards to hosts?
   * What happens during a shard rebalance or host failure?
   * How do you prevent a noisy namespace from affecting others?

---

### Constraints & Assumptions

* Workflow code is written in supported SDKs (Go, Python, Java, TypeScript)
  — the engine does not interpret YAML DAGs or Airflow-style Python.
* The engine itself is the persistence layer — workflows do not touch
  external databases directly (they do so through activities).
* Workers are stateless (workflow state is in the engine) except for the
  in-memory replay cache (sticky execution).
* Clock skew between engine hosts is bounded (NTP, < 1 second).
* Activity payloads are < 2 MB per activity; workflow history size is bounded
  by a configurable limit (default 50,000 events).
* The deployment target is Kubernetes on a major cloud provider.

---

### Evaluation Criteria

| Criteria                                    | Weight |
|---------------------------------------------|--------|
| Event sourcing + replay correctness         | 25%    |
| History management + continue-as-new        | 15%    |
| Versioning / patching mechanism             | 15%    |
| Task queue + worker routing design          | 10%    |
| Timer infrastructure                        | 10%    |
| Saga compensation semantics                 | 10%    |
| Persistence layer + sharding                | 10%    |
| Operational readiness (monitoring, failure)  | 5%     |

---

### What Sets This Apart from a Job Scheduler

| Dimension              | Job Scheduler                          | Workflow Orchestration Engine              |
|------------------------|----------------------------------------|--------------------------------------------|
| Unit of work           | Single task/job                        | Multi-step stateful process                |
| State model            | Simple state machine (pending→running→done) | Full event-sourced history              |
| Failure recovery       | Retry the job                          | Replay the workflow from history           |
| Composition            | None or DAG-only                       | Code-level: if/else, loops, fan-out/fan-in |
| Duration               | Seconds to minutes                     | Seconds to months                          |
| Side-effect guarantee  | At-least-once (retries)                | Exactly-once via replay                    |
| Versioning             | Not applicable                         | Patching API for in-flight workflows       |
| Compensation           | Not applicable                         | Saga with ordered rollback                 |
| Signals/events         | Not applicable                         | First-class: signal, query, update         |

---

### Reference Systems

Study these for inspiration:

* **Temporal** — The canonical open-source durable execution engine
* **Cadence** — Uber's predecessor to Temporal
* **Azure Durable Functions** — Microsoft's serverless durable execution
* **AWS Step Functions** — State-machine-based (JSON DSL, different paradigm)
* **Netflix Conductor** — Orchestration engine (JSON DSL, less code-centric)
* **Restate** — Newer entrant, durable execution with virtual objects
* **Inngest** — Event-driven durable functions
