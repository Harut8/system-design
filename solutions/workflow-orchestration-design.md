# Temporal-Style Workflow Orchestration Engine: Design Document

> Solution to [`tasks/workflow-orchestration.md`](../tasks/workflow-orchestration.md).

---

## Table of Contents

1. [Requirements Clarification](#1-requirements-clarification)
2. [Capacity Estimates](#2-capacity-estimates)
3. [High-Level Architecture](#3-high-level-architecture)
4. [Core Abstraction: Event Sourcing and Deterministic Replay](#4-core-abstraction-event-sourcing-and-deterministic-replay)
5. [The Workflow State Machine](#5-the-workflow-state-machine)
6. [History Management and Continue-as-New](#6-history-management-and-continue-as-new)
7. [Task Queue Design and Worker Routing](#7-task-queue-design-and-worker-routing)
8. [Sticky Execution and Replay Optimization](#8-sticky-execution-and-replay-optimization)
9. [Workflow Versioning and Patching](#9-workflow-versioning-and-patching)
10. [Timer and Schedule Infrastructure](#10-timer-and-schedule-infrastructure)
11. [Signals, Queries, and Updates](#11-signals-queries-and-updates)
12. [Saga Compensation and Failure Semantics](#12-saga-compensation-and-failure-semantics)
13. [Child Workflows and Composition](#13-child-workflows-and-composition)
14. [Persistence Layer Design](#14-persistence-layer-design)
15. [Sharding, Replication, and Cluster Topology](#15-sharding-replication-and-cluster-topology)
16. [Multi-Tenancy and Namespace Isolation](#16-multi-tenancy-and-namespace-isolation)
17. [SDK Architecture and Worker Runtime](#17-sdk-architecture-and-worker-runtime)
18. [API Design](#18-api-design)
19. [Observability and Debugging](#19-observability-and-debugging)
20. [Failure Walkthroughs](#20-failure-walkthroughs)
21. [Trade-offs](#21-trade-offs)
22. [Evolution Path](#22-evolution-path)
- [Security, Privacy, and Abuse Prevention](#security-privacy-and-abuse-prevention)

---

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|---|---|---|
| **Scope** | Does the engine own activity execution, or just dispatch? | The engine **dispatches** activity tasks to worker processes via task queues. Workers execute the activity code and report results. The engine owns workflow state, history, timers, and dispatch — not the business logic inside activities. |
| **Persistence** | Is the engine opinionated about the backing store? | The engine abstracts persistence behind a **pluggable store interface**. The reference implementation targets **Cassandra** for horizontal scale (Temporal's production choice) with a **PostgreSQL option** for simpler deployments. The design supports both. |
| **Workflow duration** | What is the maximum workflow lifetime? | **Unbounded**, with a practical recommendation to use `continue_as_new` every ~50,000 events. A subscription lifecycle workflow can run for years. |
| **SDK languages** | Are all SDKs feature-equivalent? | Yes. Each SDK (Go, Python, TypeScript, Java) implements the same contract: the workflow runs deterministic code, the SDK intercepts calls to `execute_activity()`, `sleep()`, `wait_for_signal()` etc., and either replays from history or issues new commands. The SDK is a **replay engine**, not a simple client library. |
| **Deployment** | Self-hosted or managed? | Designed for **self-hosted on Kubernetes**. A managed offering wraps the same engine with provisioning, autoscaling, and billing — but the core design is identical. |
| **Consistency** | What is the consistency model? | **Per-workflow linearizable**: all operations on a single workflow execution are serialized through the shard that owns it. Cross-workflow operations are eventually consistent. History is append-only and immutable once written. |
| **Serialization** | How are payloads encoded? | **Pluggable data converter** — default is JSON, but Protocol Buffers, MessagePack, or encrypted payloads are supported. The engine treats payloads as opaque bytes; only the SDK encodes/decodes. |
| **Backward compat** | Can the engine run Temporal-compatible workflows? | The design is **Temporal-inspired** but not wire-compatible. The concepts (event sourcing, task queues, signals, queries, continue-as-new, patching) map 1:1. Migration is an SDK-level concern. |

### Key Assumptions

1. **The hardest problem is replay correctness, not scale.** A replay engine that produces wrong results under any failure scenario is useless regardless of throughput. Correctness is the load-bearing wall; everything else is insulation.
2. **Workflow code is deterministic by convention, enforced by the SDK.** The engine does not run workflow code in a sandbox — it trusts the SDK to intercept non-deterministic operations. If a developer bypasses the SDK and calls `time.Now()` directly, the engine detects the non-determinism mismatch during replay and fails the workflow task, not silently.
3. **Event history is the source of truth.** There is no "current state" table that can diverge from history. The workflow's state is always reconstructable from its history. Mutable state tables (like `current_run` or `activity_info`) are **materialized views** of history — derivable, rebuildable, and exist only for performance.
4. **Workers are cattle.** Any worker can process any workflow task (within the same task queue). Sticky execution is an optimization, not a correctness requirement. When a sticky worker dies, the workflow is dispatched to any available worker and replayed from scratch.
5. **Activities are not deterministic, and that is the point.** Activities are where side-effects happen — calling APIs, writing databases, sending emails. The engine records activity results in history so they are never re-executed on replay.
6. **Namespace isolation is a hard boundary.** A misbehaving workflow in one namespace cannot consume resources allocated to another. Rate limits, shard assignments, and quotas are per-namespace.

### What We Are Explicitly Not Promising

- **Not** YAML/JSON DAG execution. If you want Airflow or Step Functions, this is the wrong engine. Workflows are code.
- **Not** sub-second timer precision. Timer granularity is 1 second; for sub-second scheduling, use activity-level timeouts.
- **Not** exactly-once delivery of signals. Signals are delivered at-least-once; the workflow code must be idempotent to duplicate signals (the engine deduplicates by event ID within a window).
- **Not** automatic non-determinism prevention. The SDK warns and the engine detects mismatches, but a developer who calls `rand()` directly inside workflow code will discover the problem at replay time, not compile time.

---

## 2. Capacity Estimates

### Core Scale Numbers

| Quantity | Value | Derivation |
|---|---|---|
| Workflow starts/sec | 10,000 | Stated NFR |
| Activity completions/sec | 50,000 | Stated NFR; ~5 activities per workflow avg |
| Concurrent open workflows | 10M+ | Stated NFR |
| History events/sec (writes) | ~150,000 | Each workflow start = ~3 events (started + first task scheduled + first task started); each activity = ~3 events (scheduled, started, completed). 10K starts x 3 + 50K completions x 2 (started + completed; scheduled was already written) |
| Avg events per workflow | 30 | Simple workflows: 10-15, complex: 100-500, long-running w/ continue-as-new: 50K per execution |
| Total stored events (30d retention) | ~40B | 150K events/sec x 86,400 sec/day x 30 days ≈ 389B. In practice, ~10% of peak sustained = ~40B |

### Storage Sizing

```
Event history (30-day retention):
  Events:           40 billion
  Avg event size:   500 bytes (type + payload + metadata)
  Raw data:         40B x 500B = 20 TB
  With replication:  20 TB x 3 replicas = 60 TB (Cassandra RF=3)

Visibility store (search attributes):
  Open workflows:   10M x 1 KB avg = 10 GB
  Closed (30 days): ~26B workflows x 500B avg = 13 TB
  Elasticsearch:    ~13 TB with indexing overhead

Mutable state (current execution state per workflow):
  Open workflows:   10M x 2 KB avg = 20 GB
  In-memory cache:  Hot set ~1M workflows x 5 KB = 5 GB per history host
```

### Transfer Queue and Timer Queue Throughput

```
Transfer queue writes:       ~150,000/sec (one per history event that triggers an action)
Transfer queue reads:        ~150,000/sec (matching service consumes)
Timer queue writes:          ~10,000/sec (workflow timers + activity timeouts)
Timer queue reads:           varies (batch reads of timers in firing window)
Avg timer count (open):      ~2M (10M workflows x 20% have active timer)
```

### Worker Fleet Sizing

```
Workflow workers:
  Tasks/sec to process:     ~60,000 (starts + signal deliveries + timer fires + activity completions)
  Avg replay time per task: 5ms (warm cache), 50ms (cold, 30-event avg history)
  Tasks per worker:         ~500/sec (async, I/O-bound replay is lightweight)
  Workers needed:           ~120 workflow worker instances

Activity workers:
  Completions/sec:          50,000
  Avg execution time:       200ms
  Concurrency per worker:   100 (async I/O)
  Workers needed:           ~100 activity worker instances (varies by activity type)

History service hosts:
  Shards:                   16,384 (good balance for 10M workflows)
  Events/sec per shard:     ~10 (150K / 16K shards)
  Hosts:                    16-32 (each owns 500-1000 shards)

Matching service hosts:
  Task dispatch/sec:        ~100,000 (workflow tasks + activity tasks)
  Hosts:                    8-16
```

### Network Bandwidth

```
History writes:             150K/sec x 500B = 75 MB/sec
Replay reads (cold):        ~5,000/sec x 15KB avg history = 75 MB/sec
Transfer queue:             150K/sec x 200B = 30 MB/sec
gRPC frontend:              ~200K RPC/sec x 1KB avg = 200 MB/sec
Total cluster bandwidth:    ~500 MB/sec sustained (well within 10Gbps network)
```

---

## 3. High-Level Architecture

```
                            ┌──────────────────────────────────┐
                            │          Frontend Service         │
                            │  (gRPC Gateway, Rate Limiting,    │
                            │   Namespace Routing, Auth)        │
                            └────────┬───────────┬─────────────┘
                                     │           │
                      ┌──────────────┘           └──────────────┐
                      ▼                                         ▼
            ┌──────────────────┐                    ┌──────────────────┐
            │  History Service  │                    │ Matching Service  │
            │  (Shard Owner)    │                    │ (Task Dispatch)   │
            │                  │                    │                  │
            │ ┌──────────────┐ │     Transfer       │ ┌──────────────┐ │
            │ │ Shard 0..N   │─┼────Queue──────────▶│ │ Task Queues  │ │
            │ │              │ │                    │ │ (per type)   │ │
            │ │ • History    │ │                    │ └──────┬───────┘ │
            │ │ • Mutable St │ │                    │        │         │
            │ │ • Timers     │ │                    └────────┼─────────┘
            │ │ • Transfer Q │ │                             │
            │ └──────────────┘ │                    Long-poll │ / sync match
            └────────┬─────────┘                             │
                     │                          ┌────────────┼────────────┐
                     │                          │            │            │
                     ▼                          ▼            ▼            ▼
            ┌──────────────────┐         ┌──────────┐ ┌──────────┐ ┌──────────┐
            │  Persistence     │         │ Workflow  │ │ Activity │ │ Activity │
            │  (Cassandra /    │         │ Worker 1  │ │ Worker 1 │ │ Worker N │
            │   PostgreSQL)    │         │ (replay   │ │ (execute │ │          │
            │                  │         │  engine)  │ │  side-   │ │          │
            │ • Executions     │         └──────────┘ │  effects)│ └──────────┘
            │ • History Events │                      └──────────┘
            │ • Timer Queue    │
            │ • Transfer Queue │         ┌──────────────────────┐
            │ • Visibility     │         │   Visibility Store   │
            └──────────────────┘         │   (Elasticsearch)    │
                                         │   • Search workflows │
                                         │   • Custom attrs     │
                                         └──────────────────────┘
```

### Service Responsibilities

| Service | Responsibility |
|---|---|
| **Frontend** | gRPC API gateway. Validates requests, enforces rate limits, routes to the correct history shard based on `hash(namespace + workflowID)`. Stateless and horizontally scalable. |
| **History** | The brain. Owns workflow execution state. Processes workflow task completions, persists events, manages timers, enqueues transfer tasks. Each instance owns a range of shards. Shard ownership is managed by a membership protocol (ringpop / consistent hashing). |
| **Matching** | Task dispatch. Receives tasks from the transfer queue and matches them to polling workers. Manages task queues (in-memory + persistence for overflow). Handles rate limiting per task queue. |
| **Worker** | Runs outside the engine cluster. **Workflow workers** execute the replay engine (SDK). **Activity workers** execute activity code. Workers long-poll the matching service for tasks. |
| **Persistence** | Pluggable store. Cassandra for scale, PostgreSQL for simplicity. Stores: execution state, event history, timer queue, transfer queue. |
| **Visibility** | Elasticsearch (or compatible). Indexes workflow metadata and custom search attributes for list/search queries. Dual-written from history service. |

### Core Request Flow: Start Workflow

```
1. Client calls StartWorkflow(namespace, workflowID, workflowType, input)
2. Frontend validates, rate-checks, resolves shard:
     shard = hash(namespace + workflowID) % totalShards
3. Frontend forwards to History host owning that shard
4. History service (within a single shard transaction):
   a. Check if workflowID already exists → reject or apply ID-reuse policy
   b. Create execution record in mutable_state table
   c. Append WorkflowExecutionStarted event to history
   d. Create a WorkflowTask (decision task) in the transfer queue
   e. Persist all in a single batch write
5. Transfer queue processor reads the WorkflowTask
6. Forwards it to the Matching service for the target task queue
7. Matching service holds it until a workflow worker long-polls
8. Worker receives the task with the full history (just [WorkflowExecutionStarted])
9. Worker replays the workflow code:
   a. Workflow function runs, encounters execute_activity("charge-card", ...)
   b. SDK sees no matching event in history → this is a NEW command
   c. SDK returns command: ScheduleActivityTask to the engine
10. Worker responds to History with: [ScheduleActivityTask command]
11. History service:
    a. Appends ActivityTaskScheduled event
    b. Creates an ActivityTask in the transfer queue
    c. Updates mutable_state (activity is now pending)
12. Matching service dispatches ActivityTask to an activity worker
13. Activity worker executes the activity, reports result
14. History service:
    a. Appends ActivityTaskCompleted event (with result payload)
    b. Creates another WorkflowTask (so the workflow can process the result)
15. Workflow worker receives the new WorkflowTask with updated history
16. Replay: SDK replays from beginning, encounters execute_activity()
    → finds ActivityTaskScheduled + ActivityTaskCompleted in history
    → returns the recorded result without re-executing
17. Workflow code continues to next step...
```

---

## 4. Core Abstraction: Event Sourcing and Deterministic Replay

This is the most important section. Everything else in the design exists to support this mechanism.

### The Replay Contract

The SDK (running in the workflow worker) maintains an invariant:

```
Given the same workflow code and the same event history,
the SDK will produce the same sequence of commands.
```

Commands are the workflow's "decisions": schedule an activity, start a timer, send a signal to a child, complete the workflow. On first execution, these commands have no matching events in history — they are **new**. The engine records them as events. On replay (after a crash, on a different worker, or when processing the next workflow task), the SDK replays the workflow code, and for each command it issues, it checks the history:

- **Match found**: the event in history matches the command. The SDK returns the recorded result to the workflow code. No side-effect re-executes.
- **No match (end of history)**: the command is new. The SDK emits it to the engine as a pending command.
- **Mismatch**: the command differs from the event at the same position. **Non-determinism error**. The workflow task fails. The engine retries the task (which will fail again if the code hasn't been fixed).

### What Events Look Like

```
Event #1:  WorkflowExecutionStarted   { workflowType: "order-fulfillment", input: {...} }
Event #2:  WorkflowTaskScheduled      { taskQueue: "order-workers" }
Event #3:  WorkflowTaskStarted        { workerIdentity: "worker-3" }
Event #4:  WorkflowTaskCompleted      { commands: [ScheduleActivity("reserve-inventory")] }
Event #5:  ActivityTaskScheduled       { activityType: "reserve-inventory", input: {...} }
Event #6:  ActivityTaskStarted         { workerIdentity: "activity-worker-7" }
Event #7:  ActivityTaskCompleted       { result: { reserved: true, qty: 5 } }
Event #8:  WorkflowTaskScheduled       { ... }
Event #9:  WorkflowTaskStarted         { ... }
Event #10: WorkflowTaskCompleted       { commands: [ScheduleActivity("charge-card")] }
Event #11: ActivityTaskScheduled        { activityType: "charge-card", input: {...} }
...
```

### Replay in Action: SDK Pseudocode

```python
class WorkflowReplayEngine:
    def __init__(self, history: list[Event], workflow_func):
        self.history = history
        self.history_index = 0
        self.commands = []  # new commands to send to engine
        self.workflow_func = workflow_func

    def execute_activity(self, activity_type, input):
        """Called by workflow code. Intercepts the call."""

        # During replay: check if this activity was already scheduled + completed
        scheduled_event = self._find_matching_event(
            EventType.ACTIVITY_TASK_SCHEDULED,
            {"activityType": activity_type}
        )

        if scheduled_event:
            # We've been here before. Find the completion.
            completed_event = self._find_completion_event(scheduled_event.id)
            if completed_event:
                # Activity already ran successfully. Return recorded result.
                return completed_event.result
            elif failed_event := self._find_failure_event(scheduled_event.id):
                raise ActivityError(failed_event.reason)
            else:
                # Activity was scheduled but not yet completed.
                # Block (yield) — we'll be woken when it completes.
                yield BLOCKED

        else:
            # Past end of history. This is a NEW command.
            self.commands.append(Command(
                type=CommandType.SCHEDULE_ACTIVITY,
                activity_type=activity_type,
                input=input,
            ))
            yield BLOCKED  # block until activity completes

    def sleep(self, duration):
        """Durable timer."""
        timer_event = self._find_matching_event(
            EventType.TIMER_STARTED,
            {"duration": duration}
        )
        if timer_event:
            fired_event = self._find_event(EventType.TIMER_FIRED, timer_event.id)
            if fired_event:
                return  # timer already elapsed
            yield BLOCKED
        else:
            self.commands.append(Command(
                type=CommandType.START_TIMER,
                duration=duration,
            ))
            yield BLOCKED

    def _find_matching_event(self, event_type, attrs):
        """Walk history from current position to find matching event."""
        # Sequential command matching — event N in history
        # corresponds to the Nth command the workflow issued
        if self.history_index < len(self.history):
            event = self.history[self.history_index]
            if event.type == event_type:
                self.history_index += 1
                return event
            else:
                raise NonDeterminismError(
                    f"Expected {event_type} at position {self.history_index}, "
                    f"found {event.type}"
                )
        return None  # past end of history
```

### Non-Determinism Detection

The engine detects non-determinism when the sequence of commands produced by replaying the workflow code does not match the sequence of events in history.

**Common causes of non-determinism:**

| Cause | What Happens | Mitigation |
|---|---|---|
| Calling `time.Now()` directly | Different timestamp on replay → different branch taken | SDK provides `workflow.Now()` that returns the workflow task's timestamp |
| Using `rand()` | Different random value on replay | SDK provides `workflow.SideEffect()` which records the value |
| Iterating over a `map` (Go/Python `dict`) | Non-deterministic ordering changes which activity is scheduled first | SDK documentation warns; linters catch this |
| Adding a new activity call in code v2 | History has no event for it at the expected position | Use `workflow.Patched()` (see versioning section) |
| Goroutine/thread scheduling | Different execution order | SDK-managed coroutines, not OS threads |

**When non-determinism is detected**, the workflow task fails with a `NonDeterminismError`. The engine retries the task (it will fail again). The operator sees the error in the workflow's event history and must either:
1. Fix the code and deploy,
2. Reset the workflow to a specific point, or
3. Terminate the workflow.

### Why This Matters: The Job Scheduler Comparison

A job scheduler retries a **failed job from scratch**. A workflow engine replays **from history**, skipping all side-effects that already succeeded. This means:

- A payment workflow where step 1 (charge card) succeeded and step 2 (send email) failed will **not re-charge the card** on retry. The replay sees `ActivityTaskCompleted` for the charge and returns the recorded result.
- A data pipeline workflow that processed 10,000 files and crashed at file 9,999 will **not re-process the first 9,998 files**. It replays, returns all recorded results, and resumes at file 9,999.

This is the fundamental value proposition. Exactly-once side-effect execution, not exactly-once delivery.

---

## 5. The Workflow State Machine

Each workflow execution is modeled as a state machine managed by the history service.

### Execution States

```
                  ┌───────────────────┐
                  │                   │
    StartWorkflow │   RUNNING         │
   ──────────────▶│                   │
                  │  (processing      │
                  │   events,         │◄─────── Signal / Timer / Activity Result
                  │   executing       │
                  │   workflow tasks)  │
                  │                   │
                  └─┬──┬──┬──┬──┬────┘
                    │  │  │  │  │
       ┌────────────┘  │  │  │  └────────────────┐
       ▼               │  │  │                   ▼
  ┌──────────┐         │  │  │           ┌──────────────┐
  │COMPLETED │         │  │  │           │ CONTINUED_    │
  │          │         │  │  │           │ AS_NEW        │
  └──────────┘         │  │  │           └──────────────┘
                       │  │  │                   │
                       ▼  │  │                   ▼
                 ┌────────┐│  │           (new execution
                 │ FAILED ││  │            with fresh
                 └────────┘│  │            history)
                           ▼  │
                   ┌──────────┐│
                   │CANCELLED ││
                   └──────────┘│
                               ▼
                       ┌────────────┐
                       │ TERMINATED │
                       │ (forced)   │
                       └────────────┘
                               │
                               ▼
                       ┌────────────┐
                       │ TIMED_OUT  │
                       └────────────┘
```

### Mutable State: The In-Memory Projection

While history is the source of truth, the history service maintains an in-memory **mutable state** for each active workflow. This is a materialized view built by replaying history — but cached for performance.

```
MutableState {
    execution_info {
        workflow_id:        "order-123"
        run_id:             "abc-def-ghi"
        workflow_type:      "order-fulfillment"
        status:             RUNNING
        start_time:         2024-03-15T10:00:00Z
        history_length:     47
        sticky_task_queue:  "worker-3-sticky-abc"
    }

    pending_activities: {
        activity_id_5: {
            activity_type:    "ship-order"
            scheduled_at:     2024-03-15T10:05:00Z
            attempt:          2
            heartbeat_details: { progress: "label-printed" }
            timeout:          300s
        }
    }

    pending_timers: {
        timer_id_3: {
            fire_at:    2024-04-14T10:00:00Z   // 30-day reminder
            started_at: 2024-03-15T10:05:30Z
        }
    }

    pending_child_workflows: {
        child_wf_1: {
            workflow_id:   "payment-order-123"
            namespace:     "payments"
            status:        RUNNING
        }
    }

    pending_signals: []  // signals waiting to be delivered

    buffered_events: []  // events received while a workflow task is in-flight

    search_attributes: {
        "order_id": "order-123",
        "customer_tier": "premium",
        "total_amount": 299.99
    }
}
```

This mutable state is persisted to the database on every workflow task completion (as a checkpoint) and rebuilt from history on cache miss.

---

## 6. History Management and Continue-as-New

### The History Growth Problem

Every operation on a workflow appends events to its history. A long-running workflow accumulates history:

```
Simple order workflow (10 steps):
  ~30 events x 500B = 15 KB total history
  Replay time: < 1ms

Complex ETL pipeline (100 activities):
  ~300 events x 500B = 150 KB total history
  Replay time: ~5ms

Subscription lifecycle (runs for 12 months, monthly billing):
  12 billing cycles x ~50 events each = 600 events
  + signals + timers + child workflows = ~2,000 events
  ~2,000 events x 500B = 1 MB total history
  Replay time: ~50ms

Polling workflow (check every minute for a year):
  525,600 polls x ~5 events each = 2.6M events
  WITHOUT continue-as-new: 1.3 GB history. Replay time: > 60s.
  This is catastrophic.
```

### Continue-as-New: The Solution

`continue_as_new` completes the current workflow execution and immediately starts a new execution with the same workflow ID but a **fresh history**. The new execution carries forward only a summary payload — not the full history.

```python
async def polling_workflow(state: PollingState):
    for i in range(100):  # process 100 polls per execution
        result = await workflow.execute_activity(
            "poll-external-api",
            args=[state.cursor],
        )
        state.cursor = result.next_cursor
        state.items_processed += result.count

        if result.done:
            return state  # workflow completes

        await workflow.sleep(timedelta(minutes=1))

    # After 100 iterations, continue-as-new with carried-over state
    workflow.continue_as_new(state)
```

### History Size Limits and Enforcement

| Threshold | Action |
|---|---|
| 10,000 events | SDK logs a warning. |
| 30,000 events | Engine adds a `WorkflowExecutionWarning` event. Visible in UI. |
| 50,000 events (configurable) | Engine **forces** the workflow task to fail with `HistorySizeLimitExceeded`. The workflow must call `continue_as_new` to proceed. |

### History Storage Layout

Events are stored in pages to avoid reading the entire history for every operation:

```
┌─────────────────────────────────────────────────┐
│  History Storage (per execution)                 │
│                                                  │
│  Page 0: Events 1-100      ← first page          │
│  Page 1: Events 101-200                          │
│  Page 2: Events 201-300                          │
│  ...                                             │
│  Page N: Events N*100+1 .. current               │
│                                                  │
│  Each page is a single database row:              │
│    partition_key: (shard_id, namespace, wf_id)   │
│    range_key:     (run_id, page_number)           │
│    data:          [serialized events]              │
│    encoding:      proto3 + zstd compression       │
└─────────────────────────────────────────────────┘
```

For **replay**, the SDK reads pages sequentially. For a warm (sticky) worker, only the **latest page** needs to be fetched — previous pages are in the worker's in-memory cache.

### History Archival

Completed workflows are retained for the namespace's retention period (default 30 days). After retention:

1. History is archived to object storage (S3) if archival is enabled.
2. The execution record and history are deleted from the primary store.
3. Archived workflows are still searchable via the visibility store but history retrieval requires a fetch from S3 (higher latency).

---

## 7. Task Queue Design and Worker Routing

### Two Types of Task Queues

The engine maintains two categories of task queues:

```
┌─────────────────────────────────────────────────────────────┐
│  Task Queue: "order-workers"                                 │
│                                                              │
│  ┌─────────────────────┐    ┌─────────────────────┐          │
│  │ Workflow Task Queue  │    │ Activity Task Queue  │          │
│  │                     │    │                     │          │
│  │ (replay tasks for   │    │ (activity execution │          │
│  │  workflow decision  │    │  tasks dispatched   │          │
│  │  processing)        │    │  to activity workers)│          │
│  └─────────┬───────────┘    └─────────┬───────────┘          │
│            │                          │                      │
│  Workers long-poll here       Workers long-poll here         │
│  (workflow workers)           (activity workers)             │
└─────────────────────────────────────────────────────────────┘
```

**Workflow tasks** are dispatched when:
- A new workflow is started
- An activity completes
- A timer fires
- A signal is received

**Activity tasks** are dispatched when:
- A workflow task completes with a `ScheduleActivityTask` command

### Matching Service: Sync Match vs. Persistence

The matching service implements a **sync match** optimization:

```
                     Activity Task arrives
                            │
                            ▼
                    ┌───────────────┐
                    │ Is a worker    │
                    │ currently      │──── YES ──▶ Dispatch directly
                    │ long-polling?  │             (sync match)
                    └───────┬───────┘             ~0ms latency
                            │ NO
                            ▼
                    ┌───────────────┐
                    │ Persist task   │
                    │ to matching    │
                    │ service DB     │
                    └───────┬───────┘
                            │
                      Worker polls later
                            │
                            ▼
                    Dispatch from persisted queue
                    (~50-200ms latency)
```

**Sync match** eliminates database round-trips when workers are available — the task goes directly from the transfer queue to the waiting worker's long-poll response. At scale, 60-80% of tasks are sync-matched.

### Worker-Specific Task Queues

For activities that require specific capabilities (GPU, region, special credentials):

```python
# Workflow code
result = await workflow.execute_activity(
    "gpu-inference",
    task_queue="ml-gpu-workers-us-west-2",  # explicit routing
    args=[model_id, input_data],
    start_to_close_timeout=timedelta(minutes=5),
)
```

The matching service maintains per-queue poller lists. If no worker is polling a task queue, tasks accumulate until a worker appears or the schedule-to-start timeout expires.

### Rate Limiting

Rate limiting is enforced at three levels:

| Level | Mechanism | Purpose |
|---|---|---|
| **Per-task-queue** | Token bucket in matching service, 1 token per task dispatch | Prevent one task queue from monopolizing matching service resources |
| **Per-namespace** | Token bucket in frontend, applied to all API calls | Prevent one namespace from overwhelming the cluster |
| **Global** | Backpressure from history service to frontend | Prevent cluster-wide overload |

```
Rate limiter implementation (per task queue):

  Tokens refill at configured rate (e.g., 1000 tasks/sec)
  Each task dispatch consumes 1 token
  When tokens exhausted:
    - Workflow tasks: buffered (never dropped, just delayed)
    - Activity tasks: buffered, timeout starts
    - Client API calls: rejected with RESOURCE_EXHAUSTED error + retry-after header
```

---

## 8. Sticky Execution and Replay Optimization

### The Replay Cost Problem

Every time a workflow task is processed, the SDK replays the workflow from the beginning of its history. For a workflow with 1,000 events, this means reading 1,000 events and re-executing the workflow code 1,000 steps — just to process the latest event. This is correct but expensive.

### Sticky Execution

Sticky execution routes consecutive workflow tasks to the **same worker** that processed the previous task, so the worker's in-memory cache of the workflow's state is reused.

```
Without sticky execution:
  Worker receives task → reads full history (1,000 events) → replays from event 1
  → processes new event → returns commands
  Cost: replay 1,000 events

With sticky execution:
  Worker receives task → cache hit! → reads only NEW events since last task
  → replays from event 1,000 → processes new event → returns commands
  Cost: replay 1 event (the new one)
```

### How It Works

```
┌─────────────────────────────────────────────────────────────┐
│  History Service                                             │
│                                                              │
│  On WorkflowTaskCompleted from worker-3:                     │
│    1. Record worker-3's "sticky task queue" identity         │
│    2. For the NEXT workflow task, dispatch to:                │
│       a. worker-3's sticky queue (with sticky timeout, e.g. 5s) │
│       b. If not picked up within 5s → fallback to normal queue │
│                                                              │
│  If worker-3 crashes:                                        │
│    Sticky timeout expires → task goes to normal queue         │
│    Any worker picks it up → full replay from history          │
│    New worker becomes the sticky target                       │
└─────────────────────────────────────────────────────────────┘
```

### Workflow Cache Eviction

Each workflow worker maintains an LRU cache of workflow states:

```
Worker Cache (configurable, e.g., 10,000 entries):

  key:   (namespace, workflowID, runID)
  value: {
    replayed_history_length:  997
    workflow_state:           <in-memory state after replaying 997 events>
    last_accessed:            <timestamp>
  }

  Eviction: LRU when cache is full
  On eviction: workflow state is discarded (not persisted — it's
               reconstructable from history)
  Effect of eviction: next workflow task for this workflow goes to
                      normal queue → full replay
```

### Performance Impact

| Scenario | Replay Cost | History Read |
|---|---|---|
| Sticky hit (common case, ~70%) | 1-5 events | 1 page (latest) |
| Sticky miss, warm cache (another worker has it) | 0 events (state transfer not supported — full replay) | Full history |
| Cold start (no cache anywhere) | Full history | Full history |
| After continue-as-new | Fresh execution, ~10 events | 1 page |

For a cluster processing 60,000 workflow tasks/sec with 70% sticky rate:
- 42,000 tasks/sec: ~5ms replay (sticky)
- 18,000 tasks/sec: ~50ms replay (30-event average)
- Weighted average: ~18ms per task

---

## 9. Workflow Versioning and Patching

### The Problem

You have 100,000 running workflows started on code v1. You deploy code v2 which adds a new activity between steps 2 and 3. When a v1 workflow is replayed on v2 code:

```
History (v1):
  Event 5: ActivityTaskScheduled { type: "step-2" }
  Event 6: ActivityTaskCompleted { result: ... }
  Event 7: ActivityTaskScheduled { type: "step-3" }  ← v1 went directly to step-3

Code v2 (after step-2):
  result_2 = await execute_activity("step-2", ...)
  result_2b = await execute_activity("step-2b-new", ...)  ← NEW
  result_3 = await execute_activity("step-3", ...)

Replay on v2:
  Step-2 → matches Event 5/6 ✓
  Step-2b-new → expects ActivityTaskScheduled at Event 7
               → finds ActivityTaskScheduled { type: "step-3" }
               → MISMATCH → NonDeterminismError ✗
```

### The Patching API

The SDK provides a `patched()` function (Temporal calls it `GetVersion()`):

```python
async def order_workflow(order):
    result_2 = await workflow.execute_activity("step-2", order)

    if workflow.patched("add-step-2b"):
        # This block runs for NEW workflows (v2+)
        # For OLD workflows replaying on v2, this block is SKIPPED
        result_2b = await workflow.execute_activity("step-2b-new", order)

    result_3 = await workflow.execute_activity("step-3", order)
```

### How `patched()` Works During Replay

```
┌───────────────────────────────────────────────────────────┐
│  workflow.patched("add-step-2b") execution logic:          │
│                                                            │
│  IF replaying (history has more events):                   │
│    Look for a MarkerRecorded event with patch ID            │
│    "add-step-2b" at current history position                │
│                                                            │
│    IF marker found → patch was active when this execution   │
│      was first run → return True (execute new code path)    │
│                                                            │
│    IF marker NOT found → this execution started before      │
│      the patch was deployed → return False (skip new code)  │
│                                                            │
│  IF NOT replaying (past end of history, first execution):   │
│    Record a MarkerRecorded event: { patchID: "add-step-2b" }│
│    return True (execute new code path)                      │
│                                                            │
└───────────────────────────────────────────────────────────┘
```

### Version Lifecycle

```
Phase 1: Deploy v2 with patched() guard
  - New workflows: execute the new code path, marker recorded
  - Old workflows: skip the new code path on replay (no marker in history)

Phase 2: All v1 workflows have completed (or been terminated)
  - The patched() guard is now dead code for all active workflows

Phase 3: Remove the patched() guard (code cleanup)
  - Now all workflows execute the new code path unconditionally
  - Safe because no v1 histories exist to replay

  DANGER: If you remove the guard while v1 workflows are still running,
  those workflows will hit a NonDeterminismError on their next replay.
  The engine should provide tooling to check: "are there any open
  workflows WITHOUT marker X in their history?"
```

### Multiple Versions Simultaneously

For complex migrations, multiple patches can coexist:

```python
async def payment_workflow(payment):
    if workflow.patched("v2-add-fraud-check"):
        await workflow.execute_activity("fraud-check", payment)

    await workflow.execute_activity("charge-card", payment)

    if workflow.patched("v3-add-receipt"):
        await workflow.execute_activity("send-receipt", payment)

    # v1 workflows: no fraud check, no receipt
    # v2 workflows: fraud check, no receipt
    # v3 workflows: fraud check + receipt
```

### Alternative: Workflow Type Versioning

Some deployments prefer explicit versioning over patching:

```
Task Queue: "payments-v1"  →  Workers running v1 code
Task Queue: "payments-v2"  →  Workers running v2 code

Migration: drain v1 queue (wait for all v1 workflows to complete),
           then decommission v1 workers.
```

This is simpler but requires maintaining separate worker pools during the migration window, and workflows that run for months delay decommissioning.

---

## 10. Timer and Schedule Infrastructure

### Durable Timer Storage

When workflow code calls `workflow.sleep(30 days)`:

1. History service records a `TimerStarted` event in history.
2. History service writes a timer entry to the **timer queue** (a priority queue ordered by fire time).
3. No worker holds any state. The timer exists only in the database.
4. After 30 days, the timer queue processor fires the timer:
   a. Appends `TimerFired` event to history.
   b. Creates a workflow task so the SDK can replay and unblock past the sleep.

### Timer Queue Implementation

```
┌─────────────────────────────────────────────────────────┐
│  Timer Queue (per shard, ordered by fire_time)           │
│                                                          │
│  Storage: Cassandra / PostgreSQL                          │
│                                                          │
│  Schema:                                                  │
│    shard_id           INT                                 │
│    fire_time          TIMESTAMP     -- when to fire       │
│    workflow_id        TEXT                                 │
│    run_id             UUID                                │
│    timer_id           BIGINT                              │
│    task_type          ENUM          -- timer/activity_timeout/wf_timeout  │
│                                                          │
│  PRIMARY KEY: (shard_id, fire_time, workflow_id, timer_id)│
│                                                          │
│  Timer processor (per shard, runs on owning host):        │
│    Every 1 second:                                        │
│      1. Read timers WHERE fire_time <= NOW()              │
│      2. For each fired timer:                             │
│         a. Load workflow mutable state                    │
│         b. Append TimerFired event to history             │
│         c. Create workflow task in transfer queue         │
│         d. Delete timer from timer queue                  │
│      3. All in a batch transaction per workflow           │
│                                                          │
│  Fire-time index allows efficient range scans:            │
│    "Give me all timers for shard 42 that fire before NOW" │
│    At 2M active timers across 16K shards:                 │
│    ~125 timers/shard, most with far-future fire times     │
│    Range scan touches only the few that are due           │
└─────────────────────────────────────────────────────────┘
```

### Timer Skew During Shard Failover

When a shard moves from host A to host B (host A crashed):

```
Timeline:
  T=0:    Host A owns shard 42, timer processor running
  T=5:    Host A crashes
  T=5-30: Shard 42 has no owner (membership protocol detects failure)
  T=30:   Host B acquires shard 42
  T=30:   Host B starts timer processor for shard 42
  T=30:   Host B reads all timers with fire_time <= NOW()
          → fires all timers that were due during the 25-second gap

Impact: timers fire up to 30 seconds late during failover.
        This is within the stated NFR (shard failover < 30 seconds).
        No timer is lost — they are persisted.
```

### Activity Timeouts as Timers

Activity timeouts (start-to-close, schedule-to-close, heartbeat) are implemented as timers:

```
When ScheduleActivityTask command is processed:
  1. Write ActivityTaskScheduled event
  2. Create timer: fire_time = NOW() + schedule_to_close_timeout
     This timer fires if the activity doesn't complete in time.

When ActivityTaskStarted event is received:
  1. Cancel the schedule-to-start timer (if any)
  2. Create timer: fire_time = NOW() + start_to_close_timeout

When ActivityTaskCompleted event is received:
  1. Cancel all activity timers

When heartbeat timeout timer fires:
  1. If last heartbeat was > heartbeat_timeout ago:
     → Mark activity as timed out
     → Schedule retry (if attempts remain)
```

### Cron Workflows

A cron workflow re-executes on a schedule. Each execution is independent with its own history:

```
CronSchedule: "0 9 * * MON"  (every Monday at 9 AM)

Execution 1 (Mon Mar 11):
  History: [Started, ..., Completed { result: "processed 100 items" }]

Execution 2 (Mon Mar 18):
  History: [Started { lastResult: "processed 100 items" }, ..., Completed]
  ← carries previous result as input

Execution 3 (Mon Mar 25):
  ...
```

Implementation: when a cron workflow completes, the engine creates a timer for the next cron fire time. When the timer fires, it starts a new execution (effectively `continue_as_new` with the cron's computed next-fire time).

---

## 11. Signals, Queries, and Updates

### Signals: Durable External Events

```python
# Sending a signal (from external code or another workflow):
client.signal_workflow(
    workflow_id="order-123",
    signal_name="approve",
    payload={"approver": "manager@corp.com", "approved": True}
)

# Receiving a signal (inside workflow code):
async def order_workflow(order):
    await workflow.execute_activity("prepare-order", order)

    # Block until signal received (or timeout)
    approval = await workflow.wait_for_signal(
        "approve",
        timeout=timedelta(hours=24)
    )

    if approval.approved:
        await workflow.execute_activity("ship-order", order)
    else:
        await workflow.execute_activity("cancel-order", order)
```

### Signal Delivery Mechanics

```
1. Client sends signal to Frontend
2. Frontend routes to History service (shard that owns the workflow)
3. History service:
   a. If NO workflow task is in-flight:
      → Append WorkflowExecutionSignaled event to history
      → Create a new workflow task (to wake up the workflow)
   b. If a workflow task IS in-flight (worker is currently processing):
      → Buffer the signal event
      → When the in-flight task completes, append buffered events
      → The NEXT workflow task will include the signal
4. Workflow worker receives task with updated history
5. Replay encounters wait_for_signal() → finds the signal event → returns payload
```

**Why buffer?** The workflow's deterministic code is executing on a worker. If we append the signal event while the worker is mid-replay, the history changes out from under the replay — causing non-determinism. Buffering ensures signals are only visible in the next workflow task.

### Queries: Read-Only State Inspection

Queries execute against the workflow's in-memory state without affecting history.

```python
# Define a query handler in the workflow:
@workflow.query_handler("get-order-status")
def get_order_status():
    return {"status": current_status, "items": items_processed}

# Execute a query (from external code):
status = client.query_workflow(
    workflow_id="order-123",
    query_type="get-order-status"
)
```

**Query execution path:**

1. Frontend routes query to history service.
2. History service dispatches a **query task** to the workflow worker (preferring the sticky worker).
3. Worker replays history to rebuild in-memory state (or uses cache).
4. Worker executes the query handler function against the rebuilt state.
5. Returns result to caller — no events written, no history modification.

**Stale queries**: if the workflow has many buffered events not yet processed, the query result reflects the last processed state, not the absolute latest. This is acceptable for read-only inspection.

### Updates: Validated Mutations

Updates combine the durability of signals with the synchronous response of queries:

```python
# Define an update handler:
@workflow.update_handler("add-item")
async def add_item(item):
    if item.quantity <= 0:
        raise ValueError("quantity must be positive")  # validation
    items.append(item)
    await workflow.execute_activity("reserve-item", item)
    return {"total_items": len(items)}

# Call an update (from external code):
result = client.update_workflow(
    workflow_id="order-123",
    update_name="add-item",
    args=[{"sku": "ABC", "quantity": 2}],
)
# result = {"total_items": 5}  ← synchronous response
```

Updates are persisted as events (`WorkflowExecutionUpdateAccepted`, `WorkflowExecutionUpdateCompleted`) and participate in replay — unlike queries, which leave no trace.

---

## 12. Saga Compensation and Failure Semantics

### The Saga Pattern

A saga is a sequence of operations where each operation has a compensating action. If any operation fails, the compensations for all previously succeeded operations are executed in reverse order.

```python
async def booking_workflow(booking):
    saga = workflow.Saga()

    try:
        # Step 1: Reserve hotel
        hotel = await workflow.execute_activity("reserve-hotel", booking.hotel)
        saga.add_compensation("cancel-hotel-reservation", hotel.reservation_id)

        # Step 2: Reserve flight
        flight = await workflow.execute_activity("reserve-flight", booking.flight)
        saga.add_compensation("cancel-flight-reservation", flight.reservation_id)

        # Step 3: Charge payment
        payment = await workflow.execute_activity("charge-card", booking.payment)
        saga.add_compensation("refund-payment", payment.transaction_id)

        # Step 4: Send confirmation
        await workflow.execute_activity("send-confirmation", {
            "hotel": hotel, "flight": flight, "payment": payment
        })

        return {"status": "booked", "hotel": hotel, "flight": flight}

    except ActivityError as e:
        # Step 3 failed → compensate steps 2 and 1 (reverse order)
        await saga.compensate()
        return {"status": "failed", "reason": str(e)}
```

### Compensation Execution

```
Step 3 (charge-card) fails after 3 retries
    │
    ▼
saga.compensate() is called
    │
    ├─▶ Execute "cancel-flight-reservation" (compensation for step 2)
    │     ├─ Success → record in saga state
    │     └─ Failure → retry per compensation retry policy
    │           └─ All retries exhausted → record FAILED compensation
    │              → continue to next compensation (don't stop)
    │
    └─▶ Execute "cancel-hotel-reservation" (compensation for step 1)
          ├─ Success → record in saga state
          └─ Failure → ...same as above
    │
    ▼
saga.compensate() returns CompensationResult:
  {
    "compensations_succeeded": ["cancel-flight-reservation"],
    "compensations_failed": ["cancel-hotel-reservation"],
    "requires_manual_intervention": true
  }
```

### Compensation Failure Handling

When a compensation itself fails, the system cannot automatically resolve it — this requires human intervention:

```
Compensation Failure Resolution:

1. The workflow completes with status FAILED + compensation_failures field
2. The visibility store indexes this as "requires_intervention"
3. An alert fires (from the observability layer)
4. An operator inspects the workflow history:
   - Sees "cancel-hotel-reservation" failed with "connection timeout"
   - Manually calls the hotel's cancellation API
   - Marks the compensation as resolved via admin API
5. Alternatively, the workflow can be designed to retry compensations
   periodically (with a long timer) rather than giving up:

   async def compensate_with_retry(saga):
       for attempt in range(10):
           result = await saga.compensate()
           if not result.has_failures:
               return result
           await workflow.sleep(timedelta(hours=1))  # durable retry
       return result  # give up after 10 hours
```

### Cancellation Propagation

When a workflow is cancelled:

```
Parent workflow receives CancellationRequested
    │
    ▼
Workflow code's context is cancelled
    │
    ├─▶ Pending activities receive cancellation
    │     └─ Activity can check for cancellation and clean up
    │
    ├─▶ Pending child workflows:
    │     ├─ Policy: TERMINATE → child is force-terminated
    │     ├─ Policy: REQUEST_CANCEL → child receives CancellationRequested
    │     │    (child's code decides whether to honor it)
    │     └─ Policy: ABANDON → child continues running independently
    │
    └─▶ Pending timers are cancelled
         └─ Timer events are written to history as TimerCancelled

The workflow code receives the cancellation as a CancelledError.
The workflow can catch it and run cleanup logic (saga compensation).
```

---

## 13. Child Workflows and Composition

### When to Use Child Workflows vs. Activities

| Use Case | Child Workflow | Activity |
|---|---|---|
| Operation needs its own retry/timeout policy | Yes | Yes |
| Operation has its own saga/compensation | **Yes** — needs its own history | No |
| Operation has its own signals/timers | **Yes** — first-class workflow features | No |
| Operation's result is needed by parent | Both work | Both work |
| Operation should survive parent failure | **Yes** (ABANDON policy) | No (activity is tied to parent) |
| Simple API call or DB write | No — too heavyweight | **Yes** |
| Long-running sub-process (hours/days) | **Yes** — avoids parent history bloat | No |

### Child Workflow Lifecycle

```python
async def parent_workflow(order):
    # Start child workflow — blocks until child completes (or fire-and-forget)
    payment_result = await workflow.execute_child_workflow(
        "payment-workflow",
        workflow_id=f"payment-{order.id}",
        args=[order.payment_info],
        parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
    )

    if payment_result.success:
        # Start another child for fulfillment
        await workflow.execute_child_workflow(
            "fulfillment-workflow",
            workflow_id=f"fulfill-{order.id}",
            args=[order, payment_result],
        )
```

### History Events for Child Workflows

```
Parent history:
  Event 20: StartChildWorkflowExecutionInitiated { workflowType: "payment-workflow" }
  Event 21: ChildWorkflowExecutionStarted        { workflowId: "payment-order-123" }
  ...
  Event 35: ChildWorkflowExecutionCompleted       { result: {...} }

Child history (separate, independent):
  Event 1: WorkflowExecutionStarted    { parentWorkflowId: "order-123" }
  Event 2: WorkflowTaskScheduled       { ... }
  ...
  Event 18: WorkflowExecutionCompleted  { result: {...} }
```

The parent's history records **that** a child was started and completed, but not the child's internal events. This keeps the parent's history compact.

---

## 14. Persistence Layer Design

### Schema Design (Cassandra)

The persistence layer uses four logical stores, each optimized for its access pattern:

#### Executions Store

Stores workflow mutable state and metadata.

```sql
CREATE TABLE executions (
    shard_id         INT,
    namespace_id     TEXT,
    workflow_id      TEXT,
    run_id           UUID,
    -- Mutable state (serialized protobuf)
    execution_state  BLOB,
    -- Denormalized fields for fast filtering
    workflow_type    TEXT,
    status           TEXT,       -- RUNNING, COMPLETED, FAILED, ...
    start_time       TIMESTAMP,
    close_time       TIMESTAMP,
    -- Checksum for optimistic concurrency (compare-and-swap)
    db_record_version BIGINT,
    PRIMARY KEY ((shard_id), namespace_id, workflow_id)
);
```

#### History Store

Append-only event log, paginated.

```sql
CREATE TABLE history_events (
    shard_id         INT,
    tree_id          UUID,       -- execution tree (for continue-as-new chains)
    branch_id        UUID,       -- specific branch (run)
    node_id          BIGINT,     -- event sequence number
    -- Event data
    data             BLOB,       -- serialized event (protobuf + compression)
    data_encoding    TEXT,       -- "proto3/zstd"
    PRIMARY KEY ((shard_id, tree_id), branch_id, node_id)
) WITH CLUSTERING ORDER BY (branch_id ASC, node_id ASC);
```

**Tree/branch model**: when a workflow uses `continue_as_new`, the new execution shares the same `tree_id` but gets a new `branch_id`. This enables efficient "get all events across all runs of this workflow" queries.

#### Transfer Queue

Tasks that need to be dispatched to the matching service.

```sql
CREATE TABLE transfer_tasks (
    shard_id         INT,
    task_id          BIGINT,     -- monotonically increasing per shard
    -- Task data
    namespace_id     TEXT,
    workflow_id      TEXT,
    run_id           UUID,
    task_type        TEXT,       -- WorkflowTask, ActivityTask, CloseExecution, ...
    task_queue       TEXT,       -- target task queue name
    schedule_id      BIGINT,     -- event ID that triggered this task
    data             BLOB,
    PRIMARY KEY ((shard_id), task_id)
) WITH CLUSTERING ORDER BY (task_id ASC);
```

The transfer queue is **pulled** by the history service's queue processor, which forwards tasks to the matching service. Each shard has its own transfer queue, processed by the host that owns the shard.

#### Timer Queue

Timers ordered by fire time (see section 10).

```sql
CREATE TABLE timer_tasks (
    shard_id         INT,
    fire_time        TIMESTAMP,
    -- Task identification
    namespace_id     TEXT,
    workflow_id      TEXT,
    run_id           UUID,
    timer_type       TEXT,       -- WorkflowTimer, ActivityTimeout, WorkflowTimeout
    timer_id         BIGINT,
    data             BLOB,
    PRIMARY KEY ((shard_id), fire_time, namespace_id, workflow_id, timer_id)
) WITH CLUSTERING ORDER BY (fire_time ASC);
```

### Write Path: Workflow Task Completion

When a workflow worker responds with commands, the history service executes a **single batch write**:

```
Batch Write (atomic per workflow, within one shard):
  1. Append new events to history_events
  2. Update mutable state in executions
  3. Insert transfer tasks (for activity dispatch, child workflow start, etc.)
  4. Insert timer tasks (for workflow.sleep(), activity timeouts)
  5. Update visibility record (search attributes)
  6. Increment db_record_version (optimistic concurrency check)

Cassandra: BATCH (logged batch within same partition — shard_id)
PostgreSQL: single transaction
```

This atomic write is **the** correctness guarantee. Either all state changes are persisted, or none are. There is no window where history is updated but the transfer task is missing.

### PostgreSQL Alternative Schema

For simpler deployments (< 1M concurrent workflows):

```sql
CREATE TABLE workflow_executions (
    shard_id         INTEGER NOT NULL,
    namespace_id     VARCHAR(255) NOT NULL,
    workflow_id      VARCHAR(255) NOT NULL,
    run_id           UUID NOT NULL,
    workflow_type    VARCHAR(255) NOT NULL,
    status           VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
    execution_state  BYTEA NOT NULL,
    start_time       TIMESTAMPTZ NOT NULL,
    close_time       TIMESTAMPTZ,
    search_attributes JSONB,
    db_record_version BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY (shard_id, namespace_id, workflow_id)
) PARTITION BY HASH (shard_id);

-- Partition into 16 hash partitions for parallel vacuum + query
CREATE TABLE workflow_executions_0 PARTITION OF workflow_executions
    FOR VALUES WITH (MODULUS 16, REMAINDER 0);
-- ... through _15

CREATE TABLE history_events (
    shard_id         INTEGER NOT NULL,
    namespace_id     VARCHAR(255) NOT NULL,
    workflow_id      VARCHAR(255) NOT NULL,
    run_id           UUID NOT NULL,
    event_id         BIGINT NOT NULL,
    event_type       VARCHAR(100) NOT NULL,
    data             BYTEA NOT NULL,
    PRIMARY KEY (shard_id, namespace_id, workflow_id, run_id, event_id)
) PARTITION BY HASH (shard_id);

CREATE TABLE transfer_tasks (
    shard_id         INTEGER NOT NULL,
    task_id          BIGSERIAL,
    task_type        VARCHAR(50) NOT NULL,
    namespace_id     VARCHAR(255) NOT NULL,
    workflow_id      VARCHAR(255) NOT NULL,
    run_id           UUID NOT NULL,
    task_queue       VARCHAR(255),
    data             BYTEA,
    PRIMARY KEY (shard_id, task_id)
) PARTITION BY HASH (shard_id);

CREATE INDEX idx_transfer_tasks_pending
    ON transfer_tasks (shard_id, task_id ASC);

CREATE TABLE timer_tasks (
    shard_id         INTEGER NOT NULL,
    fire_time        TIMESTAMPTZ NOT NULL,
    namespace_id     VARCHAR(255) NOT NULL,
    workflow_id      VARCHAR(255) NOT NULL,
    run_id           UUID NOT NULL,
    timer_id         BIGINT NOT NULL,
    timer_type       VARCHAR(50) NOT NULL,
    data             BYTEA,
    PRIMARY KEY (shard_id, fire_time, workflow_id, timer_id)
) PARTITION BY HASH (shard_id);

CREATE INDEX idx_timer_tasks_fire
    ON timer_tasks (shard_id, fire_time ASC);
```

**PostgreSQL trade-offs vs. Cassandra:**

| Dimension | Cassandra | PostgreSQL |
|---|---|---|
| Scale ceiling | Linear scale-out, 10M+ workflows | ~1M workflows per instance, vertical scale |
| Write throughput | 150K+/sec (distributed) | 20-30K/sec per primary |
| Operational complexity | High (compaction tuning, repair, multi-DC) | Low (pgBouncer + replicas + partitioning) |
| Consistency | Per-partition linearizable (lightweight transactions) | ACID per transaction |
| History reads | Partition key scan, O(1) per page | B-tree index scan, O(log N) |
| Timer queue | Native clustering order | B-tree index, VACUUM pressure from deletes |

---

## 15. Sharding, Replication, and Cluster Topology

### Shard Assignment

Every workflow is assigned to a shard:

```
shard_id = hash(namespace_id + workflow_id) % num_shards

num_shards = 16,384 (configurable, chosen at cluster creation, hard to change)

Why 16,384?
  10M workflows / 16,384 shards = ~610 workflows per shard
  At 16 history hosts: ~1,024 shards per host
  Fine-grained enough for balanced distribution
  Coarse enough that shard metadata fits in memory
```

### Shard-to-Host Mapping

```
┌────────────────────────────────────────────────────────┐
│  Membership Ring (Ringpop / Consistent Hashing)         │
│                                                         │
│  Host A: shards [0, 1024)                               │
│  Host B: shards [1024, 2048)                            │
│  ...                                                    │
│  Host P: shards [15360, 16384)                          │
│                                                         │
│  When Host B crashes:                                   │
│    1. Membership protocol detects failure (~5-10s)       │
│    2. Remaining hosts rebalance:                         │
│       Host A: [0, 1024) + [1024, 1366)  ← acquires 1/3 │
│       Host C: [2048, 3072) + [1366, 1707) ← acquires 1/3│
│       Host D: [3072, 4096) + [1707, 2048) ← acquires 1/3│
│    3. New shard owners load mutable state from DB        │
│    4. Resume processing transfer queues and timer queues │
│    5. Total failover time: ~15-30 seconds                │
└────────────────────────────────────────────────────────┘
```

### Why Shard Ownership Matters

All operations on a workflow go through its shard owner. This ensures:

1. **Serialization**: concurrent signals, timer fires, and activity completions for the same workflow are serialized through the shard owner. No distributed locking needed.
2. **In-memory caching**: the shard owner caches mutable state. No DB read for hot workflows.
3. **Optimistic concurrency**: `db_record_version` is checked on every write. If another host tried to modify the same workflow (split-brain), the write fails and the operation is retried on the correct owner.

### Multi-Region Deployment

```
Region A (primary):
  History hosts: 16 (own all 16,384 shards)
  Matching hosts: 8
  Frontend hosts: 8
  DB: Cassandra ring (region A nodes)

Region B (standby):
  History hosts: 16 (standby, ready to acquire shards)
  Matching hosts: 8
  Frontend hosts: 8
  DB: Cassandra ring (cross-region replication, async)

Failover:
  1. Region A becomes unhealthy
  2. External orchestrator (or operator) triggers failover
  3. Region B history hosts acquire all shards
  4. Resume processing from last persisted state
  5. RPO: seconds (async replication lag)
  6. RTO: ~60 seconds (shard acquisition + cache warm-up)
```

---

## 16. Multi-Tenancy and Namespace Isolation

### Namespace as Isolation Boundary

```
┌─────────────────────────────────────────────────┐
│  Cluster                                         │
│                                                  │
│  Namespace: "orders-prod"                        │
│    ├─ Workflows: order-fulfillment, payment, ... │
│    ├─ Retention: 30 days                         │
│    ├─ Rate limit: 5,000 starts/sec               │
│    ├─ Max concurrent: 2M workflows               │
│    └─ Search attributes: order_id, customer_tier │
│                                                  │
│  Namespace: "data-pipelines-prod"                │
│    ├─ Workflows: etl-daily, sync-to-warehouse    │
│    ├─ Retention: 7 days                          │
│    ├─ Rate limit: 1,000 starts/sec               │
│    ├─ Max concurrent: 100K workflows             │
│    └─ Search attributes: pipeline_id, dataset    │
│                                                  │
│  Namespace: "ci-cd-prod"                         │
│    ├─ ...                                        │
└─────────────────────────────────────────────────┘
```

### Noisy Neighbor Prevention

| Resource | Isolation Mechanism |
|---|---|
| **API rate** | Per-namespace token bucket in frontend (rejects with RESOURCE_EXHAUSTED) |
| **Workflow starts** | Per-namespace counter; exceeding quota rejects new starts |
| **History size** | Per-namespace storage quota; monitored, alerts at 80% |
| **Shard CPU** | Workflows from different namespaces share shards; per-namespace priority weights for queue processing |
| **Matching service** | Per-namespace rate limiters on task dispatch |
| **Visibility** | Elasticsearch index per namespace (or shared index with namespace-prefixed doc IDs + filtered queries) |

### The Shared-Shard Problem

Workflows from different namespaces land on the same shard (because sharding is by `hash(namespace + workflow_id)`). A namespace with a runaway workflow (100,000 events, constant signals) can slow down the shard owner, affecting all namespaces on that shard.

Mitigations:
1. **Per-workflow rate limiting**: the history service limits how many workflow tasks it creates per workflow per second (default: 200/sec). A workflow generating events faster than this is throttled.
2. **Per-namespace shard quota**: if a namespace dominates a shard (> 50% of its load), the history service deprioritizes that namespace's tasks on that shard, giving other namespaces headroom.
3. **Dedicated shards** (enterprise feature): a namespace can be assigned dedicated shard ranges, ensuring physical isolation at the cost of less balanced distribution.

---

## 17. SDK Architecture and Worker Runtime

### SDK Internals

The SDK is the most complex client-side component. It is a **replay engine** that runs inside the workflow worker process.

```
┌────────────────────────────────────────────────────────────┐
│  Workflow Worker Process                                    │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  SDK Runtime                                          │   │
│  │                                                       │   │
│  │  ┌─────────────┐  ┌────────────────┐  ┌────────────┐ │   │
│  │  │ Task Poller  │  │ Replay Engine  │  │ State Cache│ │   │
│  │  │ (long-poll   │  │ (drives wf     │  │ (LRU,      │ │   │
│  │  │  matching    │  │  code through  │  │  10K wfs)  │ │   │
│  │  │  service)    │  │  history)      │  │            │ │   │
│  │  └──────┬───────┘  └───────┬────────┘  └────────────┘ │   │
│  │         │                  │                           │   │
│  │         │    ┌─────────────┘                           │   │
│  │         │    │                                         │   │
│  │         ▼    ▼                                         │   │
│  │  ┌─────────────────┐                                  │   │
│  │  │ Workflow Code    │  ← User's workflow function       │   │
│  │  │ (deterministic,  │     runs here, intercepted by     │   │
│  │  │  coroutine-based)│     SDK primitives                │   │
│  │  └─────────────────┘                                  │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Activity Executor                                    │   │
│  │  (runs activity functions — can do I/O, network, etc) │   │
│  └──────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────┘
```

### Coroutine-Based Execution (Python SDK Example)

The SDK uses coroutines (not OS threads) for workflow execution. This ensures deterministic scheduling:

```python
# SDK creates a single-threaded event loop for each workflow execution.
# All workflow code runs as coroutines on this loop.
# The SDK controls which coroutine runs next — not the OS.

class WorkflowRuntime:
    def __init__(self):
        self.coroutines = []  # scheduled coroutines
        self.current = None

    def schedule(self, coro):
        self.coroutines.append(coro)

    def run_until_blocked(self):
        """Run all coroutines until they are all blocked (waiting for
        an activity, timer, or signal)."""
        while self.coroutines:
            coro = self.coroutines.pop(0)
            try:
                result = coro.send(None)
                if result == BLOCKED:
                    pass  # coroutine is waiting, don't reschedule
                else:
                    self.coroutines.append(coro)
            except StopIteration:
                pass  # coroutine completed
```

### Worker Configuration

```python
# Python SDK — worker startup
worker = Worker(
    client=temporal_client,
    task_queue="order-workers",
    workflows=[OrderWorkflow, PaymentWorkflow],
    activities=[reserve_inventory, charge_card, send_email],
    max_concurrent_workflow_tasks=100,
    max_concurrent_activity_tasks=200,
    sticky_queue_schedule_to_start_timeout=timedelta(seconds=5),
    max_cached_workflows=10_000,
)
await worker.run()
```

### Deadlock Detection

Because workflow code runs on a single-threaded coroutine loop, a workflow function that blocks the thread (synchronous I/O, `time.sleep()`, infinite loop) deadlocks the worker for that workflow. The SDK detects this:

```
Workflow task has a deadline (workflow_task_timeout, default 10s).
If the SDK hasn't returned commands within 10 seconds:
  → The task times out
  → The engine creates a new workflow task (dispatched to any worker)
  → The new worker replays from history (will hit the same deadlock)
  → After N failures, the workflow is marked as stuck
  → Alert fires
```

---

## 18. API Design

### gRPC Service Definition

```protobuf
service WorkflowService {
    // Workflow lifecycle
    rpc StartWorkflowExecution(StartWorkflowExecutionRequest)
        returns (StartWorkflowExecutionResponse);

    rpc SignalWorkflowExecution(SignalWorkflowExecutionRequest)
        returns (SignalWorkflowExecutionResponse);

    rpc SignalWithStartWorkflowExecution(SignalWithStartWorkflowExecutionRequest)
        returns (SignalWithStartWorkflowExecutionResponse);

    rpc QueryWorkflow(QueryWorkflowRequest)
        returns (QueryWorkflowResponse);

    rpc UpdateWorkflowExecution(UpdateWorkflowExecutionRequest)
        returns (UpdateWorkflowExecutionResponse);

    rpc RequestCancelWorkflowExecution(RequestCancelWorkflowExecutionRequest)
        returns (RequestCancelWorkflowExecutionResponse);

    rpc TerminateWorkflowExecution(TerminateWorkflowExecutionRequest)
        returns (TerminateWorkflowExecutionResponse);

    rpc DescribeWorkflowExecution(DescribeWorkflowExecutionRequest)
        returns (DescribeWorkflowExecutionResponse);

    rpc GetWorkflowExecutionHistory(GetWorkflowExecutionHistoryRequest)
        returns (GetWorkflowExecutionHistoryResponse);

    // Visibility
    rpc ListWorkflowExecutions(ListWorkflowExecutionsRequest)
        returns (ListWorkflowExecutionsResponse);

    rpc CountWorkflowExecutions(CountWorkflowExecutionsRequest)
        returns (CountWorkflowExecutionsResponse);

    // Worker polling
    rpc PollWorkflowTaskQueue(PollWorkflowTaskQueueRequest)
        returns (PollWorkflowTaskQueueResponse);

    rpc RespondWorkflowTaskCompleted(RespondWorkflowTaskCompletedRequest)
        returns (RespondWorkflowTaskCompletedResponse);

    rpc PollActivityTaskQueue(PollActivityTaskQueueRequest)
        returns (PollActivityTaskQueueResponse);

    rpc RespondActivityTaskCompleted(RespondActivityTaskCompletedRequest)
        returns (RespondActivityTaskCompletedResponse);

    rpc RespondActivityTaskFailed(RespondActivityTaskFailedRequest)
        returns (RespondActivityTaskFailedResponse);

    rpc RecordActivityTaskHeartbeat(RecordActivityTaskHeartbeatRequest)
        returns (RecordActivityTaskHeartbeatResponse);

    // Namespace management
    rpc RegisterNamespace(RegisterNamespaceRequest)
        returns (RegisterNamespaceResponse);

    rpc DescribeNamespace(DescribeNamespaceRequest)
        returns (DescribeNamespaceResponse);
}
```

### Key Request/Response Examples

**Start Workflow:**

```json
// Request
{
    "namespace": "orders-prod",
    "workflow_id": "order-12345",
    "workflow_type": { "name": "order-fulfillment" },
    "task_queue": { "name": "order-workers" },
    "input": { "payloads": [<serialized order data>] },
    "workflow_execution_timeout": "86400s",
    "workflow_run_timeout": "3600s",
    "workflow_task_timeout": "10s",
    "request_id": "req-uuid-abc",
    "retry_policy": {
        "initial_interval": "1s",
        "backoff_coefficient": 2.0,
        "maximum_interval": "60s",
        "maximum_attempts": 3
    },
    "search_attributes": {
        "indexed_fields": {
            "order_id": { "metadata": {"type": "Keyword"}, "data": "order-12345" },
            "customer_tier": { "metadata": {"type": "Keyword"}, "data": "premium" }
        }
    },
    "workflow_id_reuse_policy": "REJECT_DUPLICATE"
}

// Response
{
    "run_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Signal Workflow:**

```json
// Request
{
    "namespace": "orders-prod",
    "workflow_execution": {
        "workflow_id": "order-12345",
        "run_id": ""  // empty = latest run
    },
    "signal_name": "approve",
    "input": { "payloads": [<serialized approval data>] },
    "request_id": "sig-uuid-def"
}
```

**List Workflows (Visibility):**

```json
// Request
{
    "namespace": "orders-prod",
    "query": "WorkflowType = 'order-fulfillment' AND ExecutionStatus = 'Running' AND customer_tier = 'premium'",
    "page_size": 50
}

// Response
{
    "executions": [
        {
            "execution": { "workflow_id": "order-12345", "run_id": "..." },
            "type": { "name": "order-fulfillment" },
            "start_time": "2024-03-15T10:00:00Z",
            "status": "RUNNING",
            "search_attributes": { ... }
        }
    ],
    "next_page_token": "..."
}
```

---

## 19. Observability and Debugging

### Execution Trace (History as Observability)

The event history is **inherently an execution trace**. Every activity call, timer, signal, and decision is recorded with timestamps, worker identity, and payloads.

```
Workflow: order-12345 (order-fulfillment)
Started: 2024-03-15T10:00:00Z by user:api-gateway

Timeline:
  10:00:00.000  WorkflowExecutionStarted
  10:00:00.005  WorkflowTaskScheduled → order-workers
  10:00:00.052  WorkflowTaskStarted   (worker: wf-worker-3)
  10:00:00.053  WorkflowTaskCompleted  → [ScheduleActivity: reserve-inventory]
  10:00:00.055  ActivityTaskScheduled  → order-workers (activity)
  10:00:00.110  ActivityTaskStarted    (worker: act-worker-7)
  10:00:00.340  ActivityTaskCompleted   result: {reserved: true}
  10:00:00.342  WorkflowTaskScheduled
  10:00:00.395  WorkflowTaskStarted    (worker: wf-worker-3, sticky)
  10:00:00.396  WorkflowTaskCompleted   → [ScheduleActivity: charge-card]
  10:00:00.398  ActivityTaskScheduled   → payment-workers
  10:00:00.450  ActivityTaskStarted    (worker: pay-worker-2)
  10:00:02.100  ActivityTaskFailed      error: "card declined"
  10:00:02.101  ActivityTaskScheduled   (retry attempt 2, after 2s backoff)
  10:00:04.200  ActivityTaskStarted    (worker: pay-worker-5)
  10:00:04.850  ActivityTaskCompleted   result: {charged: true, txn: "txn-abc"}
  ...
```

### Metrics

| Metric | Type | Labels |
|---|---|---|
| `workflow_started_total` | Counter | namespace, workflow_type |
| `workflow_completed_total` | Counter | namespace, workflow_type, status |
| `workflow_task_latency_seconds` | Histogram | namespace, task_queue |
| `activity_task_latency_seconds` | Histogram | namespace, activity_type |
| `activity_retry_total` | Counter | namespace, activity_type |
| `workflow_task_replay_latency` | Histogram | namespace (replay time, not activity time) |
| `sticky_cache_hit_ratio` | Gauge | worker_id |
| `history_size_events` | Histogram | namespace, workflow_type |
| `timer_fire_latency_seconds` | Histogram | namespace (actual - scheduled fire time) |
| `shard_lock_latency_seconds` | Histogram | shard_id |
| `transfer_queue_depth` | Gauge | shard_id |
| `matching_sync_match_ratio` | Gauge | namespace, task_queue |

### Key Alerts

| Alert | Condition | Severity |
|---|---|---|
| Workflow task timeout rate high | > 1% of workflow tasks timing out (10s deadline) | Critical — workflow code is blocking or too complex |
| Non-determinism errors | Any `NonDeterminismError` in any namespace | Critical — broken deployment, urgent rollback needed |
| Transfer queue backlog | Depth > 10,000 for > 5 minutes | Warning — history service not keeping up |
| Timer fire delay | P99 timer fire latency > 30 seconds | Warning — timer processor overloaded or shard failover |
| Activity retry rate | > 20% of activity attempts are retries | Warning — downstream service degradation |
| History size warning | Workflow exceeds 10,000 events | Info — developer should use continue-as-new |
| Shard ownership gap | Shard unowned for > 30 seconds | Critical — membership protocol failure |

### Debugging: Replay in Development

The SDK supports local replay for debugging:

```python
# Download a production workflow's history
history = client.get_workflow_history("order-12345")

# Replay locally with your code (with or without modifications)
replayer = WorkflowReplayer(workflows=[OrderWorkflow])
result = replayer.replay_workflow(history)

# If the code matches: result is the workflow's current state
# If the code diverges: NonDeterminismError shows exactly where
```

This is the workflow equivalent of reproducing a bug with a specific input — the history is the input.

---

## 20. Failure Walkthroughs

### Scenario 1: Workflow Worker Crashes Mid-Execution

```
Timeline:
  T=0:    Worker-3 picks up workflow task for order-12345
  T=1:    Worker-3 replays 500 events, reaches current point
  T=2:    Workflow code calls execute_activity("charge-card")
  T=3:    SDK emits ScheduleActivityTask command
  T=3:    Worker-3 crashes (OOM, segfault, pod evicted)

What happens:
  T=3:    Worker-3's gRPC connection to matching service drops
  T=3:    Matching service detects dropped connection
  T=13:   Workflow task timeout fires (10s default)
          History service sees: WorkflowTask started but not completed
  T=13:   History service creates a new WorkflowTask
  T=13:   Matching service dispatches to Worker-5 (normal queue, not sticky)
  T=14:   Worker-5 receives task with full history (500 events)
  T=14.5: Worker-5 replays all 500 events (~50ms)
  T=14.5: SDK reaches execute_activity("charge-card") again
          → No matching event in history (the command was never persisted
            because Worker-3 crashed before responding)
          → SDK emits ScheduleActivityTask command (again, fresh)
  T=14.5: Worker-5 responds with command
  T=14.6: History service persists ActivityTaskScheduled event

Result: No side-effect was re-executed. The charge-card activity was
never dispatched (Worker-3 crashed before reporting the command).
The workflow resumes exactly where it was.

Key insight: commands are not persisted until the worker responds
to the history service. A crash before the response means the
commands never happened. Replay produces them again.
```

### Scenario 2: Activity Worker Crashes After Partial Work

```
Timeline:
  T=0:    Activity worker starts executing "process-batch" activity
  T=5:    Activity has processed 400 of 1000 records
  T=5:    Activity heartbeats: { progress: 400 }
  T=6:    Activity worker crashes

What happens:
  T=6:    Heartbeat stops
  T=16:   Heartbeat timeout fires (10s after last heartbeat)
  T=16:   History service appends ActivityTaskTimedOut event
  T=16:   History service creates new ActivityTask (retry attempt 2)
  T=17:   New activity worker picks up the task
  T=17:   Activity code reads heartbeat_details from the task:
            last_heartbeat = { progress: 400 }
          Activity resumes from record 401, not record 1

Result: Partial work is not lost IF the activity uses heartbeats
to checkpoint progress. Without heartbeats, the activity retries
from scratch (which is correct but wasteful).
```

### Scenario 3: History Service Host Crashes (Shard Failover)

```
Timeline:
  T=0:    Host-A owns shards [0, 1024)
  T=0:    Workflow order-12345 is on shard 42 (owned by Host-A)
  T=5:    Host-A crashes (hardware failure)

What happens:
  T=5:    Membership protocol detects Host-A is unreachable
  T=10:   Membership protocol marks Host-A as dead
  T=10:   Remaining hosts rebalance shard ownership
          Host-B acquires shards [0, 512) (including shard 42)
          Host-C acquires shards [512, 1024)
  T=11:   Host-B loads mutable state for shard 42's workflows from DB
  T=12:   Host-B starts processing:
          - Transfer queue for shard 42 (may have tasks queued)
          - Timer queue for shard 42 (may have timers that fired during gap)
  T=15:   Shard 42 is fully operational on Host-B

Impact during gap (T=5 to T=15):
  - New API calls for shard 42 workflows: rejected with UNAVAILABLE
    (client retries with backoff, frontend detects shard has no owner)
  - In-flight workflow tasks: workers are processing; they respond to
    the history service, which is unavailable → worker retries
  - Activity completions: workers report to history service → retries
  - Timers: may fire late (up to gap duration)

NO DATA LOSS: all state is in the database. The gap is purely
an availability gap, not a durability gap.
```

### Scenario 4: Non-Determinism Error After Bad Deployment

```
Timeline:
  T=0:    Workflow order-12345 running on code v1
          History has 200 events, including:
            Event 50: ActivityTaskScheduled { type: "validate-address" }
            Event 51: ActivityTaskCompleted { result: {...} }
  T=1:    Developer deploys v2 which REMOVES the validate-address call
  T=2:    A signal arrives for order-12345 → new workflow task created
  T=3:    Worker (running v2) receives task, replays from event 1
  T=3:    At event 50: v2 code does NOT call execute_activity("validate-address")
          Instead, v2 code calls execute_activity("check-inventory")
          SDK expected: ActivityTaskScheduled { type: "check-inventory" }
          Found in history: ActivityTaskScheduled { type: "validate-address" }
          → NON-DETERMINISM ERROR

What happens:
  T=3:    SDK returns WorkflowTaskFailed with NonDeterminismError
  T=3:    History service appends WorkflowTaskFailed event
  T=4:    History service retries: creates another WorkflowTask
  T=4.5:  Another worker picks it up → same error
  T=5:    After N failures, backoff kicks in (exponential, capped at 1 min)

Resolution:
  Option A: Roll back to v1 → workflow resumes normally
  Option B: Deploy v2 with proper patching:
            if workflow.patched("remove-validate-address"):
                result = await execute_activity("check-inventory", ...)
            else:
                result = await execute_activity("validate-address", ...)
  Option C: Reset the workflow to a known good event (admin operation)
  Option D: Terminate and restart with a new workflow ID
```

### Scenario 5: Saga Compensation Chain Failure

```
Travel booking workflow:
  Step 1: Reserve hotel    → SUCCESS (reservation: H-123)
  Step 2: Reserve flight   → SUCCESS (reservation: F-456)
  Step 3: Reserve car      → SUCCESS (reservation: C-789)
  Step 4: Charge payment   → FAILED (card declined)

Saga compensation begins (reverse order):
  Compensate step 3: Cancel car (C-789)
    → Attempt 1: Car service returns 500
    → Attempt 2: (after 2s backoff) Car service returns 200 → SUCCESS

  Compensate step 2: Cancel flight (F-456)
    → Attempt 1: Flight service timeout
    → Attempt 2: Flight service timeout
    → Attempt 3: Flight service returns 200 → SUCCESS

  Compensate step 1: Cancel hotel (H-123)
    → Attempt 1: Hotel service returns 500
    → Attempt 2: Hotel service returns 500
    → Attempt 3: Hotel service returns 500
    → Max retries exhausted → COMPENSATION FAILED

Workflow completes with:
  status: FAILED
  compensation_result: {
    succeeded: ["cancel-car", "cancel-flight"],
    failed: ["cancel-hotel"],
    failed_details: {
      "cancel-hotel": {
        reservation_id: "H-123",
        attempts: 3,
        last_error: "Internal Server Error",
        requires_manual_intervention: true
      }
    }
  }

Visibility store indexes: compensation_failed = true
Alert fires → operator manually cancels H-123 via hotel admin portal
```

---

## 21. Trade-offs

### Code-Based Workflows vs. DAG/YAML DSL

| Dimension | Code-Based (Temporal) | DAG/YAML (Step Functions, Airflow) |
|---|---|---|
| Expressiveness | Full programming language: if/else, loops, error handling, composition | Limited: predefined states, transitions, limited branching |
| Testability | Unit-testable with mocks | Requires deploying + running the engine |
| Debugging | Replay locally, step through code | Inspect state machine transitions in UI |
| Versioning | Complex (patching API needed) | Simple (deploy new DAG, old runs finish with old version) |
| Learning curve | Write normal code + understand replay constraints | Learn a DSL + understand its limitations |
| Determinism risk | Developer can break replay | DSL prevents non-determinism by construction |

**Our choice**: code-based. The expressiveness wins outweigh the determinism risk, which is mitigated by SDK enforcement + linting + non-determinism detection.

### Cassandra vs. PostgreSQL for Persistence

| Dimension | Cassandra | PostgreSQL |
|---|---|---|
| Horizontal scale | Yes (linear) | No (vertical + read replicas) |
| Consistency model | Per-partition linearizable | Full ACID |
| Operational cost | High (compaction, repair, anti-entropy) | Lower (managed services available) |
| Timer queue | Clustering order, efficient range scans | B-tree index, VACUUM pressure from deletes |
| Batch writes | Logged batch within partition = atomic | Transaction = atomic |
| Scale ceiling | Proven at 10M+ concurrent workflows | ~1M comfortable, ~5M with aggressive partitioning |

**Recommendation**: start with PostgreSQL for deployments < 1M concurrent workflows. Migrate to Cassandra when scale demands it. The pluggable persistence interface makes this a configuration change, not a rewrite.

### Sticky Execution vs. Stateless Workers

| Dimension | Sticky (preferred) | Stateless |
|---|---|---|
| Replay cost | Low (only new events) | High (full history every time) |
| Worker failure cost | Cache lost, next task does full replay | No additional cost |
| Memory usage | Higher (cache of workflow states) | Lower |
| Load balancing | Less flexible (affinity) | Perfectly balanced |
| Deployment | Rolling deployments drain sticky cache | No cache to drain |

**Our choice**: sticky execution with fallback to stateless. The replay cost savings (70%+ reduction) justify the complexity. The sticky timeout (5s default) ensures fast failover.

### Event Limit: Strict vs. Soft

| Approach | Behavior |
|---|---|
| Strict limit (50K events) | Workflow task fails with error; must continue-as-new | Forces developers to handle long-running patterns correctly |
| Soft limit (warn at 10K, degrade at 50K) | Performance degrades but workflow continues | Friendlier but hides problems until they're severe |

**Our choice**: strict limit at 50K with warnings at 10K and 30K. Forcing `continue_as_new` prevents a single workflow from degrading the cluster.

### Signal Delivery: Buffered vs. Immediate

| Approach | Behavior | Trade-off |
|---|---|---|
| **Buffered** (our choice) | Signals received while a workflow task is in-flight are buffered until the task completes | Deterministic: the workflow sees a consistent view of events |
| Immediate | Signals are appended to history immediately, potentially while a worker is mid-replay | Non-deterministic: worker may have already passed the point where the signal would be handled |

**Our choice**: buffered. Determinism is non-negotiable. A signal arriving 100ms later (next workflow task) is an acceptable trade-off for correctness.

---

## 22. Evolution Path

### Phase 1: MVP (Months 1-3)

- [ ] Core engine: history service, matching service, frontend
- [ ] Event sourcing with deterministic replay
- [ ] Activity execution with retry policies
- [ ] Durable timers
- [ ] Signals and queries
- [ ] Single-namespace PostgreSQL persistence
- [ ] Go SDK (primary) + Python SDK
- [ ] Basic Web UI: workflow list, history viewer
- [ ] Prometheus metrics export

### Phase 2: Production Hardening (Months 4-6)

- [ ] Multi-namespace support with isolation
- [ ] Workflow versioning / patching API
- [ ] Continue-as-new
- [ ] Child workflows
- [ ] Saga compensation framework in SDK
- [ ] Sticky execution + workflow cache
- [ ] Elasticsearch visibility store
- [ ] Cron workflows
- [ ] Rate limiting (per-namespace, per-task-queue)
- [ ] History archival to object storage

### Phase 3: Scale (Months 7-12)

- [ ] Cassandra persistence backend
- [ ] Multi-region deployment with async replication
- [ ] Shard rebalancing and host-level autoscaling
- [ ] Advanced search: custom search attributes, SQL-like query language
- [ ] Workflow updates (validated mutations)
- [ ] TypeScript + Java SDKs
- [ ] Batch operations (terminate/cancel/signal in bulk)
- [ ] Schedule support (cron-like, managed by the engine)
- [ ] Nexus: cross-namespace and cross-cluster workflow calls
- [ ] Performance: sub-100ms P99 task dispatch latency at 50K tasks/sec

### Phase 4: Enterprise (Months 12+)

- [ ] Dedicated shard ranges for namespace isolation
- [ ] Encryption at rest with customer-managed keys
- [ ] Audit logging for all API operations
- [ ] RBAC for namespace and workflow access
- [ ] Cloud-native managed offering (multi-tenant control plane)
- [ ] Workflow replay debugger in Web UI (step-through execution)
- [ ] Cost attribution per namespace/team

---

## Security, Privacy, and Abuse Prevention

Event-sourced history (§4) is **immutable and replayed**, which creates the design's hardest
privacy problem: workflow inputs, activity results and signals are stored for as long as the
history exists, and you can't edit them without breaking deterministic replay.

| Threat | Control |
|---|---|
| **Personal data in history** | Encrypt payloads **in the SDK** before they leave the worker (the pluggable data converter from §1, running in the SDK, §17), so the server stores ciphertext. The server never needs plaintext: it routes by IDs and timers. The UI and CLI decrypt through a codec service the operator authenticates to |
| **Right to erasure vs. immutable history** | **Crypto-shredding**: encrypt each payload with a per-subject key (per customer), and erase the key to make every payload for that subject unreadable at once, in history, archives and backups (below) |
| **Cross-namespace access** | Namespaces are the tenancy boundary (§16). The frontend authorizes every call by namespace from the caller's token or mTLS identity. Workers get credentials scoped to their namespace and task queues |
| **Rogue workers** | Any process that polls a task queue receives tasks and their inputs. Workers authenticate with mTLS, and task-queue polling is authorized per identity |
| **Signals and updates as an input channel** | Signals (§11) come from other services and possibly users. Validate them in the workflow like any external input, and authorize who can signal which workflow types |
| **Visibility store leaks** | Search attributes are indexed in plain text in the Elasticsearch visibility store (§3), so they must never contain personal data. Use IDs, and store the rest in the encrypted payload |

```python
class SubjectKeys:
    """One data key per data subject (user/customer). Production: keys live in a KMS or
    vault, wrapped by a master key; this dict stands in for that store."""
    def __init__(self):
        self._keys: dict[str, bytes] = {}

    def key_for(self, subject_id: str) -> bytes:
        return self._keys.setdefault(subject_id, AESGCM.generate_key(bit_length=256))

    def get(self, subject_id: str) -> bytes:
        if subject_id not in self._keys:
            raise KeyErased(subject_id)
        return self._keys[subject_id]

    def erase(self, subject_id: str) -> None:        # the GDPR erasure: delete one key
        self._keys.pop(subject_id, None)


def encrypt_payload(keys: SubjectKeys, subject_id: str, plaintext: bytes) -> dict:
    nonce = os.urandom(12)
    ct = AESGCM(keys.key_for(subject_id)).encrypt(nonce, plaintext, subject_id.encode())
    return {"subject": subject_id, "nonce": nonce, "ct": ct}      # this goes into history


def decrypt_payload(keys: SubjectKeys, blob: dict) -> bytes:
    return AESGCM(keys.get(blob["subject"])).decrypt(blob["nonce"], blob["ct"], blob["subject"].encode())
```

The subject ID is bound as associated data, so a ciphertext can't be moved to another subject.
After `erase("cust-42")`, replaying a workflow that touched cust-42 fails to decode its
payloads. That is intended: close or terminate those workflows first, then erase. Decide
up front which workflows may hold personal data (payment, onboarding) and require the codec for
their task queues, so a new workflow can't store plaintext by accident.

---

## Appendix: Why This Is Not a Job Scheduler

The most important architectural distinction bears repeating:

**A job scheduler** asks: "What tasks need to run, and when?"
- It fires tasks. It retries failed tasks. It schedules future tasks.
- Each task is independent. There is no memory of previous tasks' results.
- State is a simple enum: `pending → running → completed/failed`.

**A workflow orchestration engine** asks: "What is the current state of this business process, and what should happen next?"
- It models a stateful, multi-step process as code.
- Each step can depend on previous steps' results, external signals, timers, and child processes.
- State is a full event history — hundreds or thousands of events.
- Recovery is replay, not retry. Side-effects are never re-executed.
- A single workflow can run for months, surviving deployments, crashes, and infrastructure changes.

A job scheduler is a **task dispatcher**. A workflow orchestration engine is a **durable execution runtime**. You use the job scheduler to fire individual tasks. You use the workflow engine to orchestrate the business process that coordinates those tasks.

They complement each other: a workflow engine often dispatches activities through task queues (which behave like a job scheduler). But the engine adds the layer above: process memory, deterministic replay, compensation, versioning, and signals.
