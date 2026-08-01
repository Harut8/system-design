# Controller Tasks — writing one by hand with client-go

Do this after `api-machinery-tasks.md`. You will write a real controller with raw
client-go — no controller-runtime, no kubebuilder. That's deliberate: the
framework hides five distinct components behind one `Reconcile` method, and if you
meet them as one thing you will never debug them as separate things.

What you build: a controller that watches Pods requesting `nvidia.com/gpu` and
maintains a ConfigMap summarising **requested GPUs per node**. Small, idempotent,
and a miniature of the capacity tooling you actually want to build.

> The one idea: **a controller is a loop that makes the world match the spec, and
> the event stream only tells it when to look.**
>
> ```
>   informer ──▶ workqueue ──▶ reconcile(key)
>   (watch +      (dedupe,      (LIST current state,
>    cache)        rate-limit,   compute desired,
>                  retry)        write difference)
>
>   Never: "on event X, do Y."   Always: "given key K, make it correct."
> ```

Setup: `kind create cluster --name ctrl` and `go mod init ctrllab`.

---

## Level 0 — Orientation

1. The five components you're about to wire, and what each solves:

   | Component | Problem it solves |
   |---|---|
   | **Reflector** | LIST+WATCH, handles 410 by relisting |
   | **DeltaFIFO** | Orders changes, coalesces duplicates |
   | **Indexer** (cache/store) | Local read-only replica so you never GET the API server |
   | **Workqueue** | Dedupes keys, rate-limits, retries with backoff |
   | **Reconciler** | Your business logic, and the only part you should be writing |

2. `SharedInformerFactory` gives you the first three. You wire the last two.
3. **Shared** matters: ten controllers watching Pods share one watch and one
   cache. Creating your own informer per controller multiplies API load.

---

## Level 1 — Talk to the API from Go

- [ ] **Task 1.1 — Clientset, in-cluster or not**
  ```go
  cfg, err := rest.InClusterConfig()
  if err != nil {
      cfg, err = clientcmd.BuildConfigFromFlags("", clientcmd.RecommendedHomeFile)
  }
  clientset, err := kubernetes.NewForConfig(cfg)
  ```
  - Learn: the same binary runs on your laptop and in a Pod. Always write the
    fallback — you'll run it locally a hundred times before it ever deploys.

- [ ] **Task 1.2 — A naive poll loop, so you feel the problem**
  ```go
  for {
      pods, _ := clientset.CoreV1().Pods("").List(ctx, metav1.ListOptions{})
      fmt.Println(len(pods.Items))
      time.Sleep(5 * time.Second)
  }
  ```
  - Do: run it against a cluster and watch `kubectl get --raw /metrics | grep apiserver_request_total`.
  - Learn: correct, and unusable at scale. Every controller doing this would melt
    the API server. Informers exist to turn N pollers into one watch.

- [ ] **Task 1.3 — Rate limiting is on by default**
  - Do: set `cfg.QPS = 5; cfg.Burst = 10`, then list in a tight loop.
  - Verify: client-side throttling messages in logs.
  - Learn: client-go throttles *you* before the server does. A controller that
    seems mysteriously slow is often hitting its own QPS ceiling, not the server's.

---

## Level 2 — Informers

- [ ] **Task 2.1 — Factory, informer, lister**
  ```go
  factory := informers.NewSharedInformerFactory(clientset, 10*time.Minute)
  podInformer := factory.Core().V1().Pods()
  podLister  := podInformer.Lister()

  factory.Start(ctx.Done())
  if !cache.WaitForCacheSync(ctx.Done(), podInformer.Informer().HasSynced) {
      log.Fatal("cache sync failed")
  }
  pods, _ := podLister.List(labels.Everything())   // reads local memory, not the API
  ```
  - Learn: after sync, `podLister` is a **local in-memory replica**. Listing it is
    free. This is why controllers can afford to re-read everything on every
    reconcile — which is what makes level-triggering practical.

- [ ] **Task 2.2 — Never skip WaitForCacheSync**
  - Do: comment it out and list immediately.
  - Verify: you get zero or partial results.
  - Learn: acting on a half-populated cache means a controller that **deletes
    things it thinks are orphaned** on every restart. This is a genuinely
    dangerous bug and it only shows up under load or on cold start.

- [ ] **Task 2.3 — The resync period is not a poll**
  - Learn: the 10-minute resync replays `UpdateFunc` for every cached object with
    `old == new`. It does **not** re-hit the API. It's a safety net that re-drives
    your reconcile in case you dropped something — which is only useful *because*
    your reconciler is level-triggered. If resync breaks your controller, your
    controller isn't idempotent.

- [ ] **Task 2.4 — Event handlers enqueue, they don't work**
  ```go
  podInformer.Informer().AddEventHandler(cache.ResourceEventHandlerFuncs{
      AddFunc:    func(obj any) { enqueue(obj) },
      UpdateFunc: func(old, new any) { enqueue(new) },
      DeleteFunc: func(obj any) { enqueue(obj) },
  })
  ```
  - **Rule:** an event handler must be non-blocking and must contain no logic.
    Its entire job is to compute a key and put it on the queue. Any work done here
    blocks the shared informer for every other consumer in the process.

- [ ] **Task 2.5 — DeletedFinalStateUnknown**
  ```go
  func enqueue(obj any) {
      if tomb, ok := obj.(cache.DeletedFinalStateUnknown); ok {
          obj = tomb.Obj
      }
      key, err := cache.MetaNamespaceKeyFunc(obj)
      if err == nil { queue.Add(key) }
  }
  ```
  - Learn: if a delete happened while the watch was disconnected, the informer
    hands you a tombstone instead of the object. Omitting this check panics your
    controller on a type assertion — reliably, in production, never in testing.

---

## Level 3 — The workqueue

- [ ] **Task 3.1 — Create it**
  ```go
  // client-go >= 0.30 (typed):
  queue := workqueue.NewTypedRateLimitingQueue[string](
      workqueue.DefaultTypedControllerRateLimiter[string]())
  // older: workqueue.NewRateLimitingQueue(workqueue.DefaultControllerRateLimiter())
  ```
  - Learn: the default is an exponential backoff (5ms → 1000s) **plus** an overall
    bucket limiter (10 qps, burst 100). Two limiters, different jobs: per-item
    backoff for the failing object, global for everything.

- [ ] **Task 3.2 — Why a queue and not a goroutine per event**
  - Learn three properties you get for free:
    1. **Dedup** — a key enqueued 50 times while processing is processed once more.
    2. **Serialisation per key** — the same key is never processed concurrently,
       so you don't need locks around per-object logic.
    3. **Backoff** — a failing item retries slower instead of hot-looping.

- [ ] **Task 3.3 — The worker loop**
  ```go
  func (c *Controller) runWorker(ctx context.Context) {
      for c.processNextItem(ctx) {}
  }

  func (c *Controller) processNextItem(ctx context.Context) bool {
      key, shutdown := c.queue.Get()
      if shutdown { return false }
      defer c.queue.Done(key)          // MUST be deferred

      if err := c.reconcile(ctx, key); err != nil {
          c.queue.AddRateLimited(key)  // retry with backoff
          return true
      }
      c.queue.Forget(key)              // reset this key's backoff
      return true
  }
  ```
  - **Rule:** `Done` always, `Forget` only on success. Forgetting on failure
    resets the backoff and turns a failing item into a hot loop. Missing `Done`
    means the key is never processable again — a silent, permanent stall.

- [ ] **Task 3.4 — Prove dedup works**
  - Do: add a `time.Sleep(5*time.Second)` in reconcile, then
    `kubectl label pod X a=1 --overwrite` ten times quickly.
  - Verify: your reconcile runs about twice, not eleven times.
  - Learn: this is why "expensive reconcile" is usually fine, and why you should
    never try to batch or debounce by hand.

---

## Level 4 — The reconciler

- [ ] **Task 4.1 — Write it level-triggered**
  ```go
  func (c *Controller) reconcile(ctx context.Context, key string) error {
      // 1. OBSERVE — read the whole world, don't trust the event
      pods, err := c.podLister.List(labels.Everything())
      if err != nil { return err }

      // 2. COMPUTE — derive desired state
      gpusByNode := map[string]int64{}
      for _, p := range pods {
          if p.Spec.NodeName == "" || p.Status.Phase == corev1.PodSucceeded ||
             p.Status.Phase == corev1.PodFailed {
              continue
          }
          for _, ctr := range p.Spec.Containers {
              if q, ok := ctr.Resources.Requests["nvidia.com/gpu"]; ok {
                  gpusByNode[p.Spec.NodeName] += q.Value()
              }
          }
      }

      // 3. ACTUATE — make the world match, idempotently
      return c.writeSummary(ctx, gpusByNode)
  }
  ```
  - Learn: `key` is barely used. That's correct and it's the whole lesson — the key
    says *something about pods changed*, and the reconciler recomputes from
    scratch. Miss ten events and the eleventh still produces the right answer.

- [ ] **Task 4.2 — Idempotent actuation**
  ```go
  func (c *Controller) writeSummary(ctx context.Context, m map[string]int64) error {
      data := map[string]string{}
      for node, n := range m { data[node] = strconv.FormatInt(n, 10) }

      cm, err := c.client.CoreV1().ConfigMaps(ns).Get(ctx, name, metav1.GetOptions{})
      if apierrors.IsNotFound(err) {
          _, err = c.client.CoreV1().ConfigMaps(ns).Create(ctx,
              &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: name}, Data: data},
              metav1.CreateOptions{})
          return err
      }
      if err != nil { return err }
      if reflect.DeepEqual(cm.Data, data) { return nil }   // no-op write avoidance
      cm = cm.DeepCopy()                                   // never mutate the cache
      cm.Data = data
      _, err = c.client.CoreV1().ConfigMaps(ns).Update(ctx, cm, metav1.UpdateOptions{})
      return err
  }
  ```
  - **Two rules, both load-bearing:**
    - **`DeepCopy` before mutating anything from a lister.** The object is shared
      with every other consumer in the process. Mutating it corrupts their view,
      and the bug surfaces somewhere else entirely.
    - **Skip the write when nothing changed.** Otherwise your update triggers a
      watch event, which enqueues a key, which reconciles, which updates… a
      self-sustaining loop that looks like the cluster is haunted.

- [ ] **Task 4.3 — Handle conflicts by requeueing**
  - Do: return the error on `IsConflict` rather than retrying in-line.
  - Learn: `AddRateLimited` already does the right thing. In-line retry loops hold
    a worker and hide the failure from your metrics.

- [ ] **Task 4.4 — Run it**
  ```bash
  go run . &
  kubectl run gpu-a --image=busybox --overrides='{"spec":{"containers":[{"name":"c","image":"busybox","command":["sleep","3600"],"resources":{"limits":{"nvidia.com/gpu":"2"}}}]}}'
  kubectl get cm gpu-summary -o yaml
  ```
  - On a kind cluster there are no real GPUs, so the pod stays Pending — which is
    fine and instructive: your controller counts **requests**, not usage, exactly
    like the scheduler does (`resources-tasks.md` EC-7).

---

## Level 5 — Making it production-shaped

- [ ] **Task 5.1 — Leader election**
  ```go
  lock := &resourcelock.LeaseLock{
      LeaseMeta: metav1.ObjectMeta{Name: "gpu-summary-controller", Namespace: ns},
      Client:    clientset.CoordinationV1(),
      LockConfig: resourcelock.ResourceLockConfig{Identity: hostname},
  }
  leaderelection.RunOrDie(ctx, leaderelection.LeaderElectionConfig{
      Lock: lock, LeaseDuration: 15*time.Second, RenewDeadline: 10*time.Second,
      RetryPeriod: 2*time.Second,
      Callbacks: leaderelection.LeaderCallbacks{
          OnStartedLeading: func(ctx context.Context) { c.Run(ctx) },
          OnStoppedLeading: func() { os.Exit(0) },
      },
  })
  ```
  - Do: `kubectl get lease -n <ns>` while two replicas run.
  - Learn: two controllers writing the same object fight forever
    (`api-machinery-tasks.md` EC-8). Leader election is how you run replicas for
    *availability* without running them for *concurrency*.
  - **Note:** it is not a distributed lock. On a network partition the old leader
    may still be running until its lease expires. `OnStoppedLeading` must exit the
    process, not just stop the loop.

- [ ] **Task 5.2 — RBAC, minimally**
  ```yaml
  rules:
  - apiGroups: [""]     resources: [pods]       verbs: [get, list, watch]
  - apiGroups: [""]     resources: [configmaps] verbs: [get, create, update]
  - apiGroups: [coordination.k8s.io] resources: [leases] verbs: [get, create, update]
  ```
  - Learn: `list` and `watch` are separate verbs and informers need **both**. A
    controller that starts and then silently does nothing is usually missing
    `watch`.

- [ ] **Task 5.3 — Instrument the queue**
  - Learn the four numbers that tell you everything:
    - `workqueue_depth` — climbing means you're not keeping up
    - `workqueue_adds_total` — a huge rate means an update loop (Task 4.2)
    - `workqueue_work_duration_seconds` — your reconcile latency
    - `workqueue_retries_total` — climbing means something fails persistently
  - Given your observability background, this is the part you'll be best at and
    the part most controller authors neglect entirely.

---

## Level 6 — Advanced

- [ ] **Task 6.1 — Custom indexers**
  ```go
  podInformer.Informer().AddIndexers(cache.Indexers{
      "byNode": func(obj any) ([]string, error) {
          return []string{obj.(*corev1.Pod).Spec.NodeName}, nil
      },
  })
  pods, _ := podInformer.Informer().GetIndexer().ByIndex("byNode", "node-1")
  ```
  - Learn: turns an O(n) scan into an O(1) lookup. At 100k pods this is the
    difference between a working controller and a heap profile.

- [ ] **Task 6.2 — Scope the cache**
  ```go
  informers.NewSharedInformerFactoryWithOptions(clientset, resync,
      informers.WithNamespace("gpu-workloads"),
      informers.WithTweakListOptions(func(o *metav1.ListOptions) {
          o.LabelSelector = "workload=gpu"
      }))
  ```
  - Learn: the informer caches whatever it watches. Unscoped, your controller's
    memory equals the cluster's object count. Scope at the informer, not in
    reconcile.

- [ ] **Task 6.3 — Watch a second type**
  - Add a Node informer; enqueue a fixed sentinel key on node changes.
  - Learn: many-to-one mappings are normal. Since your reconciler recomputes
    everything anyway, the key can be a constant — a legitimate and common pattern
    for aggregate controllers.

- [ ] **Task 6.4 — Emit events**
  - Learn: `record.EventRecorder` writes Events users see in `kubectl describe`.
    Events are rate-limited and expire (~1h). They are a UX affordance, never a
    log and never a state store.

---

## Level 7 — Edge Cases & Production Nuances

### EC-1 — Mutating an object from the lister

- **Trap:** `pod.Labels["x"]="y"` on a listed object. Another controller in the
  same binary now sees a label nobody set. Or you Update and get a conflict storm.
- **Why:** listers return **pointers into the shared cache**.
- **Rule:** `DeepCopy()` before touching anything you got from a lister. No
  exceptions. This is the single most common client-go bug.

---

### EC-2 — The self-triggering update loop

- **Trap:** CPU pinned, `workqueue_adds_total` climbing forever, cluster fine.
- **Why:** reconcile writes an object it also watches, unconditionally. The write
  produces an event, which enqueues, which writes.
- **Diagnose:** log the object's `resourceVersion` — monotonically climbing with
  no external cause.
- **Fix:** compare before writing (Task 4.2), and/or use a predicate that ignores
  updates where only `resourceVersion`/`status` changed.

---

### EC-3 — `Forget` on failure

- **Trap:** a failing key retries thousands of times per second.
- **Why:** `Forget` resets that key's backoff. Calling it before checking the error
  disables the rate limiter you carefully configured.
- **Rule:** `Forget` on success only. `Done` in a `defer`, always.

---

### EC-4 — A controller that trusts `DeleteFunc`

- **Trap:** cleanup runs in `DeleteFunc`. It doesn't run after a controller
  restart, because the delete happened while you were down.
- **Rule:** external cleanup goes in a **finalizer** (`api-machinery-tasks.md`
  Level 3), reconciled like everything else. `DeleteFunc` is a hint that something
  vanished, not a guaranteed callback. If your cleanup only runs on an event, it
  will eventually not run.

---

### EC-5 — Reconcile isn't idempotent, and resync exposes it

- **Trap:** works fine, then every 10 minutes duplicates appear.
- **Why:** the resync replays every object through `UpdateFunc` with
  `old == new`. Any "create a thing" logic that doesn't check for existence
  creates a second one.
- **Rule:** set the resync to something short (30s) in development. If that breaks
  your controller, you have a real bug — resync didn't cause it, it revealed it.

---

### EC-6 — Blocking in an event handler

- **Trap:** you call the API from `AddFunc`. Under load the informer's delivery
  goroutine stalls, and **every** consumer of that shared informer stops receiving
  events — including other controllers in the same process.
- **Rule:** handlers compute a key and enqueue. Nothing else. Ever.

---

### EC-7 — Ignoring `PodSucceeded` / `PodFailed`

- **Trap:** GPU counts drift upward over days.
- **Why:** completed pods keep their `spec.resources` and `spec.nodeName` until
  garbage-collected. They're released from the node but still in your list.
- **Rule:** when computing allocation, filter on phase — and remember terminal
  pods can persist for hours. This exact bug is why capacity dashboards
  over-report; you'll recognise it from the real world.

---

### EC-8 — Watch established, cache never syncs

- **Trap:** controller starts, logs nothing, does nothing, no errors.
- **Diagnose:** `WaitForCacheSync` returning false, usually RBAC — you have `list`
  but not `watch`, so the LIST succeeds and the WATCH 403s silently.
- **Rule:** log loudly on cache-sync failure and exit non-zero. A controller that
  silently does nothing is worse than one that crashes.

---

## Cheat sheet

```go
factory := informers.NewSharedInformerFactory(cs, 10*time.Minute)
inf := factory.Core().V1().Pods()
inf.Informer().AddEventHandler(cache.ResourceEventHandlerFuncs{...})
factory.Start(ctx.Done())
cache.WaitForCacheSync(ctx.Done(), inf.Informer().HasSynced)

key, _ := cache.MetaNamespaceKeyFunc(obj)     // "namespace/name"
ns, name, _ := cache.SplitMetaNamespaceKey(key)
obj = obj.(cache.DeletedFinalStateUnknown).Obj // tombstone unwrap

queue.Add(key) / AddRateLimited(key) / AddAfter(key, d)
defer queue.Done(key); queue.Forget(key)      // Forget on success only
obj.DeepCopy()                                 // before any mutation
```

```bash
kubectl get lease -n <ns>                      # leader election state
kubectl auth can-i watch pods --as=system:serviceaccount:<ns>:<sa>
curl localhost:8080/metrics | grep workqueue_  # depth, adds, retries, duration
```

## Mental model to lock in

- **Five components, one loop:** reflector → DeltaFIFO → indexer → workqueue →
  reconciler. Frameworks hide the first four; you still have to debug them.
- **The event is a hint. The lister is the truth.** Reconcile from the world, never
  from the payload.
- **The queue gives you dedup, per-key serialisation, and backoff.** Never
  hand-roll batching or debouncing — you'll break one of the three.
- **DeepCopy before mutating. Compare before writing.** Those two lines prevent
  the two worst bugs in this file.
- **Cleanup belongs in a finalizer, not `DeleteFunc`.**
- **A controller is only as cheap as its informer cache is small.**

```text
   API server
       │ LIST+WATCH
       ▼
   Reflector ──▶ DeltaFIFO ──▶ Indexer (local cache) ──▶ Lister ──┐
                                   │                              │ reads
                              event handlers                      │
                                   │ key only                     ▼
                                   ▼                        reconcile(key)
                              Workqueue  ──── worker ──────▶  observe
                            (dedup, backoff)                   compute
                                   ▲                           actuate
                                   └──── AddRateLimited ◀── err ────┘
```
