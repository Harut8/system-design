# Workload Controllers

A Pod, by itself, is a fragile thing. Schedule one and the node dies — the Pod dies with it. Create five hundred of them by hand and you have a manual labor problem, not a system. The interesting question Kubernetes answers is not *"how do I run a container?"* — chapters [01](01-container-runtimes-cri-oci.md) and [10](10-kubelet-internals.md) settled that — but *"how do I describe a fleet of containers declaratively and let the system maintain that fleet through node failures, image updates, code rollouts, and time-based triggers?"*. The answer is **workload controllers**: a small family of built-in reconcilers that own the lifecycle of Pods at scale.

This chapter is the long-form reference for the five workload controllers shipped in `kube-controller-manager` that own *non-stateful* Pod fleets:

- **ReplicaSet** — keep N copies of a Pod template running.
- **Deployment** — orchestrate rolling updates across a chain of ReplicaSets.
- **DaemonSet** — one Pod per node, with node-by-node rolling updates.
- **Job** — run a Pod template to a fixed number of successful completions.
- **CronJob** — run a Job on a cron schedule.

StatefulSet — the sixth workload controller, owning ordered, identified, persistent Pods — gets its own deep dive in [chapter 13](13-statefulset-deep-dive.md) because its semantics differ enough (stable identity, ordered lifecycle, PVC template lifecycle) that mixing it in here would dilute both stories.

This chapter sits between [ch 08 (the controller pattern and client-go)](08-controller-pattern-and-client-go.md), which describes the *general* informer/workqueue/reconcile machinery every controller uses, and [ch 11 (Pod internals)](11-pod-internals.md), which describes what the things these controllers *produce* actually are. We assume both. We also forward-reference [ch 22 (autoscaling)](22-autoscaling.md), which stacks HPA/VPA on top of these objects, and [ch 36 (garbage collection)](36-garbage-collection-and-object-lifecycle.md), which is how the ownership graph this chapter builds gets torn down.

If you only remember one sentence: **a workload controller is a level-triggered reconciler whose desired state lives in `spec`, whose observed state lives in `status`, whose `metadata.ownerReferences` form a directed-acyclic ownership graph terminating in Pods, and whose entire job is to make the cardinality and template of those Pods match a Pod template through legal transitions.**

---

## Table of Contents

1. [The Workload-Controller Pattern](#1-the-workload-controller-pattern)
2. [Ownership, OwnerReferences, and the Adoption Rule](#2-ownership-ownerreferences-and-the-adoption-rule)
3. [ReplicaSet: The Cardinality Reconciler](#3-replicaset-the-cardinality-reconciler)
4. [Slow-Start Burst Creation](#4-slow-start-burst-creation)
5. [Deployment: The Rolling-Update Orchestrator](#5-deployment-the-rolling-update-orchestrator)
6. [The Pod-Template-Hash](#6-the-pod-template-hash)
7. [Rolling Update Math: maxSurge and maxUnavailable](#7-rolling-update-math-maxsurge-and-maxunavailable)
8. [Recreate Strategy](#8-recreate-strategy)
9. [Revision History and Rollback](#9-revision-history-and-rollback)
10. [Paused Deployments](#10-paused-deployments)
11. [Progress Tracking and progressDeadlineSeconds](#11-progress-tracking-and-progressdeadlineseconds)
12. [Deployment Status Fields](#12-deployment-status-fields)
13. [Common Rolling-Update Misconfigurations](#13-common-rolling-update-misconfigurations)
14. [DaemonSet: One Pod Per Node](#14-daemonset-one-pod-per-node)
15. [DaemonSet Update Strategies](#15-daemonset-update-strategies)
16. [DaemonSet Edge Cases](#16-daemonset-edge-cases)
17. [Job: Run to Completion](#17-job-run-to-completion)
18. [Job Completion Modes: NonIndexed vs Indexed](#18-job-completion-modes-nonindexed-vs-indexed)
19. [Job podFailurePolicy](#19-job-podfailurepolicy)
20. [Job Suspend and Queueing](#20-job-suspend-and-queueing)
21. [Job Tracking via Finalizers](#21-job-tracking-via-finalizers)
22. [CronJob: Time-Driven Jobs](#22-cronjob-time-driven-jobs)
23. [The CronJob Scheduling Algorithm](#23-the-cronjob-scheduling-algorithm)
24. [TTL-after-finished and History Pruning](#24-ttl-after-finished-and-history-pruning)
25. [observedGeneration and Status Trust](#25-observedgeneration-and-status-trust)
26. [Adoption, Orphan, and Cascade Policies](#26-adoption-orphan-and-cascade-policies)
27. [ReplicationController: A Brief Tombstone](#27-replicationcontroller-a-brief-tombstone)
28. [HPA / VPA Interaction (Forward Ref)](#28-hpa--vpa-interaction-forward-ref)
29. [Events Worth Watching](#29-events-worth-watching)
30. [Observability Metrics](#30-observability-metrics)
31. [Source-Tree Map](#31-source-tree-map)
32. [Pitfalls](#32-pitfalls)
33. [TL;DR](#33-tldr)

---

## 1. The Workload-Controller Pattern

Before any specific controller, internalize the shape they all share. A workload controller is a specialization of the generic reconciler described in [ch 08](08-controller-pattern-and-client-go.md):

```
┌──────────────────────────────────────────────────────────────────────┐
│  Workload Controller Shape                                            │
│                                                                       │
│   watch  ─►  enqueue("ns/name")                                       │
│                  │                                                    │
│                  ▼                                                    │
│   workqueue.Get()                                                     │
│                  │                                                    │
│                  ▼                                                    │
│   reconcile(key):                                                     │
│       obj    := cache.Get(key)         ← Deployment / RS / DS / Job   │
│       owned  := listChildrenInCache(obj)  ← RS / Pods / Jobs          │
│       desired:= computeDesired(obj)        ← N replicas of template   │
│       diff   := desired - owned                                       │
│       apply(diff) via apiserver                                       │
│       writeStatus(obj)                                                │
│       return                                                          │
│                                                                       │
│   Two unbreakable invariants:                                         │
│     (1) Reconcile is level-triggered and idempotent.                  │
│     (2) Every owned child carries an ownerReference back to obj.      │
└──────────────────────────────────────────────────────────────────────┘
```

The "children" differ by controller:

| Controller | Watches | Creates | Deletes | Ownership chain |
|---|---|---|---|---|
| ReplicaSet | RS, Pod | Pod | Pod | RS → Pod |
| Deployment | Deployment, RS | RS | RS (after retention) | Deployment → RS → Pod |
| DaemonSet | DS, Node, Pod | Pod | Pod | DS → Pod |
| Job | Job, Pod | Pod | Pod (after TTL) | Job → Pod |
| CronJob | CronJob, Job | Job | Job (after history limit) | CronJob → Job → Pod |

Every chain terminates in a Pod. The garbage collector ([ch 36](36-garbage-collection-and-object-lifecycle.md)) walks this chain to cascade deletes; the controllers themselves walk it to reconcile cardinality.

The other shared property is **separation of `spec` and `status`**. `spec` is the user's input — what they want. `status` is the controller's output — what is. A controller must never mutate its own object's `spec`; mutating only `status` (and creating/deleting children) keeps the spec/status contract clean and keeps GitOps tools ([ch 31](31-gitops-helm-kustomize.md)) from fighting the controller forever.

### 1.1 Two state variables you cannot ignore

Every workload object has these two `metadata` fields, and the controllers care about both:

- **`metadata.generation`** — incremented by the apiserver every time `spec` changes. The controller can detect "I haven't reacted to the latest spec yet" by comparing this against:
- **`status.observedGeneration`** — written by the controller when it has *finished* a reconcile cycle for a given generation.

Until `status.observedGeneration == metadata.generation`, no client should trust the rest of `status`. We will see this pattern recur in every section. CD tools that wait for `kubectl rollout status` are essentially polling this equality plus a few other condition checks.

### 1.2 The level-triggered guarantee

Edge-triggered controllers (react only to events) lose information on watch reconnect, on controller restart, on a missed event during a backoff. Workload controllers are designed so that *any* reconcile call from *any* trigger (informer event, periodic resync, manual requeue) yields the same correct outcome from the same input. The reconcile function reads the world from the cache, computes desired state, and issues the minimum patches needed. If it crashes mid-way and another reconcile fires, the next pass picks up exactly where it would have if the crash hadn't happened — because it re-reads the world.

This is the property that lets you redeploy `kube-controller-manager` during a rolling update without losing your rollout. Whatever it had been doing, the new instance reads etcd, sees the partial state, and continues.

---

## 2. Ownership, OwnerReferences, and the Adoption Rule

Every Kubernetes object can carry a `metadata.ownerReferences[]` list. Each entry points at another object and (optionally) declares `controller: true` and `blockOwnerDeletion: true`. This is the substrate the workload controllers build on.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: web-7c4f9d-abc12
  namespace: default
  ownerReferences:
  - apiVersion: apps/v1
    kind: ReplicaSet
    name: web-7c4f9d
    uid: 6c5e0b...
    controller: true              # only ONE controller=true allowed per object
    blockOwnerDeletion: true       # parent foreground-delete waits for me
  labels:
    app: web
    pod-template-hash: 7c4f9d
```

The rules:

1. **Exactly one** ownerReference may have `controller: true`. That one is "the" controlling owner. Multiple non-controller owners are legal but rare.
2. `blockOwnerDeletion: true` means *the parent's foreground deletion will not finalize until this dependent is gone*. The garbage collector enforces this.
3. The garbage collector ([ch 36](36-garbage-collection-and-object-lifecycle.md)) walks the ownership graph in reverse: when a parent is deleted, all dependents marked with `blockOwnerDeletion` (or all dependents under cascade=Background) are queued for deletion.

### 2.1 The full chain for a Deployment

```
┌───────────────────────────────────────────────────────────────────┐
│  Deployment "web"                                                  │
│  uid=A   generation=4   spec.replicas=3                            │
└────────────────────┬──────────────────────────────────────────────┘
                     │ owns (controller=true)
                     ▼
       ┌─────────────┴───────────────┐
       │                              │
┌──────┴──────────┐         ┌────────┴────────┐
│ RS "web-7c4f9d" │         │ RS "web-3a1b2c" │  (old revision, replicas=0)
│ uid=B           │         │ uid=C           │
│ spec.replicas=3 │         │ spec.replicas=0 │
└──────┬──────────┘         └─────────────────┘
       │ owns (controller=true)
       ▼
┌──────┴──────────┐  ┌──────────────┐  ┌──────────────┐
│ Pod web-7c4f9d- │  │ Pod web-7c.. │  │ Pod web-7c.. │
│      abc12      │  │     def34    │  │     ghi56    │
│ uid=D           │  │ uid=E        │  │ uid=F        │
└─────────────────┘  └──────────────┘  └──────────────┘
```

Three properties of this graph that recur throughout the chapter:

- **The Deployment owns ReplicaSets, not Pods directly.** The Pods are grandchildren. The Deployment controller never creates Pods; the ReplicaSet controller does.
- **Old revisions stay in the graph** (with `spec.replicas=0`) so a rollback can scale them back up without losing template history. They count against `spec.revisionHistoryLimit`.
- **Deleting the Deployment with `--cascade=background`** (default) deletes the RSes via GC; deleting the RSes cascades to Pods. Deleting with `--cascade=orphan` strips ownerReferences and leaves everything alive.

### 2.2 The adoption rule

A controller does not *only* create children. It also *adopts* existing matching children that have no controller-owner, and it *orphans* mis-owned children whose label/selector relationship has been broken. The rules, as implemented in `pkg/controller/controller_ref_manager.go`:

```
For each candidate child object C in the same namespace:

  if C.selector matches THIS controller's spec.selector
       AND C has no controller=true ownerReference
       AND THIS controller is not being deleted:
       → claim it: PATCH C to add ownerRef pointing at me

  if C has a controller=true ownerReference pointing at ME
       AND C.selector no longer matches THIS controller's spec.selector:
       → release it: PATCH C to remove the ownerRef
```

This rule explains a class of operational surprises:

- Manually-created Pods labeled the same as a ReplicaSet get adopted by that RS and counted against `spec.replicas`. They will be killed if cardinality exceeds the target.
- Editing a Pod's labels to no longer match the parent RS removes it from the RS's roster. The RS then creates a new Pod to replace it. The "orphaned" Pod is still alive, but is now ownerless and must be cleaned up manually.
- `kubectl delete deployment X --cascade=orphan` strips the controller-owner from all child RSes. The RSes continue running, owning their Pods. This is the standard "preserve Pods, replace the controller" maneuver.

### 2.3 Why we don't trust labels alone

Before ownerReferences existed (pre-1.2), controllers identified their children purely by label selector. This caused two problems:

1. Two ReplicaSets with overlapping selectors fought over the same Pods.
2. A Pod's owner was guessable but not authoritative — there was no "who actually created me" record.

OwnerReferences fixed both. Today, controllers list candidates by selector but *act* only on candidates whose ownerRef confirms they belong to *this* controller. The selector is used to discover orphans (adoption) and to compute "should I still own this?" (release).

---

## 3. ReplicaSet: The Cardinality Reconciler

The ReplicaSet is the simplest workload controller, and Deployment is mostly an orchestrator on top of multiple ReplicaSets. So we start here.

### 3.1 The ReplicaSet spec

```yaml
apiVersion: apps/v1
kind: ReplicaSet
metadata:
  name: web-7c4f9d
  namespace: default
  labels:
    app: web
    pod-template-hash: 7c4f9d
  ownerReferences:
  - apiVersion: apps/v1
    kind: Deployment
    name: web
    uid: ...
    controller: true
    blockOwnerDeletion: true
spec:
  replicas: 3
  selector:
    matchLabels:
      app: web
      pod-template-hash: 7c4f9d
  minReadySeconds: 10
  template:
    metadata:
      labels:
        app: web
        pod-template-hash: 7c4f9d
    spec:
      containers:
      - name: nginx
        image: nginx:1.27
        ports:
        - containerPort: 80
status:
  replicas: 3
  readyReplicas: 3
  availableReplicas: 3
  observedGeneration: 1
```

Three things to notice immediately:

1. **`spec.selector` is required and immutable after creation.** Trying to change it produces an admission error. The reason: a mutation of the selector would orphan the existing Pods (no longer matching) and create new ones — a guaranteed accidental outage. If you need a new selector, create a new ReplicaSet.
2. **`spec.template.metadata.labels` must be a superset of `spec.selector`.** The admission webhook enforces this. Otherwise the freshly-created Pod wouldn't match the parent's selector, and the controller would create infinite Pods.
3. **The `pod-template-hash` label** in both the selector and the template is what disambiguates this RS from sibling RSes managed by the same Deployment. We dedicate [§6](#6-the-pod-template-hash) to it.

### 3.2 The ReplicaSet controller's reconcile

Source: `pkg/controller/replicaset/replica_set.go`, function `syncReplicaSet`.

Pseudocode that captures the real shape (heavily compressed; the actual implementation has been refined for ~9 years):

```go
func (rsc *ReplicaSetController) syncReplicaSet(key string) error {
    namespace, name := splitKey(key)
    rs := rsc.rsLister.Get(namespace, name)
    if rs == nil { return nil }              // RS was deleted; nothing to do

    if rs.DeletionTimestamp != nil {
        // RS is being deleted; let GC handle the Pods.
        return rsc.updateRSStatus(rs, nil)
    }

    // Step 1: list candidate Pods in this namespace
    selector := metav1.LabelSelectorAsSelector(rs.Spec.Selector)
    allPods := rsc.podLister.Pods(namespace).List(selector)

    // Step 2: claim/release via ControllerRefManager
    filteredPods, err := rsc.claimPods(rs, selector, allPods)
    if err != nil { return err }
    // filteredPods is now exactly the Pods I own.

    activePods := filterActivePods(filteredPods)   // ignore Failed/Succeeded
    diff := len(activePods) - int(*rs.Spec.Replicas)

    var manageErr error
    if diff < 0 {
        // Too few; create -diff pods using slow-start (§4).
        manageErr = rsc.slowStartBatch(-diff, rs)
    } else if diff > 0 {
        // Too many; delete diff pods, preferring "less-Ready" first.
        manageErr = rsc.deletePods(diff, activePods, rs)
    }

    // Step 3: rewrite status
    return rsc.updateRSStatus(rs, filteredPods)
}
```

A few subtle choices in this algorithm:

- **It always operates on the cache**, not the apiserver. Listing pods through the indexer is O(matched_pods) and never touches etcd.
- **It uses `filterActivePods`** to ignore Pods in terminal states (Succeeded, Failed without restartPolicy=Always). Those Pods will be GC'd by other controllers; counting them would cause the RS to under-create.
- **Deletion picks "worst" Pods first.** The selection order is: not-Ready before Ready, not-Available before Available (younger Ready before older Ready), Pods on unscheduled nodes first, then by `controller.kubernetes.io/pod-deletion-cost` annotation, then by creation timestamp. The full ordering lives in `pkg/controller/controller_utils.go::ActivePodsWithRanks`.
- **One reconcile is one diff.** If diff is large (say 50), the RS does not create all 50 in one pass — see slow-start below.

### 3.3 Worked example: cardinality reconciliation

Start state: RS `web-7c4f9d`, `spec.replicas=3`, 3 Pods running.

```
T+0      User: kubectl scale rs web-7c4f9d --replicas=5
T+5ms    apiserver: PATCH RS; spec.replicas=5; generation=2
T+10ms   watch event → ReplicaSet controller workqueue
T+12ms   syncReplicaSet("default/web-7c4f9d"):
            cache RS has spec.replicas=5
            cache Pods matching selector: 3
            diff = 3 - 5 = -2  → create 2 pods
            slowStartBatch(2, rs)
                CREATE Pod web-7c4f9d-<random1>  (ownerRef=RS)
                CREATE Pod web-7c4f9d-<random2>  (ownerRef=RS)
            updateRSStatus:
                status.replicas = 5  (claimed pods + just-created)
                status.observedGeneration = 2
                status.readyReplicas = 3  (the new ones aren't Ready yet)
T+20ms   apiserver: create the 2 Pods (admission, validation, etcd put)
T+25ms   scheduler binds them → kubelets start them
T+30s    Pods become Ready → kubelet PATCHes Pod.status
T+30s    watch event re-enters RS controller (a Pod owned by me changed)
T+30s    syncReplicaSet: diff = 0, status.readyReplicas = 5
```

The reconcile fired twice: once for the spec change, once for the readiness change. Both yielded the correct level-triggered outcome.

### 3.4 Status semantics

The four status counters every workload controller exposes are worth memorizing:

- **`replicas`** — Pods I currently own (claimed via ownerRef) and that are not terminal.
- **`readyReplicas`** — those that have `condition Ready == true`.
- **`availableReplicas`** — those that have been Ready continuously for at least `minReadySeconds` (often 0).
- **`fullyLabeledReplicas`** — Pods whose labels exactly match `spec.template.metadata.labels`. Used to detect orphan/adopted Pods that mismatch the template (rare but useful for debugging).

`availableReplicas` is the one that matters for rolling updates and PodDisruptionBudget budgets. A Pod is Ready the moment its readiness probe passes, but it is *not* Available until it has stayed Ready for `minReadySeconds`. This buffer absorbs the flaky-startup case where a Pod becomes Ready, accepts traffic, and immediately crashes.

---

## 4. Slow-Start Burst Creation

When the diff is large (a new Deployment scales from 0 to 100, or a node loss takes out 30 Pods at once), the controller does not issue 100 parallel CREATE calls. Instead, it uses an exponential **slow-start burst**:

```
batch 1:  create 1   → wait for any failure signal
batch 2:  create 2   → wait
batch 3:  create 4   → wait
batch 4:  create 8   → wait
batch 5:  create 16  → wait
...
```

Source: `pkg/controller/controller_utils.go::SlowStartBatch`. Pseudocode:

```go
func SlowStartBatch(count int, initial int, fn func() error) (int, error) {
    remaining := count
    successes := 0
    for batchSize := initial; remaining > 0; batchSize *= 2 {
        if batchSize > remaining {
            batchSize = remaining
        }
        errs := parallel(batchSize, fn)
        successes += batchSize - len(errs)
        if len(errs) > 0 {
            return successes, errs[0]
        }
        remaining -= batchSize
    }
    return successes, nil
}
```

The rationale: a Pod creation might be rejected by admission (resource quota exhausted, PSA violation, webhook denial, bad image reference). If the controller blasts 100 CREATE calls in parallel and 100 of them fail with the same admission error, that's 100 apiserver requests and 100 audit log entries for one root cause. The slow-start probes the system: if the first creation succeeds, the second batch of two is likely fine; if it fails, the controller stops, requeues with backoff, and tries again later.

For the operator, the visible effect is: a `kubectl scale deployment X --replicas=100` from 0 reaches all 100 in roughly `log2(100) * (one create cycle)` — about seven rounds, typically 1–3 seconds end to end on a healthy apiserver.

### 4.1 The thundering-herd concern

Slow-start also dampens the apiserver burst. At cluster sizes of ~5k nodes, a HPA event that increases replicas by 50 across 20 Deployments simultaneously could otherwise produce 1000 parallel CREATE requests. With slow-start, the effective parallelism is bounded by `O(log N)` per controller, and the API Priority and Fairness layer ([ch 05](05-kube-apiserver-internals.md)) shapes the rest.

### 4.2 Symmetry: deletion is *not* slow-started

Deletion of excess Pods is parallel — there is no concept of a "delete admission failure" worth probing. The controller deletes all excess Pods in one batch. The rationale: under-creation is a degraded state worth being cautious about; over-creation is benign because the kubelet will simply terminate the extras.

---

## 5. Deployment: The Rolling-Update Orchestrator

If ReplicaSet is "keep N copies", Deployment is "transition from RS-old's template to RS-new's template without ever falling below `replicas - maxUnavailable` Ready Pods or exceeding `replicas + maxSurge` total Pods."

### 5.1 The Deployment spec

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: default
spec:
  replicas: 5
  revisionHistoryLimit: 10
  progressDeadlineSeconds: 600
  minReadySeconds: 10
  paused: false
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 25%          # ceil(5 * 0.25) = 2 extra pods allowed
      maxUnavailable: 25%    # floor(5 * 0.25) = 1 pod may be unavailable
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
      - name: nginx
        image: nginx:1.27
        readinessProbe:
          httpGet: { path: /healthz, port: 80 }
          periodSeconds: 5
        resources:
          requests: { cpu: 100m, memory: 128Mi }
status:
  replicas: 5
  updatedReplicas: 5
  readyReplicas: 5
  availableReplicas: 5
  observedGeneration: 7
  conditions:
  - type: Available
    status: "True"
  - type: Progressing
    status: "True"
    reason: NewReplicaSetAvailable
```

Key fields:

- **`spec.selector`** — immutable, just like RS. Selects what the Deployment "owns" by label.
- **`spec.template`** — the PodTemplateSpec. Any change here triggers a new revision.
- **`spec.strategy`** — `RollingUpdate` (default) or `Recreate`.
- **`spec.revisionHistoryLimit`** — how many *previous* RSes (with replicas=0) to retain. Default 10.
- **`spec.progressDeadlineSeconds`** — how long without forward progress before declaring the rollout failed. Default 600 (10 min).
- **`spec.paused`** — when true, no reconciliation runs; the controller skips entirely.

### 5.2 The hierarchy in motion

A Deployment owns multiple ReplicaSets, but at any moment one is the "current" (whose template matches `spec.template`) and zero or more are "old" (kept for revision history). The Deployment controller's job is to *scale* RSes — it never creates Pods directly.

```
Steady state:
  Deployment(spec.replicas=5)
       │
       ├── RS-NEW (replicas=5, hash=7c4f9d) — owns 5 Pods
       └── RS-OLD (replicas=0, hash=3a1b2c) — owns 0 Pods, kept for rollback

During rollout from old → new (template changed):
  Deployment(spec.replicas=5, maxSurge=2, maxUnavailable=1)
       │
       ├── RS-NEW (replicas=4, hash=b2e8a1) — owning 3 Ready Pods, 1 starting
       └── RS-OLD (replicas=3, hash=7c4f9d) — owning 3 Pods (about to scale down)
                                              total = 7 ≤ 5+2
                                              ready = 6 ≥ 5-1
```

### 5.3 The reconcile, top-level

Source: `pkg/controller/deployment/deployment_controller.go`, function `syncDeployment`. Pseudocode:

```go
func (dc *DeploymentController) syncDeployment(key string) error {
    d := dc.dLister.Get(key)
    if d == nil { return nil }

    if d.Spec.Paused {
        return dc.syncStatusOnly(d)   // observe, do not act
    }

    rsList, err := dc.getReplicaSetsForDeployment(d)
    if err != nil { return err }
    podMap, err := dc.getPodMapForDeployment(d, rsList)
    if err != nil { return err }

    if d.DeletionTimestamp != nil {
        return dc.syncStatusOnly(d)
    }

    // checkPausedConditions etc.
    if getRollbackTo(d) != nil {
        return dc.rollback(d, rsList)
    }

    scalingEvent, err := dc.isScalingEvent(d, rsList)
    if err != nil { return err }
    if scalingEvent {
        return dc.sync(d, rsList)  // pure scale, no template change
    }

    switch d.Spec.Strategy.Type {
    case appsv1.RecreateDeploymentStrategyType:
        return dc.rolloutRecreate(d, rsList, podMap)
    case appsv1.RollingUpdateDeploymentStrategyType:
        return dc.rolloutRolling(d, rsList)
    }
}
```

Notice the three independent paths:

1. **Scaling event** — `spec.replicas` changed but template didn't. No new RS; just scale the existing one. The controller distributes the new replica count across all active RSes proportionally to their current size (the *proportional scaling* algorithm — important during in-flight rollouts; see §5.5).
2. **Rollback** — deprecated `spec.rollbackTo` field (kept for backward compatibility). Today you use `kubectl rollout undo` which patches the template directly.
3. **Rollout** — template changed. Recreate or RollingUpdate.

### 5.4 The rolling-update inner loop

`rolloutRolling` is where the surge/maxUnavailable math lives. Compressed:

```go
func (dc *DeploymentController) rolloutRolling(d *Deployment, rsList []*ReplicaSet) error {
    // getAllReplicaSetsAndSyncRevision: ensures a NEW RS exists whose
    // template matches d.spec.template (creating it if necessary, bumping
    // its revision annotation). Also computes oldRSes (the rest).
    newRS, oldRSes, err := dc.getAllReplicaSetsAndSyncRevision(d, rsList, true)
    if err != nil { return err }
    allRSes := append(oldRSes, newRS)

    // Scale UP the new RS as far as the surge cap allows.
    if scaledUp, err := dc.reconcileNewReplicaSet(allRSes, newRS, d); err != nil {
        return err
    } else if scaledUp {
        return dc.syncRolloutStatus(allRSes, newRS, d)
    }

    // Scale DOWN old RSes as far as the maxUnavailable cap allows.
    if scaledDown, err := dc.reconcileOldReplicaSets(allRSes, oldRSes, newRS, d); err != nil {
        return err
    } else if scaledDown {
        return dc.syncRolloutStatus(allRSes, newRS, d)
    }

    if deploymentutil.DeploymentComplete(d, &d.Status) {
        if err := dc.cleanupDeployment(oldRSes, d); err != nil {
            return err
        }
    }
    return dc.syncRolloutStatus(allRSes, newRS, d)
}
```

The two key inner functions, `reconcileNewReplicaSet` and `reconcileOldReplicaSets`, both call the same arithmetic:

```
total = sum(rs.spec.replicas for rs in allRSes)
totalAvailable = sum(rs.status.availableReplicas for rs in allRSes)

maxTotal = d.spec.replicas + maxSurge(d)
maxScaleUp = maxTotal - total          # new RS may grow this much

minAvailable = d.spec.replicas - maxUnavailable(d)
maxScaleDown = totalAvailable - minAvailable   # old RSes may shrink this much
```

`maxSurge` is computed from a percentage if needed: `ceil(replicas * pct)`. `maxUnavailable` is `floor(replicas * pct)`. At least one of the two must be non-zero (admission rejects `maxSurge=0 && maxUnavailable=0`).

### 5.5 Proportional scaling during a rollout

A subtle case: a Deployment is mid-rollout (5 new, 3 old, target=5), and the user scales to `replicas=10`. The controller must allocate the additional 5 replicas across both RSes proportionally to their current size, otherwise the rollout's invariants (surge/unavailable budgets) get violated.

Source: `pkg/controller/deployment/util/deployment_util.go::GetProportion`. The formula:

```
proportion(rs) = round(rs.spec.replicas * deploymentReplicasToAdd / deploymentReplicasBeforeScale)
```

Worked example: target was 8 (5 new + 3 old, surge 2 → total cap 10). User scales to 10 (delta +2). The algorithm gives:
- new RS: round(5 * 2 / 8) = 1 → spec.replicas = 6
- old RS: round(3 * 2 / 8) = 1 → spec.replicas = 4

Then the next reconcile, the new RS sees a target of 6 and the old of 4, and the rolling-update loop continues as normal — but the rollout's surge/unavailable cap is recomputed against the new `spec.replicas=10`.

---

## 6. The Pod-Template-Hash

This is the single most important implementation detail in the Deployment design. Without it, the entire RS-chain model would not work.

### 6.1 The problem

A Deployment has `selector: {app: web}`. The template also has `labels: {app: web}`. The controller creates RS-OLD with `selector: {app: web}` and the user does a rollout. The controller creates RS-NEW with `selector: {app: web}`. *Both RSes now claim every Pod labeled `app: web`*. Catastrophe.

The fix: a synthetic label, `pod-template-hash`, derived from the PodTemplateSpec, is injected into:

- the new RS's `spec.selector.matchLabels`
- the new RS's `spec.template.metadata.labels`

so each RS owns *only* the Pods generated from its own template.

```
RS-OLD:
  selector: {app: web, pod-template-hash: 7c4f9d}
  template.labels: {app: web, pod-template-hash: 7c4f9d}

RS-NEW:
  selector: {app: web, pod-template-hash: b2e8a1}
  template.labels: {app: web, pod-template-hash: b2e8a1}

Deployment:
  selector: {app: web}               ← matches BOTH
  template.labels: {app: web}        ← user wrote this; no hash
```

The Deployment's selector still matches both (so the Deployment "owns" both, conceptually). But each RS's selector is *narrower*, partitioning Pods exactly.

### 6.2 How the hash is computed

Source: `pkg/controller/deployment/util/deployment_util.go::ComputeHash`.

```go
func ComputeHash(template *v1.PodTemplateSpec, collisionCount *int32) string {
    podTemplateSpecHasher := fnv.New32a()
    hashutil.DeepHashObject(podTemplateSpecHasher, *template)
    if collisionCount != nil {
        collisionCountBytes := make([]byte, 8)
        binary.LittleEndian.PutUint64(collisionCountBytes, uint64(*collisionCount))
        podTemplateSpecHasher.Write(collisionCountBytes)
    }
    return rand.SafeEncodeString(fmt.Sprint(podTemplateSpecHasher.Sum32()))
}
```

FNV-32a over the deep-hash of the PodTemplateSpec (excluding the `pod-template-hash` label itself, which is injected post-hash). The 32-bit space has ~4 billion values, which is plenty for a single Deployment's revision history — but the controller also tracks a `status.collisionCount` field that's incremented on the (extremely rare) case where two different templates produce the same FNV-32a result. The collision count then participates in the hash, deterministically picking a different bucket.

### 6.3 Hash propagation timeline

```
T+0   User edits Deployment.spec.template.spec.containers[0].image: nginx:1.27 → nginx:1.28
T+0   apiserver stores; generation bumps to 5
T+5ms Deployment controller reconciles:
        - listExistingRSes → find one with matching template hash? No.
        - Compute hash(new template) = "b2e8a1"
        - Create RS "web-b2e8a1":
            spec.selector.matchLabels: {app: web, pod-template-hash: b2e8a1}
            spec.template.metadata.labels: {app: web, pod-template-hash: b2e8a1}
            spec.replicas: 0   ← start scaled to 0
            annotation deployment.kubernetes.io/revision: "5"
T+10ms apiserver creates RS.
T+15ms ReplicaSet controller sees new RS; replicas=0 → no Pods to make.
T+15ms Deployment controller's NEXT reconcile begins the rolling update:
        - new RS spec.replicas: 0 → scale up toward surge cap
        - old RS spec.replicas: 5 → scale down once new is available
```

Notice: the *RS creation* and *RS scaling* are separate reconcile passes. The first pass creates a 0-replica RS; subsequent passes scale it. This separation is what keeps reconciles idempotent — each pass does one move toward the goal.

### 6.4 Why the hash is on Pods, not just on RSes

The Deployment-level selector (`{app: web}`) deliberately omits the hash. This means:

- **`kubectl get pods -l app=web`** returns Pods across all revisions of the Deployment (useful for human debugging).
- **Services** (`Service.spec.selector: {app: web}`) point at *all* Pods of the Deployment, across the new and old RSes during a rollout. This is why Service traffic blends old and new pods during a rolling update — by design.
- The Deployment controller's "is this RS mine?" check uses the broader selector to enumerate candidate RSes, then disambiguates by ownerRef.

If the hash were *only* on RSes and not on Pods, the Service-level traffic blending would still work, but the RS controller wouldn't be able to disambiguate "my Pods" from "the other RS's Pods." So the hash lives at both levels: on Pods (so RS selectors can be narrow) and on RSes (so the Deployment can find them by revision).

### 6.5 kubectl rollout restart

`kubectl rollout restart deployment/web` is a frequently-misunderstood command. It does **not** change any field the user wrote. It injects an annotation:

```yaml
spec:
  template:
    metadata:
      annotations:
        kubectl.kubernetes.io/restartedAt: "2026-05-23T10:15:00Z"
```

Because the annotation is part of the PodTemplateSpec, the hash changes. A new RS is created with the new hash; the rolling update proceeds; Pods are replaced. Crucially, *the image, args, env, everything else is identical*. This is how operators "roll a Deployment" to pick up a config change that doesn't show in the manifest (e.g., a Secret was updated but the Pods don't watch it).

---

## 7. Rolling Update Math: maxSurge and maxUnavailable

Two constraints govern the rolling-update budget:

```
At all times:
  (1)  sum(rs.spec.replicas for rs in allRSes)    ≤ d.spec.replicas + maxSurge
  (2)  sum(rs.status.availableReplicas for rs in allRSes) ≥ d.spec.replicas - maxUnavailable
```

The controller drives the rollout in this loop:

```
while not done:
    if can scale UP newRS without exceeding (1):
        increment newRS.spec.replicas by min(remaining_surge, oldRS_count, ...)
        wait for newRS pods to become Available (minReadySeconds gates this)
    elif can scale DOWN oldRS without violating (2):
        decrement oldRS.spec.replicas (this terminates Pods, decreasing total)
    else:
        wait (no progress this cycle)
```

### 7.1 Worked example: 5 replicas, 25% surge, 25% unavailable

```
maxSurge       = ceil(5 * 0.25) = 2
maxUnavailable = floor(5 * 0.25) = 1
totalCap       = 5 + 2 = 7
availableMin   = 5 - 1 = 4

T+0   Old RS replicas=5 (all available). New RS replicas=0.
      total=5, available=5.
      Can scale up newRS by min(7-5, 5) = 2.

T+1   Old RS replicas=5, new RS replicas=2. Pods starting.
      total=7 (at cap), available=5.
      Cannot scale up further. Wait for new Pods to be Available.

T+2   2 new pods become Available. New RS available=2.
      total=7, available=7.
      Can scale down oldRS by min(7-4, 5) = 3.
      But that would drop available to 4 — exactly the floor.
      The actual rule is to scale down (oldReady - (available-min)).
      Algorithm: scale down by available - availableMin = 7 - 4 = 3.

T+3   Old RS replicas=2 (3 Pods terminating). Pods finish terminating.
      total=4, available=4.
      Need to bring total back up; scale up newRS by 7-4=3.

T+4   New RS replicas=5. Old RS replicas=2.
      total=7, available=4 (3 new pods starting).

T+5   3 new pods become Available. total=7, available=7.
      Scale down oldRS by 7-4=3 → old replicas = -1 → clamp to 0.

T+6   Old RS replicas=0. New RS replicas=5.
      total=5, available=5. Rollout complete.
```

Five Pod creates and five Pod deletes, never exceeding 7 total, never falling below 4 available.

### 7.2 Different budget shapes

| maxSurge | maxUnavailable | Behavior |
|---|---|---|
| 25% (default) | 25% (default) | Balanced. Some surge, some downtime. |
| 100% | 0 | Double-up: spin up all new before retiring any old. No availability dip, but 2x resource for the duration. |
| 0 | 25% | Frugal: never exceed `replicas`, but tolerate dips. Slower (must terminate before creating). |
| 0 | 0 | **Rejected by admission.** No progress possible. |
| 100% | 100% | Recreate-ish: spawn all new immediately, kill all old immediately. Effectively recreate-with-double-resource. |

The "100% / 0" pattern is popular for stateless web apps with critical SLO and plenty of resource headroom. The "0 / 25%" pattern is popular for batch infrastructure where the cluster is at capacity and surge would push you over quota.

### 7.3 The minReadySeconds buffer

`spec.minReadySeconds` is the dwell time between "Pod becomes Ready" and "Pod counts as Available." During this dwell, the Pod can be terminated by a crash, an OOM, or a misbehaving readiness probe — and it still counts against the surge budget but does *not* yet count against the available floor. The rolling update will refuse to scale down old Pods until the new ones have aged past `minReadySeconds`.

Operationally, set `minReadySeconds` to a value comfortably greater than your readiness probe's "true positive" window. If your app advertises Ready after a 5-second startup but routinely crashes 10 seconds in, `minReadySeconds: 20` will catch that pattern during a rollout and refuse to progress — much better than rolling all the way and discovering production is on fire.

### 7.4 The %-rounding rule

When `maxSurge` or `maxUnavailable` is a percentage:

- `maxSurge` rounds **up**: `ceil(replicas * pct)`.
- `maxUnavailable` rounds **down**: `floor(replicas * pct)`.

This biases toward extra Pods rather than less availability. With `replicas=10, maxSurge=15%, maxUnavailable=15%`, you get `maxSurge=2, maxUnavailable=1`. With `replicas=1`, you get `maxSurge=1, maxUnavailable=0` — meaning a single-replica Deployment rolls by double-up, which is the only sane choice (a single Pod cannot be `replicas - maxUnavailable = 0` and still serve traffic).

---

## 8. Recreate Strategy

The other strategy is `Recreate`, which is conceptually simpler:

```yaml
spec:
  strategy:
    type: Recreate
```

The algorithm: delete *all* Pods from the old RS, wait until they are gone, then scale the new RS up. Source: `pkg/controller/deployment/recreate.go::rolloutRecreate`.

```
while oldRS.status.replicas > 0:
    oldRS.spec.replicas = 0
    wait for oldRS.status.replicas to drop to 0
newRS.spec.replicas = d.spec.replicas
```

There is no surge, no parallel old/new, no rolling. The rollout is downtime by design.

When to use it:
- **Apps that cannot run two versions concurrently.** Schema migration tools that hold a global lock. Singletons that bind a fixed port across all replicas. Apps with in-process leader election that breaks on overlapping membership.
- **Test fixtures** where the deployment doesn't need to stay up during rollout.
- **Resource-constrained clusters** where surging would OOM the cluster.

When *not* to use it:
- Anything user-facing. Even a one-second outage is enough to spike error rates and confuse healthchecks downstream.

The Recreate strategy honors `progressDeadlineSeconds` the same way: if the old Pods don't terminate within the deadline (preStop hook hung, gracePeriod too long), the rollout is marked failed.

---

## 9. Revision History and Rollback

Each rollout creates a new ReplicaSet and bumps a revision counter stored as an annotation on each RS:

```yaml
metadata:
  annotations:
    deployment.kubernetes.io/revision: "7"
    deployment.kubernetes.io/revision-history: "3,5"    # prior revisions if RS was re-used
    kubernetes.io/change-cause: "Update nginx to 1.28"
```

### 9.1 The revision counter

It is **monotonic per Deployment**, stored on each RS. The Deployment itself does *not* store the current revision number — it computes it from the maximum revision across owned RSes. Why per-RS? Because RSes outlive specific Deployment generations (a rollback rebinds an old RS as "current"), and the RS-level annotation is the durable record.

### 9.2 revisionHistoryLimit

After a successful rollout, the controller calls `cleanupDeployment`:

```
keep the current RS (replicas > 0) plus the most recent revisionHistoryLimit
RSes (with replicas = 0), delete the rest.
```

With the default `revisionHistoryLimit: 10`, you can `kubectl rollout undo --to-revision=N` to any of the last 10 revisions. Each retained RS is "scale-zero" — it owns no Pods, so it costs essentially nothing (a few KB in etcd plus a watch entry).

Setting `revisionHistoryLimit: 0` deletes the previous RS as soon as the rollout completes — no rollback possible. Useful in environments with extremely many Deployments where etcd object count is a concern (think 50k Deployments × 10 RS each = 500k orphan RSes).

### 9.3 kubectl rollout undo

```
kubectl rollout undo deployment/web                    # roll back one step
kubectl rollout undo deployment/web --to-revision=5    # roll to specific revision
kubectl rollout history deployment/web                 # list revisions
```

What `kubectl rollout undo` actually does:

```
1. List all RSes owned by the Deployment.
2. Find the target RS (most recent prior, or by --to-revision).
3. PATCH the Deployment: spec.template = targetRS.spec.template
4. (Optionally) annotate with kubernetes.io/change-cause: "Rollback to revision N"
```

It is *not* a special API; it is just a spec patch. The Deployment controller then reconciles: it computes `hash(newTemplate)`, finds the existing target RS with that hash, *re-uses* it (scaling it back up instead of creating a duplicate), and starts a rolling update from the current RS to the target. The "current" RS — the one we just rolled away from — becomes the new "old" and may be scaled to zero (and eventually pruned per `revisionHistoryLimit`).

This is the entire reason for keeping the pod-template-hash and the per-RS revision annotation: rollback is just "use the old RS that already has the right hash."

### 9.4 The kubernetes.io/change-cause annotation

If you set `--record` on `kubectl apply` (legacy) or annotate manually:

```
kubectl annotate deployment/web kubernetes.io/change-cause="bump to nginx 1.28"
```

The annotation propagates to the resulting RS. `kubectl rollout history` shows it as the "CHANGE-CAUSE" column. For production use, CD tools (ArgoCD, Flux) typically set this to the commit SHA or PR number, giving an audit trail without leaving the cluster.

---

## 10. Paused Deployments

```yaml
spec:
  paused: true
```

When `paused: true`, the Deployment controller short-circuits at the top of `syncDeployment` and only updates status. No reconcile happens; the current RS roster is frozen as-is. This includes mid-rollout — pausing mid-rollout leaves you with a partially-migrated set.

The intended workflow:

```
1. kubectl rollout pause deployment/web
2. kubectl set image deployment/web nginx=nginx:1.28
3. kubectl set env deployment/web FOO=bar
4. kubectl set resources deployment/web --limits=cpu=200m
   (multiple staged spec edits — no rollout yet)
5. kubectl rollout resume deployment/web
   → controller wakes up, sees the cumulative template change,
     creates one new RS with the combined diff, rolls.
```

Versus the non-paused version, which would create three intermediate RSes (one per `kubectl set`) — three rollouts cascading into each other. Paused mode reduces this to one clean rollout.

### 10.1 Pause + apply

A common GitOps pattern: in a multi-resource apply (Deployment + ConfigMap + Secret), pause the Deployment first, apply the whole set, then unpause. The pause-aware controller treats the cumulative edit as a single rollout. ArgoCD's "sync waves" can express this, as can Helm hooks.

### 10.2 Pause as a hard gate

Setting `paused: true` is also a tool for incident response. If a Deployment is rolling badly (new Pods crashing), `kubectl rollout pause` freezes the rollout in place. You then have time to investigate, possibly hand-tune the offending RS, or `kubectl rollout undo`. Without pause, every reconcile keeps trying to progress.

---

## 11. Progress Tracking and progressDeadlineSeconds

A rollout can stall for many reasons: image pull failures, scheduling failures (no node has capacity), readiness probes never passing, admission rejections. Kubernetes distinguishes between "the deployment is taking a long time" and "the deployment is failing forever" using `progressDeadlineSeconds`.

### 11.1 The Progressing condition

The Deployment carries a `Progressing` condition. The controller updates it on every reconcile:

```yaml
status:
  conditions:
  - type: Progressing
    status: "True"
    reason: NewReplicaSetCreated         # or ReplicaSetUpdated, NewReplicaSetAvailable
    lastUpdateTime: "..."
    lastTransitionTime: "..."
```

The `lastUpdateTime` is bumped each time the rollout makes *any* progress (new RS created, RS scaled up, RS scaled down, RS observed as Available). Source: `pkg/controller/deployment/progress.go::syncRolloutStatus`.

### 11.2 The deadline check

On every reconcile, if `lastUpdateTime` is older than `progressDeadlineSeconds` and the rollout is not yet complete:

```yaml
status:
  conditions:
  - type: Progressing
    status: "False"
    reason: ProgressDeadlineExceeded
```

This is the "the deployment failed" semantic. Importantly, it does **not** roll back, does **not** stop reconciling — the controller keeps trying. It just flips a condition that watchers (CD tools, alerting) can act on.

`kubectl rollout status deployment/web` watches this condition:

```
$ kubectl rollout status deployment/web
Waiting for deployment "web" rollout to finish: 3 of 5 updated replicas are available...
error: deployment "web" exceeded its progress deadline
$ echo $?
1
```

CI systems use this as the exit code: success or failure.

### 11.3 The difference from "pods crashing"

A Pod entering CrashLoopBackOff does *not* by itself fail the deployment. The deployment is "in progress" as long as Pods are being created and the controller is trying. The deadline mechanism is what eventually says "we have been trying for 10 minutes and made no headway, fail."

This is intentional separation: the Pod-level signal (CrashLoopBackOff) is the *cause*; the Deployment-level signal (ProgressDeadlineExceeded) is the *verdict*. Different layers, different observability.

### 11.4 Choosing the deadline

The default (600s = 10 min) is wrong for two opposing reasons:

- **Too short** if your image pull is slow (multi-GB image from a far registry) or your startup is heavy (a JVM warmup + JIT pass). False-positive deadline expiry.
- **Too long** if you have a CI pipeline that gates on rollout status — 10 minutes of waiting before declaring a rollout dead is painful.

Production guidance:
- For lightweight services: 120–300s.
- For heavy services (large images, slow startup): 600–1200s.
- For batch infra: rarely use this — set high (3600s) since you don't gate on it.

---

## 12. Deployment Status Fields

The full status:

```yaml
status:
  replicas: 5              # Pods currently owned (across all RSes)
  updatedReplicas: 5       # Pods owned by the NEW RS (matching spec.template)
  readyReplicas: 5         # Pods with condition Ready=True
  availableReplicas: 5     # Pods Ready for at least minReadySeconds
  unavailableReplicas: 0   # replicas - availableReplicas (clamped >= 0)
  observedGeneration: 7    # the spec.generation we last reconciled
  collisionCount: 0        # see §6.2
  conditions:
  - type: Available
    status: "True"
    reason: MinimumReplicasAvailable
  - type: Progressing
    status: "True"
    reason: NewReplicaSetAvailable
```

The condition semantics:

- **Available** — `True` iff `availableReplicas >= spec.replicas - maxUnavailable`. Reason `MinimumReplicasAvailable`. Flips to `False` (`MinimumReplicasUnavailable`) when too few Pods are available.
- **Progressing** — see §11. `True` while making forward progress; `True` (`NewReplicaSetAvailable`) when rollout completes; `False` (`ProgressDeadlineExceeded`) on failure.

Both must be `True` for the rollout to be considered healthy.

### 12.1 The "rollout complete" predicate

```go
func DeploymentComplete(d *Deployment, newStatus *DeploymentStatus) bool {
    return newStatus.UpdatedReplicas == *d.Spec.Replicas &&
           newStatus.Replicas == *d.Spec.Replicas &&
           newStatus.AvailableReplicas == *d.Spec.Replicas &&
           newStatus.ObservedGeneration >= d.Generation
}
```

All four must hold:

1. Every Pod is from the new RS (`updatedReplicas == spec.replicas`).
2. No extra Pods exist from old RSes (`replicas == spec.replicas`).
3. Every Pod is Available (`availableReplicas == spec.replicas`).
4. The controller has observed the latest generation (`observedGeneration >= generation`).

If any of these is false, `kubectl rollout status` waits.

### 12.2 The observedGeneration trap

If a Deployment is updated by a webhook (e.g., a mutating policy adds a sidecar), the apiserver bumps `generation`. The Deployment controller reconciles and writes `observedGeneration = newGeneration`. But if the controller is overloaded and lagging, you may have `observedGeneration < generation` and `status.replicas == spec.replicas`. The latter would *look* healthy, but the former tells you the status is stale. **Always check observedGeneration before trusting the rest of status.** This is true for every workload controller.

---

## 13. Common Rolling-Update Misconfigurations

Things that look like they should work, but break in subtle ways.

### 13.1 `maxSurge=0 && maxUnavailable=0`

Admission rejects this:

```
The Deployment "web" is invalid: spec.strategy.rollingUpdate.maxUnavailable:
Invalid value: intstr.IntOrString{Type:0, IntVal:0, StrVal:""}: may not be 0
when maxSurge is 0
```

Both being zero is a "no legal move" state: cannot add a new Pod (no surge), cannot terminate an old one (no slack). Admission catches it before the API server stores the bad spec.

### 13.2 `maxUnavailable: replicas`

A Deployment with `replicas: 5, maxUnavailable: 5` permits the controller to take down all five Pods at once before bringing up any new ones. This is effectively a Recreate strategy disguised as RollingUpdate, with one extra footgun: there's no admission warning. You get a full outage during every rollout.

### 13.3 Short `progressDeadlineSeconds` with slow startup

A Deployment with `progressDeadlineSeconds: 60` and a 90-second readiness probe latency (JVM warmup, dependency check) will flip `Progressing=False` on every rollout — a permanent false alarm. Either lengthen the deadline or speed up readiness (split startup probe vs readiness probe — see [ch 11 §12](11-pod-internals.md)).

### 13.4 `minReadySeconds: 0` with a slow-converging readiness probe

If your readiness probe ticks every 10 seconds and your app has a 5-second window where it accepts connections but isn't actually serving (e.g., warm cache loading), `minReadySeconds: 0` means the controller will treat a Pod as Available *immediately* upon first Ready=True. The next reconcile then scales down the old RS. Now you have a stub Pod serving traffic while your real backend isn't loaded.

Fix: set `minReadySeconds` to at least one or two readiness-probe periods (e.g., 30 seconds for a 10-second-period probe).

### 13.5 Selectors that match too much

A Deployment with `selector: {tier: backend}` is matching by a label that *many* other things in the namespace also use. The Deployment will adopt orphan Pods that happen to have that label. Worse, two such Deployments overlap and fight.

Discipline: every Deployment should have a unique selector (typically including `app: <unique-name>`). The Deployment Linter or admission policies should enforce this.

### 13.6 Changing the selector

Selectors are immutable on Deployment, RS, and (most) workload controllers. Attempting to change the selector:

```
The Deployment "web" is invalid: spec.selector: Invalid value:
field is immutable
```

This is a hard error. If you need to change selectors, you must delete (with `--cascade=orphan` to preserve Pods) and recreate. There's a small body of operational tribal knowledge around this maneuver.

### 13.7 `kubectl rollout restart` myths

A surprising number of operators believe `kubectl rollout restart` reschedules existing Pods. It does not. It changes the PodTemplateSpec (by adding a `kubectl.kubernetes.io/restartedAt` annotation), which changes the hash, which creates a new RS, which triggers a rolling update that *replaces* the existing Pods. The existing Pods themselves are never "restarted" — they are deleted and replaced. The distinction matters for stateful workloads (which keep their volumes across recreate but not across rolling-update) and for debugging cache state.

---

## 14. DaemonSet: One Pod Per Node

A DaemonSet ensures one Pod from a template runs on every node that matches the (optional) selector and tolerates the node's taints. Canonical uses:

- Node-level agents: log shipper (Fluent Bit), metrics agent (node-exporter), CNI daemon (Calico, Cilium), CSI node plugin, security agent (Falco), kube-proxy itself.
- Workloads that must run on every node for correctness, not just for scale.

### 14.1 The DaemonSet spec

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: fluent-bit
  namespace: kube-system
spec:
  revisionHistoryLimit: 10
  selector:
    matchLabels:
      name: fluent-bit
  template:
    metadata:
      labels:
        name: fluent-bit
    spec:
      priorityClassName: system-node-critical
      tolerations:
      - operator: Exists                  # tolerate ANY taint
      hostNetwork: true
      containers:
      - name: fluent-bit
        image: fluent/fluent-bit:3.0
        resources:
          requests: { cpu: 50m, memory: 100Mi }
        volumeMounts:
        - name: varlog
          mountPath: /var/log
      volumes:
      - name: varlog
        hostPath: { path: /var/log }
  updateStrategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 1
      maxSurge: 0
```

Key differences from Deployment:

- **No `spec.replicas`.** Replica count is implicit: one per matching node.
- **`spec.template.spec.tolerations`** is critical: by default DS Pods do *not* tolerate any taints. To run on a tainted node (control-plane, GPU pool, ...), the template must list explicit tolerations.
- **`spec.template.spec.priorityClassName: system-node-critical`** for system DSes — prevents preemption and gets the highest scheduling priority.
- **`spec.updateStrategy`** is the analog of Deployment's strategy: `RollingUpdate` or `OnDelete`.

### 14.2 What "matching node" means

The DS controller considers a node "matching" if all of these hold:

- The node's labels match `spec.template.spec.nodeSelector` (or `nodeAffinity` rules).
- The DS Pod's tolerations cover every taint on the node that has `effect: NoSchedule` or `effect: NoExecute`.
- The node passes Pod-level admission (PriorityClass exists, runtimeClassName supported, etc.).

If even one of these fails, no Pod is scheduled on that node and no Pod is counted in `status.desiredNumberScheduled`.

### 14.3 The controller (post-1.12 design)

Pre-1.12, the DaemonSet controller scheduled Pods *directly* by setting `spec.nodeName` to bypass the scheduler. Post-1.12, the controller creates Pods with a `nodeAffinity` matching a specific node, and the scheduler binds them like ordinary Pods. This unifies the scheduling path and lets DS Pods benefit from the scheduler's plugin framework (e.g., pod topology spread, image locality).

Source: `pkg/controller/daemon/daemon_controller.go::syncDaemonSet`. Compressed:

```go
func (dsc *DaemonSetsController) syncDaemonSet(key string) error {
    ds := dsc.dsLister.Get(key)
    if ds == nil { return nil }

    nodeList := dsc.nodeLister.List(labels.Everything())

    // For each node, decide whether a Pod is wanted.
    nodesToCreate, nodesWithPods, err := dsc.podsShouldBeOnNode(ds, nodeList)

    // Compute pods to delete: pods on nodes where they shouldn't be,
    // or older pods on nodes that already have a current pod.
    podsToDelete := dsc.findPodsToDelete(ds, nodesWithPods)

    // Create pods (slow-start)
    err = dsc.syncNodes(ds, podsToDelete, nodesToCreate)

    // Drive update strategy if template changed
    if ds.Spec.UpdateStrategy.Type == apps.RollingUpdateDaemonSetStrategyType {
        err = dsc.rollingUpdate(ds, nodeList)
    }

    return dsc.updateDaemonSetStatus(ds, nodeList, hash, true)
}
```

For each node, `podsShouldBeOnNode` runs the predicate above. The function creates a Pod with:

```yaml
spec:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchFields:
          - key: metadata.name
            operator: In
            values: [the-specific-node-name]
```

That's the trick: a hard nodeAffinity to exactly one node. The scheduler treats this Pod like any other and binds it to that node (assuming the node still passes filter). The DS controller doesn't need to know about scheduling internals.

### 14.4 The DaemonSet status

```yaml
status:
  currentNumberScheduled: 10   # nodes with at least one DS pod of the current revision
  numberMisscheduled: 0        # pods on nodes that no longer match (will be deleted)
  desiredNumberScheduled: 12   # nodes that *should* have a pod (matching predicate)
  numberReady: 10              # pods that are Ready
  numberAvailable: 10          # pods Ready for minReadySeconds
  numberUnavailable: 2         # desired - available
  updatedNumberScheduled: 10   # pods of the current template hash
  observedGeneration: 4
```

The fields are richer than Deployment's because the DS has more states to expose. `desiredNumberScheduled - numberReady` is the operator's first-pass health metric: if this is positive for a long time, some node has a DS pod that can't start (image pull failure, resource shortage on a node, missing toleration after a new taint).

---

## 15. DaemonSet Update Strategies

Two strategies, both more constrained than Deployment's:

### 15.1 RollingUpdate (default)

```yaml
updateStrategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 1
    maxSurge: 0
```

Algorithm: walk nodes; for each node where the DS Pod's hash doesn't match the current template hash, delete it (subject to maxUnavailable budget). The kubelet (or rather, the scheduler) creates a new Pod on the same node when the DS controller creates one with the updated template + nodeAffinity.

`maxUnavailable` here means "how many nodes can simultaneously be without a Ready DS Pod." If `maxUnavailable: 1`, at most one node is being updated at a time; you wait for the new Pod on node N to become Ready before deleting the Pod on node N+1.

`maxSurge` for DaemonSet (added later) allows creating the new Pod on a node *before* deleting the old one — but since both share node-local resources (hostPath, hostNetwork, host ports), this is often infeasible and is typically left at 0.

On a 1000-node cluster with `maxUnavailable: 1` and a 30-second readiness window, a DS rollout takes ~500 minutes. For large clusters, set `maxUnavailable` to a percentage (`"10%"`) to parallelize the rollout.

### 15.2 OnDelete

```yaml
updateStrategy:
  type: OnDelete
```

The controller never updates Pods. To roll, you manually `kubectl delete pod` per node, and the controller creates the replacement with the new template. Useful when:

- The rollout needs human gating per node (canary).
- An external orchestrator (node-replacement automation) is doing the per-node work.
- The application is so sensitive to restarts that even RollingUpdate's automation is unwanted.

### 15.3 Per-node restart with `kubectl rollout restart`

For RollingUpdate DSes, `kubectl rollout restart ds/X` works the same way as Deployment: inject `restartedAt`, hash changes, every Pod gets replaced subject to maxUnavailable.

---

## 16. DaemonSet Edge Cases

### 16.1 Tainted node + missing toleration

A common rollout incident: someone applies `kubectl taint node N gpu-only=true:NoSchedule`. The fluent-bit DS template has no toleration for `gpu-only`. Result: the existing fluent-bit Pod on node N is evicted (`NoExecute` would evict immediately; `NoSchedule` lets it survive but evicts on Pod restart), and no new one is created. `status.desiredNumberScheduled` drops by 1 — but only if the controller computes that node N is no longer a match. With `NoSchedule`, the controller will *not* count node N anymore, so the count looks healthy. The operator must monitor "is fluent-bit on every node we expect it on?" via an external check.

Defensive practice: DS Pods that *must* run everywhere should use blanket `tolerations: [{operator: Exists}]`. This is standard for kube-proxy, CNI, log/metric agents.

### 16.2 Preemption

A DS Pod's PriorityClass matters during scheduling pressure. System DSes use `system-node-critical` (priority 2000001000). On a packed node, when a higher-priority Pod arrives, the scheduler runs preemption. DS Pods with `system-node-critical` priority are usually safe from being preempted; lower-priority Pods on the same node get evicted instead.

If your custom DS uses `priorityClassName: ""` (default 0) and your node fills with high-priority workloads, your DS Pod might be evicted. Then the DS controller schedules a new one, the new one gets preempted again, and you have a livelock. Production DSes should always specify a priority class.

### 16.3 Surviving node drain

`kubectl drain` evicts Pods. Static DS Pods (those with `--ignore-daemonsets`, which is the default in `kubectl drain`) are *not* evicted; they stay on the draining node until the node is removed. This is intentional: you want fluent-bit to keep collecting logs from the dying node until the very end.

But "stays alive" doesn't mean "ignored": the DS controller still owns the Pod. If you `kubectl delete pod X --force` on a draining node, the DS controller creates a new one — possibly back on the same node if the drain hasn't actually removed it yet.

### 16.4 Node-name pinning and stale pods

DS Pods carry a hard nodeAffinity to a specific node name. If a node is deleted and recreated with the same name (an autoscaler replacing a faulty node), the DS controller treats the new node as a fresh match and schedules a new Pod. The old Pod (on the now-gone node) is GC'd by the Pod GC controller (it's been unreachable for `node.kubernetes.io/unreachable` plus eviction timeout).

But there's a subtle race: if the new node comes up *before* the old Pod's node-lost eviction completes, the DS controller sees one Pod on the node (the orphaned, unreachable one) and might decide not to create a new one. The reconciliation eventually self-heals after the orphan is GC'd, but it can be confusing during incidents.

### 16.5 Resource accounting

DS Pods consume node resources just like regular Pods. The kube-scheduler reserves their requests when scheduling other Pods. A DS that requests 500m CPU on every node effectively reduces every node's available CPU by 500m for user workloads. Plan for this — a 100-node cluster with five DSes requesting 500m each loses 250 cores of allocatable.

---

## 17. Job: Run to Completion

A Job runs Pods until a specified number of them complete successfully. Unlike a Deployment, the Pod template's restartPolicy is `OnFailure` or `Never` (never `Always`).

### 17.1 The Job spec

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: process-batch
  namespace: default
spec:
  completions: 100
  parallelism: 10
  backoffLimit: 6
  activeDeadlineSeconds: 3600
  ttlSecondsAfterFinished: 600
  completionMode: NonIndexed     # or Indexed
  podReplacementPolicy: Failed   # or TerminatingOrFailed
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: worker
        image: registry/batch-worker:1.0
        command: [/work, --batch-id, "$(JOB_NAME)"]
```

Fields:

- **`completions`** — total number of successful Pod completions required. Job is Complete when `status.succeeded >= completions`.
- **`parallelism`** — maximum concurrent Pods. The Job runs up to `parallelism` Pods in parallel.
- **`backoffLimit`** — total Pod failures permitted before the Job is marked Failed. Default 6.
- **`activeDeadlineSeconds`** — wall-clock time bound. If the Job runs longer (counted from the first Pod creation), all Pods are deleted and the Job is marked Failed.
- **`ttlSecondsAfterFinished`** — auto-cleanup. After the Job reaches Complete or Failed, delete the Job (and its Pods) after this many seconds.
- **`completionMode`** — NonIndexed (default) or Indexed. See §18.
- **`podReplacementPolicy`** — when a Pod is terminating (not yet terminated), do we replace it? `TerminatingOrFailed` replaces immediately on termination signal; `Failed` waits until terminal.

### 17.2 The reconcile shape

Source: `pkg/controller/job/job_controller.go::syncJob`. Compressed:

```go
func (jm *Controller) syncJob(key string) error {
    job := jm.jobLister.Get(key)
    if job == nil { return nil }

    // List pods owned by this job (via ownerRef + selector).
    pods, err := jm.getPodsForJob(job, true)

    // Categorize: active, succeeded, failed, terminating.
    active   := filterActivePods(pods)
    succeeded := countSucceeded(pods, job.Spec.CompletionMode)
    failed    := countFailedPods(pods, job.Spec.PodFailurePolicy)

    if job.Spec.Suspend != nil && *job.Spec.Suspend {
        return jm.suspendJob(job, pods)   // delete all pods, freeze
    }

    if exceedsBackoffLimit(failed, job) ||
       exceedsActiveDeadline(job) {
        return jm.failJob(job, pods, reason)
    }

    if succeeded >= job.Spec.Completions {
        return jm.completeJob(job, pods)
    }

    // Decide how many new pods to create.
    wanted := computeWantedActive(job, active, succeeded)
    diff   := wanted - len(active)

    if diff > 0 {
        // Slow-start create.
        err = jm.manageJob(job, active, diff, succeeded)
    }

    // Update status (using finalizers to track terminal pods — §21).
    return jm.updateJobStatus(job, ...)
}
```

The differences from RS:

- The Job tracks **terminal Pods** (succeeded/failed) separately. Those Pods aren't replaced (they did their job, or failed) — they are counted into status.
- The Job stops creating new Pods when `succeeded + active >= completions` (for NonIndexed) or when each index has a candidate (for Indexed).
- The Job carries finalizers on its Pods to make sure a Pod that finishes-and-is-deleted-out-from-under-the-controller still gets counted (§21).

### 17.3 backoffLimit and per-pod backoff

When a Pod fails, the controller doesn't replace it immediately. Instead, a delay is applied (the Job-level backoff):

```
attempt 1: delay = 10s
attempt 2: delay = 20s
attempt 3: delay = 40s
attempt 4: delay = 80s
attempt 5: delay = 160s
attempt 6: delay = 320s    capped at 6 minutes
```

The delay applies between the failure of one Pod and the creation of its replacement. After `backoffLimit` total failures (across all Pods, all retries), the Job is failed and remaining Pods are deleted.

For Indexed Jobs, there's an additional knob: `spec.backoffLimitPerIndex`. Each index gets its own counter; the Job continues even if some indexes have failed permanently. See §18.

### 17.4 activeDeadlineSeconds vs progressDeadlineSeconds

A common confusion: Job has `activeDeadlineSeconds`; Deployment has `progressDeadlineSeconds`. Both have units of seconds, both lead to a "failed" verdict, but they mean different things:

- **`activeDeadlineSeconds`** (Job): wall-clock bound on the entire Job's runtime. After this many seconds since the first Pod was created, fail.
- **`progressDeadlineSeconds`** (Deployment): bound on how long the rollout can go without making progress. Resets each time the rollout advances.

They are not interchangeable. A Job has no notion of "progress" — it's running until it's done or out of time.

### 17.5 Pod template constraints

The Job admission rejects:

- `restartPolicy: Always` (only `OnFailure` and `Never` are allowed). Rationale: if the container were restarted by the kubelet, the controller would never see the success or failure — the kubelet would just keep restarting forever.
- Mutations to most fields after creation. `parallelism` and `suspend` and `activeDeadlineSeconds` are mutable; nearly everything else (including the Pod template) is immutable.

---

## 18. Job Completion Modes: NonIndexed vs Indexed

### 18.1 NonIndexed (default)

Each successful Pod counts toward `completions`. There's no notion of "which work item this Pod did" — the application is responsible for atomic claim/release on whatever work queue it consumes.

```
completions: 100, parallelism: 10:

T+0   Create 10 pods (parallelism cap).
T+5s  Pod-A succeeds. succeeded=1, active=9. Create 1 more → active=10.
T+8s  Pod-B succeeds. succeeded=2, ...
...
T+5m  succeeded=100. No more Pods needed. Job Complete.
```

This is suitable for a fanout pattern: each Pod pulls one work item from a shared queue (Redis, SQS, Postgres SKIP LOCKED) and exits on success. The queue's atomicity gives at-least-once semantics; idempotent processing gives effectively-exactly-once.

### 18.2 Indexed

Each Pod is assigned a unique index from 0 to `completions-1`. The Job is complete when *every* index has at least one successful Pod.

```yaml
spec:
  completions: 100
  parallelism: 10
  completionMode: Indexed
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: worker
        image: registry/batch:1.0
        command: ["/work"]
        env:
        - name: JOB_COMPLETION_INDEX
          valueFrom:
            fieldRef:
              fieldPath: metadata.annotations['batch.kubernetes.io/job-completion-index']
```

The controller assigns each new Pod an index that hasn't yet succeeded. The index appears in:

- The Pod's annotation `batch.kubernetes.io/job-completion-index`.
- The Pod's name: `<jobname>-<index>-<random>`.
- The Pod's hostname (for headless-service addressing): `<jobname>-<index>`.

Inside the container, `JOB_COMPLETION_INDEX` is the integer string. The application uses it to deterministically pick which work to do: shard N out of M, partition K of a dataset, range [index*chunksize, (index+1)*chunksize] of a file.

### 18.3 When to use Indexed

- **Deterministic sharding** of a known input. "Process partitions 0–99 of this S3 prefix." Without an external queue.
- **Reproducibility.** Re-running with the same `completions` re-creates the same shards.
- **Per-shard observability.** Each index has a clearly-identified Pod and log stream.
- **Reduction of state** in the application. The work-distribution logic is baked into JOB_COMPLETION_INDEX, no queue needed.

### 18.4 backoffLimitPerIndex

With Indexed jobs, you can tolerate per-index failures:

```yaml
spec:
  completions: 100
  parallelism: 10
  completionMode: Indexed
  backoffLimit: 100
  backoffLimitPerIndex: 3
  maxFailedIndexes: 5
```

Each index can fail up to 3 times. Indexes that exhaust their per-index budget are marked failed but don't fail the Job — the Job continues with the surviving indexes. The Job fails only if `maxFailedIndexes` indexes have been declared dead.

Use case: a batch job over 100 files; 99 succeed, 1 has a permanently bad input. Without per-index backoff, that one bad file would fail the whole Job. With `maxFailedIndexes: 5`, you accept "up to 5 bad inputs" and complete the rest.

### 18.5 Headless Service for Indexed Jobs

Indexed Jobs combine well with a headless Service:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-job
spec:
  clusterIP: None
  selector:
    job-name: process-batch
```

This makes each Pod resolvable as `<jobname>-<index>.<service>.<ns>.svc.cluster.local`, enabling MPI-style intra-job communication. Combined with `subdomain` set on the Pod template, this is the foundation for batch training jobs (`KubeFlow MPIJob`, `Volcano gang jobs`).

---

## 19. Job podFailurePolicy

Default behavior: every Pod failure counts against `backoffLimit`. But some failures shouldn't count — a node went away (DisruptionTarget), an OOM was hit (you want to retry), the application explicitly signaled "this is a permanent error" (you want to fail immediately).

`podFailurePolicy` (GA in 1.31) gives you per-failure control.

### 19.1 The spec

```yaml
spec:
  backoffLimit: 6
  podFailurePolicy:
    rules:
    - action: FailJob          # immediately fail entire Job
      onExitCodes:
        containerName: worker
        operator: In
        values: [42, 43]
    - action: Ignore           # don't count against backoffLimit
      onPodConditions:
      - type: DisruptionTarget
    - action: Count            # count normally (default behavior, made explicit)
      onPodConditions:
      - type: Failed
    - action: FailIndex        # fail just this index (Indexed jobs only)
      onExitCodes:
        operator: In
        values: [50]
```

Rules are evaluated in order; first match wins. Available actions:

- **`FailJob`** — fail the entire Job immediately, no more retries.
- **`Ignore`** — pretend the failure didn't happen; replace the Pod, don't count against backoff.
- **`Count`** — count toward backoff (default).
- **`FailIndex`** — for Indexed Jobs, mark this index as permanently failed (counts toward `maxFailedIndexes`).

### 19.2 Use cases

```yaml
# Treat spot-instance preemption as a free retry
podFailurePolicy:
  rules:
  - action: Ignore
    onPodConditions:
    - type: DisruptionTarget
```

`DisruptionTarget` is set by the kubelet when the Pod is terminating due to node drain, eviction, preemption, or shutdown — i.e., not the application's fault. Retrying is the right move.

```yaml
# Permanent application errors should not retry
podFailurePolicy:
  rules:
  - action: FailJob
    onExitCodes:
      containerName: main
      operator: In
      values: [2]   # by convention, our app uses exit code 2 for "bad input"
```

This requires application coordination: the app must exit with a stable code that the policy can match.

### 19.3 Container vs Pod scope

The `containerName` field (in `onExitCodes`) restricts the rule to a specific container in the Pod. Use this when you have sidecars that may fail with arbitrary codes you don't want to interpret as app errors.

---

## 20. Job Suspend and Queueing

`spec.suspend: true` (GA in 1.24) is a critical feature for batch systems:

```yaml
spec:
  suspend: true
  completions: 10
  parallelism: 5
  template: ...
```

When `suspend` is true:

- If no Pods exist, none are created.
- If Pods exist, they are deleted (graceful termination).
- `status.startTime` is unset until the Job is unsuspended.

When `suspend` flips to `false`, the controller starts creating Pods according to `parallelism`. The Job's "elapsed" clock (against `activeDeadlineSeconds`) starts here, not at Job creation.

### 20.1 Why this exists

Without suspend, a Job that exists in etcd starts running immediately. For batch queueing systems (Kueue, Volcano, YuniKorn) that want to admit Jobs based on cluster capacity, fair-share, or gang-scheduling constraints, this is unworkable. Suspend lets you:

1. Create the Job in suspended state.
2. The queueing layer evaluates whether the Job should start now.
3. When the queue decides yes, the queue patches `spec.suspend: false`.
4. The Job controller starts creating Pods.

This is how Kueue's `Workload` abstraction works under the hood: it creates Jobs in suspended state, queues them in a Kueue-internal data structure, and unsuspends them when capacity is available.

### 20.2 Re-suspension

You can re-suspend a running Job. The controller deletes the running Pods and resets active state, but **counts that have already been observed** (succeeded, failed) are preserved. Resuming the Job continues toward the completion target.

For Indexed Jobs with `backoffLimitPerIndex`, this gives you a way to "pause" a long-running Job to inspect state, then resume — useful for debugging.

---

## 21. Job Tracking via Finalizers

A subtle reliability problem with Jobs: Pods can be deleted out from under the controller (a user runs `kubectl delete pod X --force`, an eviction occurs, etc.). If the controller hasn't yet read the Pod's terminal state into `status.succeeded` or `status.failed`, it could lose count.

The fix: the controller adds a finalizer (`batch.kubernetes.io/job-tracking`) to every Pod it creates. The kubelet will mark a Pod as Succeeded or Failed (status), but the Pod object cannot be deleted from etcd while the finalizer is present. The controller:

1. Creates the Pod with the finalizer set.
2. The kubelet runs the Pod; eventually it terminates; kubelet writes the terminal status.
3. The controller observes the terminal status, increments `succeeded` or `failed`.
4. The controller removes the finalizer from the Pod.
5. Now the GC can delete the Pod object.

### 21.1 The finalizer in YAML

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: process-batch-7-abc12
  finalizers:
  - batch.kubernetes.io/job-tracking
  ownerReferences:
  - apiVersion: batch/v1
    kind: Job
    name: process-batch
    uid: ...
    controller: true
    blockOwnerDeletion: true
```

### 21.2 Why this matters

Without finalizers (pre-1.22), the count was approximate. A `kubectl delete pod --grace-period=0 --force` would remove the Pod object before the controller saw its terminal state; the count would not advance. On a 1000-Pod Job, losing 1% to forceful deletion meant the Job could never complete (succeeded never reached completions). Finalizers fix this: the deletion physically blocks until the controller has accounted for the Pod.

### 21.3 Failure mode: orphaned finalizers

If the Job controller is unhealthy (kube-controller-manager crashed and is restarting), Pods with the finalizer cannot be GC'd. A wedged controller can leave thousands of finalizer-stuck Pod objects. The operational response: get the controller healthy. As a last resort, `kubectl patch pod X -p '{"metadata":{"finalizers":[]}}'` strips the finalizer — but this is the same kind of "force-delete" that the finalizer was designed to prevent. Use sparingly.

---

## 22. CronJob: Time-Driven Jobs

A CronJob is the "scheduler of Jobs." Every reconcile, the controller computes "should I create a Job now?" based on the cron schedule.

### 22.1 The CronJob spec

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: nightly-cleanup
  namespace: default
spec:
  schedule: "0 2 * * *"             # cron format: at 02:00 every day
  timeZone: "America/Los_Angeles"    # IANA name
  concurrencyPolicy: Forbid          # Allow | Forbid | Replace
  startingDeadlineSeconds: 200       # skip if more than 200s late
  suspend: false
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      backoffLimit: 2
      activeDeadlineSeconds: 1800
      ttlSecondsAfterFinished: 86400
      template:
        spec:
          restartPolicy: OnFailure
          containers:
          - name: cleaner
            image: registry/cleaner:1.0
status:
  active:
  - apiVersion: batch/v1
    kind: Job
    name: nightly-cleanup-28381440
    uid: ...
  lastScheduleTime: "2026-05-23T02:00:00-07:00"
  lastSuccessfulTime: "2026-05-22T02:00:11-07:00"
```

Key fields:

- **`spec.schedule`** — standard 5-field cron syntax (minute hour day-of-month month day-of-week), plus extensions: `@hourly`, `@daily`, `@weekly`, `@monthly`, `@yearly`, `@every 1h30m`. Step values (`*/15` for "every 15 minutes") supported. The parser is `github.com/robfig/cron/v3` with the standard parser, vendored under `vendor/github.com/robfig/cron/v3`.
- **`spec.timeZone`** (GA in 1.27) — IANA timezone name (`Europe/Berlin`, `Asia/Tokyo`). Without this, the schedule is interpreted in the kube-controller-manager's local timezone (usually UTC). **Always set this.**
- **`spec.concurrencyPolicy`**:
  - `Allow` (default) — new Jobs can run while old ones are still running.
  - `Forbid` — skip new Jobs if a previous one is still active.
  - `Replace` — delete the active Job and create a new one.
- **`spec.startingDeadlineSeconds`** — if the controller is waking up late (e.g., after a kube-controller-manager restart), only schedule Jobs that are no more than this many seconds late. Default: unset (no deadline).
- **`spec.successfulJobsHistoryLimit`** / **`spec.failedJobsHistoryLimit`** — how many completed Job objects to retain. Defaults: 3 and 1.
- **`spec.suspend`** — freeze scheduling entirely.

### 22.2 The Job naming convention

Each Job created by a CronJob has a name derived from the CronJob name plus a numeric suffix:

```
nightly-cleanup-28381440
```

The suffix `28381440` is `floor(unix_seconds / 60)` at the scheduled time — minutes since epoch. This is deterministic: the same scheduled time always produces the same Job name, which means **the apiserver's uniqueness constraint protects against the controller scheduling the same Job twice**. If two reconciles both try to create the Job for time T, one wins via the etcd CAS, the other gets `AlreadyExists` and treats it as success.

This is a small but elegant idempotency trick. It lets the controller be sloppy about exactly-once without losing correctness.

---

## 23. The CronJob Scheduling Algorithm

Source: `pkg/controller/cronjob/cronjob_controllerv2.go::syncCronJob`. The core idea:

```
On each reconcile of CronJob C:
  now := time.Now() in C.spec.timeZone
  earliestTime := C.status.lastScheduleTime  (or C.creationTimestamp if unset)
  startingDeadlineCutoff := now - C.spec.startingDeadlineSeconds  (if set)
  if startingDeadlineCutoff > earliestTime: earliestTime = startingDeadlineCutoff

  scheduledTimes := []
  for t := next_schedule_after(earliestTime); t <= now; t = next_schedule_after(t):
    scheduledTimes.append(t)
    if len(scheduledTimes) > 100:
      emit Event "TooManyMissedTimes"
      return

  if len(scheduledTimes) == 0:
    requeue at next_schedule_after(now)
    return

  mostRecent := scheduledTimes[-1]   # only schedule the most recent missed run

  if mostRecent + startingDeadlineSeconds < now:
    emit Event "MissedSchedule"
    return  # too late

  # Apply concurrency policy
  if C.status.active has running jobs:
    switch C.spec.concurrencyPolicy:
    case Allow: pass
    case Forbid: return  # skip this run
    case Replace: delete active jobs

  # Create the Job
  jobName := fmt.Sprintf("%s-%d", C.name, mostRecent.Unix()/60)
  CREATE Job jobName  (with ownerRef to CronJob)
  C.status.lastScheduleTime = mostRecent
  requeue at next_schedule_after(now)
```

### 23.1 The "too many missed" guard

The loop computes every missed schedule from `earliestTime` to `now`. For a CronJob that hasn't run in a long time (controller was down, CronJob was just unsuspended, etc.), this could be millions of iterations. The guard at 100 prevents that.

If the guard trips, the CronJob is stuck — it emits a `TooManyMissedTimes` event and refuses to schedule. Operator must update `status.lastScheduleTime` (or delete and recreate the CronJob, or set `startingDeadlineSeconds` to a sane value) to recover.

### 23.2 Why only schedule the most recent

The controller schedules *at most one* Job per reconcile, even if many schedules were missed. The reasoning: if your CronJob is meant to run every minute and we missed 30 minutes, you don't want 30 Jobs simultaneously. You want one. If your business logic genuinely needs catch-up runs, encode that in the Job itself (read state, process backlog).

The alternative (queue all missed runs) was tried in early implementations and caused thundering-herd recovery events. The "only most recent" rule is now standard.

### 23.3 The startingDeadlineSeconds dilemma

If `startingDeadlineSeconds` is **unset** (the default), the "too many missed" guard at 100 fires whenever there are >100 missed runs since `lastScheduleTime`. For a CronJob that runs every minute and a controller outage of 2 hours (120 minutes), this trips. The CronJob is stuck.

If `startingDeadlineSeconds` is **set to a small value** (e.g., 200), only missed runs in the last 200 seconds are counted. The controller never sees 100 missed runs (because they're all outside the window). The CronJob recovers gracefully.

**Practical recommendation:** always set `startingDeadlineSeconds`. A common value is `10 × period` (a CronJob that runs hourly: 36000 seconds = 10 hours). Set high enough to tolerate brief outages, low enough that you don't accidentally fire days of backlog.

### 23.4 Timezone handling

`spec.timeZone` parses the timezone in the controller and uses it for all schedule calculations. Critical correctness:

- DST transitions: `0 2 * * *` (2 AM daily) in a timezone with DST runs twice on the "fall back" day (the 2 AM clock hour happens twice) and zero times on the "spring forward" day (clock jumps 2 → 3). The robfig/cron library handles this; the Kubernetes controller respects whatever the library decides. Verify with a known schedule before relying on it for billing-critical operations.
- Container TZ is **irrelevant** for scheduling. The container sees UTC (or whatever the image is configured for) but the schedule is determined by the controller using `spec.timeZone`. Setting `TZ=...` env in the container has no effect on scheduling, only on the container's log timestamps.

### 23.5 Concurrency policy semantics

```
Allow:   schedule fires, new Job is created even if old ones still running.
         status.active accumulates over time.

Forbid:  if status.active is non-empty, skip this run.
         Note: this is "no concurrent runs", not "no overlapping runs."
         A Job that runs longer than the period blocks subsequent runs.

Replace: if status.active is non-empty, delete those Jobs first
         (which deletes their Pods), then create the new Job.
         Useful for "keep only the most recent run" patterns.
```

A subtle point with `Forbid`: the controller checks `status.active`, which is updated when the Job is created and removed when the Job completes. There's a small window where a Job has completed (Pods done) but the CronJob controller hasn't yet observed it. During that window, a new schedule could fire and (with Forbid) be skipped. The effect: under high concurrency-policy=Forbid load, you might miss a schedule even though no Job was actually running. The trade-off is part of the design.

---

## 24. TTL-after-finished and History Pruning

Two mechanisms keep cluster state bounded.

### 24.1 ttlSecondsAfterFinished on Jobs

```yaml
apiVersion: batch/v1
kind: Job
spec:
  ttlSecondsAfterFinished: 600
```

When the Job reaches Complete or Failed, a separate controller (`pkg/controller/ttlafterfinished/ttlafterfinished_controller.go`) waits `ttlSecondsAfterFinished` and then deletes the Job. The deletion cascades to its Pods.

Without this TTL, completed Jobs (and their Pods, kept around for log inspection) accumulate forever. On a cluster running 1000 cron jobs per day, this is several GB of etcd objects per month.

Common values:
- Production batch jobs: 1–24 hours (long enough to debug, short enough to bound).
- Critical Jobs whose logs you ship externally: 600s (10 min) — fast cleanup.
- Jobs you may want to retry by re-creating: 0 (delete immediately on completion).

### 24.2 CronJob history limits

`successfulJobsHistoryLimit: 3` and `failedJobsHistoryLimit: 1` are different — they tell the CronJob controller how many *previously-finished Job objects* to keep around per outcome class. Excess Jobs are deleted by the CronJob controller directly (not via TTL).

The CronJob retains its history independently of the TTL on the Job template. If the Job template sets `ttlSecondsAfterFinished: 60`, the Jobs are deleted by the TTL controller after 60 seconds and the CronJob's history-keeping is moot. If neither is set, Jobs accumulate. Use one or the other, not both at conflicting rates.

### 24.3 The TTL controller's reconcile

```go
func (tc *Controller) processJob(key string) error {
    job := tc.jobLister.Get(key)
    if job == nil { return nil }
    if !isJobFinished(job) { return nil }
    if job.Spec.TTLSecondsAfterFinished == nil { return nil }

    finishedAt := jobFinishedTime(job)
    expireAt := finishedAt.Add(time.Duration(*job.Spec.TTLSecondsAfterFinished) * time.Second)
    if time.Now().Before(expireAt) {
        tc.queue.AddAfter(key, expireAt.Sub(time.Now()))
        return nil
    }

    return tc.client.BatchV1().Jobs(job.Namespace).Delete(ctx, job.Name, ...
        Propagation: metav1.DeletePropagationBackground,
    )
}
```

The deletion uses `Background` propagation: the Job object is removed immediately, the GC controller cascades to the Pods. This is appropriate because we don't need the Job object to stick around waiting for Pod deletion to complete.

---

## 25. observedGeneration and Status Trust

A pattern that recurs in every workload controller, worth a dedicated section.

### 25.1 The contract

```yaml
metadata:
  generation: 7
status:
  observedGeneration: 7
```

- The apiserver increments `metadata.generation` on every `spec` change. Status changes do not bump it.
- The controller writes `status.observedGeneration = metadata.generation` when it has *completed a reconcile pass* for that generation.

Until `observedGeneration == generation`, the rest of `status` reflects an older spec. Conditions, counters, replica counts — all stale.

### 25.2 What to do as a watcher

A CD pipeline that waits for a rollout to complete must check:

1. `status.observedGeneration >= metadata.generation` — the controller has seen the latest spec.
2. The relevant condition (e.g., `Available=True` and `Progressing=True/NewReplicaSetAvailable`).
3. Replica counters match (`replicas == spec.replicas`, `availableReplicas == spec.replicas`, etc.).

Skipping (1) is a common mistake. If you check only (2) and (3) after a `kubectl apply`, you might catch the *previous* status briefly between the spec write and the controller's reaction. False positive: "deployment is ready" — but the controller hasn't even started reconciling the new spec.

`kubectl rollout status` handles this correctly. Hand-rolled wait scripts often don't.

### 25.3 The lag during high churn

If the kube-controller-manager is overloaded, `observedGeneration` can lag many seconds behind `generation`. The Deployment controller's workqueue tail length is observable: `workqueue_depth{name="deployment"}` in metrics. Sustained high tail length is a sign the controller is behind. The visible symptom is "my Deployment changes don't take effect for a long time."

Remediation: scale the controller (HA control plane), reduce churn from CI, increase the controller's `--concurrent-deployment-syncs` flag (default 5).

---

## 26. Adoption, Orphan, and Cascade Policies

We met adoption in §2.2; here we expand on the cascade side and the operator-facing knobs.

### 26.1 The three deletion propagation policies

```
kubectl delete deployment/web --cascade=background   (default)
kubectl delete deployment/web --cascade=foreground
kubectl delete deployment/web --cascade=orphan
```

- **Background** (default): the API server immediately removes the parent. The GC controller asynchronously deletes children. The user's command returns quickly; cleanup happens later.
- **Foreground**: the API server marks the parent with a `foregroundDeletion` finalizer and a deletionTimestamp. The parent stays visible while children are being deleted. When all children with `blockOwnerDeletion: true` are gone, the finalizer is removed and the parent is finally deleted. `kubectl get` shows the parent during this window.
- **Orphan**: the API server strips the controller-owner reference from all children (so they become orphans) and then deletes the parent. The children continue to live as ownerless objects.

For workload controllers:

- **Background** is appropriate for routine deletes.
- **Foreground** is appropriate when you want to block on cleanup completing (e.g., a CD job that needs the deletion to "finish" before moving on).
- **Orphan** is appropriate when you want to preserve Pods while replacing the parent controller (common during selector-change maneuvers, since selectors are immutable).

### 26.2 The orphan-then-recreate maneuver

```
# I need to change the selector of a Deployment from {app: web} to {app: web, version: v2}
# Selectors are immutable, so I have to recreate the Deployment.
# I do NOT want to drop traffic during the migration.

# 1. Orphan: remove the Deployment without killing Pods.
kubectl delete deployment/web --cascade=orphan

# Now the ReplicaSet web-7c4f9d is an orphan (no owner), still owns its Pods.

# 2. Create the new Deployment with new selector.
kubectl apply -f new-deployment.yaml

# 3. The new Deployment creates a new RS, scales it up.
# 4. Once the new RS is fully scaled and serving, delete the orphan RS.
kubectl delete rs/web-7c4f9d
```

This is the textbook procedure. With server-side apply and a careful `fieldManager`, you can avoid some of the friction. But the core idea — orphan to preserve, then deliberately replace — is the safe pattern.

### 26.3 Adoption gotchas

A Pod manually created in the namespace, labeled `app: web`, in the same namespace as the `web` Deployment:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: rogue
  labels:
    app: web         # matches Deployment's selector
spec:
  containers: [...]
```

What happens:

1. The Deployment selector `{app: web}` matches this Pod.
2. The Deployment's RS selector `{app: web, pod-template-hash: 7c4f9d}` does **not** match (no hash label).
3. So the RS *does not* adopt it.
4. But the Deployment controller does see it (via the Deployment selector), and reports its existence in counters.

The Pod is orphan-relative-to-the-RS but visible to the Deployment. Some Deployment status counters (specifically `replicas` at the Deployment level) include it; others (`updatedReplicas`) don't. This is a niche scenario but explains the "why is my Deployment showing 4 replicas when I asked for 3?" question.

If the orphan Pod *did* have the hash label (perhaps copied from an existing RS Pod), the RS would adopt it. Then the RS's reconciliation would see "I have 4 Pods but want 3" and delete the orphan.

---

## 27. ReplicationController: A Brief Tombstone

Before there was ReplicaSet, there was ReplicationController (RC). It's still in the API group `v1`:

```yaml
apiVersion: v1
kind: ReplicationController
metadata:
  name: web
spec:
  replicas: 3
  selector:
    app: web                # NOTE: not under matchLabels
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
      - name: nginx
        image: nginx
```

Differences from ReplicaSet:

- **Selector is `map[string]string`**, not `LabelSelector`. No `matchExpressions`, no set-based selectors.
- **No `pod-template-hash`** propagation — RCs are not orchestrated by Deployments.
- **Same cardinality semantics**, same adoption rules.

ReplicationController is retained for backward compatibility. Every new workload should use Deployment. There is no reason to choose RC today; it has no features that ReplicaSet (and Deployment-on-top) lack.

The continued existence of RC is a Kubernetes API-stability lesson: once a `v1` resource ships, it ships forever. Even when superseded, it stays in the API. This is why Kubernetes is conservative about promoting to `v1` — every promotion is a permanent commitment.

---

## 28. HPA / VPA Interaction (Forward Ref)

A full treatment of HPA, VPA, cluster-autoscaler, and Karpenter is in [chapter 22](22-autoscaling.md). Here we record the workload-controller-side interactions.

### 28.1 HPA writes spec.replicas

The HorizontalPodAutoscaler writes `spec.replicas` on its target (Deployment, ReplicaSet, or StatefulSet). The Deployment controller treats this as a scaling event (not a template change), so it scales the current RS up or down — no new RS created, no rolling update triggered.

```
HPA observes CPU usage of pods → CPU > target → compute new replicas →
PATCH deployment.spec.replicas: 10 → Deployment reconciles → RS.spec.replicas: 10 →
RS slow-start-creates 3 new pods.
```

Round-trip: ~5–15 seconds, with stabilization windows on the HPA side dampening oscillation.

### 28.2 The CD-vs-HPA fight

A common production failure: a GitOps tool (ArgoCD, Flux) has `spec.replicas: 3` in Git. The HPA writes `spec.replicas: 10`. The next sync cycle, ArgoCD detects drift (Git says 3, cluster says 10) and patches back to 3. The HPA sees CPU still high, patches to 10. The fight cycles indefinitely.

Resolutions:
- **ArgoCD**: `spec.ignoreDifferences` for `/spec/replicas` on the Deployment.
- **Flux**: similar drift-suppression annotation.
- **Server-side apply**: have the HPA's fieldManager own `/spec/replicas`. Then any other patch to that field is rejected unless `--force` is used.

The right answer is the third one. Ownership of fields by fieldManager is exactly what SSA exists for. But most teams discover SSA only after they've had the fight.

### 28.3 HPA + VPA on the same metric

VPA in `Auto` mode mutates `requests`/`limits` on Pods. HPA scales replica count based on, say, CPU usage. If they're both watching CPU:

- VPA increases CPU request → kubelet observes lower utilization (same usage, larger request) → HPA scales *down*.
- HPA scales down → fewer Pods, same load → utilization per Pod rises → VPA increases request more → cycle.

The conflict is fundamental: VPA scales the resource axis, HPA scales the count axis, both using the same observation. Solutions:
- Use VPA in `Off` or `Initial` mode (recommend only, don't mutate).
- Use HPA on a metric VPA doesn't touch (e.g., requests-per-second, queue depth).
- Don't combine them on the same workload.

### 28.4 KEDA-style scaling

KEDA (Kubernetes Event-Driven Autoscaling) is HPA layered on richer metrics (Kafka lag, AWS SQS depth, etc.). It creates a HorizontalPodAutoscaler under the hood. From the workload controller's perspective, KEDA looks identical to HPA. The interactions above apply.

---

## 29. Events Worth Watching

`kubectl describe deployment/web` (or `rs`, `ds`, `job`, `cronjob`) shows the recent events from the controller. The vocabulary:

### 29.1 Deployment events

```
ScalingReplicaSet           Normal   Scaled up replica set web-7c4f9d to 3
ScalingReplicaSet           Normal   Scaled down replica set web-3a1b2c to 0
DeploymentRollback          Normal   Rolled back deployment "web" to revision 5
ProgressDeadlineExceeded    Warning  Deployment "web" exceeded its progress deadline
ReplicaSetUpdated           Normal   Updated ReplicaSet web-7c4f9d
```

### 29.2 ReplicaSet events

```
SuccessfulCreate            Normal   Created pod: web-7c4f9d-abc12
SuccessfulDelete            Normal   Deleted pod: web-7c4f9d-xyz98
FailedCreate                Warning  Error creating: pods "web-7c4f9d-jkl45" is forbidden:
                                     exceeded quota: cpu-quota
```

### 29.3 DaemonSet events

```
SuccessfulCreate            Normal   Created pod: fluent-bit-abc12 on node-7
FailedDaemonPod             Warning  Found failed daemon pod fluent-bit-xyz98 on node-3
SuccessfulDelete            Normal   Deleted pod: fluent-bit-old123 (rolling update)
```

### 29.4 Job events

```
SuccessfulCreate            Normal   Created pod: process-batch-1
SuccessfulDelete            Normal   Deleted pod: process-batch-7 (per podReplacementPolicy)
BackoffLimitExceeded        Warning  Job has reached the specified backoff limit
DeadlineExceeded            Warning  Job was active longer than specified deadline
```

### 29.5 CronJob events

```
SuccessfulCreate            Normal   Created job nightly-cleanup-28381440
SawCompletedJob             Normal   Saw completed job: nightly-cleanup-28381439, status: Complete
MissedSchedule              Warning  Cannot determine if job needs to be started:
                                     too many missed start times (> 100)
JobAlreadyActive            Warning  Not starting job because prior execution is still active
TooManyMissedTimes          Warning  too many missed start times: 200; will not run
```

These events are short-lived (default 1 hour TTL in the apiserver event TTL). For long-term observability, ship them to a log aggregator via an events-collector DaemonSet.

### 29.6 The `Warning` events that actually matter

In order of operational severity:

1. `BackoffLimitExceeded` (Job) — work is being permanently lost.
2. `ProgressDeadlineExceeded` (Deployment) — rollout has wedged.
3. `TooManyMissedTimes` (CronJob) — scheduled work isn't running.
4. `FailedCreate` (any) — admission or quota is blocking creation.

Alert on these. The Normal events are useful for forensics but rarely worth a page.

---

## 30. Observability Metrics

Beyond events, every controller exposes Prometheus metrics. Two sources:

### 30.1 kube-controller-manager metrics

```
workqueue_depth{name="deployment"}                  current queue length
workqueue_adds_total{name="deployment"}             cumulative items added
workqueue_retries_total{name="deployment"}          cumulative retries (rate-limited)
workqueue_work_duration_seconds_bucket{name="..."}  per-item reconcile latency

deployment_controller_busy_workers                  number of workers in reconcile
replicaset_controller_busy_workers                  ditto
job_controller_busy_workers                         ditto
```

For health, `workqueue_depth` should hover near zero in steady state. Sustained depth >100 means the controller is behind on its work. The `workqueue_work_duration_seconds` histogram should have p99 well under a second; longer p99 indicates slow apiserver responses or expensive list operations.

### 30.2 kube-state-metrics

`kube-state-metrics` exposes object-level metrics derived from the apiserver:

```
kube_deployment_status_replicas{deployment="web", namespace="default"}                    5
kube_deployment_status_replicas_available{deployment="web", namespace="default"}           5
kube_deployment_status_replicas_unavailable{deployment="web", namespace="default"}         0
kube_deployment_status_replicas_updated{deployment="web", namespace="default"}             5
kube_deployment_status_observed_generation{deployment="web", namespace="default"}          7
kube_deployment_metadata_generation{deployment="web", namespace="default"}                 7

kube_replicaset_status_replicas{replicaset="web-7c4f9d", namespace="default"}              5
kube_replicaset_owner{replicaset="web-7c4f9d", owner_kind="Deployment", owner_name="web"}  1

kube_daemonset_status_current_number_scheduled{daemonset="fluent-bit", ...}                10
kube_daemonset_status_desired_number_scheduled{daemonset="fluent-bit", ...}                10
kube_daemonset_status_number_unavailable{daemonset="fluent-bit", ...}                       0

kube_job_status_active{job="process-batch", ...}                                            5
kube_job_status_succeeded{job="process-batch", ...}                                        95
kube_job_status_failed{job="process-batch", ...}                                            0

kube_cronjob_next_schedule_time{cronjob="nightly-cleanup", ...}                       1716534000
kube_cronjob_status_last_schedule_time{cronjob="nightly-cleanup", ...}                1716447600
kube_cronjob_status_active{cronjob="nightly-cleanup", ...}                                     0
```

### 30.3 The SLO trio for rollouts

For a service with rollouts, three SLOs are useful:

1. **Rollout completion p99 < 5 min** — measure as `time(observedGeneration matches generation AND Available=True) - time(generation bumped)`. Alert if breached.
2. **Rollout failure rate < 1%** — count `ProgressDeadlineExceeded` events as a ratio of rollout starts (generation bumps).
3. **Steady-state availability** — `kube_deployment_status_replicas_unavailable > 0` for more than 60 seconds in steady state (no rollout in progress) is a regression.

The third is the most important; the others are pre-warnings.

### 30.4 Job and CronJob health

For batch:

1. **Job failure rate** — `kube_job_status_failed > 0` is concerning; `> 0` for `successfulJobsHistoryLimit` of the recent Jobs is a sustained problem.
2. **CronJob missed schedules** — `kube_cronjob_status_last_schedule_time - kube_cronjob_next_schedule_time` should be bounded. Drift > 2 × period means missed runs.
3. **CronJob skew** — for global CronJobs, large skew between expected and actual run times suggests controller backlog.

---

## 31. Source-Tree Map

For diving into the implementation. All paths relative to `kubernetes/kubernetes` repo root.

```
pkg/controller/
├── controller_ref_manager.go       # adoption/release for any controller
├── controller_utils.go             # ActivePodsWithRanks, SlowStartBatch, pod filtering
│
├── deployment/
│   ├── deployment_controller.go    # main reconciler
│   ├── sync.go                     # syncDeployment, isScalingEvent
│   ├── rolling.go                  # rolloutRolling, reconcileNewReplicaSet, ...
│   ├── recreate.go                 # rolloutRecreate
│   ├── rollback.go                 # legacy rollbackTo handling
│   ├── progress.go                 # syncRolloutStatus, condition management
│   └── util/
│       └── deployment_util.go      # ComputeHash, MaxSurge, MaxUnavailable, GetProportion
│
├── replicaset/
│   └── replica_set.go              # syncReplicaSet, manageReplicas
│
├── daemon/
│   ├── daemon_controller.go        # syncDaemonSet
│   ├── update.go                   # rollingUpdate
│   └── util/
│       └── daemonset_util.go       # CreatePodTemplate, IsPodUpdated
│
├── job/
│   ├── job_controller.go           # syncJob, manageJob, finalizer handling
│   ├── indexed_job_utils.go        # JOB_COMPLETION_INDEX assignment
│   ├── pod_failure_policy.go       # rule evaluation
│   └── tracking_utils.go           # finalizer management
│
├── cronjob/
│   ├── cronjob_controllerv2.go     # syncCronJob (post-1.21 implementation)
│   └── utils.go                    # getRecentUnmetScheduleTimes
│
└── ttlafterfinished/
    └── ttlafterfinished_controller.go   # Job TTL deletion

pkg/registry/apps/deployment/strategy.go          # apiserver-side validation, immutability
pkg/registry/batch/job/strategy.go                # ditto for Job
pkg/registry/apps/daemonset/strategy.go           # ditto for DaemonSet
```

The `pkg/controller/` directory is small enough (<50k LOC across all workload controllers) to read end-to-end if you want a complete picture. The deployment controller alone is about 4000 lines and is the densest of the lot.

---

## 32. Pitfalls

The collected operational footguns from this chapter.

1. **Changing a selector on a live Deployment.** Selectors are immutable. The only way out is `--cascade=orphan` + recreate. Plan selectors thoughtfully on day one — include a unique `app` label.

2. **Apply without server-side-apply, fighting HPA.** Argo/Flux's classic spec.replicas fight with HPA. Move to SSA; let HPA's fieldManager own that field. See §28.2.

3. **Label/selector mismatch creating zombie RSes.** A misaligned RS template/selector causes the RS to create Pods that don't match its own selector. Reconcile loops infinitely. Catch this with admission policies and pre-commit linters.

4. **`kubectl rollout restart` misunderstanding.** It does not restart existing Pods. It writes an annotation that changes the template hash, which creates a new RS, which rolls. Existing Pods are killed; new ones come up. Stateful workloads lose in-memory state — by design.

5. **CronJob without `spec.timeZone`.** The controller's local TZ (usually UTC) is used. If your CronJob's intent was "at 2 AM local time", you've been running it at UTC. Always set timeZone.

6. **CronJob's `startingDeadlineSeconds` left default.** With no deadline, the "too many missed (>100)" guard trips after any sustained controller outage. The CronJob then refuses to schedule. Always set startingDeadlineSeconds.

7. **`activeDeadlineSeconds` vs `progressDeadlineSeconds` confusion.** Jobs have one (`activeDeadlineSeconds`, wall-clock total); Deployments have the other (`progressDeadlineSeconds`, time without progress). Don't paste config between them.

8. **DaemonSet update with `maxUnavailable: 1` on a huge cluster.** A 1000-node DS rolls one node at a time → ~10 hours of rollout. Use a percentage (`"10%"`) for large fleets.

9. **DS Pod template missing tolerations or PriorityClass.** A DS that lacks toleration for `node-role.kubernetes.io/control-plane:NoSchedule` won't run on control-plane nodes. Without `priorityClassName: system-node-critical`, it can be preempted during resource pressure.

10. **`maxSurge: 0 && maxUnavailable: 0`.** Admission rejects this, but only on creation. Some YAML templating tools emit it accidentally; you discover it when the next deployment is rejected.

11. **`maxUnavailable: replicas` quietly degrading to a full outage.** Looks valid, behaves like Recreate. Lint for this.

12. **Short `progressDeadlineSeconds` with slow startup.** 60-second deadline + 90-second JVM startup = always failing. Calibrate the deadline to actual startup time, not optimistic guesses.

13. **`minReadySeconds: 0` with a slow-converging app.** Pod is Ready before it's actually serving. Set minReadySeconds to at least one readiness probe period.

14. **Trusting status before checking `observedGeneration`.** Status fields lag the spec by one reconcile. Always gate on `observedGeneration >= generation`.

15. **Job restartPolicy: Always.** Rejected by admission. Must be `OnFailure` or `Never`. If you copied a Pod template from a Deployment, this is the first line to change.

16. **Indexed Job without `backoffLimitPerIndex`.** One permanently-bad index kills the entire Job after `backoffLimit` failures. Use per-index backoff with `maxFailedIndexes` for partial-success tolerance.

17. **Forgetting `ttlSecondsAfterFinished` on Jobs.** Completed Jobs accumulate in etcd. Cluster of 10k jobs/day → 300k objects/month → noticeable etcd impact.

18. **Job's pod template tolerations and PriorityClass forgotten.** Same pitfall as DSes: the Pod template needs explicit tolerations to run on tainted nodes. Often discovered when a GPU pool is added with a taint and existing Jobs stop scheduling there.

19. **CronJob concurrencyPolicy: Allow with a slow Job.** Jobs pile up; resources are exhausted; the cluster suffers. Use Forbid or Replace unless overlapping runs are explicitly desired.

20. **Deleting a Job without `--cascade=foreground` when you wanted to wait.** Default Background returns immediately; if your script then asks "are the pods gone?" the answer is "not yet." Use foreground for blocking semantics.

21. **DaemonSet on a cluster with mixed node types.** Without a `nodeSelector`, the DS Pod tries to run on every node — including the GPU node where it has no business, including the Windows node where the Linux image won't work. Always scope DSes with `nodeSelector` or `nodeAffinity`.

22. **`spec.suspend: true` left in production.** A Job created in suspended state (perhaps by an operator that meant to immediately unsuspend) never starts. Status never advances. Discovered hours later when someone checks. Always log/alert on long-suspended Jobs.

23. **Manually patching `status.replicas`.** It does nothing useful; the controller overwrites it on the next reconcile. Operators sometimes try this as a "stuck rollout" remedy and become more confused.

24. **PodDisruptionBudget interacting with rolling update.** A PDB with `minAvailable: 5` on a Deployment with `replicas: 5` and `maxUnavailable: 1` means the rolling update cannot proceed (cannot evict any pod without violating PDB). Calibrate PDB and rolling-update budgets together.

25. **Two Deployments with overlapping selectors.** Each adopts the other's Pods. Replica counts oscillate. Catch with a webhook that rejects overlapping selectors at the namespace level.

26. **Heavy `kubectl rollout restart` cadence.** Each restart creates a new RS. With `revisionHistoryLimit: 10` and a daily restart, you have a 10-day rotation of RSes. Etcd's burden is small per object, but visible in lists. Reduce restart cadence; investigate why apps need it.

---

## 33. TL;DR

Workload controllers are a small family of reconcilers, all sharing the same shape: **watch a desired-state object, claim children via selector + ownerRef, drive cardinality and template toward the spec, write status, repeat.**

- **ReplicaSet** is cardinality. Slow-start batch for creation; rank-based selection for deletion.
- **Deployment** is rolling-update orchestration over a chain of ReplicaSets. The `pod-template-hash` partitions Pods between RSes. `maxSurge` and `maxUnavailable` define the budget; `progressDeadlineSeconds` defines failure; `revisionHistoryLimit` defines rollback depth. Pause and Recreate are escape valves.
- **DaemonSet** is one-Pod-per-matching-node. Post-1.12 it uses the scheduler via hard nodeAffinity. Rolling updates walk nodes subject to `maxUnavailable`; OnDelete is manual. Always set tolerations and PriorityClass.
- **Job** is run-to-completion. `parallelism` × `completions` defines the shape; `backoffLimit` and `activeDeadlineSeconds` define the limits. Indexed completion mode gives deterministic sharding; `podFailurePolicy` gives per-failure control. Finalizers ensure terminal pods are not lost. Suspend turns Jobs into queueable units.
- **CronJob** is "create a Job on schedule." Always set `timeZone` and `startingDeadlineSeconds`. Choose `concurrencyPolicy` deliberately. Use `successfulJobsHistoryLimit` + Job's `ttlSecondsAfterFinished` to bound state.

Three invariants tie them together:

1. **ownerReferences form the ownership DAG.** Cascade deletes walk it; adoption rules fill it in.
2. **`observedGeneration` gates status trust.** Never act on a status field until the controller has caught up.
3. **Reconcile is level-triggered and idempotent.** Restart the controller, drop a watch event, replay a million reconciles — the outcome is identical from the same input.

If you internalize these, you can read the source ([§31](#31-source-tree-map)), debug a stuck rollout, design an operator, and predict the behavior of any future workload-shaped controller — including the StatefulSet, which we tackle in [chapter 13](13-statefulset-deep-dive.md).
