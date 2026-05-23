# Resource Management and QoS

A Kubernetes node is a shared multi-tenant Linux machine. The kernel doesn't know what a Pod is — it knows processes, cgroups, page-cache pages, NUMA banks, file descriptors, and PIDs. Every "resource limit" in a Pod spec is a knob on a cgroup, an `oom_score_adj` byte in `/proc`, a `cpuset` mask, or a kubelet decision to evict before the kernel kills the wrong process. Get any of those wrong and your workload silently loses 30% of its throughput, or a noisy neighbor takes the node down at 03:00 with a cascading OOM.

This chapter is about the policies the kubelet enforces and the kernel mechanisms they ride on. We start with the fundamental dichotomy — *requests drive scheduling, limits drive enforcement* (§2) — then go through CPU semantics (§4) including CFS throttling (§9) and the strong case for not setting CPU limits (§10), memory semantics (§5) including `memory.high` (§7) and the MemoryQoS feature gate, the three QoS classes (§3) and their `oom_score_adj` (§8), the static CPU manager and memory manager (§11–12), the topology manager (§13), namespace-level governance via `ResourceQuota`, `LimitRange`, and `PriorityClass` (§14–17), extended resources and hugepages (§18–19), ephemeral storage and PID pressure (§20–21), the kubelet's reservation model and the node cgroup hierarchy (§22–23), eviction signals and ranking (§24–26), in-place pod resize (§28), the right-sizing workflow (§30), observability (§31), and the 25+ ways this all goes wrong in production (§32).

Pre-reqs: chapter 00 (Linux primitives — cgroup v2, OOM killer, CFS scheduler), chapter 10 (kubelet internals — eviction manager, CPU/memory/topology managers), chapter 11 (Pod internals — the object whose `.spec.resources` we are interpreting). Forward-references: chapter 22 (autoscaling — HPA/VPA/Karpenter consume the same requests/limits), chapter 25 (multi-tenancy — `ResourceQuota` is the cap that makes shared clusters survivable), chapter 35 (perf — overcommit math at 5000-node scale).

---

## Table of Contents

1.  [Why Requests and Limits Exist](#1-why-requests-and-limits-exist)
2.  [The Two-Knob Model: Scheduling vs Enforcement](#2-the-two-knob-model-scheduling-vs-enforcement)
3.  [QoS Class Derivation](#3-qos-class-derivation)
4.  [CPU Semantics: Fractional Requests, cpu.weight, cpu.max](#4-cpu-semantics-fractional-requests-cpuweight-cpumax)
5.  [Memory Semantics: Bytes, Reservation, memory.max](#5-memory-semantics-bytes-reservation-memorymax)
6.  [The cgroup-v2 Tree Under kubepods.slice](#6-the-cgroup-v2-tree-under-kubepodsslice)
7.  [memory.high and the MemoryQoS Feature Gate](#7-memoryhigh-and-the-memoryqos-feature-gate)
8.  [OOM Scoring: Per-QoS oom_score_adj](#8-oom-scoring-per-qos-oom_score_adj)
9.  [CFS Quota and CPU Throttling](#9-cfs-quota-and-cpu-throttling)
10. [The Case for No CPU Limits](#10-the-case-for-no-cpu-limits)
11. [Static CPU Manager: Pinning Integer-CPU Pods](#11-static-cpu-manager-pinning-integer-cpu-pods)
12. [Memory Manager: NUMA-Local Allocation](#12-memory-manager-numa-local-allocation)
13. [Topology Manager: Hint Merging and Scopes](#13-topology-manager-hint-merging-and-scopes)
14. [ResourceQuota: Namespace Caps](#14-resourcequota-namespace-caps)
15. [LimitRange: Per-Object Defaults and Bounds](#15-limitrange-per-object-defaults-and-bounds)
16. [Quota Scopes and ScopeSelector](#16-quota-scopes-and-scopeselector)
17. [PriorityClass and Preemption Recap](#17-priorityclass-and-preemption-recap)
18. [Extended and Scalar Resources](#18-extended-and-scalar-resources)
19. [Hugepages: 2Mi and 1Gi](#19-hugepages-2mi-and-1gi)
20. [Ephemeral Storage](#20-ephemeral-storage)
21. [PID Limits and pid.available](#21-pid-limits-and-pidavailable)
22. [The Kubelet's Reservation Model: Allocatable](#22-the-kubelets-reservation-model-allocatable)
23. [Cgroup Hierarchy on a Kubernetes Node](#23-cgroup-hierarchy-on-a-kubernetes-node)
24. [Eviction Signals and Thresholds](#24-eviction-signals-and-thresholds)
25. [Eviction Ranking: BestEffort First](#25-eviction-ranking-besteffort-first)
26. [Eviction vs OOM Kill: Proactive vs Reactive](#26-eviction-vs-oom-kill-proactive-vs-reactive)
27. [Throttling Timeline and cpu.stat](#27-throttling-timeline-and-cpustat)
28. [In-Place Pod Resize](#28-in-place-pod-resize)
29. [VPA Integration (Forward Ref)](#29-vpa-integration-forward-ref)
30. [The Right-Sizing Workflow](#30-the-right-sizing-workflow)
31. [Observability: Metrics, Alerts, Dashboards](#31-observability-metrics-alerts-dashboards)
32. [Pitfalls](#32-pitfalls)
33. [TL;DR](#33-tldr)

---

## 1. Why Requests and Limits Exist

Linux gives processes everything they ask for, until it can't, at which point it kills somebody. That works for a laptop. It is catastrophic in a multi-tenant cluster where 100 pods on the same node compete for 64 cores and 256 GiB of RAM. The cluster needs two distinct controls, applied at two distinct times:

1. **A capacity reservation**, consulted at *scheduling* time, that says "this pod needs at least N CPU and M memory; don't place it where that's already promised to someone else." This is `requests`.
2. **A hard ceiling**, enforced at *runtime* by the kernel, that says "no matter how much spare capacity exists, this pod may not consume more than X CPU or Y memory." This is `limits`.

These are different problems with different consequences for setting them wrong:

- A pod with **no requests** is treated by the scheduler as *free* — the node looks unloaded even when its actual cgroup usage is at 95%. The scheduler keeps packing pods on, the kernel starts evicting, and the on-call learns about the cascade at 03:00.
- A pod with **no limits** can consume the entire node if nothing else is competing. Usually that is *fine* — the kernel will multiplex CPU fairly via `cpu.weight` (§4), and memory pressure will trigger eviction *before* the kernel OOM-kills the wrong thing (§24). But a runaway memory allocator with no `memory.max` *will* take the node down. Memory limits are mandatory; CPU limits often hurt more than they help (§10).
- A pod with **requests == limits** (Guaranteed class, §3) is the simplest case: scheduler and kernel agree on the budget, no overcommit, no throttling, no eviction (until the node itself runs out of headroom).

The cluster economy depends on this asymmetry. *Requests* sum to what's promised; the cluster autoscaler grows the fleet when promised > total capacity. *Limits* sum to what could *possibly* be consumed; that number is usually >> total capacity (overcommit) and is what gives Kubernetes its bin-packing efficiency. The QoS class (§3) is just a label that summarizes the relationship between these two numbers for a pod, and the kubelet uses that label to decide who dies first.

### 1.1 The mental model in one diagram

```
       PROMISED                                          ACTUAL USE
       (requests)                                        (cgroup stats)
       ─────────                                         ──────────────
         │                                                    │
         │  scheduler reserves capacity                       │  kernel enforces limits
         │  on a Node based on this                           │  via cpu.max / memory.max
         │                                                    │  on this
         ▼                                                    ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  NODE: Allocatable = Capacity                                    │
   │                       - kubeReserved                             │
   │                       - systemReserved                           │
   │                       - hard-evictionThresholds                  │
   │                                                                  │
   │   ┌────────────────────────────────────────────────────────┐     │
   │   │  Σ(pod.requests)  ≤  Allocatable     (scheduler invariant)│  │
   │   │  Σ(pod.limits)    can EXCEED Allocatable (overcommit)    │   │
   │   └────────────────────────────────────────────────────────┘     │
   │                                                                  │
   │   When Σ(actual_usage) approaches Allocatable:                   │
   │     - cpu: cpu.weight arbitrates (no kill)                       │
   │     - mem: eviction manager picks a victim (proactive)           │
   │     - mem: kernel OOM picks a victim (reactive, last resort)     │
   └──────────────────────────────────────────────────────────────────┘
```

The rest of this chapter is the long form of that diagram.

---

## 2. The Two-Knob Model: Scheduling vs Enforcement

Every `container.resources` field in a Pod spec maps to exactly one of these two semantic categories. Memorize the table:

| Field                    | Read by         | When             | Effect                                         |
| ------------------------ | --------------- | ---------------- | ---------------------------------------------- |
| `requests.cpu`           | kube-scheduler  | scheduling       | counts against node's Allocatable.cpu          |
| `requests.cpu`           | kubelet         | cgroup setup     | written as `cpu.weight` (proportional share)   |
| `requests.memory`        | kube-scheduler  | scheduling       | counts against node's Allocatable.memory       |
| `requests.memory`        | kubelet (MemQoS)| cgroup setup     | written as `memory.min` (soft reservation)     |
| `limits.cpu`             | scheduler       | (ignored)        | does NOT affect placement                      |
| `limits.cpu`             | kubelet         | cgroup setup     | written as `cpu.max` (quota/period throttling) |
| `limits.memory`          | scheduler       | (ignored)        | does NOT affect placement                      |
| `limits.memory`          | kubelet         | cgroup setup     | written as `memory.max` (kernel OOM on exceed) |
| `requests.hugepages-2Mi` | scheduler       | scheduling       | matched to node's `hugepages-2Mi` capacity     |
| `requests.ephemeral-storage` | scheduler   | scheduling       | counts against node's ephemeral-storage cap    |
| `limits.ephemeral-storage`| kubelet eviction | runtime         | triggers per-pod ephemeral eviction            |
| `requests.<extended>`    | scheduler       | scheduling       | matches via Device Plugin (CDI/Allocate)       |

Two rules from that table that surprise people:

- **`limits.cpu` and `limits.memory` are invisible to the scheduler.** A node already 100% scheduled by `requests` will *not* fit another pod even if `limits` would technically allow it; conversely, a node with 50% `requests` will accept another pod even if existing pods' `limits` add up to 400%. The scheduler is a *promises* engine, not a *usage* engine.
- **`requests.cpu` is not just a number — it becomes `cpu.weight` in the cgroup.** Two pods on the same node with `requests.cpu: 100m` each will get equal CPU when contending. A pod with `requests.cpu: 1000m` gets 10x as much CPU as a pod with `requests.cpu: 100m` under contention. This is the *only* way requests influence runtime; there's no per-pod CPU floor.

The flow:

```
Pod manifest                           Cluster                          Node
────────────                           ───────                          ────
                                                                       
spec.containers[].resources.requests   ┌─────────────┐                  
   cpu: 250m                           │  scheduler  │                  
   memory: 512Mi   ───────────────►    │  Filter:    │                  
                                       │  NodeRes-   │                  
                                       │  ourcesFit  │                  
                                       │  plugin     │                  
                                       └──────┬──────┘                  
                                              │ Bind: spec.nodeName     
                                              ▼                         
                                                                  ┌──────────────┐
                                                                  │  kubelet     │
                                                                  │  pod worker  │
                                                                  └──────┬───────┘
                                                                         │
                                                                         ▼
                                                                  ┌──────────────┐
                                                                  │  cm.New      │
                                                                  │  PodContainer│
                                                                  │  Manager     │
                                                                  └──────┬───────┘
                                                                         │
                                                                         ▼
                                                          /sys/fs/cgroup/kubepods.slice/
                                                            kubepods-burstable.slice/
                                                              kubepods-burstable-pod<UID>.slice/
                                                                cri-containerd-<CID>.scope/
                                                                  cpu.weight     ← from requests.cpu
                                                                  cpu.max        ← from limits.cpu
                                                                  memory.min     ← from requests.memory (MemQoS)
                                                                  memory.high    ← from limits.memory * 0.8 (MemQoS)
                                                                  memory.max     ← from limits.memory
                                                                  pids.max       ← from podPidsLimit
```

The scheduler reads the *top half* of that picture. The kernel enforces the *bottom half*. The two halves never speak; their only contract is "the scheduler must not over-promise `requests`."

### 2.1 What if you omit fields?

| Container spec                          | Resulting QoS class | What happens                          |
| --------------------------------------- | ------------------- | ------------------------------------- |
| nothing                                 | BestEffort          | first to die under pressure           |
| `requests.cpu` only                     | Burstable           | scheduled by CPU; no memory promise   |
| `requests.memory` only                  | Burstable           | scheduled by memory; CPU is share-only|
| `requests.cpu`, `requests.memory` (< limits) | Burstable      | guaranteed only up to requests        |
| `requests` == `limits` (both)           | Guaranteed          | scheduler & kernel align; no throttle |
| `limits` only (no requests)             | Burstable           | kubelet defaults requests = limits    |
| `LimitRange` configured for namespace   | depends             | kubelet/apiserver fills defaults      |

The fourth-from-bottom row catches everybody: setting only `limits` does not make a pod Guaranteed — instead the apiserver's defaulter copies `limits` into `requests`, which *does* make it Guaranteed *if no other container in the pod has different settings*. We'll formalize this in §3.

---

## 3. QoS Class Derivation

The QoS class is computed once, by the apiserver/kubelet, and stored as `pod.status.qosClass`. It's not a user input. The decision tree is exactly this (`pkg/apis/core/v1/helper/qos/qos.go`):

```
                        ┌─────────────────────────────────────┐
                        │  For every container in the pod:    │
                        │  - requests AND limits set for      │
                        │    both CPU and memory?             │
                        │  - requests.cpu == limits.cpu?      │
                        │  - requests.mem == limits.mem?      │
                        └──────┬───────────────────────────┬──┘
                               │ yes for ALL                │ no for any
                               ▼                            ▼
                        ┌────────────────┐         ┌────────────────────────┐
                        │  Guaranteed    │         │  Any container has     │
                        │                │         │  any requests OR       │
                        │  oom_score_adj │         │  any limits set?       │
                        │  = -997        │         └─────┬──────────┬───────┘
                        └────────────────┘               │ yes      │ no
                                                         ▼          ▼
                                            ┌─────────────────┐  ┌──────────────┐
                                            │   Burstable     │  │  BestEffort  │
                                            │                 │  │              │
                                            │  oom_score_adj  │  │  oom_score   │
                                            │  computed per-  │  │  _adj = 1000 │
                                            │  container (§8) │  │              │
                                            └─────────────────┘  └──────────────┘
```

The exact rules in code:

```go
// Simplified from pkg/apis/core/v1/helper/qos/qos.go (GetPodQOS).
func GetPodQOS(pod *v1.Pod) v1.PodQOSClass {
    requests := v1.ResourceList{}
    limits   := v1.ResourceList{}
    isGuaranteed := true
    zeroQuantity := resource.MustParse("0")

    for _, c := range pod.Spec.Containers {
        // Aggregate requests and limits across containers (sum).
        for name, q := range c.Resources.Requests {
            if name == v1.ResourceCPU || name == v1.ResourceMemory {
                existing := requests[name]
                existing.Add(q)
                requests[name] = existing
            }
        }
        for name, q := range c.Resources.Limits {
            if name == v1.ResourceCPU || name == v1.ResourceMemory {
                existing := limits[name]
                existing.Add(q)
                limits[name] = existing
            }
        }
        // Guaranteed requires: BOTH CPU and memory limits set,
        // AND limits == requests, on EVERY container.
        if len(c.Resources.Limits) == 0 ||
           c.Resources.Limits.Cpu().IsZero() ||
           c.Resources.Limits.Memory().IsZero() ||
           c.Resources.Requests.Cpu().Cmp(*c.Resources.Limits.Cpu()) != 0 ||
           c.Resources.Requests.Memory().Cmp(*c.Resources.Limits.Memory()) != 0 {
            isGuaranteed = false
        }
    }
    if isGuaranteed && len(pod.Spec.Containers) > 0 {
        return v1.PodQOSGuaranteed
    }
    if requests.Cpu().Cmp(zeroQuantity) == 0 &&
       requests.Memory().Cmp(zeroQuantity) == 0 &&
       limits.Cpu().Cmp(zeroQuantity) == 0 &&
       limits.Memory().Cmp(zeroQuantity) == 0 {
        return v1.PodQOSBestEffort
    }
    return v1.PodQOSBurstable
}
```

Three implications:

- **Init containers count.** A Guaranteed pod must have requests == limits on every init container *and* every regular container. A single init container that omits `limits.memory` demotes the whole pod to Burstable.
- **Native sidecars (1.28+) count.** Same rule applies. Adding an Istio sidecar with no resources downgrades a Guaranteed app to Burstable.
- **Ephemeral storage, hugepages, GPUs, and extended resources do *not* affect QoS.** Only `cpu` and `memory` participate in the QoS computation.

### 3.1 Practical implications of each class

| Class      | Eviction order | OOM `oom_score_adj` | CFS treatment            | NUMA pinning          | Use case                          |
| ---------- | -------------- | ------------------- | ------------------------ | --------------------- | --------------------------------- |
| BestEffort | first          | 1000                | `cpu.weight = 1` (min)   | never                 | dev jobs, batch with no SLO       |
| Burstable  | middle         | 2..999 (formula §8) | `cpu.weight` from req    | never                 | most stateless services           |
| Guaranteed | last           | -997                | `cpu.weight = MAX` and fixed `cpu.max` if limit set | yes (with static CPU manager + int CPUs) | latency-sensitive, RT, low-latency DB |

The single most important *operational* implication: **never put production workloads in BestEffort.** They are the first thing the eviction manager kills under any pressure, with no warning. A misconfigured `Deployment` with no resource block on a busy node will go into CrashLoopBackOff and you will spend two hours blaming the application before you check `kubectl get pod -o jsonpath='{.status.qosClass}'`.

---

## 4. CPU Semantics: Fractional Requests, cpu.weight, cpu.max

CPU in Kubernetes is *fractional* and *normalized to one logical core*. One core's worth of CPU is `1` or equivalently `1000m` (milliCPU). Half a core is `500m`. Two cores are `2` or `2000m`. The unit "logical core" is whatever the kernel sees — on a 64-thread Xeon, that's 64. The scheduler doesn't know about hyperthreads vs physical cores until you reach the topology manager (§13).

### 4.1 How `requests.cpu` becomes `cpu.weight`

On cgroup v2, the relevant file is `cpu.weight`. Range: 1..10000, default 100. It is a *proportional share*: under contention, two tasks with weights w1 and w2 get CPU in ratio w1:w2; with no contention, both can run flat-out.

The kubelet's conversion (`pkg/kubelet/cm/helpers_linux.go`, `MilliCPUToShares` then translated to v2 weight by `cri-containerd` / the runtime):

```
v1 cpu.shares  = max(2, milliCPU * 1024 / 1000)    // legacy
v2 cpu.weight  = ((cpu.shares - 2) * 9999) / 262142 + 1
                 // then clamped to [1, 10000]
```

Concretely:

| `requests.cpu` | `cpu.shares` (v1) | `cpu.weight` (v2) |
| -------------- | ----------------- | ----------------- |
| 100m           | 102               | 4                 |
| 250m           | 256               | 10                |
| 500m           | 512               | 20                |
| 1              | 1024              | 39                |
| 2              | 2048              | 78                |
| 4              | 4096              | 157               |
| 8              | 8192              | 314               |
| 16             | 16384             | 626               |

What this means in practice: if pod A has `requests.cpu: 1` (weight 39) and pod B has `requests.cpu: 100m` (weight 4) and they're on the same fully-loaded core, A gets ~90% of the core, B gets ~10%. With *no* contention (e.g., A is idle), B can use the entire core.

### 4.2 How `limits.cpu` becomes `cpu.max`

On cgroup v2, `cpu.max` has the form `"$QUOTA $PERIOD"` in microseconds:

```
$ cat /sys/fs/cgroup/kubepods.slice/.../cri-containerd-abc.scope/cpu.max
50000 100000
```

That reads as: "in each 100ms period, this cgroup may use 50ms of CPU time, summed across all CPUs." The default period is 100ms (configurable via `--cpu-cfs-quota-period`). The kubelet's formula:

```
quota  = limits.cpu_milli * period / 1000
       = 500 * 100000 / 1000
       = 50000   (microseconds per period, for limits.cpu=500m)
```

The two interesting cases:

- `limits.cpu: 100m` → `cpu.max = "10000 100000"`. The container can use 10ms of CPU per 100ms period. If it tries to use more (e.g., spawns 4 threads each running flat-out), it gets throttled — see §9.
- `limits.cpu: 2` → `cpu.max = "200000 100000"`. The container can use 200ms of CPU per 100ms period, i.e., *two cores' worth*, achievable only by running two threads in parallel.
- No `limits.cpu` → `cpu.max = "max 100000"`. No throttling. The container can saturate the whole machine if `cpu.weight` lets it.

### 4.3 Why `cpu.weight` and `cpu.max` are separate knobs

The two interact in a non-obvious way:

```
Scenario: pod A has requests.cpu=1, limits.cpu=2.
         pod B has requests.cpu=1, no limits.
         Node has 4 cores.
         
         Both pods are CPU-bound, running 4 threads each.
         
                                  cpu.weight   cpu.max
         pod A:                       39        200000 / 100000
         pod B:                       39        max
         
                                  Result
         ────────────────────────────────────────────────────────
         pod A capped at 2 cores by CFS quota (cpu.max).
         pod B uses the remaining 2 cores (weights equal, A maxed).
         No throttling on B. A's throttle counter increments steadily.
```

So `cpu.weight` arbitrates *between* cgroups; `cpu.max` is a per-cgroup absolute cap. Knowing this is the difference between debugging tail latency in 5 minutes and 5 hours.

### 4.4 The 1m floor

You can request as little as `1m` (one milliCPU). The kubelet rejects anything finer. The corresponding `cpu.weight` is 1 (after clamping). Sub-milli precision is meaningless because CFS scheduling granularity is in microseconds and the period is 100ms — you literally cannot account for less than one part in 100000.

---

## 5. Memory Semantics: Bytes, Reservation, memory.max

Memory in Kubernetes is *bytes*, with the usual unit suffixes (`Ki`, `Mi`, `Gi`, `Ti`, plus power-of-ten `K`, `M`, `G`, `T`). One important gotcha: `M` (1 000 000 bytes) is *not* `Mi` (1 048 576 bytes). Most production specs use `Mi`/`Gi`.

### 5.1 `requests.memory` is reserved at scheduling

Unlike `requests.cpu`, `requests.memory` doesn't have an obvious cgroup mapping. The scheduler reserves bytes against `node.status.allocatable.memory`, but the kernel does *not* receive a per-pod floor by default. If three pods on a node each requested 1 GiB and one of them is using 4 GiB while the others use 100 MiB, the kernel is happy until the *node's* total free memory drops past the kubelet's eviction threshold — at which point the kubelet picks a victim (§24).

With the MemoryQoS feature gate (§7) enabled, the kubelet *does* write `memory.min = requests.memory` to give the kernel a hint not to reclaim from that cgroup. But MemoryQoS is still beta in 1.33 and disabled by default in most distributions.

### 5.2 `limits.memory` is a hard ceiling: `memory.max`

This one *is* enforced by the kernel, immediately, and brutally. On cgroup v2:

```
$ cat /sys/fs/cgroup/kubepods.slice/.../memory.max
536870912
```

Means: this cgroup may not use more than 512 MiB of memory. The instant the cgroup's `memory.current` would exceed `memory.max`, the kernel either:

1. Reclaims memory inside the cgroup (page-cache pages, swap if allowed).
2. If reclaim fails, invokes the *cgroup* OOM killer, which picks the worst-scoring process *inside this cgroup* and kills it.

The kill is a `SIGKILL` to the chosen victim. The kubelet observes the death via the container runtime's exit code (137 = 128 + 9), and marks the container's `lastState.terminated.reason = "OOMKilled"`. If the container's `restartPolicy` permits, it's restarted.

### 5.3 Why memory limits are mandatory in production

CPU you can leave open (§10). Memory you cannot. A single misbehaving allocator with no `memory.max` will:

1. Eat all RSS on the node.
2. Push the node into eviction territory (§24).
3. The kubelet evicts pods to recover.
4. Eviction may not be fast enough: a malloc loop can burn 10 GiB/s on modern hardware.
5. The kernel global OOM killer fires.
6. The kernel global OOM killer does *not* know about QoS or the kubelet's preferences directly; it consults `oom_score_adj` (§8), which the kubelet *did* set per pod, but the kernel may still pick the wrong process if everything has the same score.
7. Worst case: the kernel kills the kubelet, the container runtime, or `systemd`. Node goes NotReady.

Setting `memory.max` puts a fence around each pod: a runaway allocator dies inside its own cgroup before it threatens the node.

### 5.4 Swap

By default, Kubernetes (since 1.8) requires swap to be *disabled* (`swapoff -a`). Otherwise the kubelet refuses to start. The reason: swap interacts badly with `memory.max` accounting and with HPA's memory-based scaling. Since 1.28 there's an experimental `failSwapOn=false` and a `NodeSwap` feature gate that allows `--swap-behavior=LimitedSwap`, but it's beta and not widely deployed. Treat swap as off.

---

## 6. The cgroup-v2 Tree Under kubepods.slice

The kubelet builds a four-level cgroup hierarchy on every node. Knowing the shape of this tree is what makes debugging tractable — most "weird resource bugs" can be answered with `cat` on the right file.

```
/sys/fs/cgroup/                                             ← cgroup-v2 unified root
├── init.scope/                                             ← PID 1 (systemd or sysvinit)
├── system.slice/                                           ← systemd-managed services
│   ├── kubelet.service/                                    ← the kubelet itself
│   ├── containerd.service/                                 ← the CRI runtime
│   └── ...
├── user.slice/                                             ← interactive logins
│
└── kubepods.slice/                                         ← LEVEL 1: ALL pods
    │   cpu.weight = 39        # weight 1000 of node CPU
    │   memory.max = max       # uncapped; node-level only
    │   cpu.max = max
    │
    ├── kubepods-besteffort.slice/                          ← LEVEL 2: BestEffort QoS
    │   │   cpu.weight = 1     # lowest priority share
    │   │   memory.max = max
    │   │
    │   └── kubepods-besteffort-pod<UID1>.slice/            ← LEVEL 3: one pod
    │       │   cpu.weight = 1
    │       │   memory.max = max
    │       │
    │       ├── cri-containerd-<CID-pause>.scope/           ← LEVEL 4: pause container
    │       │      pids.current = 1
    │       │
    │       └── cri-containerd-<CID-app>.scope/             ← LEVEL 4: app container
    │              cpu.weight = 1
    │              cpu.max = max
    │              memory.max = max
    │              pids.max = 4096      # podPidsLimit, default
    │
    ├── kubepods-burstable.slice/                           ← LEVEL 2: Burstable QoS
    │   │   cpu.weight = 33    # share between node CPU
    │   │   memory.max = max
    │   │
    │   └── kubepods-burstable-pod<UID2>.slice/             ← LEVEL 3: one pod
    │       │   cpu.weight = sum(container.requests.cpu)
    │       │   memory.max = sum(container.limits.memory) or max
    │       │
    │       ├── cri-containerd-<CID-pause>.scope/
    │       ├── cri-containerd-<CID-init>.scope/            ← init container (terminated)
    │       └── cri-containerd-<CID-app>.scope/
    │              cpu.weight = container.requests.cpu derived
    │              cpu.max    = container.limits.cpu derived (or "max")
    │              memory.max = container.limits.memory (or "max")
    │              memory.min = container.requests.memory (MemQoS only)
    │              memory.high = limits.memory * (throttlingFactor)
    │
    └── kubepods-pod<UID3>.slice/                           ← LEVEL 3 (no L2 for Guaranteed!)
        │   # Guaranteed pods are direct children of kubepods.slice
        │   cpu.weight = sum(container.requests.cpu)
        │   memory.max = sum(container.limits.memory)
        │
        └── cri-containerd-<CID-app>.scope/
               cpuset.cpus = 4-7    # set by static CPU manager (§11)
               cpuset.mems = 0      # set by memory manager (§12)
```

A few non-obvious things:

- **There is no `kubepods-guaranteed.slice`.** Guaranteed pods hang directly off `kubepods.slice/kubepods-pod<UID>.slice`. This is intentional: putting them under a shared parent would impose a parent `cpu.weight` that splits CPU *between* the parent slice and its siblings. By being direct children, Guaranteed pods get their full proportional share against the node root.
- **The pause container exists in its own scope.** It's the namespace anchor (chapter 11). Its cgroup is mostly empty (1 PID, ~100 KiB).
- **The CRI runtime names scopes `cri-containerd-<containerID>.scope` or `crio-<containerID>.scope` depending on the runtime.** containerd uses the former, CRI-O the latter. The kubelet itself doesn't write these; the CRI runtime does, with knobs derived from the OCI runtime spec (`config.json` → `linux.resources` → `unified` map for cgroup-v2).

### 6.1 Reading the tree in practice

```
$ systemctl status kubelet | grep CGroup
   CGroup: /kubepods.slice

$ ls /sys/fs/cgroup/kubepods.slice/ | head
cgroup.controllers
cgroup.events
cgroup.freeze
cgroup.max.depth
cgroup.max.descendants
cgroup.procs
cgroup.subtree_control
cgroup.threads
cgroup.type
cpu.idle
cpu.max
cpu.pressure
cpu.stat
cpu.weight
kubepods-besteffort.slice
kubepods-burstable.slice
kubepods-podb1234...slice               ← Guaranteed pod
kubepods-podc5678...slice               ← Guaranteed pod
memory.current
memory.events
memory.high
memory.max
memory.min
memory.pressure
memory.stat
pids.current
pids.max

$ cat /sys/fs/cgroup/kubepods.slice/cpu.weight
33

$ cat /sys/fs/cgroup/kubepods.slice/memory.current
4831838208                  # ~4.5 GiB currently used by ALL pods
```

This tree is the *only* truth about what's actually happening on the node. Prometheus's cAdvisor walks it every 30 seconds; `kubectl top` reads from `metrics-server` which reads from `/metrics/resource` on the kubelet which reads from `cAdvisor`.

---

## 7. memory.high and the MemoryQoS Feature Gate

`memory.max` is a *hard* limit: hit it, get killed. `memory.high` (introduced in cgroup-v2) is a *soft* limit: hit it, get throttled — the kernel forces direct reclaim inside the offending cgroup, slowing it down so it has less time to allocate. No kill, just back-pressure.

The MemoryQoS feature gate (beta in 1.27, still default-off in 1.33) makes the kubelet write `memory.high` automatically:

```
memory.min  = container.requests.memory
memory.high = floor(throttlingFactor * (limit - request) + request)
memory.max  = container.limits.memory
```

Where `throttlingFactor` defaults to 0.9. So a container with `requests.memory: 100Mi, limits.memory: 1Gi`:

```
memory.min  = 100 MiB     # kernel won't reclaim below this if it can help it
memory.high = 100 + 0.9 * (1024 - 100) = 100 + 832 = 932 MiB
memory.max  = 1024 MiB    # OOM here
```

The behavior:

- Below 100 MiB: kernel preserves these pages (won't push to swap if disabled, won't drop page-cache aggressively).
- 100–932 MiB: normal accounting.
- 932 MiB–1024 MiB: kernel forces reclaim *within this cgroup* whenever the cgroup tries to allocate. Allocations slow down. `memory.events:high` counter increments.
- > 1024 MiB: cgroup OOM killer fires.

Why this matters: without MemoryQoS, an aggressive memory grower just hits `memory.max` and dies. With MemoryQoS, the kernel pushes back *before* the cliff, giving the process a chance to slow down, complete a transaction, run a finalizer, or shed load. Apps that respect `MEMORY_PRESSURE` (via `memory.pressure` PSI) can adapt.

The cost: `memory.high` throttling is implemented by stalling the allocating task. If your app is single-threaded and the allocator is the hot path, throttling looks like a latency spike. So MemoryQoS is great for tail-latency *steadiness* (no OOMs) but trades against tail-latency *minimum* (no throttle stalls).

The feature gate:
```
# kubelet flag:
--feature-gates=MemoryQoS=true
# In KubeletConfiguration:
featureGates:
  MemoryQoS: true
```

### 7.1 Inspecting the PSI signals

cgroup-v2 exposes `memory.pressure`, `cpu.pressure`, `io.pressure` per cgroup. These are PSI (Pressure Stall Information) counters:

```
$ cat /sys/fs/cgroup/kubepods.slice/kubepods-burstable.slice/.../memory.pressure
some avg10=0.42 avg60=0.18 avg300=0.05 total=438219
full avg10=0.00 avg60=0.00 avg300=0.00 total=0
```

`some` = the percentage of time *at least one* task in this cgroup was stalled waiting for memory. `full` = the percentage of time *all* tasks were stalled. `avg10` etc. are decaying averages over 10/60/300 seconds. `total` is monotonic microseconds of stall.

In a Prometheus alert:

```yaml
- alert: PodMemoryPressureHigh
  expr: rate(container_memory_pressure_seconds_total{level="some"}[5m]) > 0.10
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "Pod {{ $labels.namespace }}/{{ $labels.pod }} memory PSI > 10%"
    description: "Container is stalling on memory allocation > 10% of the time. Consider raising memory request/limit or enabling MemoryQoS."
```

---

## 8. OOM Scoring: Per-QoS oom_score_adj

When the kernel needs to kill something to free memory, it scores every process and kills the highest scorer. The score is roughly:

```
score = (RSS_in_pages / total_pages) * 1000 + oom_score_adj
range: -1000 .. 1000
```

`oom_score_adj` lives in `/proc/<pid>/oom_score_adj` and is settable per-process. The kubelet *writes* it for every container process based on QoS class (`pkg/kubelet/qos/policy.go`):

```go
// pkg/kubelet/qos/policy.go (simplified)
const (
    KubeletOOMScoreAdj         int = -999     // kubelet itself
    DockerOOMScoreAdj          int = -999     // (legacy)
    KubeProxyOOMScoreAdj       int = -999
    GuaranteedOOMScoreAdj      int = -997
    BestEffortOOMScoreAdj      int = 1000
)

func GetContainerOOMScoreAdjust(pod *v1.Pod, container *v1.Container,
                                memoryCapacity int64) int {
    if types.IsCriticalPod(pod) {
        // static pods, kube-system priorityClassName=system-cluster-critical
        return KubeletOOMScoreAdj
    }
    switch v1qos.GetPodQOS(pod) {
    case v1.PodQOSGuaranteed:
        return GuaranteedOOMScoreAdj
    case v1.PodQOSBestEffort:
        return BestEffortOOMScoreAdj
    }
    // Burstable: scale by how much memory the container is requesting
    // relative to node capacity.
    memReq := container.Resources.Requests.Memory().Value()
    oomScoreAdj := 1000 - (1000*memReq)/memoryCapacity
    if oomScoreAdj < BurstableOOMScoreAdjMin {
        oomScoreAdj = BurstableOOMScoreAdjMin   // 2
    }
    if oomScoreAdj >= BestEffortOOMScoreAdj {
        oomScoreAdj = BestEffortOOMScoreAdj - 1 // 999
    }
    return int(oomScoreAdj)
}
```

So a Burstable pod requesting 8 GiB on a 64 GiB node:
```
oom_score_adj = 1000 - (1000 * 8) / 64 = 1000 - 125 = 875
```

Higher score = killed first. A pod requesting *more* memory gets a *lower* score (less likely to be killed) — the reasoning is "this pod negotiated for more, so we should respect that more."

### 8.1 Score reference table

| Process / class                              | `oom_score_adj` | Interpretation                          |
| -------------------------------------------- | --------------- | --------------------------------------- |
| kubelet                                      | -999            | kernel will basically never kill it     |
| container runtime (containerd, crio)         | -999            | same                                    |
| `system-node-critical` static pods           | -997            | as critical as Guaranteed               |
| Guaranteed pods                              | -997            | killed only if absolutely nothing else  |
| Burstable, big memory request                | ~100-500        | depends on request/capacity ratio       |
| Burstable, tiny memory request               | 800-999         | nearly as low priority as BestEffort    |
| Burstable, no memory request, only CPU       | 999             | scored as if no memory was requested    |
| BestEffort pods                              | 1000            | first to die                            |

The kubelet writes these scores by passing them to the CRI runtime in `ContainerConfig.linux.resources.oomScoreAdj`. The runtime sets them on the container's init process; the kernel inherits them to children (most of the time — see pitfalls).

### 8.2 What actually happens during OOM

```
Time   Event                                                                  
─────  ──────────────────────────────────────────────────────────────────────  
T+0    A container in pod X allocates 100 MiB more, exceeding its memory.max  
T+0    Kernel: memory.events:oom_kill incremented for cgroup                   
T+1ms  Kernel: scans cgroup.procs, computes oom_score for each PID            
T+2ms  Kernel: picks highest scorer, sends SIGKILL                            
T+3ms  Container PID 1 dies. All its children die (PID namespace teardown).   
T+5ms  Runtime (containerd) observes process death via exit pipe / waitpid    
T+10ms Runtime emits "Exit" event to CRI consumers (kubelet)                  
T+20ms PLEG observes container state change (or evented PLEG: immediate)      
T+50ms Kubelet's syncLoop runs SyncPod, sees container exited, exit code 137  
T+60ms Kubelet's statusManager PATCHes /pod/status:                           
        lastState.terminated.reason = "OOMKilled"                              
        lastState.terminated.exitCode = 137                                    
T+100ms If restartPolicy=Always, kubelet starts a new container (with backoff)
```

The 50–100ms gap between kernel kill and pod-status update is why dashboards sometimes show "container running" right after an OOM — you're looking at the pre-kill snapshot.

---

## 9. CFS Quota and CPU Throttling

CFS = Completely Fair Scheduler. It's the default Linux process scheduler. Its "quota" mechanism enforces `cpu.max`. The mechanism is exactly as described in §4.2: in every period (default 100ms), the cgroup gets at most `quota` microseconds of total CPU time across all CPUs.

### 9.1 The throttling timeline

```
limits.cpu = 400m  →  cpu.max = "40000 100000"  (40ms quota per 100ms period)

Single-threaded workload doing 30ms of work every 100ms:
                                                                 
Period N:    [work 30ms ───────][idle 70ms ──────────────────]   ← uses 30/40 quota
Period N+1:  [work 30ms ───────][idle 70ms ──────────────────]   ← uses 30/40 quota
No throttling. Throughput: 30ms work / 100ms = 30% of a core.    

Single-threaded workload doing 50ms of work every 100ms (busy):
                                                                 
Period N:    [work 40ms ───────────────────────][THROTTLED────]  ← hit quota at 40ms
                                                  exits early    
Period N+1:  [work 40ms ───────────────────────][THROTTLED────]  ← same
Throughput: 40ms work / 100ms = 40% of a core.                   
Tail latency: every burst > 40ms takes at least 100ms wall-clock.

Four-threaded workload, each thread doing 15ms of work every 100ms:
                                                                 
Period N:    [4 threads × 15ms parallel = 60ms quota in ~15ms wall]
             [THROTTLED for remaining 85ms ─────────────────────]
Throughput per thread: 15ms work / 100ms = 15%, but bursty.       
Latency for any single request: high (waiting for next period).  
```

That third scenario is the **multi-threaded CFS throttling trap**, and it's the single most common subtle latency bug in production Kubernetes. A Java app with 4 threads, each doing brief CPU work, can blow through a 40ms quota in 10ms of wall-clock and then wait 90ms for the next period — even though the node is 90% idle.

### 9.2 cpu.stat: the throttling counters

```
$ cat /sys/fs/cgroup/kubepods.slice/.../cri-containerd-abc.scope/cpu.stat
usage_usec      4231090112
user_usec       3892140000
system_usec     338950112
nr_periods      482103
nr_throttled    47281
throttled_usec  892341000
```

- `nr_periods`: how many CFS periods the cgroup has been observed in. ~10 per second.
- `nr_throttled`: how many of those periods ended with the cgroup hitting its quota and being throttled.
- `throttled_usec`: total microseconds the cgroup spent in throttled state.

`throttled_usec / (nr_periods * period_usec) = fraction of wall-clock time throttled`.

The cAdvisor metric:
```
container_cpu_cfs_throttled_periods_total
container_cpu_cfs_throttled_seconds_total
container_cpu_cfs_periods_total
```

A useful alert:
```yaml
- alert: PodCPUThrottlingHigh
  expr: |
    (rate(container_cpu_cfs_throttled_periods_total{container!="",container!="POD"}[5m])
     /
     rate(container_cpu_cfs_periods_total{container!="",container!="POD"}[5m])) > 0.25
  for: 15m
  labels:
    severity: warning
  annotations:
    summary: "Pod {{ $labels.namespace }}/{{ $labels.pod }} CPU throttled >25% of periods"
    description: |
      Container '{{ $labels.container }}' has been throttled in more than 25% of CFS
      periods for 15 minutes. Either raise limits.cpu, remove the limit entirely, or
      investigate why it's bursting (GC pause, async I/O completion storm, etc).
```

### 9.3 The kubelet's `--cpu-cfs-quota-period`

The CFS period defaults to 100ms. The kubelet flag `--cpu-cfs-quota-period` (since 1.18) lets you change it cluster-wide. Shorter periods (e.g., 10ms) reduce worst-case throttle wait time (you wait at most 10ms instead of 100ms) at the cost of higher accounting overhead. Long-running batch workloads benefit from longer periods; latency-sensitive apps benefit from shorter.

In KubeletConfiguration:
```yaml
cpuCFSQuotaPeriod: "10ms"
cpuCFSQuota: true     # default; set false to disable CFS quota enforcement entirely
```

`--cpu-cfs-quota=false` is the nuclear option: it disables `cpu.max` enforcement for *all* containers on the node. The kubelet still writes `cpu.weight` so contention is resolved, but no quota throttling occurs. Useful for latency-critical clusters where you've decided requests-only is the right model (§10).

### 9.4 The fix that's not a fix: `cpu.cfs_quota_us=-1` per container

Pre-1.18, an annotation on Borg-style clusters set `cpu.cfs_quota_us=-1` (no quota) for specific pods. Kubernetes never adopted this per-pod opt-out. Either set `--cpu-cfs-quota=false` cluster-wide, or omit `limits.cpu` per pod, or raise the limit until throttling stops.

---

## 10. The Case for No CPU Limits

Stating it plainly: **most workloads run faster without `limits.cpu`.** The corollary is that **`limits.memory` is still mandatory.** This is the prevailing wisdom at companies that have measured it (Google, Buoyant, Lyft, Indeed, Bryan Boreham's now-famous KubeCon talk, etc.).

### 10.1 The argument

1. CFS quota is enforced *even when there's idle CPU on the node*. A pod with `limits.cpu: 500m` will be throttled at 500m even if the other 31.5 cores are idle.
2. `cpu.weight` from `requests.cpu` already prevents a runaway pod from starving its neighbors. If pod A has `requests.cpu: 100m` and pod B has `requests.cpu: 1`, under contention B gets 10x A's CPU. Without contention, both can use the whole machine.
3. Multi-threaded apps (Java, Node, Go, anything with thread pools or async I/O completion) routinely have brief CPU bursts way above their average. CFS quota turns these bursts into tail-latency disasters.
4. Memory is *not* multiplexable. If you give two pods a hard limit, the kernel can divide CPU fairly through time slicing. It cannot divide *bytes* fairly without picking a loser. Memory limits exist because memory is binary: you have it or you don't.

### 10.2 The counter-argument

1. *Predictable* tenants are easier to schedule. A pod whose limits track its peak makes capacity planning trivial; without limits, you reason about worst-case-possible vs typical, which is uncomfortable.
2. *Hostile* workloads — anything you can't trust to be well-behaved — should not have unlimited CPU. A crypto-mining container will saturate the node.
3. *Charge-back* models often bill per limit. If you offer customers "you can use up to 4 vCPUs", they expect to be able to use 4 and you don't want them stealing 8.
4. *Resource pools that depend on bin-packing.* HPA with target CPU utilization scales based on `current_usage / requests`. If pods routinely use more than requests (because no limit), the metric is dishonest; HPA may not scale when it should.

### 10.3 The compromise that works

In practice the production pattern that works across most stacks:

- **`requests.cpu` set to ~p50 of measured usage** (or "what you need for stable steady-state"). This drives scheduling and `cpu.weight`.
- **No `limits.cpu` for latency-sensitive services.** Let `cpu.weight` arbitrate.
- **`limits.cpu` only for batch/CI/dev workloads** where you genuinely want to cap.
- **`requests.memory` and `limits.memory` both set**, with `limits.memory ≈ 1.5 × requests.memory`. Limit protects the node; the margin between request and limit is your safety buffer.
- **Use ResourceQuota** at the namespace level (§14) to cap *aggregate* CPU; this is where the "untrusted tenant" concern is handled correctly.

The cluster-wide opt-out (`--cpu-cfs-quota=false`) is appropriate when you've decided no workload should ever be CPU-throttled. The per-pod opt-out is just "leave `limits.cpu` blank."

---

## 11. Static CPU Manager: Pinning Integer-CPU Pods

For workloads where even occasional CFS throttling is unacceptable (low-latency trading, RT video transcoding, in-memory databases), the kubelet can pin specific containers to specific physical CPUs and forbid anyone else from running there. This is the **static CPU manager** (`pkg/kubelet/cm/cpumanager/`).

### 11.1 Activation

KubeletConfiguration:
```yaml
cpuManagerPolicy: "static"
cpuManagerPolicyOptions:
  full-pcpus-only: "true"          # don't split hyperthread pairs
  align-by-socket: "true"          # don't span sockets
kubeReserved:
  cpu: "500m"
  memory: "1Gi"
systemReserved:
  cpu: "500m"
  memory: "1Gi"
```

Critical: switching from `none` to `static` requires draining the node and deleting `/var/lib/kubelet/cpu_manager_state` (the state file). The kubelet refuses to start if the state file is inconsistent.

### 11.2 What qualifies for pinning

A container is eligible for exclusive CPU pinning *only if*:

1. The pod's QoS class is **Guaranteed**.
2. The container's `requests.cpu` is an **integer** (`1`, `2`, `4` — *not* `500m`, *not* `1500m`).

Containers that don't qualify (BestEffort, Burstable, fractional Guaranteed) run in the **shared pool** = (all CPUs) − (CPUs allocated to Guaranteed integer pods) − (reserved CPUs for the kubelet/system).

### 11.3 Allocation algorithm

The static manager keeps a CPU topology map (read from `/sys/devices/system/cpu/`):
- Sockets → NUMA nodes → physical cores → logical CPUs (hyperthreads).

When a Guaranteed integer-CPU pod arrives:

```
1. Topology Manager (§13) asks the CPU manager: 
   "for N CPUs, give me a hint about which NUMA node alignments are feasible."
2. CPU manager computes: 
   - Prefer whole physical cores (both hyperthread siblings together).
   - Prefer alignment to a single NUMA node.
   - Prefer alignment to a single socket.
3. Topology Manager merges with hints from Memory Manager (§12) and Device 
   Manager, picks an aligned NUMA node.
4. CPU manager allocates the chosen CPUs, writes them to the container's 
   cpuset.cpus.
5. The kubelet ALSO updates the shared pool: removes those CPUs from every 
   non-pinned container's cpuset.cpus.
```

After:

```
# Pinned Guaranteed container
$ cat /sys/fs/cgroup/kubepods.slice/kubepods-pod<UID>.slice/.../cpuset.cpus
4-7

# Shared-pool container (Burstable, in another pod)
$ cat /sys/fs/cgroup/kubepods.slice/kubepods-burstable.slice/.../cpuset.cpus
0-3,8-31           ← 4-7 excluded
```

The kernel respects `cpuset.cpus` strictly: a process pinned to CPUs 4–7 will run *only* there. The kernel scheduler doesn't have to multiplex.

### 11.4 The state file

```
$ cat /var/lib/kubelet/cpu_manager_state
{
  "policyName": "static",
  "defaultCpuSet": "0-3,8-31",
  "entries": {
    "abc-pod-uid": {
      "container-1": "4-5",
      "container-2": "6-7"
    }
  },
  "checksum": 1234567
}
```

The checksum ensures the kubelet detects state corruption. If the checksum mismatches (e.g., manual edits, partial write), the kubelet refuses to start. Recovery: drain, delete file, restart, drain again, re-admit pods.

### 11.5 When static CPU manager hurts

- Many small Burstable pods + a few Guaranteed integer pods = fragmented shared pool. The Burstable pods are squeezed onto fewer CPUs and contend more.
- Mixed-workload nodes (e.g., 70% Burstable, 30% Guaranteed) often perform *worse* with static CPU manager than with `none` because the latency-sensitive Burstable pods can no longer span all cores during bursts.
- Make sure the workload actually benefits before enabling. Benchmark.

---

## 12. Memory Manager: NUMA-Local Allocation

The memory manager (`pkg/kubelet/cm/memorymanager/`) does for memory what the CPU manager does for CPU: aligns allocations to NUMA nodes.

### 12.1 Activation

```yaml
memoryManagerPolicy: "Static"     # or "None"
reservedMemory:
- numaNode: 0
  limits:
    memory: "1Gi"
- numaNode: 1
  limits:
    memory: "1Gi"
```

Only `Static` is currently supported (`None` is the default no-op).

### 12.2 What it does

For Guaranteed pods (only — same eligibility as CPU manager), the kubelet writes `cpuset.mems` to a NUMA mask:

```
$ cat /sys/fs/cgroup/kubepods.slice/kubepods-pod<UID>.slice/.../cpuset.mems
0
```

That tells the kernel: this container's memory allocations should come from NUMA node 0 only. Combined with the CPU manager pinning to CPUs on NUMA node 0, the result is *local memory access* — every load from this container hits the close DRAM controller, not the remote one (~2x faster).

### 12.3 The two failure modes

- **Topology fragmentation**: pod requests 8 GiB and 4 CPUs, but no single NUMA node has both 8 GiB free and 4 free CPUs. With topology-manager policy `single-numa-node`, the kubelet *rejects* the pod (event: `TopologyAffinityError`). The scheduler will retry on another node — but if every node has the same fragmentation, the pod is permanently Pending.
- **Reservation mismatch**: `reservedMemory` must sum to ≥ `kubeReserved.memory + systemReserved.memory + evictionHard.memory.available`. The kubelet refuses to start if this invariant is violated.

---

## 13. Topology Manager: Hint Merging and Scopes

The topology manager (`pkg/kubelet/cm/topologymanager/`) is the arbiter that asks the CPU manager, memory manager, and device manager for *hints* about NUMA placement, merges them into a single decision, and either admits or rejects the pod.

### 13.1 The hint-merge algorithm

```
                  ┌──────────────────────────────────────┐
                  │  Pod admitted to node                │
                  │  Pod has resources: cpu=4, mem=8Gi,  │
                  │                     nvidia.com/gpu=1 │
                  └─────────────────┬────────────────────┘
                                    │
              ┌─────────────────────┼──────────────────────┐
              │                     │                      │
              ▼                     ▼                      ▼
        ┌──────────┐         ┌───────────┐         ┌─────────────┐
        │ CPU Mgr  │         │ Memory    │         │ Device Mgr  │
        │ "I can   │         │ "I can    │         │ "GPU is on  │
        │ give you │         │ give you  │         │ NUMA 1; can │
        │ NUMA 0  │         │ NUMA 0    │         │ allocate    │
        │ or NUMA1 │         │ or NUMA1  │         │ only there" │
        │ aligned" │         │ aligned"  │         │             │
        └─────┬────┘         └─────┬─────┘         └──────┬──────┘
              │                    │                      │
              └──────────────┬─────┴──────────────────────┘
                             ▼
                  ┌──────────────────────┐
                  │  Topology Manager    │
                  │  merge:              │
                  │   NUMA 0 ∩ NUMA 1 =  │
                  │   { NUMA 1 } (GPU    │
                  │     forces it)       │
                  └──────────┬───────────┘
                             │
              ┌──────────────┼──────────────┐
              │  policy:     │              │
              │  - none:     │ accept anyway, no alignment
              │  - best-effort: prefer NUMA 1, accept anything
              │  - restricted: must use ONLY NUMA 1; reject if can't
              │  - single-numa-node: ONLY a single NUMA node; reject if cross
              └──────────────┼──────────────┘
                             ▼
                  ┌──────────────────────┐
                  │  decision: NUMA 1    │
                  │  Tell CPU Mgr: CPUs  │
                  │    on NUMA 1         │
                  │  Tell Memory Mgr:    │
                  │    cpuset.mems=1     │
                  │  Tell Device Mgr:    │
                  │    GPU on NUMA 1     │
                  └──────────────────────┘
```

### 13.2 The four policies

| Policy              | Behavior                                                                  |
| ------------------- | ------------------------------------------------------------------------- |
| `none` (default)    | No hint merging. Each manager allocates independently.                    |
| `best-effort`       | Prefer aligned hint. If no aligned hint exists, accept anyway.            |
| `restricted`        | Require aligned hint. Reject if every alignment crosses NUMA boundaries.  |
| `single-numa-node`  | Require single-NUMA hint. Reject if any resource spans NUMA.              |

### 13.3 Scopes

Topology can be evaluated at two scopes:

- `container` (default): per-container alignment. A pod with two containers can land each on a different NUMA node.
- `pod`: all containers must align to the same NUMA node. Stricter; more likely to reject.

```yaml
topologyManagerPolicy: "single-numa-node"
topologyManagerScope:  "pod"
```

### 13.4 Why this is hard

Topology decisions are made *at pod admission time on the node*, after the scheduler has already chosen the node. If the topology manager rejects the pod, it goes back to scheduler as `Pending`. The scheduler may pick the same node again (because it doesn't model NUMA), and the cycle repeats — `TopologyAffinityError` events accumulate, the pod never starts.

Mitigations:
- Use the NUMA-aware scheduling plugin (in-tree, behind feature gate `NodeResourcesFitArgs.ScoringStrategy=Topology`) which models per-NUMA resources in the scheduler.
- Use the [scheduler-plugins/noderesourcetopology](https://github.com/kubernetes-sigs/scheduler-plugins) out-of-tree plugin which reads `NodeResourceTopology` CRs.
- Or just use `best-effort` policy and live with occasional cross-NUMA placement.

---

## 14. ResourceQuota: Namespace Caps

`ResourceQuota` is an admission-controller mechanism that caps aggregate resource usage *per namespace*. It runs at the apiserver, before object creation. If a new pod would push the namespace over its quota, the create is rejected with `403 Forbidden`.

### 14.1 What can be quota'd

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: team-a-quota
  namespace: team-a
spec:
  hard:
    # Compute aggregate
    requests.cpu: "100"
    requests.memory: "200Gi"
    limits.cpu: "200"
    limits.memory: "400Gi"
    
    # Extended resources
    requests.nvidia.com/gpu: "8"
    
    # Storage aggregate
    requests.storage: "10Ti"
    persistentvolumeclaims: "50"
    
    # Object counts
    pods: "1000"
    services: "100"
    services.loadbalancers: "5"
    services.nodeports: "10"
    configmaps: "200"
    secrets: "200"
    replicationcontrollers: "100"
    
    # Per-StorageClass storage caps
    gold.storageclass.storage.k8s.io/requests.storage: "5Ti"
    silver.storageclass.storage.k8s.io/requests.storage: "10Ti"
    
    # Hugepages
    hugepages-2Mi: "10Gi"
    
    # Ephemeral storage
    requests.ephemeral-storage: "100Gi"
    limits.ephemeral-storage: "200Gi"
```

### 14.2 Two surprising rules

- **If a `ResourceQuota` exists for a resource (e.g., `requests.cpu`), every pod in that namespace MUST set that field.** A pod missing `requests.cpu` is rejected at admission. This is by design: the apiserver needs to know how much quota to deduct. Combine with `LimitRange` (§15) to set defaults.
- **The quota is checked at create time, then a "used" counter is maintained.** Subsequent updates that change requests trigger re-evaluation. If a pod's actual resource usage exceeds its requests, the quota is still happy — quota is about *requests*, not actual usage.

### 14.3 The accounting controller

The `quotacontroller` (`pkg/controller/resourcequota/`) watches pods and other objects, recomputes used quota, and writes it back to the ResourceQuota's `status.used`:

```yaml
status:
  hard:
    requests.cpu: "100"
    requests.memory: 200Gi
    pods: "1000"
  used:
    requests.cpu: "47500m"
    requests.memory: 89Gi
    pods: "234"
```

`kubectl describe quota team-a-quota` shows this. When a pod creation is rejected:

```
Error from server (Forbidden): error when creating "deploy.yaml":
admission webhook "resourcequota.kubernetes.io" denied the request:
exceeded quota: team-a-quota,
requested: requests.cpu=8,
used: requests.cpu=98,
limited: requests.cpu=100
```

---

## 15. LimitRange: Per-Object Defaults and Bounds

`LimitRange` is the *defaulting + per-object validating* counterpart to ResourceQuota's *aggregate* role. It does three things:

1. Sets default requests/limits for pods that omit them.
2. Enforces min/max per container, per pod, per PVC.
3. Enforces a maxLimitRequestRatio (limits ≤ N × requests).

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: team-a-limits
  namespace: team-a
spec:
  limits:
  - type: Container
    default:                  # used as limits if omitted
      cpu: "500m"
      memory: "512Mi"
    defaultRequest:           # used as requests if omitted
      cpu: "100m"
      memory: "128Mi"
    min:
      cpu: "10m"
      memory: "32Mi"
    max:
      cpu: "8"
      memory: "16Gi"
    maxLimitRequestRatio:
      cpu: "10"               # limit ≤ 10 × request
      memory: "4"             # limit ≤ 4 × request
  - type: Pod
    max:
      cpu: "16"               # aggregate across containers
      memory: "32Gi"
  - type: PersistentVolumeClaim
    min:
      storage: "1Gi"
    max:
      storage: "1Ti"
```

### 15.1 The defaulting interaction with QoS

A pod that specifies *nothing* in a namespace with a LimitRange:

```yaml
# user submits this
apiVersion: v1
kind: Pod
metadata:
  name: bare
  namespace: team-a
spec:
  containers:
  - name: c
    image: nginx
```

…gets transformed by the LimitRange admission plugin into:

```yaml
spec:
  containers:
  - name: c
    image: nginx
    resources:
      requests:    {cpu: "100m", memory: "128Mi"}    # from defaultRequest
      limits:      {cpu: "500m", memory: "512Mi"}    # from default
```

Now its QoS class is **Burstable**, not BestEffort. The user didn't ask for that; the LimitRange did it. This is the *intent* — LimitRange is how cluster admins make sure no pod in their namespace is BestEffort by accident.

### 15.2 Order: LimitRange runs before ResourceQuota

LimitRange is a *mutating* admission plugin (it modifies the pod). ResourceQuota is *validating*. Order matters: LimitRange fills in defaults first, then ResourceQuota deducts those defaults from quota. A pod with no resource spec gets the LimitRange defaults, *those* defaults are deducted from quota.

---

## 16. Quota Scopes and ScopeSelector

A bare `ResourceQuota` applies to *every* pod in the namespace. Sometimes you want quotas that apply only to specific kinds of pods. Hence `scopes` and `scopeSelector`.

### 16.1 The built-in scopes

| Scope                | Matches pods that…                          |
| -------------------- | ------------------------------------------- |
| `Terminating`        | have `spec.activeDeadlineSeconds >= 0`      |
| `NotTerminating`     | do not have `activeDeadlineSeconds`         |
| `BestEffort`         | have QoS class `BestEffort`                 |
| `NotBestEffort`      | have QoS class `Burstable` or `Guaranteed`  |
| `PriorityClass`      | have any non-empty `priorityClassName`      |
| `CrossNamespacePodAffinity` | use cross-namespace affinity        |

Example: cap BestEffort pod count without restricting compute:

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: best-effort-pod-count
  namespace: team-a
spec:
  hard:
    pods: "100"
  scopes:
  - BestEffort
```

### 16.2 ScopeSelector with PriorityClass

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: high-priority-cpu
  namespace: team-a
spec:
  hard:
    requests.cpu: "200"     # team-a may use up to 200 CPU of high priority
  scopeSelector:
    matchExpressions:
    - scopeName: PriorityClass
      operator: In
      values: ["high"]
```

This is how you carve a namespace's allocation between priority classes. Combined with §17's PriorityClass-based preemption, you get a workable resource isolation between batch and serving workloads in the same namespace.

### 16.3 The Terminating gotcha

Pods with `activeDeadlineSeconds` set (typically Jobs, CronJob-owned pods) count against the `Terminating` scope. If you also have a separate quota matching `NotTerminating`, the same pod doesn't count against the latter. But the *default* quota (no scope) counts every pod. So:

- Default quota: all pods → counted
- `Terminating` quota: only Job-ish pods → counted
- `NotTerminating` quota: only long-running pods → counted

A pod can count against multiple quotas at once.

---

## 17. PriorityClass and Preemption Recap

`PriorityClass` is a cluster-scoped object that maps a name to an integer priority. Pods reference it via `spec.priorityClassName`.

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: high
value: 1000000
globalDefault: false
description: "high-priority production workloads"
preemptionPolicy: PreemptLowerPriority
```

Defaults shipped with Kubernetes:
- `system-cluster-critical` (2_000_000_000): essential cluster components.
- `system-node-critical` (2_000_001_000): essential node components (kubelet, kube-proxy, CNI).

### 17.1 What priority does

1. **Scheduling order**: the scheduler dequeues higher-priority pending pods first.
2. **Preemption**: if a high-priority pod can't fit anywhere, the scheduler picks a node where *evicting* one or more lower-priority pods would make it fit. It evicts them (graceful delete, respecting PodDisruptionBudgets), waits, and then binds.
3. **Eviction immunity**: the kubelet's eviction manager (§24) sorts victims by priority *within QoS class*. Higher-priority pods are evicted later.

### 17.2 Preemption respects PDBs

The scheduler runs a "would this PDB be violated?" check before nominating a victim. If preemption would put a Deployment below its `minAvailable`, the scheduler picks a different victim (or, if none exists, the preemption fails and the high-priority pod stays Pending).

### 17.3 The `PreemptionPolicy`

- `PreemptLowerPriority` (default): high-priority pods preempt lower-priority pods to fit.
- `Never`: the pod just queues. Won't kick anybody out. Used for high-priority *batch* (you want them ahead in the queue but you don't want them murdering services).

### 17.4 Without a PriorityClass, what's the priority?

If `globalDefault: true` is set on one PriorityClass, that's the default. Otherwise, default is 0. So existing pods (no priority) are at the bottom of the queue and the first to be preempted.

---

## 18. Extended and Scalar Resources

Not everything is CPU and memory. **Extended resources** are arbitrary integer-valued resources advertised by nodes and consumed by pods.

### 18.1 The advertisement side

A node controller, device plugin, or admin can write capacity for a custom resource:

```bash
# Manual patch (rare):
$ kubectl patch node node-1 --subresource=status --type='json' \
   -p='[{"op": "add", "path": "/status/capacity/example.com~1dongle", "value": "4"}]'
```

Real-world: device plugins do this automatically via `kubelet`'s registration socket (`/var/lib/kubelet/plugins_registry/`). The GPU plugin advertises `nvidia.com/gpu: 8` on each GPU node.

### 18.2 The consumption side

```yaml
spec:
  containers:
  - name: train
    image: tensorflow/tensorflow:latest-gpu
    resources:
      requests:
        nvidia.com/gpu: 1
      limits:
        nvidia.com/gpu: 1     # required: requests must equal limits for extended
```

For extended resources, **requests must equal limits**. The scheduler treats them as binary "match or no-match" — fractional GPUs aren't allowed (you can't slice a GPU in two by saying `0.5` here; multi-tenancy on a GPU is the device plugin's job via MIG/MPS).

### 18.3 What the kubelet does

When the scheduler binds a pod requesting `nvidia.com/gpu: 1`, the kubelet:

1. Calls the device plugin's `Allocate(deviceIDs=[GPU-42])` gRPC.
2. The plugin returns env vars (`NVIDIA_VISIBLE_DEVICES=GPU-42`), mount paths (`/dev/nvidia0`), and CDI annotations.
3. The kubelet passes these into the CRI ContainerConfig.
4. The runtime applies them when launching the container.

### 18.4 Scalar vs OCI device

"Extended resource" is the K8s API name. "Scalar resource" is sometimes used interchangeably. "OCI device" is the runtime concept — a device file mounted into the container. The Device Plugin maps K8s extended resources to OCI devices.

---

## 19. Hugepages: 2Mi and 1Gi

Linux normally manages memory in 4 KiB pages. For workloads with huge working sets (databases, JVMs, DPDK), 4 KiB pages cause TLB thrash — every access misses the TLB, costs a page-table walk, and burns CPU. **Hugepages** are 2 MiB or 1 GiB physical pages. Fewer TLB entries cover the same memory; databases see 10–20% throughput improvements.

### 19.1 Reservation at boot

Hugepages must be reserved from the kernel before any workload requests them. They are *not* allocatable from regular RAM on demand (without `madvise(MADV_HUGEPAGE)` and Transparent Huge Pages, which K8s doesn't use).

```bash
# In /etc/default/grub:
GRUB_CMDLINE_LINUX="default_hugepagesz=1G hugepagesz=1G hugepages=16 hugepagesz=2M hugepages=2048"
# Reserves: 16 × 1GiB pages and 2048 × 2MiB pages.

# Or at runtime (NUMA-aware):
echo 16 > /sys/devices/system/node/node0/hugepages/hugepages-1048576kB/nr_hugepages
echo 16 > /sys/devices/system/node/node1/hugepages/hugepages-1048576kB/nr_hugepages
```

### 19.2 Advertised by the kubelet

After boot, the kubelet auto-discovers hugepages and adds them to `node.status.capacity`:

```yaml
status:
  capacity:
    cpu: "32"
    memory: "131072Mi"
    hugepages-1Gi: "16Gi"
    hugepages-2Mi: "4Gi"
```

### 19.3 Consumed by pods

```yaml
spec:
  containers:
  - name: db
    image: postgres
    resources:
      requests:
        memory: "10Gi"
        hugepages-1Gi: "8Gi"     # 8 × 1GiB pages
      limits:
        memory: "10Gi"
        hugepages-1Gi: "8Gi"
    volumeMounts:
    - mountPath: /hugepages
      name: hugepage-1gi
  volumes:
  - name: hugepage-1gi
    emptyDir:
      medium: HugePages-1Gi      # the file-backed view of hugepages
```

The pod gets a tmpfs-style mount backed by hugepages; the application opens a file there and mmap()s it. Pods using hugepages must be Burstable or Guaranteed (BestEffort can't request them).

### 19.4 Hugepages and QoS

Hugepages don't participate in QoS class derivation (§3). A pod with hugepages but no CPU/memory requests is BestEffort and can be evicted; the hugepages are released back to the pool on eviction. Treat hugepages as a *capacity-bound* resource, not a QoS one.

---

## 20. Ephemeral Storage

Every container has a writable layer (the OCI runtime's overlay upperdir) and (usually) some `emptyDir` volumes. Both live on the node's filesystem and count against **ephemeral storage**.

### 20.1 What counts

- Container writable layer (anything written outside a mounted volume).
- `emptyDir` volumes (unless `medium: Memory`, which uses tmpfs and counts against memory).
- Container logs (`/var/log/pods/<pod>/<container>/0.log`).

What does *not* count: persistent volumes (those are CSI's problem), hostPath volumes, `emptyDir` with tmpfs medium.

### 20.2 Requests and limits

```yaml
resources:
  requests:
    ephemeral-storage: "1Gi"
  limits:
    ephemeral-storage: "5Gi"
```

Scheduler: counts `requests.ephemeral-storage` against `node.status.allocatable.ephemeral-storage`.

Kubelet: every 10s the eviction manager runs `du`/`statfs` on the container's writable layer and emptyDirs. If usage exceeds `limits.ephemeral-storage`, the kubelet *evicts* the pod (sends `kubectl delete --grace-period=...`). This is a per-pod eviction, not a node-level one.

### 20.3 The cost: `du` is expensive

The kubelet walking the pod's writable layer with `du` is O(files). At 100 pods × 50k files each = 5M stat() calls every 10s. On slow disks this is noticeable. Newer kubelets (1.31+) use filesystem-level quotas (project quotas on xfs/ext4) when available, which give O(1) accounting. Enable via `featureGates: LocalStorageCapacityIsolationFSQuotaMonitoring: true`.

### 20.4 nodefs vs imagefs

The node has two storage partitions in the kubelet's mental model:
- **`nodefs`**: where pod root volumes, emptyDirs, logs live. Usually `/var/lib/kubelet`.
- **`imagefs`**: where the container runtime stores images and writable layers. Usually `/var/lib/containerd` or `/var/lib/docker`.

If both are on the same filesystem (common), they're the same partition. If split (recommended for production), `imagefs.available` and `nodefs.available` are tracked separately — see §24.

---

## 21. PID Limits and pid.available

Linux's global PID space is bounded (`/proc/sys/kernel/pid_max`, default 4194304 on modern kernels). Each cgroup-v2 also has `pids.max`. Exhaust either and `fork()` returns `EAGAIN`. New processes can't start; the kubelet itself may fail to launch new containers.

### 21.1 Per-pod limit: `podPidsLimit`

```yaml
# KubeletConfiguration
podPidsLimit: 4096
```

Default: -1 (unlimited inside the cgroup; only `pid_max` caps you). With this set, every container's cgroup gets `pids.max = 4096`. If a pod tries to fork the 4097th task, `EAGAIN`.

```
$ cat /sys/fs/cgroup/kubepods.slice/.../pids.max
4096
$ cat /sys/fs/cgroup/kubepods.slice/.../pids.current
237
```

### 21.2 Node-level: `pid.available` eviction signal

The kubelet tracks total node PIDs vs `pid_max`. When the ratio crosses an eviction threshold, pods get evicted. Default thresholds:

```yaml
evictionHard:
  pid.available: "10%"        # default: 10% of pid_max remaining
evictionSoft:
  pid.available: "15%"
evictionSoftGracePeriod:
  pid.available: "1m"
```

### 21.3 Common PID exhaustion patterns

- **Java apps with high thread counts.** Each thread is a task. 1000 threads × 100 pods = 100k. Within default limits, fine. With `podPidsLimit: 1024`, every other pod will hit it during GC.
- **Process forking shell pipelines.** Old-school shell scripts that fork hundreds of subshells per request.
- **Misconfigured workers.** Gunicorn/Unicorn with thousands of workers.
- **Zombie process accumulation.** A container that fork()s but never wait()s leaves zombies. They count against `pids.current` until the parent dies.

---

## 22. The Kubelet's Reservation Model: Allocatable

Not every byte of node RAM is available to pods. The kubelet reserves chunks for itself, for the OS, and for an eviction buffer.

```
node.status.capacity         = total physical resources
node.status.allocatable      = capacity − kubeReserved − systemReserved − evictionHard
```

### 22.1 The KubeletConfiguration knobs

```yaml
kubeReserved:                  # for kubelet, container runtime, plugins
  cpu: "500m"
  memory: "1Gi"
  ephemeral-storage: "10Gi"
  pid: "1000"
systemReserved:                # for the OS (systemd, sshd, kernel...)
  cpu: "500m"
  memory: "1Gi"
  ephemeral-storage: "10Gi"
  pid: "1000"
evictionHard:                  # hard floor (eviction triggers immediately if crossed)
  memory.available: "500Mi"
  nodefs.available: "10%"
  nodefs.inodesFree: "5%"
  imagefs.available: "15%"
  pid.available: "10%"
```

Now `allocatable = capacity − (500m+500m) − (1Gi+1Gi) − 500Mi` (and similar for storage/PID).

### 22.2 The cgroups they live in

The kubelet creates a cgroup hierarchy that *enforces* these reservations:

```
/sys/fs/cgroup/
├── kubepods.slice/             ← cpu.weight set so pods get (capacity - kubeReserved - systemReserved)
├── system.slice/               ← OS workloads
│   ├── kubelet.service/        ← reserved by KubeReserved (via systemd Slice= or kubeletCgroups)
│   └── containerd.service/
└── ...
```

Two enforcement modes (`enforceNodeAllocatable`):
- `["pods"]` (default): only the `kubepods.slice` cgroup is constrained. The kubelet/system can still grow.
- `["pods", "kube-reserved", "system-reserved"]`: also constrain the kubelet's and system's cgroups. Stronger isolation but risks killing the kubelet under load — used carefully.

```yaml
enforceNodeAllocatable: ["pods"]
kubeletCgroups: "/kubelet.slice"
systemCgroups: "/system.slice"
```

### 22.3 The math

On a 32-core, 128 GiB node with the config above:

```
Capacity:    cpu=32, memory=128Gi, ephemeral-storage=400Gi, pid=4194304
Reserved:    cpu=1, memory=2Gi, ephemeral-storage=20Gi, pid=2000
Eviction:    memory=500Mi, ephemeral-storage=40Gi (10% nodefs), pid=419k (10% pid)

Allocatable: cpu=31, memory=125.5Gi, ephemeral-storage=340Gi, pid=3.77M
```

The scheduler sees `allocatable`. The pods can use at most that. The reserved 2.5Gi + 500Mi = 3Gi of memory is *invisible to pods* but is what keeps the kubelet alive when pods misbehave.

---

## 23. Cgroup Hierarchy on a Kubernetes Node

We already showed the pod-level hierarchy in §6; here's the full picture including system reservations:

```
/sys/fs/cgroup/                                       ← root cgroup-v2
│   cpu.max = max
│   memory.max = max
│   memory.current = 23 GiB                            ← total node memory used
│
├── init.scope/                                       ← PID 1
│
├── system.slice/                                     ← OS + agent processes
│   │   cpu.weight = 100 (default — could be raised by enforceNodeAllocatable)
│   │   memory.max = (systemReserved.memory) if enforced
│   │
│   ├── kubelet.service/                              ← the kubelet process
│   │   │   cpu.weight = 100
│   │   │   memory.current = 220 MiB
│   │   │
│   │   └── tasks
│   │
│   ├── containerd.service/
│   ├── sshd.service/
│   ├── systemd-resolved.service/
│   └── ...
│
├── user.slice/                                       ← interactive logins
│
└── kubepods.slice/                                   ← all pods
    │   cpu.weight = 1000 - 100 (system) - 100 (kube)
    │   memory.max = max OR (allocatable.memory) if enforced
    │   memory.current = sum of pod memory.current
    │
    ├── kubepods-besteffort.slice/                    ← BestEffort QoS group
    │   │   cpu.weight = 1
    │   │   memory.max = max
    │   │
    │   ├── kubepods-besteffort-pod<UID>.slice/
    │   └── ...
    │
    ├── kubepods-burstable.slice/                     ← Burstable QoS group
    │   │   cpu.weight = computed (between BestEffort and Guaranteed)
    │   │   memory.max = max
    │   │
    │   ├── kubepods-burstable-pod<UID>.slice/
    │   │   ├── cri-containerd-<pause>.scope/
    │   │   ├── cri-containerd-<init>.scope/
    │   │   └── cri-containerd-<app>.scope/
    │   │           cpu.weight = (request.cpu derived)
    │   │           cpu.max    = (limit.cpu derived) or "max"
    │   │           memory.max = (limit.memory)
    │   │           memory.high = (with MemoryQoS)
    │   │           memory.min  = (with MemoryQoS)
    │   │           pids.max   = podPidsLimit
    │   │           cpuset.cpus = (with static CPU manager: shared pool)
    │   │
    │   └── ...
    │
    └── kubepods-pod<UID>.slice/                      ← Guaranteed pod (direct child!)
        │   cpu.weight = sum of requests
        │   memory.max = sum of limits
        │   cpuset.cpus = 4-7 (pinned)
        │   cpuset.mems = 0   (NUMA pinned)
        │
        └── cri-containerd-<app>.scope/
                cpu.weight = ...
                cpu.max    = ...
                memory.max = ...
```

### 23.1 Pid namespace vs cgroup namespace

Note that the *cgroup* hierarchy is orthogonal to the *PID* namespace. A container has its own PID namespace (PID 1 inside, mapped to some host PID outside). The cgroup namespace controls which slice of the cgroup tree the container sees in its own `/proc/self/cgroup`. The kubelet's containers have cgroup namespaces enabled by default (since 1.20).

---

## 24. Eviction Signals and Thresholds

The kubelet's eviction manager (`pkg/kubelet/eviction/`) periodically polls node-level resource signals and, if any threshold is crossed, evicts pods until pressure resolves. This is the *proactive* counterpart to the kernel's *reactive* OOM killer.

### 24.1 The six core signals

| Signal                  | Source                          | What it means                          |
| ----------------------- | ------------------------------- | -------------------------------------- |
| `memory.available`      | `cgroupfs` (root cgroup)        | bytes of memory free at node level     |
| `nodefs.available`      | `statfs` on root filesystem     | free bytes on /var/lib/kubelet         |
| `nodefs.inodesFree`     | `statfs` on root filesystem     | free inodes on /var/lib/kubelet        |
| `imagefs.available`     | `statfs` on imagefs (if split)  | free bytes on /var/lib/containerd      |
| `imagefs.inodesFree`    | `statfs` on imagefs             | free inodes                            |
| `pid.available`         | `/proc/sys/kernel/pid_max`      | unused PIDs                            |
| `allocatableMemory.available` | aggregated cgroup pod stats | distinct from node memory.available    |

### 24.2 Hard vs soft thresholds

```yaml
evictionHard:                  # cross → immediate eviction (no grace period)
  memory.available: "100Mi"
  nodefs.available: "10%"
  nodefs.inodesFree: "5%"
  imagefs.available: "15%"
  imagefs.inodesFree: "5%"
  pid.available: "10%"
  
evictionSoft:                  # cross → eviction after evictionSoftGracePeriod
  memory.available: "300Mi"
  nodefs.available: "15%"
  pid.available: "15%"

evictionSoftGracePeriod:       # how long the signal must be over threshold
  memory.available: "1m30s"
  nodefs.available: "2m"
  pid.available: "1m30s"

evictionMaxPodGracePeriod: 60   # how long evicted pods get to terminate
evictionPressureTransitionPeriod: 5m   # cooldown after pressure resolved
```

Hard eviction means the kubelet uses `gracePeriodOverride=0` — pods get SIGKILL immediately. Soft eviction means pods get their normal `terminationGracePeriodSeconds`, capped at `evictionMaxPodGracePeriod`.

### 24.3 The decision tree

```
                  ┌──────────────────────────────────┐
                  │  Eviction manager runs every 10s │
                  └────────────────┬─────────────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
   ┌────────────────────┐                  ┌───────────────────────┐
   │  Read signals:     │                  │  Read pod usage from  │
   │  - memory.available│                  │  cgroup stats + statfs│
   │  - nodefs.*        │                  │  per pod              │
   │  - imagefs.*       │                  └───────────┬───────────┘
   │  - pid.available   │                              │
   └────────┬───────────┘                              │
            │                                          │
            ▼                                          │
   ┌────────────────────────────────────────────────┐  │
   │  For each signal:                              │  │
   │    if signal < evictionHard[signal] → HARD     │  │
   │    elif signal < evictionSoft[signal] for      │  │
   │         > evictionSoftGracePeriod → SOFT       │  │
   └────────┬───────────────────────────────────────┘  │
            │                                          │
            ▼                                          ▼
   ┌────────────────────────────────────────────────────────────┐
   │  rank pods by eviction priority (§25):                     │
   │  1. Resource type matches signal (e.g., memory pressure → │
   │     evict by memory usage; disk pressure → by disk usage) │
   │  2. QoS class: BestEffort < Burstable < Guaranteed        │
   │  3. Priority (lower priority dies first)                  │
   │  4. Usage above requests (more overshoot dies first)      │
   │  5. Pod start time (older dies first, last)               │
   └────────┬───────────────────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────────────────────────┐
   │  Pick top victim.                       │
   │  Evict via apiserver delete with        │
   │  appropriate grace period.              │
   │  Update node condition:                 │
   │    MemoryPressure | DiskPressure |      │
   │    PIDPressure                          │
   │  Re-evaluate next tick.                 │
   └─────────────────────────────────────────┘
```

### 24.4 Node conditions during pressure

When the eviction manager detects pressure, it sets node conditions:

```yaml
status:
  conditions:
  - type: MemoryPressure
    status: "True"
    lastHeartbeatTime: "2025-05-23T14:32:01Z"
    reason: KubeletHasInsufficientMemory
  - type: DiskPressure
    status: "False"
  - type: PIDPressure
    status: "False"
```

The scheduler watches these conditions and avoids placing new pods on nodes with `MemoryPressure=True` (BestEffort) or `DiskPressure=True` (everything). The kubelet rejects new pods at admission while pressure persists.

### 24.5 The cooldown

`evictionPressureTransitionPeriod` (default 5m) prevents flapping: once pressure resolves, the kubelet waits 5 minutes before clearing the node condition. Otherwise a node oscillating around the threshold would oscillate the condition, the scheduler would oscillate placements, and so on.

---

## 25. Eviction Ranking: BestEffort First

The eviction manager's ranking algorithm (`pkg/kubelet/eviction/helpers.go`, `rankMemoryPressure` etc.) is roughly:

```go
// Pseudocode of eviction ranking under memory pressure
func rankPodsForMemoryEviction(pods []*v1.Pod) []*v1.Pod {
    sort.SliceStable(pods, func(i, j int) bool {
        // 1. Critical pods never evicted.
        if isCritical(pods[i]) { return false }
        if isCritical(pods[j]) { return true }
        
        // 2. QoS class: BestEffort < Burstable < Guaranteed.
        qi, qj := qos(pods[i]), qos(pods[j])
        if qi != qj {
            return qosOrder(qi) < qosOrder(qj)   // BestEffort first
        }
        
        // 3. Pod priority (lower priority = die first).
        pi, pj := priority(pods[i]), priority(pods[j])
        if pi != pj {
            return pi < pj
        }
        
        // 4. Memory usage relative to request.
        //    More overshoot of request = die first.
        oi := memUsage(pods[i]) - memRequest(pods[i])
        oj := memUsage(pods[j]) - memRequest(pods[j])
        if oi != oj {
            return oi > oj
        }
        
        // 5. Tie-break by pod start time (older survives longer? actually
        //    older first to die — but in practice this rarely tips the scale).
        return pods[i].Status.StartTime.Before(pods[j].Status.StartTime)
    })
    return pods
}
```

For **disk pressure**, the ranking uses ephemeral-storage usage instead of memory.

For **PID pressure**, by process count.

### 25.1 Why "overshoot of request"?

Two Burstable pods, same priority, on the same node, both contributing to memory pressure:

- Pod A: `requests.memory=1Gi, limits.memory=2Gi`, currently using 1.5 GiB.
- Pod B: `requests.memory=1Gi, limits.memory=2Gi`, currently using 1.9 GiB.

A's overshoot = 0.5 GiB. B's overshoot = 0.9 GiB. B dies first. The logic: B is *more responsible* for the pressure because it asked for the same amount but is consuming more.

### 25.2 Guaranteed pods are not invulnerable

A Guaranteed pod is *last* in the eviction queue, but it can still be evicted if:
- The node has only Guaranteed pods and is still under pressure.
- Or the Guaranteed pod itself is using more memory than its limit (which is impossible — the kernel would have OOM-killed it first — but disk usage *can* exceed limit briefly).

Generally a node under pressure with only Guaranteed pods means your scheduler over-promised; this is rare and indicates a configuration bug (e.g., kubeReserved too low).

### 25.3 Eviction events

```
$ kubectl get events --field-selector reason=Evicted
LAST SEEN   TYPE     REASON   OBJECT          MESSAGE
3m12s       Warning  Evicted  pod/foo-abc     The node was low on resource: memory. Container foo was using 1.2Gi, which exceeds its request of 512Mi.
```

These events live in the namespace of the evicted pod. Always shipped to long-term storage; they vanish from etcd after 1 hour by default.

---

## 26. Eviction vs OOM Kill: Proactive vs Reactive

Two mechanisms exist for memory pressure. They look similar but they're different:

| Property                | Eviction                    | OOM kill                  |
| ----------------------- | --------------------------- | ------------------------- |
| Triggered by            | kubelet polling             | kernel detecting pressure |
| Granularity             | whole pod                   | one process in a cgroup   |
| Notice                  | grace period (or zero)      | immediate SIGKILL         |
| Selection               | QoS class + priority + over-request | oom_score_adj      |
| Pod restartPolicy       | respected (pod terminates)  | respected (container restart) |
| Status reason           | `Evicted`                   | `OOMKilled` (container) or kernel msg |
| Triggering threshold    | `evictionHard.memory.available` (e.g., 500Mi free at node) | `memory.max` exceeded for a cgroup |

The relationship:

```
   ↑ memory pressure
   │                            ┌─────────────────────────┐
                                │  cgroup OOM kill        │
                                │  (a pod exceeded its    │
                                │   memory.max — its own  │
                                │   pod-local cgroup)     │
                                └─────────────────────────┘
                                
   ────  evictionHard.memory.available threshold (e.g., 500 MiB) ────
                                
                                ┌─────────────────────────┐
                                │  Kubelet eviction       │
                                │  (node-wide pressure;   │
                                │   kubelet picks victim) │
                                └─────────────────────────┘
                                
   ────  evictionSoft.memory.available threshold (e.g., 1 GiB) ────
                                
                                graceful eviction with delay
                                
   ↓ memory headroom
```

The cgroup OOM is per-pod and happens whenever an *individual* pod exceeds *its own* `memory.max`. It doesn't need node-level pressure. A single pod with `limits.memory: 100Mi` will get OOM-killed when it tries to use 101 MiB, even if the node has 100 GiB of free RAM.

Kubelet eviction is node-wide. It only activates when *aggregate* memory pressure is high. It evicts pods preemptively so the *kernel* OOM never has to fire — the kernel OOM is a last resort that might kill the wrong thing.

### 26.1 The order in a real cascade

1. Node has 64 GiB; 60 GiB used by pods; 4 GiB free.
2. A Burstable pod with `limits.memory: 8Gi` starts allocating fast.
3. As that pod's cgroup approaches its 8 GiB limit, the *cgroup* OOM may fire before the node-level signal moves much. Pod dies. Memory freed.
4. Or: pod is well-behaved, only uses 6 GiB, but two *other* pods grow simultaneously. Node free drops below `evictionHard.memory.available=500Mi`.
5. Kubelet eviction manager fires: picks the worst BestEffort or worst-overshoot Burstable. Evicts. Free memory recovers.
6. Or: eviction is too slow (allocator burns memory at 5 GiB/s). Kernel global OOM fires before kubelet can act. The kernel picks the highest `oom_score_adj` (BestEffort or big-Burstable). That process dies.

The whole design is layered so that the kernel OOM is only invoked when both the kubelet eviction and the cgroup OOM failed to catch it.

---

## 27. Throttling Timeline and cpu.stat

A concrete trace of CPU throttling, end-to-end, observable from the metrics.

```
Pod spec:
  resources:
    requests: { cpu: 100m }
    limits:   { cpu: 400m }
  
cpu.max = "40000 100000"   (40ms quota / 100ms period)
cpu.weight = 4              (from 100m request)

Workload: 4-thread Java app, each thread does ~30ms of CPU work per request.

Period 1: [00ms-100ms]
   t=0    request arrives, 4 threads spin up
   t=0    all 4 threads execute in parallel
   t=10ms each thread has done 10ms wall × 4 = 40ms cgroup CPU; QUOTA HIT
   t=10ms kernel throttles ALL tasks in cgroup until end of period
   t=100ms next period begins; cumulative wall time: 100ms
          per-thread CPU used: 10ms × 4 = 40ms of 120ms requested
          request still not complete

Period 2: [100ms-200ms]
   t=100ms threads resume, do another 10ms each
   t=110ms quota hit again
   t=200ms next period
          per-thread CPU used: 20ms each; still 30ms requested
          
Period 3:
   t=200ms threads resume, do another 10ms
   t=210ms quota hit
   t=300ms next period

Period 4:
   t=300ms threads resume, do final 0ms
   request complete

WALL CLOCK: 300ms for a request that needed 30ms of CPU.
THROTTLED:  3 periods × 90ms = 270ms of throttle.
PERIODS:    3 throttled out of 3 = 100% throttling rate.

cpu.stat after this request:
   nr_periods       3 (more, this is just our delta)
   nr_throttled     3
   throttled_usec   270000
```

The user-visible symptom: P99 latency 300ms for a workload that benchmarks at 30ms in isolation. Root cause: 4 threads × `limits.cpu=400m` = 100m per thread effective; threads complete only 25% as fast as their isolated benchmark.

### 27.1 The metric in Prometheus

```
container_cpu_cfs_throttled_periods_total{container="app",pod="foo"} 12847
container_cpu_cfs_periods_total{container="app",pod="foo"} 14223
```

Throttle rate = 12847 / 14223 = 90.3%.

### 27.2 The PSI signal

```
$ cat /sys/fs/cgroup/kubepods.slice/.../cpu.pressure
some avg10=78.20 avg60=72.14 avg300=68.32 total=482310291
full avg10=2.10 avg60=1.42 avg300=1.18 total=8429101
```

`some avg10=78%` means: over the last 10 seconds, the cgroup had at least one task waiting for CPU 78% of the time. That's a *severe* indicator of CFS throttling or CPU contention.

### 27.3 Prometheus alert

```yaml
- alert: ContainerCPUThrottled
  expr: |
    rate(container_cpu_cfs_throttled_periods_total{container!="",container!="POD",image!=""}[5m]) /
    rate(container_cpu_cfs_periods_total{container!="",container!="POD",image!=""}[5m]) > 0.25
  for: 15m
  labels:
    severity: warning
  annotations:
    summary: "Container {{ $labels.namespace }}/{{ $labels.pod }}/{{ $labels.container }} CFS-throttled"
    description: |
      More than 25% of CFS periods ended in throttle for 15 minutes.
      This usually indicates limits.cpu is too low, or that the workload
      is multi-threaded and exceeds limits in burst.
      Action: either raise limits.cpu, remove the limit, or investigate
      the workload's burst pattern (GC pauses, async I/O storms, etc).
```

---

## 28. In-Place Pod Resize

Historically, changing a pod's resources required deleting and recreating the pod. Since 1.27 (alpha) and 1.32 (GA), Kubernetes supports **in-place resize**: change `resources` on a running pod and have the kubelet adjust cgroup files without restarting.

### 28.1 The `resize` subresource

```bash
$ kubectl patch pod my-pod --subresource resize --patch '
spec:
  containers:
  - name: app
    resources:
      requests: { cpu: "500m", memory: "1Gi" }
      limits:   { cpu: "1",    memory: "2Gi" }
'
```

This goes to a special apiserver subresource (`/api/v1/.../pods/<name>/resize`) that bypasses the usual immutability of `pod.spec.containers[].resources`.

### 28.2 Resize policy

The pod can declare per-resource resize behavior:

```yaml
spec:
  containers:
  - name: app
    resources:
      requests: { cpu: "100m", memory: "256Mi" }
      limits:   { cpu: "500m", memory: "512Mi" }
    resizePolicy:
    - resourceName: cpu
      restartPolicy: NotRequired       # default: in-place
    - resourceName: memory
      restartPolicy: RestartContainer  # required if memory needs restart
```

Memory resize is sometimes risky (JVMs, RocksDB, malloc arenas may not handle dynamic shrink) — the operator can say "for memory changes, restart the container."

### 28.3 What the kubelet does

When the apiserver applies the patch, the kubelet's pod worker:

1. Recomputes the cgroup values.
2. Checks if the node has capacity for the new request (admits or denies).
3. If admitted: writes new `cpu.weight`, `cpu.max`, `memory.max` to the cgroup files.
4. Updates `pod.status.containerStatuses[].resources` to reflect the actual applied values.

For memory *shrinking*: the kubelet attempts to write the new (smaller) `memory.max`. If `memory.current > new memory.max`, the kernel forces reclaim and may OOM-kill. The kubelet can be configured to detect this and refuse the resize.

### 28.4 Resize status conditions

The pod gets two new conditions during a resize:

```yaml
status:
  conditions:
  - type: PodResizePending
    status: "True"
    reason: Deferred
    message: "Node cannot fit new size now"
  - type: PodResizeInProgress
    status: "True"
```

VPA (§29) is the primary consumer of this API. Without VPA, manual in-place resize is rarely useful in production.

---

## 29. VPA Integration (Forward Ref)

The Vertical Pod Autoscaler (`autoscaling.k8s.io/v1`) is the primary consumer of in-place resize. It has three components:

- **Recommender**: watches pod metrics, computes recommended requests/limits from a histogram of past usage (default: 90th percentile + safety margin).
- **Updater**: evicts pods whose current requests differ significantly from recommendations.
- **Admission Controller**: rewrites resource requests on pod create, using the recommender's output.

With in-place resize (1.32+), VPA *Updater* can resize in-place instead of evicting — much smoother, no downtime.

Chapter 22 covers VPA in full. The key interaction with this chapter: VPA reads the metrics we discuss in §31, applies the right-sizing heuristic from §30, and writes back into the pod's `resources` block.

A non-obvious caveat: VPA conflicts with HPA on the same metric. HPA scales *replicas* based on CPU usage; VPA changes *requests*, which changes the *base* of the HPA's "% of request" metric. Result: oscillation. Mitigation: VPA in `Off` (recommend-only) mode + HPA controlling replicas; or VPA on memory + HPA on CPU.

---

## 30. The Right-Sizing Workflow

How do you actually decide what numbers to put in your spec? The workflow that works:

### 30.1 Measure

Deploy with *generous* requests and *no* CPU limits (or a generous one) for at least a week. Capture:

- p50, p95, p99 CPU usage (from `container_cpu_usage_seconds_total`).
- p50, p95, p99 memory working set (from `container_memory_working_set_bytes`).
- Throttling rate (from `container_cpu_cfs_throttled_periods_total`).

```promql
# p99 CPU usage over 7 days
quantile_over_time(0.99,
  rate(container_cpu_usage_seconds_total{namespace="prod",pod=~"my-app-.*"}[5m])[7d:5m])

# p99 memory working set
quantile_over_time(0.99,
  container_memory_working_set_bytes{namespace="prod",pod=~"my-app-.*"}[7d])
```

### 30.2 Set requests

- `requests.cpu` ≈ **p99 of measured CPU usage** (or p95, depending on how aggressive you want bin-packing). This guarantees scheduler-promised CPU = peak-needed CPU.
- `requests.memory` ≈ **p99 of working-set memory**, rounded up.

For predictable workloads, requests = p99 means you essentially never hit contention. For bursty workloads, you may want `requests = p50` and rely on `cpu.weight` + headroom to absorb bursts.

### 30.3 Set limits

- `limits.cpu`: **omit** for latency-sensitive services. Set to `2 × requests` for cap-able batch.
- `limits.memory` ≈ **1.5 × requests.memory**. The 50% headroom is your safety margin: it accommodates allocator overhead, GC peaks, and occasional anomalies, while still bounding worst-case node impact.

### 30.4 Iterate

After deploying with these numbers, re-measure for a week. Watch:
- `container_oom_events_total` (memory limit too tight).
- `kube_pod_container_status_terminated_reason{reason="OOMKilled"}` (same, with backoff).
- `container_cpu_cfs_throttled_periods_total` (CPU limit too tight).
- Evictions (`kubelet_evictions{eviction_signal=...}`).

If any of those fire, adjust.

### 30.5 The pattern in YAML

```yaml
spec:
  containers:
  - name: api
    image: my/api:1.2.3
    resources:
      requests:
        cpu: "500m"        # ~p95 measured usage
        memory: "1Gi"      # ~p99 measured working set
      limits:
        # no cpu limit (latency-sensitive)
        memory: "1500Mi"   # 1.5× request
```

For batch:

```yaml
spec:
  containers:
  - name: trainer
    image: my/trainer:1.0
    resources:
      requests:
        cpu: "4"
        memory: "16Gi"
      limits:
        cpu: "8"           # cap; batch can be throttled
        memory: "24Gi"
```

---

## 31. Observability: Metrics, Alerts, Dashboards

### 31.1 The essential metrics

**From kube-state-metrics** (object-level):
- `kube_pod_container_resource_requests{resource="cpu"}`
- `kube_pod_container_resource_requests{resource="memory"}`
- `kube_pod_container_resource_limits{resource="cpu"}`
- `kube_pod_container_resource_limits{resource="memory"}`
- `kube_pod_status_qos_class`
- `kube_resourcequota{resource="...",type="used"}` and `type="hard"`

**From cAdvisor** (`/metrics/cadvisor`) (actual usage):
- `container_cpu_usage_seconds_total` (counter)
- `container_memory_working_set_bytes` (gauge, the "RSS that counts")
- `container_memory_rss` (gauge, the "RSS without page-cache")
- `container_memory_cache` (gauge)
- `container_memory_max_usage_bytes` (gauge, the high-watermark)
- `container_cpu_cfs_throttled_periods_total`
- `container_cpu_cfs_throttled_seconds_total`
- `container_cpu_cfs_periods_total`
- `container_oom_events_total`
- `container_fs_usage_bytes`
- `container_fs_inodes_free`

**From kubelet** (`/metrics`):
- `kubelet_evictions{eviction_signal="memory.available"}`
- `kubelet_node_name` (target labeling)
- `kubelet_running_pods`
- `kubelet_running_containers`

**From the kernel / node-exporter**:
- `node_memory_MemAvailable_bytes`
- `node_filesystem_avail_bytes`
- `node_filesystem_inodes_free`
- `node_filesystem_files_free`

### 31.2 Essential alerts

```yaml
groups:
- name: pod-resources
  rules:
  
  - alert: PodMemoryUsageNearLimit
    expr: |
      (container_memory_working_set_bytes{container!="",container!="POD"}
       /
       on(pod, container, namespace) kube_pod_container_resource_limits{resource="memory"}) > 0.9
    for: 10m
    labels: { severity: warning }
    annotations:
      summary: "Pod {{ $labels.namespace }}/{{ $labels.pod }} near memory limit"
      description: |
        Container '{{ $labels.container }}' is using >90% of its memory limit
        for 10 minutes. OOMKill is likely. Investigate or raise limit.
  
  - alert: PodOOMKilled
    expr: |
      increase(container_oom_events_total[5m]) > 0
    labels: { severity: critical }
    annotations:
      summary: "Pod {{ $labels.namespace }}/{{ $labels.pod }} OOMKilled"
      description: "OOM event detected; check memory.max sizing."
  
  - alert: PodCPUThrottlingHigh
    expr: |
      (rate(container_cpu_cfs_throttled_periods_total[5m])
       /
       rate(container_cpu_cfs_periods_total[5m])) > 0.25
    for: 15m
    labels: { severity: warning }
    annotations:
      summary: "Pod {{ $labels.namespace }}/{{ $labels.pod }} CPU-throttled"
      description: |
        Container '{{ $labels.container }}' has been throttled >25% of periods
        for 15m. Investigate limit sizing or remove limit for this workload.
  
  - alert: NodeMemoryPressure
    expr: kube_node_status_condition{condition="MemoryPressure",status="true"} == 1
    for: 5m
    labels: { severity: warning }
    annotations:
      summary: "Node {{ $labels.node }} under memory pressure"
      description: "Eviction manager has marked the node MemoryPressure=True."
  
  - alert: NodeDiskPressure
    expr: kube_node_status_condition{condition="DiskPressure",status="true"} == 1
    for: 5m
    labels: { severity: warning }
    annotations:
      summary: "Node {{ $labels.node }} under disk pressure"
  
  - alert: NodePIDPressure
    expr: kube_node_status_condition{condition="PIDPressure",status="true"} == 1
    for: 5m
    labels: { severity: warning }
    annotations:
      summary: "Node {{ $labels.node }} under PID pressure"
  
  - alert: ResourceQuotaNearLimit
    expr: |
      (kube_resourcequota{type="used"} / on(namespace,resource,resourcequota)
       kube_resourcequota{type="hard"}) > 0.9
    for: 30m
    labels: { severity: warning }
    annotations:
      summary: "ResourceQuota {{ $labels.namespace }}/{{ $labels.resourcequota }} near limit"
      description: "{{ $labels.resource }} usage > 90% of quota."
  
  - alert: PodEvicted
    expr: |
      increase(kubelet_evictions{eviction_signal!=""}[10m]) > 0
    labels: { severity: warning }
    annotations:
      summary: "Pods evicted on node {{ $labels.node }} (signal: {{ $labels.eviction_signal }})"
      description: "Investigate which workloads are over-promised."
  
  - alert: NodeAllocatableExhausted
    expr: |
      sum by (node) (kube_pod_container_resource_requests{resource="memory"})
      /
      kube_node_status_allocatable{resource="memory"} > 0.95
    for: 15m
    labels: { severity: warning }
    annotations:
      summary: "Node {{ $labels.node }} memory >95% promised"
      description: "Requests exhausting allocatable; pods may not schedule."
```

### 31.3 The "requests vs usage" right-sizing dashboard

A single dashboard with two side-by-side panels per workload:

- Panel 1: `kube_pod_container_resource_requests{resource="cpu"}` plotted against `rate(container_cpu_usage_seconds_total[5m])`. Big gap (request >> usage) = waste. No gap (usage tracking request) = sized correctly. Usage > request = under-promised; either raise request or expect throttling.
- Panel 2: same for memory.

VPA recommendations can be plotted alongside as a third line. This dashboard pays for itself in cluster compute savings within a month.

---

## 32. Pitfalls

The 25+ ways resource management goes wrong in production. Some of these come up once a year; some come up every week.

### 32.1 No requests — every node looks "free"

The scheduler treats unspecified `requests.cpu`/`requests.memory` as 0. Workload runs, uses real CPU and memory, but the node's `allocatable - requested` stays at "almost full" because nothing is reserved. More pods get scheduled. Node hits eviction territory. Pods get evicted (BestEffort first). Pager fires at 3am. → Always set requests. Use LimitRange to provide defaults.

### 32.2 CPU limit causing tail latency

The single most-common subtle bug. Multi-threaded app + `limits.cpu` set tight + occasional bursts = CFS throttles entire app for the rest of the 100ms period, blowing tail latency. → Remove CPU limits on latency-sensitive workloads. Memory limits stay.

### 32.3 Memory limit at working-set size — guaranteed OOM

Setting `limits.memory = avg_working_set` means the first GC pause / cache spike / connection storm causes an OOM. → Limit ≈ 1.5× p99 working set. Or: don't set a limit and rely on eviction (but then a single bad pod can take down the node).

### 32.4 QoS BestEffort in production

A team copy-pastes a Helm chart without resource blocks. Pod is BestEffort. Eviction manager picks it as the first victim under any pressure. Service goes into CrashLoopBackOff. → Use `LimitRange` to default to Burstable.

### 32.5 Missing ephemeral-storage requests

Pod writes 50 GiB of logs into `/var/log` (which is in the writable layer). Node's `nodefs.available` drops. Eviction fires, kills *other* pods. → Set `requests.ephemeral-storage` and `limits.ephemeral-storage`; ship logs externally.

### 32.6 Low podPidsLimit

`podPidsLimit: 1024` looked safe until a Java app spawned 800 threads. `pids.max` hit; new thread → `fork()` returns `EAGAIN`; app crashes in non-obvious ways (typically NIO selector init failure). → Default to 4096, raise for heavy-thread apps.

### 32.7 cgroup-v1 vs v2 differences

Old kernels use cgroup-v1. `memory.high` is v2-only. CFS quota semantics are subtly different (v1: `cpu.cfs_quota_us` and `cpu.cfs_period_us`; v2: `cpu.max` is one file). Tools (kubectl-tree, kubectl-resource, custom scripts) that hardcode v1 paths break on v2. → Standardize on v2; require kernel ≥5.8.

### 32.8 LimitRange too strict — pod creation fails

LimitRange with `max.cpu: 1` prevents *any* legitimate compute-heavy pod from being created in the namespace. Error: `pods "x" is forbidden: maximum cpu usage per Container is 1, but limit is 4`. → LimitRange is a guardrail, not a budget; size it generously, use ResourceQuota for actual budgets.

### 32.9 ResourceQuota counts Terminating pods

A Job creates a pod that's `Completed` but not yet garbage-collected; it still counts against `requests.cpu` in the quota. New job submission fails. → Use `ttlSecondsAfterFinished` on Jobs, or a quota scope `NotTerminating` to exclude them.

### 32.10 Object-count quota exhaustion on PVC churn

`persistentvolumeclaims: 50` quota. CI creates 50 PVCs/hour with `ttlSecondsAfterFinished: 0` Jobs. PVCs not GC'd in time. Quota saturates. New CI runs fail. → Lower TTL, raise quota, or use `ephemeral` PVCs.

### 32.11 PriorityClass without resource budget

Setting high priority on a Deployment means the scheduler will preempt to fit it. If you forgot a ResourceQuota scoped to that PriorityClass, one rogue user can preempt *the entire cluster*. → PriorityClass + PriorityClass-scoped quota together.

### 32.12 CPU manager state file lost

Node reboots; `/var/lib/kubelet/cpu_manager_state` is on tmpfs and disappears. Kubelet starts in static mode with no state; existing containers' `cpuset.cpus` is wrong. Mismatched accounting. → State file lives on disk; verify on every reboot.

### 32.13 Topology manager rejecting pods on small nodes

`single-numa-node` policy on a single-socket node = fine. On a 2-socket node where a pod requests 12 cores but each NUMA has only 8 = perpetual rejection. → Use `best-effort` unless you've verified your workload genuinely benefits from strict locality.

### 32.14 Underestimating /var/log

Verbose logging + log rotation slack + crash dumps + tmpfile cleanup race + uncaught exceptions printing stack traces = `/var/log` at 90%. `nodefs.available` triggers eviction of *unrelated* pods. → Centralize logs (Loki, Fluent Bit shipping to S3), or split `/var/log` to its own filesystem.

### 32.15 Overcommit ratio in autoscaler

Cluster Autoscaler scales nodes based on *unscheduled* pods (= pending requests). If you've overcommitted (limits >> requests) and CPU usage spikes to *limit*, the autoscaler doesn't know — it sees requests satisfied. Throttling explodes; latency degrades. The autoscaler should be sized off *usage*, not *requests*, for this case. → KEDA + Prometheus-based custom autoscaler for usage-based scaling.

### 32.16 Init container resource bookkeeping

For QoS computation, every init container counts. For *scheduling*, the kubelet uses `max(max init container request, sum of regular container requests)`. So an init container with `requests.cpu: 4` reserves 4 CPUs at scheduling but releases them after init completes. → Don't over-request on init containers; it inflates apparent reservation.

### 32.17 Mixed kubeletReserved enforcement

Setting `enforceNodeAllocatable: ["pods", "kube-reserved"]` puts a hard cgroup memory limit on the kubelet itself. If the kubelet leaks (e.g., a pod with thousands of containers + status manager backlog), the *kubelet* gets OOM-killed. Node dies. → Default to `["pods"]` only unless you have rigorous kubelet memory monitoring.

### 32.18 hugepages reservation without NUMA awareness

Reserving hugepages at boot via `hugepages=N` puts them all on NUMA node 0. Pods scheduled to NUMA-1 cores get remote memory access. → Use per-node reservation via `/sys/devices/system/node/node*/hugepages/...`.

### 32.19 GPU not visible to topology manager pre-1.20

Older device plugins didn't report NUMA topology. Topology manager couldn't align CPU + memory to GPU. → Update device plugins, enable `KubeletPodResourcesGetAllocatable` and `CPUManagerPolicyAlphaOptions=full-pcpus-only` for proper alignment.

### 32.20 ResourceQuota race during admission

Two concurrent pod creates can both pass quota check at admission and both be admitted, briefly exceeding quota. The controller reconciles `status.used` shortly after. Rare but causes paging if the alert is "quota exceeded > 0". → Allow a small margin (5%) in alerts.

### 32.21 Pod with only init container counting in QoS

A pod with one init container that has full requests/limits, and *no* regular containers (rare but legal — restartable init containers / sidecars from 1.28+) — its QoS is computed correctly, but the kubelet's pod cgroup machinery has historically been confused. Pre-1.30, edge cases in cgroup writes. → Use 1.30+ for sidecar-heavy designs.

### 32.22 `oom_score_adj` not inheriting

The kubelet sets `oom_score_adj` on the container's main PID via the CRI. Children inherit at fork time. But: a process that calls `prctl(PR_SET_DUMPABLE, 0)` or runs setuid loses inheritance. The OOM killer may then pick a child with `oom_score_adj=0` instead of the intended -997. → Audit containers that exec setuid binaries; consider hardening with `seccomp` to forbid those paths.

### 32.23 In-place resize on a deployment without RollingUpdate

VPA in `Auto` mode + Deployment with `Recreate` strategy = VPA evicts all pods at once when resizing. → Use RollingUpdate or VPA `Initial` mode.

### 32.24 Memory.high spike during JVM startup

JVMs allocate aggressively during class-loading. With MemoryQoS enabled, hitting `memory.high` throttles the allocator → JVM startup takes 3× longer. → Disable MemoryQoS for JVM-heavy clusters, or pre-warm with `JAVA_OPTS=-XX:+AlwaysPreTouch -Xmx<limit>`.

### 32.25 Eviction thresholds vs node-exporter memory accounting

Node-exporter's `node_memory_MemAvailable_bytes` includes reclaimable page-cache as "available". The kubelet's `memory.available` signal can be different (it reads from cgroup root `memory.current`). Alert on the kubelet's view, not node-exporter's, for eviction-relevance. → `kubelet_*` metrics, not `node_*`, for eviction thresholds.

### 32.26 Cron jobs creating ephemeral pods at quota edge

CronJob with concurrencyPolicy=Allow and quota tight on `pods: 100`. Two cron runs overlap during a slow execution; one of them can't schedule (`pods` quota exceeded). Silently fails forever until investigation. → concurrencyPolicy=Forbid or quota with generous headroom.

### 32.27 `kubectl top` vs cgroup truth

`kubectl top pod` uses `/metrics/resource` (lightweight) which is updated every 10 seconds. Spikes shorter than 10s never appear. For sub-second insight, scrape cAdvisor directly or use kubelet `/stats/summary`. → Don't make decisions based on `kubectl top`.

### 32.28 ServiceAccount token projection counts as ephemeral storage?

No. Projected service account tokens are in tmpfs. But many people *think* they count, and over-provision ephemeral-storage. → Don't be that person.

### 32.29 Node drain leaves Guaranteed pods stuck

A drain operation tries to evict pods. Guaranteed pods with PDBs blocking eviction → stuck. → Set PDBs with realistic minAvailable, or `--delete-emptydir-data --force` (with care).

### 32.30 Resource fragmentation at the cluster level

20 nodes each with 1 CPU free, but a pod needs 4. Scheduler sees 20 CPUs total free — but can't fit. Pod stays Pending forever. → Bin-pack proactively (scheduler `MostAllocated` scoring) or use Karpenter to consolidate.

---

## 33. TL;DR

Resource management in Kubernetes is two knobs at two layers:

- **`requests`** drive *scheduling*. They count against the node's `Allocatable = Capacity − kubeReserved − systemReserved − evictionHard`. `requests.cpu` also becomes `cpu.weight` in the cgroup (proportional share under contention). `requests.memory` becomes `memory.min` only with the MemoryQoS feature gate.
- **`limits`** drive *enforcement* by the kernel. `limits.cpu` → `cpu.max` (CFS quota/period throttling, default period 100ms). `limits.memory` → `memory.max` (hard cap, cgroup OOM on exceed).

The three QoS classes are computed from the spec, not user-declared:

- **Guaranteed**: every container has CPU and memory requests == limits. `oom_score_adj = -997`. Last to die. Eligible for static CPU manager pinning if `requests.cpu` is an integer.
- **Burstable**: anything else with at least one request or limit. `oom_score_adj` scales 2–999 by memory-request fraction of node capacity.
- **BestEffort**: no requests or limits anywhere. `oom_score_adj = 1000`. First to die. Don't use in production.

The cgroup-v2 tree under `/sys/fs/cgroup/kubepods.slice/` has three branches: `kubepods-besteffort.slice/`, `kubepods-burstable.slice/`, and direct children for Guaranteed pods. Each pod is a slice; each container is a scope. The kubelet writes `cpu.weight`, `cpu.max`, `memory.max`, optionally `memory.min`/`memory.high`, `pids.max`, and (with managers) `cpuset.cpus`, `cpuset.mems`.

**CPU throttling** is the silent killer. CFS quota throttles a cgroup *even if the rest of the node is idle*. Multi-threaded apps blow through quota in a fraction of a period and wait. `container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total > 25%` for 15 minutes is a "fix it now" alert. The fix is usually **remove the CPU limit**; rely on `cpu.weight` from requests for fairness.

**Memory limits are mandatory** in production. Without them, a runaway pod takes down the node. With them, the cgroup OOM contains the blast radius. Set limit ≈ 1.5× p99 working-set.

**The static CPU manager** pins integer-CPU Guaranteed pods to exclusive CPUs. The **memory manager** does the NUMA-equivalent. The **topology manager** is the arbiter that merges hints into a single NUMA placement, with policies `none`, `best-effort`, `restricted`, `single-numa-node`, at scope `container` or `pod`. Strict policies reject pods that can't be NUMA-aligned; use `best-effort` unless you've measured the benefit.

**ResourceQuota** caps aggregate resources per namespace. **LimitRange** sets per-object defaults and bounds. Quotas can be scoped (BestEffort, Terminating, PriorityClass) so that different workload classes have separate budgets. **PriorityClass + preemption** lets high-priority pods kick out low-priority ones.

**Extended resources** (`nvidia.com/gpu` etc.) are integer-only and require `requests == limits`. **Hugepages** are reserved at boot and consumed via `hugepages-2Mi`/`hugepages-1Gi` resources. **Ephemeral storage** counts container writable layer + emptyDirs + logs. **PID limits** (`podPidsLimit`, default unlimited; `pid.available` eviction signal) prevent fork bombs.

**Eviction** is the kubelet's proactive defense against node pressure, distinct from the kernel's reactive OOM. Six signals (`memory.available`, `nodefs.available`, `nodefs.inodesFree`, `imagefs.available`, `imagefs.inodesFree`, `pid.available`), each with hard and soft thresholds. Ranking: BestEffort → Burstable → Guaranteed, then by priority, then by overshoot of request. The eviction manager sets node conditions (`MemoryPressure`, `DiskPressure`, `PIDPressure`) that the scheduler avoids.

**In-place pod resize** (GA in 1.32) lets you change `resources` on a running pod via the `resize` subresource without restarting (unless `resizePolicy` demands a restart for memory). VPA is the primary consumer.

**The right-sizing workflow**: measure for a week with generous defaults, set `requests.cpu` ≈ p95–p99 measured CPU, `requests.memory` ≈ p99 working-set, `limits.memory` ≈ 1.5× request, omit `limits.cpu` for latency-sensitive services. Iterate.

**Observability** is non-negotiable: alert on OOMKilled, throttling >25%, memory >90% of limit, node MemoryPressure/DiskPressure/PIDPressure, ResourceQuota >90%, eviction count >0. `container_cpu_cfs_throttled_periods_total`, `container_memory_working_set_bytes`, `container_oom_events_total`, `kubelet_evictions{eviction_signal=...}`, `kube_pod_container_resource_*` are the five metric families you must scrape.

The sentence to remember: **requests buy you scheduling and a CPU share; limits buy you a CPU ceiling and a memory ceiling; CPU ceilings hurt more than they help; memory ceilings are non-negotiable; and the kubelet's eviction manager will save the node before the kernel OOM has to.**

Next: chapter 22 covers **autoscaling** — HPA scales replicas based on the metrics we measured here, VPA changes the requests we set here (via in-place resize), Cluster Autoscaler / Karpenter grow nodes when `requests` exhaust `Allocatable`. Every autoscaler is downstream of the choices in this chapter.
