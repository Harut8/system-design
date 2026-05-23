# The Controller Pattern, client-go, and controller-runtime: A Staff-Level Deep Dive

A staff-engineer reference for the single programming model that underlies every Kubernetes built-in controller, every CRD operator, and every custom reconciler ever written. This is the chapter that the next thirty depend on. The reconcile loop is the entire programming model of Kubernetes; everything else is configuration around it.

We start from the axioms — what *declarative* and *level-triggered* actually mean and why anything else is wrong — then walk every component of client-go (Reflector, DeltaFIFO, Indexer, Informer, SharedInformerFactory, Workqueue, RateLimiter, Leader Election) byte by byte. We layer controller-runtime (Manager, Reconciler, Cache, Client, Builder, Predicates) on top. We finish with the discipline questions that distinguish a working controller from a correct one: Spec/Status separation, Generation/ObservedGeneration, Finalizers, Optimistic Concurrency, Server-Side Apply, testing with envtest, observability, and the long list of pitfalls every staff engineer has stepped on.

If you have not read chapter 05 (the apiserver — where informers list and watch from) and chapter 04 (etcd — what backs the watch stream), put a finger on those and come back. Chapter 12 (built-in workload controllers) and chapter 23 (CRDs and operators) are direct sequels.

---

## Table of Contents

1. [The Level-Triggered, Declarative Axiom](#1-the-level-triggered-declarative-axiom)
2. [The Reconcile Contract](#2-the-reconcile-contract)
3. [The Components, Top Down](#3-the-components-top-down)
4. [Reflector: ListAndWatch Internals](#4-reflector-listandwatch-internals)
5. [DeltaFIFO: The Buffered Change Queue](#5-deltafifo-the-buffered-change-queue)
6. [Indexer and Cache Reads](#6-indexer-and-cache-reads)
7. [Event Handlers and Resync](#7-event-handlers-and-resync)
8. [Workqueue Deep](#8-workqueue-deep)
9. [The Full Reconcile-Loop Scaffolding](#9-the-full-reconcile-loop-scaffolding)
10. [Leader Election](#10-leader-election)
11. [controller-runtime Overlay](#11-controller-runtime-overlay)
12. [Owns, Watches, and Custom Enqueue Mappings](#12-owns-watches-and-custom-enqueue-mappings)
13. [Status vs Spec Discipline](#13-status-vs-spec-discipline)
14. [The Conditions Pattern](#14-the-conditions-pattern)
15. [Generation, ResourceVersion, and UID](#15-generation-resourceversion-and-uid)
16. [Finalizers: The Deletion Guard](#16-finalizers-the-deletion-guard)
17. [Optimistic Concurrency in Updates](#17-optimistic-concurrency-in-updates)
18. [Server-Side Apply from Controllers](#18-server-side-apply-from-controllers)
19. [Predicates: Filtering Events Before They Enqueue](#19-predicates-filtering-events-before-they-enqueue)
20. [Filters and Scoping](#20-filters-and-scoping)
21. [Multi-Cluster Controllers (preview)](#21-multi-cluster-controllers-preview)
22. [Testing: envtest and Fake Client](#22-testing-envtest-and-fake-client)
23. [Performance and Resource Math](#23-performance-and-resource-math)
24. [Observability: The Standard Metrics](#24-observability-the-standard-metrics)
25. [Pitfalls: The Long List](#25-pitfalls-the-long-list)
26. [TL;DR](#26-tldr)

---

## 1. The Level-Triggered, Declarative Axiom

Kubernetes has exactly one programming model and it is older than Kubernetes. It comes from process control theory: a setpoint, a sensor, an actuator, and a loop that drives the sensor toward the setpoint. In Kubernetes vocabulary:

- The **setpoint** is the object's `spec`. The user writes it.
- The **sensor** is the object's `status`, plus whatever real-world state the controller can observe (other API objects, cloud APIs, hardware).
- The **actuator** is the controller. It compares `spec` to observed reality and emits whatever side effects move reality toward `spec`.
- The **loop** runs forever. It does not stop when the difference is zero; it just becomes a no-op.

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │                       THE ONLY DIAGRAM THAT MATTERS                  │
   ├──────────────────────────────────────────────────────────────────────┤
   │                                                                      │
   │                       ┌─────────────────┐                            │
   │                       │  desired state  │                            │
   │                       │   (object.spec) │◄────── user / GitOps       │
   │                       └────────┬────────┘                            │
   │                                │                                     │
   │                                ▼                                     │
   │                      ┌───────────────────┐                           │
   │     ┌───────────────►│                   │                           │
   │     │                │     reconcile     │──► side effects:          │
   │     │                │                   │     • write status        │
   │     │                └─────────┬─────────┘     • create/update Pods  │
   │     │                          │               • call cloud APIs     │
   │     │                          ▼               • patch other objects │
   │     │                ┌──────────────────┐                            │
   │     │   watch + cache│ observed state   │                            │
   │     └────────────────│ (status + world) │                            │
   │                      └──────────────────┘                            │
   │                                                                      │
   │  Properties this loop MUST have:                                     │
   │   - Idempotent: f(x) == f(f(x))                                      │
   │   - Level-triggered: reads current state, not events                 │
   │   - Eventually consistent: many iterations are allowed to converge   │
   │   - Single concurrency per key                                       │
   │                                                                      │
   └──────────────────────────────────────────────────────────────────────┘
```

### 1.1 Why edge-triggered is wrong

An "edge-triggered" controller is one that reacts to *events* as they arrive: "I saw an Add, so I create a Pod"; "I saw a Delete, so I clean up a LoadBalancer." This is how most developers first try to write a controller, because it mirrors how they write GUI event handlers or Kafka consumers.

It is broken. Here is the exhaustive list of ways an event-triggered controller misses an event:

1. **Watch reconnect.** The apiserver kicks watches periodically (default ~5 to 30 minutes, randomized) and watch streams break on any apiserver restart, rolling upgrade, network blip, or load-balancer rebalance. Each reconnect issues a fresh List, and any events the controller "missed" between the last seen `resourceVersion` and the relist are not replayed individually — they are collapsed into the new state of each object.
2. **Controller restart.** When your binary restarts (deploy, OOMKill, crashloop, normal autoscaling), the in-memory queue and the watch stream are gone. The new process starts fresh with a List, which returns *current state*, not *events since I died*.
3. **Resync.** A SharedInformer can be configured with a resync period that re-delivers every object in the cache as `Update` events, even when nothing changed. An edge-triggered controller has to do "if not really an update, ignore" logic on every callback, and the moment it gets that wrong it diverges from reality.
4. **DeltaFIFO compression.** Multiple updates to the same object that arrive while a previous one is still in the queue can be compressed into a single delta. The intermediate values are gone.
5. **410 Gone.** When the apiserver's watch cache no longer covers the client's `resourceVersion` (etcd compaction, watch cache eviction, or a too-slow consumer that fell behind), the server returns `410 Gone` and the client must relist. Same outcome as point 1.
6. **Object-level coalescing.** The watch cache itself may coalesce rapid sequential modifications of the same object before they fan out to watchers.

A level-triggered controller doesn't care about any of this. On every iteration it reads the *current* state of the object from its local cache and asks: "what side effects, if any, would make reality match this spec?" If it is invoked five times for the same object with the same spec, the first call does work; the rest do nothing. If it never received the Add event but is enqueued by a periodic resync, the work still gets done.

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │   EDGE-TRIGGERED vs LEVEL-TRIGGERED                                  │
   ├──────────────────────────────────────────────────────────────────────┤
   │                                                                      │
   │   EDGE-TRIGGERED                       LEVEL-TRIGGERED               │
   │   "react to events"                    "drive observed → desired"    │
   │                                                                      │
   │   func OnAdd(obj):                     func Reconcile(key):          │
   │     createDeployment(obj)                obj := cache.Get(key)       │
   │                                          if obj == nil:              │
   │   func OnUpdate(old, new):                 deleteSideEffects(key)    │
   │     if specChanged:                        return                    │
   │       updateDeployment(new)              applyToDesired(obj)         │
   │                                          status := observe(obj)      │
   │   func OnDelete(obj):                    writeStatus(obj, status)    │
   │     cleanupSideEffects(obj)                                          │
   │                                        Same function regardless of   │
   │   Different code paths.                how the key got enqueued:     │
   │   Events can be dropped.                Add, Update, Delete, Resync, │
   │   Restart loses state.                 explicit Requeue — all the   │
   │                                        same.                         │
   └──────────────────────────────────────────────────────────────────────┘
```

The slogan: **events are hints, not truth.** They are how you decide *when* to reconcile, never *what* to do. The cache is the truth. The cache is what you read.

### 1.2 The declarative axiom

"Declarative" in Kubernetes means: the user submits a desired-state document; the system makes it true and keeps it true. It is not "the user submits commands and the system runs them." Once you accept that, several invariants fall out automatically:

- A controller never deletes objects unless the spec says they should not exist. (Imperative shells delete on command; declarative shells delete because the model changed.)
- A controller never refuses to act on an object just because it acted on it before. Repetition is the norm.
- Two controllers acting on the same object must not assume their writes are the only writes. Server-side apply (section 18) was invented to make this safe.
- Controllers are bots; humans are also bots. The system makes no distinction between an admin's `kubectl edit` and an operator's `Patch`. Both are spec changes, both trigger reconciliation, both must converge.

### 1.3 What "reconcile" actually returns

In practice, the reconcile function returns one of four outcomes:

```go
// controller-runtime's Result; client-go's "raw" loop uses
// AddRateLimited / AddAfter / nothing to express the same thing.
type Result struct {
    Requeue      bool          // re-enqueue immediately (with rate limit)
    RequeueAfter time.Duration // re-enqueue after this delay
}

// Outcome 1: success, no follow-up needed.
return ctrl.Result{}, nil

// Outcome 2: I'm done for now but check again in a minute (cert expiry, TTL, ...).
return ctrl.Result{RequeueAfter: 60 * time.Second}, nil

// Outcome 3: I made progress but more work to do; re-enqueue with backoff.
return ctrl.Result{Requeue: true}, nil

// Outcome 4: error. Re-enqueue with rate-limited exponential backoff;
// also surface in metrics and logs.
return ctrl.Result{}, fmt.Errorf("...")
```

Note that returning `(Result{}, nil)` does **not** mean "this object will never reconcile again." It means "I don't need to be re-enqueued *by me, right now*." It will still reconcile on the next Add/Update/Delete event, on the next resync, on any owned-object change, and on any explicit enqueue from another controller. That is the level-triggered guarantee.

---

## 2. The Reconcile Contract

If the chapter ends here you still know enough to write a correct controller. The contract has four clauses.

### 2.1 Idempotent

`Reconcile(X)` called twice on the same observed state must produce the same side effects the first time and no side effects the second time. Practically this means:

- "Create child Pod" is wrong. "Ensure child Pod exists with this spec" is right.
- "POST to a cloud LB" is wrong. "Apply this declared LB; if it already matches, do nothing" is right.
- Counters in `status` are tricky. If you bump a counter every reconcile, you violate idempotence — except if you're tracking something the counter is meant to count, like "observed restart count." Then the *world* drives the counter, not the reconcile.

```go
// Wrong:
func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    obj := &myv1.Thing{}
    r.Get(ctx, req.NamespacedName, obj)
    obj.Status.ReconcileCount++          // bumped every call!
    r.Status().Update(ctx, obj)
    pod := newPodFor(obj)
    return ctrl.Result{}, r.Create(ctx, pod)   // fails on second call (AlreadyExists)
}

// Right:
func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    obj := &myv1.Thing{}
    if err := r.Get(ctx, req.NamespacedName, obj); err != nil {
        return ctrl.Result{}, client.IgnoreNotFound(err)
    }
    desired := newPodFor(obj)
    existing := &corev1.Pod{}
    err := r.Get(ctx, client.ObjectKeyFromObject(desired), existing)
    switch {
    case apierrors.IsNotFound(err):
        return ctrl.Result{}, r.Create(ctx, desired)
    case err != nil:
        return ctrl.Result{}, err
    default:
        if !specEquivalent(existing, desired) {
            existing.Spec = desired.Spec
            return ctrl.Result{}, r.Update(ctx, existing)
        }
        return ctrl.Result{}, nil
    }
}
```

The second form is verbose but is the canonical level-triggered shape: read, compare, apply if different, return.

### 2.2 Level-triggered

Reconcile must depend only on the *current* state of its inputs, never on how it got here. The signature `Reconcile(ctx, req) (Result, error)` is intentionally narrow: `req` carries only `(namespace, name)`. There is no "what changed" parameter, no diff, no event type. If you find yourself wanting to know "was this an Add or an Update?" you are writing an edge-triggered controller; refactor.

The only legitimate use of "what changed" is at the **predicate** layer, *before* the workqueue (section 19) — predicates can decide that an Update with no spec change isn't worth enqueuing. But the reconciler itself never knows the predicate verdict; it just sees a key.

### 2.3 Eventually consistent

A correct controller is allowed to take many reconciles to converge. It is allowed to wait for external systems (a Pod becomes Ready, a cloud LB finishes provisioning, a TLS cert is issued). It is *not* allowed to block synchronously inside Reconcile waiting for those things.

The pattern is: do as much work as you can right now, write status to reflect "I'm waiting for X," and return `(Result{}, nil)` or `(Result{RequeueAfter: 30s}, nil)`. The next reconcile (triggered by the watch event on X, by a resync, or by your RequeueAfter) picks up where you left off.

```go
// Wrong: blocking inside reconcile.
func (r *Reconciler) Reconcile(...) (ctrl.Result, error) {
    cert := requestCert(...)
    for !cert.Ready() {
        time.Sleep(10 * time.Second)   // blocks a worker goroutine forever
    }
    return ctrl.Result{}, nil
}

// Right: return, re-check next time.
func (r *Reconciler) Reconcile(...) (ctrl.Result, error) {
    cert := observeCertStatus(...)
    if !cert.Ready() {
        // We watch Certificate objects via Owns(); the watch will re-enqueue
        // us when cert status changes. The RequeueAfter is a safety net.
        meta.SetStatusCondition(&obj.Status.Conditions, metav1.Condition{
            Type: "Ready", Status: metav1.ConditionFalse,
            Reason: "WaitingForCert", Message: "TLS cert not yet issued",
            ObservedGeneration: obj.Generation,
        })
        return ctrl.Result{RequeueAfter: 60 * time.Second}, r.Status().Update(ctx, obj)
    }
    // ... proceed with work that depends on the cert being ready ...
    return ctrl.Result{}, nil
}
```

### 2.4 Single concurrency per key

The workqueue (section 8) guarantees that at most one worker is reconciling a given key at a time. If two events arrive for the same key while a worker is busy, they are coalesced — when the worker calls `Done(key)`, the queue checks whether the key was re-added during processing and, if so, re-queues it once.

This is a *guarantee from the workqueue*, not from the reconciler. It frees the reconciler from internal locking on the object: you cannot have two of your own goroutines reconciling the same Pod. It does **not** protect you from:

- Two *different* controllers reconciling the same object (e.g., a CronJob controller and a third-party annotation controller both writing different annotations on the same Pod).
- Two replicas of the *same* controller (defeated by leader election; section 10).
- Concurrent reconciles of *different* keys (your shared state must be safe across worker goroutines).

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │   THE FOUR CONTRACT CLAUSES                                          │
   ├──────────────────────────────────────────────────────────────────────┤
   │                                                                      │
   │   1. Idempotent          f(x) == f(f(x))                             │
   │   2. Level-triggered     reads current state, ignores history        │
   │   3. Eventually          may take many reconciles to converge;       │
   │      consistent          never blocks waiting                        │
   │   4. Single concurrency  one worker per key at a time                │
   │      per key                                                         │
   │                                                                      │
   └──────────────────────────────────────────────────────────────────────┘
```

---

## 3. The Components, Top Down

client-go's `tools/cache` package implements a small set of composable building blocks. They have unfortunate names (the codebase calls the same thing "the Store," "the Indexer," "the cache," and "the Lister") but the pieces are sharp and the layering is real. Here they are, top down, in the order data flows from etcd to your reconcile function:

```
┌──────────────────────────────────────────────────────────────────────────┐
│                                                                          │
│   kube-apiserver  (HTTP/2 watch stream, protobuf, resourceVersion-based) │
│                                                                          │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │  GET ?watch=true&resourceVersion=N
                                     │  (long-lived chunked HTTP/2 stream)
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  REFLECTOR                                                               │
│   ListAndWatch loop: List on start, then Watch with resourceVersion;     │
│   on stream error → exponential backoff → relist + watch.                │
│   Pushes Added/Updated/Deleted/Sync/Replaced into ↓                      │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │  Add(obj, type), Replace([]obj), Resync()
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  DELTAFIFO                                                               │
│   Keyed by namespace/name. Each key holds an ordered list of Deltas.     │
│   Pop returns all deltas for one key; consumer processes in order        │
│   and is expected to apply them to the Indexer.                          │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │  Pop(processFunc)
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  PROCESSDELTAS  (the "controller" loop inside the informer)              │
│   For each delta:                                                        │
│     - update Indexer: Add/Update/Delete                                  │
│     - notify event handlers: OnAdd/OnUpdate/OnDelete                     │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
              ┌──────────────────────┼─────────────────────────┐
              ▼                      ▼                         ▼
        ┌───────────┐         ┌─────────────┐           ┌──────────────┐
        │  INDEXER  │         │ EVENT       │           │ EVENT        │
        │  (cache)  │         │ HANDLER #1  │           │ HANDLER #2   │
        │           │         │ enqueue key │           │ (other       │
        │ ByIndex   │         │ for ctrl-A  │           │  controller) │
        │ Get/List  │         └─────┬───────┘           └──────┬───────┘
        └───────────┘               │                          │
              ▲                     ▼                          ▼
              │              ┌────────────┐             ┌────────────┐
              │              │ WORKQUEUE  │             │ WORKQUEUE  │
              │              │ A          │             │ B          │
              │              └─────┬──────┘             └────────────┘
              │                    │
              │                    ▼
              │              ┌────────────┐
              │              │ WORKER     │
              └──────────────│ POOL (A)   │  cache.Get(key) ──► Reconcile
                             └────────────┘
                                  │
                                  ▼
                            apiserver writes
                            (back to top)
```

Let's name the pieces precisely:

- **Reflector** (`staging/src/k8s.io/client-go/tools/cache/reflector.go`): one goroutine that owns a `ListerWatcher` for a single `(GroupVersionResource, namespace, labelSelector, fieldSelector)` tuple. It runs the ListAndWatch loop.
- **DeltaFIFO** (`tools/cache/delta_fifo.go`): the in-memory queue that buffers reflector output. Keyed; ordered per key; non-blocking to producers; blocking Pop for consumers.
- **Indexer / ThreadSafeStore** (`tools/cache/thread_safe_store.go`, `tools/cache/store.go`): the local cache. RWMutex-protected hash table from `namespace/name` to the object, plus any user-registered secondary indexes.
- **Controller** (`tools/cache/controller.go`): a small loop that pulls from DeltaFIFO and forwards to Indexer + handlers. Not to be confused with *your* controller (the reconciler). Sometimes called the "informer's controller" or the "delta-processing controller."
- **Informer** = Reflector + DeltaFIFO + Indexer + Controller + event-handler registry, glued together.
- **SharedInformer** = an informer with multiple event-handler listeners; the most common kind. Adds buffering and per-listener resync.
- **SharedInformerFactory** (`tools/cache/shared_informer.go` / `informers/factory.go`): a process-wide registry that ensures at most one informer per `(GVR, namespace, labelSelector)`. Multiple controllers that want events for Pods share the *same* underlying Reflector and Indexer.
- **Workqueue** (`tools/cache/workqueue/*`): a deduping, rate-limiting, delaying queue of *keys* (not objects).
- **RateLimiter**: the policy that decides how long to delay a key when it is re-enqueued after an error.
- **Worker pool**: the user's goroutines that loop `Get(key) → Reconcile → Done(key)`.
- **Leader Election** (`tools/leaderelection/*`): a separate goroutine that fights for a Lease object and only allows reconciles to run on the winning replica.

The genius of this layering is that everything above the workqueue is shared and reusable; everything below it is your code. A SharedInformerFactory feeds an arbitrary number of controllers with zero extra apiserver load — there is still exactly one watch stream per GVR/scope.

---

## 4. Reflector: ListAndWatch Internals

A Reflector is roughly 600 lines of Go in `staging/src/k8s.io/client-go/tools/cache/reflector.go` and it carries enormous load. Conceptually it does this in a loop:

```go
func (r *Reflector) ListAndWatch(stopCh <-chan struct{}) error {
    // Phase 1: initial List
    options := metav1.ListOptions{
        ResourceVersion:      "",  // or "0" — see below
        AllowWatchBookmarks:  true,
    }
    list, err := r.listerWatcher.List(options)
    if err != nil {
        return err
    }
    items := meta.ExtractList(list)
    listMetaInterface, _ := meta.ListAccessor(list)
    resourceVersion := listMetaInterface.GetResourceVersion()

    // Replace the entire store contents in one shot.
    // DeltaFIFO turns this into "Replaced" deltas for current keys
    // and "Deleted" deltas for keys that vanished.
    if err := r.store.Replace(items, resourceVersion); err != nil {
        return err
    }
    r.setLastSyncResourceVersion(resourceVersion)

    // Phase 2: Watch from listVersion forward, forever (until error).
    for {
        timeoutSeconds := int64(minWatchTimeout.Seconds() *
            (rand.Float64() + 1.0))  // randomized to avoid thundering herd
        watcher, err := r.listerWatcher.Watch(metav1.ListOptions{
            ResourceVersion:     resourceVersion,
            AllowWatchBookmarks: true,
            TimeoutSeconds:      &timeoutSeconds,
        })
        if err != nil {
            // Connection error, etc. Will relist after backoff.
            return err
        }
        if err := r.watchHandler(start, watcher, &resourceVersion, ...); err != nil {
            if !isExpectedError(err) {
                return err
            }
            // "Expected" includes 410 Gone, which signals a relist.
            return err
        }
    }
}
```

The wrapper around `ListAndWatch` is a backoff loop:

```go
func (r *Reflector) Run(stopCh <-chan struct{}) {
    wait.BackoffUntil(func() {
        if err := r.ListAndWatch(stopCh); err != nil {
            r.watchErrorHandler(r, err)
        }
    }, r.backoffManager, true, stopCh)
}
```

So when a watch breaks (network glitch, apiserver restart, 410 Gone, controlled timeout), the Reflector silently relists and re-watches. The application code never sees the disruption — it only sees a stream of deltas, with a transient burst of `Replaced` deltas when the relist happens.

### 4.1 The first List: resourceVersion="" vs "0"

There is one subtle knob: the `ResourceVersion` parameter on the initial List.

- `ResourceVersion=""` (empty string) → **quorum read from etcd**. The apiserver bypasses its watch cache and reads through to etcd, performing a linearizable read. Slow, expensive, definitive.
- `ResourceVersion="0"` → **read from the watch cache**. The apiserver returns whatever it has in its in-memory snapshot; the data may be a few milliseconds stale but no etcd round-trip is required.

Default reflectors use `""` on the very first List of process lifetime and `"0"` on subsequent relists during the same process. The reasoning is:

- On startup, you must guarantee that you do not miss any object that exists. A quorum read gives you that. (Reading from a watch cache that is still warming up could return a partial snapshot.)
- On a relist (because the watch broke), the cache is already populated and you just need to refresh; "0" is sufficient because the next watch from the returned RV will catch up any gap.

For very large clusters, the quorum read on startup is *the* expensive thing — a List of 100 000 Pods can take seconds and adds noticeable load to etcd. Hence: **share informers**. One informer, one List, N controllers (section 23).

### 4.2 Watch error handling: 410 Gone and friends

The watch stream can fail in three ways the Reflector cares about:

1. **Stream timeout**: the apiserver chose to close the connection because the randomized watch timeout (`TimeoutSeconds`) expired. Not an error; just reopen.
2. **Network/server error**: connection dropped, apiserver shutting down, etc. Back off and reopen; if reopen also fails, fall back to relist.
3. **410 Gone (`StatusGone`)**: the apiserver says "the resourceVersion you asked me to watch from is too old; I no longer have those events." This happens when the client falls behind for too long, or when etcd compaction has removed those revisions (chapter 04). Reflector must relist with `RV=""` (or `"0"` after the first List) to recover a fresh snapshot.

```
┌──────────────────────────────────────────────────────────────────────────┐
│   REFLECTOR STATE MACHINE                                                │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│        ┌─────────────────┐                                               │
│        │      LIST       │  initial: RV="" (quorum)                      │
│        │   (full set)    │  relist:  RV="0" (cache)                      │
│        └────────┬────────┘                                               │
│                 │  resourceVersion N                                     │
│                 ▼                                                        │
│        ┌─────────────────┐                                               │
│   ┌──► │      WATCH      │  GET ?watch=true&resourceVersion=N            │
│   │    │   (delta stream)│                                               │
│   │    └────┬───────┬────┘                                               │
│   │         │       │                                                    │
│   │  events │       │ error                                              │
│   │         ▼       │                                                    │
│   │   advance RV    │                                                    │
│   │   (per event)   │                                                    │
│   │         │       ▼                                                    │
│   │         │  ┌──────────┐                                              │
│   │         │  │  ERROR   │  classify                                    │
│   │         │  └────┬─────┘                                              │
│   │         │       │                                                    │
│   │         │       ├── stream timeout ──┐                               │
│   │         │       │                    │                               │
│   │         │       ├── transient ──► backoff ─┐                         │
│   │         │       │                          │                         │
│   │         │       └── 410 Gone ──────────────┼──► RELIST               │
│   │         │                                  │                         │
│   │         └──────────────────────────────────┘                         │
│   │                                            │                         │
│   └────────────────────────────────────────────┘                         │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Bookmarks

Newer apiservers emit `Bookmark` watch events periodically. Their only payload is a fresh `resourceVersion`; their meaning is "you are caught up to here, even though no objects have changed." Reflectors use bookmarks to advance their stored `resourceVersion` so that if the watch breaks, they can resume from a recent point without missing events and without having to relist.

Without bookmarks, a quiet object stream (e.g., a CRD that rarely changes) would keep the client's `resourceVersion` far behind the cluster's current RV. After enough etcd compactions, the next watch reconnect would hit 410 Gone and force a relist. Bookmarks make this much rarer.

### 4.4 The cost of a relist vs a watch event

| Operation | apiserver cost | etcd cost | Network cost |
|---|---|---|---|
| Watch event (single object change) | ~1 µs (cache hit, fan out) | 0 (already delivered) | ~size of object × N watchers |
| List from watch cache (RV="0") | O(N) cache traversal | 0 | ~size of all objects in scope |
| List quorum (RV="") | O(N) cache + linearizable read | one linearizable Read | ~size of all objects in scope |
| 410 Gone → relist | O(N) | as above | as above |

A single watch event delivering a 5 KB Pod object to 1000 watchers is 5 MB of network — but it's already on the apiserver, the etcd cost is zero, and the apiserver scales fan-out reasonably well via the watch cache.

A relist of a 100 000-pod cluster is ~500 MB through the apiserver and is a *load event*. This is why a flapping watch (frequent 410 Gone) is a far worse outage than a noisy watch.

---

## 5. DeltaFIFO: The Buffered Change Queue

DeltaFIFO sits between the Reflector and the Indexer. Its job is to buffer changes so that:

- The Reflector never blocks on the consumer.
- The consumer processes changes one *key* at a time, in order, with at most one in-flight key.
- Multiple changes to the same object that arrive in quick succession are appended together so they can be applied atomically.

Located in `staging/src/k8s.io/client-go/tools/cache/delta_fifo.go`.

### 5.1 The data structure

Conceptually:

```go
type DeltaFIFO struct {
    lock  sync.RWMutex
    cond  sync.Cond           // signaled on Add and Close

    // items: keyed by "namespace/name", value is the ordered list of
    // deltas for that key. Note: not a single Delta — a *slice*.
    items map[string]Deltas

    // queue: ordered list of keys with pending deltas. FIFO.
    queue []string

    // populated, initialPopulationCount: used to implement HasSynced().
    // The Reflector calls Replace() once with the initial list;
    // we set initialPopulationCount = len(items) and decrement it as
    // each key is Pop'd. HasSynced() returns true once it reaches 0.
    populated              bool
    initialPopulationCount int

    keyFunc KeyFunc
    // ... knownObjects (the Indexer), emitDeltaTypeReplaced, etc.
}

type Deltas []Delta
type Delta struct {
    Type   DeltaType
    Object interface{}
}

type DeltaType string
const (
    Added    DeltaType = "Added"
    Updated  DeltaType = "Updated"
    Deleted  DeltaType = "Deleted"
    Replaced DeltaType = "Replaced"  // from a relist
    Sync     DeltaType = "Sync"      // from a resync
)
```

The invariant: for each key in the queue, `items[key]` is a non-empty ordered slice of deltas. When the consumer calls `Pop`, it gets back the entire slice for the front key and the key is removed from `items` and `queue`.

### 5.2 Delta compression

If multiple deltas arrive for the same key before the consumer pops it, they accumulate. The DeltaFIFO does some compression:

- `Add` followed by `Delete` does *not* collapse to nothing — the consumer needs to see the Delete to remove the object from caches and to fire OnDelete handlers, even though the net effect on the local state is "object not present." (This is critical for orphan cleanup.)
- `Update` followed by `Update` does collapse: the second Update replaces the first in the slice (`dedupDeltas`). Only the latest wire-format object is retained, because the consumer only needs to know "current state of this object."
- `Sync` followed by anything else is kept; `Sync` is the resync signal.

The point of compression is to avoid unbounded memory growth when a producer (the Reflector) outpaces the consumer.

### 5.3 Replaced: the relist signal

After a Reflector relist, it calls `DeltaFIFO.Replace(items, resourceVersion)`. The FIFO does this:

1. For each item in the new list, append a `Replaced` delta to its key.
2. For each key that was in the FIFO *or* in the known-objects Indexer but is *not* in the new list, append a synthetic `Deleted` delta with `DeletedFinalStateUnknown` marker.

The point of step 2 is critical: between the watch breaking and the relist returning, objects may have been deleted. The Reflector cannot tell which ones; it just sees they're not in the new list. So it emits a synthetic Delete to make sure downstream handlers run cleanup.

```go
// pkg/client/tools/cache/delta_fifo.go (simplified)
func (f *DeltaFIFO) Replace(list []interface{}, resourceVersion string) error {
    f.lock.Lock()
    defer f.lock.Unlock()

    keys := sets.NewString()
    for _, item := range list {
        key, _ := f.KeyOf(item)
        keys.Insert(key)
        f.queueActionLocked(Replaced, item)  // or "Sync" in legacy code paths
    }

    // For every known key not in the new list, emit a synthetic Delete.
    if f.knownObjects != nil {
        for _, k := range f.knownObjects.ListKeys() {
            if keys.Has(k) {
                continue
            }
            obj, _, _ := f.knownObjects.GetByKey(k)
            f.queueActionLocked(Deleted, DeletedFinalStateUnknown{
                Key: k, Obj: obj,
            })
        }
    }
    if !f.populated {
        f.populated = true
        f.initialPopulationCount = keys.Len()
    }
    return nil
}
```

`DeletedFinalStateUnknown` is a sentinel type that wraps the last-known object. Handlers must handle it specially (see section 7), because the object inside is potentially stale — it's whatever we had cached at the time of the relist, which may be older than the actual delete.

### 5.4 HasSynced

The `HasSynced()` method returns true once the initial list has been popped through. It is **not** "the cache reflects current cluster state forever" — it is "I have seen at least one full snapshot." Controllers must wait for `HasSynced()` before starting their work; otherwise they'd see an empty cache and incorrectly conclude that no objects exist.

```go
func (c *Controller) Run(stopCh <-chan struct{}) {
    go c.informer.Run(stopCh)

    // Block until all informers have done their initial List.
    if !cache.WaitForCacheSync(stopCh, c.podInformer.HasSynced,
                                       c.svcInformer.HasSynced) {
        runtime.HandleError(fmt.Errorf("timed out waiting for caches to sync"))
        return
    }

    // Only now is it safe to start workers.
    for i := 0; i < c.workers; i++ {
        go wait.Until(c.runWorker, time.Second, stopCh)
    }
    <-stopCh
}
```

This is the single most common bug in handwritten controllers: not waiting for cache sync, then crashing because `cache.Get` returns nil and the code path didn't expect that.

---

## 6. Indexer and Cache Reads

The Indexer (`tools/cache/thread_safe_store.go` plus `tools/cache/index.go`) is the in-memory cache that backs all reads. It is:

- A `map[string]interface{}` (the primary store, keyed by namespace/name).
- Zero or more secondary `Indexers` — user-defined functions that map an object to a set of index keys, used to answer queries like "give me all Pods on this Node" without scanning.
- An `sync.RWMutex` protecting both.

```go
// pkg/client-go/tools/cache/thread_safe_store.go (sketch)
type threadSafeMap struct {
    lock  sync.RWMutex
    items map[string]interface{}

    // indexers map[name]IndexFunc:
    //   "byNode": func(obj) []string { return []string{pod.Spec.NodeName} }
    indexers Indexers

    // indices map[indexName] map[indexKey] set-of-object-keys:
    //   "byNode" → "node-1" → {"default/pod-a", "default/pod-b"}
    indices Indices
}

type IndexFunc func(obj interface{}) ([]string, error)
type Indexers map[string]IndexFunc
type Indices map[string]Index
type Index   map[string]sets.String
```

### 6.1 Adding an indexer

You can attach custom indexers at informer construction time:

```go
podInformer := factory.Core().V1().Pods().Informer()
podInformer.AddIndexers(cache.Indexers{
    "byNode": func(obj interface{}) ([]string, error) {
        pod, ok := obj.(*corev1.Pod)
        if !ok {
            return nil, nil
        }
        return []string{pod.Spec.NodeName}, nil
    },
})

// Later, look up all pods on node-7 without scanning:
pods, err := podInformer.GetIndexer().ByIndex("byNode", "node-7")
```

This is how the scheduler, the kubelet, kube-proxy, and most controllers avoid O(N) scans. A typical kube-controller-manager configures multiple indexers per informer to support its inner queries.

The cost is memory: each indexer adds `O(N)` extra map entries. The standard advice is "add indexers for queries that run frequently in your hot path; don't preemptively index everything."

### 6.2 Composite key: namespace/name

The primary key is always the string `namespace/name` (and just `name` for cluster-scoped objects), produced by `cache.MetaNamespaceKeyFunc`:

```go
// pkg/client-go/tools/cache/store.go
func MetaNamespaceKeyFunc(obj interface{}) (string, error) {
    if d, ok := obj.(DeletedFinalStateUnknown); ok {
        return d.Key, nil
    }
    meta, err := meta.Accessor(obj)
    if err != nil {
        return "", fmt.Errorf("object has no meta: %v", err)
    }
    if len(meta.GetNamespace()) > 0 {
        return meta.GetNamespace() + "/" + meta.GetName(), nil
    }
    return meta.GetName(), nil
}

func SplitMetaNamespaceKey(key string) (namespace, name string, err error) {
    parts := strings.Split(key, "/")
    switch len(parts) {
    case 1:
        return "", parts[0], nil       // cluster-scoped
    case 2:
        return parts[0], parts[1], nil // namespaced
    }
    return "", "", fmt.Errorf("unexpected key format: %q", key)
}
```

Workqueues store these keys, not objects. There are two reasons:

1. **Coalescing.** Two workqueue Adds of the same key produce one entry. If we stored objects, we'd have to define "same" and we'd risk losing updates between enqueue and process.
2. **Always-fresh.** When the worker pops a key, it reads the *current* cached object — not the snapshot from when the event fired. This is what makes the controller level-triggered.

### 6.3 Read semantics: stale-but-monotonic

The cache reflects what the watch stream has delivered so far. It is:

- **Stale by some small amount.** Between the time the apiserver commits a change to etcd and the time the watch event reaches your Reflector and is applied to the Indexer, there is a delay — typically a few milliseconds in a healthy cluster, but unbounded in the presence of slow consumers, network problems, or apiserver pressure.
- **Never future.** The cache cannot show you an object state that hasn't yet been committed. The apiserver also can't, but it's worth saying — your reads are always *behind* the true state, never ahead.
- **Per-key monotonic.** Within a single key, you see events in order. Watch streams are sequenced; the Reflector preserves order; the DeltaFIFO is FIFO per key. You will never see version N after version N+1 for the same object.
- **Not cross-key consistent.** Different objects' updates can be reordered relative to each other. If you write Pod A then Pod B, another controller may see B before A. There is no global snapshot.

The "never future" property is what makes the cache safe for read-only consumption. The "stale by some small amount" property is what makes it dangerous for write decisions — but mostly tolerable, because you'll get a new event soon and reconcile again.

The lurking exception: when *you* just wrote an object, your cache may not yet reflect your own write. The pattern is:

```go
// Bad: write then immediately read from cache.
r.Update(ctx, obj)
fresh := &Thing{}
r.Get(ctx, key, fresh)
// fresh may NOT reflect your write yet! The cache hasn't seen the
// watch event from your own update.

// Good: trust the next reconcile.
r.Update(ctx, obj)
return ctrl.Result{}, nil   // the watch event will re-enqueue us soon.
```

If you absolutely need read-your-writes consistency on the same call (rare; almost always a smell), use a *direct* (non-caching) client. controller-runtime's `Client` exposes both via `mgr.GetAPIReader()`.

### 6.4 List vs Get on the cache

```go
// O(1) lookup, returns the cached object (or nil if not present).
obj, exists, err := indexer.GetByKey("default/my-pod")

// O(N) scan, returns all objects in scope (everything the informer
// was configured for — namespace + selector).
objs := indexer.List()

// O(M) where M = objects matching this index key.
objs, err := indexer.ByIndex("byNode", "node-7")
```

The objects returned are *the same pointers as in the cache*. Mutating them is a bug — you would mutate the cache for every other reader. Always `DeepCopy` before modifying:

```go
pod, exists, _ := podLister.Get("default/my-pod")
if !exists { return }
podCopy := pod.DeepCopy()
podCopy.Annotations["mycontroller/seen-at"] = time.Now().Format(time.RFC3339)
r.Update(ctx, podCopy)
```

controller-runtime's `Client.Get` does the DeepCopy for you on the way out, which is one reason it's preferred over raw `Lister` access. But there is no free lunch — DeepCopy on a big object (Pod with 50 containers, Deployment with a complex PodTemplate) is not free either. In hot loops, raw Lister reads + manual selective DeepCopy can be faster.

---

## 7. Event Handlers and Resync

When a delta is processed by the informer's internal controller (`processLoop` → `processDeltas`), two things happen:

1. The Indexer is updated (Add, Update, Delete).
2. Every registered event handler is notified.

The interface:

```go
// pkg/client-go/tools/cache/shared_informer.go
type ResourceEventHandler interface {
    OnAdd(obj interface{}, isInInitialList bool)
    OnUpdate(oldObj, newObj interface{})
    OnDelete(obj interface{})
}

// Convenience: ResourceEventHandlerFuncs gives you function literals.
type ResourceEventHandlerFuncs struct {
    AddFunc    func(obj interface{})
    UpdateFunc func(oldObj, newObj interface{})
    DeleteFunc func(obj interface{})
}
```

Registration:

```go
podInformer.AddEventHandler(cache.ResourceEventHandlerFuncs{
    AddFunc:    func(obj interface{})          { enqueue(obj) },
    UpdateFunc: func(_, obj interface{})       { enqueue(obj) },
    DeleteFunc: func(obj interface{})          { enqueue(obj) },
})

// or, with a per-handler resync period:
podInformer.AddEventHandlerWithResyncPeriod(handler, 30 * time.Minute)
```

### 7.1 The canonical handler: enqueue a key

The almost-universal handler pattern:

```go
func (c *Controller) enqueue(obj interface{}) {
    key, err := cache.DeletionHandlingMetaNamespaceKeyFunc(obj)
    if err != nil {
        runtime.HandleError(fmt.Errorf("couldn't get key for %v: %v", obj, err))
        return
    }
    c.workqueue.Add(key)
}
```

Three things to notice:

- `DeletionHandlingMetaNamespaceKeyFunc` is `MetaNamespaceKeyFunc` plus handling of `DeletedFinalStateUnknown` (extracts the embedded `Key`). Use it everywhere; the plain version crashes on tombstones.
- We enqueue a *key*, not the object. The worker re-reads the object from the cache when it pops.
- The handler does almost no work. Heavy logic in handlers is wrong — handlers run on the shared informer's processing goroutine and block all other handlers behind them.

### 7.2 OnDelete and DeletedFinalStateUnknown

When the Reflector misses a delete (because the watch broke, then the relist returned a smaller set), the synthetic Delete carries a `DeletedFinalStateUnknown`:

```go
DeleteFunc: func(obj interface{}) {
    pod, ok := obj.(*corev1.Pod)
    if !ok {
        // Either a tombstone or a different type. Handle the tombstone case.
        tombstone, ok := obj.(cache.DeletedFinalStateUnknown)
        if !ok {
            runtime.HandleError(fmt.Errorf("expected Pod, got %T", obj))
            return
        }
        pod, ok = tombstone.Obj.(*corev1.Pod)
        if !ok {
            runtime.HandleError(fmt.Errorf("tombstone wraps non-Pod: %T", tombstone.Obj))
            return
        }
    }
    // ... handle the pod ...
},
```

If your DeleteFunc only handles `*corev1.Pod` and not the tombstone, you will leak side effects on every watch-break-then-relist sequence. This is one of the most common bugs in legacy controllers; the function `DeletionHandlingMetaNamespaceKeyFunc` exists specifically to make the simple "enqueue a key" case bullet-proof.

### 7.3 Resync: a heartbeat, not a refresh

Each registered handler can request a "resync period." Every period, the informer walks its cache and emits an `OnUpdate(old, new)` event for every object — where `old == new` (the same object). This is *not* a refetch from the apiserver; it's a synthetic re-delivery of what's in the cache.

```go
podInformer.AddEventHandlerWithResyncPeriod(handler, 30 * time.Minute)
```

Why bother? Because in a level-triggered world, you want to make extra sure that no key is sitting in the cache without being reconciled. Maybe the worker crashed before calling Done; maybe a handler had a transient bug; maybe an enqueue was lost. Resync re-enqueues everything periodically as a safety net.

This is also why your handler should *not* assume that "Update means something actually changed." Most handlers just enqueue the key and let the reconciler figure out whether work is needed:

```go
UpdateFunc: func(oldObj, newObj interface{}) {
    // Don't try to optimize here. Always enqueue.
    enqueue(newObj)
},
```

If you do want to optimize (e.g., skip enqueueing on status-only changes), that belongs in a predicate (section 19), not in the handler.

### 7.4 Resync period rules

- The factory has a *minimum* default resync period; per-handler resync periods are rounded up to this minimum.
- A resync period of zero disables resync for that handler — the handler only fires on real watch events.
- All handlers on the same SharedInformer share the same underlying watch. They differ only in resync period.

A common default is 10 minutes; some controllers disable resync entirely if they trust their workqueue requeue logic. The default in controller-runtime is 10 hours (`SyncPeriod` on the Manager), which is intentionally long: controller-runtime relies on predicates and explicit Requeue rather than resync as the safety net.

---

## 8. Workqueue Deep

Workqueues are the most carefully designed and most under-appreciated piece of client-go. Code lives in `staging/src/k8s.io/client-go/util/workqueue/`.

### 8.1 The interfaces

```go
// pkg/util/workqueue/queue.go
type Interface interface {
    Add(item interface{})
    Len() int
    Get() (item interface{}, shutdown bool)
    Done(item interface{})
    ShutDown()
    ShutDownWithDrain()
    ShuttingDown() bool
}

type DelayingInterface interface {
    Interface
    AddAfter(item interface{}, duration time.Duration)
}

type RateLimitingInterface interface {
    DelayingInterface
    AddRateLimited(item interface{})
    Forget(item interface{})
    NumRequeues(item interface{}) int
}
```

The vocabulary:

- `Add(item)`: enqueue immediately.
- `AddAfter(item, dur)`: enqueue after delay.
- `AddRateLimited(item)`: enqueue after the rate limiter's delay (typically exponential backoff for this item).
- `Get()` (blocking): pop the next item, mark it as "processing."
- `Done(item)`: tell the queue you're done. If the item was re-Added while you held it, the queue re-enqueues it now.
- `Forget(item)`: reset the rate limiter's memory of this item — it has been successfully reconciled, the next failure should start backoff from zero.

### 8.2 The implementation: three sets

The non-delayed `Type` implementation in `queue.go` is famously elegant. It has three internal collections:

```go
type Type struct {
    cond *sync.Cond

    // ordered: keys in FIFO order, deduplicated.
    queue []t

    // dirty: keys that need processing. The set the queue logically holds.
    dirty set

    // processing: keys currently held by a worker.
    processing set

    shuttingDown bool
}
```

The Add/Get/Done dance:

```
   Add(K):
     if dirty.Has(K): return         // already pending
     dirty.Insert(K)
     if processing.Has(K): return    // worker holds it; will re-enqueue at Done
     queue.append(K)
     cond.Signal()

   Get():
     wait until len(queue) > 0
     K = queue.pop_front()
     processing.Insert(K)
     dirty.Remove(K)
     return K

   Done(K):
     processing.Remove(K)
     if dirty.Has(K):                 // came back while we were holding it
       queue.append(K)
       cond.Signal()
```

The dirty set is the "logical queue" — every key that needs reconciliation. The processing set is the "currently in flight." The ordered slice is the "FIFO of unblocked work."

The result:

- **Dedupe**: adding the same key 1000 times while a worker holds it once results in *one* re-enqueue at Done time. No work is amplified by event spam.
- **At-most-one in flight**: by the dirty/processing split, only one worker can hold a given key.
- **No lost updates**: if a key is re-added during processing, the post-Done re-enqueue guarantees we'll reconcile again with the latest state.

```
┌──────────────────────────────────────────────────────────────────────────┐
│   WORKQUEUE STATE: dirty + processing + ordered queue                    │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   Initial state:                                                         │
│     queue:      []                                                       │
│     dirty:      {}                                                       │
│     processing: {}                                                       │
│                                                                          │
│   Add(A):                                                                │
│     queue:      [A]                                                      │
│     dirty:      {A}                                                      │
│     processing: {}                                                       │
│                                                                          │
│   Add(A):  (already dirty; no-op)                                        │
│     queue:      [A]                                                      │
│     dirty:      {A}                                                      │
│     processing: {}                                                       │
│                                                                          │
│   Get() → A:                                                             │
│     queue:      []                                                       │
│     dirty:      {}                                                       │
│     processing: {A}                                                      │
│                                                                          │
│   Add(A):  (re-added while processing)                                   │
│     queue:      []                                                       │
│     dirty:      {A}                                                      │
│     processing: {A}    ← held back; will re-enqueue at Done              │
│                                                                          │
│   Done(A):                                                               │
│     queue:      [A]                                                      │
│     dirty:      {A}                                                      │
│     processing: {}                                                       │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 8.3 Done is mandatory

If you `Get` a key and never `Done` it, the key is *permanently stuck* in the processing set. Any subsequent Add of that key will be silently held back forever, waiting for a Done that will never come.

The canonical pattern is `defer Done(key)`:

```go
func (c *Controller) processNextItem() bool {
    key, shutdown := c.queue.Get()
    if shutdown {
        return false
    }
    defer c.queue.Done(key)                       // ← MANDATORY

    if err := c.reconcile(key.(string)); err != nil {
        c.queue.AddRateLimited(key)               // re-enqueue with backoff
        runtime.HandleError(err)
        return true
    }
    c.queue.Forget(key)                           // reset backoff
    return true
}
```

Forgetting `Done` is the single most common bug after "forgot to wait for cache sync."

### 8.4 RateLimitingInterface and the default rate limiter

The rate-limiting queue wraps a delaying queue and adds backoff. The "rate limiter" interface:

```go
// pkg/util/workqueue/rate_limiting_queue.go
type RateLimiter interface {
    When(item interface{}) time.Duration
    Forget(item interface{})
    NumRequeues(item interface{}) int
}
```

`When(item)` returns how long the caller should wait before processing `item` again. The default rate limiter is `DefaultControllerRateLimiter`:

```go
func DefaultControllerRateLimiter() RateLimiter {
    return NewMaxOfRateLimiter(
        NewItemExponentialFailureRateLimiter(5*time.Millisecond, 1000*time.Second),
        &BucketRateLimiter{
            Limiter: rate.NewLimiter(rate.Limit(10), 100), // 10 qps, 100 burst
        },
    )
}
```

Two limiters combined:

1. **Per-item exponential backoff.** `5ms × 2^(NumRequeues)`, capped at 1000s. So an item that fails repeatedly is backed off 5ms, 10ms, 20ms, ... up to ~17 minutes.
2. **Overall bucket limit.** 10 queries per second, burst 100. This is the *global* rate limit across all keys — protects the apiserver from a stampede when many objects need work at once.

`MaxOfRateLimiter` returns the larger of the two delays for each item. A key with one failure gets `max(5ms, ~0) = 5ms`. A flock of 200 brand-new keys all enqueueing at once gets paced at 10 qps thanks to the bucket.

For controllers that hit external APIs (cloud LBs with strict rate limits, for example), you typically configure a tighter custom bucket — say 1 qps, burst 5.

### 8.5 AddAfter and the delaying queue

The delaying queue layer adds a heap of `(item, ready-time)`:

```go
// pkg/util/workqueue/delaying_queue.go
type delayingType struct {
    Interface  // the underlying simple queue
    clock      clock.Clock
    waitingForAddCh chan *waitFor
    // a single goroutine reads from waitingForAddCh, maintains a heap,
    // sleeps until the next ready-time, then Adds to the underlying queue.
}
```

`AddAfter(key, 30*time.Second)` puts `(key, now+30s)` in the heap. The delaying goroutine wakes up at 30s and calls `Add(key)`, which goes through the normal dedupe path. If `Add(key)` was called immediately during those 30s, the immediate Add wins and the delayed Add is a no-op when it lands.

This is what powers `Result{RequeueAfter: ...}` in controller-runtime.

---

## 9. The Full Reconcile-Loop Scaffolding

We now have all the pieces. Here is an end-to-end, idiomatic, working client-go controller in about 80 lines of Go. It watches Foo CRs (a hypothetical custom resource) and ensures each has a corresponding ConfigMap.

```go
package main

import (
    "context"
    "fmt"
    "time"

    corev1 "k8s.io/api/core/v1"
    apierrors "k8s.io/apimachinery/pkg/api/errors"
    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
    "k8s.io/apimachinery/pkg/util/runtime"
    "k8s.io/apimachinery/pkg/util/wait"
    "k8s.io/client-go/informers"
    "k8s.io/client-go/kubernetes"
    corev1listers "k8s.io/client-go/listers/core/v1"
    "k8s.io/client-go/tools/cache"
    "k8s.io/client-go/tools/clientcmd"
    "k8s.io/client-go/util/workqueue"
)

type Controller struct {
    kube      kubernetes.Interface
    cmLister  corev1listers.ConfigMapLister
    cmSynced  cache.InformerSynced
    queue     workqueue.RateLimitingInterface
}

func NewController(kube kubernetes.Interface, factory informers.SharedInformerFactory) *Controller {
    cmInformer := factory.Core().V1().ConfigMaps()
    c := &Controller{
        kube:     kube,
        cmLister: cmInformer.Lister(),
        cmSynced: cmInformer.Informer().HasSynced,
        queue:    workqueue.NewNamedRateLimitingQueue(workqueue.DefaultControllerRateLimiter(), "configmaps"),
    }
    cmInformer.Informer().AddEventHandler(cache.ResourceEventHandlerFuncs{
        AddFunc:    c.enqueue,
        UpdateFunc: func(_, obj interface{}) { c.enqueue(obj) },
        DeleteFunc: c.enqueue,
    })
    return c
}

func (c *Controller) enqueue(obj interface{}) {
    key, err := cache.DeletionHandlingMetaNamespaceKeyFunc(obj)
    if err != nil {
        runtime.HandleError(err)
        return
    }
    c.queue.Add(key)
}

func (c *Controller) Run(ctx context.Context, workers int) {
    defer runtime.HandleCrash()
    defer c.queue.ShutDown()

    if !cache.WaitForCacheSync(ctx.Done(), c.cmSynced) {
        return
    }
    for i := 0; i < workers; i++ {
        go wait.UntilWithContext(ctx, c.runWorker, time.Second)
    }
    <-ctx.Done()
}

func (c *Controller) runWorker(ctx context.Context) {
    for c.processNext(ctx) {
    }
}

func (c *Controller) processNext(ctx context.Context) bool {
    item, shutdown := c.queue.Get()
    if shutdown {
        return false
    }
    defer c.queue.Done(item)

    key := item.(string)
    if err := c.reconcile(ctx, key); err != nil {
        c.queue.AddRateLimited(key)
        runtime.HandleError(fmt.Errorf("reconcile %q: %w", key, err))
        return true
    }
    c.queue.Forget(key)
    return true
}

func (c *Controller) reconcile(ctx context.Context, key string) error {
    ns, name, err := cache.SplitMetaNamespaceKey(key)
    if err != nil {
        return err
    }
    cm, err := c.cmLister.ConfigMaps(ns).Get(name)
    if apierrors.IsNotFound(err) {
        // Object gone; clean up external state if any. Nothing to do here.
        return nil
    }
    if err != nil {
        return err
    }
    // Level-triggered work: ensure cm has an annotation.
    if cm.Annotations["seen-by/example-controller"] == "true" {
        return nil
    }
    cmCopy := cm.DeepCopy()
    if cmCopy.Annotations == nil {
        cmCopy.Annotations = map[string]string{}
    }
    cmCopy.Annotations["seen-by/example-controller"] = "true"
    _, err = c.kube.CoreV1().ConfigMaps(ns).Update(ctx, cmCopy, metav1.UpdateOptions{})
    return err
}

func main() {
    cfg, _ := clientcmd.BuildConfigFromFlags("", "/home/me/.kube/config")
    kube := kubernetes.NewForConfigOrDie(cfg)
    factory := informers.NewSharedInformerFactory(kube, 10*time.Minute)
    ctrl := NewController(kube, factory)
    ctx := context.Background()
    factory.Start(ctx.Done())
    ctrl.Run(ctx, 2)
}
```

That is the entire pattern. Every controller in `kubernetes/kubernetes` (the upstream repo) follows this exact shape, with variations only in: number of informers, indexers, OwnerRef handling, leader election, and the reconcile body.

The control flow in pictures:

```
   factory.Start ─► informer.Run ─► Reflector ─► DeltaFIFO ─► processDeltas
                                                                    │
                                                                    ▼
                                                              [Indexer]
                                                                    │
                                                                    ▼
                                                          [EventHandler:
                                                           enqueue(key)]
                                                                    │
                                                                    ▼
                                                              [Workqueue]
                                                                    │
                                                                    ▼
                                              ┌─────── runWorker × N ───────┐
                                              │                             │
                                              │   key = queue.Get()         │
                                              │   defer queue.Done(key)     │
                                              │                             │
                                              │   obj = lister.Get(key)     │
                                              │   reconcile(obj)            │
                                              │                             │
                                              │   on err: AddRateLimited    │
                                              │   on ok:  Forget            │
                                              └─────────────────────────────┘
```

---

## 10. Leader Election

Most controllers run as a Deployment with `replicas: 2` or `replicas: 3` for high availability. But only *one* replica may reconcile at a time — otherwise two replicas race on every object, each writing slightly different state. Leader election picks the winner.

Code: `staging/src/k8s.io/client-go/tools/leaderelection/`.

### 10.1 The Lease object

The mechanism is a `coordination.k8s.io/v1 Lease`:

```yaml
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata:
  name: my-controller
  namespace: kube-system
spec:
  holderIdentity: "my-controller-7d9c4-xyz"          # pod name or unique ID
  leaseDurationSeconds: 15                            # how long the lease is valid
  acquireTime: "2026-05-23T10:00:00Z"
  renewTime:   "2026-05-23T10:00:08Z"
  leaseTransitions: 47
```

The contract: the holder's lease is valid until `renewTime + leaseDurationSeconds`. Every other candidate must wait until that point passes before attempting to claim the lease. The holder must renew before expiry to retain the lease.

### 10.2 RunOrDie pattern

```go
import (
    "k8s.io/client-go/tools/leaderelection"
    "k8s.io/client-go/tools/leaderelection/resourcelock"
    coordinationv1 "k8s.io/api/coordination/v1"  // implied
)

func main() {
    cfg, _ := rest.InClusterConfig()
    kube := kubernetes.NewForConfigOrDie(cfg)

    podName := os.Getenv("POD_NAME")  // from downward API
    lock := &resourcelock.LeaseLock{
        LeaseMeta: metav1.ObjectMeta{
            Name:      "my-controller",
            Namespace: "kube-system",
        },
        Client: kube.CoordinationV1(),
        LockConfig: resourcelock.ResourceLockConfig{
            Identity: podName,
        },
    }

    leaderelection.RunOrDie(context.Background(), leaderelection.LeaderElectionConfig{
        Lock:            lock,
        ReleaseOnCancel: true,
        LeaseDuration:   15 * time.Second,
        RenewDeadline:   10 * time.Second,
        RetryPeriod:     2 * time.Second,
        Callbacks: leaderelection.LeaderCallbacks{
            OnStartedLeading: func(ctx context.Context) {
                // We won. Start all controllers.
                runControllers(ctx)
            },
            OnStoppedLeading: func() {
                // We lost the lease. The process should exit.
                // RunOrDie will kill it after this returns.
                klog.Fatal("lost leadership; exiting")
            },
            OnNewLeader: func(identity string) {
                if identity == podName {
                    return
                }
                klog.Infof("new leader elected: %s", identity)
            },
        },
    })
}
```

### 10.3 The three timing parameters

The defaults are `LeaseDuration=15s`, `RenewDeadline=10s`, `RetryPeriod=2s`. The invariant: `LeaseDuration > RenewDeadline > RetryPeriod`. Why these values?

- **LeaseDuration (15s)**: how long a non-leader must wait after the lease's `renewTime` before attempting to take over. If the current leader is silent for 15 seconds, candidates assume it's dead.
- **RenewDeadline (10s)**: the leader gives up if it cannot renew within this window. The constraint is `RenewDeadline < LeaseDuration` so that if the leader is partitioned from the apiserver, *it knows it has lost* before *any other candidate would conclude the lease is expired*. This prevents the leader from continuing to act believing it is still leader.
- **RetryPeriod (2s)**: how often the leader tries to renew, and how often non-leaders poll to see if the lease is free. Smaller values = faster failover but more apiserver load.

```
   Time:   0       2       4       6       8      10      12      14      15
           │       │       │       │       │       │       │       │       │
   Lease   ▼       ▼       ▼       ▼       ▼       ▼       ▼       ▼       ▼
   Leader: RENEW   RENEW   RENEW   RENEW   RENEW   ─?─    ─?─    ─?─    EXPIRED
            ↑       ↑       ↑       ↑       ↑       ↑
            └ RetryPeriod = 2s renews
                                           │       │
                                           └ RenewDeadline = 10s; leader gives up here
                                                                            ▲
                                                                            └ LeaseDuration = 15s; non-leaders take over here
```

The gap: between RenewDeadline (10s) and LeaseDuration (15s), the leader has stopped trying to renew and stopped acting, but no other candidate has yet tried to claim. This 5-second buffer is the safety zone — it ensures that no two pods both believe they hold the lease simultaneously, *assuming clocks are synchronized within the buffer*.

### 10.4 The fencing-token gap

Leases do **not** provide fencing tokens (in the Lamport / Kleppmann sense). A fencing token would be a monotonically increasing integer attached to every write the leader does, so that the apiserver could reject writes from "old" leaders. Lease-based leader election in client-go offers no such guarantee.

What this means in practice: there is always a brief moment, after a network partition heals, when the old leader has not yet noticed it lost the lease and the new leader is already acting. During this overlap, *two reconcilers can run at once*.

Defense: every write should be guarded by optimistic concurrency on `resourceVersion`. When the new leader writes, it bumps the resourceVersion; when the old leader (with its now-stale view) tries to write, it gets a 409 Conflict. This is automatic for `Update` and explicit for `Patch` with `resourceVersion` preconditions.

```
   Time:   T0          T1          T2
                       ▲           ▲
                       │           │
                       └ partition │
                                   └ partition heals, old leader still
                                     thinks it's leader for ~5 seconds
   Old leader: ─── acts ─── acts ─── tries to write ─── 409 Conflict
   New leader:        ─── starts acting ─── writes succeed (RV bumped)
```

The lesson: leader election prevents the *common case* of dual reconciliation. Optimistic concurrency on resourceVersion catches the *edge case* of dual writes during failover. Both are required for correctness.

### 10.5 Why every controller-manager-style binary needs leader election

Run `replicas: 2` without leader election and you have:

- Two informers per GVR (instead of one) → 2× apiserver load.
- Both replicas writing → race conditions, conflicting updates, wasted work.
- Status fields flipping back and forth as each replica reconciles independently.

Leader election is the *single switch* that makes a controller HA. Without it, replicas are not "HA," they are "two controllers fighting."

The only kind of controller that shouldn't use leader election is one where each replica is intentionally responsible for a *different* slice of work — e.g., the kubelet, where each instance reconciles only the pods bound to its own node. Then sharding replaces leader election.

---

## 11. controller-runtime Overlay

`sigs.k8s.io/controller-runtime` is the de facto framework for new controllers (built-ins are still hand-rolled with client-go for historical reasons). It wraps client-go in a more opinionated API that handles the boilerplate from sections 4–10 automatically.

### 11.1 The Manager

The `Manager` is the top-level container. One Manager per process. It owns:

- A shared **Cache** (one informer per GVR, with global label/field/namespace scoping).
- A **Client** (read-through-cache for Get/List of cached types; direct apiserver for everything else).
- Leader election (built in; off by default).
- A metrics server (Prometheus, default port 8080).
- A health probe server (port 8081).
- The `Start(ctx)` lifecycle that starts informers, waits for cache sync, starts controllers, then blocks until ctx is canceled.

```go
import (
    ctrl "sigs.k8s.io/controller-runtime"
    "sigs.k8s.io/controller-runtime/pkg/manager"
    "sigs.k8s.io/controller-runtime/pkg/healthz"
)

func main() {
    mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), manager.Options{
        Scheme:                 scheme,
        Metrics:                server.Options{BindAddress: ":8080"},
        HealthProbeBindAddress: ":8081",
        LeaderElection:         true,
        LeaderElectionID:       "my-operator.example.com",
    })
    if err != nil { os.Exit(1) }

    if err := (&MyReconciler{Client: mgr.GetClient()}).SetupWithManager(mgr); err != nil {
        os.Exit(1)
    }

    mgr.AddHealthzCheck("ping", healthz.Ping)
    mgr.AddReadyzCheck("ping", healthz.Ping)

    if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
        os.Exit(1)
    }
}
```

### 11.2 The Reconciler interface

```go
// pkg/reconcile/reconcile.go
type Reconciler interface {
    Reconcile(ctx context.Context, req Request) (Result, error)
}

type Request struct {
    NamespacedName types.NamespacedName  // ns + name
}

type Result struct {
    Requeue      bool
    RequeueAfter time.Duration
}
```

That is the entire contract. Your reconciler implements this one method:

```go
type MyReconciler struct {
    client.Client
    Scheme *runtime.Scheme
}

func (r *MyReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    log := ctrl.LoggerFrom(ctx)

    var obj myv1.MyKind
    if err := r.Get(ctx, req.NamespacedName, &obj); err != nil {
        if apierrors.IsNotFound(err) {
            log.Info("object gone")
            return ctrl.Result{}, nil
        }
        return ctrl.Result{}, err
    }

    if !obj.DeletionTimestamp.IsZero() {
        return r.handleDelete(ctx, &obj)
    }

    return r.handleCreateOrUpdate(ctx, &obj)
}
```

### 11.3 The Client

`mgr.GetClient()` returns a `client.Client` that reads through the cache and writes through the apiserver. Reads of cached types (anything the controller has `For`/`Owns`/`Watches` for) come from the local cache. Reads of un-cached types fall through to the apiserver.

```go
type Client interface {
    Reader  // Get, List
    Writer  // Create, Delete, Update, Patch, DeleteAllOf
    StatusClient  // Status()  returns a SubResourceWriter
    SubResourceClient(subResource string) SubResourceClient
    Scheme() *runtime.Scheme
    RESTMapper() meta.RESTMapper
}
```

For situations where you must read *uncached* (read-your-writes, watching un-cached types), use `mgr.GetAPIReader()`:

```go
direct := mgr.GetAPIReader()
err := direct.Get(ctx, key, &obj)   // hits apiserver, no cache
```

### 11.4 The Builder

```go
func (r *MyReconciler) SetupWithManager(mgr ctrl.Manager) error {
    return ctrl.NewControllerManagedBy(mgr).
        For(&myv1.MyKind{}).
        Owns(&appsv1.Deployment{}).
        Owns(&corev1.Service{}).
        Watches(
            &corev1.Secret{},
            handler.EnqueueRequestsFromMapFunc(r.findKindsUsingSecret),
        ).
        WithEventFilter(predicate.GenerationChangedPredicate{}).
        WithOptions(controller.Options{MaxConcurrentReconciles: 5}).
        Complete(r)
}
```

What this does:

- `For(&MyKind{})`: the primary type. The controller watches it; events enqueue `request{ns,name}`.
- `Owns(&Deployment{})`: the controller watches Deployments and enqueues *their owner* (looked up via `metadata.ownerReferences`). So if a child Deployment is deleted, we reconcile the parent.
- `Watches(...)`: watch a third type with a custom enqueue mapping. Used for the "watch X, reconcile Y" pattern (next section).
- `WithEventFilter(...)`: a predicate applied to every watch event before it triggers enqueue.
- `WithOptions(...)`: controller-level options like worker count.
- `Complete(r)`: build and register the controller.

`MaxConcurrentReconciles` is the worker count. Default is 1. For most controllers, 1 is correct because the limiting factor is the apiserver, not your CPU. Bump it only if you have many independent objects and a clear bottleneck.

### 11.5 The internal layering

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │  Manager                                                            │
   │  ┌───────────────────────────────────────────────────────────────┐  │
   │  │  Cache (one informer per GVR + scope)                         │  │
   │  │                                                               │  │
   │  │  Pod-informer    Secret-informer    MyKind-informer  ...      │  │
   │  └───────────────┬────────────┬─────────────────┬─────────────────┘  │
   │                  │            │                 │                   │
   │  ┌───────────────▼────────────▼─────────────────▼─────────────────┐ │
   │  │  Cache-backed Client  (Read: cache, Write: apiserver)          │ │
   │  └───────────────────────────┬────────────────────────────────────┘ │
   │                              │                                       │
   │  ┌───────────────────────────▼────────────────────────────────────┐ │
   │  │  Controller(s)                                                 │ │
   │  │                                                                │ │
   │  │   Controller "mykind"                                          │ │
   │  │     watches:  MyKind (For), Deployment (Owns), Secret (Watches)│ │
   │  │     workqueue with N workers                                   │ │
   │  │     Reconcile()                                                │ │
   │  └────────────────────────────────────────────────────────────────┘ │
   │                                                                     │
   │  ┌────────────────────────────────────────────────────────────────┐ │
   │  │  Leader Election (Lease) + Metrics (Prom) + Health probes      │ │
   │  └────────────────────────────────────────────────────────────────┘ │
   └─────────────────────────────────────────────────────────────────────┘
```

---

## 12. Owns, Watches, and Custom Enqueue Mappings

The Builder offers three ways to wire up watches. The distinctions matter.

### 12.1 For: the primary type

```go
.For(&MyKind{})
```

This sets up the main watch. Every Add/Update/Delete on a `MyKind` object enqueues `{namespace, name}` as a `Request`. There can be only one `For` per controller.

### 12.2 Owns: children with an OwnerReference

```go
.Owns(&appsv1.Deployment{})
.Owns(&corev1.Service{})
```

This sets up a watch on the *child* type and, when an event fires, walks the `metadata.ownerReferences` to find the owning `MyKind`, then enqueues that owner's key.

For the lookup to work, the controller must be setting OwnerReferences correctly when it creates children:

```go
dep := &appsv1.Deployment{...}
if err := ctrl.SetControllerReference(&obj, dep, r.Scheme); err != nil {
    return ctrl.Result{}, err
}
err := r.Create(ctx, dep)
```

`SetControllerReference` sets `controller=true` and `blockOwnerDeletion=true` on the new OwnerReference, marking *this* OwnerReference as the canonical parent. There can be at most one controller-OwnerReference per object.

The Owns watch only fires for events where the child has an OwnerReference back to your kind. Children created by other controllers, or with no OwnerReference, are invisible.

### 12.3 Watches: arbitrary cross-type triggers

```go
.Watches(
    &corev1.Secret{},
    handler.EnqueueRequestsFromMapFunc(r.findKindsUsingSecret),
)
```

This is for cases where there's no OwnerReference relationship. The classic example: your CR references a Secret by name; when the Secret changes, you want to reconcile the CR.

The mapping function:

```go
func (r *MyReconciler) findKindsUsingSecret(ctx context.Context, obj client.Object) []ctrl.Request {
    secret, ok := obj.(*corev1.Secret)
    if !ok {
        return nil
    }
    var list myv1.MyKindList
    if err := r.List(ctx, &list, client.InNamespace(secret.Namespace)); err != nil {
        return nil
    }
    var requests []ctrl.Request
    for _, k := range list.Items {
        if k.Spec.SecretRef == secret.Name {
            requests = append(requests, ctrl.Request{NamespacedName: types.NamespacedName{
                Namespace: k.Namespace, Name: k.Name,
            }})
        }
    }
    return requests
}
```

A few important things:

- `findKindsUsingSecret` is called on the informer's event-processing goroutine. It must be fast. If you find yourself doing heavy work, either add an indexer (so the List becomes O(1)) or push the logic into Reconcile.
- The List uses the *cached* client, so it's an in-memory scan. With an indexer keyed by `spec.secretRef`, it would be O(1).
- Returning `[]ctrl.Request{}` (or nil) means "this event triggers no reconciles," which is exactly the right answer when no MyKind references this Secret.

### 12.4 The "watch X, reconcile Y" pattern

The above pattern generalizes. Common cases:

- Watch ConfigMaps, reconcile owning Deployments (rollout when config changes).
- Watch Secrets (e.g., TLS certs), reconcile Ingresses or Services that use them.
- Watch Nodes, reconcile Pods affected by Node taints changing.
- Watch every Pod in a Namespace, reconcile a single "namespace summary" object.

```
                    ┌──────────────┐
                    │  CHANGE: X   │
                    └──────┬───────┘
                           │
              ┌────────────▼────────────┐
              │  EnqueueRequestsFromMap │  ← runs on informer goroutine
              │   findOwnersOf(X)       │
              └────────────┬────────────┘
                           │ []Request
                           ▼
                    ┌──────────────┐
                    │   Workqueue  │
                    └──────┬───────┘
                           ▼
                    Reconcile(Y₁), Reconcile(Y₂), ...
```

### 12.5 Predicates: filtering before enqueue

You can attach a predicate to any watch source:

```go
.For(&MyKind{}, builder.WithPredicates(predicate.GenerationChangedPredicate{}))
.Owns(&appsv1.Deployment{}, builder.WithPredicates(predicate.ResourceVersionChangedPredicate{}))
```

The predicate runs on the event before it triggers the enqueue mapping. Predicates are CPU-savers — they prevent the workqueue from growing during noisy update streams. We cover them in detail in section 19.

---

## 13. Status vs Spec Discipline

This is the discipline question that separates a working controller from a correct one. The rule: **spec is the user's, status is the controller's**, and they live in different planes.

### 13.1 The status subresource

For CRDs, you enable the status subresource explicitly:

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: mykinds.example.com
spec:
  group: example.com
  names: { kind: MyKind, plural: mykinds }
  scope: Namespaced
  versions:
  - name: v1
    served: true
    storage: true
    subresources:
      status: {}                  # ← enables /status subresource
    schema:
      openAPIV3Schema:
        type: object
        properties:
          spec:    { type: object, ... }
          status:  { type: object, ... }
```

This single line splits writes into two surfaces:

- `PATCH /apis/example.com/v1/.../mykinds/foo` modifies only spec (and metadata). It ignores status entirely; if you send a status, the apiserver discards it.
- `PATCH /apis/example.com/v1/.../mykinds/foo/status` modifies only status. It ignores spec; if you send a spec, the apiserver discards it.

The benefit: a controller patching `/status` cannot accidentally clobber `spec`, and a user patching `spec` cannot accidentally clobber `status`. The two writers can't fight.

### 13.2 The status client

In controller-runtime, status writes go through `client.Status()`:

```go
// WRONG (writes to /, not /status):
r.Update(ctx, obj)

// RIGHT (writes to /status):
r.Status().Update(ctx, obj)

// Equivalent with Patch:
r.Status().Patch(ctx, obj, client.MergeFrom(original))
```

If you call `r.Update(ctx, obj)` on a CRD that has the status subresource enabled, the apiserver will silently *discard* your status changes. The Update succeeds and changes nothing in status. This is one of the most confusing bugs in controller code — the call returns nil, no error is reported, but status is unchanged.

### 13.3 Generation behavior

A key feature of the status subresource: a patch to status does **not** bump `metadata.generation`. Generation is intended to track *spec* changes only. This matters for the ObservedGeneration pattern (section 15).

```
   /spec patch    → generation++   resourceVersion++
   /status patch  → generation     resourceVersion++   (gen unchanged)
   metadata patch → generation     resourceVersion++   (gen unchanged)
```

This is also why predicates often look at generation: a status-only update from the controller itself does not change generation, so a `GenerationChangedPredicate` correctly filters it out and prevents the controller from being re-enqueued by its own status writes.

### 13.4 The discipline

- **Users / GitOps** only ever write spec. Never mutate status from GitOps; if you do, you'll fight the controller forever.
- **Controllers** only ever write status (for objects they own). Mutating spec from a controller is a sin (section 25). The exception is when *one* controller is the spec-owner (e.g., HPA owns `spec.replicas` on a Deployment) and uses Server-Side Apply (section 18) with a stable fieldManager.
- **Defaulters / admission** can mutate spec on creation but never after, except via admission policies that the user has explicitly opted into.

Most "infinite reconcile loop" bugs are caused by violating this discipline.

---

## 14. The Conditions Pattern

`metav1.Condition` is the standard way for controllers to surface state:

```go
type Condition struct {
    Type               string               // "Ready", "Available", "Progressing", ...
    Status             ConditionStatus      // True | False | Unknown
    ObservedGeneration int64                // generation at the time of evaluation
    LastTransitionTime metav1.Time          // when Status last changed
    Reason             string               // CamelCase machine-readable cause
    Message            string               // human-readable
}
```

A controller manages a `[]Condition` slice in status. The convention: each `Type` appears at most once. Updating a condition means either appending a new entry (if the type isn't present) or updating an existing one (preserving `LastTransitionTime` if `Status` didn't change).

The helper:

```go
// k8s.io/apimachinery/pkg/api/meta/conditions.go
meta.SetStatusCondition(&obj.Status.Conditions, metav1.Condition{
    Type:               "Ready",
    Status:             metav1.ConditionTrue,
    ObservedGeneration: obj.Generation,
    Reason:             "ReconcileSucceeded",
    Message:            "all subresources are healthy",
})
```

`SetStatusCondition`:

- Finds an existing condition with the same `Type`.
- If absent, appends.
- If present and `Status` matches, updates `Reason`/`Message`/`ObservedGeneration` but preserves `LastTransitionTime`.
- If present and `Status` differs, sets `LastTransitionTime = now()`.

### 14.1 Standard condition types

The community has converged on a handful of standard condition types:

- `Ready`: the object is fully functional. The single most important condition.
- `Available`: the object can serve traffic (for workloads with a distinction between "ready to be promoted" and "ready to serve").
- `Progressing`: there is in-flight work (rolling update, provisioning).
- `Degraded`: still serving but in a reduced state.
- `Reconciling`: actively being reconciled (rarely used; mostly redundant with Progressing).

CRD authors should publish their accepted condition types in API docs. Consumers should treat unknown types as informational.

### 14.2 Reasons

`Reason` is intentionally machine-readable: CamelCase, no whitespace, suitable for grouping in dashboards. Examples:

- `Reason: "Reconciling"`
- `Reason: "WaitingForCert"`
- `Reason: "QuotaExceeded"`
- `Reason: "DependencyMissing"`

Avoid free-text reasons; that's what `Message` is for.

### 14.3 Patterns

```go
// Surface a transient wait:
meta.SetStatusCondition(&obj.Status.Conditions, metav1.Condition{
    Type:               "Ready",
    Status:             metav1.ConditionFalse,
    Reason:             "WaitingForBackingStore",
    Message:            fmt.Sprintf("backing PVC %q not yet bound", pvcName),
    ObservedGeneration: obj.Generation,
})

// Surface success:
meta.SetStatusCondition(&obj.Status.Conditions, metav1.Condition{
    Type:               "Ready",
    Status:             metav1.ConditionTrue,
    Reason:             "ReconcileSucceeded",
    ObservedGeneration: obj.Generation,
})

// Surface a permanent error (user must intervene):
meta.SetStatusCondition(&obj.Status.Conditions, metav1.Condition{
    Type:               "Ready",
    Status:             metav1.ConditionFalse,
    Reason:             "InvalidConfiguration",
    Message:            "spec.replicas must be > 0",
    ObservedGeneration: obj.Generation,
})
```

Tools like `kubectl wait --for=condition=Ready` and ArgoCD's health checks consume conditions. Surface them faithfully.

---

## 15. Generation, ResourceVersion, and UID

These three metadata fields confuse everyone the first time. The distinctions are sharp.

### 15.1 The fields

- `metadata.uid`: **immutable identity**. Set by the apiserver at creation. Never changes. If an object is deleted and recreated with the same name, the new one has a new UID. UIDs are used to detect "is this the same object as before, or a recreation?"
- `metadata.resourceVersion`: **modification token**. Set by the apiserver on every write. Opaque string (interpreted as an integer in etcd terms but officially opaque). Used by Update/Patch for optimistic concurrency.
- `metadata.generation`: **spec-change counter**. Starts at 1. Incremented by the apiserver whenever a write changes `spec`. *Not* incremented by writes to `status` or `metadata`. Used by the ObservedGeneration pattern.

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │  Field            Bumped by spec  Bumped by status  Set on create    │
   ├──────────────────────────────────────────────────────────────────────┤
   │  uid              no              no                yes (immutable)  │
   │  resourceVersion  yes             yes               yes              │
   │  generation       yes             no                yes (= 1)        │
   └──────────────────────────────────────────────────────────────────────┘
```

### 15.2 The ObservedGeneration pattern

A controller stores `status.observedGeneration` to record "the last `metadata.generation` I have fully reconciled."

```go
type MyKindStatus struct {
    ObservedGeneration int64               `json:"observedGeneration,omitempty"`
    Conditions         []metav1.Condition  `json:"conditions,omitempty"`
    // ...
}

func (r *MyReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    var obj myv1.MyKind
    if err := r.Get(ctx, req.NamespacedName, &obj); err != nil {
        return ctrl.Result{}, client.IgnoreNotFound(err)
    }
    if obj.Status.ObservedGeneration == obj.Generation {
        // No spec change since we last reconciled. We still re-check
        // observed-world state in case it changed, but we know the user
        // hasn't asked for anything new.
    }
    // ... do work, possibly multi-reconcile ...

    // At the END of successful reconciliation:
    obj.Status.ObservedGeneration = obj.Generation
    meta.SetStatusCondition(&obj.Status.Conditions, metav1.Condition{
        Type: "Ready", Status: metav1.ConditionTrue,
        ObservedGeneration: obj.Generation,
        Reason: "ReconcileSucceeded",
    })
    return ctrl.Result{}, r.Status().Update(ctx, &obj)
}
```

External observers can ask "is the controller caught up?" by comparing `spec.generation` to `status.observedGeneration`:

- `observedGeneration == generation`: the controller has fully processed the latest spec.
- `observedGeneration < generation`: the controller is still catching up.
- `observedGeneration > generation`: impossible; would indicate a bug.

GitOps engines and `kubectl wait` use this to decide "has the cluster converged?"

**Critical mistake**: setting `observedGeneration = generation` *before* finishing the work. The whole point of the field is "I have done what spec asks." Setting it early lies to the world. The correct pattern is: set ObservedGeneration only when you set Conditions to `Ready=True`, after all work is done.

### 15.3 ResourceVersion in writes

`resourceVersion` is the optimistic concurrency token. The Update and Patch verbs accept (and Update *requires*) the current RV:

```go
// Update sends the entire object back; apiserver checks that the
// RV in the request matches the stored RV. If not, returns 409 Conflict.
err := r.Update(ctx, obj)

// Strategic merge / JSON merge / JSON patch: the controller-runtime
// helpers send the request RV in the metadata. If you omit it
// (Patch without RV precondition), the apiserver applies the patch
// to whatever RV is current — last write wins.
err := r.Patch(ctx, obj, client.MergeFrom(original))
```

For status patches, the same applies. The standard pattern is to keep a copy of the original object before mutating it locally, then `MergeFrom(original)` to compute the diff:

```go
original := obj.DeepCopy()
// ... mutate obj ...
err := r.Status().Patch(ctx, obj, client.MergeFrom(original))
```

### 15.4 UID for ownership and tombstones

UIDs prevent the "ABA problem" in OwnerReferences and watch tombstones.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: my-pod
  ownerReferences:
  - apiVersion: apps/v1
    kind: ReplicaSet
    name: my-rs-7d9c4
    uid: 9b3f...                 # the UID of the RS that owns this pod
    controller: true
    blockOwnerDeletion: true
```

If the RS is deleted and a new one is created with the same name, the new RS has a new UID. The Pod's OwnerReference still points to the old UID and is now dangling — the GC controller will see this and delete the Pod (since its owner no longer exists).

This is why you must use `ctrl.SetControllerReference` rather than hand-rolling OwnerReferences: it sets UID correctly, and it refuses to set a second controller-OwnerReference (only one is allowed).

---

## 16. Finalizers: The Deletion Guard

Some objects require cleanup of external state when they're deleted: a CR that represents a cloud LoadBalancer must delete the LB; a CR that represents a database must drop the database. The apiserver alone cannot do this — it only knows about etcd objects.

Finalizers are the hook.

### 16.1 The lifecycle

When a user issues `kubectl delete mykind foo`:

1. The apiserver sets `metadata.deletionTimestamp` and `metadata.deletionGracePeriodSeconds`.
2. The apiserver does **not** delete the object from etcd. It now appears in watch streams with `deletionTimestamp != nil`.
3. Each controller with a registered finalizer sees the deletion and performs cleanup of external state.
4. When done, each controller removes its finalizer string from `metadata.finalizers`.
5. When the `finalizers` slice is empty, the apiserver actually removes the object from etcd. A watch Delete event fires.

```
   user kubectl delete X                  
        │
        ▼
   apiserver: set X.deletionTimestamp = now              ┐
              do NOT delete from etcd                    │
              fire Update watch event                    │
                                                         │
   controller A sees deletionTimestamp != nil            │
     cleanup external state                              │  cleanup phase
     remove finalizer "a.example.com/cleanup"            │  (can be many
     PATCH /apis/...                                     │   reconciles)
                                                         │
   controller B sees deletionTimestamp != nil            │
     cleanup external state                              │
     remove finalizer "b.example.com/cleanup"            ┘
        │
        ▼
   apiserver: finalizers empty?                         
              YES → really delete from etcd
              fire Delete watch event
```

### 16.2 Registering a finalizer

```go
const myFinalizer = "mycontroller.example.com/cleanup"

func (r *MyReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    var obj myv1.MyKind
    if err := r.Get(ctx, req.NamespacedName, &obj); err != nil {
        return ctrl.Result{}, client.IgnoreNotFound(err)
    }

    // Case 1: object is being deleted.
    if !obj.DeletionTimestamp.IsZero() {
        if controllerutil.ContainsFinalizer(&obj, myFinalizer) {
            if err := r.cleanupExternal(ctx, &obj); err != nil {
                return ctrl.Result{}, err
            }
            controllerutil.RemoveFinalizer(&obj, myFinalizer)
            if err := r.Update(ctx, &obj); err != nil {
                return ctrl.Result{}, err
            }
        }
        return ctrl.Result{}, nil
    }

    // Case 2: object is alive. Register finalizer if not present.
    if !controllerutil.ContainsFinalizer(&obj, myFinalizer) {
        controllerutil.AddFinalizer(&obj, myFinalizer)
        if err := r.Update(ctx, &obj); err != nil {
            return ctrl.Result{}, err
        }
        // Will reconcile again after the Update event.
        return ctrl.Result{}, nil
    }

    // Case 3: normal reconcile.
    return r.reconcileAlive(ctx, &obj)
}

func (r *MyReconciler) cleanupExternal(ctx context.Context, obj *myv1.MyKind) error {
    // MUST be idempotent — may be called many times during the
    // deletion phase (each retry after a transient error).
    if err := r.cloudLB.Delete(obj.Status.LBID); err != nil {
        if !isNotFound(err) {
            return err
        }
        // LB already gone; that's fine.
    }
    return nil
}
```

### 16.3 The idempotence requirement

Cleanup may be called many times. If your first attempt to delete the cloud LB succeeded but the controller crashed before removing the finalizer, the next reconcile will try to delete the LB again. The cleanup must handle "already gone" as success, not as error.

```go
// Wrong:
func cleanup(obj *MyKind) error {
    return cloud.DeleteLB(obj.Status.LBID)  // errors if LB already deleted
}

// Right:
func cleanup(obj *MyKind) error {
    err := cloud.DeleteLB(obj.Status.LBID)
    if err == nil || isNotFound(err) {
        return nil
    }
    return err
}
```

### 16.4 Zombie objects

If a controller is uninstalled while one of its objects has its finalizer set, the object becomes a **zombie**: deletionTimestamp is set, finalizers contain a string that no one will ever remove, the object is invisible to most tooling (`kubectl get` shows it with "Terminating" but most users learn to ignore that), and it can never be cleaned up.

The fix:

```bash
kubectl patch mykind foo --type=json \
  -p='[{"op":"remove","path":"/metadata/finalizers"}]'
```

This is a *manual cleanup* you do only when you know the external state is already gone (e.g., you tore down the cloud account). Forgetting finalizers is a top-tier production hazard. Chapter 36 covers the broader garbage-collection model.

### 16.5 Finalizers and PVCs: a real case study

Kubernetes ships finalizers on built-in objects too. PVCs have `kubernetes.io/pvc-protection`: while a Pod references the PVC, the finalizer remains, even if the user deletes the PVC. The PV protection controller removes the finalizer once no Pods reference the PVC. This is what prevents accidentally deleting a PVC while it's mounted.

Similarly, Namespaces have a finalizer (`kubernetes`), which is removed by the namespace controller only after all objects in the namespace have been deleted. This is why deleting a namespace with stuck objects leaves the namespace in "Terminating" state forever.

---

## 17. Optimistic Concurrency in Updates

The apiserver implements optimistic concurrency on `metadata.resourceVersion`. A naive client that just patches without preconditions can clobber other writers.

### 17.1 Update with RV check

```go
// Read current state:
err := r.Get(ctx, key, &obj)         // obj.ResourceVersion = "12345"
obj.Spec.Replicas = 5
err = r.Update(ctx, &obj)            // sends RV=12345 in request

// If another writer updated between Get and Update, the apiserver
// has RV=12346 stored. The Update fails:
//
//   StatusConflict (409):
//     Operation cannot be fulfilled on mykinds.example.com "foo":
//     the object has been modified; please apply your changes to the
//     latest version and try again
```

### 17.2 The RetryOnConflict helper

```go
import "k8s.io/client-go/util/retry"

err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
    var obj myv1.MyKind
    if err := r.Get(ctx, key, &obj); err != nil {
        return err
    }
    obj.Status.Phase = "Ready"
    return r.Status().Update(ctx, &obj)
})
```

`DefaultRetry` is exponential: 10ms, 20ms, 40ms, 80ms, 160ms, max 5 retries.

Critical: the Get **must** be inside the retry loop. If you do:

```go
// WRONG: stale RV reused on retry.
var obj myv1.MyKind
r.Get(ctx, key, &obj)
err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
    obj.Status.Phase = "Ready"
    return r.Status().Update(ctx, &obj)
})
```

Every retry sends the same stale RV and gets the same conflict. The retry helper isn't smart enough to refresh; you have to.

### 17.3 Patch with optimistic RV

`Patch` is by default RV-less: it applies to whatever is current. If you want optimistic checks on Patch, use a precondition:

```go
err := r.Patch(ctx, obj, client.MergeFrom(original),
    &client.PatchOptions{Raw: &metav1.PatchOptions{
        FieldManager: "my-controller",
    }})
```

For `MergeFrom(original)`, the patch payload is the diff between `original` and `obj`. The diff is small; conflicts are rare. But there is no RV check unless you set `Preconditions.UID` or `Preconditions.ResourceVersion` explicitly.

### 17.4 Blind retry is wrong

A common bug:

```go
// WRONG:
for {
    err := r.Update(ctx, &obj)
    if err == nil { break }
    if !apierrors.IsConflict(err) { return err }
    // Sleep and retry — but obj is stale!
    time.Sleep(100 * time.Millisecond)
}
```

This loops forever (or until you give up), because you keep sending the same stale RV. Even worse:

```go
// EVEN MORE WRONG:
for {
    err := r.Update(ctx, &obj)
    if err == nil { break }
    if !apierrors.IsConflict(err) { return err }
    // Re-fetch RV but keep our mutations:
    var current myv1.MyKind
    r.Get(ctx, key, &current)
    obj.ResourceVersion = current.ResourceVersion
    // Retry...
}
```

This sends the *new* RV but with *our* (potentially stale) field values. We may overwrite a concurrent update. The right pattern is to re-fetch the whole object, re-apply our mutation idempotently, and retry. `RetryOnConflict` with the Get inside is the canonical implementation.

---

## 18. Server-Side Apply from Controllers

Server-Side Apply (SSA) is the modern way for a controller to express "I own these fields; here are my desired values; merge with whatever else is on the object."

### 18.1 Why SSA

Two concrete problems with traditional Update/Patch:

1. **Multi-writer fight.** Two controllers both compute the desired Pod spec, each calls `Update`. Whichever runs second overwrites the first. With strategic-merge-patch, the result depends on field-level rules that no one remembers.
2. **Field ownership ambiguity.** When a user runs `kubectl edit` on a Deployment, kubectl sends the full object. The apiserver can't tell which fields the user changed versus which were already there from the controller; the next reconcile overwrites the user's edits.

SSA fixes both by tracking *which writer owns which field*.

### 18.2 The model

Each field of an object has a list of `managedFields` entries:

```yaml
metadata:
  name: my-pod
  managedFields:
  - manager: "kubectl"
    operation: "Apply"
    fieldsType: "FieldsV1"
    fieldsV1:
      f:metadata:
        f:labels:
          f:app: {}
      f:spec:
        f:replicas: {}
  - manager: "my-controller"
    operation: "Apply"
    fieldsType: "FieldsV1"
    fieldsV1:
      f:spec:
        f:template:
          f:spec:
            f:containers:
              k:{"name":"main"}:
                f:image: {}
```

Two managers, each owning a distinct set of fields. Each `Apply` patch lists which fields the writer claims; the apiserver merges into the existing object, *removing* fields the writer used to own but is no longer applying (this is how you "release" ownership).

### 18.3 The Patch call

```go
import (
    "k8s.io/apimachinery/pkg/types"
    "sigs.k8s.io/controller-runtime/pkg/client"
)

desired := &appsv1.Deployment{
    TypeMeta: metav1.TypeMeta{
        APIVersion: "apps/v1", Kind: "Deployment",
    },
    ObjectMeta: metav1.ObjectMeta{
        Namespace: "default", Name: "my-app",
    },
    Spec: appsv1.DeploymentSpec{
        Replicas: ptr.To[int32](3),
        Selector: &metav1.LabelSelector{
            MatchLabels: map[string]string{"app": "my-app"},
        },
        Template: corev1.PodTemplateSpec{ /* ... */ },
    },
}

err := r.Patch(ctx, desired,
    client.Apply,
    client.ForceOwnership,
    client.FieldOwner("my-controller"),
)
```

The `client.Apply` patch type maps to `application/apply-patch+yaml`. The `FieldOwner` is the manager string. `ForceOwnership` is needed when another manager already owns one of the fields you're claiming — without it, the apply fails with a conflict listing the contested fields.

### 18.4 The Result claim discipline

The key SSA rule: **only put the fields you claim in your apply payload.** If you fill in `Spec.Replicas` and leave it set to zero because you don't care, SSA reads that as "I claim Replicas, and my value is 0," and the apiserver will set Replicas to 0 and steal ownership from whoever had it (typically HPA).

This means the typed builder pattern is dangerous for SSA — you have to *omit* fields you don't want to claim, which means using pointer types, omitempty, or unstructured objects.

The recommended approach is to use the **apply configuration** types:

```go
import (
    appsv1apply "k8s.io/client-go/applyconfigurations/apps/v1"
    corev1apply "k8s.io/client-go/applyconfigurations/core/v1"
)

depConfig := appsv1apply.Deployment("my-app", "default").
    WithSpec(appsv1apply.DeploymentSpec().
        WithReplicas(3).
        WithSelector(metav1apply.LabelSelector().
            WithMatchLabels(map[string]string{"app": "my-app"})).
        WithTemplate(corev1apply.PodTemplateSpec().
            WithSpec(corev1apply.PodSpec().
                WithContainers(corev1apply.Container().
                    WithName("main").
                    WithImage("nginx:1.27")))))

_, err := kubeClient.AppsV1().Deployments("default").Apply(ctx, depConfig,
    metav1.ApplyOptions{FieldManager: "my-controller", Force: true})
```

Each `With*` call explicitly claims that field. Anything you don't `With` is not in the payload and is not claimed.

### 18.5 SSA from controller-runtime

controller-runtime supports SSA via the `client.Apply` patch type, but the typed apply configurations aren't always available for CRDs. For CRDs, you can construct an unstructured object containing only the fields you claim:

```go
obj := &unstructured.Unstructured{}
obj.SetGroupVersionKind(schema.GroupVersionKind{
    Group: "example.com", Version: "v1", Kind: "MyKind",
})
obj.SetNamespace("default")
obj.SetName("foo")
unstructured.SetNestedField(obj.Object, "running", "spec", "desiredPhase")

err := r.Patch(ctx, obj, client.Apply,
    client.ForceOwnership, client.FieldOwner("my-controller"))
```

Or use the `kubebuilder`-generated typed apply configs (`pkg/applyconfiguration/...`), which are scaffolded for your CRD types and behave the same as the built-in ones.

### 18.6 Conflicts

If two managers both claim the same field and have different desired values, the second apply gets back a conflict error listing the contested fields:

```
Apply failed with 1 conflict: conflict with "other-controller":
  .spec.replicas
```

You can:

- Refuse: the user/controller must resolve.
- Force (`ForceOwnership`): steal ownership. Use sparingly; this is how you erase a competing manager's claim.

The right behavior depends on the field. For autoscaling, HPA should `Force` on `spec.replicas` and the user's Deployment manifest should *not* claim `replicas`. For most other fields, the user is the ultimate owner and the controller should respect their value.

---

## 19. Predicates: Filtering Events Before They Enqueue

Predicates are CPU-savers. They run inside the informer's event delivery, before any enqueue happens. A predicate decides whether an event should propagate to the workqueue.

```go
type Predicate interface {
    Create(event.CreateEvent) bool
    Update(event.UpdateEvent) bool
    Delete(event.DeleteEvent) bool
    Generic(event.GenericEvent) bool
}
```

### 19.1 GenerationChangedPredicate

The single most important predicate. It returns `false` on Update events where `oldObj.Generation == newObj.Generation`. This filters out status-only writes (which don't bump generation) and metadata-only writes.

```go
.For(&myv1.MyKind{}, builder.WithPredicates(predicate.GenerationChangedPredicate{}))
```

Use this whenever:

- Your controller writes its own status (otherwise its own writes re-enqueue itself, causing endless loops if status writes are not idempotent).
- The primary object's status is updated by other controllers (e.g., a Pod whose status is updated frequently by kubelet — without this predicate, every status change re-enqueues all controllers watching Pods).

When *not* to use it:

- If your controller needs to react to status changes on the watched objects. For example, if you watch Pods and react to Pod readiness, you cannot use GenerationChangedPredicate (Pod status changes don't bump generation; you'd never see them).

A common pattern: GenerationChangedPredicate on `For`, no predicate on `Owns`. Your primary object's spec is what matters; your children's status is what tells you whether your work is done.

### 19.2 ResourceVersionChangedPredicate

Returns `false` if RV is unchanged. Used to filter out resync events (where the cache re-delivers the same object with no actual change).

```go
.For(&MyKind{}, builder.WithPredicates(predicate.ResourceVersionChangedPredicate{}))
```

This is weaker than GenerationChangedPredicate — it still passes through status-only changes. Use it when you can't use GenerationChanged but want to skip pure resyncs.

### 19.3 LabelChangedPredicate, AnnotationChangedPredicate

Return `false` if labels/annotations are unchanged. Useful in controllers that key off label/annotation values:

```go
.For(&corev1.Pod{}, builder.WithPredicates(predicate.LabelChangedPredicate{}))
```

### 19.4 Custom predicates

```go
podHasOurLabel := predicate.Funcs{
    CreateFunc: func(e event.CreateEvent) bool {
        return e.Object.GetLabels()["example.com/managed-by"] == "my-controller"
    },
    UpdateFunc: func(e event.UpdateEvent) bool {
        return e.ObjectNew.GetLabels()["example.com/managed-by"] == "my-controller"
    },
    DeleteFunc: func(e event.DeleteEvent) bool {
        return e.Object.GetLabels()["example.com/managed-by"] == "my-controller"
    },
}

.For(&corev1.Pod{}, builder.WithPredicates(podHasOurLabel))
```

Combine predicates with `predicate.And` / `predicate.Or`:

```go
.For(&corev1.Pod{},
    builder.WithPredicates(
        predicate.And(
            podHasOurLabel,
            predicate.GenerationChangedPredicate{},
        ),
    ),
)
```

### 19.5 Predicates are CPU savers, not correctness fixes

Predicates filter events *before* they enqueue, so they reduce reconcile call volume. But they cannot make an incorrect controller correct — the reconcile function still needs to handle every state. A predicate that filters too aggressively will cause missed reconciles; the level-triggered design means you'll catch up on the next resync (10 hours by default in controller-runtime), but in the meantime your status is stale.

Rule of thumb: use predicates to filter *predictably uninteresting* events. Never use them to encode business logic.

---

## 20. Filters and Scoping

By default, an informer watches *all* objects of a given type in the cluster. For multi-tenant or scoped operators, you can restrict the informer at construction time, saving memory and reducing apiserver fan-out.

### 20.1 Scoping the cache

controller-runtime's Manager accepts `cache.Options`:

```go
mgr, err := ctrl.NewManager(cfg, manager.Options{
    Scheme: scheme,
    Cache: cache.Options{
        DefaultNamespaces: map[string]cache.Config{
            "tenant-a": {},
            "tenant-b": {},
        },
        ByObject: map[client.Object]cache.ByObject{
            &corev1.Secret{}: {
                Label: labels.SelectorFromSet(labels.Set{
                    "example.com/managed": "true",
                }),
            },
            &corev1.ConfigMap{}: {
                Namespaces: map[string]cache.Config{
                    "kube-system": {},
                },
            },
        },
    },
})
```

What this does:

- `DefaultNamespaces`: all watches default to these two namespaces. Watches set up via the builder use this scope.
- `ByObject[Secret]`: Secrets are only watched if they carry `example.com/managed=true`. The label selector is passed to the apiserver via `labelSelector` in the watch.
- `ByObject[ConfigMap]`: ConfigMaps are only watched in `kube-system`.

These constraints are *enforced server-side*: the apiserver filters the watch stream and only sends matching objects. You reduce memory in your process *and* CPU on the apiserver.

### 20.2 Tenant-scoped operators

A common pattern: an operator that should only manage resources in a set of tenant namespaces. Without scoping, the operator would List/Watch every Pod in the cluster, even though it ignores 99% of them.

```go
tenants := strings.Split(os.Getenv("WATCH_NAMESPACES"), ",")
nsConfig := map[string]cache.Config{}
for _, ns := range tenants {
    nsConfig[ns] = cache.Config{}
}

mgr, _ := ctrl.NewManager(cfg, manager.Options{
    Cache: cache.Options{
        DefaultNamespaces: nsConfig,
    },
})
```

For cluster-wide singletons (e.g., a CRD that's cluster-scoped), use `cache.Options.ByObject` to override per-type.

### 20.3 Cluster-scoped vs namespaced controllers

| Property | Cluster-scoped controller | Namespaced controller |
|---|---|---|
| Cache | Full cluster | Restricted by namespace list |
| Memory | High (everything) | Low (just tenant) |
| RBAC | ClusterRole | Role (per namespace) |
| Leader election | One per cluster | One per namespace or one per cluster |
| Object scope it manages | Cluster or namespaced | Namespaced only |

The choice is mostly about blast radius. A cluster-scoped operator that has a bug can affect every workload; a namespaced one affects only its tenant. Hard multi-tenancy designs lean toward namespaced operators (one operator per tenant), at the cost of higher operational overhead.

### 20.4 Field selectors

Some types (Pods, Events, Nodes) support field selectors:

```go
Cache: cache.Options{
    ByObject: map[client.Object]cache.ByObject{
        &corev1.Pod{}: {
            Field: fields.SelectorFromSet(fields.Set{
                "spec.nodeName": os.Getenv("NODE_NAME"),
            }),
        },
    },
},
```

This is how the kubelet's informer is configured: each kubelet only watches Pods bound to its own Node. The apiserver does the filtering; the kubelet never sees pods on other nodes.

Field selector support is *not* universal — only a fixed set of fields per type are indexed on the apiserver. CRDs generally don't support field selectors at all (CRD field selectors are a recent and limited feature).

---

## 21. Multi-Cluster Controllers (preview)

Chapter 26 covers multi-cluster control planes in depth. Here is the controller-runtime layer that makes it possible.

### 21.1 The problem

A multi-cluster controller manages objects across N clusters from a single process. Example: a "namespace sync" controller that creates the same Namespace in every cluster registered with a parent management cluster.

The naive approach (one Manager per cluster, all in one process) has problems:

- Leader election is per-cluster — N leases.
- Cache memory scales with N × objects/cluster.
- Reconcile must know which cluster a request came from.

### 21.2 mc-runtime

`sigs.k8s.io/multicluster-runtime` extends controller-runtime with a cluster-per-cache pattern:

```go
mgr, _ := mcmanager.New(cfg, provider, manager.Options{...})
// provider supplies a Cluster object per registered remote cluster.

(&MyReconciler{}).SetupWithManager(mgr)

// Reconcile(ctx, req) now has req.ClusterName populated;
// the manager routes events from each cluster's informer to the
// same reconciler with the cluster identifier in scope.
```

A `mcreconcile.Request` extends the standard Request with `ClusterName`:

```go
type Request struct {
    ctrl.Request
    ClusterName string
}
```

Inside Reconcile, you obtain the per-cluster client from the manager:

```go
func (r *MyReconciler) Reconcile(ctx context.Context, req mcreconcile.Request) (ctrl.Result, error) {
    cluster, err := r.mgr.GetCluster(ctx, req.ClusterName)
    if err != nil { return ctrl.Result{}, err }
    cl := cluster.GetClient()
    var obj v1.Foo
    err = cl.Get(ctx, req.NamespacedName, &obj)
    // ... reconcile against this cluster ...
}
```

Each cluster has its own Cache (informers), but they share the workqueue and the reconciler logic.

### 21.3 The trade-offs

- **Latency.** A reconcile against a remote cluster pays network RTT for every Read/Write (no in-process cache helps if the change has to happen on the remote cluster). Cache reads stay fast; writes do not.
- **Failure isolation.** If cluster N is partitioned, its informer goes silent. Other clusters' reconciles continue. Be careful: if your reconciler reads from N before deciding what to do in cluster M, an N partition blocks M's reconcile.
- **RBAC.** Each cluster needs RBAC granting your service account watch/list/create/update on the relevant types. Managing N kubeconfigs is its own problem.
- **Memory.** N × (informer memory) per type, even if 99% of N clusters have only a handful of objects.

Chapter 26 covers Karmada, KubeFed, ClusterAPI, and the higher-level patterns that build on this primitive.

---

## 22. Testing: envtest and Fake Client

A reconciler is a function from cluster state to cluster state. Testing it well means feeding it state and asserting on the outcomes.

### 22.1 envtest: a real apiserver

`sigs.k8s.io/controller-runtime/pkg/envtest` brings up a stripped-down apiserver and etcd inside the test binary. This is the gold standard for testing controllers: real API surface, real admission, real watch fan-out.

```go
import (
    . "github.com/onsi/ginkgo/v2"
    . "github.com/onsi/gomega"
    "sigs.k8s.io/controller-runtime/pkg/envtest"
)

var _ = BeforeSuite(func() {
    testEnv := &envtest.Environment{
        CRDDirectoryPaths: []string{"../config/crd/bases"},
    }
    cfg, err := testEnv.Start()
    Expect(err).NotTo(HaveOccurred())

    mgr, err := ctrl.NewManager(cfg, ctrl.Options{Scheme: scheme})
    Expect(err).NotTo(HaveOccurred())

    Expect((&MyReconciler{Client: mgr.GetClient(), Scheme: scheme}).
        SetupWithManager(mgr)).To(Succeed())

    go func() {
        Expect(mgr.Start(ctx)).To(Succeed())
    }()
})

var _ = Describe("MyKind reconciler", func() {
    It("creates a Deployment for each MyKind", func() {
        obj := &myv1.MyKind{
            ObjectMeta: metav1.ObjectMeta{Name: "foo", Namespace: "default"},
            Spec:       myv1.MyKindSpec{Replicas: 3},
        }
        Expect(k8sClient.Create(ctx, obj)).To(Succeed())

        Eventually(func() error {
            var dep appsv1.Deployment
            return k8sClient.Get(ctx, client.ObjectKey{
                Namespace: "default", Name: "foo",
            }, &dep)
        }, "5s").Should(Succeed())
    })
})
```

Notes:

- envtest does **not** run the kube-controller-manager, kubelet, or scheduler. There is no Deployment controller; pods are not scheduled; nothing actually runs. envtest is for testing the *reconcile logic*, not the cluster ecosystem.
- The `Eventually(...)` block polls because reconciliation is asynchronous. Don't `time.Sleep`; let Eventually handle the wait.
- CRDs are loaded from manifests on disk; this validates that your CRD schema is well-formed.

### 22.2 Fake client: no apiserver

For pure unit tests, `sigs.k8s.io/controller-runtime/pkg/client/fake` provides an in-memory client.

```go
import "sigs.k8s.io/controller-runtime/pkg/client/fake"

func TestReconciler(t *testing.T) {
    obj := &myv1.MyKind{
        ObjectMeta: metav1.ObjectMeta{Name: "foo", Namespace: "default"},
        Spec:       myv1.MyKindSpec{Replicas: 3},
    }
    cl := fake.NewClientBuilder().
        WithScheme(scheme).
        WithObjects(obj).
        WithStatusSubresource(&myv1.MyKind{}).
        Build()

    r := &MyReconciler{Client: cl, Scheme: scheme}
    _, err := r.Reconcile(context.Background(), ctrl.Request{
        NamespacedName: types.NamespacedName{Namespace: "default", Name: "foo"},
    })
    if err != nil { t.Fatal(err) }

    var dep appsv1.Deployment
    if err := cl.Get(context.Background(), client.ObjectKey{
        Namespace: "default", Name: "foo",
    }, &dep); err != nil {
        t.Fatalf("expected Deployment to exist: %v", err)
    }
    if *dep.Spec.Replicas != 3 {
        t.Errorf("expected 3 replicas, got %d", *dep.Spec.Replicas)
    }
}
```

`WithStatusSubresource` is required if your type has the status subresource; without it, `r.Status().Update()` will fail.

The fake client is significantly faster than envtest (no process startup, no JSON encoding) and is the right choice for unit-testing the reconciler logic in isolation.

### 22.3 What to test

- **Happy path**: spec → expected side effects.
- **Idempotence**: call Reconcile twice; second call must be a no-op.
- **No-change**: object with no relevant changes; reconcile should be a no-op.
- **Deletion**: object with deletionTimestamp; reconcile cleans up and removes finalizer.
- **Error injection**: apiserver returns 409 Conflict, 404 NotFound, 500 Internal — reconcile must handle gracefully.
- **Owned-resource drift**: child Deployment manually edited; reconcile reverts.

A good test suite has at least one test per branch of the reconcile function. controller-runtime gives you everything you need to drive these without a real cluster.

### 22.4 Avoid sleeping

The biggest test smell in controller code is `time.Sleep(...)`. Reconciliation is event-driven; tests should be too. Use `Eventually` with a timeout (envtest) or call `Reconcile` directly and assert on outcomes (fake client). If a test legitimately needs to assert "no work happens within 1 second," use `Consistently`:

```go
Consistently(func() bool {
    var dep appsv1.Deployment
    err := k8sClient.Get(ctx, key, &dep)
    return apierrors.IsNotFound(err)
}, "1s").Should(BeTrue())
```

`Consistently` polls and fails if the assertion ever becomes false within the window.

---

## 23. Performance and Resource Math

A controller is a process that holds N objects in memory and runs M reconciles per second. The performance envelope is largely determined by the informer memory and the workqueue throughput. Here are the numbers.

### 23.1 Informer memory

Each informer holds the full set of in-scope objects in its Indexer, deserialized into Go structs. As a rule of thumb:

```
   memory(informer) ≈ N_objects × (avg_object_size + go_overhead)

   typical:
     Pod          ~5 KB serialized, ~12 KB in memory
     Service      ~2 KB serialized, ~5 KB
     ConfigMap    ~depends on data; can be 1 MB
     Deployment   ~8 KB serialized, ~20 KB
     CRD-of-yours ~depends; usually 1–10 KB
```

For a 5000-node cluster running 100 000 Pods, the Pod informer alone is ~1.2 GB. This is one of the major reasons SharedInformerFactory exists: you pay this cost once per process, regardless of how many controllers consume Pod events.

### 23.2 Per-informer goroutines

Each Reflector is one goroutine (the ListAndWatch loop). Each informer adds one goroutine for the processLoop. Each event handler is invoked sequentially on the informer's distributor goroutine. So each informer is a constant number of goroutines (≈ 4), regardless of object count.

A controller-runtime Manager with 20 watched types has ≈ 80 goroutines for the cache layer, plus the worker goroutines (default 1 per controller, multiplied by `MaxConcurrentReconciles`).

### 23.3 One informer per (GVR, scope)

The single most important rule for memory: **never instantiate two informers for the same (GVR, namespace, labelSelector) tuple**. Always go through the SharedInformerFactory.

```go
// WRONG (two informers, two watches, two caches):
informer1 := factory.Core().V1().Pods().Informer()
informer2 := informers.NewSharedInformerFactory(client, 0).Core().V1().Pods().Informer()

// RIGHT:
informer := factory.Core().V1().Pods().Informer()
// add multiple handlers if multiple consumers:
informer.AddEventHandler(handlerA)
informer.AddEventHandler(handlerB)
```

controller-runtime's Manager enforces this automatically via `mgr.GetCache()`. The bug usually appears when someone manually instantiates a separate `client-go` factory inside the same process as the Manager — boom, double memory.

### 23.4 Workqueue throughput

The default rate limiter caps at 10 qps overall and ~10ms minimum per-item. A single-worker controller can therefore do at most ~100 reconciles/sec when warm (no backoff), and the apiserver is usually the bottleneck before then.

Multiple workers help only if reconciles have I/O dead time. If your reconcile is mostly cache-lookup and a single apiserver write, more workers don't help — you'll just hit the rate limiter sooner.

Per-key serial reconcile is the throughput unit. If you have 10 000 objects that all need work, the bottleneck is `10000 / qps`. At 10 qps that's 1000 seconds; at 100 qps (with a tighter rate limiter), 100 seconds.

### 23.5 Bottleneck scaling

| Bottleneck | Symptom | Mitigation |
|---|---|---|
| Workqueue depth | metric grows; reconciles delayed | More workers; faster reconcile; tighter predicates |
| Per-item rate limit | individual keys reconciled slowly | Tune rate limiter; reduce error rate |
| Apiserver QPS | reconciles return 429 | Bump client QPS (kubeconfig); apiserver scaling |
| Cache memory | OOMKill | Scope cache (namespace/label); drop unused informers |
| Reflector relist | spikes; apiserver load | Increase watch cache size; reduce 410 Gone events |
| Reconcile CPU | high pod CPU; few reconciles/sec | Profile; reduce work per reconcile; cache external state |

### 23.6 The 5000-node rule of thumb

For very large clusters:

- Pod informer: ~1 GB
- Node informer: ~50 MB
- ConfigMap informer: highly variable; often 100 MB+
- Total per-process baseline for a Manager watching 5–10 common types: ~2–4 GB resident.

If you find this too large, the answer is **scope** (section 20) or **slice** (one operator instance per shard, using a label-selector partition). Multi-shard operators are how Karpenter and a few others handle scale.

---

## 24. Observability: The Standard Metrics

controller-runtime exposes a fixed set of Prometheus metrics on the Manager's metrics endpoint (default `:8080/metrics`). Knowing these by heart is part of being able to operate a controller.

### 24.1 Reconcile metrics

```
controller_runtime_reconcile_total{controller="mykind", result="success|error|requeue|requeue_after"}
  Counter. Total reconciles, partitioned by outcome.

controller_runtime_reconcile_errors_total{controller="mykind"}
  Counter. Reconciles that returned a non-nil error.

controller_runtime_reconcile_time_seconds{controller="mykind"}
  Histogram. Wall-clock reconcile duration.

controller_runtime_active_workers{controller="mykind"}
  Gauge. Workers currently inside Reconcile.

controller_runtime_max_concurrent_reconciles{controller="mykind"}
  Gauge. Configured worker count.
```

### 24.2 Workqueue metrics

```
workqueue_depth{name="mykind"}
  Gauge. Items pending in the workqueue (dirty + queue).

workqueue_adds_total{name="mykind"}
  Counter. Total Add calls.

workqueue_queue_duration_seconds{name="mykind"}
  Histogram. How long items waited in queue before being Get'd.

workqueue_work_duration_seconds{name="mykind"}
  Histogram. How long the worker held an item (between Get and Done).

workqueue_unfinished_work_seconds{name="mykind"}
  Gauge. Age of the oldest currently-processing item. Should be small.

workqueue_longest_running_processor_seconds{name="mykind"}
  Gauge. Wall-clock of the longest in-flight reconcile.

workqueue_retries_total{name="mykind"}
  Counter. Total AddRateLimited calls (re-enqueues after error).
```

### 24.3 Leader election metrics

```
leader_election_master_status{name="my-controller"}
  Gauge. 1 if this replica is the leader, 0 otherwise.

leader_election_slowpath_total{name="my-controller"}
  Counter. Number of times the leader took longer than RenewDeadline/2
  to renew, indicating apiserver contention.
```

### 24.4 Recommended alerts

```yaml
- alert: ControllerWorkqueueDepth
  expr: workqueue_depth{name="mykind"} > 100
  for: 10m
  annotations:
    summary: "Workqueue {{ $labels.name }} backlog: {{ $value }}"

- alert: ControllerLongRunningReconcile
  expr: workqueue_longest_running_processor_seconds{name="mykind"} > 300
  for: 5m
  annotations:
    summary: "Workqueue {{ $labels.name }} has a reconcile running > 5m"

- alert: ControllerReconcileErrorRate
  expr: rate(controller_runtime_reconcile_errors_total[5m]) > 0.1
  for: 10m
  annotations:
    summary: "Controller {{ $labels.controller }} error rate: {{ $value }}/s"

- alert: LeaderElectionNotElected
  expr: max(leader_election_master_status{name="my-controller"}) == 0
  for: 1m
  annotations:
    summary: "No leader for my-controller — all replicas down?"
```

### 24.5 Tracing

controller-runtime can emit OpenTelemetry traces if you wire `ctrl.SetLogger` and configure an OTEL exporter. The default is silent. Tracing reconciles is gold for diagnosing slow operators in production — you can see exactly which apiserver call took 800ms.

### 24.6 Events

`record.EventRecorder` lets a controller emit Kubernetes Events:

```go
r.Recorder.Event(obj, corev1.EventTypeNormal, "Reconciled",
    "successfully reconciled MyKind/foo")

r.Recorder.Eventf(obj, corev1.EventTypeWarning, "DependencyMissing",
    "could not find Secret %q", obj.Spec.SecretRef)
```

Events are short-lived (default 1 hour in etcd) and visible via `kubectl describe`. They are *not* a substitute for logs or metrics; they are a user-facing surface for "what happened to this object." Use them sparingly — every event is an etcd write.

---

## 25. Pitfalls: The Long List

Every staff engineer who has shipped a controller has stepped on at least half of these.

**1. Writing an edge-triggered controller.** Reacting to Add/Update/Delete callbacks instead of reading current state in Reconcile. Drops events on watch reconnect, restart, resync. → Section 1.

**2. Forgetting `Done(key)`.** Worker pops a key, processes it, never calls Done. The key is permanently stuck in the processing set; future Adds are silently held back. Always `defer queue.Done(key)`. → Section 8.

**3. Requeue-on-success spam.** Returning `Result{Requeue: true}` from every reconcile, including no-op reconciles. The workqueue spins at the rate limiter's cap, eating CPU and producing no work. Return `Result{}, nil` when there's nothing to do.

**4. Reading from apiserver inside Reconcile.** Calling `r.APIReader.Get()` or constructing a fresh client each call. Burns apiserver QPS; bypasses the cache; turns a fast in-memory lookup into an apiserver round-trip. Use `r.Get()` (cached) for cached types.

**5. Reconcile takes a lock another reconciler also takes.** Two controllers in the same process, both worker-pooled, both grab a shared mutex. Deadlocks the moment they both reconcile related objects. Make Reconcile lock-free; if you need shared state, use sync.Map or channel-based dispatch.

**6. Leader election with too-short RenewDeadline.** Setting `RenewDeadline=2s` to "fail over faster" — the apiserver's median latency under load can exceed 2s, so the leader unnecessarily abdicates and the controllers flap. Defaults (15s/10s/2s) are tuned; don't change without measurement.

**7. Finalizer with no remover.** Controller adds a finalizer in v1 but the cleanup logic was never finished. When users delete the CR, the object stays in "Terminating" forever. Test the deletion path explicitly. → Section 16.

**8. Mutating spec from the controller.** Controller updates `spec.replicas` (or any spec field). GitOps engine reverts. Controller reverts. Infinite loop. Spec is the user's; status is the controller's. The narrow exception is when one controller is the explicit owner of a spec field via Server-Side Apply (HPA on Deployment.spec.replicas). → Section 13.

**9. Reading from the cache before HasSynced.** Workers start before `WaitForCacheSync` returns. Cache appears empty. Controller "garbage-collects" half the cluster because nothing has an OwnerReference yet. Always block on cache sync.

**10. Setting `observedGeneration = generation` before work is done.** External observers conclude "the controller is caught up" while reconciliation is mid-flight. GitOps engine claims success; the next health check fails mysteriously. Set ObservedGeneration only at the end, when conditions are also updated. → Section 15.

**11. Not honoring DeletionTimestamp.** Controller continues to create children, ignoring that the parent is being deleted. Children appear after the parent is supposedly gone. Always check `obj.DeletionTimestamp.IsZero()` at the start of Reconcile.

**12. Goroutine leak on watch error.** Custom controllers that fork goroutines per watch event but never cancel them on errors. Process memory climbs forever. Use a context tied to the manager's lifecycle and propagate it to every goroutine.

**13. Status updates that bump generation.** Patching the object via the wrong subresource (writing to `/` instead of `/status`). Generation increments; predicates re-enqueue; controller reconciles its own status writes. Always use `r.Status().Update()` / `r.Status().Patch()`. → Section 13.

**14. Predicates that filter out events the controller needs.** Adding `GenerationChangedPredicate` to an `Owns` clause where the owned object's status (not spec) carries the signal. Controller never reconciles when the child becomes Ready. Predicates apply per-source; choose carefully.

**15. Re-using `RetryOnConflict` without re-fetching.** Get outside the retry callback; retry sends the same stale RV; conflict forever. → Section 17.

**16. Storing data in annotations instead of status.** Annotations are mutable strings, semi-structured. Status is typed. Use status for data the controller computes; use annotations only for cross-controller metadata that has no schema.

**17. Cache reads of types you didn't watch.** controller-runtime's cached client lazily instantiates an informer for any type you read. Result: a "cheap" `r.List(&corev1.Pods{})` silently spins up a Pod informer (potentially gigabytes). Watch types explicitly via the builder; for uncached reads, use `mgr.GetAPIReader()`.

**18. DeepCopy of a million-object list every reconcile.** A reconcile that calls `r.List(&corev1.Pods{})` to count pods. List returns DeepCopies; on a large cluster that's a CPU vortex. Add an indexer; query by index; iterate without copying.

**19. Custom EnqueueRequestsFromMapFunc with O(N) work.** The map function runs on the informer goroutine. If it lists every Foo in the cluster on every Secret event, you've made event delivery O(Foo×Secret). Add an indexer.

**20. Confusing `Watches` and `Owns`.** Owns walks OwnerReferences; Watches needs a map function. Using Watches when Owns would suffice clutters the code; using Owns when there's no OwnerReference simply doesn't fire.

**21. ForceOwnership in SSA used liberally.** Forces ownership on every field every reconcile, stealing ownership from any legitimate other writer (kubectl edits, HPA). Use Force only on fields you genuinely own forever.

**22. Logging the full object on every reconcile.** Pod objects can be 20 KB JSON; structured logging at 100 reconciles/sec is 2 MB/sec of log volume. Log identifiers and outcomes; log the full object only at debug level.

**23. SetControllerReference on objects in another namespace.** Cross-namespace OwnerReferences are not allowed; the GC controller treats them as nonexistent. Children of a cluster-scoped object can live in any namespace and reference it; but children in namespace A cannot reference a parent in namespace B.

**24. Trusting the cache for write-after-read.** You wrote the object; the watch event hasn't propagated yet; your next Reconcile reads the old version from the cache. Either return early after a write (let the next reconcile re-evaluate), or use `mgr.GetAPIReader()` for strict reads.

**25. No backoff on external API failures.** Cloud LB API rate-limits to 5 qps; your controller hits it at 50 qps; LB API returns 429; you AddRateLimited the key; same 50 qps next minute. Configure a tighter per-controller rate limiter for external-heavy reconciles.

**26. Reconcile that side-effects then returns an error.** Side effect happened; error means re-enqueue; second reconcile does the side effect again. Either make side effects idempotent (correct) or design reconcile as "first compute the diff, then apply it atomically, then return."

**27. Owning fields on objects you don't own.** A controller that sets `spec.replicas` on a Deployment it didn't create. If HPA owns it too, you fight forever. If a user owns it, you erase their edits. Even SSA doesn't fix this — it just makes the fight visible.

**28. Catching panics in reconcile and continuing.** A panic in Reconcile is caught by controller-runtime and the key is re-queued. Catching panics yourself and "continuing" hides bugs and produces inconsistent state. Let panics propagate; fix the bug.

---

## 26. TL;DR

The single sentence: **Kubernetes is a level-triggered, declarative state machine; every controller is a function from current state to desired state, run repeatedly until they converge, with a workqueue-backed rate-limited event loop in front.**

The pieces in order from the apiserver down to your reconcile function:

```
   apiserver  ──watch──►  Reflector ──Add/Update/Delete──►  DeltaFIFO
                                                                  │
                                                                  ▼
                                                          processDeltas
                                                          ┌────┬────┐
                                                          ▼    ▼    ▼
                                                    Indexer  handlers (enqueue key)
                                                                  │
                                                                  ▼
                                                            Workqueue
                                                          (deduping + rate-limited)
                                                                  │
                                                                  ▼
                                                          worker.runWorker
                                                            ┌──── Get(key)
                                                            │     defer Done(key)
                                                            │     obj = cache.Get(key)
                                                            │     Reconcile(obj)
                                                            └──── on err: AddRateLimited
                                                                  on ok:  Forget
```

The four contract clauses you must honor every time:

1. **Idempotent**: same input, same outputs; second call is a no-op.
2. **Level-triggered**: read current state; ignore history.
3. **Eventually consistent**: many reconciles to converge; never block waiting.
4. **Single concurrency per key**: workqueue guarantees this.

The discipline that separates working from correct:

- **Spec is the user's; status is the controller's.** Use the status subresource. `r.Status().Update()`, never `r.Update()` for status writes.
- **Generation tracks spec; ObservedGeneration is set at the END of work.** External observers use the gap to know "is the controller caught up?"
- **Finalizers guard external state.** Add on first reconcile; remove only after cleanup; cleanup must be idempotent.
- **Optimistic concurrency on `resourceVersion`** — re-fetch inside `RetryOnConflict`, never blind retry.
- **Server-Side Apply with a stable `fieldManager`** for any field shared across multiple writers.
- **Predicates filter at the source**, not inside Reconcile. `GenerationChangedPredicate` on `For` is the canonical pattern for "ignore my own status writes."
- **Leader election (Lease, 15s/10s/2s) plus optimistic CAS** is the HA story. Leases don't fence; CAS catches the gap.
- **Always WaitForCacheSync before starting workers.**
- **Always defer Done(key).**

controller-runtime is client-go with the boilerplate written for you: Manager owns the shared Cache, Client, leader election, and metrics; Builder wires `For`/`Owns`/`Watches` into the same workqueue; Reconciler is the single function you write. The plumbing in sections 4–10 is still under there, and when something is wrong, you debug down to that layer.

Test with `envtest` for integration and the `fake` client for unit tests; never sleep, always `Eventually`. Watch the standard metrics: `workqueue_depth`, `workqueue_longest_running_processor_seconds`, `controller_runtime_reconcile_errors_total`. Alert on backlog and on long-running reconciles. Profile when reconciles slow; usually the answer is an indexer or a tighter predicate.

This loop is the entire programming model. Every built-in controller — Deployment, ReplicaSet, StatefulSet, Endpoints, GC, Node, Service — is this loop. Every CRD operator you'll ever write is this loop. Every cloud-controller-manager component is this loop. Once you see it, the rest of Kubernetes is configuration around it.

Next chapter, 09: the scheduler — the one controller that is not *purely* a reconciler, because its inputs include cluster-wide feasibility computations that the rest of the controllers never need. Then 10–14: the kubelet and the data plane that *runs* the work this loop creates. The reconcile loop is the brain; the data plane is the muscle. Both speak through the apiserver.
