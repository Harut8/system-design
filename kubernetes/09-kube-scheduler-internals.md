# Kube-Scheduler Internals: A Deep Dive

How kube-scheduler turns a stream of unbound Pods into a stream of `spec.nodeName` writes. This chapter unpacks the scheduling framework, every extension point, every built-in plugin, the preemption algorithm, topology-spread math, scheduling profiles, scheduling gates, and the operational shape of scheduling at 5,000 nodes. The audience is staff engineers who already know what a Pod is and want to reason about scheduling behavior the way the scheduler itself does.

The scheduler is, at its core, a planner + pipeline very similar to a database query engine: it ingests a logical request (a Pod spec), runs it through a sequence of stages (filter then score, parallel to predicate-then-cost), produces a physical decision (a node name), and commits via a single side-effect (a Bind write). The analogies are not cosmetic — both systems are stuck with the same engineering tensions: correctness under concurrency, fairness under load, fast paths for the common case, escape valves for the unusual one, and a stable observability surface so humans can debug what the machine decided.

---

## Table of Contents

1. [The Scheduler's Job (and What It Is Not)](#1-the-schedulers-job-and-what-it-is-not)
2. [Architecture and the Two-Cycle Model](#2-architecture-and-the-two-cycle-model)
3. [The Scheduling Framework: Extension Points in Order](#3-the-scheduling-framework-extension-points-in-order)
4. [The Scheduling Queue: activeQ, podBackoffQ, unschedulableQ](#4-the-scheduling-queue-activeq-podbackoffq-unschedulableq)
5. [The Node Info Cache (Snapshot Semantics)](#5-the-node-info-cache-snapshot-semantics)
6. [NodeResourcesFit: Filter and Score in Detail](#6-noderesourcesfit-filter-and-score-in-detail)
7. [Affinity, Anti-Affinity, and Their Costs](#7-affinity-anti-affinity-and-their-costs)
8. [Pod Topology Spread Constraints](#8-pod-topology-spread-constraints)
9. [Taints and Tolerations](#9-taints-and-tolerations)
10. [PriorityClass and Preemption](#10-priorityclass-and-preemption)
11. [Scheduling Gates (KEP-3521)](#11-scheduling-gates-kep-3521)
12. [Profiles and Multiple Schedulers](#12-profiles-and-multiple-schedulers)
13. [Scoring Strategies (Bin-Pack vs Spread)](#13-scoring-strategies-bin-pack-vs-spread)
14. [Performance at Scale](#14-performance-at-scale)
15. [Scheduler ↔ Autoscaler / Karpenter](#15-scheduler--autoscaler--karpenter)
16. [Scheduler ↔ Descheduler](#16-scheduler--descheduler)
17. [Gang / Batch Scheduling](#17-gang--batch-scheduling)
18. [KubeSchedulerConfiguration in Practice](#18-kubeschedulerconfiguration-in-practice)
19. [Building a Custom Plugin (Out-of-Tree)](#19-building-a-custom-plugin-out-of-tree)
20. [Observability and Alerts](#20-observability-and-alerts)
21. [The Bind Operation (the Only Write)](#21-the-bind-operation-the-only-write)
22. [Pitfalls and Anti-Patterns](#22-pitfalls-and-anti-patterns)
23. [TL;DR](#23-tldr)

---

## 1. The Scheduler's Job (and What It Is Not)

### 1.1 One Sentence

**kube-scheduler watches Pods whose `spec.nodeName` is the empty string, picks a node, and writes that node's name into the Pod via the `/binding` subresource.**

That is all. Everything else — pulling the image, creating cgroups, attaching the volume, starting the container, watching it run, restarting it on failure — belongs to the kubelet (chapter 10). The scheduler does not confirm that the Pod ran. It does not confirm that the kubelet accepted the assignment. It does not retry if the kubelet rejects the Pod with a node-side admission failure. It writes a name and walks away.

This separation is the most important fact about kube-scheduler. Many real-world bugs and outages come from engineers expecting the scheduler to do more (validate runtime state, react to crashed Pods, fix node fragmentation, rebalance load). It does none of those things. It is a one-shot decision engine: input is a Pod and a snapshot of cluster state, output is a node name, and the function is essentially stateless across decisions.

### 1.2 Why the Split Exists

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                  RESPONSIBILITY BOUNDARY                                     │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Pod created (spec.nodeName == "")                                           │
│        │                                                                     │
│        ▼                                                                     │
│  ┌──────────────────────────────┐                                            │
│  │  kube-scheduler              │   "Where should this run?"                 │
│  │  - read snapshot             │                                            │
│  │  - filter feasible nodes     │                                            │
│  │  - score them                │                                            │
│  │  - write Bind                │                                            │
│  └────────────┬─────────────────┘                                            │
│               │ spec.nodeName = "node-7"                                     │
│               ▼                                                              │
│  ┌──────────────────────────────┐                                            │
│  │  kubelet on node-7           │   "Make this Pod real."                    │
│  │  - mount volumes (CSI)       │                                            │
│  │  - set up network (CNI)      │                                            │
│  │  - pull image                │                                            │
│  │  - create containers (CRI)   │                                            │
│  │  - run probes                │                                            │
│  │  - report status             │                                            │
│  └──────────────────────────────┘                                            │
│                                                                              │
│  If kubelet fails to admit the pod (e.g., out of disk, no GPU after all),    │
│  it sets a status condition. It does NOT call the scheduler back.            │
│  A controller (or human) deletes the pod; a new pod is created; the          │
│  scheduler sees a fresh pod with empty spec.nodeName and tries again.        │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

The split is brutal but deliberate:

- **Latency separation**: scheduling is a fast in-memory decision (≪ 10ms per pod). Image pulls take seconds to minutes. If scheduling waited for image pull, throughput would collapse.
- **Blast radius separation**: a buggy scheduler corrupts placement decisions but cannot wedge a node. A buggy kubelet wedges its own node but cannot wedge scheduling.
- **Independent scaling**: there is one (HA) scheduler. There are N kubelets. The scheduler is sized by Pod-creation rate; kubelets are sized per-node.
- **Concurrency model separation**: the scheduler is essentially single-threaded for the *decision* (one Pod's filter+score runs to completion before the next Pod's begins, see §2). The kubelet runs N pod workers in parallel.

### 1.3 What the Scheduler Reads and Writes

| Object | Read | Write |
| --- | --- | --- |
| Pod | yes (watch) | only `spec.nodeName` via `/binding` and `status.conditions[PodScheduled]` |
| Node | yes (watch) | no |
| PersistentVolume / PVC | yes (watch) | no (PreBind may *trigger* dynamic provisioning, but the volume controller does the write) |
| StorageClass / CSIDriver / CSINode | yes (watch) | no |
| Service | yes (watch) | no |
| ReplicaSet / Deployment / StatefulSet | no | no |
| Event | no | yes (publishes scheduling decisions, FailedScheduling) |
| PodDisruptionBudget | yes (watch) | no (consulted by preemption) |

The scheduler's only Pod writes are the bind and a status condition. It does not touch `metadata`, `spec.containers`, or any other field. This is why mutating webhooks (admission, chapter 05) are the right place to inject scheduling-relevant labels — by the time the scheduler sees the Pod, no further mutation will happen from the scheduler's side.

### 1.4 What Happens If You Set `spec.nodeName` Manually

If you create a Pod with `spec.nodeName` already set, **the scheduler never sees it**. The scheduler watches only for Pods where that field is empty. The kubelet on the named node sees the Pod via its own watch and tries to run it.

This is how static Pods, mirror Pods, and some DaemonSet-style patterns work. It is also a footgun:

- Taints are not consulted. A pod with `nodeName=...` lands on the node even if the node has `NoSchedule` taints.
- Resource fit is not pre-checked. The kubelet will admit-or-reject after the fact (and you'll see `OutOfCPU`, `OutOfMemory`, or `NodeAffinity` admission failures).
- The scheduling profile is bypassed entirely. None of your custom plugins run.
- Topology spread is not enforced. Anti-affinity is not enforced.

Rule of thumb: never hand-set `nodeName` in user workloads. It is reserved for `kubectl debug node/...`-style escape hatches and DaemonSet controllers (which compute the assignment in the controller itself).

---

## 2. Architecture and the Two-Cycle Model

### 2.1 The Big Picture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                       KUBE-SCHEDULER PROCESS                                 │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  Informers / Reflectors (watching the apiserver)                       │  │
│  │  ┌─────────┐ ┌────────┐ ┌─────┐ ┌──────┐ ┌──────────┐ ┌────────────┐   │  │
│  │  │  Pods   │ │ Nodes  │ │ PVs │ │ PVCs │ │   PDB    │ │ CSINode... │   │  │
│  │  └────┬────┘ └───┬────┘ └──┬──┘ └──┬───┘ └────┬─────┘ └─────┬──────┘   │  │
│  └───────┼──────────┼─────────┼───────┼──────────┼─────────────┼──────────┘  │
│          │          │         │       │          │             │             │
│          ▼          ▼         ▼       ▼          ▼             ▼             │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │                      SCHEDULER CACHE (snapshot source)                 │  │
│  │   - NodeInfo per node (allocatable, requested, pods, ports, images)    │  │
│  │   - Assumed pods (bind in flight)                                      │  │
│  │   - Generation counter (snapshot revs)                                 │  │
│  └─────────────────────────────┬──────────────────────────────────────────┘  │
│                                │  snapshot()                                  │
│                                ▼                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │                      SCHEDULING QUEUE                                  │  │
│  │  ┌──────────┐   ┌──────────────┐   ┌────────────────┐                  │  │
│  │  │ activeQ  │   │ podBackoffQ  │   │ unschedulableQ │                  │  │
│  │  │  (heap)  │   │  (heap by    │   │   (set)         │                 │  │
│  │  │priority  │   │   backoff)   │   │                 │                 │  │
│  │  └─────┬────┘   └──────┬───────┘   └────────┬────────┘                 │  │
│  │        ▲ pop           │ when due           │ on cluster event         │  │
│  │        └───────────────┴────────────────────┘                          │  │
│  └─────────────────────────────┬──────────────────────────────────────────┘  │
│                                │ pop(pod)                                     │
│                                ▼                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  SCHEDULING CYCLE  (serial: one pod at a time)                         │  │
│  │  PreFilter → Filter → PostFilter? → PreScore → Score → Normalize →     │  │
│  │   Reserve → Permit                                                     │  │
│  │  (decides node n; calls cache.AssumePod(pod, n))                       │  │
│  └─────────────────────────────┬──────────────────────────────────────────┘  │
│                                │ go schedulingCtx.bind(pod, n)                │
│                                ▼                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  BINDING CYCLE   (one goroutine per pod, runs in parallel)             │  │
│  │  PreBind → Bind → PostBind                                              │  │
│  │  (writes spec.nodeName via /binding; on failure, ForgetPod from cache) │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

The shape is: one queue feeds one scheduling cycle (serial), which feeds N parallel binding cycles. The serial scheduling cycle is what allows the scheduler to reason about resource budgets and assumed pods without locking; the parallel binding cycle is what allows slow side-effects (volume provisioning, CSI attach) to overlap.

### 2.2 Why Scheduling Is Serial and Binding Is Parallel

The scheduling cycle reads from a *snapshot* of the cache and may pretend a candidate pod is already on a candidate node (assumed-pod). If two scheduling cycles ran in parallel, they could both decide to place a pod on the same node and double-count its resources.

The binding cycle, in contrast, only writes the pod's `spec.nodeName` and runs side effects (PreBind, e.g. volume provisioning). The resource accounting was already committed during Reserve in the scheduling cycle — the binding cycle does not need the snapshot to be quiescent.

Concretely:

- **Scheduling cycle duration**: target p99 < 100ms for a 5k-node cluster; healthy clusters run p50 in single-digit milliseconds.
- **Binding cycle duration**: can be seconds (dynamic PV provisioning, CSI attach negotiation). Acceptable, because it doesn't block the next pod's scheduling decision.

If the binding cycle were also serial, a single slow volume bind would freeze scheduling for the whole cluster. The scheduler would become a bottleneck on storage latency. The two-cycle split was added precisely to remove that coupling.

### 2.3 Code Map

The relevant Kubernetes source code lives roughly at:

```
kubernetes/
├── pkg/scheduler/
│   ├── scheduler.go                  scheduleOne(), the main loop driver
│   ├── schedule_one.go               scheduling + binding entry points
│   ├── framework/
│   │   ├── interface.go              Plugin, Framework, Status interfaces
│   │   ├── runtime/                  framework runtime: parallelizer, plugin chain
│   │   ├── plugins/
│   │   │   ├── noderesources/        NodeResourcesFit + scoring
│   │   │   ├── nodeaffinity/
│   │   │   ├── interpodaffinity/
│   │   │   ├── podtopologyspread/
│   │   │   ├── tainttoleration/
│   │   │   ├── volumebinding/
│   │   │   ├── defaultpreemption/
│   │   │   └── ... one directory per built-in plugin
│   │   └── parallelize/              parallelize.Until for per-node fan-out
│   ├── internal/
│   │   ├── queue/                    SchedulingQueue, PriorityQueue
│   │   └── cache/                    Cache, NodeInfo, snapshot()
│   ├── apis/config/                  KubeSchedulerConfiguration types
│   └── profile/                      profile registry, scheduler-name routing
```

When you read the rest of this chapter, mentally pin each concept to a directory above. Plugin authors mostly touch `framework/plugins/<x>` and the configuration types under `apis/config`.

---

## 3. The Scheduling Framework: Extension Points in Order

The scheduling framework is the heart of kube-scheduler since v1.19. It defines a fixed sequence of extension points; every built-in behavior is a plugin registered at one or more of those points; custom plugins plug into the same points; profiles select which plugins are enabled.

### 3.1 The Pipeline (Big Picture)

```
┌───────────────────────────────────────────────────────────────────────────────┐
│                    SCHEDULING FRAMEWORK EXTENSION POINTS                      │
├───────────────────────────────────────────────────────────────────────────────┤
│                                                                               │
│   queue → ┌──────────────────────┐                                            │
│           │     QueueSort        │   (1 plugin only — orders activeQ)         │
│           └──────────┬───────────┘                                            │
│                      │                                                        │
│                      ▼                                                        │
│           ┌──────────────────────┐                                            │
│           │    PreEnqueue        │   gating: schedulingGates                  │
│           └──────────┬───────────┘                                            │
│                      │   pod enters activeQ                                   │
│                      ▼                                                        │
│  ┌────────────────────────────────────────────────────────────────────────┐   │
│  │                       SCHEDULING CYCLE (serial)                        │   │
│  │                                                                        │   │
│  │   ┌──────────────────────┐                                             │   │
│  │   │     PreFilter        │   compute state, may early-reject           │   │
│  │   └──────────┬───────────┘                                             │   │
│  │              ▼                                                         │   │
│  │   ┌──────────────────────┐                                             │   │
│  │   │       Filter         │   per-node feasibility, parallel fan-out    │   │
│  │   │  (over all nodes)    │                                             │   │
│  │   └──────────┬───────────┘                                             │   │
│  │              │ feasibleNodes                                           │   │
│  │              │                                                         │   │
│  │              ├── if len == 0 ──┐                                       │   │
│  │              │                  ▼                                      │   │
│  │              │       ┌──────────────────────┐                          │   │
│  │              │       │     PostFilter       │  e.g., DefaultPreemption │   │
│  │              │       └──────────┬───────────┘                          │   │
│  │              │                  │ nominated node or unschedulable      │   │
│  │              │ <────────────────┘                                      │   │
│  │              ▼                                                         │   │
│  │   ┌──────────────────────┐                                             │   │
│  │   │      PreScore        │   compute state for Score                   │   │
│  │   └──────────┬───────────┘                                             │   │
│  │              ▼                                                         │   │
│  │   ┌──────────────────────┐                                             │   │
│  │   │       Score          │   per-node 0..100, parallel fan-out         │   │
│  │   └──────────┬───────────┘                                             │   │
│  │              ▼                                                         │   │
│  │   ┌──────────────────────┐                                             │   │
│  │   │   NormalizeScore     │   per-plugin rerange                        │   │
│  │   └──────────┬───────────┘                                             │   │
│  │              ▼                                                         │   │
│  │   ┌──────────────────────┐                                             │   │
│  │   │ pick winner (argmax) │   weighted sum across plugins               │   │
│  │   └──────────┬───────────┘                                             │   │
│  │              ▼                                                         │   │
│  │   ┌──────────────────────┐                                             │   │
│  │   │      Reserve         │   commit resources optimistically           │   │
│  │   └──────────┬───────────┘                                             │   │
│  │              ▼                                                         │   │
│  │   ┌──────────────────────┐                                             │   │
│  │   │       Permit         │   approve / wait / deny                     │   │
│  │   └──────────┬───────────┘                                             │   │
│  │              │     on deny: Unreserve, requeue                         │   │
│  └──────────────┼─────────────────────────────────────────────────────────┘   │
│                 │ go func() {                                                  │
│                 ▼                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐     │
│  │                      BINDING CYCLE (per pod, parallel)                │    │
│  │                                                                      │     │
│  │   ┌──────────────────────┐                                           │     │
│  │   │      PreBind         │   e.g., bind dynamic PV                   │     │
│  │   └──────────┬───────────┘                                           │     │
│  │              ▼                                                       │     │
│  │   ┌──────────────────────┐                                           │     │
│  │   │        Bind          │   write spec.nodeName via /binding        │     │
│  │   └──────────┬───────────┘                                           │     │
│  │              ▼                                                       │     │
│  │   ┌──────────────────────┐                                           │     │
│  │   │      PostBind        │   notify, metrics                         │     │
│  │   └──────────────────────┘                                           │     │
│  │                                                                      │     │
│  └──────────────────────────────────────────────────────────────────────┘     │
│                                                                               │
└───────────────────────────────────────────────────────────────────────────────┘
```

The extension points are points in time. A plugin can subscribe to one or many (e.g., NodeResourcesFit is both a Filter and a Score plugin). The framework guarantees ordering: PreFilter before Filter, Filter before PostFilter, and so on. Within a single extension point, plugins run in registration order — but you should write plugins to be order-independent at the same point.

### 3.2 QueueSort (Exactly One)

QueueSort orders the activeQ. **Exactly one QueueSort plugin can be active per profile.** The default is `PrioritySort`, which orders by Pod priority (descending) then by creation timestamp (ascending — older first as a tiebreak).

The interface is essentially a comparator:

```go
type QueueSortPlugin interface {
    Plugin
    Less(*QueuedPodInfo, *QueuedPodInfo) bool
}
```

`Less(a, b) == true` means a sorts before b. The default implementation:

```go
func (pl *PrioritySort) Less(a, b *framework.QueuedPodInfo) bool {
    p1 := podutil.GetPodPriority(a.Pod)
    p2 := podutil.GetPodPriority(b.Pod)
    return (p1 > p2) || (p1 == p2 && a.Timestamp.Before(b.Timestamp))
}
```

If you want strict FIFO across all priorities (rare; some batch users want it), you write a custom QueueSort that ignores priority. If you want fair-share across namespaces, you write one that interleaves namespace queues. This is the simplest hook in the framework and one of the most powerful: it changes the order of every subsequent decision.

### 3.3 PreEnqueue (Gate-Keeping)

PreEnqueue runs *before* a Pod enters the activeQ. If any PreEnqueue plugin returns non-Success, the Pod is held in the unschedulableQ (more precisely, the gated set, see §11). PreEnqueue is what implements `spec.schedulingGates` — a Pod with one or more gates set fails the built-in `SchedulingGates` PreEnqueue plugin until a controller removes them.

PreEnqueue is intentionally cheap: it runs every time the scheduler considers re-enqueueing a pod, which can be on every cluster event. Do not do expensive work here. The canonical use is "look at a few labels/fields on the pod; decide yes/no".

### 3.4 PreFilter

PreFilter runs once per scheduling cycle, before any per-node Filter. It serves two purposes:

1. **Compute state** to share with the Filter pass (so you don't recompute it N times).
2. **Early reject** if you can determine the Pod is unschedulable on *any* node without looking at individual nodes (e.g., the Pod requests `nvidia.com/gpu=8` and your cluster only has 4-GPU nodes — that's not a PreFilter check, but the pattern is similar).

The classic PreFilter output is a `CycleState` entry — an in-memory struct keyed by plugin name, scoped to one scheduling cycle, that subsequent extension points (Filter, Score, Reserve) can read without re-querying caches.

```go
type PreFilterPlugin interface {
    Plugin
    PreFilter(ctx context.Context, state *CycleState, p *v1.Pod) (*PreFilterResult, *Status)
    PreFilterExtensions() PreFilterExtensions  // for AddPod/RemovePod when other pods change
}
```

`PreFilterResult` may include a node-name allow-list (e.g., `NodeAffinity` may compute "only these 80 nodes can possibly match" and pass that hint down, so Filter doesn't run over the other 4,920).

### 3.5 Filter (Per-Node Feasibility)

Filter is the central feasibility check. For each candidate node, every Filter plugin runs in sequence; the first non-Success status disqualifies the node.

Built-in Filter plugins (one paragraph each):

- **NodeName** — trivial: if `pod.spec.nodeName` is set (somehow — usually the scheduler doesn't see such pods), require the candidate node to match. In practice this exists for completeness; the queue filter (Pods with empty nodeName) means it rarely fires.
- **NodeUnschedulable** — if the node has `spec.unschedulable=true` (set by `kubectl cordon`), reject. Tolerated only if the pod has the matching `node.kubernetes.io/unschedulable` toleration (which is automatically added to DaemonSet pods).
- **TaintToleration** — for every `NoSchedule` taint on the node, the pod must have a matching toleration. `PreferNoSchedule` is *not* a hard reject; it's a soft score penalty in the Score phase. `NoExecute` is a hard reject at admission AND triggers eviction of existing non-tolerating pods (handled by the node-lifecycle controller, not the scheduler).
- **NodeAffinity** — evaluates `spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution` (the hard rule) and pre-stores the soft rule for Score. Also evaluates the legacy `spec.nodeSelector`.
- **NodePorts** — if the pod requests host ports (`hostPort: 8080`), no other pod on the node may already bind that (host IP, protocol, port) tuple. The per-node NodeInfo maintains a set of used host-port tuples.
- **NodeResourcesFit** — the big one. For every requestable resource (cpu, memory, ephemeral-storage, extended resources, hugepages-*, scalar/named resources), checks `allocatable - sumOfRequests - pod.requests >= 0`. See §6.
- **VolumeBinding** — if the pod has PVCs, decides whether the PVCs can be bound to PVs that exist on (or are zone-compatible with) this node. Triggers delayed binding for `WaitForFirstConsumer` storage classes. This is also a PreBind plugin (it actually executes the bind in the binding cycle).
- **VolumeRestrictions** — enforces that certain volume types (e.g., AWS EBS in ReadWriteOnce mode) are not double-attached. Mostly historical; CSI drivers now report this via `volumeattachment` and `csinode` objects.
- **VolumeZone** — if the pod's PV is zone-pinned (e.g., AWS EBS in `us-east-1a`), the candidate node must be in the same zone.
- **NodeVolumeLimits** — caps on how many volumes of a given type can attach to a node. Each CSI driver reports its per-node attach limit; this plugin honors it. The classic case is the AWS EBS attach limit (~25–39 depending on instance type).
- **PodTopologySpread** — enforces hard topology spread (`whenUnsatisfiable: DoNotSchedule`). See §8.
- **InterPodAffinity** — enforces pod affinity (`requiredDuringSchedulingIgnoredDuringExecution`) and pod anti-affinity (same level). See §7.
- **SchedulingGates** — actually a PreEnqueue plugin (covered in §11), but the Filter list often references it for completeness.

Filter runs in parallel across nodes via `framework.parallelize`. The scheduler iterates the node tree (a zone-balanced cursor — see §14) and runs each node through the filter chain in a worker pool sized by `--parallelism` (default 16).

### 3.6 PostFilter (Preemption Lives Here)

PostFilter runs **only if Filter found zero feasible nodes**. It is the framework's "the pod can't fit anywhere — try harder" hook. The built-in plugin is `DefaultPreemption`. See §10 for the full algorithm.

PostFilter is allowed to:

- Nominate a node for the pod (write `status.nominatedNodeName`) for future scheduling attempts.
- Trigger preemption (evict lower-priority pods to make room).
- Decide the pod simply cannot be scheduled and return.

If PostFilter nominates a node, the *current* pod is not bound immediately. Instead, preemption deletes the victim pods (`DeletionTimestamp` set, drain over `terminationGracePeriodSeconds`), and the original pod returns to the queue. On a future scheduling attempt, the nominated node is preferred — but if it no longer fits (e.g., another higher-priority pod stole it), the scheduler picks a different one.

### 3.7 PreScore

Parallel to PreFilter, but for Score. Computes per-cycle state used by Score plugins. Example: `InterPodAffinity` PreScore precomputes the topology counts of pods matching the affinity terms so that the per-node Score doesn't redo the work N times.

### 3.8 Score (Per-Node 0..100)

Score plugins assign each *feasible* node (already passed Filter) a score in `[0, 100]`. The framework normalizes per-plugin (NormalizeScore) and then computes a weighted sum across all Score plugins to pick a winner.

Built-in Score plugins:

- **NodeResourcesFit** — score by how much resource headroom remains. The default strategy is `LeastAllocated` (spread); alternatives `MostAllocated` (bin-pack) and `RequestedToCapacityRatio` (custom function). See §13.
- **NodeResourcesBalancedAllocation** — penalizes nodes where (e.g.) CPU utilization differs from memory utilization. Encourages picking nodes whose post-placement utilization is *balanced* across CPU and memory. Practical effect: tends to avoid filling cpu-light nodes with cpu-heavy pods, leaving the cpu-heavy work on cpu-rich nodes.
- **ImageLocality** — favors nodes that already have the pod's container images cached. Significant for very large images (multi-GB ML models); negligible for tiny images. Score is proportional to total image size cached on the node, with diminishing returns past 1000 GB.
- **InterPodAffinity** — soft-affinity score (and soft-anti-affinity penalty).
- **NodeAffinity** — soft `preferredDuringSchedulingIgnoredDuringExecution` weights.
- **PodTopologySpread** — soft spread (`whenUnsatisfiable: ScheduleAnyway`).
- **TaintToleration** — `PreferNoSchedule` taints become a small score penalty here.
- **NodePreferAvoidPods** — deprecated annotation-based "please don't put pods of this controller on this node". Largely subsumed by taints. (Removed in newer releases — if you see it cited, treat it as historical.)

Score plugins each have an integer `weight` in the configuration. The final per-node score is:

```
finalScore(node) = sum over plugins p of (weight_p * normalize(score_p(node)))
```

The default weights are tuned so that no single plugin dominates; if you change one, audit the others.

### 3.9 NormalizeScore

After Score returns raw values in `[MinNodeScore, MaxNodeScore] = [0, 100]`, the framework optionally calls NormalizeScore on each plugin's scores — across all nodes — so a plugin can rerange (e.g., scale by max observed). Most plugins are no-ops here; the meaningful normalizers are `InterPodAffinity` and `NodeResourcesBalancedAllocation`.

### 3.10 Reserve / Unreserve

Reserve is called on every plugin (that registers as Reserve) once the winning node is chosen, but *before* binding starts. It is where the scheduler commits its decision to the in-memory cache via `AssumePod`. From this point onward, the cache reflects "the pod is on this node" — even though etcd does not yet.

If anything downstream (Permit, PreBind, Bind) fails, the framework calls Unreserve on the same plugins to roll back the assumption (`ForgetPod`). Unreserve must be idempotent and order-independent.

A custom Reserve plugin might:

- Reserve an external resource (a GPU UUID in a remote allocator).
- Decrement a custom quota.
- Update a remote scheduler-companion state.

Reserve is the last point inside the scheduling cycle. After Permit, the binding cycle runs in a new goroutine.

### 3.11 Permit (Gate, Wait, Approve, Deny)

Permit can return:

- **Success** — proceed to PreBind immediately.
- **Wait(timeout)** — pause the pod in a holding area; the binding cycle does not start. The framework provides `Approve(podUID)` / `Reject(podUID, reason)` APIs that the plugin (or a companion controller) calls later to unblock. If the timeout expires, the pod is treated as denied and the scheduling cycle returns to Unreserve.
- **Denied** — Unreserve, fail.

Permit is the gang-scheduling hook. A gang plugin says "I've reserved 8 GPUs across 8 different pods on different nodes; wait until I've reserved all 8 before letting any of them bind." See §17.

### 3.12 PreBind

Runs in the binding cycle. The canonical PreBind plugin is `VolumeBinding`: it asks the volume controller (out-of-process, in kube-controller-manager) to provision the PV, then watches for the PVC to become Bound. If PreBind fails, the framework runs Unreserve in the scheduling cycle's plugins and re-queues the pod.

PreBind is where slow side-effects live. The architectural intent: **anything that must happen before the kubelet sees the assignment, but is too slow for the serial scheduling cycle, goes here.**

### 3.13 Bind

The single mandatory write. The framework allows multiple Bind plugins to be registered, but they run in order and the first one to claim the pod terminates the chain. The default `DefaultBinder` plugin issues:

```
POST /api/v1/namespaces/{ns}/pods/{name}/binding
{
  "apiVersion": "v1",
  "kind": "Binding",
  "metadata": {"name": "<pod>", "namespace": "<ns>"},
  "target": {"apiVersion": "v1", "kind": "Node", "name": "<node>"}
}
```

The apiserver's binding subresource patches `spec.nodeName` and triggers the watch event the chosen kubelet was waiting for. The scheduler also updates `status.conditions[type=PodScheduled].status=True`.

### 3.14 PostBind

Fire-and-forget. Used for metrics, notifications, logs. Not for anything that must happen — if PostBind fails, the pod is already bound and the world has moved on.

### 3.15 Worked Example: One Pod Through the Pipeline

To make the abstract concrete, here is a single pod's journey through every extension point. The pod:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: frontend-7b9d-x9q4z
  labels:
    app: frontend
    pod-template-hash: 7b9d
spec:
  schedulerName: default-scheduler
  priorityClassName: production
  containers:
  - name: web
    image: registry.example.com/frontend:v3.7.2
    resources:
      requests:
        cpu: "500m"
        memory: "1Gi"
      limits:
        cpu: "2"
        memory: "2Gi"
    ports:
    - containerPort: 8080
  affinity:
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
      - labelSelector:
          matchLabels:
            app: frontend
        topologyKey: kubernetes.io/hostname
  topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels:
        app: frontend
  tolerations:
  - key: workload-class
    operator: Equal
    value: production
    effect: NoSchedule
```

Cluster: 40 nodes across 3 zones (a, b, c). Current frontend distribution: zone a has 4, zone b has 4, zone c has 3.

**Step 0 — Watch.** The apiserver delivers the pod's ADD event to the scheduler. The PriorityClass admission resolved `production` to value `1000` and stamped `spec.priority`.

**Step 1 — PreEnqueue.** `SchedulingGates` checks `spec.schedulingGates` — empty, OK. The pod enters activeQ.

**Step 2 — QueueSort.** PrioritySort orders by `(priority desc, timestamp asc)`. This pod's priority is 1000; it slots into the heap accordingly.

**Step 3 — Pop.** A scheduler worker pops the pod. Begin scheduling cycle.

**Step 4 — PreFilter.** Each PreFilter plugin runs in sequence:
- `NodeResourcesFit.PreFilter` records pod requests in CycleState: cpu=500m, memory=1Gi.
- `NodePorts.PreFilter` records "no host ports requested" (containerPort doesn't equal hostPort).
- `PodTopologySpread.PreFilter` precomputes the per-zone counts of `app=frontend` pods: {a: 4, b: 4, c: 3}.
- `InterPodAffinity.PreFilter` precomputes the topology counts of `app=frontend` pods per hostname: 11 nodes each have exactly 1 pod (= the existing frontends), 29 nodes have 0.
- `VolumeBinding.PreFilter` sees no PVCs; no-op.
- `NodeAffinity.PreFilter` sees no nodeAffinity; no-op.

**Step 5 — Filter.** The scheduler iterates the node tree in zone-round-robin order. For each node, the filter chain runs. Worked outcomes for one representative node from each zone:

- *Node `n-a-01`* (zone a, no taints, plenty of resources, no existing frontend pod):
  - `NodeUnschedulable`: pass (not cordoned).
  - `NodeName`: pass (no nodeName set).
  - `TaintToleration`: pass (no taints to consider; pod's `workload-class=production` toleration is just sitting there unused).
  - `NodeAffinity`: pass (no affinity).
  - `NodePorts`: pass.
  - `NodeResourcesFit`: 500m + already-used 6000m on n-a-01 ≤ allocatable 8000m → pass.
  - `VolumeRestrictions` / `NodeVolumeLimits` / `VolumeBinding` / `VolumeZone`: pass.
  - `PodTopologySpread`: placing here makes zone a → 5 pods, min still 3 → skew(a) = 2 > maxSkew=1 → **REJECT**.
  - (Filter short-circuits on the first rejection.)
- *Node `n-b-02`* (zone b, no frontend on it, plenty of room):
  - Same as above until PodTopologySpread: zone b → 5 pods, min still 3 → skew(b)=2 → **REJECT**.
- *Node `n-c-05`* (zone c, no frontend on it):
  - PodTopologySpread: zone c → 4 pods, min becomes 4 (a:4, b:4, c:4) → skew=0 → pass.
  - `InterPodAffinity`: this node has 0 frontends; anti-affinity says "no frontend on the same hostname"; 0 < 1 → pass.
  - **FEASIBLE.**
- *Node `n-c-01`* (zone c, already has one frontend):
  - PodTopologySpread: pass (would still satisfy).
  - InterPodAffinity: node already has a frontend pod, anti-affinity violated → **REJECT**.

After the full Filter pass, perhaps 4 nodes in zone c (the ones without an existing frontend) come out feasible. The scheduler will stop iterating once it hits `percentageOfNodesToScore` worth of feasible candidates.

**Step 6 — PostFilter.** Skipped — Filter found feasible nodes.

**Step 7 — PreScore.** Each Score plugin's PreScore runs:
- `InterPodAffinity.PreScore`: precompute the per-node anti-affinity penalty surface.
- `PodTopologySpread.PreScore`: precompute the spread score.
- `NodeAffinity.PreScore`: no soft affinity, no-op.
- `TaintToleration.PreScore`: precompute PreferNoSchedule penalties (none on these nodes).
- `NodeResourcesFit.PreScore`: nothing to precompute (each Score call evaluates independently).

**Step 8 — Score.** For each feasible node (4 candidates), every Score plugin returns 0–100:
- `n-c-05`: NodeResourcesBalancedAllocation=72, ImageLocality=0 (image not cached here), InterPodAffinity=100 (no nearby frontends), NodeResourcesFit=68 (LeastAllocated), NodeAffinity=0 (none), PodTopologySpread=100 (perfect spread), TaintToleration=100.
- `n-c-07`: balanced=70, ImageLocality=85 (image cached!), inter-affinity=100, fit=70, topology=100, taint=100.
- `n-c-08`: balanced=60, locality=0, inter-affinity=100, fit=55, topology=100, taint=100.
- `n-c-12`: balanced=75, locality=85, inter-affinity=100, fit=72, topology=100, taint=100.

**Step 9 — NormalizeScore.** Most plugins identity-normalize. `InterPodAffinity` and `NodeResourcesBalancedAllocation` may rescale.

**Step 10 — Weighted sum.** Default weights: balanced=1, locality=1, inter-affinity=2, fit=1, topology=2, taint=3, nodeaff=2.
- n-c-05 = 72+0+200+68+0+200+300 = 840
- n-c-07 = 70+85+200+70+0+200+300 = 925
- n-c-08 = 60+0+200+55+0+200+300 = 815
- n-c-12 = 75+85+200+72+0+200+300 = 932

**Winner: n-c-12.** A tiny margin over n-c-07 driven by ImageLocality.

**Step 11 — Reserve.** `VolumeBinding.Reserve` (no PVCs, no-op). `cache.AssumePod(pod, "n-c-12")` runs; the cache now treats this pod as running on n-c-12 for any subsequent scheduling decisions in this scheduler's lifecycle.

**Step 12 — Permit.** Permit plugins (none enabled by default): return Success immediately.

**Step 13 — Hand off to binding cycle.** `go schedulingCycle.bind(pod, "n-c-12")`. Scheduling cycle is done; next pod can be popped.

**Step 14 — PreBind.** `VolumeBinding.PreBind` (no PVCs, no-op).

**Step 15 — Bind.** `DefaultBinder` POSTs to `/api/v1/namespaces/default/pods/frontend-7b9d-x9q4z/binding` with target `n-c-12`. The apiserver patches `spec.nodeName=n-c-12` and writes to etcd.

**Step 16 — PostBind.** Metrics emitted (`scheduler_pod_scheduling_duration_seconds` observed end-to-end), event posted (`Scheduled: Successfully assigned default/frontend-7b9d-x9q4z to n-c-12`).

**Step 17 — Watch handoff.** The kubelet on n-c-12 sees the pod via its watch and begins reconciliation: image pull, sandbox setup, CNI, container start. The scheduler's part is done.

Total time from pop to bind in a healthy cluster: 2–20 milliseconds (the Bind itself is the long pole, dominated by apiserver+etcd write latency).

### 3.16 Putting the Pipeline in a Table

| Point | Per-pod or per-node | Can reject | Common uses |
| --- | --- | --- | --- |
| QueueSort | per-pod | — | priority/FIFO ordering |
| PreEnqueue | per-pod | yes (gate) | scheduling gates |
| PreFilter | per-pod | yes (early) | precompute shared state |
| Filter | per-node | yes | feasibility |
| PostFilter | per-pod | — | preemption |
| PreScore | per-pod | — | precompute Score state |
| Score | per-node | — | ranking |
| NormalizeScore | per-plugin | — | rescale |
| Reserve | per-pod | yes (with Unreserve) | commit to cache |
| Permit | per-pod | yes (wait/deny) | gang scheduling |
| PreBind | per-pod | yes | volume bind, slow setup |
| Bind | per-pod | yes (one wins) | the write |
| PostBind | per-pod | — | notify, metrics |

---

### 3.17 The Status Code Vocabulary

Every plugin returns a `*Status` — a wrapped enum plus a message and a list of failed predicates. The codes:

| Code | Meaning | Where used |
| --- | --- | --- |
| `Success` | The plugin allows the pod | every extension point |
| `Error` | Internal error; the scheduler aborts and retries the pod | every extension point |
| `Unschedulable` | This node (or all nodes) doesn't work; "soft" — the pod is unschedulable but may schedule later | Filter, PostFilter, Permit |
| `UnschedulableAndUnresolvable` | Like Unschedulable, but no future cluster change will fix it (e.g., wrong arch) | Filter |
| `Wait` | Permit returns this to hold the pod | Permit only |
| `Skip` | Plugin elected not to participate in this cycle | PreFilter, PreScore (signals to skip Filter/Score for this plugin) |
| `Pending` | (newer) plugin needs an external event | PreEnqueue |

The distinction between `Unschedulable` and `UnschedulableAndUnresolvable` matters: the latter means "no point waking up on cluster events — this pod will never schedule unless its spec changes." Used by NodeAffinity when no node has a required label and adding such a node is logically impossible from the current cluster shape, or by VolumeBinding when the PVC requests a storage class that doesn't exist.

The framework aggregates per-node statuses into the FailedScheduling event you see in `kubectl describe pod`. The aggregation is plugin-aware: it groups by reason ("4 node(s) didn't tolerate {dedicated: gpu}", "20 node(s) didn't match Pod's node affinity/selector") rather than dumping a flat list.

### 3.18 CycleState: The Per-Cycle Scratchpad

`CycleState` is a typed map (`map[string]StateData`) that lives for the lifetime of one scheduling cycle. Plugins use it to pass computed data between extension points without re-computing. Conventions:

- Key is unique per plugin and per data type (e.g., `NodeResourcesFit.prefilterStateKey`).
- Values are immutable once written (plugins should not mutate after PreFilter).
- It's not safe for concurrent writes — but reads from Filter (which runs in parallel across nodes) are safe.

Example (paraphrased from `pkg/scheduler/framework/plugins/noderesources/fit.go`):

```go
const preFilterStateKey = "PreFilter" + Name

type preFilterState struct {
    framework.Resource           // pod's effective requests
}

func (s *preFilterState) Clone() framework.StateData { return s }

func (f *Fit) PreFilter(ctx context.Context, cycleState *framework.CycleState, pod *v1.Pod) (*PreFilterResult, *framework.Status) {
    cycleState.Write(preFilterStateKey, computePodResourceRequest(pod))
    return nil, nil
}

func (f *Fit) Filter(ctx context.Context, cycleState *framework.CycleState, pod *v1.Pod, nodeInfo *framework.NodeInfo) *framework.Status {
    s, err := getPreFilterState(cycleState)
    if err != nil {
        return framework.NewStatus(framework.Error, err.Error())
    }
    // ... actually do the per-node check using s ...
}
```

The pattern: compute once in PreFilter, store, read N times in Filter. This is the engineering reason most plugins are PreFilter + Filter as a pair — anything that would otherwise be O(N) of repeated work moves to PreFilter as O(1).

---

## 4. The Scheduling Queue: activeQ, podBackoffQ, unschedulableQ

The scheduling queue (`pkg/scheduler/internal/queue`) is a three-queue construct that decides *what to attempt next*. Diagram first.

```
┌────────────────────────────────────────────────────────────────────────────┐
│                       SCHEDULING QUEUE STATE MACHINE                       │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│        new pod arrives (PreEnqueue OK)                                     │
│                  │                                                         │
│                  ▼                                                         │
│         ┌────────────────┐                                                 │
│         │    activeQ     │  heap ordered by QueueSort (default: priority)  │
│         │                │                                                 │
│         │  pop() → run   │                                                 │
│         │  scheduling    │                                                 │
│         │  cycle         │                                                 │
│         └───┬────┬────┬──┘                                                 │
│             │    │    │                                                    │
│ scheduled OK│    │    │ Unschedulable                                      │
│   (bound)   │    │    │ (no fit, preempted, etc.)                          │
│             │    │    └──────────┐                                         │
│             │    │               ▼                                         │
│             │    │      ┌────────────────────┐                             │
│             │    │      │  unschedulableQ    │                             │
│             │    │      │  (waits for event) │                             │
│             │    │      └─────────┬──────────┘                             │
│             │    │                │ relevant cluster event                 │
│             │    │                │ (NodeAdded, PodDeleted, …)             │
│             │    │                ▼                                        │
│             │    │      ┌────────────────────┐                             │
│             │    │      │  podBackoffQ       │                             │
│             │    │      │  exponential       │                             │
│             │    │      │  backoff: 1,2,4,…  │                             │
│             │    │      └─────────┬──────────┘                             │
│             │    │                │ backoff expires                        │
│             │    │                ▼                                        │
│             │    └────────► back to activeQ                                │
│             ▼                                                              │
│         (binding cycle, then done)                                         │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 The Three Queues

**activeQ** — a heap (priority queue) ordered by the registered QueueSort. The scheduler's main loop calls `activeQ.Pop()`; this blocks if the queue is empty.

**podBackoffQ** — a heap ordered by the time at which the pod becomes eligible to retry. After a failed scheduling attempt, the pod enters podBackoffQ with `nextAttemptAt = now + backoff(attempts)`. A background goroutine moves expired entries to activeQ.

**unschedulableQ** — a set (not a heap) of pods that failed scheduling and are *waiting for a cluster event* that might change the answer. A pod that fails because "no node has 4 GPUs free" sits here until a Node is added, a Pod is deleted, a Node's status changes, etc.

The unschedulableQ is the most subtle piece of the scheduler. It exists because constantly retrying a pod that can't possibly schedule (no relevant cluster state has changed) is wasted work. The scheduler watches for cluster events and uses an event-to-pod mapping to decide which unschedulable pods to wake up.

### 4.2 Backoff Math

Backoff is exponential with caps. From `pkg/scheduler/internal/queue/scheduling_queue.go`:

```
podInitialBackoffSeconds = 1   (default)
podMaxBackoffSeconds     = 10  (default)

backoffDuration(attempts) = min(initial * 2^(attempts-1), max)

attempts | backoffDuration
       1 | 1s
       2 | 2s
       3 | 4s
       4 | 8s
       5 | 10s   (capped)
       6 | 10s
       …
```

The cap of 10 seconds is a deliberate choice: it keeps pending pods responsive to cluster changes while avoiding tight retry loops. Some operators raise it to 30s in clusters that have many "stuck" pods (e.g., waiting on autoscaler-provisioned capacity), to reduce scheduler load — at the cost of slower convergence when capacity does arrive.

### 4.3 Event-Driven Wake-Up

The scheduler maintains a static map of `ClusterEvent → []PluginName`. Each plugin declares which events might unblock a pod that failed because of *that* plugin's filter. Examples:

| ClusterEvent | Plugins that care |
| --- | --- |
| Node Add | NodeAffinity, NodeResourcesFit, NodePorts, VolumeZone, TaintToleration, … |
| Node Update | TaintToleration (taints changed), NodeAffinity (labels changed), NodeResourcesFit (allocatable changed) |
| Node Delete | (none; a deleted node only matters via the next add) |
| Pod Add | InterPodAffinity, PodTopologySpread (a new pod changes counts) |
| Pod Delete | NodeResourcesFit (frees resources), PodTopologySpread, InterPodAffinity |
| Pod Update (label change) | InterPodAffinity, PodTopologySpread |
| PersistentVolume Add | VolumeBinding |
| StorageClass Add | VolumeBinding |
| CSINode Update | VolumeBinding, NodeVolumeLimits |

When an event fires, the scheduler walks the unschedulableQ. For each pod that failed because of one of the plugins listed for that event, it moves the pod to the podBackoffQ (so it retries after the backoff). Pods whose failure reason doesn't match the event stay put.

This is why "I deleted some pods and the new ones still don't schedule" is sometimes confusing — if the failure plugin was, say, `NodeAffinity` (no node with the required label), `Pod Delete` doesn't wake those pods. You need to *add a node* with the right label.

### 4.4 The "Stuck on unschedulableQ" Diagnosis Pattern

The most common scheduling pathology in production is "pods are pending forever". The diagnostic ladder:

1. **`kubectl describe pod`** — read the events. The scheduler writes a `FailedScheduling` event with a reason: `0/40 nodes are available: 30 Insufficient cpu, 10 Insufficient memory`. That line is gold; it tells you *which Filter plugins rejected which nodes*. Read it as: "30 nodes failed NodeResourcesFit on CPU, 10 failed it on memory".
2. **Check the queue metrics**: `scheduler_pending_pods{queue="unschedulable"}`. If it grew at the same time as the failed scheduling, your pod is sitting on unschedulableQ waiting for the right event.
3. **Map the reason to events**. "Insufficient cpu" → wait for a Node Add or a Pod Delete. If neither will happen, the pod is permanently stuck and you need an autoscaler (or human capacity).
4. **Check for typos in selectors**. `nodeSelector: gpu=true` vs `nodeSelector: gpu="true"` — the second is correct YAML; the first parses as a boolean and may not match label values that are strings. (This bites people.)
5. **Check schedulingGates** — if `spec.schedulingGates` is non-empty, no PreEnqueue success means the pod never reaches activeQ at all. See §11.

---

### 4.5 The MoveAllToActive Sweep

When certain "broad" events happen (e.g., a periodic flush, the scheduler having just bound a pod, a force-requeue trigger), the queue performs `MoveAllToActiveOrBackoff`: every pod on the unschedulableQ is examined and moved to either activeQ (if its backoff has expired) or podBackoffQ (otherwise). This is the "give every stuck pod another chance" hammer.

The conditions that trigger MoveAll are limited and intentional — over-eager sweeps cause busy loops. The two main triggers:

1. **Periodic flush** — every 30 seconds, regardless of events, the queue retries pods that have been on unschedulableQ longer than `podMaxInUnschedulablePodsDuration` (default 5 minutes). This is the fallback for pods whose wake-up event the scheduler missed.
2. **After a successful bind** — when one pod schedules, other pods that were blocked by "no resources" may now fit (a newly-vacated slot, an autoscaler-provisioned node, etc.). Recent versions are more targeted here, only waking pods that match the event types.

If you observe pods stuck on unschedulableQ for ≥5 minutes despite cluster changes, the periodic flush will eventually wake them; the symptom is "the pod sits, then suddenly schedules five minutes later." Diagnose by checking what event *should* have woken it; that often reveals a plugin's event registration gap.

### 4.6 Per-Pod State in QueuedPodInfo

The queue doesn't just store pods — it stores `QueuedPodInfo`:

```go
type QueuedPodInfo struct {
    PodInfo                     *PodInfo
    Timestamp                   time.Time           // when first enqueued
    Attempts                    int                 // for backoff math
    InitialAttemptTimestamp     *time.Time
    UnschedulablePlugins        sets.Set[string]    // which plugins rejected last time
    PendingPlugins              sets.Set[string]    // for PreEnqueue
    Gated                       bool
}
```

`UnschedulablePlugins` is the magic field for event matching. When the queue decides whether a cluster event should wake a pod, it checks: does any of `UnschedulablePlugins` register interest in this event? If yes, wake. If no, leave it.

This is why "delete a pod that was using a unique label" doesn't always wake pods that had nothing to do with that label. The event-to-plugin-to-pod chain has to match all three.

---

## 5. The Node Info Cache (Snapshot Semantics)

The scheduler maintains a `Cache` (under `pkg/scheduler/internal/cache`) that aggregates, per node:

- `Allocatable` (from `node.status.allocatable`)
- `Requested` (sum of `requests` across all pods on the node, including "assumed" pods)
- `UsedPorts` (set of host-port tuples in use)
- `ImageStates` (image name → size, count of nodes that have it)
- `PVCRefCounts` (PVC name → count of pods using it on this node)
- `Generation` (monotonic; bumped on any change)

Between scheduling cycles, the scheduler takes a *snapshot* of the cache. The snapshot is a copy-on-write data structure — unchanged NodeInfo entries share memory between snapshots. The scheduling cycle reads only from the snapshot, never directly from the live cache.

Why snapshot semantics? Because the cache changes under your feet. A pod created on node X by *another* scheduler (yes, this happens — DaemonSet pods set `spec.nodeName` directly), or a node's allocatable changing because of device-plugin updates, would cause inconsistencies if the cycle read live data.

### 5.1 The Assumed-Pod Trick

When the scheduling cycle picks node N for pod P, the Reserve extension point calls `cache.AssumePod(P, N)`. The cache treats P as if it were already running on N — its requests count against N's allocatable for *future* scheduling decisions.

But P isn't bound yet (binding happens after Reserve). If the binding cycle fails, `ForgetPod` is called to undo the assumption. If the binding succeeds, the watch event on the apiserver eventually delivers the bound pod back to the scheduler; the scheduler "promotes" the assumed pod to a real pod (via `AddPod`) and removes the assumption.

There's a race: what if a new pod is scheduled while P is assumed but not yet bound, and the binding fails? Answer: the new pod was scheduled against a cache that counted P's resources, so its decision was correct for the world-as-of-the-snapshot. When P's binding fails, Forget is called, P's resources free up, and the unschedulableQ wake-up logic re-evaluates pending pods. Eventual consistency holds.

### 5.2 Stale Cache TTL

Assumed pods have a TTL (default 30 seconds). If a pod sits "assumed" longer than the TTL without becoming bound (binding cycle failed quietly, scheduler restarted mid-bind, etc.), the cache evicts the assumption. This is the "self-healing" property of the cache; it prevents leaks if the binding cycle's Unreserve path doesn't run.

---

## 6. NodeResourcesFit: Filter and Score in Detail

NodeResourcesFit is the workhorse plugin. It both filters (does the pod fit?) and scores (how full is the node?). Almost every pod that schedules touches this plugin's logic.

### 6.1 What Is a Resource?

A "resource" in Kubernetes is an `(string, Quantity)` pair on `pod.spec.containers[*].resources.requests` and `…limits`. There are four categories:

| Category | Examples | Source |
| --- | --- | --- |
| Native CPU/memory | `cpu`, `memory` | reported by kubelet from cgroups |
| Native ephemeral storage | `ephemeral-storage` | reported by kubelet (rootfs free) |
| HugePages | `hugepages-2Mi`, `hugepages-1Gi` | reported by kubelet via sysfs |
| Extended resources | `nvidia.com/gpu`, `example.com/foo` | reported by device plugins |
| Scalar named resources | same as extended | same |

The scheduler treats all of them identically: an opaque resource name with a Quantity. Whatever the device plugin says is allocatable is what the scheduler will fit against.

### 6.2 Filter Math

For each candidate node N and each requested resource R:

```
fits(N, P, R) = (allocatable[R] - sumOfRequests[R] - P.requests[R]) >= 0

where
  allocatable[R]    = N.status.allocatable[R]
  sumOfRequests[R]  = sum over all assumed+running pods Q on N of:
                        sum over containers c in Q of:
                          (max(c.requests[R], c.initRequests[R])  if init runs sequentially)
                          (separately tracked for sidecar init containers since 1.29)
  P.requests[R]     = pod-level effective request (max of init vs sum of main, etc.)
```

The scheduler reduces pod-level effective requests differently for init containers vs regular containers:

- **Regular containers**: their requests sum.
- **Init containers** (sequential): the *max* request across init containers (only one runs at a time).
- **Sidecar init containers** (KEP-753, `restartPolicy: Always` on an init container, GA in 1.29): these run for the lifetime of the pod, so their requests *sum with* regular containers.

Effective pod request for resource R:

```
podRequests[R] = max(
    sum(regular containers' requests[R]) + sum(sidecar init containers' requests[R]),
    max(non-sidecar init containers' requests[R])
)
```

Practical implication: a pod with a 4Gi init container that exits, plus three 1Gi main containers, requests `max(4, 3) = 4Gi`. Add a 2Gi sidecar init container and it becomes `max(4, 3+2) = 5Gi`.

### 6.3 Limits Don't Affect Scheduling

This trips people up constantly: **`resources.limits` is never consulted by the scheduler**. Only `requests` matters for fit. Limits affect runtime behavior (cgroup caps, OOM, CPU throttling — chapter 21) but are invisible to scheduling decisions.

Consequence: a pod with `requests: cpu=100m, limits: cpu=4` schedules as a 100m pod. The scheduler will happily put 50 such pods on a 4-core node (sum of requests = 5 cores, but allocatable is 4, so actually only 40 fit — but the point is, *limits* didn't enter the calculation).

This is why oversubscription is the default in Kubernetes: requests are what the scheduler reserves, limits are what the runtime caps. You overcommit by setting limits > requests.

### 6.4 Score Math (Strategies)

NodeResourcesFit has three score strategies, configured via `pluginConfig`:

**LeastAllocated** (default, "spread"):
```
score(N) = average over resources R of:
    ((allocatable[R] - requested[R]) / allocatable[R]) * 100
```
Higher score → more free → prefer it. This pushes pods toward emptier nodes.

**MostAllocated** ("bin-pack"):
```
score(N) = average over resources R of:
    (requested[R] / allocatable[R]) * 100
```
Higher score → fuller → prefer it. Encourages packing pods onto fewer nodes so other nodes can scale down (works well with Cluster Autoscaler).

**RequestedToCapacityRatio** (custom-shape):
```
score(N, R) = userDefinedShape((requested[R] + pod.requests[R]) / allocatable[R])
```
The user provides a list of `(utilization, score)` points; the scheduler interpolates linearly. Lets you say "prefer 80% utilization; penalize anything above 95%".

Choose `MostAllocated` for cost-optimized clusters (let nodes empty and scale down). Choose `LeastAllocated` for latency-sensitive workloads (don't let nodes get hot). RequestedToCapacityRatio is for clusters with non-monotonic preferences (e.g., "I want nodes to be 70% full, not more not less").

### 6.5 Extended Resources Don't Have Defaults

If a pod requests `nvidia.com/gpu: 1` but no nodes report that resource as allocatable, NodeResourcesFit filters out *every* node. The pod sits forever (or until a node with GPUs joins). This is the source of many "pod sits Pending" surprises: the user thought GPUs were ubiquitous, but the device plugin isn't installed on the right nodes.

**Always pair GPU requests with a `nodeSelector` or `nodeAffinity` that scopes to GPU nodes.** This isn't strictly required for correctness (NodeResourcesFit handles it), but it produces better failure messages and avoids surprising the descheduler.

---

## 7. Affinity, Anti-Affinity, and Their Costs

### 7.1 Three Kinds of Affinity

```
spec.affinity:
  nodeAffinity:                      # pod ↔ node
    requiredDuringSchedulingIgnoredDuringExecution:  ...   # HARD: filter
    preferredDuringSchedulingIgnoredDuringExecution: ...   # SOFT: score

  podAffinity:                       # pod ↔ pod (co-locate)
    requiredDuringSchedulingIgnoredDuringExecution:  ...   # HARD
    preferredDuringSchedulingIgnoredDuringExecution: ...   # SOFT

  podAntiAffinity:                   # pod ↔ pod (anti-co-locate)
    requiredDuringSchedulingIgnoredDuringExecution:  ...   # HARD
    preferredDuringSchedulingIgnoredDuringExecution: ...   # SOFT
```

The `…DuringExecution` half of the name is a promise of future Kubernetes versions to evict pods if their affinity rule becomes violated post-scheduling. *That feature does not exist.* All affinity is `IgnoredDuringExecution` — once scheduled, the pod stays put even if labels change. Treat the names as scars.

### 7.2 NodeAffinity in Detail

```yaml
nodeAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    nodeSelectorTerms:                          # OR across terms
    - matchExpressions:                         # AND within a term
      - key: kubernetes.io/arch
        operator: In
        values: [amd64]
      - key: workload-class
        operator: In
        values: [batch, batch-spot]
    - matchExpressions:
      - key: kubernetes.io/arch
        operator: In
        values: [arm64]
      - key: workload-class
        operator: In
        values: [batch-arm]
  preferredDuringSchedulingIgnoredDuringExecution:
  - weight: 50
    preference:
      matchExpressions:
      - key: topology.kubernetes.io/zone
        operator: In
        values: [us-east-1a]
```

Operators: `In`, `NotIn`, `Exists`, `DoesNotExist`, `Gt`, `Lt`. The first two take a values list; the next two take none; the last two take a single numeric string.

NodeAffinity is essentially free at scale: the plugin's PreFilter can compute the matching node set once and pass it as a node-name hint to Filter, so the per-node Filter pass becomes O(matching-nodes) not O(all-nodes).

### 7.3 PodAffinity / PodAntiAffinity in Detail

```yaml
podAntiAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
  - labelSelector:
      matchLabels:
        app: redis
    topologyKey: kubernetes.io/hostname    # 1 redis per node
  - labelSelector:
      matchLabels:
        app: redis
    topologyKey: topology.kubernetes.io/zone
    namespaces: [prod]                      # cross-namespace match
    namespaceSelector: {}                   # alternative: select by label
```

The semantics: for hard anti-affinity with `topologyKey=hostname`, the rule means "no other pod matching this labelSelector may exist on a node with the same `kubernetes.io/hostname` label". For a `topologyKey=zone`, the rule means "no other pod matching the labelSelector may exist in the same zone".

`topologyKey` is the trap. It must be a label that exists on every node you care about (otherwise the plugin treats the node as having no topology and the rule may not apply consistently). Common values:

- `kubernetes.io/hostname` (one pod per node)
- `topology.kubernetes.io/zone`
- `topology.kubernetes.io/region`
- Custom labels like `rack`, `failure-domain`

### 7.4 The O(P × N) Cost

Pod affinity is expensive. Here's why:

For each candidate node N, the Filter must check: does N have a pod matching the labelSelector? But that's not the full check — it must determine the topology-domain of N and ask "is there any pod matching the labelSelector on any node in N's topology domain?"

With P pods matching the selector and N candidate nodes, the naive cost is O(P × N). The InterPodAffinity plugin does heroic precomputation in PreFilter/PreScore (it pre-counts pods per topology domain), so the per-Filter cost becomes O(1) lookup. But the precomputation itself is O(P × N) in the worst case (every Pod creation event invalidates).

At 5,000 nodes and 50,000 pods, pod affinity terms with `topologyKey=hostname` and broad labelSelectors are the single largest source of scheduling latency. The standard advice: **don't use pod anti-affinity for spread; use PodTopologySpread (§8).** Pod anti-affinity remains useful for genuine "must not co-locate" rules — pairs of pods where co-location is incorrect, not merely undesirable.

### 7.5 Cross-Namespace Affinity

By default, pod-affinity rules match pods in the same namespace as the pod evaluating the rule. You can broaden the match by listing namespaces explicitly or by using a `namespaceSelector` (label-selector on Namespace objects). Cross-namespace affinity is also expensive — the plugin must consider pods across the entire cluster.

---

## 8. Pod Topology Spread Constraints

PodTopologySpread is the modern primitive for "spread my replicas across failure domains". It's strictly more expressive (and cheaper) than pod anti-affinity for spread use cases.

### 8.1 The Spec

```yaml
spec:
  topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: DoNotSchedule        # or ScheduleAnyway
    labelSelector:
      matchLabels:
        app: web
    minDomains: 3                            # require at least 3 domains
    matchLabelKeys: [pod-template-hash]      # spread per-revision
    nodeAffinityPolicy: Honor                # respect nodeAffinity in counts
    nodeTaintsPolicy: Honor                  # respect taints in counts
```

### 8.2 Skew, Defined

The plugin counts pods matching `labelSelector` per topology domain (each distinct value of the node label `topologyKey`). The *skew* of a domain D is:

```
skew(D) = countOfMatchingPods(D) - min over all eligible domains of countOfMatchingPods
```

`maxSkew` is the maximum permitted skew. If placing a new pod on a node in domain D would make `skew(D) > maxSkew`, the constraint is violated. `whenUnsatisfiable=DoNotSchedule` makes that a hard Filter rejection; `ScheduleAnyway` makes it a soft Score penalty.

### 8.3 Worked Example

Three zones (`a`, `b`, `c`), `maxSkew: 1`, `labelSelector: app=web`. Current state:

```
zone a: 3 pods
zone b: 2 pods
zone c: 2 pods
```

`min = 2`. So:

```
skew(a) = 3 - 2 = 1   ← OK (≤ maxSkew=1)
skew(b) = 2 - 2 = 0
skew(c) = 2 - 2 = 0
```

A new pod arrives:

- If placed in zone a → counts become (4, 2, 2), min=2, skew(a)=2 → VIOLATES maxSkew=1.
- If placed in zone b → counts become (3, 3, 2), min=2, skew(a)=1, skew(b)=1, skew(c)=0 → OK.
- If placed in zone c → counts become (3, 2, 3), min=2 → OK.

So the Filter (under `DoNotSchedule`) rejects nodes in zone a. The Score (under `ScheduleAnyway` for some other constraint) would penalize zone a but allow it.

### 8.4 minDomains

If you have three zones but only one of them currently has pods, `min = 0` for the empty zones — except those empty zones might not be visible to the plugin (if no node has been added to them yet, the topology key value isn't observable). `minDomains` forces the plugin to assume at least N domains exist, treating "missing" domains as having 0 pods. This is essential when you have known-cardinality topologies (e.g., always exactly 3 AZs).

Without `minDomains`, a freshly-bootstrapped service that only has pods in one zone may not spread to other zones because the plugin doesn't know they exist.

### 8.5 The "Spread by Zone, then by Host" Recipe

```yaml
topologySpreadConstraints:
- maxSkew: 1
  topologyKey: topology.kubernetes.io/zone
  whenUnsatisfiable: DoNotSchedule
  labelSelector: { matchLabels: { app: web } }
  minDomains: 3
- maxSkew: 1
  topologyKey: kubernetes.io/hostname
  whenUnsatisfiable: ScheduleAnyway
  labelSelector: { matchLabels: { app: web } }
```

The first constraint enforces even zone distribution (hard). The second softly spreads within each zone across hosts. This is the right pattern for HA workloads in a multi-AZ cluster.

### 8.6 Topology Spread vs PodAntiAffinity

Almost every use of `podAntiAffinity` with `topologyKey=hostname` is better expressed as PodTopologySpread with the same key and `maxSkew=1`. Reasons:

- **Cheaper at scale**: TopologySpread precomputes per-domain counts; AntiAffinity does O(P×N).
- **More expressive**: TopologySpread lets you say "skew of 2 is fine" (`maxSkew: 2`). AntiAffinity is all-or-nothing.
- **Better with autoscalers**: TopologySpread plays better with the cluster autoscaler when scaling from 0.

Anti-affinity is still the right tool for "literally must not be on the same node" (e.g., two pods that fight for a host port, or master/standby pairs where co-location is a correctness bug).

### 8.7 `nodeAffinityPolicy` and `nodeTaintsPolicy`

By default (since GA), spread counts consider only nodes the pod *could* be scheduled to (i.e., nodes that pass the pod's nodeAffinity and toleration checks). You can flip these policies to `Ignore` to count all nodes regardless of whether the pod could land there. The default `Honor` is almost always correct.

### 8.8 `matchLabelKeys`

`matchLabelKeys: [pod-template-hash]` says "only spread against pods with the same value for this label as me". For Deployments, `pod-template-hash` is unique per ReplicaSet. So each rolling-update revision spreads independently, and you don't get a transient "skewed" warning during rollouts when half the pods belong to the new revision and half to the old.

---

## 9. Taints and Tolerations

Taints are node-side rules ("don't put pods here unless they say it's OK"); tolerations are pod-side acknowledgements ("I'm OK with this taint"). The scheduler enforces all of this via the TaintToleration plugin (Filter and Score).

### 9.1 The Three Effects

| Effect | Filter behavior | Score behavior | Eviction |
| --- | --- | --- | --- |
| `NoSchedule` | hard reject if no toleration | — | none |
| `PreferNoSchedule` | always accept | per-taint Score penalty | none |
| `NoExecute` | hard reject if no toleration | — | yes (existing pods evicted unless they tolerate, after `tolerationSeconds`) |

`NoExecute` evictions are handled by the node-lifecycle controller, *not* the scheduler. The scheduler only ensures new pods land on tolerating nodes.

### 9.2 `tolerationSeconds`

```yaml
tolerations:
- key: node.kubernetes.io/not-ready
  operator: Exists
  effect: NoExecute
  tolerationSeconds: 300        # tolerate for 5 minutes, then evict
```

When a `NoExecute` taint is applied, pods that don't tolerate it are evicted immediately. Pods that tolerate it stay forever (if `tolerationSeconds` is unset) or until the timer expires (if set).

The default `tolerationSeconds` for the built-in `not-ready` and `unreachable` taints is 300s — this is why a node going `NotReady` doesn't immediately remove pods; there's a 5-minute grace period. Tune via `--default-not-ready-toleration-seconds` on the apiserver (admission injects the toleration).

### 9.3 Built-in Taints

The node-lifecycle controller automatically applies:

| Taint | When | Effect |
| --- | --- | --- |
| `node.kubernetes.io/not-ready` | kubelet reports NotReady | NoExecute |
| `node.kubernetes.io/unreachable` | controller can't reach kubelet | NoExecute |
| `node.kubernetes.io/memory-pressure` | kubelet eviction signal | NoSchedule (for BestEffort) |
| `node.kubernetes.io/disk-pressure` | kubelet eviction signal | NoSchedule |
| `node.kubernetes.io/pid-pressure` | kubelet eviction signal | NoSchedule |
| `node.kubernetes.io/network-unavailable` | CNI not ready | NoSchedule |
| `node.kubernetes.io/unschedulable` | `kubectl cordon` | NoSchedule |
| `node.cloudprovider.kubernetes.io/uninitialized` | cloud-controller before init | NoSchedule |

### 9.4 Why DaemonSets Tolerate Everything

A DaemonSet exists to run a pod on every node, including unhealthy ones (you want your node-monitoring agent to keep running even if the node is `NotReady`). The DaemonSet controller injects tolerations for the standard built-in taints into every pod it creates. This is also why DaemonSet pods often have `spec.nodeName` set by the controller directly (skipping the scheduler) — historically, that was how it worked. Modern DaemonSets use the scheduler (since 1.12+), with the explicit tolerations doing the work.

### 9.5 Dedicated Node Pools

The canonical pattern for "only certain pods can use these nodes":

```bash
kubectl taint nodes gpu-node-1 dedicated=gpu:NoSchedule
```

```yaml
tolerations:
- key: dedicated
  operator: Equal
  value: gpu
  effect: NoSchedule
```

Plus a matching `nodeSelector` or `nodeAffinity` to attract the pod to those nodes. The taint excludes; the affinity attracts. You need both — the taint alone keeps other pods off, but doesn't pull your pods on.

---

## 10. PriorityClass and Preemption

### 10.1 PriorityClass

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: high
value: 1000000              # int32; higher = more important
globalDefault: false         # if true, pods without priorityClassName get this
preemptionPolicy: PreemptLowerPriority   # or "Never"
description: "Latency-sensitive frontend pods"
```

Pods reference it via `spec.priorityClassName: high`. The PriorityClass admission controller resolves the name to an integer at admission time and stamps `pod.spec.priority` (immutable thereafter).

The value range is `[-2^31, 2^31-1]`. Values >= `10^9` (one billion) are reserved for system-critical classes:

| Built-in | Value | Use |
| --- | --- | --- |
| `system-cluster-critical` | 2000000000 | cluster-wide essentials (kube-dns, etc.) |
| `system-node-critical` | 2000001000 | per-node essentials (node-exporter, fluent-bit, csi-driver) |

User workloads should use values < 10^9. The convention is: 1000, 10000, 100000 for low/medium/high; reserve higher values for unusual cases.

### 10.2 `preemptionPolicy: Never`

A PriorityClass can declare `preemptionPolicy: Never`. This means: pods of this class still queue at high priority (they're scheduled before lower-priority pods in the activeQ), but they *do not* trigger preemption of lower-priority pods. The high-priority pod just waits.

Use case: an important batch job that should go first when capacity exists, but shouldn't kick out interactive workloads to get scheduled now.

### 10.3 Preemption Algorithm

Preemption is a PostFilter plugin (`DefaultPreemption`). It runs only after Filter has found zero feasible nodes for the pod. The algorithm, at a high level:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          PREEMPTION DECISION TREE                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   pod P fails Filter on all N nodes                                         │
│                  │                                                          │
│                  ▼                                                          │
│       ┌──────────────────────┐                                              │
│       │ priorityClass set?   │── no ──▶ no preemption; pod waits            │
│       └──────────┬───────────┘                                              │
│                  │ yes                                                      │
│                  ▼                                                          │
│       ┌──────────────────────┐                                              │
│       │ preemptionPolicy ==  │── Never ──▶ no preemption; pod waits         │
│       │ PreemptLowerPriority?│                                              │
│       └──────────┬───────────┘                                              │
│                  │ yes                                                      │
│                  ▼                                                          │
│       ┌──────────────────────────────────────────┐                          │
│       │ For each node n in candidate set         │                          │
│       │ (filtered to those where some pods could │                          │
│       │ be removed to make P fit):               │                          │
│       │                                          │                          │
│       │  1. Find pods on n with priority < P.    │                          │
│       │  2. Sort them by priority asc, then by   │                          │
│       │     pod-disruption-cost desc.            │                          │
│       │  3. Greedy: remove pods one at a time    │                          │
│       │     until P fits (dry-run filter).       │                          │
│       │  4. Then try to put pods back as long    │                          │
│       │     as P still fits (minimize victims).  │                          │
│       │  5. Reject node if any PDB has           │                          │
│       │     DisruptionsAllowed=0 for victims.    │                          │
│       └──────────┬───────────────────────────────┘                          │
│                  ▼                                                          │
│       ┌──────────────────────────────────────────┐                          │
│       │ Pick node with the "cheapest" set:       │                          │
│       │  - fewest PDB violations (none ideally)  │                          │
│       │  - highest min priority among victims    │                          │
│       │  - fewest victims                        │                          │
│       │  - highest sum of victims' priorities    │                          │
│       │  - earliest victim start time (recent    │                          │
│       │    pods are cheaper to kill)             │                          │
│       └──────────┬───────────────────────────────┘                          │
│                  ▼                                                          │
│       ┌──────────────────────────────────────────┐                          │
│       │ Set P.status.nominatedNodeName = n       │                          │
│       │ Delete victims (gracePeriod honored)     │                          │
│       │ Return; P re-enters queue                │                          │
│       │ (it does NOT bind immediately)           │                          │
│       └──────────────────────────────────────────┘                          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 10.4 Dry-Run, Best-Effort

The Filter check during preemption is a *dry run* on the candidate node: "if these pods weren't here, would P pass Filter?" That's why preemption can decide to evict, even though it can't guarantee P will land on that node. After eviction, the node might be claimed by another higher-priority pod. The preempting pod's `nominatedNodeName` is a *preference*, not a reservation.

This best-effort nature is a frequent source of confusion. "I preempted, but my pod didn't land there!" Correct — preemption frees space; landing is a separate decision in the next scheduling cycle.

### 10.5 PodDisruptionBudgets and Preemption

If evicting a victim would violate a PDB (i.e., the PDB has `DisruptionsAllowed=0` and the victim is part of the protected set), the scheduler **prefers** to skip that node — but if no other node works, it will *still* preempt and the PDB violation is recorded. PDBs are not hard barriers to preemption; they are tiebreakers.

This is a deliberate choice. Strict PDB enforcement during preemption would let a misconfigured PDB block all preemption forever, which is worse than the alternative. The trade-off: critical workloads should set high priority *and* PDB; relying on PDB alone won't save them from a higher-priority preemptor.

### 10.6 Cross-Node Preemption

Preemption is *single-node*. The plugin never says "evict one pod from node A and one from node B to make P fit on either." This bounds the search and keeps it tractable; it also means that very large pods (that no single node could accommodate even with all evictable pods gone) are simply unschedulable.

If you need cross-node preemption (or gang preemption), you're in custom-scheduler territory (chapter 34).

### 10.7 Preemption is Not Capacity Management

A common mistake: using priority+preemption as a substitute for autoscaling. "We'll set our batch jobs to low priority; when the latency-sensitive workload spikes, it preempts them." That works once. The second time, the batch jobs are gone, the latency workload still needs more, and there's nothing left to preempt — you're out of capacity.

Preemption shines for *transient* priority inversions (a brief spike in important work) where the deprived low-priority work can wait. For sustained capacity differences, you need an autoscaler.

---

## 11. Scheduling Gates (KEP-3521)

### 11.1 What They Do

```yaml
spec:
  schedulingGates:
  - name: example.com/quota-check
  - name: example.com/license-validated
```

A pod with non-empty `schedulingGates` fails the built-in `SchedulingGates` PreEnqueue plugin. It never enters the activeQ. It sits in the unschedulableQ (technically a gated set, but effectively the same) until something *clears the gates* by removing them from `spec.schedulingGates`.

Only certain operations on the gates field are allowed: a controller can *remove* gates but cannot *add* them after pod creation. (The pod's gates list can only shrink monotonically.) This prevents flapping; once cleared, a gate stays cleared.

### 11.2 The Pattern

The intended use is a *queueing controller* sitting between pod creation and scheduling:

```
Pod created with gates: [quota]
      │
      ▼
[Quota controller watches pods with quota gate]
      │
      ├─ If quota available: PATCH pod removing quota gate
      │                          │
      │                          ▼
      │                  Scheduler enqueues, runs scheduling cycle
      │
      └─ If quota not available: leave gate, mark in status
```

Practical examples:

- **Batch queue management** (Kueue, Volcano): the queue manager controls when pods become eligible for scheduling.
- **License validation**: don't schedule until a license check returns OK.
- **External provisioning gates**: don't schedule until a backing external resource (e.g., a remote DB) is ready.

Before scheduling gates existed, the workaround was to create the pod with an unsatisfiable nodeSelector (the "carve a label in stone" trick) and have a controller remove the selector when ready. Gates are cleaner: explicit, no semantic abuse, and visible in the API.

### 11.3 The Failure Mode: Stuck Gates

The single biggest hazard with gates: a controller crashes or is misconfigured and never removes its gate. The pod sits forever with no scheduling attempts. The events are quiet (the scheduler never tried, so there's no `FailedScheduling`). Diagnosis: `kubectl get pod <name> -o jsonpath='{.spec.schedulingGates}'`. Always include a Prometheus alert on pods with `spec.schedulingGates` older than N minutes.

---

## 12. Profiles and Multiple Schedulers

### 12.1 Profiles in a Single Scheduler

A scheduler binary can run multiple *profiles*, each with its own plugin set and weights. Pods choose a profile via `spec.schedulerName`:

```yaml
# In KubeSchedulerConfiguration:
profiles:
- schedulerName: default-scheduler
  plugins:
    score:
      enabled:
      - name: NodeResourcesFit
        weight: 1
- schedulerName: bin-packer
  plugins:
    score:
      enabled:
      - name: NodeResourcesFit
        weight: 5
      disabled:
      - name: NodeResourcesBalancedAllocation
  pluginConfig:
  - name: NodeResourcesFit
    args:
      scoringStrategy:
        type: MostAllocated
```

```yaml
# In a pod:
spec:
  schedulerName: bin-packer
```

The pod is routed to the named profile. Both profiles share the same queue, cache, and informers — only the plugin chain differs.

### 12.2 Multiple Scheduler Binaries

You can also run a second scheduler binary alongside the default. The architecture is straightforward: both schedulers watch all pods; each only considers pods whose `spec.schedulerName` matches its configuration; both maintain independent caches; both write Bind via the apiserver.

The risk: race conditions. If two schedulers both think they own pod P (misconfigured schedulerName matching, or a bug), both will try to bind it. Whichever bind succeeds first wins; the other gets a conflict error. Not catastrophic, but messy. Always make `schedulerName` unique per scheduler instance.

### 12.3 Profile vs Separate Binary — When to Pick Which

| Use profile | Use separate binary |
| --- | --- |
| Different scoring strategies for different workloads | Plugin requires custom Go code (out-of-tree) |
| Different plugin weights | Different Kubernetes version of the scheduler |
| Different filter sets sharing the same plugin code | Different lifecycle (release independently) |
| Operational simplicity (one HA target) | Different fault-isolation requirements |
| All teams use the upstream scheduler image | One team needs experimental code |

Profiles are almost always the right answer if you can use them. A separate binary is the answer when you need new code that you don't want to upstream (chapter 34).

---

## 13. Scoring Strategies (Bin-Pack vs Spread)

The single most operationally consequential scheduler decision is whether you want to spread or bin-pack. This is configured via `NodeResourcesFit`'s `scoringStrategy.type`:

### 13.1 LeastAllocated (Spread)

```yaml
pluginConfig:
- name: NodeResourcesFit
  args:
    scoringStrategy:
      type: LeastAllocated
      resources:
      - name: cpu
        weight: 1
      - name: memory
        weight: 1
```

Pods land on emptier nodes. Pros: even load distribution, less noisy-neighbor risk, better resilience to single-node failures (smaller blast radius). Cons: every node is partially used; you can't scale a node down without migrating pods first.

Choose for: latency-sensitive workloads, clusters with stable size (not autoscaling much), shared-tenancy environments.

### 13.2 MostAllocated (Bin-Pack)

```yaml
    scoringStrategy:
      type: MostAllocated
```

Pods land on fullest nodes. Pros: empty nodes are *fully* empty — the cluster autoscaler can immediately scale them down. Cost goes down because you run fewer nodes. Cons: hotter nodes per node, more noisy-neighbor risk on the full ones, worse blast radius.

Choose for: spot/preemptible-heavy fleets, cost-optimized clusters, workloads with predictable resource patterns.

### 13.3 RequestedToCapacityRatio

```yaml
    scoringStrategy:
      type: RequestedToCapacityRatio
      requestedToCapacityRatio:
        shape:
        - utilization: 0
          score: 0
        - utilization: 70
          score: 10
        - utilization: 90
          score: 5
        - utilization: 100
          score: 0
```

Custom non-monotonic shape. Above defines "prefer nodes that would end up at ~70% utilization; avoid both empty and full nodes". Useful when you have specific operational targets (e.g., "always reserve 20% buffer for autoscaling reactivity").

### 13.4 Per-Resource Weights

All strategies accept per-resource weights. A GPU-heavy cluster might weight `nvidia.com/gpu` much higher than CPU so that GPU utilization dominates the scoring decision:

```yaml
    scoringStrategy:
      type: MostAllocated
      resources:
      - name: cpu
        weight: 1
      - name: memory
        weight: 1
      - name: nvidia.com/gpu
        weight: 10
```

Resources omitted from the list get weight 0 (not counted at all). To require a resource in the calculation, you must list it.

---

## 14. Performance at Scale

The scheduler's performance characteristics matter most in three regimes: 1000 nodes, 5000 nodes, and 5000+ nodes (where the SIG-scalability suite officially stops testing).

### 14.1 percentageOfNodesToScore

The scheduler does not evaluate every node when filtering — once it finds "enough" feasible nodes, it stops. The threshold is:

```
nodesToScore = max(
    minFeasibleNodesToFind,                      # default 100
    int(nodes * percentageOfNodesToScore / 100)  # default 50%
)
```

Defaults: 50% capped below at 100 nodes minimum. For a 5000-node cluster, the scheduler evaluates the first 2500 nodes (in node-tree order; see below) that pass Filter. The remaining 2500 are not considered for scoring.

You can raise this to 100% for small clusters (cheap to evaluate all nodes; better scoring outcomes) or lower it for very large clusters (where 50% is still too much work).

```yaml
percentageOfNodesToScore: 30        # consider 30%, min 100
```

### 14.2 Node Tree Iteration (Fairness)

Iteration order matters: if the scheduler always evaluated nodes in zone A first, all pods would land in zone A even with spread enabled (Filter stops at 50%, never sees the other zones).

The scheduler maintains a *node tree* organized by zone and region. Iteration round-robins across zones: zone A, zone B, zone C, zone A, zone B, zone C… so the first 100 nodes visited include a fair mix of zones.

```
       region                              ───── per-cycle starting point
        / | \                              rotates around (next_zone++)
   zoneA zoneB zoneC
   /|\   /|\   /|\
  ... nodes ...

Iteration order: zoneA[0], zoneB[0], zoneC[0], zoneA[1], zoneB[1], …
```

### 14.3 Parallelism

The Filter step parallelizes across nodes. The parallelism is controlled by:

```yaml
parallelism: 16
```

Default is 16 goroutines. Tune up to roughly the number of CPU cores for very large clusters (50+ cores) — but watch the apiserver: aggressive parallelism in the scheduler can drive read-load on caches that other watchers share.

### 14.4 The 5000-Node Reality

At 5000 nodes:

- One scheduling cycle in the steady state: ~10–30ms p99 with default settings.
- Memory usage: ~2–4 GB resident (largely the NodeInfo cache and snapshot history).
- Pod throughput: roughly 100–200 pods/sec in burst, sustained ~50/sec depending on Bind latency.
- Per-node informer load: ~1 watch event per node update; significant during cordon/uncordon sweeps.

If your scheduler can't keep up, in roughly this priority:

1. Reduce `percentageOfNodesToScore` (cheaper Score).
2. Audit your custom plugins — most performance issues at scale come from a custom Filter that does O(N) work per node.
3. Disable expensive built-in plugins you don't need (e.g., if no one uses PodAntiAffinity in your cluster, disable `InterPodAffinity`).
4. Use scheduler profiles to split workloads — high-throughput pods get a lean profile; specialized workloads get a heavy profile.
5. Run multiple schedulers and shard by `schedulerName`.

### 14.5 The Apiserver Side

The scheduler is also bounded by apiserver write throughput. Each bind is a write. With 200 binds/sec you're driving 200 writes/sec to etcd, which is meaningful (etcd defaults handle low thousands, but scheduler binds compete with controller updates, node status updates, etc.).

Use `--kube-api-qps` and `--kube-api-burst` to tune the scheduler's own apiserver client (defaults 50 and 100 respectively — far too low for large clusters). Bump to 300/600 or higher.

---

## 15. Scheduler ↔ Autoscaler / Karpenter

### 15.1 The Boundary

The scheduler does not know about future capacity. If no node satisfies a pending pod, the scheduler logs `FailedScheduling` and moves on. It has no concept of "wait, more nodes are coming".

The Cluster Autoscaler (or Karpenter) lives outside the scheduler:

```
┌──────────────────────────────────────────────────────────────────┐
│                  AUTOSCALING DECISION LOOP                       │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│   [Cluster Autoscaler]                                           │
│        │ every N seconds                                         │
│        ▼                                                         │
│   List all pods with status:                                     │
│     - Pending                                                    │
│     - condition[PodScheduled].reason == "Unschedulable"          │
│     - failure reason mentions resource scarcity                  │
│        │                                                         │
│        ▼                                                         │
│   For each candidate node group, simulate scheduler:             │
│     "if I added a node of this shape, would this pod fit?"       │
│        │                                                         │
│        ▼                                                         │
│   Pick the cheapest node group that fits the most pending pods   │
│        │                                                         │
│        ▼                                                         │
│   Call cloud provider API to add the node                        │
│        │                                                         │
│        ▼                                                         │
│   Node joins cluster (kubelet registers, becomes Ready)          │
│        │                                                         │
│        ▼                                                         │
│   The Node Add event wakes up the unschedulableQ                 │
│        │                                                         │
│        ▼                                                         │
│   Scheduler re-evaluates pending pods; they now schedule         │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 15.2 Karpenter's Twist

Karpenter (cloud-agnostic, by AWS, now CNCF) takes the same loop but picks instance shapes dynamically per-pending-pod rather than from pre-defined node groups. It evaluates pending pods, computes the cheapest set of EC2 instances (or equivalent) that fits them, and provisions directly.

From the scheduler's perspective, Karpenter is just another producer of Node objects. Nothing changes in the scheduler. From the operator's perspective, Karpenter feels like "pods directly create nodes" — but really, the scheduler still does the scheduling. Karpenter just produces nodes that match.

### 15.3 The "Pending pod doesn't trigger scale-up" Pitfall

Autoscalers only act on pods that are pending because of *resources* (and a few other reasons). They do not provision capacity for pods that are unschedulable due to:

- NodeAffinity that no node matches (no candidate node group provides the label).
- Taints that no node group has matching tolerations for.
- VolumeZone mismatches.
- SchedulingGates (autoscaler treats these as not-yet-scheduling, so doesn't react).

If your pods are pending and no scale-up is happening, check the autoscaler's logs for "skipped pod" entries and verify the failure reason is one it understands.

---

## 16. Scheduler ↔ Descheduler

The scheduler decides where pods go at admission time. It does *not* revisit decisions. A pod scheduled on node X at 9 AM stays on node X until something deletes it, even if by noon node X is severely overloaded and node Y is empty.

The Descheduler (a separate controller, not part of kube-scheduler) is the steady-state counterpart. It evicts pods that are in "bad" placements according to configured strategies; the deletion triggers a new pod that the scheduler then places using current state.

### 16.1 Common Descheduler Strategies

| Strategy | Action |
| --- | --- |
| `LowNodeUtilization` | evict pods from over-utilized nodes to balance the cluster |
| `HighNodeUtilization` | evict pods from under-utilized nodes (for bin-packing + scale-down) |
| `RemovePodsViolatingNodeAffinity` | evict pods whose nodeAffinity rules no longer match (labels changed) |
| `RemovePodsViolatingNodeTaints` | evict pods that don't tolerate new taints |
| `RemovePodsViolatingInterPodAntiAffinity` | evict pods that violate anti-affinity (rule added post-scheduling) |
| `RemovePodsViolatingTopologySpreadConstraint` | rebalance spread |
| `RemoveDuplicates` | evict so that no two replicas of the same controller share a node (where possible) |
| `RemovePodsHavingTooManyRestarts` | evict pods that are flapping (signal of bad placement / bad node) |
| `PodLifeTime` | evict pods older than N (forces rescheduling — useful with daily rebalance) |

### 16.2 Why Descheduling Is Necessary

The scheduler is greedy and locally optimal. Over time, the cluster drifts:

- Nodes are added; old pods don't get re-balanced onto them.
- PodTopologySpread constraints added to a Deployment after rollout don't retroactively spread existing pods.
- Spot/preemptible nodes come and go; survivors get crowded.
- A node's labels change; pods with old affinity become "incorrectly" placed.

The Descheduler is a steady-state cleanup. It respects PDBs (won't evict if doing so would violate one). It honors `descheduler.alpha.kubernetes.io/evict: "false"` annotations as a do-not-touch marker.

### 16.3 The Eviction Mechanics

Descheduler doesn't call delete on the pod. It calls the **eviction API**: `POST /api/v1/namespaces/<ns>/pods/<name>/eviction`. This is the same API `kubectl drain` uses. It honors PDBs and triggers a graceful shutdown via `terminationGracePeriodSeconds`. The replacement pod is created by the pod's owner (ReplicaSet, StatefulSet, etc.) and goes through the normal scheduling path.

---

## 17. Gang / Batch Scheduling

The default scheduler schedules one pod at a time. For workloads that need *all-or-nothing* placement — distributed training jobs, Spark stages, MPI ranks — this is fatal: you might successfully schedule 7 of 8 pods, the 8th can't fit, and the 7 successfully-running pods sit idle waiting for the 8th forever.

### 17.1 The Permit Pattern

The framework's Permit extension point exists for this. A gang plugin works like:

1. Pod arrives; its PodGroup CRD (or similar) declares "I'm part of group G of size 8".
2. Filter, Score run normally — Permit is reached.
3. At Permit, the gang plugin checks: are 8 pods of group G ready to bind?
   - If no: return `Wait(timeout)`. The pod is held; Reserve has committed cache; the binding cycle does not start.
   - If yes: this pod returns Success, and the plugin programmatically calls `Approve` on all the previously-waiting pods. All 8 binding cycles start together.

Failure cases:

- Timeout expires before all 8 are ready: each pod's Permit returns Denied; Unreserve frees their resources; they re-enter the queue.
- One pod's binding cycle fails (e.g., volume bind error): only that pod fails; the others remain bound. This is a known weakness — gang scheduling at Permit only synchronizes the *decision*, not the *runtime success*.

### 17.2 Why Default Scheduler Is Bad at It

Even with the Permit hook, the default scheduler runs Filter/Score serially. If your gang has 8 pods, you wait through 8 serial scheduling cycles. If a Filter rejects pod 5, the previous 4 are stuck in Permit holding state, eating reserved resources until they time out.

Real gang schedulers (Volcano, Yunikorn, Kueue) often *batch-schedule*: they look at the whole gang in a single decision, allocate as a unit, and bind everything together. That requires deeper changes than a Permit plugin can express.

### 17.3 The Project Landscape

- **Kueue**: a CNCF project that does queue/quota management above the scheduler. Pods are gated by scheduling gates until Kueue admits them, then the default scheduler places them. Doesn't do true gang scheduling, but pairs well with workloads that have well-formed batch admission semantics.
- **Volcano**: full-fledged batch scheduler with PodGroup CRD, gang scheduling, fair-share queues. Often runs as a *separate scheduler binary* (`schedulerName: volcano`).
- **Yunikorn**: Apache project, batch/multi-tenant scheduler with hierarchical queues.

Chapter 34 covers custom schedulers and these projects in depth.

---

## 18. KubeSchedulerConfiguration in Practice

### 18.1 The v1 Schema

```yaml
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration

# Scheduling parallelism (default 16)
parallelism: 32

# Percentage of nodes to score (default 50%, min 100 nodes)
percentageOfNodesToScore: 50

# Backoff timings
podInitialBackoffSeconds: 1
podMaxBackoffSeconds: 10

# Leader election (when running HA)
leaderElection:
  leaderElect: true
  resourceName: kube-scheduler
  resourceNamespace: kube-system
  resourceLock: leases
  leaseDuration: 15s
  renewDeadline: 10s
  retryPeriod: 2s

# API server client tuning
clientConnection:
  kubeconfig: /etc/kubernetes/scheduler.conf
  qps: 300
  burst: 600

# One or more profiles
profiles:
- schedulerName: default-scheduler
  plugins:
    queueSort:
      enabled:
      - name: PrioritySort
    preFilter:
      enabled:
      - name: NodeResourcesFit
      - name: NodePorts
      - name: VolumeRestrictions
      - name: PodTopologySpread
      - name: InterPodAffinity
      - name: VolumeBinding
      - name: NodeAffinity
    filter:
      enabled:
      - name: NodeUnschedulable
      - name: NodeName
      - name: TaintToleration
      - name: NodeAffinity
      - name: NodePorts
      - name: NodeResourcesFit
      - name: VolumeRestrictions
      - name: NodeVolumeLimits
      - name: VolumeBinding
      - name: VolumeZone
      - name: PodTopologySpread
      - name: InterPodAffinity
    postFilter:
      enabled:
      - name: DefaultPreemption
    preScore:
      enabled:
      - name: InterPodAffinity
      - name: PodTopologySpread
      - name: TaintToleration
      - name: NodeAffinity
      - name: NodeResourcesFit
    score:
      enabled:
      - name: NodeResourcesBalancedAllocation
        weight: 1
      - name: ImageLocality
        weight: 1
      - name: InterPodAffinity
        weight: 2
      - name: NodeResourcesFit
        weight: 1
      - name: NodeAffinity
        weight: 2
      - name: PodTopologySpread
        weight: 2
      - name: TaintToleration
        weight: 3
    reserve:
      enabled:
      - name: VolumeBinding
    permit: {}
    preBind:
      enabled:
      - name: VolumeBinding
    bind:
      enabled:
      - name: DefaultBinder
    postBind: {}
  pluginConfig:
  - name: DefaultPreemption
    args:
      minCandidateNodesPercentage: 10
      minCandidateNodesAbsolute: 100
  - name: NodeResourcesFit
    args:
      scoringStrategy:
        type: LeastAllocated
        resources:
        - name: cpu
          weight: 1
        - name: memory
          weight: 1
  - name: PodTopologySpread
    args:
      defaultingType: System          # or "List"
      defaultConstraints:
      - maxSkew: 3
        topologyKey: topology.kubernetes.io/zone
        whenUnsatisfiable: ScheduleAnyway
      - maxSkew: 5
        topologyKey: kubernetes.io/hostname
        whenUnsatisfiable: ScheduleAnyway
  - name: InterPodAffinity
    args:
      hardPodAffinityWeight: 1
  - name: VolumeBinding
    args:
      bindTimeoutSeconds: 600
```

### 18.2 A Second Profile (Bin-Packing)

```yaml
- schedulerName: bin-packer
  plugins:
    score:
      enabled:
      - name: NodeResourcesFit
        weight: 5
      - name: ImageLocality
        weight: 1
      disabled:
      - name: NodeResourcesBalancedAllocation       # don't balance, fill nodes
      - name: PodTopologySpread                     # don't spread, pack
  pluginConfig:
  - name: NodeResourcesFit
    args:
      scoringStrategy:
        type: MostAllocated
        resources:
        - name: cpu
          weight: 1
        - name: memory
          weight: 1
```

This profile, used by pods with `spec.schedulerName: bin-packer`, packs onto fullest nodes. Use case: batch jobs in an autoscaled cluster — you want them to crowd onto a few nodes so the unused ones can scale down.

### 18.3 Per-Plugin Args Quirks

Each plugin's `args` is a typed struct under `pkg/scheduler/apis/config`. The `apiVersion` of the args is implied by the parent KubeSchedulerConfiguration version. Some quirks:

- `NodeResourcesFit.scoringStrategy.resources`: if empty, the plugin defaults to `[cpu, memory]` with weight 1 each.
- `PodTopologySpread.defaultingType`: `System` injects cluster-wide default constraints; `List` requires you to provide them explicitly. Default is `System`.
- `DefaultPreemption.minCandidateNodesPercentage / minCandidateNodesAbsolute`: limit how many nodes preemption considers. Larger = better preemption decisions; smaller = faster.
- `InterPodAffinity.hardPodAffinityWeight`: weight to apply to hard affinity terms in scoring (in addition to filtering). Default 1.

---

## 19. Building a Custom Plugin (Out-of-Tree)

The full treatment is chapter 34; this is the orientation.

### 19.1 The Interface

A plugin is a Go type implementing one or more interfaces from `pkg/scheduler/framework`:

```go
type Plugin interface {
    Name() string
}

type FilterPlugin interface {
    Plugin
    Filter(ctx context.Context, state *CycleState, pod *v1.Pod, nodeInfo *NodeInfo) *Status
}

type ScorePlugin interface {
    Plugin
    Score(ctx context.Context, state *CycleState, pod *v1.Pod, nodeName string) (int64, *Status)
    ScoreExtensions() ScoreExtensions  // optional NormalizeScore
}

// And so on for PreFilter, PostFilter, PreScore, Reserve, Permit, PreBind, Bind, PostBind, QueueSort, PreEnqueue.
```

### 19.2 Registering and Deploying

The standard pattern (the upstream `scheduler-plugins` project) is:

1. Implement your plugin(s) as Go code.
2. Fork the kube-scheduler binary; in `cmd/scheduler/main.go`, register your plugin into the framework:
   ```go
   command := app.NewSchedulerCommand(
       app.WithPlugin(mything.Name, mything.New),
   )
   ```
3. Build a new container image.
4. Deploy as a second scheduler with `schedulerName: my-scheduler` (or replace the default if you're brave).
5. Apply a KubeSchedulerConfiguration that enables your plugin in the appropriate extension points.

The `scheduler-plugins` repo provides a curated set of out-of-tree plugins (CapacityScheduling, NodeResourceTopology for NUMA-aware scheduling, PodGroup-based gang scheduling, Trimaran for utilization-aware scoring, etc.) along with build infrastructure for adding your own.

### 19.3 Common Plugin Pitfalls

- **CycleState misuse**: don't share state across pods (different scheduling cycles). The state is per-pod.
- **Mutating shared caches**: never modify the Snapshot or NodeInfo; treat them as read-only.
- **Slow Filter**: a plugin doing 50ms of work per node makes scheduling unusably slow at 5000 nodes (4 minutes per pod just in your plugin).
- **Forgetting Unreserve**: if Reserve allocates external state, Unreserve must reliably release it.
- **Permit timeout pitfall**: a long Permit wait holds resources in cache; design timeouts carefully.

---

## 20. Observability and Alerts

### 20.1 The Core Metrics

| Metric | What it tells you |
| --- | --- |
| `scheduler_scheduling_algorithm_duration_seconds` | time spent in Filter+Score (the "decision" cost) |
| `scheduler_pod_scheduling_duration_seconds` | end-to-end: from pod creation to bind |
| `scheduler_pod_scheduling_sli_duration_seconds` | newer, normalized SLI version |
| `scheduler_pending_pods{queue="active\|backoff\|unschedulable\|gated"}` | queue depths |
| `scheduler_pod_scheduling_attempts` | histogram: how many tries before a pod scheduled (high = lots of backoff) |
| `scheduler_preemption_attempts_total` | count of preemption attempts |
| `scheduler_preemption_victims` | histogram of how many pods were preempted per attempt |
| `scheduler_unschedulable_pods` | pods declared unschedulable |
| `scheduler_framework_extension_point_duration_seconds{extension_point, plugin, profile}` | per-plugin latency |
| `scheduler_plugin_evaluation_total{extension_point, plugin, profile, status}` | per-plugin invocation counts |
| `scheduler_schedule_attempts_total{result}` | scheduled / unschedulable / error |
| `scheduler_goroutines` | scheduler-internal worker count |
| `scheduler_queue_incoming_pods_total{event, queue}` | what's driving pods into queues |

### 20.2 Practical Alerts

```
# Pending pods stuck too long
- alert: SchedulerPendingPodsHigh
  expr: scheduler_pending_pods{queue!="active"} > 50
  for: 5m
  annotations:
    summary: "Many pods cannot be scheduled"
    description: "{{ $value }} pods sitting in non-active queues for >5min"

# Preemption spike (something unhealthy)
- alert: SchedulerPreemptionStorm
  expr: rate(scheduler_preemption_attempts_total[5m]) > 1
  for: 10m
  annotations:
    summary: "Preemption rate above 1/sec for 10 minutes"

# Slow decisions
- alert: SchedulerSlowDecisions
  expr: histogram_quantile(0.99, rate(scheduler_scheduling_algorithm_duration_seconds_bucket[5m])) > 0.5
  for: 10m

# Stuck on gates
- alert: PodsStuckOnGates
  expr: count(kube_pod_spec_scheduling_gates_count > 0) > 0
  for: 30m
```

### 20.3 The `kubectl describe pod` Signal

The single most useful diagnostic surface is the Event log on a Pending pod. The scheduler emits:

```
Type     Reason            From               Message
----     ------            ----               -------
Warning  FailedScheduling  default-scheduler  0/40 nodes are available:
                                              4 node(s) had taint {dedicated: gpu}, that the pod didn't tolerate,
                                              20 node(s) didn't match Pod's node affinity/selector,
                                              16 Insufficient cpu.
```

Read every clause. Each summarizes how many nodes failed which Filter plugin. This message is generated from the per-plugin Status messages combined and deduplicated.

### 20.4 The `nominatedNodeName` Signal

If preemption ran, `pod.status.nominatedNodeName` is set:

```bash
kubectl get pod <name> -o jsonpath='{.status.nominatedNodeName}'
```

That tells you "the scheduler picked this node, kicked some pods off, and will try this node first next time." If the pod still doesn't land there, the nomination got stolen — investigate which pod won.

---

## 21. The Bind Operation (the Only Write)

The Bind extension point writes the pod's node assignment. The default `DefaultBinder` issues:

```
POST /api/v1/namespaces/<ns>/pods/<name>/binding
Content-Type: application/json

{
  "kind": "Binding",
  "apiVersion": "v1",
  "metadata": {
    "name": "<pod-name>",
    "namespace": "<pod-ns>",
    "uid": "<pod-uid>"
  },
  "target": {
    "kind": "Node",
    "name": "<node-name>",
    "apiVersion": "v1"
  }
}
```

The apiserver's `/binding` subresource handler:

1. Validates the request (target node exists; pod exists; pod's `spec.nodeName == ""`).
2. Patches `spec.nodeName = <node-name>`.
3. Writes to etcd.
4. Returns 201 Created.

The patch is observed by:
- The kubelet on the named node (it begins reconciliation).
- The scheduler itself (it removes the pod from "assumed" and treats it as fully bound).
- Other controllers that care about pod placement (workload controllers, endpoint controllers, etc.).

### 21.1 Why `/binding`, Not a Regular PATCH?

The `/binding` subresource is special because:

- It's atomic: only `spec.nodeName` is set; no other fields are touched.
- It has dedicated authorization: an RBAC role can grant `pods/binding` without granting `pods` (so the scheduler can bind without being able to mutate arbitrary fields).
- It enforces invariants: cannot bind a pod that already has `nodeName` set; cannot bind to a non-existent node.

### 21.2 The PodScheduled Condition

Alongside the bind, the scheduler patches:

```yaml
status:
  conditions:
  - type: PodScheduled
    status: "True"
    reason: ""
    lastTransitionTime: ...
```

This condition is the "officially scheduled" signal that other components watch for. If scheduling failed and preemption nominated a node, the condition is:

```yaml
- type: PodScheduled
  status: "False"
  reason: Unschedulable
  message: "0/40 nodes are available: ..."
```

The `Unschedulable` reason is what the autoscaler keys on to decide whether to provision capacity.

---

## 22. Pitfalls and Anti-Patterns

A staff-level guide is incomplete without a list of footguns. Here are the ones we see in production over and over:

### 22.1 Assuming Limits Influence Scheduling

They do not. `requests` is the only thing the scheduler reads. We've seen on-call decks that say "set high limits to get scheduling priority" — entirely wrong.

### 22.2 PodAntiAffinity (hostname) Instead of TopologySpread

Most spread requirements (one replica per node, one per zone) are better served by PodTopologySpread. AntiAffinity at scale costs O(P×N); TopologySpread costs effectively O(1) with precomputed counts.

### 22.3 GPU Requests Without a NodeSelector

A pod with `resources.requests.nvidia.com/gpu: 1` will only land on GPU nodes (no CPU node has that as allocatable). But if your cluster has both labeled and unlabeled GPU nodes (legacy), the lack of nodeSelector means the scheduler may pick a labeled node for *non-GPU* pods, wasting expensive GPU nodes on CPU work.

Always pair GPU requests with a nodeSelector that scopes to GPU nodes. And taint your GPU nodes so non-GPU pods don't accidentally land there.

### 22.4 Huge nodeSelector Cardinality

`nodeSelector: { instance-id: i-0abc... }` (a unique ID) reduces the candidate set to one node. Filter is fine, but Score is meaningless (only one node to score). You lose all the scheduler's intelligence. Use labels with meaningful cardinality: zone, instance-family, GPU model.

### 22.5 `system-cluster-critical` on User Pods

Setting a pod's priorityClass to `system-cluster-critical` (value 2 billion) means it preempts almost everything. Users do this to "make my pod important". The right answer is to define custom PriorityClasses below 10^9. The `system-*` classes are reserved for the control plane.

### 22.6 Tainting Every Node by Mistake

`kubectl taint nodes --all special=true:NoSchedule` taints *every* node. Now nothing without a matching toleration can schedule. We've seen this happen via Ansible playbooks that didn't have proper guards. Verify: `kubectl get nodes -o jsonpath='{.items[*].spec.taints}'` after any taint operation.

### 22.7 Relying on Preemption for Capacity

Preemption frees space by evicting work that was already running. It does not create new capacity. A cluster that's at 100% with no preemptable workloads will simply queue more high-priority pods forever. Capacity comes from the autoscaler, not the scheduler.

### 22.8 Manual `nodeName` in Workloads

Setting `spec.nodeName` skips the scheduler entirely. No taints, no affinity, no resource fit, no topology — none of it. The kubelet will admit or reject. Reserve for: daemon-set-like controllers (which should use a DaemonSet), debug pods (which should use `kubectl debug node/...`), or static pods (managed by kubelet directly via filesystem manifests).

### 22.9 Scheduling Gates Left Behind

A controller crashes and leaves gates in place. Pods sit Pending forever, silently — no `FailedScheduling` event, just nothing. Always alert on `pod.spec.schedulingGates` being non-empty for longer than expected.

### 22.10 Pod Priority Inversion

Setting low priority on long-running batch jobs makes them preemptable. That's fine until the batch jobs *complete deliverables* over their run; getting preempted at hour 47 of 48 destroys 47 hours of work. Pair low priority with checkpointing, or use a higher priority. The scheduler doesn't know about work-in-progress; it just kicks pods out.

### 22.11 `topologyKey` That Some Nodes Lack

If `topologyKey: rack` is set on a spread constraint but only 80% of nodes have a `rack` label, the spread plugin treats the 20% as having no topology. The constraint is silently weaker than intended. Always ensure topology labels are universal on the relevant node set.

### 22.12 Cross-Profile Plugin Conflicts

Running two scheduler profiles that both write to the same shared state (e.g., both modifying a cache via Reserve) can produce subtle bugs. Plugins should treat the framework's `CycleState` as their only mutable state; shared caches should be read-only.

### 22.13 Image Pulls Confused with Scheduling

"Pod is stuck Pending." Check `status.containerStatuses` — if any container shows `ImagePullBackOff`, the pod is **scheduled**, but the kubelet can't pull. The scheduler did its job. Now it's a kubelet/registry/auth problem.

### 22.14 Soft Constraints Treated as Guarantees

`preferredDuringSchedulingIgnoredDuringExecution` is a *hint*. The scheduler will violate it if other constraints conflict. A `preferred` nodeAffinity for zone A does not mean "all my pods in zone A"; it means "prefer zone A when convenient". For guarantees, use `required`.

### 22.15 Watching Filter Failures Without Filter Reasons

The aggregated `FailedScheduling` event collapses per-node reasons into a summary. If a strange Filter plugin is rejecting, you may need verbose logs to see which one (`--v=5` on the scheduler). Don't be afraid to crank verbosity during a debug session.

### 22.16 Default Pod Topology Spread (`defaultingType: System`)

The scheduler ships with cluster-wide default spread constraints in newer versions. They apply to pods that don't declare their own constraints (with some opt-out semantics via `pod.spec.topologySpreadConstraints`). If you observe pods spreading "for no reason", suspect the defaults.

### 22.17 Long PodGroups That Never Form

Gang schedulers (Volcano, custom) hold pods at Permit until the group is complete. If the group never completes (someone misnamed the group label, or some pods got deleted by a flaky controller), the rest sit forever in Permit. Always monitor Permit waits.

### 22.18 Mixing `Tolerate` and Hard `NodeAffinity` Carelessly

A pod that tolerates a taint *can* land on the tainted node, but doesn't *have* to. Tolerations are permission, not direction. Pair with `nodeAffinity` (or `nodeSelector`) to actually direct the pod to the tainted nodes.

### 22.19 Forgetting That Bind Is Async

Just because the scheduler said "bound" doesn't mean the kubelet has acted. There's a watch round-trip (often hundreds of milliseconds at scale, sometimes seconds). Don't assume `spec.nodeName` set ⇒ pod running.

### 22.20 Trusting CPU Throttling to Save You

If two pods both request 1 CPU and both burst to 4 CPU on a 4-core node, both get throttled. The scheduler placed them based on requests; it does not know about burstiness. If your workloads have huge limit/request ratios, expect throttling-induced latency surprises.

---

## 23. TL;DR

**The job.** kube-scheduler watches Pods with `spec.nodeName == ""`, picks a node by running each Pod through the scheduling framework, and writes the choice via `POST /pods/<n>/binding`. It does not run the pod; that's the kubelet's job.

**The architecture.** One scheduling cycle at a time, serial; many binding cycles in parallel. The scheduling cycle picks a node from a snapshot of the cache; the binding cycle does slow side effects (volume bind) and the apiserver write. The split is what keeps the scheduler from blocking on storage.

**The framework.** Every behavior is a plugin at one of ~13 extension points: QueueSort, PreEnqueue, PreFilter, Filter, PostFilter, PreScore, Score, NormalizeScore, Reserve, Permit, PreBind, Bind, PostBind. Built-in plugins implement nodeAffinity, taints, resources, ports, volumes, topology spread, pod affinity, and preemption. Custom plugins compose into the same chain.

**The queue.** Three queues — activeQ (heap by priority), podBackoffQ (heap by retry-after, exponential 1s→10s), unschedulableQ (waiting for cluster events). Most "stuck Pending" mysteries are an unschedulable pod whose wake-up event never comes; the diagnostic is `kubectl describe pod` plus `scheduler_pending_pods{queue=...}`.

**Resources.** NodeResourcesFit filters with `allocatable - sumRequests - podRequests >= 0` for each resource (cpu, memory, ephemeral-storage, hugepages, extended). Limits never matter for scheduling. Init containers contribute via max; sidecar init (1.29+) contribute via sum.

**Affinity vs spread.** NodeAffinity is cheap and powerful. PodAffinity/AntiAffinity is expensive at scale (O(P×N) precompute); use PodTopologySpread instead for spread requirements. Topology spread uses `skew = count(D) - min(counts)`, gated by `maxSkew`.

**Taints.** `NoSchedule` (hard reject), `PreferNoSchedule` (score penalty), `NoExecute` (evict + reject). Built-in taints from the node lifecycle controller: `not-ready`, `unreachable`, `memory-pressure`, `disk-pressure`, `pid-pressure`, `unschedulable`. DaemonSets tolerate them all.

**Priority and preemption.** PriorityClass is an integer; values ≥ 10^9 are reserved for system-critical. Preemption (DefaultPreemption PostFilter plugin) finds the cheapest set of lower-priority pods on a single node to evict, honors PDBs as tiebreakers, sets `nominatedNodeName` for next-attempt preference. It's dry-run / best-effort; eviction frees space but doesn't reserve it.

**Gates (KEP-3521).** `spec.schedulingGates` blocks PreEnqueue until a controller removes the gates. The pattern for queue/quota-based admission (Kueue). The biggest hazard: stuck gates with no signal.

**Profiles.** One scheduler binary can run many profiles, each with its own plugin set and weights, selected by `spec.schedulerName`. Multiple binaries when you need custom Go code; profiles for everything else.

**Bin-pack vs spread.** `NodeResourcesFit.scoringStrategy = MostAllocated` for bin-packing (good with autoscalers), `LeastAllocated` for spread (good for latency), `RequestedToCapacityRatio` for custom shapes.

**Performance.** `percentageOfNodesToScore` (default 50%, min 100) caps Filter cost. Node tree iteration round-robins across zones for fairness. `parallelism: 16` on Filter. At 5000 nodes, healthy clusters do single-digit-ms scheduling cycles; expensive plugins (PodAntiAffinity at scale) blow that up.

**Autoscaler / Karpenter.** Outside the scheduler. They watch pending pods and provision nodes. The scheduler never asks for capacity. Pods pending for non-resource reasons (affinity, taints) don't trigger scale-up.

**Descheduler.** Outside the scheduler. Runs in steady state; evicts pods in bad placements (low-utilized nodes, violating affinity, duplicates) so they're rescheduled with fresh state.

**Gang scheduling.** Default scheduler is bad at it; Permit gate plus a companion controller (or full alternatives like Volcano/Yunikorn/Kueue) make it work.

**Observability.** Track `scheduler_pending_pods` per queue, `scheduler_pod_scheduling_duration_seconds`, `scheduler_preemption_attempts_total`, `scheduler_framework_extension_point_duration_seconds`. Alert on pending pods > N for >5min, preemption rate spikes, p99 scheduling latency.

**Pitfalls.** Limits don't affect scheduling. PodAntiAffinity at hostname is slow. GPU requests without nodeSelector waste capacity. System priorities on user pods preempt the world. Manual `nodeName` bypasses everything. Scheduling gates without monitoring silently strand pods. Soft constraints are hints, not promises.

**The mental model.** Treat the scheduler as a planner with the same shape as a query engine: a sequential pipeline that takes a request and a snapshot, runs filter then score, and commits once. Everything you know about planners — locality of state, snapshot semantics, parallel inner loops, cost models, the importance of stable observability — applies here. Most production scheduler problems are not scheduler bugs; they're misconfiguration that violates one of those properties. Read the events, read the metrics, and assume the scheduler is doing exactly what its configuration told it to do.
