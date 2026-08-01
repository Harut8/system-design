# Operator Tasks — building the real thing with controller-runtime

Do this after `controller-tasks.md`. Everything here is a convenience over what
you already wrote by hand, and you'll recognise each piece.

What you build: a **`GPUBudget`** operator — a namespaced CRD declaring a GPU
allocation cap, with a controller that observes actual requested GPUs, reports
them in status, and marks the budget breached. It's a small, honest version of
what ScaleOps/Cast AI sell, and it's the artifact that moves your Kubernetes
knowledge from *studied* to *implemented*.

> The one idea: **controller-runtime is `for` + `owns` + `reconcile`.** The
> Manager owns the caches, clients, leader election, metrics and health endpoints
> you wired manually last time. You now write only the reconciler — which is why
> it's important you already know what's underneath.

Setup:
```bash
kind create cluster --name op
go install sigs.k8s.io/kubebuilder/v4/cmd/kubebuilder@latest
mkdir gpubudget && cd gpubudget
kubebuilder init --domain lab.example.com --repo lab.example.com/gpubudget
kubebuilder create api --group capacity --version v1alpha1 --kind GPUBudget
```

---

## Level 0 — Orientation

1. Read the scaffold before writing anything:
   - `api/v1alpha1/gpubudget_types.go` — your Go structs; the CRD YAML is *generated* from these
   - `internal/controller/gpubudget_controller.go` — your reconciler
   - `cmd/main.go` — the Manager: caches, leader election, metrics, health
   - `config/` — kustomize manifests, also generated
2. `make manifests generate` — regenerates CRD YAML and deepcopy functions from
   your structs and markers. **Never hand-edit `config/crd/`**; it's output.
3. Map it to what you built by hand:

   | By hand (client-go) | controller-runtime |
   |---|---|
   | SharedInformerFactory | `mgr.GetCache()`, created by `For`/`Owns`/`Watches` |
   | Lister | `r.Client.Get` / `List` (cache-backed by default) |
   | Workqueue + worker loop | the framework's, per-controller |
   | `reconcile(key)` | `Reconcile(ctx, req)` |
   | leaderelection.RunOrDie | `ctrl.Options{LeaderElection: true}` |

---

## Level 1 — Design the type

- [ ] **Task 1.1 — spec is desired, status is observed**
  ```go
  type GPUBudgetSpec struct {
      //+kubebuilder:validation:Minimum=0
      MaxGPUs int64 `json:"maxGPUs"`

      //+kubebuilder:validation:Optional
      Selector *metav1.LabelSelector `json:"selector,omitempty"`
  }

  type GPUBudgetStatus struct {
      AllocatedGPUs      int64              `json:"allocatedGPUs"`
      PodCount           int32              `json:"podCount"`
      ObservedGeneration int64              `json:"observedGeneration,omitempty"`
      Conditions         []metav1.Condition `json:"conditions,omitempty"`
  }
  ```
  - **Rule:** if a user types it, it's spec. If a controller computes it, it's
    status. Nothing lives in both. Getting this wrong is the defining mistake of a
    first CRD and it can't be fixed without an API version bump.

- [ ] **Task 1.2 — Markers earn their keep**
  ```go
  //+kubebuilder:object:root=true
  //+kubebuilder:subresource:status
  //+kubebuilder:resource:shortName=gb
  //+kubebuilder:printcolumn:name="Max",JSONPath=".spec.maxGPUs",type=integer
  //+kubebuilder:printcolumn:name="Allocated",JSONPath=".status.allocatedGPUs",type=integer
  //+kubebuilder:printcolumn:name="Ready",JSONPath=".status.conditions[?(@.type=='Ready')].status",type=string
  type GPUBudget struct { ... }
  ```
  - Do: `make manifests && kubectl apply -k config/crd && kubectl get gb`
  - Learn: printer columns are the difference between an operator people use and
    one they resent. `kubectl get gb` should answer the question without `-o yaml`.

- [ ] **Task 1.3 — Validate in the schema, not in code**
  ```go
  //+kubebuilder:validation:XValidation:rule="self.maxGPUs % 2 == 0",message="maxGPUs must be even"
  ```
  - Learn: CEL runs in the API server, synchronously, with no webhook, no certs
    and no failure mode (`api-machinery-tasks.md` Task 6.3). Every rule you can
    express here is a rule you never have to defend in a webhook.

- [ ] **Task 1.4 — Start at v1alpha1 and mean it**
  - Learn: served CRD versions are a compatibility contract. `v1alpha1` signals
    you may break it. Going to `v1` means conversion webhooks forever. Stay alpha
    longer than feels comfortable.

---

## Level 2 — The reconciler

- [ ] **Task 2.1 — The shape**
  ```go
  func (r *GPUBudgetReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
      log := logf.FromContext(ctx)

      var budget capacityv1alpha1.GPUBudget
      if err := r.Get(ctx, req.NamespacedName, &budget); err != nil {
          return ctrl.Result{}, client.IgnoreNotFound(err)
      }

      var pods corev1.PodList
      if err := r.List(ctx, &pods, client.InNamespace(req.Namespace)); err != nil {
          return ctrl.Result{}, err
      }

      var total int64
      var count int32
      for _, p := range pods.Items {
          if p.Status.Phase == corev1.PodSucceeded || p.Status.Phase == corev1.PodFailed {
              continue                       // controller-tasks.md EC-7
          }
          for _, c := range p.Spec.Containers {
              if q, ok := c.Resources.Requests["nvidia.com/gpu"]; ok {
                  total += q.Value()
              }
          }
          count++
      }

      budget.Status.AllocatedGPUs = total
      budget.Status.PodCount = count
      budget.Status.ObservedGeneration = budget.Generation

      cond := metav1.Condition{Type: "Ready", Status: metav1.ConditionTrue,
          Reason: "WithinBudget", Message: fmt.Sprintf("%d/%d GPUs", total, budget.Spec.MaxGPUs)}
      if total > budget.Spec.MaxGPUs {
          cond.Status, cond.Reason = metav1.ConditionFalse, "BudgetExceeded"
      }
      meta.SetStatusCondition(&budget.Status.Conditions, cond)

      if err := r.Status().Update(ctx, &budget); err != nil {
          return ctrl.Result{}, client.IgnoreNotFound(err)
      }
      log.Info("reconciled", "allocated", total, "max", budget.Spec.MaxGPUs)
      return ctrl.Result{}, nil
  }
  ```
  - Learn: `client.IgnoreNotFound` is the idiom for "object deleted between the
    event and now" — an entirely normal race, not an error.

- [ ] **Task 2.2 — `Status().Update()`, not `Update()`**
  - Do: try `r.Update(ctx, &budget)` after setting status fields.
  - Verify: status is silently discarded (`api-machinery-tasks.md` Task 2.3, now
    biting you in Go).
  - **Rule:** with `//+kubebuilder:subresource:status`, status writes go through
    `r.Status()`. Only.

- [ ] **Task 2.3 — `meta.SetStatusCondition` handles the bookkeeping**
  - Learn: it only bumps `lastTransitionTime` when `status` actually changes, and
    dedupes by `type`. Hand-appending to the conditions slice produces an
    ever-growing array and a write on every reconcile — which is EC-2 from
    `controller-tasks.md` with extra steps.

- [ ] **Task 2.4 — Return values are the whole control API**

  | Return | Meaning |
  |---|---|
  | `ctrl.Result{}, nil` | done; don't requeue |
  | `ctrl.Result{}, err` | requeue with **exponential backoff** and log the error |
  | `ctrl.Result{RequeueAfter: d}, nil` | requeue after `d`; success, just check again |

  - **Rule:** return the error rather than swallowing it and requeuing manually.
    The framework's backoff is the one you want, and swallowing the error hides it
    from `controller_runtime_reconcile_errors_total`.

---

## Level 3 — Wiring what triggers a reconcile

- [ ] **Task 3.1 — `For` and `Watches`**
  ```go
  func (r *GPUBudgetReconciler) SetupWithManager(mgr ctrl.Manager) error {
      return ctrl.NewControllerManagedBy(mgr).
          For(&capacityv1alpha1.GPUBudget{}).
          Watches(&corev1.Pod{},
              handler.EnqueueRequestsFromMapFunc(r.budgetsForPod)).
          Named("gpubudget").
          Complete(r)
  }

  // a Pod change must reconcile every GPUBudget in that Pod's namespace
  func (r *GPUBudgetReconciler) budgetsForPod(ctx context.Context, o client.Object) []ctrl.Request {
      var list capacityv1alpha1.GPUBudgetList
      if err := r.List(ctx, &list, client.InNamespace(o.GetNamespace())); err != nil {
          return nil
      }
      reqs := make([]ctrl.Request, 0, len(list.Items))
      for _, b := range list.Items {
          reqs = append(reqs, ctrl.Request{NamespacedName: types.NamespacedName{
              Namespace: b.Namespace, Name: b.Name}})
      }
      return reqs
  }
  ```
  - Learn: `For` is the primary type. `Watches` + a map function is how a change to
    an unrelated object reaches the right key. This mapping is the part beginners
    get wrong — without it, your operator only reconciles when the CR itself is
    edited, which is almost never.

- [ ] **Task 3.2 — `Owns` for things you create**
  - Learn: `Owns(&corev1.ConfigMap{})` watches ConfigMaps and maps them back to the
    owner via **ownerReference** — the back-pointer from
    `api-machinery-tasks.md` Level 3. It only works if you called
    `controllerutil.SetControllerReference`, and it gives you cascading deletion
    for free.

- [ ] **Task 3.3 — Predicates cut the noise**
  ```go
  For(&capacityv1alpha1.GPUBudget{},
      builder.WithPredicates(predicate.GenerationChangedPredicate{}))
  ```
  - Learn: `generation` only changes on **spec** edits, so this filters out the
    reconciles caused by your own status writes. This is the clean structural fix
    for the self-triggering loop (`controller-tasks.md` EC-2) — better than
    comparing before writing, because it never enqueues in the first place.
  - **Careful:** on the *watched* Pod type you usually want the opposite. Pod
    status changes are exactly what you care about, and `GenerationChangedPredicate`
    would filter them all out.

- [ ] **Task 3.4 — Prove it end to end**
  ```bash
  make install && make run &
  kubectl create ns team-a
  kubectl apply -f - <<'EOF'
  apiVersion: capacity.lab.example.com/v1alpha1
  kind: GPUBudget
  metadata: {name: team-a-budget, namespace: team-a}
  spec: {maxGPUs: 4}
  EOF
  kubectl run g1 -n team-a --image=busybox --overrides='{"spec":{"containers":[{"name":"c","image":"busybox","command":["sleep","3600"],"resources":{"limits":{"nvidia.com/gpu":"6"}}}]}}'
  kubectl get gb -n team-a
  ```
  - Verify: `Allocated` becomes 6, `Ready` becomes `False`.

---

## Level 4 — Finalizers and cleanup

- [ ] **Task 4.1 — The standard block**
  ```go
  const finalizer = "capacity.lab.example.com/cleanup"

  if !budget.DeletionTimestamp.IsZero() {
      if controllerutil.ContainsFinalizer(&budget, finalizer) {
          if err := r.cleanupExternal(ctx, &budget); err != nil {
              return ctrl.Result{}, err          // retry; do NOT remove yet
          }
          controllerutil.RemoveFinalizer(&budget, finalizer)
          if err := r.Update(ctx, &budget); err != nil {
              return ctrl.Result{}, err
          }
      }
      return ctrl.Result{}, nil                  // terminating: stop here
  }
  if controllerutil.AddFinalizer(&budget, finalizer) {
      if err := r.Update(ctx, &budget); err != nil {
          return ctrl.Result{}, err
      }
  }
  ```
  - **Rule:** only add a finalizer if you have **external** state to clean up
    (a cloud resource, an external registration). Anything inside the cluster
    should use ownerReferences and let the garbage collector do it.

- [ ] **Task 4.2 — Break it deliberately**
  - Do: make `cleanupExternal` always return an error, create a budget, delete it.
  - Verify: it hangs in terminating forever; the namespace won't delete either.
  - Unstick: `kubectl patch gb <n> -n team-a -p '{"metadata":{"finalizers":null}}' --type=merge`
  - Learn: this is `api-machinery-tasks.md` EC-3, self-inflicted. Feel it once in
    a kind cluster so you never ship it.

- [ ] **Task 4.3 — Cleanup must be idempotent**
  - Learn: it will run more than once — retries, restarts, resyncs. "Already gone"
    must be a success, not an error.

---

## Level 5 — Testing and shipping

- [ ] **Task 5.1 — envtest**
  - Do: `make test`
  - Learn: envtest runs a **real API server and etcd**, no kubelet. Your controller
    talks to a genuine API — schema validation, subresources, watches all real —
    but nothing ever runs. Perfect for controllers, useless for anything needing a
    pod to actually start.

- [ ] **Task 5.2 — Write a real test**
  ```go
  It("marks the budget exceeded", func() {
      Expect(k8sClient.Create(ctx, budget)).To(Succeed())
      Expect(k8sClient.Create(ctx, gpuPod(6))).To(Succeed())
      Eventually(func() int64 {
          var got capacityv1alpha1.GPUBudget
          _ = k8sClient.Get(ctx, key, &got)
          return got.Status.AllocatedGPUs
      }, "10s", "250ms").Should(Equal(int64(6)))
  })
  ```
  - **Rule:** always `Eventually`, never a bare assert. Reconciliation is
    asynchronous by definition; a synchronous assertion is a flake you haven't met
    yet.

- [ ] **Task 5.3 — Deploy it**
  ```bash
  make docker-build docker-push IMG=<registry>/gpubudget:v0.1.0
  make deploy IMG=<registry>/gpubudget:v0.1.0
  kubectl -n gpubudget-system logs deploy/gpubudget-controller-manager -c manager
  ```
  - Learn: `make deploy` applies RBAC generated from your `//+kubebuilder:rbac`
    markers. Add a new type to your reconciler and forget the marker, and you get
    a silent permission failure (`controller-tasks.md` EC-8).

- [ ] **Task 5.4 — Read the metrics you get free**
  - `controller_runtime_reconcile_total{result=}`, `..._errors_total`,
    `..._time_seconds`, plus all the `workqueue_*` series from before.
  - This is where your observability background is worth more than most operator
    authors': add a Grafana dashboard and SLOs for your own controller. Almost
    nobody does, and it's a genuinely differentiating thing to show.

---

## Level 6 — Where this goes next

- [ ] **Task 6.1 — Enforcement, not just reporting**
  - Add a validating webhook that rejects Pods pushing a namespace over budget.
  - Learn: reporting is a controller; **prevention is admission**. Different
    extension point, different failure modes — a broken webhook with
    `failurePolicy: Fail` can block all pod creation cluster-wide.

- [ ] **Task 6.2 — Real utilization, not requests**
  - Feed DCGM metrics in and compare *requested* against *actually used* GPUs.
  - Learn: this is the gap that makes GPU FinOps a market — `resources-tasks.md`
    EC-7, at fleet scale. You already understand the DCGM semantics; this is where
    that knowledge becomes an artifact.

- [ ] **Task 6.3 — Publish it**
  - A working operator with tests, RBAC, metrics and a README is rung-2 evidence
    with nothing to caveat. Write the build-log: what you got wrong, what the
    resync taught you, why the finalizer wedged your cluster. Build-logs are
    honest by construction and they're read by the people you want to be hired by.

---

## Level 7 — Edge Cases & Production Nuances

### EC-1 — The cached client doesn't see your own write

- **Trap:** you `Create` then `Get` and it isn't there.
- **Why:** `r.Client` reads from the manager's cache, populated by a watch
  (`api-machinery-tasks.md` EC-2).
- **Fix:** use the object the API already returned. If you truly need fresh, get an
  uncached reader via `mgr.GetAPIReader()`.

---

### EC-2 — `Owns` silently does nothing

- **Trap:** you create ConfigMaps, declare `Owns(&corev1.ConfigMap{})`, and changes
  to them never trigger a reconcile.
- **Why:** `Owns` maps via ownerReference. You forgot
  `controllerutil.SetControllerReference(&budget, cm, r.Scheme)`.
- **Rule:** `Owns` is a *consequence* of ownership, not a declaration of it.

---

### EC-3 — Status update conflicts under load

- **Trap:** logs full of `the object has been modified`.
- **Why:** optimistic concurrency (`api-machinery-tasks.md` Task 4.5), and the
  reconcile is slow enough that the object moves underneath it.
- **Fix:** return the error and let the backoff handle it, or use
  `retry.RetryOnConflict` for the update alone. Do **not** re-run the whole
  reconcile in a loop.

---

### EC-4 — Reconciling on your own status writes

- **Trap:** infinite reconciles, `reconcile_total` climbing with an idle cluster.
- **Fix:** `predicate.GenerationChangedPredicate{}` on the primary type
  (Task 3.3). Confirm by logging `generation` vs `resourceVersion`: a climbing
  resourceVersion with a static generation means status-only churn.

---

### EC-5 — A map function that lists the whole cluster

- **Trap:** a Pod-change map function doing an unscoped `List` on every pod event.
  At 50k pods that's a cluster-wide list per event.
- **Fix:** scope to the namespace (as in Task 3.1), and add a field index:
  ```go
  mgr.GetFieldIndexer().IndexField(ctx, &capacityv1alpha1.GPUBudget{}, "spec.someRef", ...)
  ```

---

### EC-6 — CRD schema changes that can't take effect

- **Trap:** you add a field, `make manifests`, redeploy — and the API server
  rejects it as unknown.
- **Why:** you didn't re-apply the CRD, only the controller. The CRD is the schema;
  the controller binary is not.
- **Rule:** `make install` (CRDs) and `make deploy` (controller) are separate
  steps. Removing a field is worse — data in etcd is pruned against the schema, so
  a removed field is **destroyed on next write**.

---

### EC-7 — Leader election makes the second replica look broken

- **Trap:** you scale to 2 and one pod logs nothing.
- **Why:** working exactly as intended (`controller-tasks.md` Task 5.1). Replicas
  are for failover, not throughput.
- **Rule:** if you need throughput, raise `MaxConcurrentReconciles` — the queue
  already guarantees one key is never processed twice concurrently, so this is
  safe by construction.

---

## Cheat sheet

```bash
kubebuilder init --domain X --repo Y
kubebuilder create api --group G --version v1alpha1 --kind K
make manifests generate     # regenerate CRDs + deepcopy from markers
make install                # apply CRDs
make run                    # run controller locally against current context
make test                   # envtest
make docker-build docker-push deploy IMG=...
kubectl patch <cr> -p '{"metadata":{"finalizers":null}}' --type=merge   # unstick
```

```go
ctrl.NewControllerManagedBy(mgr).For(&T{}).Owns(&U{}).
    Watches(&V{}, handler.EnqueueRequestsFromMapFunc(f)).
    WithOptions(controller.Options{MaxConcurrentReconciles: 4}).Complete(r)

client.IgnoreNotFound(err)
r.Status().Update(ctx, obj)                        // status subresource only
meta.SetStatusCondition(&obj.Status.Conditions, c)
controllerutil.SetControllerReference(owner, obj, r.Scheme)
controllerutil.AddFinalizer / RemoveFinalizer / ContainsFinalizer
return ctrl.Result{RequeueAfter: time.Minute}, nil
```

## Mental model to lock in

- **The Manager owns everything shared**; you own only `Reconcile`. Everything it
  provides, you built by hand in `controller-tasks.md`.
- **`For` = my type. `Owns` = things I created (via ownerReference).
  `Watches` + map = anything else.** Most first operators are missing the third.
- **Status goes through `Status()`.** Silently dropped otherwise.
- **`GenerationChangedPredicate` on the primary type** is the structural cure for
  self-triggered reconcile loops — but never put it on a watched type whose
  *status* is what you care about.
- **Finalizers are for external state only.** In-cluster children get
  ownerReferences and free garbage collection.
- **Everything is idempotent or it's broken.** Retries, resyncs and restarts will
  all re-run your code with no warning.

```text
   ┌──────────────── Manager ────────────────┐
   │  cache (shared informers)               │
   │  client (cache-backed reads, live writes)│
   │  leader election · metrics · health      │
   └───────────────┬──────────────────────────┘
                   │
   For(GPUBudget) ──┤
   Watches(Pod) ────┼──▶ workqueue ──▶ Reconcile(ctx, req)
   Owns(ConfigMap) ─┘                     │
                                          ├─ Get CR         (cache)
                                          ├─ List Pods      (cache)
                                          ├─ compute totals
                                          ├─ Status().Update()
                                          └─ return Result{} / err ─▶ backoff
```
