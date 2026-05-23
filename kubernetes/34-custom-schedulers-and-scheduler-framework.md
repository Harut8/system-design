# Custom Schedulers and the Scheduler Framework: A Deep Dive

How to extend, replace, or compose Kubernetes scheduling for workloads the default scheduler was never designed to handle: gang-scheduled training jobs, multi-tenant batch queues, NUMA-aware HPC, cost-aware spot fleets, and time-windowed analytics. This chapter assumes Chapter 09 (kube-scheduler internals) and builds on it. Where 09 explained *how* the framework executes a single pod through Filter and Score, this chapter explains *how to put your own code into that pipeline*, *what production projects (Volcano, YuniKorn, Kueue, scheduler-plugins) actually do*, and *which extension surface to choose for which workload*. It is staff-level: we read framework interfaces, KubeSchedulerConfiguration YAML, PodGroup CRDs, and the queue trees of YuniKorn, and we close on the operational reality of running more than one scheduler in a cluster.

---

## Table of Contents

1.  [When the Default Scheduler Isn't Enough](#1-when-the-default-scheduler-isnt-enough)
2.  [Three Ways to Extend Scheduling](#2-three-ways-to-extend-scheduling)
3.  [The Scheduler Framework, Re-examined](#3-the-scheduler-framework-re-examined)
4.  [Building a Plugin in Go](#4-building-a-plugin-in-go)
5.  [The scheduler-plugins Project (kubernetes-sigs)](#5-the-scheduler-plugins-project-kubernetes-sigs)
6.  [CycleState: Per-Cycle Plugin Memory](#6-cyclestate-per-cycle-plugin-memory)
7.  [Permit, Wait, Approve, Deny — the Gang Mechanism](#7-permit-wait-approve-deny--the-gang-mechanism)
8.  [Gang Scheduling Implementations](#8-gang-scheduling-implementations)
9.  [Volcano Deep Look](#9-volcano-deep-look)
10. [Apache YuniKorn Deep Look](#10-apache-yunikorn-deep-look)
11. [Multi-Scheduler Deployments](#11-multi-scheduler-deployments)
12. [KubeSchedulerConfiguration and Profiles](#12-kubeschedulerconfiguration-and-profiles)
13. [Capacity Scheduling and Hierarchical Quotas](#13-capacity-scheduling-and-hierarchical-quotas)
14. [NUMA-Aware Scheduling (node-resource-topology)](#14-numa-aware-scheduling-node-resource-topology)
15. [Network-Topology Scheduling (Trimaran)](#15-network-topology-scheduling-trimaran)
16. [Cost-Aware Scheduling](#16-cost-aware-scheduling)
17. [Topology Spread with Custom Topology Keys](#17-topology-spread-with-custom-topology-keys)
18. [Time-Windowed Scheduling via Scheduling Gates](#18-time-windowed-scheduling-via-scheduling-gates)
19. [External Schedulers (YuniKorn, Poseidon, and the kubelet contract)](#19-external-schedulers-yunikorn-poseidon-and-the-kubelet-contract)
20. [Scheduler Extender (the Pre-Framework Webhook)](#20-scheduler-extender-the-pre-framework-webhook)
21. [Kueue: Admission-Time Queueing](#21-kueue-admission-time-queueing)
22. [Kueue Deep Look — Workload, Cohort, ResourceFlavor](#22-kueue-deep-look--workload-cohort-resourceflavor)
23. [The Scheduler-Aware Operator Pattern](#23-the-scheduler-aware-operator-pattern)
24. [Dominant Resource Fairness (DRF)](#24-dominant-resource-fairness-drf)
25. [Bin-Packing vs Spread Strategies](#25-bin-packing-vs-spread-strategies)
26. [Karpenter as a Scheduler (Sort Of)](#26-karpenter-as-a-scheduler-sort-of)
27. [Custom Plugin Pitfalls](#27-custom-plugin-pitfalls)
28. [Testing Custom Plugins](#28-testing-custom-plugins)
29. [Versioning Your Scheduler Binary](#29-versioning-your-scheduler-binary)
30. [Replace / Multi / Plugin — the Decision](#30-replace--multi--plugin--the-decision)
31. [Real-World Deployments](#31-real-world-deployments)
32. [Operating Multiple Schedulers](#32-operating-multiple-schedulers)
33. [Pitfalls](#33-pitfalls)

---

## TL;DR

The default kube-scheduler is one pod at a time, online, greedy, single-cluster. Real workloads need gangs (all-or-none), queues (multi-tenant fairness), topology (NUMA/network), cost (spot mixes), and time-of-day windows. Kubernetes gives you four extension surfaces — multiple schedulers, scheduler-framework plugins, scheduling gates, and (legacy) the scheduler extender HTTP webhook. Plus an *adjacent* surface — admission-time queueing via Kueue — that doesn't extend the scheduler at all, it gates pods before they reach it.

You almost never replace the whole scheduler. The decision tree is: *want gang or queues for batch?* → Volcano or YuniKorn (separate binary, scoped via `spec.schedulerName`) or Kueue (admission gating + default scheduler). *Want topology-aware decisions?* → scheduler-plugins NRT plugin compiled into your kube-scheduler image. *Want time-of-day or async business logic?* → scheduling gates + your controller. *Want a custom score function?* → write a Score plugin and ship a custom kube-scheduler image. Replacing kube-scheduler outright is what YuniKorn does and is what Karpenter approximates in user-space; both have years of investment behind them. You won't.

The framework is a pipeline: QueueSort → PreEnqueue → PreFilter → Filter → PostFilter → PreScore → Score → NormalizeScore → Reserve → Permit → PreBind → Bind → PostBind. Plugins implement one or more of these interfaces and register through a profile in `KubeSchedulerConfiguration`. CycleState is the per-cycle scratchpad that lets PreFilter cache data for Filter. Permit is the magic — it can WAIT, holding a pod until another controller approves or denies, which is how every gang scheduler in Kubernetes works under the hood.

The 80% rule: most "we need a custom scheduler" requests are actually "we need PriorityClass + ResourceQuota + topology spread + Karpenter". The 20% that genuinely need it is HPC, ML training, multi-tenant analytics. Recognize which one you're in before you start writing Go.

---

## 1. When the Default Scheduler Isn't Enough

The default kube-scheduler is good. After a decade of investment it does NodeAffinity, PodAffinity/AntiAffinity, TopologySpreadConstraints, TaintToleration, NodeResourcesFit (with bin-pack or spread scoring), VolumeBinding, ImageLocality, InterPodAffinity, PodTopologySpread, and PreEnqueue gating. For 90% of stateless web workloads, you do not need anything else. The remaining 10% — the workloads that *actually* drive the scheduler-extension industry — share a small number of properties.

### 1.1 Gang Scheduling: All-or-None Admission

Spark, MPI, Horovod, DeepSpeed, Ray, and any synchronous distributed training framework needs **N pods running simultaneously or none of them.** The default scheduler is one-pod-at-a-time. It will happily admit 7 of your 8 workers, leaving 7 pods burning GPU memory waiting for the 8th to schedule. Worst case it never schedules — the 7 admitted pods occupy resource that pod 8 needs, deadlock. Spark's driver eventually times out and exits, killing the whole job.

What you want: "schedule all 8 or schedule none, and don't hold partial reservations." This is **gang scheduling**, sometimes called **coscheduling**. It requires either a separate scheduler that batches admission decisions (Volcano, YuniKorn), a Permit plugin that holds pods in a waiting room until the gang is complete (scheduler-plugins coscheduling), or an admission-time queue that releases the entire gang together (Kueue).

### 1.2 Batch Fairness Across Tenants

A shared analytics cluster with five teams, each running Spark/Flink jobs. Team A submits 1000 pods at midnight. Team B submits 50 pods at 1am. With first-come-first-served scheduling, team B waits hours. With **dominant resource fairness (DRF)**, the scheduler interleaves admissions so each tenant makes progress proportional to their fair share. This is built into YuniKorn and Volcano. The default scheduler has no concept of tenants or fair-share — `PriorityClass` is a single global ordering, not a queue tree.

### 1.3 Topology-Aware Placement

Modern compute hardware is non-uniform. A node may have:
- Two CPU sockets with their own memory controllers (NUMA nodes).
- 8 GPUs split across PCIe topology — some pairs are connected via NVLink (fast), others must go over the PCIe root (slow).
- A NIC bound to one socket (cross-socket DMA is expensive).
- An SR-IOV VF that lives on a particular PF.

A pod that uses 4 CPUs + 16 GiB memory + 1 GPU runs *much* better if all of those are on the same NUMA node. The default scheduler picks nodes by total free CPU and total free memory; it doesn't see NUMA. The `topology manager` on the kubelet can refuse a pod after binding (causing a TopologyAffinityError and a reschedule), but that's expensive — you've already paid the bind, image pull is in-flight, and the kubelet rejects. Better: the **scheduler** should know that node-7 has 4 free CPUs on NUMA0 with a free GPU on NUMA0, and node-8 has 4 free CPUs but the only free GPU is on NUMA1. Choose node-7.

That's what the `node-resource-topology` scheduler-plugins plugin does, via the `NodeResourceTopology` CRD.

### 1.4 Cost-Aware Placement

In cloud you pay differently for different instance families. Spot/preemptible nodes are cheap but unreliable. The default scheduler treats nodes as fungible (modulo labels and taints). If you want "prefer spot when SLA permits, fall back to on-demand", you either:
- Use Karpenter, which scales nodes based on pending pod requirements and chooses cheap instances by default.
- Write a Score plugin that reads `node.metadata.labels["node.kubernetes.io/instance-type"]` and a cost table, scoring cheaper nodes higher.
- Use `nodeAffinity` weighted preferences as a coarse approximation.

The plugin approach is more flexible — you can score on combined CPU price + memory price + spot probability + carbon intensity.

### 1.5 Custom Resources: GPU Models, FPGA Bitstreams, Slots

`nvidia.com/gpu: 1` says "I want one GPU". It doesn't say *which* GPU. A100 vs H100 vs L4 are wildly different. A model trained on an A100 may not fit on an L4 because of memory. The device plugin can advertise these as `nvidia.com/gpu-h100`, but if the workload is happy with either H100 or A100 (just not L4), the default scheduler can't express "this or that". Custom plugins can: a Filter that consults a compatibility map, or a Score plugin that prefers the cheaper compatible model.

FPGA bitstreams need similar logic — the FPGA must be programmed with a specific bitstream before the pod can use it. A custom scheduler can co-bind a "program-this-bitstream" job ahead of the workload pod, or filter to nodes that already have the right bitstream loaded.

Slot scheduling (one slot per accelerator, several slots per node, slot has fungible-with-restrictions semantics) is a similar pattern, used heavily by Tencent, ByteDance, and others.

### 1.6 Time-of-Day / Window Scheduling

Batch ML jobs only allowed during off-peak hours (00:00–06:00 in the cluster's region). Default scheduler has no clock-awareness. Solution: use **scheduling gates** — the pod is created with `spec.schedulingGates: [{name: "off-peak-only"}]`, and a controller clears the gate at midnight and re-adds it at 6am for pods that haven't been scheduled.

This pattern generalizes: any *asynchronous prerequisite* (a license token, a data preload, a cross-cluster lease) maps onto a scheduling gate cleared by a controller. The default scheduler is untouched — it just sees an unschedulable pod until the gate clears, at which point the pod re-enters the active queue.

### 1.7 The 80/20

Before you build anything, check whether the requirement is really one of:
- `PriorityClass` for cross-workload precedence.
- `PodTopologySpread` for "spread across zones/racks".
- `nodeAffinity` for "this hardware family".
- `PodAffinity/AntiAffinity` for "same/different host".
- `Karpenter` for "the right node type appears just in time".
- `ResourceQuota + LimitRange` for "this team can't take the whole cluster".

If all five aren't enough, *now* consider the rest of this chapter.

---

## 2. Three Ways to Extend Scheduling

There are three orthogonal ways to put your scheduling logic into a Kubernetes cluster. They compose: you can run a plugin inside a custom scheduler binary that you deploy alongside the default, while also using scheduling gates. Most real systems do.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  THE THREE EXTENSION SURFACES                                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  1) MULTIPLE SCHEDULERS                                                 │
│  ┌────────────────────────┐  ┌────────────────────────┐                │
│  │ kube-scheduler         │  │ my-custom-scheduler    │                │
│  │ (default profile)      │  │ (your binary)          │                │
│  │ watches pods where     │  │ watches pods where     │                │
│  │ schedulerName=         │  │ schedulerName=         │                │
│  │  "default-scheduler"   │  │  "my-custom-scheduler" │                │
│  └────────────────────────┘  └────────────────────────┘                │
│           │                              │                              │
│           └──── both watch all nodes ────┘                              │
│  Pod opts in:  spec.schedulerName: my-custom-scheduler                  │
│                                                                         │
│  2) SCHEDULER FRAMEWORK PLUGIN  (inside kube-scheduler)                 │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │ kube-scheduler binary                                            │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │  │
│  │  │ NodeAffinity │→ │ Filter       │→ │ YOUR PLUGIN  │ ← here    │  │
│  │  │ (built-in)   │  │ NodePorts    │  │ at Filter    │           │  │
│  │  └──────────────┘  └──────────────┘  └──────────────┘           │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│  You compile your code into a kube-scheduler image; same binary as     │
│  upstream plus your plugin. Single watch, single cache.                 │
│                                                                         │
│  3) SCHEDULING GATES  (asynchronous bookkeeping)                        │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │ Pod created with spec.schedulingGates: [{name: "wait-for-X"}]   │  │
│  │       ↓                                                          │  │
│  │ Default scheduler ignores it (PreEnqueue refuses to admit)      │  │
│  │       ↓                                                          │  │
│  │ Your controller does the bookkeeping (license check, gang ready)│  │
│  │       ↓                                                          │  │
│  │ Controller PATCHes pod, removing the gate from spec             │  │
│  │       ↓                                                          │  │
│  │ Default scheduler picks it up, runs the full pipeline           │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│  No custom scheduler. The default scheduler does the heavy lifting.    │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Multiple Schedulers

Run two or more scheduler binaries in the cluster. Each is a normal Deployment in `kube-system`. Each watches *all* nodes (it needs to see the topology) but only *pods that opt in by `spec.schedulerName`*. You as the developer pick a name (`my-batch-scheduler`) and configure pods to request that scheduler. The default scheduler ignores pods whose `schedulerName` doesn't match its own profile.

Pros:
- **Total isolation.** Bugs in your scheduler can't break the default scheduler's pods.
- **Independent release cycle.** You can deploy v0.1-alpha while production stays on the stable default.
- **Different code.** You can fork the default scheduler heavily, swap CycleState behavior, or implement your own.

Cons:
- **Two watch caches.** Both schedulers list+watch all nodes, all pods. At 10k nodes that's measurable apiserver load.
- **Two leader elections.** Each scheduler needs its own lease (different `leaderElection.resourceName`).
- **Two binaries to operate.** Metrics, alerts, dashboards, version skew.
- **Race on node resources.** Both schedulers see the same node free capacity. Scheduler A binds pod X to node-1; scheduler B's snapshot is stale for ~1s; scheduler B binds pod Y assuming node-1 still has the capacity. The kubelet rejects Y, scheduler B retries. Eventually consistent, but visible in scheduling latency tails.

Volcano and YuniKorn are typically deployed this way: run alongside the default scheduler, pods opt in via `schedulerName: volcano` or `schedulerName: yunikorn`.

### 2.2 Scheduler Framework Plugin

A plugin is Go code that implements one or more framework interfaces (`FilterPlugin`, `ScorePlugin`, `PermitPlugin`, ...) and registers them with the scheduler at startup. You compile it into a kube-scheduler binary (or a fork) and ship the resulting image.

Pros:
- **One binary, one cache.** No duplicated watch traffic.
- **Highest fidelity.** Your plugin sees the same CycleState and NodeInfo cache as built-in plugins — no stale data, no separate snapshot.
- **Best performance.** No HTTP round-trip (unlike Extender).
- **All extension points available.** You can hook into QueueSort, PreEnqueue, Permit, etc. — surfaces not exposed via Extender.

Cons:
- **Single binary, single blast radius.** A panic in your Filter takes down the scheduler for *all* pods, not just yours.
- **Version skew.** Framework API is internal; it changes between minor releases. You must rebuild for each Kubernetes upgrade.
- **Plugin or no plugin, the binary is the kube-scheduler binary.** You can't run your plugin alongside upstream without forking the binary.

Most production "custom" schedulers are this: kube-scheduler + scheduler-plugins + your in-house plugin, all compiled together.

### 2.3 Scheduling Gates

Scheduling gates (KEP-3521, GA in 1.30) let a pod declare *named prerequisites* that must be cleared before the scheduler even tries to schedule it. They are first-class fields on the PodSpec:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gated-pod
spec:
  schedulingGates:
    - name: example.com/license-check
    - name: example.com/off-peak-window
  containers:
    - name: app
      image: nginx
```

While `schedulingGates` is non-empty, the pod's `status.conditions["PodScheduled"]` is `False` with reason `SchedulingGated`, and the scheduler refuses to consider it (the framework's PreEnqueue check fails). Your controller observes the pod, performs the asynchronous work, then **removes the gate** with a strategic merge patch:

```yaml
spec:
  schedulingGates:
    - name: example.com/off-peak-window   # license-check removed
```

When the array reaches empty, PreEnqueue passes, the scheduler picks up the pod.

Key constraint: **gates are immutable once removed.** You cannot re-add a gate to a pod. This is by design — a pod that becomes schedulable must not be made unschedulable again by an external actor, or scheduling becomes unbounded.

Pros:
- **Uses the default scheduler.** Your controller only does the bookkeeping; the scheduler does scheduling.
- **Simple model.** A gate is a string; clearing is one PATCH.
- **Composes with everything.** Volcano, Karpenter, Kueue, plain Deployment — all work with gates.

Cons:
- **One-shot.** Cannot re-gate.
- **Spec-level field.** Gates live in PodSpec, which is immutable for most fields; gates are explicitly excluded from immutability rules but the surface is awkward.
- **Doesn't help with admission once unschedulable.** If your pod fails scheduling for capacity reasons after the gate clears, you have to wait for capacity like any other pod.

Kueue uses gates for some flows. Volcano's PodGroup admission is similar in spirit (the PodGroup holds until ready), though implemented at a different layer.

---

## 3. The Scheduler Framework, Re-examined

Chapter 09 walked through the pipeline once. Here we re-examine each extension point with an eye toward *plugin authoring*: which interface to implement, what's safe to do, what blocks the cycle.

```
┌────────────────────────────────────────────────────────────────────────┐
│                  SCHEDULER FRAMEWORK PIPELINE                          │
│                  (with custom plugin slots highlighted)                │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│   ┌──────────────────────┐                                             │
│   │   activeQ (heap)     │   QueueSort      ─── exactly one            │
│   │   sorted by priority │ ◀─ plugin defines Less(p1,p2)              │
│   └─────────┬────────────┘                                             │
│             │ pop next pod                                             │
│             ▼                                                          │
│   ┌──────────────────────┐                                             │
│   │  PreEnqueue          │   "is this pod admissible yet?"             │
│   │  (gate / quota check)│   ◀── checks schedulingGates here          │
│   └─────────┬────────────┘                                             │
│             │ admitted to scheduling cycle                             │
│             ▼                                                          │
│   ┌──────────────────────┐  ◀─── per cycle: serial                    │
│   │ PreFilter            │   compute pod summary (resources, affin,    │
│   │ • write CycleState   │   topology terms). Cache in CycleState.     │
│   └─────────┬────────────┘                                             │
│             │ for each node in snapshot, in parallel                   │
│             ▼                                                          │
│   ┌──────────────────────┐                                             │
│   │ Filter (per-node)    │   each plugin returns Success / Unsched /   │
│   │ • read CycleState    │   Error. Any Unsched → node infeasible.    │
│   └─────────┬────────────┘                                             │
│             │ if zero feasible nodes                                   │
│             ▼                                                          │
│   ┌──────────────────────┐                                             │
│   │ PostFilter           │   preemption lives here                     │
│   │ (default: dryrun     │   (DefaultPreemption plugin)                │
│   │  preemption)         │                                             │
│   └─────────┬────────────┘                                             │
│             │ if feasible set ≥ 1                                      │
│             ▼                                                          │
│   ┌──────────────────────┐                                             │
│   │ PreScore             │   precompute scoring inputs                 │
│   └─────────┬────────────┘                                             │
│             ▼                                                          │
│   ┌──────────────────────┐                                             │
│   │ Score (per-node)     │   return 0..100 (MaxNodeScore)              │
│   │ • parallel across    │   multiple plugins → sum × weight           │
│   │   feasible nodes     │                                             │
│   └─────────┬────────────┘                                             │
│             ▼                                                          │
│   ┌──────────────────────┐                                             │
│   │ NormalizeScore       │   if your score range is skewed, normalize  │
│   └─────────┬────────────┘                                             │
│             ▼                                                          │
│   ┌──────────────────────┐                                             │
│   │ Reserve              │   "I'm going to assume this pod is on       │
│   │ (or Unreserve on fail)│   node N for cache purposes"                │
│   └─────────┬────────────┘                                             │
│             ▼                                                          │
│   ┌──────────────────────┐                                             │
│   │ Permit               │   return Success / Wait / Reject            │
│   │ • Wait blocks until  │   gang scheduling uses Wait here            │
│   │   another actor      │   (timeout configurable)                    │
│   │   approves           │                                             │
│   └─────────┬────────────┘                                             │
│             │ released — binding cycle (parallel across pods)          │
│             ▼                                                          │
│   ┌──────────────────────┐                                             │
│   │ PreBind              │   volume bind, attach, mount preconditions  │
│   └─────────┬────────────┘                                             │
│             ▼                                                          │
│   ┌──────────────────────┐                                             │
│   │ Bind                 │   PATCH spec.nodeName                       │
│   └─────────┬────────────┘                                             │
│             ▼                                                          │
│   ┌──────────────────────┐                                             │
│   │ PostBind             │   metrics, audit, post-bind controller hint │
│   └──────────────────────┘                                             │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Extension Points and Their Interfaces

The Go interfaces live at `k8s.io/kubernetes/pkg/scheduler/framework/interface.go`. Abbreviated:

| Extension Point | Interface | Returns | Called |
|---|---|---|---|
| QueueSort | `QueueSortPlugin` | `Less(p1, p2 *QueuedPodInfo) bool` | when ordering activeQ |
| PreEnqueue | `PreEnqueuePlugin` | `PreEnqueue(ctx, pod) *Status` | before adding to activeQ |
| PreFilter | `PreFilterPlugin` | `PreFilter(ctx, state, pod) (*PreFilterResult, *Status)` | once per scheduling cycle |
| Filter | `FilterPlugin` | `Filter(ctx, state, pod, nodeInfo) *Status` | per (pod, node) |
| PostFilter | `PostFilterPlugin` | `PostFilter(ctx, state, pod, filteredNodeStatusMap) (*PostFilterResult, *Status)` | when zero feasible |
| PreScore | `PreScorePlugin` | `PreScore(ctx, state, pod, nodes) *Status` | once per cycle, after filter |
| Score | `ScorePlugin` | `Score(ctx, state, pod, nodeName) (int64, *Status)` | per (pod, feasible-node) |
| ScoreExtensions | `ScoreExtensions` | `NormalizeScore(ctx, state, pod, scores) *Status` | after all Score calls |
| Reserve | `ReservePlugin` | `Reserve(ctx, state, pod, nodeName) *Status` + `Unreserve(...)` | once chosen node |
| Permit | `PermitPlugin` | `Permit(ctx, state, pod, nodeName) (*Status, time.Duration)` | after reserve |
| PreBind | `PreBindPlugin` | `PreBind(ctx, state, pod, nodeName) *Status` | binding cycle |
| Bind | `BindPlugin` | `Bind(ctx, state, pod, nodeName) *Status` | binding cycle |
| PostBind | `PostBindPlugin` | `PostBind(ctx, state, pod, nodeName)` | after successful bind |

A single Go type can implement multiple interfaces, e.g., scheduler-plugins' `Coscheduling` plugin implements PreFilter, PostFilter, PreEnqueue, Permit, and Reserve all together.

### 3.2 Status and Codes

Plugin methods return `*framework.Status` with a code:

| Code | Meaning | Effect |
|---|---|---|
| `Success` | OK | continue |
| `Error` | internal error | abort cycle, retry pod |
| `Unschedulable` | node/pod combination won't work, no preemption | this node out of feasible set |
| `UnschedulableAndUnresolvable` | won't work even with preemption | preemption skipped for this node |
| `Wait` (only from Permit) | hold the pod | bind blocked until WaitForPermit approves |
| `Skip` | plugin doesn't apply | proceeds as if successful |

Distinguishing `Unschedulable` vs `UnschedulableAndUnresolvable` matters — if your Filter knows preemption can't help (e.g., the node lacks a required hardware feature), return `UnschedulableAndUnresolvable` to short-circuit the preemption cycle.

### 3.3 Where to Put Logic

Decision table for new plugin authors:

| You want to... | Use point |
|---|---|
| Refuse a pod entirely on the queue side | PreEnqueue |
| Order pods relative to each other in the queue | QueueSort |
| Eliminate a node from consideration | Filter |
| Bias selection toward a node | Score |
| Run preemption logic | PostFilter |
| Block a pod waiting for sibling pods | Permit |
| Do work before binding (volumes, leases) | PreBind |
| Replace bind itself (e.g., bind to off-cluster runtime) | Bind |
| Write metrics or post-bind notifications | PostBind |

**Avoid:** putting heavy logic in Filter. Filter is called *per node, per cycle*. With 5000 nodes and a 50 µs Filter, that's 250 ms per scheduling decision — your throughput collapses. Move precomputation to PreFilter and cache in CycleState.

---

## 4. Building a Plugin in Go

A minimal example: a Filter plugin that rejects nodes labeled `maintenance=true` unless the pod has toleration `maintenance-ok=true`. (Yes, taints would do this — we use it because the structure is illustrative.)

### 4.1 The Plugin

```go
// pkg/plugins/maintenancefilter/plugin.go
package maintenancefilter

import (
    "context"

    v1 "k8s.io/api/core/v1"
    "k8s.io/apimachinery/pkg/runtime"
    "k8s.io/kubernetes/pkg/scheduler/framework"
)

const Name = "MaintenanceFilter"

type MaintenanceFilter struct {
    handle framework.Handle
}

// Ensure the type implements the FilterPlugin interface.
var _ framework.FilterPlugin = &MaintenanceFilter{}

// Name returns the plugin name, must match the YAML config.
func (m *MaintenanceFilter) Name() string { return Name }

// New is the factory function the framework calls at startup.
func New(_ context.Context, _ runtime.Object, h framework.Handle) (framework.Plugin, error) {
    return &MaintenanceFilter{handle: h}, nil
}

// Filter is called once per (pod, node) pair.
func (m *MaintenanceFilter) Filter(
    ctx context.Context,
    state *framework.CycleState,
    pod *v1.Pod,
    nodeInfo *framework.NodeInfo,
) *framework.Status {
    node := nodeInfo.Node()
    if node == nil {
        return framework.NewStatus(framework.Error, "node is nil")
    }

    if node.Labels["maintenance"] != "true" {
        return framework.NewStatus(framework.Success)
    }

    // Maintenance mode. Allow only if the pod tolerates it.
    for _, t := range pod.Spec.Tolerations {
        if t.Key == "maintenance-ok" && t.Value == "true" {
            return framework.NewStatus(framework.Success)
        }
    }

    return framework.NewStatus(
        framework.UnschedulableAndUnresolvable,
        "node is in maintenance and pod does not tolerate it",
    )
}
```

### 4.2 The Main Function

`cmd/scheduler/main.go` boots the scheduler with our plugin registered:

```go
package main

import (
    "os"

    "k8s.io/component-base/cli"
    _ "k8s.io/component-base/logs/json/register"
    "k8s.io/kubernetes/cmd/kube-scheduler/app"

    "example.com/pkg/plugins/maintenancefilter"
)

func main() {
    // app.NewSchedulerCommand returns the kube-scheduler cobra command;
    // we pass in our plugin's New factory paired with its registered name.
    command := app.NewSchedulerCommand(
        app.WithPlugin(maintenancefilter.Name, maintenancefilter.New),
    )
    code := cli.Run(command)
    os.Exit(code)
}
```

This is the same `app.NewSchedulerCommand` that the upstream `kube-scheduler` binary uses — we are linking to it and adding our plugin. Build:

```bash
go build -o my-scheduler ./cmd/scheduler
```

`my-scheduler` is now a fully functional kube-scheduler with one extra plugin available.

### 4.3 The KubeSchedulerConfiguration

The plugin must be enabled in a profile:

```yaml
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
leaderElection:
  leaderElect: true
  resourceName: my-scheduler        # MUST differ from default's "kube-scheduler"
  resourceNamespace: kube-system
profiles:
  - schedulerName: my-scheduler
    plugins:
      filter:
        enabled:
          - name: MaintenanceFilter
```

Pass to the binary: `--config /etc/kubernetes/my-scheduler-config.yaml`.

A pod opts in:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: regular-app
spec:
  schedulerName: my-scheduler
  containers:
    - name: app
      image: nginx
```

If you don't set `schedulerName`, the pod is picked up by the **default-scheduler** profile (which is in the same binary if you also defined it in the same config) — see §12 for multi-profile configs.

### 4.4 Where the Built-In Plugin Code Lives

For reference when reading and writing plugins:

- `k8s.io/kubernetes/pkg/scheduler/framework/plugins/`
  - `noderesources/` — NodeResourcesFit, BalancedAllocation, LeastAllocated, MostAllocated
  - `nodeaffinity/` — required + preferred affinity
  - `interpodaffinity/`
  - `volumebinding/`
  - `tainttoleration/`
  - `podtopologyspread/`
  - `imagelocality/`
  - `defaultbinder/` — the bind plugin
  - `defaultpreemption/` — the PostFilter that does preemption
- `k8s.io/kubernetes/pkg/scheduler/framework/runtime/framework.go` — the runtime that calls plugins
- `k8s.io/kubernetes/pkg/scheduler/internal/queue/scheduling_queue.go` — activeQ implementation

Reading the volumebinding plugin's PreFilter→Filter→PreBind sequence is the canonical example of "use CycleState to pass data between phases."

---

## 5. The scheduler-plugins Project (kubernetes-sigs)

`github.com/kubernetes-sigs/scheduler-plugins` is the out-of-tree home for reference plugins that don't fit into the in-tree codebase but are widely useful. SIG-Scheduling maintains it; the plugins track the latest k8s release.

Available plugins (as of recent releases):

| Plugin | Purpose | Source path |
|---|---|---|
| `Coscheduling` | gang scheduling via PodGroup CRD | `pkg/coscheduling` |
| `CapacityScheduling` | hierarchical quotas with preemption | `pkg/capacityscheduling` |
| `NodeResourceTopology` | NUMA-aware via NRT CRD | `pkg/noderesourcetopology` |
| `Trimaran` (LowRiskOverCommit, LoadVariationRiskBalancing, TargetLoadPacking) | network/load-aware scoring | `pkg/trimaran` |
| `NetworkAware` | network-topology-aware via service graph | `pkg/networkaware` |
| `PreemptionToleration` | priority class with toleration | `pkg/preemptiontoleration` |
| `Sysched` | system-call-pattern-aware co-scheduling | `pkg/sysched` |
| `NodeResourcesAllocatable` | scoring by allocatable rather than capacity | `pkg/noderesources` |

The repository ships a `kube-scheduler` Docker image that has all these plugins compiled in. To use it, you write a KubeSchedulerConfiguration that enables the plugins you want, mount it as a ConfigMap, and run the scheduler-plugins image instead of the upstream kube-scheduler image. Same binary contract, more plugins available.

Example enabling Coscheduling and NodeResourceTopology:

```yaml
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
profiles:
  - schedulerName: scheduler-plugins-scheduler
    plugins:
      multiPoint:
        enabled:
          - name: Coscheduling
          - name: NodeResourceTopologyMatch
            weight: 5
        disabled:
          # Coscheduling provides its own QueueSort; disable the default one
          - name: PrioritySort
    pluginConfig:
      - name: Coscheduling
        args:
          permitWaitingTimeSeconds: 60
      - name: NodeResourceTopologyMatch
        args:
          scoringStrategy:
            type: LeastNUMANodes
```

`multiPoint` is a shortcut that enables/disables a plugin at *all* extension points it implements, instead of listing it under each (filter, score, ...) separately. Coscheduling implements PreEnqueue+PreFilter+PostFilter+Reserve+Permit; without `multiPoint` you'd list it five times.

---

## 6. CycleState: Per-Cycle Plugin Memory

Every scheduling cycle creates a fresh `CycleState`, a string→`StateData` map scoped to one pod's pass through the pipeline. It is the only safe place for a plugin to pass data between its own extension points (e.g., PreFilter → Filter → PostFilter → Reserve).

### 6.1 The API

```go
type CycleState struct {
    storage    map[string]StateData
    recordPluginMetrics bool
    SkipFilterPlugins   sets.Set[string]
    SkipScorePlugins    sets.Set[string]
}

type StateData interface {
    Clone() StateData    // must deep-copy
}

func (c *CycleState) Read(key StateKey) (StateData, error)
func (c *CycleState) Write(key StateKey, val StateData)
func (c *CycleState) Delete(key StateKey)
```

The state is **per-pod, per-cycle.** It is discarded when the cycle ends — successful bind, error, or unschedulable. A new cycle (for the next pod, or for this pod's next retry) gets a fresh CycleState.

### 6.2 A Real Use: VolumeBinding

The volume binding plugin needs to figure out which PVCs the pod has, which are unbound, and what node-feasibility constraints each unbound PVC implies. That computation is expensive (it walks PV/PVC objects, evaluates storage class topology). The plugin does it once in PreFilter:

```go
// pkg/scheduler/framework/plugins/volumebinding/volume_binding.go (abridged)

type stateData struct {
    podVolumeClaims     *PodVolumeClaims
    podVolumesByNode    map[string]*PodVolumes
    sync.Mutex
}

func (d *stateData) Clone() framework.StateData { return d }

const stateKey = "PreFilter" + Name  // namespaced key

func (p *VolumeBinding) PreFilter(
    ctx context.Context, cs *framework.CycleState, pod *v1.Pod,
) (*framework.PreFilterResult, *framework.Status) {
    claims, err := p.podVolumeClaims(pod)
    if err != nil { /* ... */ }
    cs.Write(stateKey, &stateData{podVolumeClaims: claims})
    return nil, framework.NewStatus(framework.Success)
}

func (p *VolumeBinding) Filter(
    ctx context.Context, cs *framework.CycleState, pod *v1.Pod, ni *framework.NodeInfo,
) *framework.Status {
    raw, err := cs.Read(stateKey)
    if err != nil { /* PreFilter must run first */ }
    state := raw.(*stateData)
    podVolumes, reasons, err := p.binder.FindPodVolumes(pod, state.podVolumeClaims, ni.Node())
    // ...
    state.Lock()
    state.podVolumesByNode[ni.Node().Name] = podVolumes
    state.Unlock()
    return nil
}

func (p *VolumeBinding) PreBind(
    ctx context.Context, cs *framework.CycleState, pod *v1.Pod, nodeName string,
) *framework.Status {
    raw, _ := cs.Read(stateKey)
    state := raw.(*stateData)
    podVolumes := state.podVolumesByNode[nodeName]
    return p.binder.BindPodVolumes(ctx, pod, podVolumes)
}
```

Three uses of CycleState across PreFilter, Filter, and PreBind. The key namespacing convention (`stateKey = "PreFilter" + Name`) avoids collisions with other plugins.

### 6.3 Rules

- **Always implement `Clone()`.** The framework may need to clone CycleState during preemption simulation. A naive return-self is fine if your data is immutable; if it's mutable (a map, a slice), deep-copy.
- **Use a typed key.** `framework.StateKey` is a string; collisions are runtime panics. Convention is `"PluginName-purpose"`.
- **Don't leak across cycles.** CycleState is reset every cycle. If you put data there that should persist (e.g., a counter), it's gone next pod. Persistent state goes in the plugin struct fields with proper locking.
- **Don't store apiserver references.** Pod and Node pointers come from informer caches; they may be mutated by the next list/watch event. If you store one, copy it.

---

## 7. Permit, Wait, Approve, Deny — the Gang Mechanism

Permit is the unique extension point. It can return a duration: "wait at most this long for someone else to allow this pod." During the wait, the pod is in the **waiting room** — the scheduler holds the reservation on the chosen node but does not yet bind. Another goroutine (in the same scheduler process, or another component talking to the scheduler) can call `WaitForPermit().Approve(uid)` or `Reject(uid, reason)` to release the wait.

This is exactly the primitive needed for gang scheduling: each pod in the gang independently reaches Permit, declares itself "waiting", and the *last* pod (the one that completes the gang) triggers an approve-all signal. Until that signal, every pod is parked at Permit. If the timeout expires, the framework treats it as Unschedulable, releases the reservation, and the pods go back to the queue.

### 7.1 The Permit Interface

```go
type PermitPlugin interface {
    Plugin
    // Permit returns Success / Wait / Reject.
    // If Wait, the second return is the timeout.
    Permit(ctx context.Context, state *CycleState, pod *v1.Pod, nodeName string) (*Status, time.Duration)
}
```

A Wait return holds the pod indefinitely up to the timeout. The waiting pod is identified by UID. To approve or reject from another goroutine:

```go
// inside the plugin, or any code with access to framework.Handle
handle.IterateOverWaitingPods(func(wp framework.WaitingPod) {
    if wp.GetPod().Labels["podgroup"] == "ranjit-job-42" {
        wp.Allow("Coscheduling")  // permit plugin name
    }
})

// or to reject:
wp.Reject("Coscheduling", "gang timed out")
```

`framework.WaitingPod` exposes `GetPod()`, `Allow(pluginName)`, `Reject(pluginName, msg)`. Allow needs to be called *for the same plugin name* that issued the Wait — multiple Permit plugins can each independently wait, and a pod is only released when *all* have allowed.

### 7.2 Coscheduling Wait Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│   COSCHEDULING PERMIT FLOW (scheduler-plugins)                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ Pod creation: gang has 4 workers, podgroup.minMember=4              │
│                                                                     │
│ time   pod    framework cycle                  state                │
│ ────   ────   ───────────────                  ─────                │
│ T+0    w1     Filter/Score/Reserve → Permit:   Wait, 1 waiting      │
│               returns Wait(60s)                                     │
│ T+0.1  w2     Filter/Score/Reserve → Permit:   Wait, 2 waiting      │
│ T+0.2  w3     Filter/Score/Reserve → Permit:   Wait, 3 waiting      │
│ T+0.3  w4     Filter/Score/Reserve → Permit:                        │
│               counts waiting in same PodGroup: 3 → 4 → minMember    │
│               iterates over waiting pods,                           │
│               calls wp.Allow("Coscheduling") on w1, w2, w3          │
│               returns Success for w4                                │
│ T+0.4  ALL    Binding cycle runs in parallel for w1–w4              │
│                                                                     │
│ Failure mode 1: only 3 pods ever arrive                             │
│   T+60   w1   Wait timeout → Unschedulable, Reserve undone          │
│                pod returns to activeQ                               │
│                (the gang never starts)                              │
│                                                                     │
│ Failure mode 2: w3 can't be scheduled                               │
│   T+30   w3   Filter fails on all nodes                             │
│               PostFilter notices podgroup, rejects w1, w2:          │
│               wp.Reject("Coscheduling", "gang infeasible")          │
│               w1, w2 leave waiting room, go back to queue           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

The "last pod approves all" mechanism is the entire trick. Once you see it, gang scheduling stops being mysterious.

### 7.3 Permit in a Hand-Built Plugin

A minimal gang plugin (much simplified vs. scheduler-plugins):

```go
type Gang struct {
    handle  framework.Handle
    waiting map[string]int            // podgroup name → count waiting
    target  map[string]int            // podgroup name → minMember
    mu      sync.Mutex
}

func (g *Gang) Permit(ctx context.Context, _ *framework.CycleState, pod *v1.Pod, _ string) (*framework.Status, time.Duration) {
    pg := pod.Labels["podgroup.k8s.io/name"]
    if pg == "" {
        return framework.NewStatus(framework.Success), 0
    }

    g.mu.Lock()
    g.waiting[pg]++
    count := g.waiting[pg]
    need := g.target[pg]
    g.mu.Unlock()

    if count < need {
        return framework.NewStatus(framework.Wait, fmt.Sprintf("waiting for gang %s (%d/%d)", pg, count, need)),
            60 * time.Second
    }

    // We're the last one. Release all the others.
    g.handle.IterateOverWaitingPods(func(wp framework.WaitingPod) {
        if wp.GetPod().Labels["podgroup.k8s.io/name"] == pg {
            wp.Allow(Name)
        }
    })
    g.mu.Lock()
    delete(g.waiting, pg)
    g.mu.Unlock()
    return framework.NewStatus(framework.Success), 0
}
```

Production gang implementations have to handle: pods leaving the waiting room (cycle abort), PodGroup CRDs created/deleted, queues of PodGroups, priority interleaving, preemption interaction. The real Coscheduling plugin is ~1500 lines.

---

## 8. Gang Scheduling Implementations

Three production approaches to gang scheduling in Kubernetes:

| Project | Approach | Mechanism |
|---|---|---|
| **Volcano** | Replacement scheduler binary | volcano-scheduler watches a PodGroup CRD, queues PodGroups, admits when minMember pods can fit, then creates all pods atomically (or releases gates). Pods opt in via `schedulerName: volcano`. |
| **scheduler-plugins Coscheduling** | Native scheduler-framework Permit plugin | Pods labeled with PodGroup; Permit plugin waits in framework's waiting room until minMember pods reach Permit, then approves all. Pods stay in the kube-scheduler-compatible binary. |
| **YuniKorn** | Replacement scheduler binary | yunikorn-scheduler watches all pods, organizes them into hierarchical queues, gang-schedules via Application reservations. Pods opt in via `schedulerName: yunikorn` and labels. |

Volcano and YuniKorn are *replacement* schedulers in the sense that they run their own binary and pods route to them via `schedulerName`. The default kube-scheduler is unaware of those pods. Coscheduling is the *in-process* alternative — same binary as kube-scheduler, just more plugins.

The trade-offs:

| | Volcano | scheduler-plugins Coscheduling | YuniKorn |
|---|---|---|---|
| Binary | separate | kube-scheduler image with plugin | separate (in Go, on top of yunikorn-core) |
| Apiserver watch cost | doubled (separate scheduler) | normal | doubled |
| Code base maturity | ~7 years, CNCF graduated | ~5 years, less production hardening | ~5 years, Apache top-level |
| Job CRD | yes (`volcano-sh/Job`) | no (just PodGroup) | no (Application is an in-memory concept) |
| Queue tree | flat queues | none (priority only) | hierarchical, with parent-child borrowing |
| DRF / fairness | yes | no | yes (default policy) |
| Preemption | yes, reclaim-based | inherits framework preemption | yes, queue-aware |
| Default-scheduler compatibility | side-by-side (different schedulerName) | replaces some plugins | side-by-side |

If your workload is "ML training jobs with all-or-none semantics, single cluster, light queueing", Coscheduling is the lowest-overhead choice. If you also need queue trees and DRF, Volcano or YuniKorn.

---

## 9. Volcano Deep Look

`volcano-sh/volcano` is a CNCF graduated project, originally born from Huawei's batch-system internals. It ships:

- `volcano-scheduler` — replacement scheduler binary, watches PodGroup, Job, Queue CRDs plus all Pods labelled for it.
- `volcano-controller-manager` — controllers for Job (volcano's own Job type), Queue, PodGroup, command, GC.
- `volcano-admission` — validating + mutating webhook to inject PodGroup references into pods.
- CRDs: `Job`, `PodGroup`, `Queue`, `Command`.

### 9.1 PodGroup

PodGroup represents the gang. Every pod in a gang must reference the same PodGroup either via owner reference (Volcano's controller adds this automatically for Volcano Jobs) or via the annotation `scheduling.k8s.io/group-name`.

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata:
  name: my-training-job
  namespace: ml
spec:
  minMember: 8                              # all-or-nothing across 8 pods
  minResources:                             # gate: gang only starts when this much fits
    cpu: 32
    memory: 128Gi
    nvidia.com/gpu: 8
  queue: ml-team-a                          # which queue admits this
  priorityClassName: training-high
  minTaskMember:
    worker: 8                               # named tasks (Volcano Job concept)
status:
  phase: Running
  running: 8
  succeeded: 0
  failed: 0
  conditions: [...]
```

The volcano-scheduler watches PodGroup. Its **session** abstraction runs continuously: every few hundred milliseconds, the scheduler takes a snapshot of all unscheduled pods grouped by PodGroup, walks queues in priority+DRF order, and for each PodGroup checks whether `minMember` pods can be scheduled to feasible nodes *simultaneously*. If yes, all of them are bound in this session. If no, the PodGroup remains pending and is reconsidered next session.

Crucially: **a Volcano session is atomic across the whole gang.** This is different from the kube-scheduler one-pod-at-a-time + Permit-wait approach. Volcano simulates the entire gang's placement before binding any pod.

### 9.2 Queue

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: ml-team-a
spec:
  weight: 4                                # DRF weight, relative to siblings
  capability:                              # hard ceiling
    cpu: 200
    memory: 800Gi
    nvidia.com/gpu: 32
  reclaimable: true                        # can borrow under-utilized capacity from
                                           # other queues, but must give back on demand
  guarantee:                               # soft floor — at least this much always available
    resource:
      cpu: 50
      memory: 200Gi
      nvidia.com/gpu: 8
```

A Queue is a multi-tenant container for PodGroups. Each Queue has weight (DRF), capability (hard cap), guarantee (soft floor), and `reclaimable` (can be preempted by other queues). The scheduler's `proportion` plugin (volcano internal) computes share by `used/weight` and schedules from the queue with the lowest share.

### 9.3 Volcano Job (Optional)

Volcano provides its own `Job` type that's a superset of `batch/v1.Job`:

```yaml
apiVersion: batch.volcano.sh/v1alpha1
kind: Job
metadata:
  name: tf-distributed
spec:
  minAvailable: 5
  schedulerName: volcano
  policies:
    - event: PodEvicted
      action: RestartJob
  tasks:
    - replicas: 1
      name: ps
      template:
        spec:
          containers:
            - name: tf
              image: tensorflow:2.x
              command: ["python", "ps.py"]
    - replicas: 4
      name: worker
      template:
        spec:
          containers:
            - name: tf
              image: tensorflow:2.x
              command: ["python", "worker.py"]
```

Volcano creates a PodGroup automatically and emits pods for each task. `minAvailable: 5` means 1 ps + 4 workers must run together. `policies` define automatic responses to events (restart the whole job if any pod is evicted) — the classic batch-system pattern.

### 9.4 Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│   VOLCANO ARCHITECTURE                                                 │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│   ┌─────────────────┐    apply Volcano Job                             │
│   │ user/system     │ ────────────────────────────►  apiserver         │
│   └─────────────────┘                                                  │
│                                                                        │
│   apiserver watches:                                                   │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │  vc-controller-manager                                        │    │
│   │   • job-controller: Job → PodGroup + Pods                    │    │
│   │   • podgroup-controller: PodGroup lifecycle                  │    │
│   │   • queue-controller: Queue admit + capacity calc            │    │
│   └──────────────────────────────────────────────────────────────┘    │
│                              │                                         │
│                              ▼ create PodGroup + Pods                  │
│                          apiserver                                     │
│                              │                                         │
│                              ▼ watch                                   │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │  vc-scheduler                                                 │    │
│   │   Loop (every 500ms-1s):                                      │    │
│   │     1. OpenSession — snapshot nodes, pods, podgroups, queues  │    │
│   │     2. Plugins:                                               │    │
│   │        proportion (DRF fair share)                            │    │
│   │        priority (intra-queue ordering)                        │    │
│   │        predicates (node-affinity, resources, etc)             │    │
│   │        nodeorder (scoring)                                    │    │
│   │        gang (enforce minMember atomic admission)              │    │
│   │        binpack (bin-pack scoring)                             │    │
│   │     3. Actions: enqueue → allocate → preempt → reclaim → backfill │
│   │     4. CloseSession — commit binds (Volcano writes Pod.spec.nodeName) │
│   └──────────────────────────────────────────────────────────────┘    │
│                              │                                         │
│                              ▼ bind                                    │
│                          apiserver                                     │
│                              │                                         │
│                              ▼ watch                                   │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │  kubelet on chosen node — normal pod startup                  │    │
│   └──────────────────────────────────────────────────────────────┘    │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

The session model is what makes Volcano different. The default scheduler is event-driven (every pod is scheduled when it appears); Volcano is interval-driven (the scheduler considers everything together every cycle). This costs latency (~500ms for new pods even if free capacity exists) but gives gang-atomic semantics for free.

### 9.5 When to Choose Volcano

- AI/ML training with synchronous distributed workloads (TF, PyTorch DDP, Horovod, MPI).
- Spark/Flink-on-k8s with strict gang requirements.
- Multi-tenant batch clusters where queue trees matter.
- HPC ports from Slurm — Volcano's Job semantics are deliberately Slurm-flavored.

Avoid for:
- Latency-sensitive online services (the session interval adds 500ms+).
- Mixed online + batch clusters where you don't want a separate scheduler binary (use Kueue instead).

---

## 10. Apache YuniKorn Deep Look

`apache/yunikorn-k8shim` and `apache/yunikorn-core` together build a full alternative to kube-scheduler with strong queue semantics. Originally from Cloudera (and the Apache Hadoop YARN tradition), YuniKorn was the first widely-adopted alternative scheduler with hierarchical queues.

### 10.1 Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│   YUNIKORN ARCHITECTURE                                                │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│   ┌──────────────────────────────────────────────────────────────┐    │
│   │  yunikorn-core (Go, generic scheduling engine)                │    │
│   │   • Queue tree (root → tenant-a → user-1, user-2)             │    │
│   │   • Application abstraction                                   │    │
│   │   • Allocation (DRF/FIFO/LIFO/FAIR/binpacking)                │    │
│   │   • Preemption                                                │    │
│   │   • Gang scheduling                                           │    │
│   │   • Reservations                                              │    │
│   └─────────────────────────┬────────────────────────────────────┘    │
│                             │ gRPC scheduler-interface                  │
│   ┌─────────────────────────┴────────────────────────────────────┐    │
│   │  yunikorn-k8shim (k8s adapter)                                │    │
│   │   • watches Pods (where schedulerName=yunikorn)               │    │
│   │   • groups Pods into Applications by label                    │    │
│   │   • routes scheduling requests to core                        │    │
│   │   • binds Pods (spec.nodeName) when core returns allocation   │    │
│   └─────────────────────────┬────────────────────────────────────┘    │
│                             │                                          │
│   ┌─────────────────────────┴────────────────────────────────────┐    │
│   │  apiserver (normal k8s)                                       │    │
│   └──────────────────────────────────────────────────────────────┘    │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

YuniKorn's core is *not* Kubernetes-specific — the same engine has been used with Hadoop YARN. The k8shim translates k8s concepts (Pods, Nodes, namespaces) into YuniKorn concepts (Applications, allocations, queues). This separation means YuniKorn can implement scheduling logic that has no direct k8s analog (queue priorities, application priorities within a queue, hierarchical resource borrowing).

### 10.2 Queue Tree

```
                          root
                         /    \
                        /      \
                  tenant-a    tenant-b
                  /   \          \
                 /     \          \
              prod    dev      data-science
              w=8     w=2         w=10
```

Each queue has weight (DRF), capacity (max), guaranteed-min, ACL (which users can submit), pre-emption policy. Children inherit and refine the parent's policy. Resources can be borrowed across siblings within a parent (if `reclaimable=true`), so `dev` can use `prod`'s unused capacity but must release when `prod` needs it.

Configuration in a ConfigMap:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: yunikorn-configs
  namespace: yunikorn
data:
  queues.yaml: |
    partitions:
      - name: default
        placementrules:
          - name: tag                  # use label to choose queue
            value: queue
            create: false
        queues:
          - name: root
            queues:
              - name: tenant-a
                submitacl: "alice,bob"
                resources:
                  guaranteed:
                    memory: 100G
                    vcore: 50
                  max:
                    memory: 400G
                    vcore: 200
                queues:
                  - name: prod
                    properties:
                      application.sort.policy: fifo
                      preemption.policy: default
                  - name: dev
                    properties:
                      application.sort.policy: fair
              - name: tenant-b
                submitacl: "carol"
                resources:
                  guaranteed:
                    memory: 100G
                    vcore: 50
                  max:
                    memory: 200G
                    vcore: 100
```

A pod is placed into a queue by **placement rule**. Common rules:
- `tag` — look at a pod label (e.g., `queue=root.tenant-a.prod`).
- `user` — derive from the pod's submitter (ServiceAccount or annotation).
- `namespace` — one queue per namespace.

### 10.3 Application

YuniKorn groups pods into Applications. Pods with the same `applicationId` label join the same Application; the Application is the unit YuniKorn schedules at queue level. This maps naturally onto Spark drivers (one app = one Spark job), Flink jobs, training jobs.

Pod labels:
```yaml
metadata:
  labels:
    applicationId: spark-job-42
    queue: root.tenant-a.prod
```

YuniKorn also supports gang scheduling via Application annotations:

```yaml
annotations:
  yunikorn.apache.org/task-group-name: spark-driver
  yunikorn.apache.org/task-groups: |
    [{
      "name": "spark-driver",
      "minMember": 1,
      "minResource": { "cpu": "1", "memory": "2G" }
    }, {
      "name": "spark-executor",
      "minMember": 4,
      "minResource": { "cpu": "2", "memory": "4G" }
    }]
```

YuniKorn reserves placeholder pods for each group, then swaps real pods in when the application starts. This is more complex than Volcano's PodGroup but maps more directly onto the Spark/Flink "I will need N executors, eventually" model.

### 10.4 Scheduling Policies

Per-queue, you choose:
- **FIFO** — strictly first-come-first-served, no fairness.
- **FAIR** — DRF across applications in the queue.
- **DRF** — across users, considering dominant resource.
- **LIFO** — last in, first served (rare).
- **stateaware** — newer apps get priority while older apps make progress (a "give new jobs a chance" heuristic).

### 10.5 When to Choose YuniKorn

- Multi-tenant analytics (Spark, Flink, Hive-on-k8s) with strong cross-team isolation.
- Migration from YARN where the queue model is already a mental fixture.
- Mature DRF and queue ACL needs.

Avoid for:
- Single-tenant or low-team-count clusters — overkill.
- ML training that's not on the Spark/Ray ecosystem — Volcano fits better.

---

## 11. Multi-Scheduler Deployments

Running multiple schedulers in a cluster is mostly a deployment exercise. Each scheduler is a Deployment, each elects its own leader, each has its own `schedulerName`. Pods opt in.

### 11.1 Deployment YAML

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-custom-scheduler
  namespace: kube-system
spec:
  replicas: 2                        # one leader, one standby
  selector:
    matchLabels: { app: my-custom-scheduler }
  template:
    metadata:
      labels: { app: my-custom-scheduler }
    spec:
      serviceAccountName: my-scheduler
      priorityClassName: system-cluster-critical
      containers:
        - name: scheduler
          image: registry.example.com/my-scheduler:v0.4.2
          command:
            - /my-scheduler
            - --config=/etc/scheduler/config.yaml
            - --v=2
          volumeMounts:
            - name: config
              mountPath: /etc/scheduler
              readOnly: true
          resources:
            requests: { cpu: 200m, memory: 512Mi }
            limits:   { cpu: 1,    memory: 1Gi }
          livenessProbe:
            httpGet: { path: /healthz, port: 10259, scheme: HTTPS }
          readinessProbe:
            httpGet: { path: /healthz, port: 10259, scheme: HTTPS }
      volumes:
        - name: config
          configMap: { name: my-scheduler-config }
```

The serviceaccount needs RBAC: list/watch on Pods, Nodes, PVs, PVCs, StorageClasses, CSIDrivers, CSIStorageCapacities; create on PodBindings; update on Pods (for status); and create/update/get on Leases in `kube-system` for leader election. The kubernetes-sigs/scheduler-plugins repo ships a reference RBAC manifest in `manifests/install/charts/as-a-second-scheduler/`.

### 11.2 Leader Election

Each scheduler binary uses `leaderElection.resourceName` to pick its own lock. **You must change this from the default** (`kube-scheduler`) or your scheduler will fight the real kube-scheduler for the same lease and never become leader. (Note: this only causes scheduling stalls, not corruption — Bind is idempotent — but it's confusing.)

```yaml
leaderElection:
  leaderElect: true
  resourceName: my-custom-scheduler
  resourceNamespace: kube-system
  leaseDuration: 15s
  renewDeadline: 10s
  retryPeriod: 2s
```

### 11.3 Routing Pods

A pod chooses its scheduler:

```yaml
spec:
  schedulerName: my-custom-scheduler
```

If unset (default `default-scheduler`), the upstream kube-scheduler picks it up. Make this explicit in your controllers — relying on the default is the #1 cause of "my pod went to the wrong scheduler."

### 11.4 Race Conditions

Two schedulers, both with stale snapshots of node capacity. Worst case:

```
Scheduler A's snapshot at T=0:
  node-1: cpu free = 4 cores

Scheduler A binds pod-X (2 cores) to node-1 at T=0.1s
  Scheduler A's snapshot updates locally (assumed-pod)
  Scheduler A's cache: node-1 cpu free = 2 cores

Scheduler B's snapshot at T=0:  (same starting state)
  node-1: cpu free = 4 cores

Scheduler B at T=0.2s decides to bind pod-Y (3 cores) to node-1
  apiserver accepts the binding
  But the actual remaining capacity (after pod-X) is 2 cores
  kubelet on node-1 sees pod-Y exceeds its allocatable; rejects, sends event
  Pod-Y goes back to scheduler B's queue
```

Mitigations:
- Each scheduler watches all pods (not just its own) so its NodeInfo cache reflects all bound pods. The default kube-scheduler already does this; ensure your custom scheduler does too.
- Watch for the FailedScheduling event and retry on the binding side, not just at scheduling.
- Don't run two schedulers that target overlapping node pools without coordination (e.g., a `nodeSelector` or taint to partition).

---

## 12. KubeSchedulerConfiguration and Profiles

Within one binary, you can run multiple **profiles**. A profile is a named (schedulerName) collection of plugins and weights. A pod opts in via `spec.schedulerName == profile.schedulerName`. This is the cleanest way to support two scheduling regimes (e.g., "batch" vs "online") with a single scheduler binary and one watch cache.

### 12.1 Multi-Profile Config

```yaml
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
leaderElection:
  leaderElect: true
  resourceName: my-scheduler
  resourceNamespace: kube-system

profiles:
  # Profile 1: replicates the default scheduler for online workloads
  - schedulerName: default-scheduler
    plugins:
      score:
        enabled:
          - name: NodeResourcesFit
            weight: 1
          - name: PodTopologySpread
            weight: 2
          - name: InterPodAffinity
            weight: 2
    pluginConfig:
      - name: NodeResourcesFit
        args:
          scoringStrategy:
            type: LeastAllocated      # spread for online
            resources:
              - name: cpu
                weight: 1
              - name: memory
                weight: 1

  # Profile 2: bin-packing + custom plugin for batch
  - schedulerName: batch-scheduler
    plugins:
      filter:
        enabled:
          - name: MaintenanceFilter
      score:
        enabled:
          - name: NodeResourcesFit
            weight: 3
          - name: ImageLocality
            weight: 1
    pluginConfig:
      - name: NodeResourcesFit
        args:
          scoringStrategy:
            type: MostAllocated       # bin-pack for batch
            resources:
              - name: cpu
                weight: 1
              - name: memory
                weight: 1
              - name: nvidia.com/gpu
                weight: 5
```

Now:
- Pods without `schedulerName` set → use `default-scheduler` profile → spread placement.
- Pods with `schedulerName: batch-scheduler` → bin-pack placement, MaintenanceFilter applied.

Single binary, single watch, two behaviors. This is the *recommended* approach when "different workloads need different scheduling logic" — you avoid the deployment complexity of two scheduler processes.

### 12.2 Per-Plugin Args

Most plugins accept arguments via `pluginConfig`. Examples:

- `NodeResourcesFit`: `scoringStrategy.type` is one of `LeastAllocated`, `MostAllocated`, `RequestedToCapacityRatio`; resources to consider, weights per resource.
- `PodTopologySpread`: `defaultConstraints` for cluster-wide spread (added even if pod doesn't specify).
- `InterPodAffinity`: `hardPodAffinityWeight` for the hard rules' contribution to score.
- `VolumeBinding`: `bindTimeoutSeconds`, `shape` for delayed binding scoring.
- `Coscheduling` (scheduler-plugins): `permitWaitingTimeSeconds`, `podGroupBackoffSeconds`.

The schema for each plugin's args lives in `pkg/scheduler/apis/config/v1/`.

### 12.3 The `multiPoint` Shortcut

If a plugin implements multiple extension points (Coscheduling does five), listing it under each is verbose. `multiPoint` enables/disables across all of them:

```yaml
plugins:
  multiPoint:
    enabled:
      - name: Coscheduling
    disabled:
      - name: PrioritySort      # because Coscheduling provides its own QueueSort
```

The framework wires Coscheduling into PreEnqueue, PreFilter, PostFilter, Reserve, Permit automatically.

---

## 13. Capacity Scheduling and Hierarchical Quotas

`scheduler-plugins`'s **CapacityScheduling** plugin (`pkg/capacityscheduling`) adds queue-tree-style quotas inside the framework itself, without replacing the scheduler. It uses two CRDs: `ElasticQuota` (the quota) and `ElasticQuotaTree` (hierarchical relationships).

### 13.1 The Problem

`ResourceQuota` is admission-time: when a namespace exceeds its quota, the next Pod create is rejected by the apiserver. There's no concept of:
- Borrowing under-utilized quota from a sibling namespace.
- Preempting workloads within a quota when a higher-priority pod arrives.
- "Soft" guarantees (this namespace gets at least X, but can grow up to Y when capacity allows).

### 13.2 ElasticQuota

```yaml
apiVersion: scheduling.x-k8s.io/v1alpha1
kind: ElasticQuota
metadata:
  name: team-a-quota
  namespace: team-a
spec:
  min:                       # guaranteed
    cpu: "32"
    memory: 128Gi
  max:                       # can borrow up to this when capacity exists
    cpu: "128"
    memory: 512Gi
```

The CapacityScheduling plugin watches ElasticQuotas. At PostFilter time (preemption), if a pod from namespace `team-a` is unschedulable and `team-a`'s usage is below `min`, the plugin attempts to preempt pods from namespaces over their own `min` (using borrowed capacity) to make room. This restores the team-a minimum at the cost of evicting borrowed workloads.

### 13.3 Comparison

|   | ResourceQuota | Capacity Plugin | Volcano Queue | YuniKorn Queue |
|---|---|---|---|---|
| Enforcement | admission-time | scheduler-time | scheduler-time | scheduler-time |
| Hierarchical | no | yes (via tree CRD) | flat | yes |
| Borrowing | no | yes (min→max) | yes (reclaimable) | yes (max + parent) |
| Preemption | no | yes | yes | yes |
| Requires plugin/scheduler change | no (in apiserver) | yes (custom kube-scheduler image) | yes (separate scheduler) | yes (separate scheduler) |

If you can stay within the default scheduler binary, CapacityScheduling is the lightest-touch answer. If you need full queue semantics, Volcano or YuniKorn.

---

## 14. NUMA-Aware Scheduling (node-resource-topology)

The `NodeResourceTopology` (NRT) plugin lets the scheduler see *intra-node* topology — specifically NUMA, but extensible to PCIe and other partitions. It requires a side component to populate per-node NRT CRDs.

### 14.1 The NRT CRD

```yaml
apiVersion: topology.node.k8s.io/v1alpha2
kind: NodeResourceTopology
metadata:
  name: node-7        # one NRT per node, name == node name
zones:
  - name: node-0
    type: Node              # NUMA node 0
    resources:
      - name: cpu
        capacity: "24"
        allocatable: "20"
        available: "12"
      - name: memory
        capacity: 192Gi
        allocatable: 180Gi
        available: 100Gi
      - name: nvidia.com/gpu
        capacity: "4"
        allocatable: "4"
        available: "2"
  - name: node-1
    type: Node              # NUMA node 1
    resources:
      - name: cpu
        capacity: "24"
        allocatable: "20"
        available: "20"
      - name: memory
        capacity: 192Gi
        allocatable: 180Gi
        available: 180Gi
      - name: nvidia.com/gpu
        capacity: "4"
        allocatable: "4"
        available: "4"
attributes:
  - name: topologyManagerPolicy
    value: single-numa-node
```

### 14.2 The Reporter

The CRD must be kept fresh. The reference reporter is `noderesourcetopology-exporter` from `kubernetes-sigs/node-feature-discovery` (or its sibling `resource-topology-exporter`). It runs as a DaemonSet, reads `/sys/devices/system/node/` and the kubelet's pod-resources API (`/var/lib/kubelet/pod-resources/kubelet.sock`), and updates the NRT every few seconds.

If the reporter is missing on a node, the NRT plugin treats that node as unknown topology — depending on config, either skipped (strict) or treated as non-NUMA (permissive).

### 14.3 The Plugin

NodeResourceTopologyMatch runs at Filter and Score:

- **Filter**: given the pod's resource requests and `topologyManagerPolicy: single-numa-node`, check whether any single zone of the node has enough free capacity. If not, the node is filtered.
- **Score**: among feasible nodes, prefer nodes where the fit uses fewer NUMA nodes, or where after placement the most free capacity remains. Configurable via `scoringStrategy.type`:
  - `LeastAllocated` — same as default scheduler but per-NUMA.
  - `MostAllocated` — bin-pack within NUMA.
  - `LeastNUMANodes` — prefer single-NUMA fits over cross-NUMA spreads.

### 14.4 Interaction with the Kubelet's Topology Manager

The kubelet's topology manager (Chapter 10, §6) is the actual enforcer — it decides which NUMA node each container's CPUs/devices come from, *after* the pod is bound. The scheduler plugin's job is to avoid binding to nodes where the topology manager will fail.

Without the plugin: the scheduler binds a 4-CPU+1-GPU pod to a node with 4 free CPUs total but split as 2 on NUMA0 and 2 on NUMA1, and the GPU on NUMA0. The topology manager admit fails with TopologyAffinityError, the pod is evicted, scheduling retries — wasted cycle.

With the plugin: the scheduler sees only nodes where 4 CPUs + 1 GPU fit on a single NUMA node, so the topology manager always succeeds.

---

## 15. Network-Topology Scheduling (Trimaran)

`Trimaran` is a family of plugins from scheduler-plugins that use *load metrics* (CPU, memory, network) collected from a metrics provider (Prometheus, custom) to make scoring decisions.

Sub-plugins:
- **TargetLoadPacking** — prefer nodes whose actual CPU utilization is close to a target (default 65%), aiming to keep nodes evenly loaded.
- **LoadVariationRiskBalancing** — score nodes by `mean + risk * stddev` of recent utilization; prefer stable nodes.
- **LowRiskOverCommit** — similar to TargetLoadPacking but explicitly accounts for variance.

The plugins read from a `MetricsProvider` interface; the reference implementation is a Prometheus client that queries node CPU/memory utilization. You configure the Prometheus URL in plugin args.

### 15.1 Why It Matters

The default scheduler's NodeResourcesFit uses *requests*, not actual usage. A node with 80% requested but 20% used looks "full" to the scheduler. Trimaran uses actual usage and packs the under-utilized nodes.

This is dangerous in isolation — if the busy workload suddenly bursts, you have no headroom. Trimaran's job is to give you that headroom *across* nodes by spreading the *actual* load, not the requested load.

### 15.2 Network-Aware (NetworkAware Plugin)

The newer `NetworkAware` plugin in scheduler-plugins is different: it considers application-level network topology. The plugin reads:
- `NetworkTopology` CRD — node-to-node latency/bandwidth measurements.
- `AppGroup` CRD — declares which apps talk to which, the heaviness of the edge.

It scores nodes such that frequently-communicating pods land on close-by nodes (same rack, same zone). Useful for tightly-coupled HPC, distributed databases (Cassandra, Cockroach) that want low-latency replicas.

The catch: someone has to populate `NetworkTopology` — either a static manifest or a probe DaemonSet that pings node-to-node. Most clusters never do this; the plugin is more researchy than production.

---

## 16. Cost-Aware Scheduling

Two approaches in practice:

### 16.1 In-Plugin

A Score plugin reads `node.metadata.labels["node.kubernetes.io/instance-type"]` and a static cost table, scoring cheaper instances higher. Combined with a Filter that respects pod SLA labels (e.g., a critical pod refuses spot nodes):

```go
func (c *CostScorer) Score(_ context.Context, _ *framework.CycleState, pod *v1.Pod, nodeName string) (int64, *framework.Status) {
    node, err := c.handle.SnapshotSharedLister().NodeInfos().Get(nodeName)
    if err != nil { return 0, framework.AsStatus(err) }
    instanceType := node.Node().Labels["node.kubernetes.io/instance-type"]
    pricePerHour, ok := c.costTable[instanceType]
    if !ok { return 50, framework.NewStatus(framework.Success) } // unknown → neutral
    // map price: $0.10/hr → 100, $1.00/hr → 10, log-scale
    score := int64(math.Max(0, 100 - 50*math.Log10(pricePerHour*10)))
    return score, framework.NewStatus(framework.Success)
}
```

The pod can express SLA via `nodeAffinity` (require `karpenter.sh/capacity-type=spot` for cheap workloads, require `=on-demand` for critical) — this works without a plugin.

### 16.2 In Karpenter

Karpenter (see §26) considers cost natively. NodePools list instance types in cheapest-first order; the consolidation controller continuously rebinds pods onto cheaper nodes when possible. For cost-driven clusters, Karpenter does more than a Score plugin can: it can *provision* a new node at the cheap price point rather than just choosing among existing.

### 16.3 The Spot/On-Demand Mix

A common pattern: most pods land on spot; critical pods require on-demand; preemption-tolerant pods accept either. Implemented with:

```yaml
# critical: only on-demand
nodeAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    nodeSelectorTerms:
      - matchExpressions:
          - key: karpenter.sh/capacity-type
            operator: In
            values: ["on-demand"]

# preemption-tolerant: weighted preference for spot
nodeAffinity:
  preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      preference:
        matchExpressions:
          - key: karpenter.sh/capacity-type
            operator: In
            values: ["spot"]
```

A custom Cost Score plugin layers on top — when both spot and on-demand nodes have free capacity for a flexible pod, prefer spot.

---

## 17. Topology Spread with Custom Topology Keys

`PodTopologySpread` (built into the default scheduler) spreads pods across topology keys: by default `topology.kubernetes.io/zone` and `kubernetes.io/hostname`. You can use *any* node label as a topology key.

```yaml
spec:
  topologySpreadConstraints:
    - maxSkew: 1
      topologyKey: rack                        # custom: spread across racks
      whenUnsatisfiable: ScheduleAnyway
      labelSelector:
        matchLabels: { app: cassandra }
    - maxSkew: 2
      topologyKey: switch                      # spread across switches
      whenUnsatisfiable: DoNotSchedule
      labelSelector:
        matchLabels: { app: cassandra }
```

For this to work, nodes must be labeled:

```yaml
metadata:
  labels:
    rack: rack-3
    switch: switch-fab-7
```

Typically the labels come from `node-feature-discovery`, the cloud provider's controller (AWS labels nodes with topology.k8s.aws/zone-id, topology.k8s.aws/network-node-layer-1 for instance), or a custom DaemonSet.

The scheduler doesn't need a custom plugin for this — `PodTopologySpread` is built in. You only need the labels to exist. Custom plugins enter the picture when you want to spread *across pods of different specs* or implement non-spread constraints (e.g., "this pod must be in the same rack as that other pod" — that's PodAffinity with a custom topologyKey, also built in).

The advanced case for a custom plugin: spread across a topology key whose value comes from a *resource* rather than a label (e.g., spread across GPUs that share an NVLink island). That requires the plugin to inspect the device manager state, which isn't directly accessible — typically you encode the island into a node label and use TopologySpread anyway.

---

## 18. Time-Windowed Scheduling via Scheduling Gates

Suppose you want batch jobs to run only between 00:00 and 06:00 cluster-time. The simplest implementation:

### 18.1 The Pattern

```
┌─────────────────────────────────────────────────────────────────────┐
│   TIME-WINDOWED SCHEDULING                                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ Pod created at 14:00:                                               │
│   spec.schedulingGates: [{name: "off-peak-only"}]                   │
│       ↓                                                             │
│ Default scheduler PreEnqueue refuses → pod stuck on activeQ tail    │
│ Pod status: SchedulingGated                                         │
│       ↓                                                             │
│ window-controller reconcile @ 14:00:                                │
│   it's day-time → gate stays                                        │
│       ↓                                                             │
│ window-controller reconcile @ 00:00:                                │
│   it's off-peak → PATCH pod removing "off-peak-only" gate           │
│       ↓                                                             │
│ Pod's schedulingGates becomes []                                    │
│       ↓                                                             │
│ Default scheduler picks up pod, schedules normally                  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 18.2 The Controller

```go
func (r *WindowReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    var pod corev1.Pod
    if err := r.Get(ctx, req.NamespacedName, &pod); err != nil {
        return ctrl.Result{}, client.IgnoreNotFound(err)
    }

    // Find our gate.
    idx := -1
    for i, g := range pod.Spec.SchedulingGates {
        if g.Name == "example.com/off-peak-only" {
            idx = i
            break
        }
    }
    if idx == -1 {
        return ctrl.Result{}, nil   // not our gate
    }

    if !isOffPeak(time.Now()) {
        // requeue for the next reconcile pass at the boundary
        return ctrl.Result{RequeueAfter: untilOffPeak(time.Now())}, nil
    }

    // Remove the gate via patch.
    patch := client.MergeFrom(pod.DeepCopy())
    pod.Spec.SchedulingGates = append(pod.Spec.SchedulingGates[:idx], pod.Spec.SchedulingGates[idx+1:]...)
    return ctrl.Result{}, r.Patch(ctx, &pod, patch)
}
```

Total custom code: ~40 lines of Go plus a controller-runtime Manager. No custom scheduler, no kube-scheduler patches, no version skew worry.

### 18.3 Alternative: Suspend

For Jobs specifically, `spec.suspend: true` is similar: the Job controller doesn't create Pods. A controller patches `suspend: false` at the off-peak window. This works at Job level, gates work at Pod level — choose based on which level you control.

---

## 19. External Schedulers (YuniKorn, Poseidon, and the kubelet contract)

The kubelet doesn't care which scheduler bound the pod. The contract is purely: `pod.spec.nodeName == thisNode` → run it. Anything that writes nodeName on a pod can act as a scheduler.

### 19.1 YuniKorn (Already Covered)

A full external scheduler that completely replaces kube-scheduler's responsibilities for pods that opt in. The yunikorn-k8shim issues PATCHes setting `spec.nodeName`. No framework, no plugins — entirely separate implementation.

### 19.2 Poseidon / Firmament

Research project from IBM and Microsoft (~2018) that mapped scheduling to a min-cost-max-flow problem on a graph. Pods, nodes, and resource constraints became graph edges with costs; the optimal scheduling was the min-cost flow. In theory: globally optimal placement across thousands of pods at once, with cycle times under a second.

In practice: the graph construction had pathological cases, the implementation never matured beyond research, and the project is **not maintained** as of recent years. Mentioned here only because it occasionally appears in literature; do not deploy.

### 19.3 The Lesson

Replacing kube-scheduler is hard not because the bind contract is hard (it's trivial — set nodeName) but because *kube-scheduler does a lot of work the kubelet relies on*. Volume binding (especially WaitForFirstConsumer storage classes), preemption with PDB respect, scheduler-time pod admission for resource quotas via plugins — all of these need to be re-implemented in any replacement. Volcano and YuniKorn each have years of code for these corner cases. A from-scratch replacement scheduler is a multi-year project.

---

## 20. Scheduler Extender (the Pre-Framework Webhook)

Before the scheduler framework existed (pre-1.16), the only out-of-tree extension was the **scheduler extender**: a webhook that kube-scheduler called at Filter, Prioritize (now Score), Preempt, and Bind. The webhook is an HTTP server you write in any language.

### 20.1 The Config

```yaml
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
extenders:
  - urlPrefix: "http://my-extender.kube-system.svc:8888/scheduler"
    filterVerb: "filter"
    prioritizeVerb: "prioritize"
    weight: 1
    enableHTTPS: false
    nodeCacheCapable: true
    managedResources:
      - name: example.com/special
        ignoredByScheduler: true
    httpTimeout: 30s
```

### 20.2 The Webhook

The extender receives JSON like:

```json
{
  "Pod": { ... full pod spec ... },
  "Nodes": { "items": [ {nodename, allocatable, ...}, ... ] },
  "NodeNames": ["node-1", "node-2", "node-3"]
}
```

And returns:

```json
{
  "Nodes": { "items": [...] },
  "NodeNames": ["node-1", "node-3"],
  "FailedNodes": { "node-2": "no FPGA bitstream loaded" },
  "Error": ""
}
```

For Prioritize, it returns per-node scores.

### 20.3 Pros and Cons

Pros:
- Language-agnostic — Python, Java, Rust, anything.
- Decoupled deployment — extender is a separate process.
- Predates the framework — runs on any kube-scheduler version.

Cons:
- **HTTP per scheduling cycle.** Even with `nodeCacheCapable: true` (only changed nodes sent), latency is dominated by the round-trip.
- **Cannot participate in Permit, PreEnqueue, QueueSort, Reserve, PostBind.** Only Filter, Prioritize, Preempt, Bind.
- **Deprecated for new use.** The framework supersedes it. The Kubernetes scheduler team recommends migrating to framework plugins.

Still supported. Some legacy systems (early GPU schedulers, some FPGA orchestrators) still use it. Don't write new ones.

---

## 21. Kueue: Admission-Time Queueing

Kueue (`kubernetes-sigs/kueue`, CNCF, SIG-Scheduling) is the newest entrant. It is **not a scheduler**. It is an admission-time controller that decides whether to *release* workloads (Jobs, JobSets, RayJobs, MPIJobs, Pods, deployments) to the underlying scheduler. The underlying scheduler is the default kube-scheduler. Kueue holds workloads back; once admitted, normal scheduling proceeds.

This is conceptually different from Volcano/YuniKorn (which replace the scheduler) and different from coscheduling (which gates inside the scheduler). Kueue is *upstream* of the scheduler.

### 21.1 The Model

```
┌────────────────────────────────────────────────────────────────────────┐
│   KUEUE TWO-PHASE MODEL                                                │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│ User creates Job:                                                      │
│   metadata.labels: { kueue.x-k8s.io/queue-name: team-a }              │
│                                                                        │
│ Kueue job-controller creates a Workload object representing the job:   │
│   apiVersion: kueue.x-k8s.io/v1beta1                                   │
│   kind: Workload                                                       │
│   spec: { podSets: [{count: 8, template: ...}], priorityClass, ... }  │
│   status: { admission: nil }                                           │
│                                                                        │
│ Underlying Job has suspend: true → no pods created yet                 │
│                                                                        │
│ Kueue scheduler reconciles:                                            │
│   1. Read all pending Workloads                                        │
│   2. Sort by priority, age, fair-share                                 │
│   3. For each: check ClusterQueue capacity + ResourceFlavor fit       │
│   4. If admittable: PATCH Workload.status.admission = { flavors, ... }│
│   5. Patch Job.spec.suspend = false → underlying controller releases   │
│                                                                        │
│ Job creates pods → default kube-scheduler picks up + schedules         │
│                                                                        │
│ Job completes → Kueue marks Workload Finished, releases quota          │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

The default scheduler does the placement work. Kueue does the queueing and quota work. Clean separation.

### 21.2 Why Not Just ResourceQuota

ResourceQuota rejects creates beyond quota. Kueue *holds* and admits when capacity allows. The difference matters for batch:
- ResourceQuota: 100 jobs submitted, 50 fit in quota, 50 are rejected with error. The user must retry — annoying for automated pipelines.
- Kueue: 100 jobs submitted, 50 admitted, 50 wait. As earlier jobs finish, Kueue admits more. No retry needed.

Also: Kueue supports `cohort` — groups of queues that borrow capacity from each other (similar to YuniKorn's parent-child queues but flatter). And `ResourceFlavor` — instance-type-aware quota.

---

## 22. Kueue Deep Look — Workload, Cohort, ResourceFlavor

### 22.1 Workload

Kueue's atomic admission unit is the Workload. Each Job/MPIJob/RayJob etc. has a corresponding Workload object created by an integration controller. The Workload describes the resource needs *with all variation* (the Job might have parallelism=8, each pod requesting 4 CPU + 16Gi).

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: Workload
metadata:
  name: pytorch-job-42-wl
  namespace: ml
spec:
  queueName: ml-team-a-local
  priorityClassName: training-mid
  podSets:
    - name: master
      count: 1
      template:
        spec:
          containers:
            - name: pytorch
              resources:
                requests:
                  cpu: "4"
                  memory: 16Gi
                  nvidia.com/gpu: 1
    - name: worker
      count: 7
      template:
        spec:
          containers:
            - name: pytorch
              resources:
                requests:
                  cpu: "4"
                  memory: 16Gi
                  nvidia.com/gpu: 1
status:
  admission:
    clusterQueue: ml-team-a
    podSetAssignments:
      - name: master
        flavors:
          cpu: cpu-on-demand
          memory: cpu-on-demand
          nvidia.com/gpu: a100-on-demand
        resourceUsage:
          cpu: "4"
          memory: 16Gi
          nvidia.com/gpu: "1"
      - name: worker
        flavors:
          cpu: cpu-spot
          memory: cpu-spot
          nvidia.com/gpu: a100-spot
        resourceUsage:
          cpu: "28"
          memory: 112Gi
          nvidia.com/gpu: "7"
```

Note: `admission.podSetAssignments` records *which flavor* of each resource was selected for each podset. The master is on-demand A100; the workers are spot A100. Kueue's logic chose this combination to fit within the ClusterQueue's quota.

### 22.2 LocalQueue and ClusterQueue

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata:
  name: ml-team-a-local
  namespace: ml
spec:
  clusterQueue: ml-team-a
---
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: ml-team-a
spec:
  namespaceSelector: { matchLabels: { team: a } }
  cohort: ml
  preemption:
    withinClusterQueue: LowerPriority
    reclaimWithinCohort: Any
  resourceGroups:
    - coveredResources: [cpu, memory]
      flavors:
        - name: cpu-on-demand
          resources:
            - name: cpu
              nominalQuota: "200"
              borrowingLimit: "100"
            - name: memory
              nominalQuota: 800Gi
              borrowingLimit: 400Gi
        - name: cpu-spot
          resources:
            - name: cpu
              nominalQuota: "400"
              borrowingLimit: "200"
            - name: memory
              nominalQuota: 1600Gi
              borrowingLimit: 800Gi
    - coveredResources: [nvidia.com/gpu]
      flavors:
        - name: a100-on-demand
          resources:
            - name: nvidia.com/gpu
              nominalQuota: "16"
        - name: a100-spot
          resources:
            - name: nvidia.com/gpu
              nominalQuota: "32"
              borrowingLimit: "16"
```

LocalQueue is namespace-scoped and forwards to a ClusterQueue. ClusterQueue is cluster-scoped, has the actual quota, and lives in a Cohort.

### 22.3 ResourceFlavor

ResourceFlavor describes an *instance type* (or a class of nodes). It's how Kueue distinguishes "32 vCPU of spot c5" from "32 vCPU of on-demand c5".

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: ResourceFlavor
metadata:
  name: cpu-spot
spec:
  nodeLabels:
    karpenter.sh/capacity-type: spot
  nodeTaints:
    - key: karpenter.sh/capacity-type
      value: spot
      effect: NoSchedule
  tolerations:
    - key: karpenter.sh/capacity-type
      operator: Equal
      value: spot
      effect: NoSchedule
```

When Kueue admits a Workload to flavor `cpu-spot`, it injects:
- `nodeAffinity` requiring `karpenter.sh/capacity-type=spot` nodes.
- `tolerations` for the spot taint.

So when the pods run, they land only on spot nodes. The default scheduler enforces this; Kueue just decorated the pod spec.

### 22.4 Cohort

```
        cohort: ml
           │
   ┌───────┼───────┐
   │       │       │
team-a  team-b  team-c
 200cpu  200cpu  200cpu       ← nominalQuota each
borrow: 100      0            ← borrowingLimit
```

Cohort members can borrow capacity from siblings up to their `borrowingLimit`. If team-a is full and team-b is idle, team-a can use up to 200 + 100 = 300 cpu. When team-b submits work, Kueue can reclaim (preempt) borrowed pods in team-a to restore fairness — depending on `preemption.reclaimWithinCohort`.

### 22.5 Preemption

Three preemption modes:
- **withinClusterQueue** — when a new high-priority Workload arrives in a queue and the queue's own usage exceeds nominalQuota with borrowing, Kueue can preempt lower-priority Workloads in the same queue.
- **reclaimWithinCohort** — Kueue can preempt workloads from sibling queues that are currently borrowing this queue's nominal capacity.
- **Never** — disabled.

Preemption is implemented by deleting the Workload's underlying Job (or suspending it, depending on integration), letting pods clean up gracefully. The Job goes back to pending; Kueue queues it again.

### 22.6 Workload Integration

Kueue ships built-in integrations for:
- `batch/v1.Job`
- `kubeflow.org/MPIJob`
- `kubeflow.org/PyTorchJob`, `TFJob`, `XGBoostJob`, etc.
- `ray.io/RayJob`, `RayCluster`
- `jobset.x-k8s.io/JobSet`
- Plain Pods (with annotation)

Each integration is a controller that watches its CRD, creates the matching Workload, and unsuspends/admits when Kueue admits.

### 22.7 When to Choose Kueue

- Multi-tenant ML training cluster with diverse frameworks (Kubeflow, Ray, MPI).
- Want to keep the default scheduler.
- Want admission-time quotas with borrowing.
- Don't need the scheduler itself to be tenant-aware.

Limit:
- No gang scheduling within a workload by default — that's the underlying scheduler's job. Workloads are all-or-nothing at admission, but once admitted, pods schedule one at a time. For true gang (e.g., MPI), the underlying integration (kubeflow MPI controller) handles it, often by leveraging the scheduler-plugins coscheduling plugin under the hood.

---

## 23. The Scheduler-Aware Operator Pattern

Custom operators can be scheduler-aware without writing scheduler code. The pattern: your operator creates Pods with scheduling gates, then clears them when ready.

### 23.1 Example: Lease-Holding Job

An operator that runs jobs requiring an external resource lease (e.g., a SaaS API quota slot). The operator can't schedule the pod until the lease is granted.

```go
func (r *LeasedJobReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    var lj v1alpha1.LeasedJob
    if err := r.Get(ctx, req.NamespacedName, &lj); err != nil { ... }

    // Step 1: ensure Pod exists with gate.
    pod := &corev1.Pod{
        ObjectMeta: metav1.ObjectMeta{
            Name: lj.Name + "-pod",
            OwnerReferences: []metav1.OwnerReference{ ownerOf(&lj) },
        },
        Spec: corev1.PodSpec{
            SchedulingGates: []corev1.PodSchedulingGate{
                {Name: "example.com/lease-held"},
            },
            Containers: [...],
        },
    }
    if err := r.ensure(ctx, pod); err != nil { ... }

    // Step 2: request lease.
    leased, err := r.leaseClient.TryAcquire(ctx, lj.Spec.LeaseRef)
    if err != nil { return ctrl.Result{RequeueAfter: 30 * time.Second}, nil }
    if !leased { return ctrl.Result{RequeueAfter: 30 * time.Second}, nil }

    // Step 3: clear gate.
    if hasGate(pod, "example.com/lease-held") {
        patch := client.MergeFrom(pod.DeepCopy())
        pod.Spec.SchedulingGates = removeGate(pod.Spec.SchedulingGates, "example.com/lease-held")
        return ctrl.Result{}, r.Patch(ctx, pod, patch)
    }
    return ctrl.Result{}, nil
}
```

When the pod finishes, the operator releases the lease. The default scheduler did all the work; the operator only handled the lease bookkeeping.

### 23.2 Other Asynchronous Prerequisites

This pattern applies to:
- Cross-cluster resource provisioning (gate cleared when a sibling cluster confirms readiness).
- License token acquisition (commercial SAP/Oracle software).
- Data pre-warming (don't start the training pod until the dataset is in node-local cache).
- Network configuration (don't schedule the pod until the SR-IOV VF is provisioned).
- Compliance check (a CIS scan must pass before the pod can run on a given node — though this is usually done with admission, gates work for asynchronous checks).

---

## 24. Dominant Resource Fairness (DRF)

DRF (Ghodsi et al., 2011) generalizes max-min fairness to multi-resource environments. The idea: each tenant has a *dominant resource* — the resource of which it consumes the largest share. Fairness is enforced on the dominant share: at any time, the tenant with the smallest dominant share is admitted first.

### 24.1 Why It Matters

Cluster has 100 CPUs and 500 GiB. Team A's jobs are CPU-heavy (1 CPU + 1 GiB per pod); team B's jobs are memory-heavy (1 CPU + 50 GiB per pod). With equal weight, simple share-based fairness gives both teams 50% of *something*. Of what?

- DRF: team A's dominant resource is CPU (1/100 per pod). Team B's dominant resource is memory (50/500 = 1/10 per pod). DRF schedules to equalize dominant shares: A gets 50 CPU + 50 GiB (50% CPU share), B gets 5 pods × 1 CPU + 250 GiB (50% memory share). Both teams are "50% of their dominant resource."

This is the only fairness scheme that works across heterogeneous resource demands without starving one team or the other.

### 24.2 Implementations

- **YuniKorn**: default policy. Per-queue dominant share computed across all running applications. Admission order: lowest dominant share next.
- **Volcano**: `proportion` plugin computes share by `(resource used / capability)`, picks the resource with maximum ratio as dominant. Each session schedules from the queue with the lowest dominant ratio.
- **Default kube-scheduler**: no concept of DRF. PriorityClass is global, not per-tenant.
- **Kueue**: no DRF as such; queue-level priority and borrowing within cohort. Fairness across cohorts is implicit (each gets its nominalQuota).

### 24.3 Caveats

DRF assumes consumption is the only signal. Real clusters have:
- Idle time (you don't punish a tenant for being absent earlier).
- Bursty demand (DRF can starve burstable workloads).
- Priority (a tenant may have higher priority for some workloads — DRF doesn't compose well with priorities).

Production scheduler queues add corrections: aging (older waiting jobs get share boost), guaranteed minimum (a floor below which DRF won't push you), priority weights (high-priority tenants get amplified share). Volcano and YuniKorn implement these on top of DRF.

---

## 25. Bin-Packing vs Spread Strategies

A single knob with outsized effect on cluster economics: **NodeResourcesFit**'s scoring strategy.

### 25.1 The Three Strategies

| Strategy | Behavior | Cost Implication |
|---|---|---|
| `LeastAllocated` | prefer nodes with most free capacity | spread → fault-tolerant, but more nodes used → higher cost |
| `MostAllocated` | prefer nodes with least free capacity (after pod added) | bin-pack → fewer nodes used → lower cost, more eviction risk if node fails |
| `RequestedToCapacityRatio` | configurable curve mapping (requested/capacity) → score | tunable; can model "85% target" or "stay below 60%" |

```yaml
pluginConfig:
  - name: NodeResourcesFit
    args:
      scoringStrategy:
        type: MostAllocated         # bin-pack
        resources:
          - { name: cpu, weight: 1 }
          - { name: memory, weight: 1 }
          - { name: nvidia.com/gpu, weight: 5 }   # bias toward GPU bin-packing
```

### 25.2 Implications

- **Online services**: LeastAllocated. You want each node to have headroom for traffic bursts; spreading minimizes blast radius from a node failure.
- **Batch workloads**: MostAllocated. Bin-pack so fewer nodes are running → cluster-autoscaler / Karpenter can scale down idle nodes faster → cost win.
- **Mixed**: use profiles (§12) — online pods use one scheduler profile with LeastAllocated, batch pods use another with MostAllocated.

### 25.3 RequestedToCapacityRatio

The most flexible:

```yaml
scoringStrategy:
  type: RequestedToCapacityRatio
  resources:
    - { name: cpu, weight: 1 }
    - { name: memory, weight: 1 }
  requestedToCapacityRatio:
    shape:
      - { utilization: 0,  score: 10 }
      - { utilization: 70, score: 8 }
      - { utilization: 85, score: 0 }
      - { utilization: 100, score: -10 }
```

This says: score nodes at 70% utilization with 8, peaks at empty, drops to 0 at 85%, negative beyond. The scheduler will fill nodes to 70% then move to fresh nodes — a *target* utilization model. Useful for "leave headroom for autoscaling lag" patterns.

### 25.4 Interaction with Autoscaling

- **Cluster Autoscaler**: scales nodes based on pending pods. Bin-packing helps CA — fewer nodes, easier to find scale-down candidates.
- **Karpenter**: simulates placement before provisioning. Bin-pack is implicit; Karpenter chooses node sizes that just-fit the pending pods. The scheduler's bin-pack strategy is then about how to place pods on existing nodes when Karpenter hasn't yet scaled.
- **Trade-off**: bin-pack + Karpenter is the canonical cost-optimized stack. Spread + reserve capacity is the canonical reliability stack. Picking one is a business decision.

---

## 26. Karpenter as a Scheduler (Sort Of)

Karpenter (CNCF, originated at AWS) is officially a node-provisioning autoscaler — Chapter 22 covers its autoscaling role. But Karpenter does something that blurs the line: it **simulates scheduling** to decide what nodes to provision.

### 26.1 What Karpenter Does

```
Pending pods (unschedulable from kube-scheduler's POV)
         │
         ▼
  Karpenter watches:
   1. Group pods by compatibility (same nodeSelector, taints, etc.)
   2. For each group: simulate placement on candidate node types
   3. Pick the cheapest node type that fits the group
   4. Provision via cloud API (EC2/GCE/Azure)
   5. New node registers with cluster
         │
         ▼
  Default scheduler binds the pending pods to the new node
```

Karpenter's "scheduling simulation" is a user-space implementation of the placement logic — it runs in Karpenter's Go process, not kube-scheduler. It re-implements much of the framework's Filter logic (taints, affinity, topology spread, resource fit) because it needs to know "can these 8 pods fit on a c5.4xlarge?" before provisioning.

### 26.2 Why This Matters for "Custom Schedulers"

If your driving requirement is "schedule onto the cheapest possible mix of instances", Karpenter solves it without you writing a scheduler plugin. Karpenter:
- Tracks instance types and prices.
- Considers spot vs on-demand.
- Bin-packs in simulation.
- Can consolidate (terminate underutilized nodes, force pods to denser placement).
- Respects all standard scheduling constraints (taints, affinities, topology spread).

The default scheduler still does the *actual* binding. Karpenter is upstream of it, like Kueue, but at a different layer (node provisioning rather than workload admission).

### 26.3 Karpenter's Limitations

- Single-cluster.
- No gang scheduling natively (rely on coscheduling plugin or Kueue if needed).
- No queue trees / DRF.
- Cloud-specific (AWS, then Azure, GCE in progress).

For ML training: Karpenter + Kueue is increasingly the standard. Karpenter provisions GPU nodes; Kueue gates workloads on quota; default scheduler binds.

---

## 27. Custom Plugin Pitfalls

### 27.1 Long-Running Filter

Filter runs per pod, per node, per cycle. A 10ms Filter on a 1000-node cluster is 10s per pod. The scheduling queue grinds.

**Mitigation**: move computation to PreFilter; Filter does only quick comparisons. Cache nodes-of-interest in CycleState. Or use Score (which only runs on feasible nodes — fewer iterations).

### 27.2 Stateful PreFilter Leaking Across Cycles

Storing state in plugin struct fields (not CycleState) across pod scheduling cycles is a common bug. CycleState is the right place; plugin struct fields should hold only long-lived state (a Lister handle, a client to an external service).

```go
// BAD: leak per-pod data across cycles
type MyPlugin struct {
    handle      framework.Handle
    lastPodSeen *v1.Pod                // stale data on next pod
}

// GOOD: per-pod data in CycleState
type myCycleData struct { thing int }
func (d *myCycleData) Clone() framework.StateData { return d }

func (p *MyPlugin) PreFilter(_ ctx, cs *framework.CycleState, pod *v1.Pod) (*framework.PreFilterResult, *framework.Status) {
    cs.Write("MyPlugin", &myCycleData{thing: computeThing(pod)})
    return nil, framework.NewStatus(framework.Success)
}
```

### 27.3 Skewed Score Range

Score must return 0..100 (`MaxNodeScore`). If you return 0..1000000, the framework normalizes badly: your scores swamp other plugins'.

**Mitigation**: implement `ScoreExtensions.NormalizeScore` to clamp/scale into [0, 100].

```go
func (p *MyPlugin) NormalizeScore(_ context.Context, _ *framework.CycleState, _ *v1.Pod, scores framework.NodeScoreList) *framework.Status {
    var max int64
    for _, s := range scores { if s.Score > max { max = s.Score } }
    if max == 0 { return framework.NewStatus(framework.Success) }
    for i, s := range scores {
        scores[i].Score = (s.Score * framework.MaxNodeScore) / max
    }
    return framework.NewStatus(framework.Success)
}

func (p *MyPlugin) ScoreExtensions() framework.ScoreExtensions { return p }
```

### 27.4 Forgetting Preemption Interaction

Returning `Unschedulable` from Filter triggers the framework's preemption logic; returning `UnschedulableAndUnresolvable` skips preemption. Pick deliberately.

If your Filter rejects based on a property *no preemption could fix* (e.g., "node lacks the GPU model"), return `UnschedulableAndUnresolvable` — preempting workloads on that node won't summon a GPU. If the rejection is capacity-based ("not enough free CPU"), return `Unschedulable` so preemption can try.

### 27.5 Apiserver Calls in Filter

A Filter that does `kubeClient.Get(node)` on every call hits the apiserver O(nodes × pods) times. At 5k nodes, 1000 pending pods, this is 5M Gets per scheduling burst. The apiserver dies.

**Mitigation**: use the informer cache via `handle.SharedInformerFactory()` or `handle.SnapshotSharedLister()`. These read from in-process caches, no network.

```go
// BAD
node, _ := p.client.CoreV1().Nodes().Get(ctx, nodeName, metav1.GetOptions{})

// GOOD
nodeInfo, _ := p.handle.SnapshotSharedLister().NodeInfos().Get(nodeName)
node := nodeInfo.Node()
```

### 27.6 Custom Scheduler Binary Lagging K8s Version

Each Kubernetes minor release changes the framework API at least slightly. If your scheduler is built against 1.28 and the cluster is on 1.30, you may see panics, missing fields, or — subtler — silently wrong behavior because a new field your plugin doesn't read is now load-bearing.

**Mitigation**: build CI that compiles your scheduler against multiple k8s versions; release a new image with each k8s upgrade; pin to known-tested combinations.

### 27.7 Score That Considers External State

Score plugins that consult external systems (Prometheus, a license server, an SLA database) introduce non-determinism and latency. If Prometheus is slow, every scheduling decision is slow. If Prometheus is down, scoring fails.

**Mitigation**: cache external state in the plugin (with a goroutine refreshing every N seconds); Score returns the cached value. Accept staleness in exchange for predictable latency.

### 27.8 Reserve Without Unreserve

Reserve is "tentatively assign pod to this node." If a later phase fails (Permit reject, PreBind error), Unreserve must roll it back. Forgetting Unreserve leaks the reservation in your plugin's accounting.

The framework calls Unreserve automatically for plugins implementing both — make sure you implement both halves.

### 27.9 Multiple Permit Plugins Interacting

If two Permit plugins both Wait, the pod isn't released until *both* approve. If plugin A approves but plugin B times out, the pod is rejected. Design Permit plugins to not require coordination — each should be independently approve-able.

### 27.10 Bind Plugin Conflicts

Only one Bind plugin can run per pod. If two plugins both register at Bind, the second never runs. The framework guards against this — your registration will be rejected — but it's still a config-time mistake.

---

## 28. Testing Custom Plugins

### 28.1 Unit Tests

The framework testing package provides fakes:

```go
// pkg/scheduler/framework/runtime/framework_helper_test.go references
import (
    "k8s.io/kubernetes/pkg/scheduler/framework"
    "k8s.io/kubernetes/pkg/scheduler/framework/runtime"
    schedulertesting "k8s.io/kubernetes/pkg/scheduler/testing"
)

func TestMaintenanceFilter(t *testing.T) {
    tests := []struct {
        name     string
        nodeLabels map[string]string
        tolerations []v1.Toleration
        want     framework.Code
    }{
        {"clean node", nil, nil, framework.Success},
        {"maintenance node, no toleration", map[string]string{"maintenance": "true"}, nil, framework.UnschedulableAndUnresolvable},
        {"maintenance node, tolerated", map[string]string{"maintenance": "true"},
            []v1.Toleration{{Key: "maintenance-ok", Value: "true"}}, framework.Success},
    }
    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            node := schedulertesting.MakeNode().Name("n").Label("maintenance", tt.nodeLabels["maintenance"]).Obj()
            pod := schedulertesting.MakePod().Name("p").Tolerations(tt.tolerations).Obj()
            ni := framework.NewNodeInfo()
            ni.SetNode(node)
            p := &MaintenanceFilter{}
            got := p.Filter(context.Background(), framework.NewCycleState(), pod, ni)
            if got.Code() != tt.want {
                t.Errorf("got %v want %v", got.Code(), tt.want)
            }
        })
    }
}
```

### 28.2 Integration Tests

Spin up a real apiserver + etcd via `envtest` (controller-runtime/pkg/envtest) and run the scheduler binary against it. `kubernetes/test/integration/scheduler/` has examples of this pattern in-tree.

### 28.3 scheduler-perf Benchmark Suite

`k8s.io/kubernetes/test/integration/scheduler_perf` is the canonical benchmark suite. It defines workload mixes (50k pods, 500 nodes, etc.) and measures scheduling throughput and latency for a given configuration. Run it against your plugin-enabled binary to catch performance regressions.

Sample workload:

```yaml
- name: SchedulingBasic
  workloadTemplate:
    - opcode: createNodes
      countParam: $initNodes
    - opcode: createPods
      countParam: $initPods
  workloads:
    - name: 5000Nodes
      params:
        initNodes: 5000
        initPods: 20000
```

A regression here (your plugin makes scheduling 10x slower) means you absolutely cannot ship.

### 28.4 Soak Tests

In a staging cluster, run your scheduler for a week against realistic workloads. Watch:
- Scheduler memory growth (leaks).
- Goroutine count growth (leaked goroutines).
- pending pod age distribution (P99 should be sub-second).
- API server load.

---

## 29. Versioning Your Scheduler Binary

A custom scheduler is a *Kubernetes component*. It must respect the Kubernetes version skew policy:

- **kube-scheduler is permitted to be within +/- 1 minor of the apiserver.** A scheduler built against 1.30 framework code can run against a 1.29 or 1.31 apiserver but not 1.28 or 1.32.
- Plugin code that uses internal types (`pkg/scheduler/framework`) may break at compile time on minor bumps.
- The KubeSchedulerConfiguration API itself versions (`v1`, `v1beta3`, etc.) and old configs may need migration.

### 29.1 CI Matrix

Recommended:

```yaml
# .github/workflows/test.yaml (excerpt)
strategy:
  matrix:
    k8s_version: ["1.29", "1.30", "1.31"]
steps:
  - run: go test -mod=mod -tags ${{ matrix.k8s_version }} ./...
  - run: ./hack/integration-test.sh ${{ matrix.k8s_version }}
```

You ship images tagged per k8s version: `my-scheduler:v0.4.2-k8s1.30`, `my-scheduler:v0.4.2-k8s1.31`. Operators install the image matching their cluster.

### 29.2 Go Modules Pinning

In `go.mod`:

```
require (
    k8s.io/kubernetes v1.30.0
    k8s.io/api v0.30.0
    k8s.io/apimachinery v0.30.0
    k8s.io/client-go v0.30.0
    k8s.io/component-base v0.30.0
)
```

Note: `k8s.io/kubernetes` import requires a `replace` directive for *every* `k8s.io/*` subpackage because the k/k repo publishes a v0.0.0 placeholder. The scheduler-plugins repo has a battle-tested `go.mod` to copy from.

### 29.3 The Reality

Most custom-scheduler shops carry the maintenance cost on every k8s upgrade. If you don't have engineering capacity for this, do not write a custom scheduler — use Kueue or Karpenter, both of which carry their own k8s upgrade story.

---

## 30. Replace / Multi / Plugin — the Decision

```
You have a scheduling requirement the default scheduler can't handle.
│
├── Is it an "admit work to capacity" problem?
│     (queue, fair-share, multi-tenant, gang at admission)
│   │
│   ├── ML/AI with heterogeneous frameworks → Kueue
│   ├── Spark/Flink/multi-tenant analytics → YuniKorn (or Volcano)
│   └── HPC / synchronous distributed → Volcano
│
├── Is it a "decide between nodes" problem?
│     (topology, cost, custom resource matching)
│   │
│   ├── NUMA / device topology → scheduler-plugins NRT plugin
│   ├── Network proximity → scheduler-plugins Trimaran/NetworkAware
│   ├── Cost → Karpenter + cost-aware NodeAffinity (or score plugin)
│   └── Custom resource semantics → custom Filter/Score plugin
│
├── Is it an "asynchronous prerequisite" problem?
│     (off-peak, license, lease, pre-warm)
│   └── Scheduling gates + your controller. No custom scheduler.
│
└── Is it really "I want to write my own scheduler"?
      Stop. You almost certainly don't.
      Go back, classify the actual requirement above.
```

Risk vs reward:

| Approach | Cost (engineering) | Risk (blast radius) | Reward |
|---|---|---|---|
| Scheduling gates + controller | Low | Very low | Solves async prereqs only |
| Kueue | Low (config) | Low | Admission queueing, quotas |
| scheduler-plugins (pre-built) | Low (config) | Low | Pre-built plugins for common cases |
| Custom plugin (in default scheduler image) | Medium | Medium (own scheduling for whole cluster) | Maximum flexibility, single binary |
| Custom scheduler binary (multi-scheduler) | High | Low (scoped via schedulerName) | Maximum flexibility, full isolation |
| Replace kube-scheduler | Very high | Very high | Only if you're Volcano/YuniKorn |

The middle row — custom plugin in a multi-profile scheduler binary — is what most "we built a custom scheduler" teams actually do.

---

## 31. Real-World Deployments

### 31.1 ML Training Cluster

```
┌─────────────────────────────────────────────────────────────┐
│  Karpenter — provisions A100/H100 nodes as needed           │
│      ▲                                                       │
│      │ pending pods                                          │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Default kube-scheduler                              │    │
│  │  with scheduler-plugins image:                      │    │
│  │   - Coscheduling (gang for PyTorch DDP, MPI)        │    │
│  │   - NodeResourceTopology (NUMA + GPU affinity)      │    │
│  │   - NodeResourcesFit (MostAllocated, GPU-weighted)  │    │
│  └─────────────────────────────────────────────────────┘    │
│      ▲                                                       │
│      │ admitted pods (suspend=false)                         │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ Kueue — queues PyTorchJob, MPIJob, RayJob, JobSet   │    │
│  │   ClusterQueue per team, cohort=ml                  │    │
│  │   ResourceFlavor: a100-on-demand vs a100-spot       │    │
│  └─────────────────────────────────────────────────────┘    │
│      ▲                                                       │
│      │ jobs                                                  │
│  Users (kubeflow / Ray operator)                            │
└─────────────────────────────────────────────────────────────┘
```

This is the modern reference: Kueue (admission/quotas) + scheduler-plugins (gang + topology) + Karpenter (nodes). No bespoke scheduler binary.

### 31.2 Multi-Tenant Analytics

```
┌─────────────────────────────────────────────────────────────┐
│  YuniKorn (alongside default scheduler)                     │
│   Hierarchical queues per team, DRF per queue               │
│   Per-team ACL, capacity, borrowing                         │
│   Spark/Flink jobs go to yunikorn; everything else to       │
│   the default scheduler                                     │
└─────────────────────────────────────────────────────────────┘
```

Spark/Flink CRDs label pods with `schedulerName: yunikorn`. Web services use the default. Clean split, no contention.

### 31.3 HPC Cluster

```
┌─────────────────────────────────────────────────────────────┐
│  Volcano (replaces scheduler for batch)                     │
│   PodGroup per MPI job                                      │
│   Queue per project, with capacity/guarantee                │
│   topology-aware via Volcano's `tdm` plugin                 │
│   Preemption with PDB respect                               │
└─────────────────────────────────────────────────────────────┘
```

HPC users come from Slurm; Volcano Job feels familiar. Plus PodGroup + minResources gives strict gang semantics.

### 31.4 Cost-Optimized Web Tier

```
┌─────────────────────────────────────────────────────────────┐
│  Default kube-scheduler                                     │
│   profile: default-scheduler (LeastAllocated for prod)      │
│   profile: spot-scheduler    (MostAllocated for batch)      │
│      ▲                                                       │
│  Karpenter                                                  │
│   NodePool: prod (on-demand m5)                             │
│   NodePool: batch (spot c6g, weighted preference)           │
└─────────────────────────────────────────────────────────────┘
```

Two scheduler profiles in one binary; two Karpenter NodePools. No custom scheduling code. Cost-optimization comes from Karpenter + nodeAffinity + profile choice.

---

## 32. Operating Multiple Schedulers

When you run more than one scheduler binary, operational complexity multiplies. Each scheduler is a control-plane component with its own:

### 32.1 Leader Election

Each scheduler must have a unique `leaderElection.resourceName` (the Lease object name). If two binaries share a name, they fight for the lease and only one becomes active; the other sees a stale view and may write conflicting binds. Diagnostic: `kubectl -n kube-system get lease | grep scheduler` — every scheduler should have its own.

Set `leaderElect: false` only in single-replica deployments where you accept restart-time downtime.

### 32.2 Metrics

The kube-scheduler exposes Prometheus metrics on `:10259/metrics` (HTTPS). Common metrics:
- `scheduler_pending_pods` — gauge of pending in each queue (activeQ, backoffQ, unschedulableQ).
- `scheduler_pod_scheduling_attempts` — histogram.
- `scheduler_scheduling_attempt_duration_seconds` — per-result histogram (scheduled / unschedulable / error).
- `scheduler_framework_extension_point_duration_seconds` — per-plugin, per-extension-point.

If two schedulers share metric names but are different processes, Prometheus must scrape each separately with `instance` labels distinguishing them. Dashboards must filter or aggregate carefully — a sum across schedulers may double-count or hide outliers.

Custom plugins should expose their own metrics:

```go
import "github.com/prometheus/client_golang/prometheus"

var permitWaitCount = prometheus.NewCounterVec(
    prometheus.CounterOpts{
        Subsystem: "myplugin",
        Name: "permit_wait_total",
        Help: "Number of times Permit returned Wait.",
    },
    []string{"podgroup"},
)
```

Register at plugin init. Use unique subsystem name to avoid collision.

### 32.3 Audit and Tracing

The apiserver audit log records all PATCH spec.nodeName events. To distinguish which scheduler bound a pod, inspect:
- `auditID` and `user.username` — the ServiceAccount issuing the bind tells you which scheduler.
- A custom annotation set by your scheduler at bind time (`example.com/scheduled-by: my-scheduler-v0.4.2`) helps post-mortems.

### 32.4 Alerts

Per-scheduler alerts you want:
- Pending-pod backlog > threshold for > 5 minutes.
- Leader election flapping (frequent acquire/lose).
- Plugin error rate > 0.1%.
- P99 scheduling latency > 1s.

Tag every alert with the scheduler name to avoid "which scheduler is on fire" confusion.

### 32.5 Upgrades

Schedulers upgrade independently. Always upgrade the default kube-scheduler in lockstep with the apiserver (within +/- 1 minor). Custom schedulers should be tested against the new k8s minor before the cluster upgrade. The order:

1. Test custom scheduler against new k8s minor in staging.
2. Build new image tagged for that minor.
3. Cluster upgrade (control plane → workers).
4. Roll custom scheduler image to new tag.

If you must skew temporarily, default scheduler is the safer to upgrade first (kube has more compatibility shims); custom scheduler last.

---

## 33. Pitfalls

A non-exhaustive list of the ways production custom-scheduler deployments break.

1. **Multiple scheduler replicas without leader election** → both are active, both bind pods, race conditions. Always set `leaderElect: true` unless you have exactly one replica and accept restart downtime.
2. **Same `leaderElection.resourceName` as kube-scheduler** → your scheduler fights the default for the same Lease. Neither becomes leader reliably; pods stall. Use a unique name.
3. **Volcano + default scheduler racing on the same pod** → the pod has `schedulerName: ""` (default) but Volcano's webhook sets it to `volcano`. Webhook misconfigured; both schedulers attempt to bind. Pod ends up bound twice or not at all. Always verify which scheduler claims a pod.
4. **`spec.schedulerName` missing on pods you intended for your scheduler** → default scheduler picks them up; your scheduler never sees them. Set explicitly in operators/templates. Don't rely on namespace defaults.
5. **Scheduling gate set but no controller clears it** → pod permanently `SchedulingGated`. Always pair gate creation with a controller; alert on `SchedulingGated` pods older than a threshold.
6. **Removing a gate then re-adding it** → not allowed by the API server. Once removed, gates can never come back. Design so the controller only ever removes; if you need re-gating, recreate the pod.
7. **Custom plugin reading from apiserver on every Filter call** → apiserver DOS. Use informer caches via `handle.SnapshotSharedLister()`. Confirm with apiserver metric `apiserver_request_total{verb="GET",resource="nodes"}` — should be near zero from your scheduler.
8. **NodeResourceTopology CRD missing on some nodes** → the NRT plugin treats them as unknown. Configure `cacheResyncPeriod`, ensure the NRT exporter is on every node (DaemonSet tolerating all taints), monitor `noderesourcetopology` count vs node count.
9. **Kueue Workload created without a matching ClusterQueue** → Workload is "Inadmissible" forever; if the underlying integration creates pods anyway (broken integration), they bypass quota. Always validate the ClusterQueue exists; alert on Workloads in Inadmissible state.
10. **Large gang stuck because of one pod's unsatisfiable request** → 7 pods waiting at Permit, 8th can't filter to any node, eventually all 7 time out. Diagnose with Volcano's `vcctl` or coscheduling metrics. Common cause: GPU request exceeds any node's capacity.
11. **Custom scheduler preempts pods without checking PodDisruptionBudgets** → eviction storm during pod churn. The framework's defaultpreemption plugin respects PDBs; if you write custom preemption, you must too.
12. **`Score` plugin with absolute values like 0..100000** → swamps other plugins in summed score. Implement `NormalizeScore` to remap to 0..100.
13. **`Filter` plugin doing crypto, DNS, or blocking I/O** → scheduler stalls. Filter must be CPU-only and microsecond-scale. Move I/O to PreFilter (still serial but once-per-cycle) or a background refresh in plugin struct fields.
14. **QueueSort ordering causing starvation** → pods at the tail never get popped because new high-priority pods keep arriving. The default PrioritySort uses priority then creation time, which is starvation-prone for low-priority pods under bursty high-priority load. Volcano and YuniKorn add aging.
15. **"Our default scheduler is good enough" — discovered after six months of custom plugin work** → the requirement was actually a PodTopologySpread + PriorityClass + Karpenter NodePool. Always do the §1.7 check first.
16. **Coscheduling plugin requires the scheduler-plugins image, not stock kube-scheduler** → you enabled it in config but the binary doesn't have the plugin compiled in; scheduler refuses to start with "no factory registered." Use the scheduler-plugins image or build your own with the plugin imported.
17. **Forgetting to bump scheduler image on k8s upgrade** → after `kubeadm upgrade` to 1.31, your custom scheduler still on 1.29 framework code skews by 2 minor versions, undefined behavior. Pin image tag to k8s version in your IaC; gate cluster upgrade on scheduler upgrade.
18. **Webhook (admission) that mutates pod resource requests after the scheduler has cached node fit** → the cache snapshot is stale; scheduler binds to a node that can't actually fit. Mutating webhooks run before scheduling, but if a separate controller patches resources later, the scheduler's cache is invalidated only on next list/watch cycle. Don't post-scheduling-mutate resources.
19. **`schedulerName` typo** → pod is bound by *no* scheduler. Sits in `Pending` forever. Validate at admission with a webhook or VAP that ensures `schedulerName` is one of the known scheduler names.
20. **Two custom schedulers both claim pods via label selector** → neither knows the other exists; both attempt to bind. The apiserver accepts whichever wins the PATCH race. Disambiguate via `schedulerName` strictly.
21. **Permit Wait timeout shorter than gang assembly time** → for a 64-pod gang on a slow apiserver, default 60s may not be enough. Tune `permitWaitingTimeSeconds` to expected assembly time × 3.
22. **CycleState `Clone()` shallow-copies a map** → during preemption simulation, mutations to the cloned state corrupt the original. Implement deep Clone.
23. **Plugin panics in Filter** → the framework recovers and marks the node Error, but pods may all be marked Error and re-queued in a tight loop. Watch for `scheduler_framework_extension_point_duration_seconds{result="Error"}` spikes and check logs for panics.
24. **Bind plugin uses the wrong API version** → e.g., issuing a bind subresource on a deprecated version that the apiserver no longer serves. Bind regresses; pods stuck. Use the framework's defaultbinder unless you have a specific reason not to.
25. **Custom QueueSort orders by an external system's metric** → external system goes down, scheduler can't sort, queue stalls. QueueSort must be a pure function of pod fields or framework-tracked metadata; no I/O.
26. **PreEnqueue used to block forever** → equivalent to a poorly-implemented gate but harder to debug because the pod doesn't show `SchedulingGated`. Use real schedulingGates if you need long blocks.
27. **Volcano queue with `weight: 0`** → queue receives no share, jobs stall forever. Validate weights > 0 at admission.
28. **YuniKorn placement rule matches no queue** → application falls into root.default if it exists, or is rejected. Set up a catch-all rule pointing to a low-priority queue.
29. **Kueue cohort with overlapping memberships** → a ClusterQueue can only belong to one cohort. Mis-config rejects the CQ.
30. **Custom resource (GPU model label) only on a subset of nodes** → Filter rejects all other nodes; if the subset is empty (e.g., during a maintenance taint window), pods stall. Use Required + tolerations carefully and validate at submit time.

---

## TL;DR (Reprise)

The default kube-scheduler is enough for stateless web workloads. For everything else — gang scheduling, queue trees, NUMA, cost, time windows, custom resources — there is a hierarchy of extensions, from cheapest to most expensive:

1. **Scheduling gates** + your controller. Zero scheduler change. Best for async prerequisites.
2. **KubeSchedulerConfiguration profiles** + built-in plugin tuning. Zero new code. Best for "spread vs bin-pack" and similar global toggles.
3. **scheduler-plugins** (Coscheduling, NRT, Capacity, Trimaran). Zero new code, ship the upstream-maintained image. Best for "we need NUMA-aware" and similar named features.
4. **Custom plugin in your kube-scheduler image**. Some Go code. Best when you have a custom resource or business rule.
5. **Multi-scheduler** (Volcano, YuniKorn). Separate binary. Best for "batch alongside online" with strong isolation.
6. **Admission-time queueing** (Kueue). Sits upstream of any scheduler. Best for multi-framework ML/AI clusters.
7. **Karpenter** (autoscaler that simulates scheduling). Best for cost-optimized fleets.

The framework — QueueSort, PreEnqueue, PreFilter, Filter, PostFilter, PreScore, Score, NormalizeScore, Reserve, Permit, PreBind, Bind, PostBind — is your toolkit. CycleState is per-cycle scratchpad. Permit is the magic — it can WAIT, which is how every gang scheduler in Kubernetes works.

**Almost no team should write a custom scheduler binary.** The right answer for 95% of "custom scheduling" needs is one of: gates + controller, profile config, Kueue, or scheduler-plugins. The remaining 5% is HPC, multi-tenant analytics, and the kinds of ML training clusters that justify a project the size of Volcano or YuniKorn. Be honest about which category you're in before you start writing Go.
