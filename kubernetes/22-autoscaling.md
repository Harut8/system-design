# Autoscaling: HPA, VPA, Cluster Autoscaler, Karpenter, KEDA

Kubernetes ships with three orthogonal autoscaling axes and a thriving ecosystem layered on top of them. The first axis is **pod replica count** — when the work coming in grows, run more copies of the workload. The second is **pod resource size** — give each replica more CPU and memory, or take excess back. The third is **node count** — when the scheduler can't place all the pods, buy more machines; when nodes sit idle, return them to the cloud. Each axis has its own controller, its own metrics pipeline, its own failure modes, and its own pitfalls. They run independently, they make decisions on different time horizons, and unless you reason about them as a single closed-loop control system, they will fight each other, cause outages, or quietly burn money.

This chapter is the staff-engineer's tour of the autoscaling stack. We start with the **Horizontal Pod Autoscaler** (HPA): the original `autoscaling/v1` API, the `autoscaling/v2` metrics types (`Resource`, `Pods`, `Object`, `External`, `ContainerResource`), the reconcile loop, the `desiredReplicas` formula and its multi-metric `max()`, and the behavior block that ended a decade of oscillation complaints. We then dive into the **metrics pipeline** — `metrics.k8s.io`, `custom.metrics.k8s.io`, `external.metrics.k8s.io` — and how `metrics-server`, `prometheus-adapter`, and KEDA's metrics-api adapter actually answer those queries. We cover the **Vertical Pod Autoscaler** (VPA), its three-controller architecture (Recommender / Updater / AdmissionController), the histogram-based recommendation algorithm, and why pre-1.27 VPA in `Auto` mode is essentially a controlled outage. We dissect the **Cluster Autoscaler** (CA), its node-group abstraction, the simulation it runs against unschedulable pods, the six expanders (`random`, `most-pods`, `least-waste`, `price`, `priority`, `grpc`), and the surprisingly conservative scale-down logic. Then we move to **Karpenter**, the AWS-born (now CNCF) replacement that throws away node groups, picks instance types per pod, and adds consolidation and drift as first-class operations. Finally **KEDA**, which extends the HPA from "metric numbers" to "any external event source" — Kafka lag, SQS depth, cron schedules — and unlocks both scale-from-zero and the `ScaledJob` pattern.

The chapter sits between [ch 09 (kube-scheduler)](09-kube-scheduler-internals.md), which decides *where* a pod runs, and [ch 12 (workload controllers)](12-workload-controllers.md), which owns the Deployment/StatefulSet `replicas` field that the HPA mutates. Sibling chapter [ch 08 (controller pattern)](08-controller-pattern-and-client-go.md) is the prerequisite mental model: every autoscaler in this chapter is just another controller running a reconcile loop against the API server. If you only remember one sentence: **HPA scales the replica count, VPA scales the per-pod size, CA/Karpenter scales the node count, KEDA scales on events, and the only thing they all share is that none of them owns the resource they appear to control — they patch the `scale` subresource, the pod's request, or call a cloud API, and then trust the rest of the system to converge.**

---

## Table of Contents

1.  [The Three Autoscaling Axes](#1-the-three-autoscaling-axes)
2.  [HorizontalPodAutoscaler: the autoscaling/v2 API](#2-horizontalpodautoscaler-the-autoscalingv2-api)
3.  [The HPA Reconcile Loop](#3-the-hpa-reconcile-loop)
4.  [The desiredReplicas Formula](#4-the-desiredreplicas-formula)
5.  [HPA Metric Types in Depth](#5-hpa-metric-types-in-depth)
6.  [The Metrics API Federation](#6-the-metrics-api-federation)
7.  [metrics-server: Resource Metrics in Practice](#7-metrics-server-resource-metrics-in-practice)
8.  [Custom Metrics with prometheus-adapter](#8-custom-metrics-with-prometheus-adapter)
9.  [External Metrics and KEDA's Metrics Adapter](#9-external-metrics-and-kedas-metrics-adapter)
10. [HPA Behavior: scaleUp, scaleDown, and Stabilization](#10-hpa-behavior-scaleup-scaledown-and-stabilization)
11. [ContainerResource: Per-Container Autoscaling](#11-containerresource-per-container-autoscaling)
12. [Multi-Metric HPAs and the max() Rule](#12-multi-metric-hpas-and-the-max-rule)
13. [Vertical Pod Autoscaler Architecture](#13-vertical-pod-autoscaler-architecture)
14. [VPA Recommendation Algorithm](#14-vpa-recommendation-algorithm)
15. [VPA Update Modes and In-Place Resize](#15-vpa-update-modes-and-in-place-resize)
16. [HPA + VPA: The Coexistence Rule](#16-hpa--vpa-the-coexistence-rule)
17. [Cluster Autoscaler Architecture](#17-cluster-autoscaler-architecture)
18. [CA Scale-Up: Simulating the Scheduler](#18-ca-scale-up-simulating-the-scheduler)
19. [CA Scale-Down: Underutilization and Safety](#19-ca-scale-down-underutilization-and-safety)
20. [CA Expanders: How a Node Group Is Chosen](#20-ca-expanders-how-a-node-group-is-chosen)
21. [CA Scale-from-Zero](#21-ca-scale-from-zero)
22. [Karpenter Architecture and NodePool](#22-karpenter-architecture-and-nodepool)
23. [Karpenter Consolidation and Drift](#23-karpenter-consolidation-and-drift)
24. [Karpenter vs Cluster Autoscaler: Tradeoffs](#24-karpenter-vs-cluster-autoscaler-tradeoffs)
25. [KEDA: Event-Driven Autoscaling](#25-keda-event-driven-autoscaling)
26. [KEDA Scalers and Triggers](#26-keda-scalers-and-triggers)
27. [ScaledJob: Per-Event Jobs](#27-scaledjob-per-event-jobs)
28. [Scale-from-Zero Semantics](#28-scale-from-zero-semantics)
29. [Predictive and Custom Autoscaling](#29-predictive-and-custom-autoscaling)
30. [The Closed-Loop Feedback System](#30-the-closed-loop-feedback-system)
31. [Scale-Up Latency Breakdown](#31-scale-up-latency-breakdown)
32. [Cross-Component Interactions and Failure Modes](#32-cross-component-interactions-and-failure-modes)
33. [Cost Optimization in Practice](#33-cost-optimization-in-practice)
34. [Observability: Metrics and Dashboards](#34-observability-metrics-and-dashboards)
35. [Pitfalls](#35-pitfalls)
36. [TL;DR](#36-tldr)

---

## 1. The Three Autoscaling Axes

Autoscaling on Kubernetes is not one feature; it is three independent dimensions plus one event-driven adapter on top.

```
                ┌──────────────────────────────────────────────────────────┐
                │              THREE AUTOSCALING AXES                       │
                └──────────────────────────────────────────────────────────┘

  Axis 1: REPLICA COUNT          Axis 2: POD SIZE             Axis 3: NODE COUNT
  ─────────────────────          ────────────────             ──────────────────
   HPA  (and KEDA)                VPA                          CA  /  Karpenter

   patches .spec.replicas         patches container             calls cloud API to
   on Deployment / RS /           .resources.requests           add or remove VMs;
   StatefulSet via the            (and limits, optionally)      tracks node groups
   scale subresource              on the pod template           or, in Karpenter,
                                                                provisions per-pod

      ┌──────────┐                   ┌──────────┐                ┌──────────┐
      │ replicas │                   │  cpu:    │                │  nodes:  │
      │  3 →  9  │                   │  100m →  │                │  4 →  6  │
      │          │                   │  500m    │                │          │
      └──────────┘                   └──────────┘                └──────────┘

  Signal: CPU/mem,                 Signal: 8-day                 Signal: pending
          custom, ext              histogram of                  pods (CA) or
                                   actual usage                  pod requirements
                                                                 (Karpenter)

  Horizon: seconds                 Horizon: minutes              Horizon: minutes
                                   to hours                      to hours
```

**Axis 1 — replica count (horizontal pod autoscaling).** When CPU rises from 50% to 80%, you want more replicas of the same workload. The HPA reads a metric, computes a desired replica count, and patches the `scale` subresource on a `Deployment`, `ReplicaSet`, `StatefulSet`, or any CRD that implements `/scale`. Note what it does *not* do: it does not create pods, it does not delete pods, it does not pick which pod to delete on scale-down. All of that is the workload controller's job (ch 12). The HPA writes a number; the rest of the system reacts.

**Axis 2 — pod resource size (vertical pod autoscaling).** When a pod consistently uses 700 mCPU but its `requests.cpu` is `100m`, you have a binpacking lie: the scheduler thinks it's a small pod, the kubelet's cgroup is set for a small pod, but the workload behaves like a large one. The VPA observes actual usage over days, fits a histogram, and rewrites `requests` (and optionally `limits`) to match reality. It does *not* change replica count. Its time horizon is longer than the HPA's because resource right-sizing is a long-tail decision: you don't want to bounce a request from 500m to 800m to 400m every minute.

**Axis 3 — node count (cluster autoscaling).** Both of the above produce pods. Pods need nodes. When the scheduler can't place a pod because no node has enough CPU/memory/GPU/storage, the pod sits in `Pending`. The cluster autoscaler (CA) or Karpenter notices, decides which instance type and how many to add, and calls the cloud provider's API. On the way down, when nodes sit underutilized, the same component drains them and asks the cloud to terminate. CA does this through pre-configured node groups (ASGs on AWS, MIGs on GCP, VMSS on Azure); Karpenter does it per-pod with no node groups at all.

**KEDA is the fourth thing** — but it is *not* a fourth axis. KEDA extends the HPA's metric source from "CPU and memory" to "Kafka consumer lag, SQS queue depth, cron schedules, Prometheus expressions, anything." Internally KEDA creates an HPA, registers itself as an `external.metrics.k8s.io` provider, and lets the HPA do the actual scaling work. From the HPA's perspective, KEDA is just another metrics adapter. From the user's perspective, KEDA is the *only* way to scale on events because the HPA itself can only consume metrics from the standardized APIs.

### The cardinal rule: never autoscale on the same metric in two places

If the HPA scales replicas on CPU and the VPA scales requests on CPU, the system has a positive feedback loop. The HPA sees high CPU per pod, adds replicas, average CPU per pod drops. The VPA sees lower CPU and shrinks requests. Now each pod has fewer requests, so the kernel gives each one more headroom on the same node — but the HPA *also* sees lower CPU (relative to the new smaller request) and scales replicas down. The pods that survive now spike again because there are fewer of them, and the cycle begins again, this time in the opposite direction. **The only safe pattern is HPA-on-CPU + VPA-recommendation-only** — the VPA emits recommendations but does not apply them; humans (or a separate batch process) apply them at known-safe times. We will return to this in §16.

### What this chapter assumes

You have read ch 08 (the controller pattern: every autoscaler is a controller), ch 09 (the scheduler, which is what produces "pending" pods that CA reacts to), ch 11 (pod internals, especially the `resources.requests` field), and ch 12 (workload controllers, which own the `scale` subresource that HPA patches). If you don't know what `scale` is, the rest of this chapter will read as magic.

---

## 2. HorizontalPodAutoscaler: the autoscaling/v2 API

The HPA exists in three API versions, and the older ones still ship — but only the newest matters for new code.

| API version | Status | Capability |
|-------------|--------|------------|
| `autoscaling/v1` | Legacy, still served | Only CPU utilization. No memory, no custom metrics, no behavior. |
| `autoscaling/v2beta1` | Removed in 1.22 | First multi-metric, first custom metrics. Historical. |
| `autoscaling/v2beta2` | Removed in 1.26 | Added `behavior`. Historical. |
| `autoscaling/v2` | GA since 1.23 | The version you use. |

Everything in this chapter assumes `autoscaling/v2`. If you find an `autoscaling/v1` HPA in your repo, treat it as tech debt: it cannot express memory, cannot express multi-metric, cannot express behavior, and the kubectl pretty-printer silently lies about its semantics.

### The canonical HPA manifest

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: checkout-api
  namespace: shop
spec:
  scaleTargetRef:                          # what to scale
    apiVersion: apps/v1
    kind: Deployment
    name: checkout-api
  minReplicas: 3
  maxReplicas: 50
  metrics:                                 # one or more signals; max() wins
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Resource
    resource:
      name: memory
      target:
        type: AverageValue
        averageValue: 600Mi
  - type: Pods
    pods:
      metric:
        name: http_requests_per_second
      target:
        type: AverageValue
        averageValue: "200"
  - type: External
    external:
      metric:
        name: sqs_queue_depth
        selector:
          matchLabels:
            queue: checkout-events
      target:
        type: Value
        value: "1000"
  behavior:                                # rate-limiting on scale decisions
    scaleUp:
      stabilizationWindowSeconds: 0        # react immediately on the way up
      policies:
      - type: Percent
        value: 100                          # up to 100% more pods
        periodSeconds: 60
      - type: Pods
        value: 4                            # or up to 4 more pods
        periodSeconds: 60
      selectPolicy: Max                     # whichever policy permits more
    scaleDown:
      stabilizationWindowSeconds: 300       # 5 min cool-down on the way down
      policies:
      - type: Percent
        value: 10                            # at most 10% fewer pods
        periodSeconds: 60
      selectPolicy: Max
```

Four things are worth pointing out about this manifest before we dissect each field.

**`scaleTargetRef`** is *not* a label selector. The HPA does not select pods. It selects *the resource that owns the replica count*, and that resource must implement the `scale` subresource (`/apis/apps/v1/namespaces/shop/deployments/checkout-api/scale`). Deployments, ReplicaSets, StatefulSets, and properly-instrumented CRDs (`subresources: { scale: {...} }` in the CRD definition) qualify. DaemonSets do not — and the HPA correctly refuses to scale them: a DaemonSet's replica count is "one per node," not a number.

**`metrics` is a list, and the list is OR'd via `max()`**, not AND'd. We will come back to this in §12. The intuition: if any one signal says "more pods," you get more pods.

**`behavior` is mandatory in practice, optional in the API.** Without it, the HPA uses defaults that work for one-tier web apps and that destroy any workload with state (a `scaleDown.stabilizationWindowSeconds` of 300s and a 100%/15s scaleUp policy). Real production HPAs set this block.

**No `selector`.** The HPA never reads `.spec.selector`. It always asks the target's `scale` subresource for `status.selector`, which is the canonical answer for "which pods belong to this workload." This matters because the HPA needs to know which pods to sample metrics from, and it gets that from the controller (Deployment, RS, etc.) which got it from its `.spec.selector`. The HPA itself is selector-free.

### What lives at the API surface

The HPA is implemented by the **horizontal-pod-autoscaler controller**, one of the controllers inside `kube-controller-manager`. Source: `kubernetes/pkg/controller/podautoscaler/`. Files of note:

- `horizontal.go` — the reconcile loop and main dispatcher
- `replica_calculator.go` — the `desiredReplicas` math, the per-metric and multi-metric paths
- `metrics/` — the clients for `metrics.k8s.io`, `custom.metrics.k8s.io`, `external.metrics.k8s.io`
- `monitor/` — Prometheus metrics exported by the controller itself

When you `kubectl create -f hpa.yaml`, the manifest hits the API server, is persisted to etcd, and the controller-manager's HPA controller informer wakes up. The controller adds the new HPA to its work queue and reconciles. There is no admission-side enforcement of "this metric actually exists" — if you ask for an external metric that no adapter serves, the HPA goes to `ScalingActive=False` with reason `FailedGetExternalMetric` and stays there until the metric appears.

---

## 3. The HPA Reconcile Loop

The HPA is a controller, and like every controller in this book it follows the pattern from ch 08: observe → decide → act. The cycle runs every `--horizontal-pod-autoscaler-sync-period` (default 15 seconds; the flag lives on `kube-controller-manager`).

```
                      HPA RECONCILE LOOP  (every syncPeriod = 15s default)

   ┌────────────────────────────────────────────────────────────────────────┐
   │  T = 0                                                                  │
   │                                                                         │
   │  ┌─────────────────────────┐                                            │
   │  │ 1. Get target's current │  GET /apis/apps/v1/.../deployments/X/scale │
   │  │    replica count        │  ──► currentReplicas = 6                   │
   │  └─────────────────────────┘                                            │
   │                                                                         │
   │  ┌─────────────────────────┐                                            │
   │  │ 2. Resolve label selector│  status.selector = "app=checkout"          │
   │  │    from /scale          │                                            │
   │  └─────────────────────────┘                                            │
   │                                                                         │
   │  ┌─────────────────────────┐                                            │
   │  │ 3. For each metric:     │                                            │
   │  │    fetch current value  │  metrics.k8s.io ──► pod CPU usages          │
   │  │    via metrics API      │  custom.metrics.k8s.io ──► pod custom        │
   │  │                         │  external.metrics.k8s.io ──► queue depth     │
   │  └─────────────────────────┘                                            │
   │                                                                         │
   │  ┌─────────────────────────┐                                            │
   │  │ 4. Compute desired per  │  desired = ceil(curR * curM / tgtM)         │
   │  │    metric (formula §4)  │  e.g. ceil(6 * 85 / 70) = 8                 │
   │  └─────────────────────────┘                                            │
   │                                                                         │
   │  ┌─────────────────────────┐                                            │
   │  │ 5. Take max across all  │  m1=8  m2=7  m3=12  ──►  desiredAll = 12   │
   │  │    metrics              │                                            │
   │  └─────────────────────────┘                                            │
   │                                                                         │
   │  ┌─────────────────────────┐                                            │
   │  │ 6. Apply behavior:      │  stabilizationWindow, percent/pods caps    │
   │  │    rate-limit the move  │  ──► after policy: 9 (capped from 12)     │
   │  └─────────────────────────┘                                            │
   │                                                                         │
   │  ┌─────────────────────────┐                                            │
   │  │ 7. Clamp to [min, max]  │  min=3 max=50 ──► 9                         │
   │  └─────────────────────────┘                                            │
   │                                                                         │
   │  ┌─────────────────────────┐                                            │
   │  │ 8. If different from    │  PATCH /scale {replicas: 9}                │
   │  │    current, patch       │  (no patch if curR == desired)             │
   │  │    /scale subresource   │                                            │
   │  └─────────────────────────┘                                            │
   │                                                                         │
   │  ┌─────────────────────────┐                                            │
   │  │ 9. Write status         │  conditions, lastScaleTime, currentMetrics │
   │  │    (subresource)        │                                            │
   │  └─────────────────────────┘                                            │
   │                                                                         │
   └────────────────────────────────────────────────────────────────────────┘
                                  │
                                  │  sleep 15s
                                  ▼
                            ── next cycle ──
```

A few details that matter for production.

**Step 3 is parallelized per HPA.** The controller pools workers (default 5) and processes multiple HPAs concurrently. A single HPA's metrics are *not* parallelized — they're fetched in order. If you have one HPA with five metrics, that's five sequential API calls; if any one of them is slow, the whole HPA pauses.

**Step 3 has a hard timeout.** Each metrics fetch has its own client timeout (`metrics-relist-interval` and the underlying HTTP client default of 30s for the metrics API). A slow Prometheus adapter directly inflates HPA latency. Operators routinely discover that a single Prometheus query taking 25 seconds turns a 15-second loop into a 30-second loop with cascading staleness.

**Step 4 uses *ready* pods only.** Specifically, only pods that are `Ready=True` and whose age exceeds `--horizontal-pod-autoscaler-cpu-initialization-period` (default 5 minutes for CPU) and `--horizontal-pod-autoscaler-initial-readiness-delay` (30 seconds) contribute to the metric average. New pods are ignored on the assumption that their CPU is dominated by warm-up cost. If you scale up by 10× during a burst, the HPA only sees the original pods' load *until* the new ones cross the initialization threshold — which is why aggressive scale-up can briefly double the desired count before settling.

**Step 6 (behavior) runs even if step 4-5 said "no change."** Stabilization windows reach back into history; a value that says "8" today might have been "12" two minutes ago, and the behavior block may keep us at 11 because of the historical max. See §10.

**Step 8 is a PATCH, not a PUT.** Specifically, an `application/strategic-merge-patch+json` against `/scale`. The patch is one field: `{"spec":{"replicas": N}}`. The API server's `/scale` handler translates this into a PUT against the parent object (Deployment) with optimistic concurrency. If the parent's resourceVersion has moved (e.g., a `kubectl apply` raced), the patch is rejected with 409 Conflict, and the HPA retries on the next loop.

**Step 9 writes to the status subresource.** Status writes do *not* bump the spec's resourceVersion, so they don't cause GitOps drift alerts. They also expose conditions that operators read: `AbleToScale`, `ScalingActive`, `ScalingLimited`. The last one — `ScalingLimited=True` — means the HPA *wanted* to scale further but `min/max` or `behavior` blocked it. Set an alert on this.

### Source paths

- `kubernetes/pkg/controller/podautoscaler/horizontal.go` — `reconcileAutoscaler`
- `kubernetes/pkg/controller/podautoscaler/replica_calculator.go` — `GetResourceReplicas`, `GetExternalMetricReplicas`
- `kubernetes/staging/src/k8s.io/metrics/pkg/apis/` — the three metrics APIs

---

## 4. The desiredReplicas Formula

The HPA's core math is one line, and you should be able to recite it in your sleep.

```
desiredReplicas = ceil( currentReplicas * (currentMetricValue / targetMetricValue) )
```

That's it. Everything else — multi-metric, behavior, tolerances — is wrapped around this expression.

Concretely, with `currentReplicas = 6`, `currentMetricValue = 85` (let's say 85% CPU), `targetMetricValue = 70`:

```
desired = ceil( 6 * (85 / 70) )
        = ceil( 6 * 1.214 )
        = ceil( 7.285 )
        = 8
```

So six pods at 85% should become eight pods to bring the average down toward 70%.

### Three things the formula gets right

**It's linear in the ratio.** If you're 2× over target, you need (about) 2× the replicas. If you're 10× over target, you need (about) 10× the replicas. This is the right shape for stateless workloads where work per pod is roughly constant.

**It uses ceiling, not floor.** Better to over-provision by one pod than to under-provision by one pod and oscillate.

**It uses *current* replicas, not target replicas from last cycle.** This makes the loop self-correcting: even if a previous cycle picked the wrong number, the next cycle's input is the truth of what's running now.

### Three things the formula doesn't capture

**It assumes work-per-pod is constant.** If your workload has a per-replica fixed cost (each pod establishes a database connection, loads a 2 GB model into memory, joins a leader election) that does not divide cleanly across replicas, the formula overshoots. Doubling replicas does not halve per-pod CPU if half the CPU was startup cost.

**It assumes the metric is causally tied to replicas.** Memory usage often is not. A JVM with a 4 GB heap will use 4 GB regardless of how many replicas exist. Scaling on memory often produces "memory is high, add replicas, memory is still high per pod, add more replicas, OOM-killed, repeat" — see pitfall §35.

**It assumes the metric responds within the reconcile cycle.** Some metrics have inherent lag: SQS queue depth doesn't drop instantly when you add consumers, because consumers take seconds to pull. The HPA reads the still-high depth and adds *more* consumers. Eventually depth drops, but you've over-scaled. The `behavior.scaleUp.stabilizationWindowSeconds` field is the official fix for this — see §10.

### The tolerance band

There is a band around the target where the HPA does nothing. This is to prevent thrash on micro-fluctuations.

```
                          tolerance = 0.1 (default)

   noScale region: 0.9 <= currentMetric/targetMetric <= 1.1
   ──────────────────────────────────────────────────────────

     currentMetric/targetMetric
     ──────┬─────────┬─────────┬─────────┬─────────┬─────►
          0.5       0.9       1.0       1.1       2.0
   scale down  │ no change region │ scale up

   So at target=70 with current=65 (ratio 0.928): NO SCALE
   But  at target=70 with current=63 (ratio 0.900): SCALE DOWN
```

The tolerance is `--horizontal-pod-autoscaler-tolerance` on the controller manager (default `0.1`). It is a global per-cluster flag, not per-HPA. Tuning it down (e.g., to 0.05) makes the HPA more reactive but also more chatty. We've seen clusters set it to `0.15` to dampen scale-down churn on noisy workloads, at the cost of running with up to 15% excess capacity in steady state.

### Multi-metric: max() wins

When the spec lists multiple metrics, each is evaluated independently producing `desiredReplicas_i`. The HPA then picks `max(desiredReplicas_1, ..., desiredReplicas_n)`.

```
  metric 1 (cpu):       desired = 8
  metric 2 (memory):    desired = 6
  metric 3 (qps):       desired = 11
  metric 4 (queue):     desired = 5
                                ─────
                          max = 11
```

The rule is "the most cautious signal wins" — if any signal says "we need 11," we run 11. This is correct for safety. It is also why people frequently add a fourth metric to an HPA, watch the scale aggressively jump, and discover that one of the four metrics has a much lower target than they realized. Always sanity-check each metric's desired count individually before adding it.

---

## 5. HPA Metric Types in Depth

The `autoscaling/v2` HPA recognises five metric types. Each has a different fetch path and a different target syntax.

### 5.1 `Resource` — CPU and memory from metrics-server

```yaml
- type: Resource
  resource:
    name: cpu                    # or memory
    target:
      type: Utilization          # percentage of requests
      averageUtilization: 70
```

This is the original HPA mode. It reads pod metrics from `metrics.k8s.io` (served by metrics-server). The target type can be:

- `Utilization` — percentage of `requests` (this is the most common). If `requests.cpu = 200m` and the pod is using `140m`, utilization is 70%.
- `AverageValue` — absolute value per pod (e.g., `averageValue: 500m`). No requests needed.

**`Utilization` requires `resources.requests` to be set.** If the pod's CPU request is empty, the HPA computes a percentage of zero and goes to `ScalingActive=False`. This is the single most common HPA configuration bug — see pitfall §35.

### 5.2 `Pods` — custom metrics averaged across pods

```yaml
- type: Pods
  pods:
    metric:
      name: http_requests_per_second
    target:
      type: AverageValue
      averageValue: "200"
```

The metric is fetched from `custom.metrics.k8s.io`, scoped to the pods owned by the target's selector. The target is always `AverageValue` (you cannot use `Utilization` because there's no analogous "request" for a custom metric).

Use this when the metric is naturally per-pod: HTTP request rate, queue items processed per second, in-flight WebSocket connections per replica.

### 5.3 `Object` — custom metric on a specific object

```yaml
- type: Object
  object:
    describedObject:
      apiVersion: networking.k8s.io/v1
      kind: Ingress
      name: checkout
    metric:
      name: requests_per_second
    target:
      type: Value
      value: "10k"
```

The metric is fetched from `custom.metrics.k8s.io` but is tied to a non-pod object (here, an Ingress). The HPA reads one number and uses it as the entire current value (no averaging).

Use this when the metric lives at a higher level than the pods: total traffic through an Ingress, total RPS at a gRPC LB, total active connections on a Service.

### 5.4 `External` — metrics from outside the cluster

```yaml
- type: External
  external:
    metric:
      name: sqs_queue_depth
      selector:
        matchLabels:
          queue: checkout-events
    target:
      type: AverageValue
      averageValue: "100"          # per-pod target
      # or
      # type: Value
      # value: "5000"              # absolute
```

Fetched from `external.metrics.k8s.io`. This is the only metric type where the value is not tied to a Kubernetes object at all. SQS depth, GCS bucket size, GitHub Actions queue length, a value scraped from a third-party API — all `External`.

`AverageValue` means "divide by replica count, then target per replica." `Value` means "the raw value should equal the target." For a queue with 5,000 messages and `averageValue: 100`, the HPA wants 50 replicas. For the same queue and `value: 5000`, the HPA wants exactly 1 replica (the value matches the target).

### 5.5 `ContainerResource` — per-container CPU/memory

```yaml
- type: ContainerResource
  containerResource:
    name: cpu
    container: app                # not "istio-proxy"
    target:
      type: Utilization
      averageUtilization: 70
```

GA since 1.30. Like `Resource`, but scoped to a single container in the pod. This solves the sidecar problem: if your pod has `app` (does real work) and `istio-proxy` (does negligible CPU), then `Resource` averages over both containers and dilutes the signal from the one that matters. `ContainerResource` reads only `app`'s usage.

This is the *correct* metric type for any pod that runs sidecars. The number of HPAs we have seen that scale on `Resource: cpu` while running a sidecar mesh — and consequently scale far too eagerly because the sidecar's idle CPU drags the average down — is large.

### 5.6 Summary table

| Metric type | API served by | Scope | Target types | Typical use |
|-------------|---------------|-------|--------------|-------------|
| `Resource` | metrics-server | pod, averaged | `Utilization`, `AverageValue` | CPU/memory |
| `ContainerResource` | metrics-server | container, averaged | `Utilization`, `AverageValue` | CPU/memory with sidecars |
| `Pods` | prometheus-adapter (custom) | pod, averaged | `AverageValue` | per-pod custom signal |
| `Object` | prometheus-adapter (custom) | one named object | `Value`, `AverageValue` | per-Ingress, per-Service |
| `External` | KEDA / prometheus-adapter (external) | global | `Value`, `AverageValue` | queue depth, external system |

---

## 6. The Metrics API Federation

The HPA does not talk to Prometheus, metrics-server, or KEDA directly. It talks to **three Kubernetes APIs** that look like ordinary API server resources but are served by external aggregated API servers.

```
                  ┌────────────────────────────────────────────────────────┐
                  │                  kube-apiserver                         │
                  │                                                         │
                  │   /apis/metrics.k8s.io/v1beta1                         │
                  │   /apis/custom.metrics.k8s.io/v1beta1                  │
                  │   /apis/external.metrics.k8s.io/v1beta1                │
                  └─────┬───────────────┬───────────────┬──────────────────┘
                        │               │               │
                        │ APIService    │ APIService    │ APIService
                        │ registration  │ registration  │ registration
                        ▼               ▼               ▼
                  ┌──────────┐    ┌─────────────┐  ┌──────────────────┐
                  │ metrics- │    │ prometheus- │  │ keda-operator-   │
                  │ server   │    │ adapter     │  │ metrics-apiserver │
                  └────┬─────┘    └──────┬──────┘  └────────┬──────────┘
                       │                 │                  │
                       │ summary API     │ PromQL           │ scaler logic
                       ▼                 ▼                  ▼
                  ┌──────────┐    ┌─────────────┐  ┌──────────────────┐
                  │ kubelet  │    │ Prometheus  │  │  Kafka / SQS /    │
                  │ /stats   │    │             │  │  Prometheus / ... │
                  └──────────┘    └─────────────┘  └──────────────────┘
```

### How APIService aggregation works

When you install metrics-server, the install manifest creates an `APIService` resource:

```yaml
apiVersion: apiregistration.k8s.io/v1
kind: APIService
metadata:
  name: v1beta1.metrics.k8s.io
spec:
  group: metrics.k8s.io
  groupPriorityMinimum: 100
  versionPriority: 100
  service:
    name: metrics-server
    namespace: kube-system
    port: 443
  insecureSkipTLSVerify: true     # or proper CA bundle
  version: v1beta1
```

This tells the kube-apiserver: "when you receive a request for `/apis/metrics.k8s.io/v1beta1/*`, do not handle it yourself — proxy it to the `metrics-server` Service." The apiserver becomes a reverse proxy for that path. The HPA controller, which is just a client of the apiserver, doesn't know or care that the response came from a different process.

You can have **at most one APIService per group/version**. This is why you cannot run prometheus-adapter *and* KEDA both as `external.metrics.k8s.io` providers — they would collide. KEDA solves this by serving on `external.metrics.k8s.io/v1beta1` *itself*, with prometheus-adapter as a fallback for non-KEDA cases (or, more commonly, only one of the two is installed).

### `kubectl top` and the metrics-server endpoint

```bash
kubectl get apiservice v1beta1.metrics.k8s.io
# NAME                     SERVICE                      AVAILABLE   AGE
# v1beta1.metrics.k8s.io   kube-system/metrics-server   True        45d

kubectl get --raw "/apis/metrics.k8s.io/v1beta1/pods" | jq '.items[0]'
# {
#   "metadata": { "name": "checkout-7d8f-2x4j5", "namespace": "shop", ... },
#   "containers": [
#     { "name": "app", "usage": { "cpu": "143m", "memory": "412Mi" } }
#   ],
#   "timestamp": "2026-05-23T14:22:08Z",
#   "window": "30s"
# }
```

The `window` field matters: metrics-server returns a *rate* computed over the last 30 seconds, not an instantaneous value. This is also the HPA's input granularity. You cannot get sub-30-second responsiveness from CPU-based HPA, period.

### Polling vs streaming

All three metrics APIs are pull-based, not streaming. The HPA polls every 15 seconds. The adapters cache results between polls (metrics-server caches the kubelet summary for `--metric-resolution`, default 15 seconds). End-to-end staleness is therefore up to 30 seconds (kubelet sample window) + 15 seconds (adapter cache) + 15 seconds (HPA poll interval) = up to a minute. For most workloads this is fine. For low-latency burst patterns, you reach for KEDA (event-driven, no polling staleness on the upstream metric).

---

## 7. metrics-server: Resource Metrics in Practice

`metrics-server` is the canonical implementation of `metrics.k8s.io`. It is a small Go binary that:

1. Scrapes each kubelet's `/metrics/resource` endpoint every `--metric-resolution` (default 15s).
2. Aggregates results in-memory (no persistence; restart loses history).
3. Serves `metrics.k8s.io` via an aggregated API server.

```yaml
# metrics-server deployment (excerpt)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: metrics-server
  namespace: kube-system
spec:
  replicas: 2                                      # HA
  template:
    spec:
      containers:
      - name: metrics-server
        image: registry.k8s.io/metrics-server/metrics-server:v0.7.2
        args:
        - --kubelet-preferred-address-types=InternalIP,ExternalIP,Hostname
        - --kubelet-use-node-status-port
        - --metric-resolution=15s                  # scrape interval
        - --kubelet-insecure-tls                   # or --kubelet-certificate-authority
        - --secure-port=10250
        ports:
        - name: https
          containerPort: 10250
        resources:
          requests:
            cpu: 100m
            memory: 200Mi
        livenessProbe:
          httpGet: { path: /livez, port: https, scheme: HTTPS }
        readinessProbe:
          httpGet: { path: /readyz, port: https, scheme: HTTPS }
```

### Operational realities

**metrics-server is in-memory only.** When a metrics-server pod restarts, it has no history. HPAs that depend on it will go to `ScalingActive=False` for the duration of the gap (roughly 30 seconds: one scrape cycle to populate, plus the HPA's next poll). HA with two replicas helps but doesn't eliminate this — both replicas have independent caches and reset on their own restarts. For most clusters this is acceptable. For clusters where any HPA gap is unacceptable, you either (a) make metrics-server a 3-replica deployment with PodDisruptionBudget, or (b) use Prometheus + prometheus-adapter for resource metrics too (which has a TSDB and survives restarts).

**`--kubelet-insecure-tls` is the default in many installers and is fine.** The kubelet uses a self-signed cert in most distributions; verifying it against the cluster CA is more trouble than it's worth on internal control-plane links. Production clusters with strict policy use `--kubelet-certificate-authority=/etc/kubernetes/pki/ca.crt`.

**Scrape failures matter.** If metrics-server can't reach a kubelet (firewall, certificate, node down), pods on that node have no metrics. The HPA will see one of:

- `FailedGetResourceMetric` — every pod failed.
- `ScalingActive=True` with a missing-pod-tolerance setting — some pods missing, default tolerance allows up to `--horizontal-pod-autoscaler-cpu-initialization-period` worth of missing samples.

Always check metrics-server logs on `ScalingActive=False` complaints.

**There is one metrics-server per cluster.** Multi-cluster federation does not work at this layer. Each cluster has its own.

### What metrics-server is *not* for

- Long-term trend analysis (use Prometheus).
- Per-namespace quota enforcement (use ResourceQuota objects).
- VPA recommendation (VPA reads from Prometheus or its own historical store, not metrics-server).
- Alerting (use Prometheus).

It exists for one purpose: feed the HPA and `kubectl top`. Treat it as a load-bearing dependency for autoscaling but do not build other tooling on top of it.

---

## 8. Custom Metrics with prometheus-adapter

When you need `Pods`, `Object`, or `External` HPA metrics, the most common provider is **prometheus-adapter** (formerly k8s-prometheus-adapter). It runs as a Deployment, registers as the APIService for `custom.metrics.k8s.io` and/or `external.metrics.k8s.io`, and translates Kubernetes-shaped metric requests into PromQL queries.

### Install topology

```
            kube-apiserver
                  │
                  │ /apis/custom.metrics.k8s.io/v1beta1/namespaces/shop/pods/*/http_requests_per_second
                  ▼
            prometheus-adapter (Deployment in kube-system or monitoring)
                  │
                  │ PromQL: sum(rate(http_requests_total{namespace="shop",pod=~"checkout-.*"}[1m])) by (pod)
                  ▼
            Prometheus
                  │
                  │ scrape jobs
                  ▼
            workload Pods (instrumented with /metrics endpoints)
```

### The `ConfigMap` rules language

prometheus-adapter is driven by a rules configuration that tells it which Prometheus series to expose and how to map them to Kubernetes shape.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: adapter-config
  namespace: monitoring
data:
  config.yaml: |
    rules:
    # ─────────────── Pods metric: http_requests_per_second ───────────────
    - seriesQuery: 'http_requests_total{namespace!="",pod!=""}'
      resources:
        overrides:
          namespace: { resource: namespace }
          pod:       { resource: pod }
      name:
        matches: "^(.*)_total$"
        as: "${1}_per_second"          # "http_requests_per_second"
      metricsQuery: |
        sum(rate(<<.Series>>{<<.LabelMatchers>>}[2m])) by (<<.GroupBy>>)

    # ─────────────── Object metric: ingress requests_per_second ───────────
    - seriesQuery: 'nginx_ingress_controller_requests{ingress!=""}'
      resources:
        overrides:
          namespace: { resource: namespace }
          ingress:   { group: networking.k8s.io, resource: ingresses }
      name:
        matches: "^(.*)$"
        as: "${1}_per_second"
      metricsQuery: |
        sum(rate(<<.Series>>{<<.LabelMatchers>>}[1m])) by (<<.GroupBy>>)

    externalRules:
    # ─────────────── External metric: rabbitmq queue depth ───────────────
    - seriesQuery: 'rabbitmq_queue_messages{queue!=""}'
      name:
        matches: "^rabbitmq_queue_messages$"
        as: "rabbitmq_queue_depth"
      metricsQuery: |
        max(<<.Series>>{<<.LabelMatchers>>}) by (queue)
```

Four pieces tie the YAML to the HPA spec.

**`seriesQuery`** discovers which Prometheus series the adapter exposes. The adapter periodically lists matching series and registers a virtual resource per series.

**`resources.overrides`** declares how Prometheus labels map to Kubernetes resources. The label `namespace` maps to the `namespace` resource, `pod` to `pod`, `ingress` to the Ingress object. If you forget this, the metric is exposed but the HPA's selector can't bind to it.

**`name.matches / as`** renames the metric. `http_requests_total` becomes `http_requests_per_second` because the `metricsQuery` is a rate. Lying about the unit will burn you at 3 AM.

**`metricsQuery`** is the PromQL template. The placeholders `<<.Series>>`, `<<.LabelMatchers>>`, and `<<.GroupBy>>` are substituted with the actual series name, the label matchers derived from the HPA's selector, and the group-by clause from the resource overrides.

### How the HPA query flows through

The HPA spec asks for:

```yaml
- type: Pods
  pods:
    metric: { name: http_requests_per_second }
    target: { type: AverageValue, averageValue: "200" }
```

The HPA controller, seeing `Pods` type with name `http_requests_per_second`, calls:

```
GET /apis/custom.metrics.k8s.io/v1beta1/namespaces/shop/pods/*/http_requests_per_second?labelSelector=app%3Dcheckout
```

prometheus-adapter receives this, looks up the rule for `http_requests_per_second`, substitutes:

```
sum(rate(http_requests_total{namespace="shop",pod=~"checkout-7d8f-.*"}[2m])) by (namespace, pod)
```

…queries Prometheus, gets back per-pod values, and returns them shaped as a `MetricValueList`. The HPA averages those values and runs the formula.

### Operational realities

**Discovery is periodic, not on-demand.** The adapter lists series every `--metrics-relist-interval` (default 10 minutes). If you ship a new metric, you wait up to 10 minutes for it to appear in the adapter's catalog before the HPA can use it. Lower this for dev environments; leave it at 10 minutes for production where you don't want to hammer Prometheus with `series()` calls.

**Adapter caching matters.** prometheus-adapter caches each metric query for `--metric-cache-duration` (default 1 minute). The HPA polls every 15 seconds and gets the cached value three out of four times. This is usually fine. If you need faster reaction, lower it — at the cost of more Prometheus load.

**The PromQL template is the most common bug source.** A `rate()` window of `[30s]` against a Prometheus scrape interval of `60s` returns nothing (rate needs at least two samples). A `[5m]` rate dampens spikes you needed to react to. Get the windowing right: `scrape_interval × 2` is the minimum; `scrape_interval × 4` is the safe default.

---

## 9. External Metrics and KEDA's Metrics Adapter

External metrics — queue depth, total active sessions, anything not derived from a pod or in-cluster object — flow through `external.metrics.k8s.io`. There are two practical providers.

### Option A: prometheus-adapter externalRules (shown above)

Works when the external system already exposes a Prometheus exporter (RabbitMQ exporter, CloudWatch exporter, Datadog exporter). Pros: one adapter, one config. Cons: you must run the exporter, and the adapter caches/refreshes on its own schedule independent of the upstream metric's freshness.

### Option B: KEDA's metrics-api adapter

KEDA installs its own `external.metrics.k8s.io` APIService (`keda-operator-metrics-apiserver`). Each `ScaledObject` you create registers a "metric" on this APIService corresponding to the trigger you defined. The HPA queries that metric; KEDA's apiserver answers by running the trigger's scaler logic (talking directly to Kafka, SQS, Redis, etc.) and returning a number.

```
                kube-apiserver
                       │
                       │ /apis/external.metrics.k8s.io/v1beta1/namespaces/checkout/s0-kafka-orders
                       ▼
                keda-operator-metrics-apiserver
                       │
                       │ Lookup ScaledObject "orders-consumer" trigger 0 (kafka)
                       │ Call kafka scaler: get consumer-group lag
                       ▼
                Kafka broker
```

KEDA's advantage is that the scaler talks directly to the source — no Prometheus, no exporter. Its disadvantage is that the metric is *only* visible while a `ScaledObject` references it; it's not a general-purpose external metric provider.

### When you have both

You cannot have two APIService providers for `external.metrics.k8s.io`. In practice:

- If you use KEDA for events, install KEDA's external metrics adapter and skip prometheus-adapter's `externalRules`.
- If you only use prometheus-based external metrics, use prometheus-adapter and skip KEDA.
- Both is rare; if you need it, run prometheus-adapter as `custom.metrics.k8s.io` only and KEDA as `external.metrics.k8s.io` only.

---

## 10. HPA Behavior: scaleUp, scaleDown, and Stabilization

Before the `behavior` block (added in `autoscaling/v2beta2`, GA in `autoscaling/v2`), the HPA had three hardcoded behaviors:

- Scale up: up to 100% increase per minute.
- Scale down: up to 100% decrease per minute, but limited by `--horizontal-pod-autoscaler-downscale-stabilization` (default 5 minutes).
- No way to override per-HPA.

The `behavior` block makes each of these tunable per HPA, plus adds `selectPolicy` and explicit `Percent` vs `Pods` policies.

### Anatomy of `behavior`

```yaml
behavior:
  scaleUp:
    stabilizationWindowSeconds: 0
    selectPolicy: Max                  # Max | Min | Disabled
    policies:
    - type: Percent
      value: 100
      periodSeconds: 60
    - type: Pods
      value: 4
      periodSeconds: 60
  scaleDown:
    stabilizationWindowSeconds: 300
    selectPolicy: Max
    policies:
    - type: Percent
      value: 10
      periodSeconds: 60
```

**`stabilizationWindowSeconds`**: when computing the desired replica count, look back over the last *N* seconds of recommendations and use the *extremum* (max for scaleDown, min for scaleUp) instead of just the latest. On scale-down with `stabilizationWindowSeconds: 300`, if any reconcile in the past 5 minutes recommended a higher count, that higher count wins. This is what prevents flapping when load briefly dips below target and then rises again.

**`policies`**: each policy is a rate limit. `Percent: 100, periodSeconds: 60` means "at most 100% growth in 60 seconds." `Pods: 4, periodSeconds: 60` means "at most 4 absolute pods added in 60 seconds." Policies are evaluated against actual scaling history, not desired values.

**`selectPolicy`**:
- `Max` (default for scaleUp) — pick the policy that *permits more change*. With `Percent: 100` and `Pods: 4`, a 3-replica deployment is allowed to grow by max(3, 4) = 4 (the absolute policy wins because percent would only allow 3). A 10-replica deployment is allowed to grow by max(10, 4) = 10 (the percent policy wins).
- `Min` — pick the policy that *permits less change*. Useful for scaleDown to be the most conservative.
- `Disabled` — turn off scaling in that direction entirely.

### Timeline visualization

```
   load
    ▲
    │             ╱╲    ╱╲
    │            ╱  ╲  ╱  ╲
    │           ╱    ╲╱    ╲___
    │          ╱             ╲
    │  ───────                ──────
    └──────────────────────────────────────────► time
         t0    t1    t2    t3    t4    t5

   without behavior (default stabilization 0s up, 300s down):
   replicas: 3 → 7 → 9 → 9 → 5 → 5 → 4 → 4
                          ↑     ↑   ↑
                          │     │   downscale window expired
                          │     downscale held by max of past 5 min
                          spike fully covered

   with scaleUp.stabilizationWindowSeconds: 60:
   replicas: 3 → 3 → 7 → 9 → 9 → 5 → 5 → 4
                       ↑
                       1-minute delay before reacting up
                       (smooths very brief spikes)
```

### Common behavior patterns

**Aggressive scale-up, conservative scale-down (web tier):**

```yaml
behavior:
  scaleUp:
    stabilizationWindowSeconds: 0
    policies:
    - type: Percent
      value: 200          # double every 30s if needed
      periodSeconds: 30
  scaleDown:
    stabilizationWindowSeconds: 600   # 10 minutes
    policies:
    - type: Percent
      value: 10
      periodSeconds: 60
```

**Cautious scale-up, fast scale-down (batch worker pool):**

```yaml
behavior:
  scaleUp:
    stabilizationWindowSeconds: 60    # wait 1m before adding
    policies:
    - type: Pods
      value: 2            # at most 2 new workers per minute
      periodSeconds: 60
  scaleDown:
    stabilizationWindowSeconds: 30    # release idle workers fast
    policies:
    - type: Percent
      value: 50
      periodSeconds: 30
```

**Stateful — no scale-down allowed:**

```yaml
behavior:
  scaleDown:
    selectPolicy: Disabled
```

Used by stateful services where scaling down is a deliberate operator action (StatefulSets backing leader-elected systems, for example).

### What stabilization windows do *not* fix

Stabilization windows smooth the *desired replica count*. They do not smooth the *metric*. A flapping CPU metric will still produce flapping desired counts; stabilization just hides the flapping from the scale subresource. If your metric is noisy at its source, fix the metric (`rate()` over a longer window, EMA in the exporter), don't paper over it with a longer stabilization window.

---

## 11. ContainerResource: Per-Container Autoscaling

The classic `Resource` metric averages CPU and memory across *all* containers in a pod. This was fine when pods had one container. Service-mesh sidecars, log shippers, OAuth proxies, and observability agents make it wrong.

### The sidecar dilution problem

```
   pod = [ app: 800m CPU,  istio-proxy: 20m CPU ]
   averaged across containers: 410m

   pod request: app=1000m, istio-proxy=100m → total request 1100m
   "utilization" = 410 / 1100 = 37%

   reality: app is at 800/1000 = 80% — desperately needs more replicas
   HPA sees 37%, well under target 70%, does nothing
```

`ContainerResource` fixes this by scoping the metric to a named container.

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: app
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: app
  minReplicas: 3
  maxReplicas: 30
  metrics:
  - type: ContainerResource
    containerResource:
      name: cpu
      container: app          # ignore istio-proxy
      target:
        type: Utilization
        averageUtilization: 70
```

### Caveats

**The container must exist in every pod.** If your Deployment evolves and renames `app` to `server`, the HPA goes to `ScalingActive=False` with `FailedGetContainerResourceMetric`. There is no graceful migration; you must update the HPA at the same time.

**`Utilization` requires that container to have CPU/memory requests.** The kubelet reports `usage`; the HPA divides by `requests` to get utilization. If the container has no request, no utilization is computable.

**Only works on metrics-server-served resource metrics.** You cannot use `ContainerResource` for custom metrics; for those, you scope by Prometheus label inside the adapter's PromQL template.

### When to prefer `Resource`

If your pod really is one container — or if all containers are doing work that scales together (rare, but it happens) — `Resource` is fine and slightly cheaper to compute. Use `ContainerResource` whenever sidecars are present, which in practice means most pods running on a mesh.

---

## 12. Multi-Metric HPAs and the max() Rule

Listing multiple metrics is the right answer for workloads where no single signal captures load.

```yaml
metrics:
- type: ContainerResource
  containerResource:
    name: cpu
    container: app
    target: { type: Utilization, averageUtilization: 70 }
- type: Pods
  pods:
    metric: { name: http_requests_per_second }
    target: { type: AverageValue, averageValue: "150" }
- type: External
  external:
    metric: { name: redis_queue_depth, selector: { matchLabels: { queue: jobs } } }
    target: { type: AverageValue, averageValue: "10" }
```

The HPA evaluates each metric, gets a `desiredReplicas_i`, and uses `max(desired_1, desired_2, desired_3)`.

### Why max() is correct

For autoscaling, the right policy is "be ready for the worst case any one signal predicts." If CPU says we need 8 and queue depth says we need 14, we should provision 14: serving the queue requires 14 workers, and the CPU signal is implicitly understating because at 14 replicas the per-pod CPU will be lower than at 8.

### Why min() would be catastrophic

If any one metric goes to zero or stops being reported, min() would drive the workload to `minReplicas` regardless of the other signals. Imagine your QPS metric pipeline breaks: with min(), the cluster scales down to 3 replicas even though queue depth is screaming at 5000 messages.

### The hidden cost of adding a metric

Every metric in the list is one extra metric API call per reconcile. With five metrics and a 15-second loop, that's 20 metric calls per minute per HPA. Across 200 HPAs you've doubled the load on metrics-server and prometheus-adapter. The cardinality also matters: a `Pods` metric scoped to a 100-replica deployment is one PromQL query that returns 100 series. At scale these queries become the dominant cost on the metrics path.

### Conditions to inspect

```
$ kubectl describe hpa checkout
...
Conditions:
  Type            Status  Reason             Message
  ----            ------  ------             -------
  AbleToScale     True    ReadyForNewScale   recommended size matches current size
  ScalingActive   True    ValidMetricFound   the HPA was able to successfully calculate
                                              a replica count from cpu resource utilization
  ScalingLimited  False   DesiredWithinRange the desired count is within the acceptable range
```

`AbleToScale=False` → the target's `/scale` subresource failed. Permissions, missing CRD scale config, target not found.

`ScalingActive=False` → all metrics failed to fetch. Investigate the metrics API path.

`ScalingLimited=True` → the HPA wanted to scale further but `min/max` or `behavior` capped it. Alert on this; it means your autoscaler is hitting its bounds.

---

## 13. Vertical Pod Autoscaler Architecture

The VPA is *not* a built-in. It is a separately-installed CRD-driven set of three controllers from `kubernetes/autoscaler/vertical-pod-autoscaler`. Production teams treat it as essential; many engineers don't know it exists.

```
         ┌─────────────────────────────────────────────────────────────┐
         │                  VPA: three-component design                 │
         └─────────────────────────────────────────────────────────────┘

           ┌─────────────────┐
           │  vpa-recommender │  ◄── Prometheus / metrics-server (optional history sources)
           │                  │  ◄── kube-apiserver: list Pods, watch PodMetrics
           │  builds histogram│
           │  computes target │
           │  writes status   │     PATCH vpa.status.recommendation
           └────────┬─────────┘
                    │
                    ▼
           ┌─────────────────┐
           │  VPA CR (the     │
           │   resource you   │
           │   created)       │
           │  .status.        │
           │   recommendation │
           └────────┬─────────┘
                    │
                    ▼
           ┌─────────────────┐                  ┌─────────────────────┐
           │   vpa-updater    │  if mode=Auto:  │   pod evictions     │
           │                  │ ──────────────► │   (one at a time,    │
           │  watches VPAs,   │                 │   honoring PDB)      │
           │  evicts pods     │                 └─────────────────────┘
           │  whose requests  │
           │  diverge from    │
           │  recommendation  │
           └─────────────────┘

           ┌─────────────────────────────┐
           │   vpa-admission-controller   │  ◄── mutating webhook on pod CREATE
           │                              │
           │   intercepts new pods,       │  PATCH pod.spec.containers[].resources
           │   rewrites requests/limits   │  with values from VPA.status.recommendation
           │   to match recommendation    │
           └─────────────────────────────┘
```

### Three components, three responsibilities

**Recommender** (one Deployment, single-active). Watches all VPA objects, fetches metrics, computes recommendations, writes them to VPA status. This is the only component that does math.

**Updater** (one Deployment, single-active). Watches VPA objects with `updateMode: Recreate` or `Auto`. For each pod whose `resources` diverges from the recommendation by more than a threshold, evicts the pod (so it gets recreated with new requests via the admission controller). Honors PodDisruptionBudgets — will not evict if PDB would be violated. Rate-limited globally to avoid evicting half the cluster at once.

**Admission Controller** (one Deployment, replicated for HA). Mutating admission webhook on `CREATE` of pods. When a pod is created — whether by Deployment scale-up, Updater eviction, or anything else — the webhook looks up the matching VPA, reads the recommendation, and rewrites `spec.containers[].resources.requests` (and optionally limits) before persistence.

### The VPA manifest

```yaml
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: checkout-vpa
  namespace: shop
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: checkout-api
  updatePolicy:
    updateMode: "Auto"             # Off | Initial | Auto | Recreate
    minReplicas: 2                  # don't evict if fewer than 2 ready
  resourcePolicy:
    containerPolicies:
    - containerName: app
      mode: Auto
      controlledResources: ["cpu", "memory"]
      controlledValues: RequestsAndLimits   # or "RequestsOnly"
      minAllowed:
        cpu: 50m
        memory: 64Mi
      maxAllowed:
        cpu: "4"
        memory: 8Gi
    - containerName: istio-proxy
      mode: "Off"                  # don't touch sidecar
```

**`updateMode`**:
- `Off` — Recommender computes recommendations, writes status. No mutation. Use this everywhere first; treat the status as advice.
- `Initial` — admission controller rewrites pods *at creation time*. Existing pods are not touched. New pods inherit the recommendation. Safe; no disruption.
- `Auto` — same as `Recreate` today; reserved for future in-place resize.
- `Recreate` — updater evicts pods that drift; admission controller rewrites on recreation. Causes disruption.

**`controlledValues`** — default is `RequestsAndLimits`, which scales limits proportionally. `RequestsOnly` preserves whatever limit the workload defined. We strongly recommend `RequestsAndLimits` for new workloads; manual limit-setting always lags reality.

**`minReplicas`** in `updatePolicy` — VPA's eviction safety. If fewer than this many pods are `Ready`, the updater refuses to evict. Combined with PDB, this prevents the VPA from breaking the workload while updating it.

### Why three components, not one

Splitting prevents a single component from being a SPOF for both decisions and mutations. If the recommender crashes, recommendations get stale but admission still works. If the updater crashes, existing pods are unchanged but new pods still get current recommendations. If the admission controller crashes, new pods are created without VPA mutation (and a fail-open webhook setting means cluster operations continue). This is a deliberate failure-mode design.

---

## 14. VPA Recommendation Algorithm

VPA does not just "use the average." It builds an exponentially-decayed histogram of resource usage and picks specific percentiles for different recommendations.

### Data ingestion

The Recommender pulls per-container resource samples. Two ingestion modes:

1. **From metrics-server's `PodMetrics`** — every recommender cycle (default 1 minute), list `PodMetrics`, attribute each sample to the VPA that targets its pod.
2. **From Prometheus** — for historical bootstrap when the recommender restarts. Configure with `--storage=prometheus --prometheus-address=...`. The recommender queries `container_cpu_usage_seconds_total` and `container_memory_working_set_bytes` for the past 8 days on startup, then transitions to metrics-server.

Without Prometheus, a recommender restart loses 8 days of history and must rebuild. This is acceptable but means recommendations will be jittery for the first day after a restart.

### The histogram

For each container × resource, the recommender keeps a histogram of usage:

```
   CPU histogram (logarithmic buckets):

   buckets:   0   50m  100m  150m  220m  330m  490m  730m  1.1   1.6   ...
   weights:   ▏    ▎     ▌    ▊    ▉   ▉▉▉   ▉▉▉   ▉▉    ▎     ▏

   exponential decay: each sample's weight halves every 24 hours
```

Decay (`--cpu-histogram-decay-half-life` and `--memory-histogram-decay-half-life`, default 24 hours) means recent samples dominate. A pod that ran at 800m for 6 hours yesterday weighs ~30% of a pod running at 600m for the past hour.

### The three recommendations

The recommender emits three numbers per resource per container:

| Field | Percentile | Used by |
|-------|-----------|---------|
| `lowerBound` | p50 of the histogram | Updater: "is the request too high?" |
| `target` | p90 | Admission controller: this becomes the new request |
| `upperBound` | p95 | Updater: "is the request too low?" |
| `uncappedTarget` | p90 without min/maxAllowed clamping | Diagnostic — shows "what we would have recommended" |

The defaults are tunable via `--target-cpu-percentile`, `--recommendation-margin-fraction`, and a confidence interval that widens as the sample size shrinks.

**Why p90 for target?** It covers most usage spikes without paying for the absolute worst case. p99 would mean the workload runs with 30% headroom on average — wasteful. p50 would mean the workload is OOM-killed half the time — broken.

**Why p50 for lowerBound?** A request lower than p50 means at least half of all observed samples exceed the request. That's a strong "your sizing is wrong" signal.

**Confidence margin.** New VPAs (less than 24 hours of data) get an inflated upperBound and a wide lowerBound so the updater doesn't churn before there's signal.

### What VPA cannot model

**Diurnal patterns.** The 8-day exponentially-weighted histogram smooths daily variation into a single distribution. If your workload runs at 100m at 3 AM and 1000m at 3 PM, VPA picks somewhere around p90 of *both peaks combined* — probably 900m. You over-provision at 3 AM and just barely fit at 3 PM. There is no "scale me up at 3 PM" mode in VPA. That's KEDA's cron scaler territory.

**Multi-modal distributions.** A workload that has two modes — idle (50m) and busy (800m) — produces a bimodal histogram. p90 captures the busy mode, but the smooth interpolation means VPA may "split the difference" in odd ways. For multi-modal workloads, prefer HPA (which scales replicas dynamically) over VPA (which fixes a per-pod size).

**Sub-minute spikes.** The recommender samples per minute. A 30-second spike to 5× normal is partially smoothed away.

---

## 15. VPA Update Modes and In-Place Resize

The painful truth about VPA pre-Kubernetes 1.27: changing a pod's resources requires deleting the pod and recreating it. There was no API for "resize this pod in place." The Updater's only tool was eviction.

### The eviction problem

```
   updateMode: Auto

   Updater sees: pod checkout-7d8f-xx has requests=200m, recommendation=600m
                 ratio = 600/200 = 3.0 — well outside acceptable range

   Updater action:
     1. Check PDB: is eviction allowed? If not, skip.
     2. Check VPA's minReplicas: are enough pods Ready? If not, skip.
     3. Check global eviction rate limit (default: 0.5 evictions per minute
        per VPA, configurable with --eviction-tolerance).
     4. POST /api/v1/namespaces/shop/pods/checkout-7d8f-xx/eviction
     5. Pod is terminated → Deployment controller creates replacement →
        admission webhook intercepts new pod CREATE → injects new requests
        → scheduler places pod → kubelet starts container with new requests
```

Steady-state time for the rolled-resize of a 10-pod deployment with PDB `maxUnavailable: 1`: ~10 × (pod startup + readiness probe) ≈ 5-15 minutes. During that window, the workload runs with mixed sizing.

### In-place pod resize (1.27+, beta in 1.33)

KEP-1287 introduced the `resize` subresource on Pods. With the `InPlacePodVerticalScaling` feature gate enabled, you can PATCH a running pod's `containers[*].resources` and the kubelet adjusts the container's cgroup limits without restart.

```bash
kubectl patch pod checkout-7d8f-xx --subresource resize --patch '
  spec:
    containers:
    - name: app
      resources:
        requests: { cpu: "600m", memory: "512Mi" }
        limits:   { cpu: "1",    memory: "1Gi" }
'
```

Resize policy is per resource:

```yaml
containers:
- name: app
  resources:
    requests: { cpu: "200m", memory: "256Mi" }
  resizePolicy:
  - resourceName: cpu
    restartPolicy: NotRequired       # CPU can grow without restart
  - resourceName: memory
    restartPolicy: RestartContainer  # memory growth needs restart for some JVMs
```

When VPA learns to use the resize subresource (in progress as of writing — the `InPlaceOrRecreate` mode is the target), the VPA update path goes from "evict and recreate" to "patch in place." Disruption drops by an order of magnitude.

### Today's reality

For production today:

- **`updateMode: Off`** for important workloads. Read the recommendation, manually apply it during normal deploys, get a real CI/CD signal.
- **`updateMode: Initial`** for new workloads. Their first pod gets the recommendation at creation, but a pod sitting at 100m → 500m boundary won't churn.
- **`updateMode: Auto`** only for tolerant workloads — batch jobs, ML training pods, dev clusters.

We will revisit in a year when in-place resize ships GA broadly.

---

## 16. HPA + VPA: The Coexistence Rule

The single most important VPA rule:

> **HPA and VPA must never autoscale on the same metric.**

Concretely:

| Combination | Verdict |
|-------------|---------|
| HPA on CPU + VPA on CPU (`updateMode: Auto`) | **NEVER**. Oscillates. |
| HPA on CPU + VPA on CPU (`updateMode: Off` — recommendations only) | OK. Humans apply VPA suggestions. |
| HPA on CPU + VPA on memory (`updateMode: Auto`) | OK. Different axes. |
| HPA on custom metric (e.g., QPS) + VPA on CPU/memory (`Auto`) | OK and recommended. |

### Why HPA-on-CPU + VPA-on-CPU oscillates

Cycle 1:
```
  HPA: avg CPU 85% > target 70% → scale replicas 5 → 7
  VPA: pods using 700m, request 200m → recommendation 800m
        Updater evicts a pod; replacement gets requests=800m
```

Cycle 2 (next reconcile):
```
  Pod with 800m request, but now there are 7 replicas spreading load.
  avg CPU per pod drops to 250m. utilization = 250/800 = 31%
  HPA: 31% < target 70% → scale replicas 7 → 4
  VPA: pods using 250m, request 800m → recommendation 280m
        Updater evicts pods to shrink them
```

Cycle 3:
```
  4 replicas with 280m request, total capacity dropped.
  Load back to 700m/pod → utilization 250%
  HPA: scale up dramatically
  VPA: scale up dramatically
  System thrashes.
```

The fix is to autoscale replicas on a metric *causally tied to incoming work* — QPS, queue depth, RPC concurrency — and to autoscale resources on actual usage. They don't share a feedback path.

### Recommended pattern

```yaml
# HPA on QPS (work proxy)
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: { name: app-hpa }
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: app }
  minReplicas: 3
  maxReplicas: 30
  metrics:
  - type: Pods
    pods:
      metric: { name: http_requests_per_second }
      target: { type: AverageValue, averageValue: "150" }

---
# VPA on CPU/memory (pod size) — recommendations only
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata: { name: app-vpa }
spec:
  targetRef: { apiVersion: apps/v1, kind: Deployment, name: app }
  updatePolicy:
    updateMode: "Off"            # advisory; apply via CI
  resourcePolicy:
    containerPolicies:
    - containerName: app
      controlledResources: ["cpu", "memory"]
      minAllowed: { cpu: 100m, memory: 128Mi }
      maxAllowed: { cpu: "2",  memory: 4Gi }
```

The HPA reacts to load. The VPA tells engineers "your sizing is off"; they fix it in code, push through CI, and the next rollout has correct sizes. Best of both axes, no fight.

---

## 17. Cluster Autoscaler Architecture

The Cluster Autoscaler (CA) is a separate binary, not part of `kube-controller-manager`. It runs as a Deployment in `kube-system`. One CA instance per cluster, leader-elected for HA.

```
                     ┌──────────────────────────────────────────────────┐
                     │            CA reconcile loop (every ~10s)        │
                     └──────────────────────────────────────────────────┘

   ┌──────────────────────────┐
   │ 1. List pods             │  → unschedulable pods (status.conditions
   │    via informer cache    │    = PodScheduled: False, reason=Unschedulable)
   └────────────┬─────────────┘
                │
                ▼
   ┌──────────────────────────┐
   │ 2. For each unschedulable│  Use the scheduler's predicate/framework
   │    pod, simulate         │  in-process: would adding a node from
   │    scheduling against    │  node group X allow this pod to schedule?
   │    each node group       │
   └────────────┬─────────────┘
                │
                ▼
   ┌──────────────────────────┐
   │ 3. Pick best node group  │  Apply --expander (random, most-pods,
   │    via expander logic    │  least-waste, price, priority, grpc)
   └────────────┬─────────────┘
                │
                ▼
   ┌──────────────────────────┐
   │ 4. Increase desired      │  AWS:   UpdateAutoScalingGroup
   │    size of that ASG/MIG/ │  GCP:   resize on MIG
   │    VMSS via cloud SDK    │  Azure: scale VMSS
   └──────────────────────────┘

       — parallel: scale-down loop, every --scan-interval —

   ┌──────────────────────────┐
   │ A. For each node,        │
   │    is it underutilized   │  CPU+mem requests < --scale-down-utilization-threshold
   │    AND has been so for   │  (default 0.5) for --scale-down-unneeded-time
   │    the unneeded-time?    │  (default 10m)
   └────────────┬─────────────┘
                │
                ▼
   ┌──────────────────────────┐
   │ B. Can all its pods be   │  Use scheduler simulation again: can each
   │    rescheduled elsewhere │  pod fit somewhere else, respecting PDBs?
   │    safely?               │
   └────────────┬─────────────┘
                │
                ▼
   ┌──────────────────────────┐
   │ C. Cordon, drain, then   │  honors PDB; uses pod eviction API
   │    decrease ASG/MIG size │
   └──────────────────────────┘
```

### Why simulation, not heuristics

The CA does not just look at "is there a node with X free CPU." Real scheduling requires:

- Taints and tolerations
- Node selectors and affinity rules
- Pod affinity / anti-affinity
- Topology spread constraints
- Resource limits (CPU, memory, ephemeral storage, custom resources like `nvidia.com/gpu`)
- Volume node affinity (zone matching for PV-bound pods)

To get all of that right, the CA links the actual scheduler framework as a library (`k8s.io/kubernetes/pkg/scheduler/...`) and runs it against an in-memory snapshot. For each node group, it computes "a template node looks like this," adds the template node to the snapshot, and asks the scheduler "now does this pod fit?" If yes, that node group is a candidate.

### Node groups

Every cloud provider exposes a "group of identical VMs" abstraction:

| Cloud | Node group |
|-------|-----------|
| AWS | Auto Scaling Group (ASG) |
| GCP | Managed Instance Group (MIG) |
| Azure | Virtual Machine Scale Set (VMSS) |
| OpenStack | Heat ResourceGroup |
| vSphere | Cluster API MachineDeployment |

The CA's contract with a node group is: *all instances are identical*. Same instance type, same labels, same taints. This is the source of the CA's biggest limitation — see §24. If you want some nodes to be `c6i.2xlarge` and others `r6i.4xlarge`, you create two ASGs; the CA picks one or the other for each unschedulable pod, but it cannot mix within a group.

### Source paths

- `kubernetes/autoscaler/cluster-autoscaler/main.go` — entry point
- `kubernetes/autoscaler/cluster-autoscaler/core/static_autoscaler.go` — main loop
- `kubernetes/autoscaler/cluster-autoscaler/simulator/` — scheduler simulation
- `kubernetes/autoscaler/cluster-autoscaler/expander/` — expanders
- `kubernetes/autoscaler/cluster-autoscaler/cloudprovider/{aws,gce,azure,...}/` — cloud bindings

---

## 18. CA Scale-Up: Simulating the Scheduler

The CA scale-up loop is the most algorithmically interesting part. Let's walk through one cycle in detail.

### Step 1: Snapshot the cluster

```go
// Conceptual
snapshot := simulator.NewSnapshot(
    allNodes(),
    allPods(),
)
```

The snapshot is an immutable, in-memory copy of the cluster state at decision time. All subsequent decisions are made against this snapshot, then applied at the end. This avoids the "decide based on stale state, race with the scheduler" problem.

### Step 2: Find unschedulable pods

```
   For each Pod p in snapshot:
       if p.Status.Conditions has (type: PodScheduled, status: False, reason: Unschedulable):
           if not yet attempted in this cycle:
               candidates.append(p)
```

A pod is "unschedulable" when the scheduler explicitly recorded that it couldn't be placed. The reason field tells you why ("Insufficient cpu," "Insufficient memory," "no nodes match node selector"). The CA only acts on pods that have been marked unschedulable; it does *not* speculatively grow ahead of demand.

### Step 3: Simulate for each node group

```go
for _, nodeGroup := range allNodeGroups {
    templateNode := nodeGroup.TemplateNode()  // what a new node would look like
    snapshotCopy := snapshot.Clone()
    snapshotCopy.AddNode(templateNode)

    for _, pod := range candidates {
        if scheduler.WouldSchedule(pod, snapshotCopy) {
            // this pod could fit on a new node from this group
            matchedPods[nodeGroup] = append(matchedPods[nodeGroup], pod)
        }
    }
}
```

The simulation determines, for each `(node group, pod)` pair, whether adding one node from that group would let the pod schedule. The result is a map from node group → list of pods that group can serve.

### Step 4: Pick winners via expander

If `matchedPods` has only one entry, that group is the winner. If multiple groups could serve the same set of pods, the expander breaks the tie (see §20).

### Step 5: Bin-pack to determine how many nodes

After picking a group, the CA bin-packs the matched pods onto template nodes. If 30 pending pods can fit on a single template node, the CA grows the group by 1. If they need 4 nodes, the CA grows by 4. This computation respects all scheduler constraints including pod anti-affinity (which often dominates the count — "spread one replica per node" means N replicas need N nodes).

### Step 6: Call cloud API

```
   ASG checkout-workers: desired 12 → 14
```

The CA calls the cloud provider's "set desired capacity" API and returns. It does *not* block on instance readiness. The next reconcile (10 seconds later) will see whether the new nodes joined the cluster.

### Step 7: Mark pods as "tried"

The CA tags unschedulable pods with the time of last scale-up attempt. If the new nodes don't join within `--max-node-provision-time` (default 15 minutes), the CA gives up on that group for that pod and tries another (or marks the pod's request as "unfulfillable"). This is the **capacity-failure path**: when AWS literally has no `m6i.4xlarge` available in `us-east-1c`, the CA must back off and try a different group or a different zone. The CA does not retry forever.

### Behavior when no group fits

If no node group can satisfy a pending pod (e.g., the pod requires a GPU but no GPU groups are configured), the CA emits an event `NotTriggerScaleUp` on the pod with a message explaining which constraints prevented matching. This event is the canonical signal "you have an unfulfillable pod, please look at your node group setup."

---

## 19. CA Scale-Down: Underutilization and Safety

Scale-down is the dangerous direction. Removing a node disrupts every pod on it. The CA is correspondingly conservative.

### The underutilization gate

A node is a candidate for removal when:

1. **Utilization is below threshold.** Sum of pod `requests` (not actual usage!) is less than `--scale-down-utilization-threshold` (default 0.5, i.e., 50%) of the node's allocatable. CPU and memory are evaluated separately; both must be below threshold.

2. **Has been below threshold for `--scale-down-unneeded-time`** (default 10 minutes). This is the time-decay equivalent of a stabilization window. A node that briefly drops to 30% utilization during a deploy doesn't count.

3. **Has not been recently scaled up.** `--scale-down-delay-after-add` (default 10 minutes) prevents scale-down right after scale-up, which would thrash.

4. **All its pods can be rescheduled elsewhere.** This is the expensive check: for each pod on the candidate node, simulate scheduling it onto any other existing node. If any pod cannot be placed (respecting PDBs, affinities, etc.), the node is *not* a candidate.

### Pods that block scale-down

| Pod property | Effect |
|--------------|--------|
| `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"` annotation | Blocks scale-down absolutely. |
| Pod from a DaemonSet (and not annotated as ignorable) | Default: ignored (DaemonSets don't block); flip via `--daemonset-eviction-for-empty-nodes`. |
| Pod with local storage (`emptyDir` size > 0) | Blocks by default unless `cluster-autoscaler.kubernetes.io/safe-to-evict: "true"`. |
| Pod with PV bound to specific node | Blocks. |
| Pod managed by a PDB at `disruptionsAllowed: 0` | Blocks. |
| Mirror pod (static pods) | Ignored. |
| `kube-system` pods (without proper annotations) | Default behavior: blocks scale-down. Override with `--skip-nodes-with-system-pods=false`. |

The cumulative effect is that "safe scale-down" is rare in real clusters. A node running CoreDNS, kube-proxy (DaemonSet, ignored), one prometheus-node-exporter (DaemonSet, ignored), and one user pod with an `emptyDir`/PV blocks scale-down until either the user pod is movable or someone explicitly annotates.

### The drain protocol

When a node is selected for removal:

```
   1. Cordon (taint NoSchedule)        — kubectl cordon equivalent
   2. For each pod on the node (in deterministic order):
        a. Honor PDB (eviction API blocks if PDB violated)
        b. Pod is recreated by its owner (Deployment / RS / SS)
        c. Wait for new pod to be scheduled and Ready
   3. Once all pods drained:
        a. Decrease node group size by 1
        b. Cloud API terminates instance
        c. Node object deleted from cluster (via node controller)
```

The drain is sequential per node but parallel across nodes (up to `--max-empty-bulk-delete` empty nodes at a time, default 10; or `--max-scale-down-parallelism` for the general case, default 10).

### Empty-node fast path

A node with *only DaemonSet pods* (i.e., functionally empty) can be removed faster: no eviction is needed, just deletion. `--max-empty-bulk-delete` controls how many can go at once. This is the path most exercised by typical workload-down events.

### Scale-down config map (a real example)

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-autoscaler-status
  namespace: kube-system
data:
  status: |
    Cluster-autoscaler status at 2026-05-23 14:22:08:
    Cluster-wide:
      Health:      Healthy (ready=18 unready=0 notStarted=0 longNotStarted=0 registered=18)
      ScaleUp:     NoActivity (ready=18 registered=18)
      ScaleDown:   CandidatesPresent (candidates=2)

    NodeGroups:
      Name:        checkout-workers-us-east-1a
      Health:      Healthy (ready=6 cloudProviderTarget=6 minSize=2 maxSize=20)
      ScaleUp:     NoActivity
      ScaleDown:   NoCandidates
      Name:        batch-workers-us-east-1a
      Health:      Healthy (ready=4 cloudProviderTarget=4 minSize=0 maxSize=15)
      ScaleUp:     NoActivity
      ScaleDown:   CandidatesPresent (candidates=2)
```

This is the canonical operator view: which groups are healthy, how many nodes are candidates for removal, what scale operations are in flight. Pin this in your operations dashboard.

---

## 20. CA Expanders: How a Node Group Is Chosen

When the CA finds multiple node groups that could each satisfy a set of pending pods, the `--expander` flag controls the tiebreaker. As of CA 1.26+, you can pass multiple expanders comma-separated; they're applied in order until one returns a unique winner.

| Expander | Decision rule | When to use |
|----------|---------------|-------------|
| `random` | Pick at random | Default; almost never the right choice. |
| `most-pods` | Pick the group that would let the most pending pods schedule | Maximizes parallelism per scale-up event. |
| `least-waste` | Pick the group whose template node has the least leftover CPU/memory after placing the matched pods | Bin-packing optimizer; minimizes overprovisioning. |
| `price` | Pick the cheapest group (uses cloud-provider pricing data) | Cost-driven environments. AWS spot integration. |
| `priority` | Pick by user-defined ordered priority (configmap-driven) | Multi-region preference, lifecycle preferences. |
| `grpc` | Defer to an external gRPC service | Custom logic outside CA. |

### The `priority` expander config

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-autoscaler-priority-expander
  namespace: kube-system
data:
  priorities: |-
    100:
      - .*spot.*us-east-1a.*
      - .*spot.*us-east-1b.*
    50:
      - .*spot.*us-east-1c.*
    10:
      - .*on-demand.*
```

The CA checks groups in descending priority order. Highest matching priority wins; ties within a priority level fall through to the next expander.

A real-world stack: `--expander=priority,least-waste,random`. Try priority first (prefer spot); fall back to least-waste (bin-pack); finally tiebreak with random.

### `least-waste` worked example

Pending pod requirements: 6 vCPU, 16 GiB memory.

Group A template: `m6i.2xlarge` (8 vCPU, 32 GiB). After placing the pod, leftover = 2 vCPU + 16 GiB.
Group B template: `c6i.4xlarge` (16 vCPU, 32 GiB). After placing the pod, leftover = 10 vCPU + 16 GiB.

`least-waste` picks Group A (smaller leftover).

But: if there are *six* pending pods of that size, Group A fits zero (the second pod doesn't fit on the same 8-vCPU node), Group B fits two per node. `most-pods` would beat `least-waste` here. Combine expanders thoughtfully.

### `price` and the spot story

The `price` expander uses cloud provider pricing data (AWS: `aws ec2 describe-spot-price-history`). For AWS, you typically run one ASG per (instance type × zone × purchase option), and `price` picks the cheapest at decision time. This is *the* feature spawned Karpenter — once you accept that you want price-aware multi-instance-type selection, the CA's "one type per group" model is the limiting factor.

### Why `random` is the default

History. When CA shipped, expanders were experimental. `random` was the predictable fallback. Modern clusters override it; legacy clusters still run `random` and overprovision quietly. Audit your CA flags.

---

## 21. CA Scale-from-Zero

A long-standing CA pain point: a node group with `minSize: 0` couldn't be scaled up because the CA needed an existing node to know what a "node from this group" looks like (resources, labels, taints).

CA 1.14+ added "scale-from-zero" by reading the node group's metadata directly from the cloud provider — AWS Launch Template, GCP instance template, Azure VMSS profile. Now the CA can synthesize a template node without ever having had one.

### Required tags / labels

The CA needs to know the *node labels* and *taints* the new node will have *before* it boots. The cloud provider tag system carries this.

**AWS ASG tags** (consumed by CA when `minSize=0`):

```
   k8s.io/cluster-autoscaler/node-template/label/topology.kubernetes.io/zone = us-east-1a
   k8s.io/cluster-autoscaler/node-template/label/node.kubernetes.io/instance-type = m6i.2xlarge
   k8s.io/cluster-autoscaler/node-template/label/workload-class = batch
   k8s.io/cluster-autoscaler/node-template/taint/dedicated = batch:NoSchedule
   k8s.io/cluster-autoscaler/node-template/resources/nvidia.com/gpu = 1
   k8s.io/cluster-autoscaler/enabled = true
   k8s.io/cluster-autoscaler/<cluster-name> = owned
```

**GCP / Azure** have analogous metadata fields.

If you forget these tags and set `minSize: 0`, the CA will not scale the group up because it can't predict whether the pending pod's node selector / tolerations match. You'll see `NotTriggerScaleUp` events with reasons like "node label X does not match pod selector."

### ConfigMap for CA flags

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cluster-autoscaler
  namespace: kube-system
spec:
  template:
    spec:
      containers:
      - name: cluster-autoscaler
        image: registry.k8s.io/autoscaling/cluster-autoscaler:v1.30.1
        command:
        - ./cluster-autoscaler
        - --v=4
        - --stderrthreshold=info
        - --cloud-provider=aws
        - --skip-nodes-with-local-storage=false
        - --skip-nodes-with-system-pods=false
        - --expander=priority,least-waste
        - --balance-similar-node-groups
        - --scale-down-utilization-threshold=0.5
        - --scale-down-unneeded-time=10m
        - --scale-down-delay-after-add=10m
        - --max-node-provision-time=15m
        - --node-group-auto-discovery=asg:tag=k8s.io/cluster-autoscaler/enabled,k8s.io/cluster-autoscaler/my-cluster
        env:
        - name: AWS_REGION
          value: us-east-1
```

The `--node-group-auto-discovery` flag is how CA finds groups: it scans for ASGs with the magic tags. The CA does *not* require a static list of groups.

### Scale-from-zero limitations

**The template tags must be correct.** A label on the template that doesn't actually exist on real nodes (because the kubelet doesn't add it) means the CA scales up, the new node boots without that label, and the pending pod still can't be scheduled. The CA marks the scale-up as failed after `--max-node-provision-time`. You'll see this most often with custom labels added by node-bootstrapping scripts that didn't propagate to the ASG tags.

**Resources beyond CPU/memory need explicit tags.** GPUs, hugepages, custom-resource-name fields — all need `resources/<name> = <count>` tags. The CA does not query the cloud provider for "what extended resources does this instance type have."

---

## 22. Karpenter Architecture and NodePool

Karpenter (originally AWS, now CNCF) is the next-generation node autoscaler. It throws away the node-group abstraction and provisions instances directly per pending pod. The model is fundamentally different from CA:

| Concept | CA | Karpenter |
|---------|----|-----------|
| Unit of scale | Node group (homogeneous) | Individual instance |
| Instance selection | Cloud-provider scaling group decides | Karpenter decides |
| Multi-instance-type | One group per type | Single NodePool can fit hundreds of types |
| Capacity type mix | One group per (on-demand vs spot) | Single NodePool can mix |
| Decision speed | ~30-60s cloud round-trip + cycle time | ~10s, often parallelized |
| Bin-packing | Implicit via group config | Explicit, per-pod |
| Consolidation | Manual: drain + scale-down | First-class operation |
| Drift | Not really a concept | First-class operation |

### NodePool: the "kind of nodes we want"

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: default
spec:
  template:
    metadata:
      labels:
        billing-team: platform
    spec:
      requirements:
      - key: karpenter.k8s.aws/instance-category
        operator: In
        values: ["c", "m", "r"]
      - key: karpenter.k8s.aws/instance-cpu
        operator: In
        values: ["4", "8", "16", "32"]
      - key: karpenter.k8s.aws/instance-generation
        operator: Gt
        values: ["5"]
      - key: kubernetes.io/arch
        operator: In
        values: ["amd64", "arm64"]
      - key: karpenter.sh/capacity-type
        operator: In
        values: ["spot", "on-demand"]
      - key: topology.kubernetes.io/zone
        operator: In
        values: ["us-east-1a", "us-east-1b", "us-east-1c"]
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      taints:
      - key: example.com/special
        value: "true"
        effect: NoSchedule
      expireAfter: 720h               # 30 days; trigger drift
  limits:
    cpu: 2000
    memory: 4000Gi
  weight: 10
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 30s
    budgets:
    - nodes: 10%
    - nodes: "0"
      schedule: "0 9 * * mon-fri"     # no disruption during business-hours peak
      duration: 8h
```

Notable fields.

**`requirements`** is a list of `NodeSelectorRequirement` — the same shape as `nodeAffinity`. Karpenter intersects this set with each pending pod's nodeAffinity / nodeSelector / topology constraints to find candidate instance types. For the above pool, candidates are: c/m/r-family AMD64 or ARM64 instances with 4-32 vCPU, generation > 5, any of three zones, spot or on-demand. That's potentially hundreds of instance types.

**`nodeClassRef`** points to an `EC2NodeClass` (AWS), `AKSNodeClass`, `GKENodeClass`, etc. — the cloud-specific shape: AMI, security groups, subnets, IAM role, user data, block device mappings. NodePool is cloud-neutral; NodeClass is cloud-specific.

**`expireAfter`** triggers drift after the node has been alive that long. With 720h (30 days), nodes are rotated monthly even if nothing else changed — picks up new AMIs, kernel patches, etc.

**`weight`** lets multiple NodePools coexist with preference: higher weight wins when both could fit a pod.

**`disruption`** controls voluntary disruption (consolidation, drift, expiration). See §23.

### EC2NodeClass: the AWS-specific shape

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: default
spec:
  amiFamily: AL2023
  amiSelectorTerms:
  - alias: al2023@latest
  subnetSelectorTerms:
  - tags:
      karpenter.sh/discovery: my-cluster
  securityGroupSelectorTerms:
  - tags:
      karpenter.sh/discovery: my-cluster
  role: KarpenterNodeRole-my-cluster
  tags:
    Environment: production
    karpenter.sh/discovery: my-cluster
  blockDeviceMappings:
  - deviceName: /dev/xvda
    ebs:
      volumeSize: 100Gi
      volumeType: gp3
      iops: 3000
      throughput: 125
      encrypted: true
  detailedMonitoring: true
  metadataOptions:
    httpEndpoint: enabled
    httpTokens: required               # IMDSv2 only
    httpPutResponseHopLimit: 1
  userData: |
    #!/bin/bash
    /etc/eks/bootstrap.sh my-cluster \
      --kubelet-extra-args '--node-labels=foo=bar'
```

### How Karpenter provisions

```
                Karpenter pod-watcher loop:

   1. Watch pods (informer)
   2. Filter: pod is unschedulable AND
              owned by a NodePool's requirements (via pod nodeSelector etc.)
   3. Batch pending pods over a short window (default 1s)
   4. For each batch, compute the "best" instance(s):
      - Bin-pack all pods onto a hypothetical instance,
        try every candidate type that satisfies requirements,
        score by $/pod and waste
   5. CreateFleet on AWS (or equivalent) for the chosen instances
   6. Watch new nodes register; create NodeClaim objects to track them
```

The batching window (1 second default) is key: it lets Karpenter make one big bin-packing decision per scale-up event instead of provisioning a new instance per pod. The result is dramatically better packing than CA, which makes one decision per node-group at a time.

### NodeClaim: the per-instance object

For every node Karpenter provisions, it creates a `NodeClaim` CR. The NodeClaim links a Node object to its NodePool, tracks the cloud instance ID, and is the unit of drift detection.

```yaml
# kubectl get nodeclaim
NAME                     TYPE          ZONE         NODE                                            READY   AGE
default-7vmqf            c6i.xlarge    us-east-1a   ip-10-0-1-23.us-east-1.compute.internal        True    12m
default-q9k8b            r6i.2xlarge   us-east-1b   ip-10-0-2-47.us-east-1.compute.internal        True    3h
default-z2nbg            m6i.large     us-east-1c   ip-10-0-3-91.us-east-1.compute.internal        True    18m
```

---

## 23. Karpenter Consolidation and Drift

The single biggest cost-savings feature in Karpenter is **consolidation**: actively replacing nodes with cheaper ones (or removing them entirely) when the pods on them could fit better elsewhere.

### Consolidation modes

```yaml
disruption:
  consolidationPolicy: WhenEmptyOrUnderutilized    # or WhenEmpty
  consolidateAfter: 30s
```

**`WhenEmpty`** — only consolidate when a node has zero workload pods (DaemonSets ignored). Conservative; matches CA's empty-node scale-down.

**`WhenEmptyOrUnderutilized`** — also consolidate when a node could be replaced by a smaller/cheaper instance, or when its pods could move to other existing nodes leaving this one empty. This is the aggressive mode and is the default for new NodePools.

**`consolidateAfter`** — wait this long after the last pod change before consolidating. Prevents bouncing during deploys.

### Three types of consolidation

```
   1. Empty node deletion
   ──────────────────────────────
   Node A: [DaemonSets only]
   Action: terminate A.

   2. Single-node consolidation (replace with smaller)
   ──────────────────────────────────────────────────
   Node A: m6i.2xlarge, $0.384/hr, hosting 4 pods using 4 vCPU + 8 GiB
   Replacement: c6i.xlarge, $0.17/hr (4 vCPU + 8 GiB)
   Savings: $0.21/hr per node ≈ $150/month
   Action: provision new c6i.xlarge, drain A onto it, terminate A.

   3. Multi-node consolidation (combine)
   ─────────────────────────────────────
   Node A: 30% utilized
   Node B: 25% utilized
   Combined would fit on one node.
   Action: provision/select target node, drain both A and B onto it,
           terminate the originals.
```

The cost model is: only consolidate if the replacement is strictly cheaper (using the cloud provider's pricing API) and the move is safe (PDBs honored, all pods reschedulable).

### Drift

A node is "drifted" when its current configuration no longer matches what its NodePool/NodeClass would produce *today*. Examples:

- AMI updated in the NodeClass (`amiSelectorTerms` resolves to a newer image)
- Instance type removed from NodePool requirements
- Tags changed
- User data changed
- Node has exceeded `expireAfter`

Drift triggers a voluntary disruption: Karpenter creates a new node matching current spec, drains the drifted node onto it, and terminates the drifted node. This is how you do "rolling node OS upgrades" with Karpenter — change the AMI alias, Karpenter rotates every node within `disruption.budgets`.

```
   kubectl describe nodeclaim default-7vmqf
   ...
   Conditions:
     Type           Status   Reason             Message
     ----           ------   ------             -------
     Drifted        True     AmiDrift           AMI 'ami-0123' is no longer the
                                                 latest match for amiSelectorTerms
     Initialized    True
     Ready          True
   ...
```

### Disruption budgets

Voluntary disruption (consolidation, drift, expiration) is rate-limited by budgets.

```yaml
disruption:
  budgets:
  - nodes: 10%                  # default: at most 10% of nodes disrupting at once
  - nodes: "0"                  # zero disruptions during business hours
    schedule: "0 9 * * mon-fri"
    duration: 8h
  - nodes: "5"                  # absolute count budget for batch hours
    schedule: "0 23 * * *"
    duration: 4h
```

Multiple budgets are evaluated as a set; the most restrictive applies. The `schedule` field is a standard cron expression. This gives operators "no disruption during Black Friday peak" semantics natively, where CA required custom logic.

### Forceful vs voluntary disruption

Karpenter distinguishes:

- **Voluntary** — consolidation, drift, expiration. Subject to disruption budgets, PDBs honored, drains gracefully.
- **Forceful** — health-check failures, manual `kubectl delete node`, NodeClaim deletion. Not subject to budgets.

PDBs still apply for forceful disruption *during eviction*, but the decision to evict has already been made.

---

## 24. Karpenter vs Cluster Autoscaler: Tradeoffs

| Dimension | CA | Karpenter |
|-----------|-----|-----------|
| **Setup complexity** | Mature; works with `eksctl`, terraform-eks-module, etc. | Newer; AWS-first, GKE/AKS support evolving |
| **Configuration model** | Node groups (one per type/zone/lifecycle) | Single NodePool can span dozens of types |
| **Cloud support** | AWS, GCP, Azure, OpenStack, vSphere, Equinix, Hetzner, ... | AWS (mature), Azure (GA), GCP (GA), others (community) |
| **Instance selection** | Pick group, group picks type | Pick instance type per scale-up |
| **Bin-packing** | Per-group, after the fact | Active across the cluster |
| **Spot integration** | Per-group spot ASG, spot-or-OD | Native mix, spot interruption handled |
| **Scale-up latency** | ~60-120s (ASG round-trip + cycle) | ~30-60s (CreateFleet direct) |
| **Consolidation** | Empty-node only (manual otherwise) | Active replacement of underutilized |
| **Drift** | Not modeled | First-class |
| **GitOps integration** | Static config | CR-driven, fits GitOps |
| **State complexity** | Stateless (reads from cloud + apiserver) | NodeClaim CRs (additional state to manage) |
| **Multi-arch (ARM)** | Possible with separate groups | Native |
| **Custom resources (GPU)** | Tag-driven; works | Native via NodePool requirements |

### When to stay on CA

- You're running on a cloud where Karpenter isn't yet GA (some niche providers, OpenStack).
- Your cluster is small, stable, and the cost of managed groups is fine.
- You need very specific node-group semantics that don't map to NodePool (e.g., one ASG per tenant with strict cost attribution).
- You're using a managed Kubernetes service where Karpenter is not the supported autoscaler.

### When to migrate to Karpenter

- You're paying for over-provisioning. The combination of multi-instance-type + active consolidation typically saves 20-50% on compute spend.
- You want spot/on-demand mix at the pod level.
- You're frustrated by ASG-level scale-from-zero ergonomics.
- You want rolling AMI updates via drift.

### Migration path

You can run both at the same time. Most teams gradually move workloads:

1. Install Karpenter alongside CA.
2. Create a Karpenter NodePool with a `nodeSelector` like `runtime: karpenter` and add that selector/toleration to one workload's PodTemplate.
3. Watch Karpenter provision; observe correctness.
4. Migrate the next workload.
5. Eventually shrink CA-managed ASGs to `min=0`; Karpenter handles everything new.

Critical: CA and Karpenter cannot manage the same set of nodes. Each must manage disjoint sets, marked with tags or NodePool-specific selectors.

---

## 25. KEDA: Event-Driven Autoscaling

KEDA (Kubernetes Event-Driven Autoscaling) — CNCF graduated as of 2024 — is the autoscaler that makes the HPA useful for non-CPU workloads. It does not replace the HPA. Internally, KEDA *creates* an HPA, serves the metric the HPA needs via `external.metrics.k8s.io`, and lets the HPA do the actual scaling.

```
              ┌────────────────────────────────────────────────────┐
              │                  KEDA architecture                  │
              └────────────────────────────────────────────────────┘

   ┌─────────────────┐
   │ User creates    │       apiVersion: keda.sh/v1alpha1
   │ ScaledObject    │       kind: ScaledObject
   │                 │       spec: { scaleTargetRef, triggers, ... }
   └────────┬────────┘
            │ create
            ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  keda-operator (controller)                                  │
   │                                                              │
   │  For each ScaledObject:                                      │
   │    1. Create/update an HPA with spec.metrics derived from    │
   │       the ScaledObject's triggers.                           │
   │       Each trigger → one External metric on the HPA.         │
   │    2. Register the metric on keda-operator-metrics-apiserver │
   │       (the APIService for external.metrics.k8s.io).          │
   │    3. Manage scale-to-zero: when all triggers report zero,   │
   │       deactivate (scale to 0); when any trigger reports work,│
   │       activate (scale up to minReplicas).                    │
   └─────────────────────────────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  HPA (created by KEDA, but a real HPA)                       │
   │                                                              │
   │  Reads metrics from external.metrics.k8s.io                  │
   │  Computes desiredReplicas via the standard formula           │
   │  Patches the target's /scale subresource                     │
   └────────┬────────────────────────────────────────────────────┘
            │ query metric
            ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  keda-operator-metrics-apiserver                             │
   │                                                              │
   │  For each query, look up the ScaledObject, invoke the        │
   │  trigger's scaler logic (Kafka client, SQS client, etc.),    │
   │  return the metric value.                                    │
   └─────────────────────────────────────────────────────────────┘
            │ direct connection
            ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  Event source: Kafka, SQS, Pub/Sub, Redis, Prometheus, ...    │
   └─────────────────────────────────────────────────────────────┘
```

### Why this design

The HPA is the canonical autoscaling primitive. It handles behavior, stabilization, multi-metric `max()`, the `/scale` subresource. KEDA's authors saw no reason to reimplement that; they layered on top. Every ScaledObject becomes an HPA, and the HPA does what HPAs do.

The novelty is on the *metrics* side. The HPA can only read pre-existing metrics APIs; you can't tell it "scale on Kafka lag" directly. KEDA's metrics-apiserver translates the HPA's polling queries into live calls to the event source.

### ScaledObject manifest

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: orders-consumer
  namespace: orders
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: orders-consumer
  pollingInterval: 30                    # how often KEDA queries the source
  cooldownPeriod: 300                    # how long to wait after triggers go to 0
  idleReplicaCount: 0                    # scale to 0 when idle (omit for non-zero idle)
  minReplicaCount: 1                     # min when active
  maxReplicaCount: 50
  fallback:
    failureThreshold: 3
    replicas: 5                          # if metric source unreachable, use this
  advanced:
    horizontalPodAutoscalerConfig:
      name: keda-hpa-orders-consumer     # opt-in: name the underlying HPA
      behavior:
        scaleDown:
          stabilizationWindowSeconds: 300
          policies:
          - type: Percent
            value: 50
            periodSeconds: 60
        scaleUp:
          stabilizationWindowSeconds: 0
          policies:
          - type: Percent
            value: 100
            periodSeconds: 30
  triggers:
  - type: kafka
    metadata:
      bootstrapServers: kafka.bus.svc.cluster.local:9092
      consumerGroup: orders-consumer
      topic: orders
      lagThreshold: "50"                  # target lag per replica
      offsetResetPolicy: latest
    authenticationRef:
      name: kafka-trigger-auth
  - type: prometheus
    metadata:
      serverAddress: http://prometheus.monitoring.svc:9090
      threshold: "100"
      query: sum(rate(orders_outbound_pending[2m]))
```

Key fields beyond what an HPA has.

**`pollingInterval`** is how often KEDA actively queries the event source. This is the analog of `metric-resolution` for metrics-server. Lower = more responsive, higher = less load on Kafka/SQS/etc.

**`cooldownPeriod`** is the *idle* cool-down: how long all triggers must report zero before KEDA scales to `idleReplicaCount`. This is in addition to the HPA's `scaleDown.stabilizationWindowSeconds`.

**`idleReplicaCount`** enables scale-to-zero. Set to 0, and when all triggers say "no work," the workload scales to 0 (saving CPU, memory, license cost). When work arrives, KEDA scales back up to `minReplicaCount` first, then the HPA scales further as needed.

**`fallback`** is "what if I can't reach the metric source?" Without this, the HPA goes to `ScalingActive=False` and freezes at whatever replica count it has. With a fallback, it scales to a known-safe count instead. Use this for any production ScaledObject.

**`advanced.horizontalPodAutoscalerConfig.behavior`** propagates `behavior` into the underlying HPA. Use this — without it, the HPA gets default behavior.

---

## 26. KEDA Scalers and Triggers

KEDA ships 60+ scalers as of v2.16. The common ones:

| Scaler | What it measures | Typical use |
|--------|------------------|-------------|
| `kafka` | Consumer-group lag per partition | Kafka consumer workloads |
| `rabbitmq` | Queue depth or message rate | RabbitMQ consumer workloads |
| `aws-sqs-queue` | `ApproximateNumberOfMessages` | SQS consumer workloads |
| `gcp-pubsub` | Subscription backlog | Pub/Sub workers |
| `azure-servicebus` | Queue/topic message count | Azure ServiceBus consumers |
| `prometheus` | PromQL expression result | Anything you can write in PromQL |
| `postgresql` | SQL query returning a number | Pending-jobs tables |
| `redis` / `redis-streams` | List length / stream length | Redis-backed queues |
| `mysql` / `mssql` | SQL query | Same as postgres |
| `cron` | Time-based; constant value during a window | Predictable bursts (business hours) |
| `cpu` / `memory` | Wraps Resource metrics | Use HPA directly unless you need other triggers in the same ScaledObject |
| `http-add-on` | HTTP request rate via KEDA's add-on interceptor | Scale-to-zero for HTTP services |
| `external` | Custom gRPC scaler | Anything not built-in |

### Kafka scaler in detail

```yaml
triggers:
- type: kafka
  metadata:
    bootstrapServers: kafka.bus.svc.cluster.local:9092
    consumerGroup: orders-consumer
    topic: orders
    lagThreshold: "50"
    activationLagThreshold: "10"       # don't activate until lag > 10
    offsetResetPolicy: latest
    allowIdleConsumers: "false"        # never scale beyond partition count
    scaleToZeroOnInvalidOffset: "true" # if offset is uncommitted, treat as 0
    excludePersistentLag: "false"      # don't count partitions with no progress
  authenticationRef:
    name: kafka-trigger-auth
```

**`lagThreshold: 50`** means "target: 50 messages of lag per replica." Total lag of 500 → 10 replicas wanted.

**`activationLagThreshold`** is the scale-from-zero gate. KEDA activates (scales 0 → minReplicas) when *total* lag exceeds this. Below it, the workload stays at 0. This prevents a single straggler message from waking the workload — important when activation cost is non-trivial (model loading, cold-start, etc.).

**`allowIdleConsumers: false`** caps replicas at the partition count. Kafka can only deliver to N consumers in a group where N = partitions. Scaling above that is wasteful.

**`authenticationRef`** points to a `TriggerAuthentication`:

```yaml
apiVersion: keda.sh/v1alpha1
kind: TriggerAuthentication
metadata:
  name: kafka-trigger-auth
  namespace: orders
spec:
  secretTargetRef:
  - parameter: sasl
    name: kafka-secret
    key: sasl-mechanism
  - parameter: username
    name: kafka-secret
    key: username
  - parameter: password
    name: kafka-secret
    key: password
```

### Prometheus scaler

```yaml
triggers:
- type: prometheus
  metadata:
    serverAddress: http://prometheus.monitoring.svc:9090
    metricName: pending_orders            # used as the external metric name
    threshold: "100"                       # target value
    activationThreshold: "10"
    query: |
      sum(rate(orders_pending_total[2m]))
```

This is the universal escape hatch. Any signal Prometheus can answer becomes a KEDA scaler. We've seen teams scale on:

- Database connection pool saturation (`pg_stat_activity` count)
- Garbage collection rate (`go_gc_duration_seconds`)
- Latency p99 (`histogram_quantile(0.99, ...)`) — controversial; works for steady-state but spiky
- External API rate-limit remaining (custom exporter)

The trick is the same as with prometheus-adapter: get the PromQL right and the time window right.

### Cron scaler — predictable scaling

```yaml
triggers:
- type: cron
  metadata:
    timezone: America/Los_Angeles
    start: "0 9 * * *"
    end: "0 17 * * *"
    desiredReplicas: "30"
```

During the 9 AM - 5 PM window, the cron trigger contributes a desired value of 30. Outside the window, it contributes 0. Combine with other triggers (CPU, queue depth) for "ensure at least 30 during business hours, scale higher if needed."

This is the canonical "pre-warm before peak" pattern. It's also how teams paper over slow scale-up (LLM model loading, JVM warm-up): pre-warm 10 minutes before the expected peak using cron, then let the load-driven triggers scale further.

---

## 27. ScaledJob: Per-Event Jobs

KEDA has a second resource — `ScaledJob` — for workloads where each event should spawn a single short-lived Job rather than scale a long-running Deployment.

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledJob
metadata:
  name: nightly-batch
  namespace: data
spec:
  jobTargetRef:
    parallelism: 5
    completions: 5
    backoffLimit: 2
    template:
      spec:
        restartPolicy: Never
        containers:
        - name: batch
          image: example/batch-runner:v3.1
          env:
          - name: QUEUE_NAME
            value: nightly-tasks
  pollingInterval: 30
  successfulJobsHistoryLimit: 5
  failedJobsHistoryLimit: 5
  maxReplicaCount: 100
  scalingStrategy:
    strategy: "default"          # default | accurate | custom
  triggers:
  - type: aws-sqs-queue
    metadata:
      queueURL: https://sqs.us-east-1.amazonaws.com/123/nightly-tasks
      queueLength: "1"
      awsRegion: us-east-1
```

### Why ScaledJob exists

A Deployment scaled to N replicas keeps the pods alive after work finishes — they sit idle waiting for the next event. For long-running consumers this is correct. For batch event processing where each "event" is a discrete unit of work (transcode this video, run this Spark job, process this ML inference), you want a *Job* per event. The pod terminates on completion, releasing its node-slot.

ScaledJob handles this by creating a new Job each time KEDA's scaler reports work, up to `maxReplicaCount` concurrent jobs.

### `scalingStrategy`

- **`default`** — at each polling interval, the number of running jobs subtracts from the demand to compute how many to create.
- **`accurate`** — also accounts for completed-but-not-yet-cleaned-up jobs to avoid double-counting.
- **`custom`** — provide a custom formula.

Default works for >95% of cases. Use accurate when polling interval is shorter than typical job runtime.

### When ScaledJob, when ScaledObject?

- **ScaledObject** when: pods process many events over their lifetime; idle-but-warm is acceptable; you want HPA-style scale-up/down.
- **ScaledJob** when: each event is a unit of work; pods should terminate after; you need backoff/retry semantics natively (Jobs have `backoffLimit`).

A common pattern: ScaledObject for the "online" message-bus consumer; ScaledJob for the "batch" worker pool fed by a different SQS queue.

---

## 28. Scale-from-Zero Semantics

Two distinct "scale-from-zero" features exist; they're often conflated.

### KEDA scale-to-zero (mature)

KEDA has supported zero-replica scaling since v1. Set `idleReplicaCount: 0` (or omit `minReplicaCount`) on a ScaledObject, and the workload scales to 0 when all triggers report zero. When work arrives, KEDA reads the trigger, sees nonzero, and *deactivates the underlying HPA's `min=0` semantics* to scale to `minReplicaCount` (typically 1).

The activation latency:

```
   T+0     Event arrives at source (Kafka topic, SQS queue)
   T+30s   KEDA's next poll detects nonzero (pollingInterval=30s)
   T+30s   KEDA modifies HPA, target scale subresource to minReplicaCount
   T+31s   Deployment controller creates pod
   T+31s   Scheduler places pod (assuming node available)
   T+45s   Pod pulls image (cache hit) and starts container
   T+50s   Readiness probe passes
   T+50s   Pod begins consuming events
```

In practice, scale-from-zero is 30-90 seconds, dominated by KEDA's polling interval. Lower pollingInterval to reduce; balance against load on the event source.

### HPA `minReplicas: 0` (1.30+)

Kubernetes 1.30 made `HPAScaleToZero` GA. The HPA itself can now have `minReplicas: 0`. Before 1.30, the HPA refused `minReplicas=0`. Now it works — but only if at least one of the HPA's metrics is `Object` or `External` (not `Resource` or `Pods`, because those require pods to exist to compute).

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
spec:
  minReplicas: 0
  maxReplicas: 20
  metrics:
  - type: External
    external:
      metric: { name: sqs_queue_depth, ... }
      target: { type: AverageValue, averageValue: "10" }
```

The semantics are simpler than KEDA's: when external metric value is 0, desired = 0. When it goes nonzero, desired = ceil(metricValue / target). The HPA patches scale, the deployment creates pods.

### Differences

| Feature | KEDA | HPA `min=0` (1.30+) |
|---------|------|---------------------|
| When did it ship? | KEDA v1, ~2019 | k/k 1.30 GA |
| Polling | KEDA's own poll | HPA's metric API path |
| Activation logic | `activationThreshold` (don't activate until X) | None — zero or nonzero |
| Event sources | 60+ scalers | Only via external.metrics.k8s.io |
| Cool-down | `cooldownPeriod` (idle) + HPA's stabilization | HPA's stabilization only |
| Maturity | Mature | New |

Recommendation: stay with KEDA for now. Even when running on 1.30+, KEDA's polling-and-activation model is more robust for event-driven scaling. The HPA `min=0` is useful for non-KEDA-managed workloads driven directly by an external metrics provider.

---

## 29. Predictive and Custom Autoscaling

The five autoscalers above (HPA, VPA, CA, Karpenter, KEDA) cover ~95% of production needs. The remaining 5% requires predictive or fully custom logic.

### Predictive autoscaling: not in tree

Kubernetes has no first-party predictive autoscaler. Several third-party projects fill the gap:

| Project | Idea |
|---------|------|
| [KEDA cron trigger](#) | Time-based, not really predictive — but covers known schedules. |
| [KEDA predictkube scaler](#) | Forecasts metrics with statistical/ML models. |
| AWS Predictive Scaling (EC2 native) | Not Kubernetes-aware; works on the ASG below CA. |
| [k8s-pod-autoscaler](https://github.com/jthomperoo/predictive-horizontal-pod-autoscaler) | Pluggable HPA replacement with ML forecasting. |
| Custom controllers + CRDs | The escape hatch. |

The recurring pattern: read history from Prometheus, train a forecaster (ARIMA, Prophet, LSTM), emit a forecast as a metric, scale on the *forecast* via standard HPA. The complexity isn't in the scaling — it's in the forecasting and the failure mode when the forecast is wrong.

### Custom controllers

Building your own autoscaler is straightforward (ch 08). Watch some objects, decide a replica count, patch `/scale`. Use this when:

- Your scaling logic requires multi-cluster awareness (the HPA is per-cluster).
- Your scaling logic must integrate with capacity-planning systems (you have an external "budget" oracle).
- You need cross-workload coordination ("scale workload A only after B is up").

Reference designs in the wild: Argo Rollouts' analysis-based scaling, Knative's scale-to-zero (Knative has its own activator and autoscaler that predate KEDA), Pinterest's Autoscaler, Lyft's KEDA-derived custom logic.

---

## 30. The Closed-Loop Feedback System

When you put HPA + VPA + CA/Karpenter + KEDA together, you get a closed-loop control system spanning seconds to hours. Understanding the feedback paths is the only way to debug autoscaling at scale.

```
                        load arrives
                              │
                              ▼
                  ┌──────────────────────┐
                  │ application pods     │
                  │ (Deployment)         │
                  └─────┬────────────────┘
                        │ emit metrics
                        ▼
                  ┌──────────────────────┐
                  │ metrics-server /     │
                  │ Prometheus / KEDA    │
                  └─────┬────────────────┘
                        │
                        ▼
                  ┌──────────────────────┐
                  │ HPA controller       │ ── reconcile every 15s
                  │ desired = N+k        │
                  └─────┬────────────────┘
                        │ patch /scale
                        ▼
                  ┌──────────────────────┐
                  │ Deployment controller│
                  │ creates k new pods   │
                  └─────┬────────────────┘
                        │ new pods Pending
                        ▼
                  ┌──────────────────────┐
                  │ kube-scheduler       │
                  │ tries to bind        │
                  └─────┬────────────────┘
                        │ no fit → Unschedulable
                        ▼
                  ┌──────────────────────┐
                  │ CA / Karpenter       │ ── reconcile every 10s
                  │ provisions nodes     │
                  └─────┬────────────────┘
                        │
                        ▼
                  ┌──────────────────────┐
                  │ cloud provider       │
                  │ launches VM          │
                  └─────┬────────────────┘
                        │ ~60-120s to Ready
                        ▼
                  ┌──────────────────────┐
                  │ kubelet registers,   │
                  │ scheduler binds pods │
                  └─────┬────────────────┘
                        │ pod starts serving
                        ▼
                       loop closes — load drops per-pod, HPA stabilizes
```

### Time horizons

| Layer | Horizon |
|-------|---------|
| Application picks up new event | ms |
| Metrics scrape interval | 15-60 s |
| HPA reconcile cycle | 15 s |
| HPA stabilizationWindow (typical scaleUp) | 0 s |
| Pod scheduling (existing node) | < 1 s |
| Pod start (cached image, simple app) | 1-5 s |
| Pod readiness | 5-30 s (probe-dependent) |
| CA/Karpenter reconcile | 10 s |
| Cloud VM launch | 30-120 s |
| Node kubelet registration | 30-60 s |
| Node Ready | 60-180 s |
| VPA recommendation update | ~1 min |
| VPA-driven pod eviction | 1-10 min |

The dominating cost on scale-up from "all nodes full" is the cloud VM provision time (60-180s). All clever autoscaling tweaks in the world cannot beat the cloud's provisioning latency. Solutions: pre-provision warm pools, run with headroom, or use spot/preemptible with lots of small instances.

### Stability conditions

For the loop to be stable:

1. The metric must be a leading indicator (or at minimum a coincident indicator) of demand.
2. The reaction time must be shorter than the metric's natural change time.
3. The reactions must be appropriately damped (`behavior` block).
4. Pods must reach Ready within a reasonable time (long warm-up = HPA over-scales while waiting).

If any of these fails, the loop oscillates. The fix is always: change the metric, change the behavior config, or fix the application's startup time.

---

## 31. Scale-Up Latency Breakdown

A staff engineer's most-asked autoscaling question: "How long does it take to react to a spike?" The answer depends on whether new nodes are needed.

### Scenario A: existing nodes have headroom

```
   T+0       Load spike begins
   T+0..15s  Application pods process more requests; CPU rises
   T+15s     metrics-server scrape captures elevated CPU
   T+15..30s HPA's next reconcile sees the metric
   T+30s     HPA computes desired = N+k, patches /scale
   T+30s     Deployment controller creates k new pods
   T+30s     Scheduler binds new pods to existing nodes (~milliseconds)
   T+30..45s kubelet pulls image (cache hit → fast)
   T+45..60s container starts, readiness probe passes
   T+60s     New pods begin serving traffic
   T+60..75s metrics begin reflecting reduced per-pod load

   Total: ~60 seconds for "make existing nodes work harder"
```

### Scenario B: nodes are full

```
   T+0       Load spike begins
   T+30s     HPA scales replicas N → N+k (as above)
   T+30s     New pods enter Pending state
   T+30..40s Scheduler tries to bind, fails, marks Unschedulable
   T+40s     CA / Karpenter sees Unschedulable pods
   T+40..60s CA simulation; Karpenter batches and picks instance type
   T+60s     Cloud CreateInstance / increase ASG desired
   T+60..150s VM is provisioned, boots, joins as Ready node
   T+150s    Scheduler can now bind pending pods
   T+150..180s pods pull images on the new node (cold pull!)
   T+180..210s containers start, readiness probes pass
   T+210s    New pods serving traffic

   Total: ~3-5 minutes for "add capacity"
```

### Scenario C: scale-from-zero (KEDA, no warm pods, no warm nodes)

Add 30-60 seconds for KEDA's polling interval on top of Scenario B.

### Mitigations

- **Headroom**: run with extra capacity so Scenario A applies, not B. Cost vs latency tradeoff.
- **Pre-warmed nodes**: Karpenter's `nodes` overprovisioning pattern (use an empty Deployment with low-priority pods that get evicted when real pods arrive).
- **Image pre-pulling**: kubelet's `--registry-pull-progress-deadline` is one tunable; mirror to a regional registry (ECR replication); or run a node-bootstrap script that pre-pulls priority images.
- **Smaller instances**: provisioning a `c6i.xlarge` is faster than `c6i.16xlarge` in some clouds (smaller VMs have higher capacity). Karpenter's NodePool can prefer smaller types.
- **Image size**: a 200MB image pulls in seconds; a 4GB image takes a minute. The cheapest scale-up improvement is image-size reduction.

---

## 32. Cross-Component Interactions and Failure Modes

The autoscalers interact in non-obvious ways. The most common cross-component issues:

### HPA scale-up → CA can't find capacity

HPA wants 50 replicas. Each replica needs 4 vCPU + 8 GiB. CA tries to scale-up node group, but cloud returns `InsufficientInstanceCapacity`. CA marks the scale-up as failed, pods remain Pending. HPA continues to want 50, but the workload is stuck.

**Mitigation**: multiple node groups across instance types and zones. Karpenter handles this automatically by trying alternative types in the same NodePool.

### CA scale-down → cascade eviction

CA decides node N is underutilized. Drains. During drain, pods are evicted. They go Pending. Scheduler can't find space (because CA just took capacity). HPA sees lower load (briefly, during the drain), maybe scales down. Now you have a cascade: drain → pending → CA scales up the *other* groups → eventually settles.

**Mitigation**: PDBs that keep enough replicas Ready; `--max-scale-down-parallelism` to limit blast radius; `scale-down-delay-after-add` to prevent quick reversals.

### Karpenter consolidation during a deploy

Deploy starts: rolling update creates new pods. Some old pods on nodes are terminating, briefly leaving those nodes "underutilized." Karpenter starts consolidating, terminating nodes that are about to receive new pods. Result: thrashing.

**Mitigation**: `consolidateAfter: 30s` (default) — wait 30s after the last pod change. Disruption budgets blocking consolidation during business hours.

### KEDA + HPA `min=0` + CA: cold-start cliff

ScaledObject with `idleReplicaCount: 0`. Workload scales to 0. CA scales down the node that hosted it (now empty). Event arrives. KEDA activates; pod enters Pending; CA provisions a new node. End-to-end activation: 3-5 minutes.

**Mitigation**: keep a small standing pool (`minReplicaCount: 1`), or use a separate "always-on" node group for KEDA workloads, or accept the cold-start.

### VPA evicts during HPA scale-up

VPA decides pods need more memory. Evicts pods one at a time. Simultaneously HPA decides to scale up (load is rising). Now you have: VPA evicting + HPA creating + scheduler binding + maybe CA provisioning. The system handles it, but observability is chaos: you see 30 pod restarts in 5 minutes and ten different conditions.

**Mitigation**: avoid VPA `updateMode: Auto` for HPA-driven workloads. Use `Off` or `Initial`.

### ResourceQuota blocking CA

CA wants to provision more nodes, succeeds at the cloud level — but the namespace's ResourceQuota caps total CPU. New pods fail to schedule because `quota exceeded`, even though the cluster has capacity. CA does nothing because the unschedulable reason isn't "no capacity."

**Mitigation**: raise the quota or carve workloads into namespaces with appropriate quotas.

---

## 33. Cost Optimization in Practice

Autoscaling is half about reliability and half about cost. The cost playbook:

### Spot/preemptible everywhere it's safe

```yaml
# Karpenter: prefer spot, fall back to on-demand
spec:
  template:
    spec:
      requirements:
      - key: karpenter.sh/capacity-type
        operator: In
        values: ["spot", "on-demand"]
  weight: 100             # plus a lower-weight on-demand-only pool
```

Spot is 60-90% cheaper. Karpenter handles spot-interruption notices natively (the AWS spot termination warning) by draining nodes 2 minutes before interruption.

PDBs and replica counts must tolerate spot churn. A 3-replica deployment with `maxUnavailable: 1` and `topologySpreadConstraints` across zones is fine on spot. A singleton stateful pod is not.

### Right-size with VPA (in recommendation mode)

Run VPA with `updateMode: Off` cluster-wide. Read the recommendations weekly. Apply them via code review and rollout. After a quarter, you'll have shrunk most workloads by 20-50% from their original "guessed" requests.

### Karpenter consolidation = perma-savings

Enable `WhenEmptyOrUnderutilized` everywhere. The cluster will continuously self-compact. Watch the `karpenter_nodes_created_total` and `karpenter_nodes_terminated_total` metrics — you should see substantial activity even during stable load.

### Priority classes for mixed-tier workloads

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: high-priority
value: 1000000
globalDefault: false
description: "Production user-facing services"

---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: low-priority-preemptible
value: -10
globalDefault: false
description: "Batch jobs; can be preempted by anything"
```

Low-priority jobs scheduled on capacity that would otherwise sit idle. When a high-priority workload needs that capacity, preemption evicts the batch job and the job is rescheduled (or its Job controller retries). Net effect: 90% bin-packing.

### Pre-provisioning with overprovisioner pods

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cluster-overprovisioner
  namespace: kube-system
spec:
  replicas: 4
  template:
    spec:
      priorityClassName: low-priority-preemptible
      containers:
      - name: pause
        image: registry.k8s.io/pause:3.9
        resources:
          requests:
            cpu: 1
            memory: 1Gi
```

These pause pods sit on otherwise-empty capacity. When real workloads need that capacity, the pause pods are preempted (because their priority is below zero), the real workloads schedule instantly, and the overprovisioner Deployment goes to "want more replicas but cannot schedule" — which triggers CA/Karpenter to provision more capacity *in the background*. Effect: scale-up latency for real workloads drops to ~5 seconds.

### Headroom by design

A simpler version: just run HPA with a low target (e.g., `averageUtilization: 50` instead of `70`). You always have 50% headroom. Cost: ~30% more pods at steady state. Benefit: scale-up doesn't need new nodes for moderate spikes.

---

## 34. Observability: Metrics and Dashboards

The autoscaling stack emits metrics in three buckets: the controllers' own metrics, kube-state-metrics for the objects, and metrics-server / Prometheus for the resource numbers.

### Critical metrics by component

**HPA** (from kube-state-metrics):

```
kube_horizontalpodautoscaler_status_current_replicas{namespace, horizontalpodautoscaler}
kube_horizontalpodautoscaler_status_desired_replicas{namespace, horizontalpodautoscaler}
kube_horizontalpodautoscaler_status_condition{condition, status}
kube_horizontalpodautoscaler_spec_max_replicas
kube_horizontalpodautoscaler_spec_min_replicas
```

From the controller-manager itself:

```
horizontal_pod_autoscaler_controller_metric_computation_total_duration_seconds
```

**Alert idea**: `kube_horizontalpodautoscaler_status_condition{condition="ScalingLimited", status="true"} == 1` for more than 10 minutes. Means an HPA wanted to grow but couldn't (max reached, or behavior policy blocking).

**Alert idea**: `kube_horizontalpodautoscaler_status_condition{condition="ScalingActive", status="false"} == 1` for more than 5 minutes. Means metrics aren't flowing.

**VPA** (from kube-state-metrics):

```
kube_verticalpodautoscaler_status_recommendation_containerrecommendations_target
kube_verticalpodautoscaler_status_recommendation_containerrecommendations_lowerbound
kube_verticalpodautoscaler_status_recommendation_containerrecommendations_upperbound
kube_verticalpodautoscaler_spec_updatepolicy_updatemode
```

**Alert idea**: `target > 2 * spec.containers[*].resources.requests.cpu` for 24 hours. Means workloads are persistently under-provisioned per VPA's view.

**CA** (from the binary itself):

```
cluster_autoscaler_unschedulable_pods_count
cluster_autoscaler_cluster_safe_to_autoscale
cluster_autoscaler_nodes_count{state="ready|unready|notStarted|longUnregistered"}
cluster_autoscaler_scaled_up_nodes_total
cluster_autoscaler_scaled_down_nodes_total
cluster_autoscaler_failed_scale_ups_total{reason}
cluster_autoscaler_function_duration_seconds{function="..."}
```

**Alert idea**: `cluster_autoscaler_unschedulable_pods_count > 0` for 10 minutes. Pods cannot be placed.

**Alert idea**: `rate(cluster_autoscaler_failed_scale_ups_total[5m]) > 0`. Cloud capacity failures.

**Karpenter**:

```
karpenter_nodepool_usage          (per resource, per nodepool)
karpenter_nodepool_limit
karpenter_nodes_allocatable
karpenter_nodes_total_pods
karpenter_nodes_created_total
karpenter_nodes_terminated_total{reason="empty|consolidation|drift|expiration|interruption"}
karpenter_disruption_evaluation_duration_seconds
karpenter_disruption_decisions_total
karpenter_pods_state{state="pending|...", nodepool}
karpenter_cloudprovider_instance_type_offerings_available
karpenter_cloudprovider_errors_total{controller, method}
```

**Alert idea**: `sum by (nodepool) (rate(karpenter_nodes_terminated_total{reason="interruption"}[5m])) > 1`. Spot-interruption rate elevated; capacity unstable.

**Alert idea**: `karpenter_pods_state{state="pending"} > 0` for 5 minutes. Karpenter can't satisfy a pod.

**KEDA**:

```
keda_scaler_active{namespace, scaledObject, scaler}
keda_scaler_metrics_value{namespace, scaledObject, scaler, metric}
keda_scaler_errors_total
keda_scaledobject_paused
keda_internal_scale_loop_latency
```

**Alert idea**: `rate(keda_scaler_errors_total[5m]) > 0`. The scaler can't reach the event source.

### Dashboard sketch

A staff-level cluster autoscaling dashboard has four sections:

1. **Per-namespace HPAs**: current vs desired replicas, ScalingActive status, time since last scale event. Heatmap of HPAs that frequently hit max.
2. **VPA recommendations vs requests**: scatter plot. Workloads in the upper-right are under-provisioned; lower-left are over-provisioned.
3. **Node provisioner activity**: rate of nodes created/terminated, average time-to-Ready, count of unschedulable pods, capacity errors.
4. **Cost view**: spot vs on-demand mix, average node utilization, $ per pod-hour. Trend over weeks.

Most teams ship a Grafana dashboard with mostly the above; the [kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) helm chart ships a workable starting point.

---

## 35. Pitfalls

### HPA pitfalls

**1. HPA on `Resource: cpu` without `resources.requests.cpu` set.** `Utilization` is `usage/request`. With request=0, the calculation is undefined and the HPA goes to `ScalingActive=False`. Always set requests on autoscaled pods.

**2. HPA target too low → oscillation.** If you target 30% CPU, normal variance makes the workload bounce above/below 30% every few seconds. Set targets to 60-80% for CPU.

**3. HPA on memory.** Memory rarely correlates with replica count. JVMs/Go runtimes allocate up to GC thresholds regardless of replicas. Memory-driven HPA tends to over-scale during normal GC cycles and never scale down. If you must, target absolute `AverageValue`, not `Utilization`, and pad the target high.

**4. HPA target on multi-container pods using `Resource` not `ContainerResource`.** The sidecar dilutes the signal. Use `ContainerResource` and name the container that does real work.

**5. Stabilization window too long during scale-up.** A 5-minute scaleUp stabilization means a 5-minute lag responding to load spikes. Default is 0; only raise this for flapping signals.

**6. Aggressive scaleDown stabilization causes OOMs.** Setting `scaleDown.stabilizationWindowSeconds: 0` and a 100% policy means a brief metric dip can drop replicas dramatically, leaving survivors to absorb the next spike. Conservative default: 300-600 s.

**7. Multi-metric: a metric you forgot is the dominant one.** Add metrics one at a time and verify the desired count per metric before combining.

**8. HPA + GitOps fight on `replicas`.** Argo/Flux sees `replicas: 3` in git, HPA sets it to 7, GitOps sees drift and resets to 3, HPA sets it to 7. The fix: configure your GitOps tool to ignore `.spec.replicas` (`ignoreDifferences` in Argo, `force: false` in Flux, or remove `replicas` from the manifest entirely).

**9. Custom metric stale because adapter cache or scrape interval.** Don't expect sub-30-second response from prometheus-adapter.

### VPA pitfalls

**10. HPA + VPA on the same metric.** Oscillation. Cardinal rule of autoscaling (see §16).

**11. VPA `updateMode: Auto` evicting your service during peak.** Updater honors PDB but evicts at any time. Use `Off` (recommendation only) for production until in-place resize GAs.

**12. VPA on a workload with diurnal pattern.** Smooths peak and trough into one number; over-provisions at trough, under-provisions at peak. Use HPA for diurnal workloads, VPA for noisy-but-stationary ones.

**13. VPA target with too few samples.** New deployments have nothing to learn from; recommendations are wide bands. Bootstrap with hand-picked requests and let VPA refine.

### CA pitfalls

**14. CA scaling down a node bound to a PV in another zone.** Pods on that node have zone-pinned PVs; rescheduling fails. CA must skip these nodes; verify your storage class has `volumeBindingMode: WaitForFirstConsumer`.

**15. CA `random` expander in a cost-sensitive cluster.** Random picks between cheap and expensive groups equally. Use `priority` or `price`.

**16. CA `--skip-nodes-with-local-storage=true` blocking everything.** Many DaemonSets and sidecars use emptyDir. With this on, no node ever scales down. Set it to false and use pod annotations to opt specific pods out.

**17. CA can't read node-template tags after creating from launch template.** Real node labels differ from template tags, scale-from-zero loops. Validate ASG tags against actual kubelet output.

**18. CA in HA: split-brain.** Multiple CA instances think they're leader, both try to scale. Use proper leader election (`--leader-elect=true`) and a single Lease object.

### Karpenter pitfalls

**19. Karpenter drift rolling all nodes simultaneously.** Default disruption budget is 10%, which is usually fine, but a too-aggressive budget (e.g., 100%) rolls everything at once. Always set a sane budget.

**20. Karpenter consolidation thrashing during deploys.** Set `consolidateAfter: 30s` minimum; some teams set 60-120 s.

**21. Karpenter NodePool requirements too narrow.** Specifying `instance-cpu = ["4"]` means Karpenter can *only* use 4-vCPU instances. If they're capacity-constrained, scale-up fails. Always allow a range.

**22. Karpenter NodePool requirements too broad.** Allowing `instance-cpu` from 2 to 96 means Karpenter may pick a 96-vCPU instance to serve a single small pod. Set sensible caps with `limits` and instance-size guards.

**23. Karpenter spot-only NodePool with stateful workloads.** Spot interruption can take down a stateful pod mid-write. Use `karpenter.sh/capacity-type: on-demand` for stateful pools.

### KEDA pitfalls

**24. KEDA TriggerAuthentication wrong.** Scaler logs `unauthorized` errors; HPA goes to `ScalingActive=False`; workload stays at 0 even when work arrives. Test auth before going to production.

**25. ScaledObject `minReplicaCount` and `maxReplicaCount` mismatch with underlying HPA bounds.** KEDA overrides them, but if you have a pre-existing HPA on the same target, the conflict is undefined. Don't run both.

**26. Scale-from-zero not instant.** The polling interval and cold-start mean 30-120 s. If your SLA is sub-second activation, scale-from-zero is wrong; run minReplicas ≥ 1.

**27. KEDA scaler talking to a metric source through a network policy that blocks it.** KEDA's metrics apiserver pod can't reach Kafka because of NetworkPolicy. Test the pod-to-source path.

**28. KEDA cooldownPeriod too short.** Workload bounces 0 → 1 → 0 → 1 every few minutes during steady low traffic. Raise cooldown to 5-10 minutes.

### Cross-component pitfalls

**29. ResourceQuota blocking CA.** Cluster has capacity, namespace doesn't. CA does nothing; pods sit Pending with `quota exceeded`. Raise quota.

**30. Priority skew.** All workloads have priority 0; preemption never happens. Or all critical workloads have the same priority — preemption is non-deterministic. Use a small set of priority classes and assign them deliberately.

**31. CA scale-down evicting pods to nodes that immediately get scaled down too.** Sequential scale-down across multiple underutilized nodes can drain the cluster faster than the workload settles. Use `--max-scale-down-parallelism` and PDBs.

**32. Karpenter and CA managing the same nodes.** They'll fight; both will issue terminate calls. Each must manage disjoint sets. Tag nodes accordingly; in EKS, use separate NodePools and ASGs.

**33. HPA + VPA + Karpenter all reacting to the same load spike.** HPA wants more pods, VPA wants bigger pods, Karpenter wants new nodes. They can't coordinate. The result is usually fine, but observability becomes confusing. Trace through the timeline.

**34. Scale-up "succeeds" but pods don't start.** New node joins, but image pull fails (registry down) or the kubelet rejects the pod (taint mismatch). HPA still wants more, CA still adds more, none of them are doing anything useful. Always alert on Pending pods independent of autoscaler activity.

**35. Cold-start dominating perceived latency.** Customers report 30-second response times during scale-up. The autoscalers are working correctly; the problem is the workload's own startup (JVM warm-up, model loading). Pre-warm or use init phases.

---

## 36. TL;DR

Kubernetes autoscaling is three orthogonal axes plus one event-driven layer on top, and they all run as independent controllers patching the API server.

**Axis 1 — replicas.** The HPA reads metrics from `metrics.k8s.io`, `custom.metrics.k8s.io`, or `external.metrics.k8s.io` and patches the `scale` subresource. Formula: `desired = ceil(current * currentMetric / target)`, multi-metric `max()` wins, 15-second cycle, behavior block rate-limits the move. Five metric types: `Resource`, `ContainerResource` (for sidecar-heavy pods), `Pods`, `Object`, `External`.

**Axis 2 — pod size.** The VPA's three components (Recommender, Updater, AdmissionController) compute a histogram of usage over 8 days, emit `lowerBound` (p50) / `target` (p90) / `upperBound` (p95), and either advise (`Off`), apply on creation (`Initial`), or evict-and-recreate (`Auto/Recreate`). Pre-1.27, the only resize path is eviction; 1.27+ has in-place resize but it's not yet wired into VPA broadly. **Never run HPA-on-CPU and VPA-on-CPU together** — they oscillate.

**Axis 3 — nodes.** The Cluster Autoscaler watches unschedulable pods, simulates scheduling against each node group's template node, picks a group via an expander (`priority` + `least-waste` is the production stack), and calls the cloud API. Scale-down is conservative: a node must be underutilized for 10 minutes, with all pods reschedulable, before it's drained. **Karpenter** throws away node groups, provisions per-pod, mixes instance types and lifecycles in one NodePool, does active consolidation and drift, and is the default new-cluster choice on AWS and increasingly elsewhere.

**KEDA** extends the HPA's metric source to any event. It creates an HPA under the hood, serves the metric via its own `external.metrics.k8s.io` adapter, and adds scale-from-zero with activation thresholds. 60+ scalers cover Kafka, SQS, Pub/Sub, ServiceBus, Prometheus, cron, and dozens of databases and queues. `ScaledJob` extends the pattern to one-Job-per-event workloads.

The four work together as a closed-loop system spanning 15 seconds (HPA cycle) to 5 minutes (cold node + cold image). The dominant scale-up latency is cloud VM provisioning (60-180s). Mitigate with headroom, overprovisioner pods, smaller instances, and image-size reduction.

**Cost optimization** is mostly: spot capacity (Karpenter handles interruptions), Karpenter `WhenEmptyOrUnderutilized` consolidation, VPA in recommendation-only mode for right-sizing, priority classes for preemptible batch workloads, and disciplined targets (60-80% utilization, not 30%).

**Observability** lives across `kube_horizontalpodautoscaler_*`, `kube_verticalpodautoscaler_*`, `cluster_autoscaler_*`, `karpenter_*`, and `keda_*`. Alert on `ScalingActive=False`, `ScalingLimited=True` for more than 10 minutes, persistent unschedulable pods, and spot-interruption rates.

The pitfalls cluster around a few themes: forgetting requests, scaling on the wrong metric (memory!), HPA+VPA fighting on the same axis, GitOps fighting the HPA's replica writes, expander defaults that ignore cost, NodePool requirements that are too narrow or too broad, KEDA auth bugs, and the eternal "ResourceQuota blocked the scale-up but CA can't tell."

If you remember one thing: each autoscaler patches one field, none of them coordinate, and the only safe mental model is to draw the closed loop on a whiteboard before adding more autoscalers to a workload. Start simple — HPA on a per-pod work metric (QPS, queue depth via KEDA), VPA in recommendation mode, Karpenter with priority+spot — and add complexity only when measured behavior demands it.
