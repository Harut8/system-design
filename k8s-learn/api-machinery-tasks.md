# API Machinery Tasks — the layer every tool sits on

The foundation for Track B. Do this before `controller-tasks.md`. Nothing here
needs Go — it's all `kubectl` and `curl` against the API server, because the
point is to see the protocol before you see a library that wraps it.

> The one idea: **the API server is a versioned, watchable key-value store with
> validation.** Every controller, operator, scheduler and webhook is just a client
> of it. `kubectl` is not special — it is one client among many, and everything it
> does you can do over HTTP.
>
> ```
>   client ──LIST──▶  full state + resourceVersion=N
>          ──WATCH──▶ stream of changes since N ──▶ N+1, N+2, ...
>                          │
>                          └── gap? → 410 Gone → LIST again from scratch
> ```

Setup: `kind create cluster --name apilab` and `kubectl create ns api-lab`.

---

## Level 0 — Orientation

1. Start a proxy so you can talk to the API without auth plumbing:
   ```bash
   kubectl proxy --port=8001 &
   curl -s localhost:8001/api/v1/namespaces/api-lab/pods | head -20
   ```
2. See every group/version the server knows:
   ```bash
   kubectl api-versions
   kubectl api-resources --verbs=list -o wide | head -30
   ```
   Note the `VERBS` column — `get list watch create update patch delete
   deletecollection`. That set is the *entire* vocabulary. There is no other verb.
3. `kubectl get pod X -v=8` — read the actual HTTP. Every kubectl command is one
   or more of those eight verbs against a REST path.

---

## Level 1 — resourceVersion and the watch protocol

- [ ] **Task 1.1 — Find a resourceVersion**
  - Do: `kubectl get ns api-lab -o jsonpath='{.metadata.resourceVersion}{"\n"}'`
  - Learn: it's a string, not a number, and it is **opaque**. It happens to be an
    etcd revision today. You may compare for equality; you may never compare for
    ordering, do arithmetic, or persist it as meaningful.

- [ ] **Task 1.2 — Watch a resource from a point in time**
  - Do, in one terminal:
    ```bash
    RV=$(kubectl get pods -n api-lab -o jsonpath='{.metadata.resourceVersion}')
    curl -N -s "localhost:8001/api/v1/namespaces/api-lab/pods?watch=1&resourceVersion=$RV"
    ```
  - In another: `kubectl run w1 --image=busybox -n api-lab -- sleep 3600`
  - Verify: you get a stream of JSON lines with `"type":"ADDED"`, then `"MODIFIED"`
    several times as status fills in.
  - Learn: **one create produced many events.** Events are not user actions; they
    are object versions. A controller that counts events counts nothing useful.

- [ ] **Task 1.3 — LIST is a snapshot with a bookmark**
  - Do: `curl -s "localhost:8001/api/v1/namespaces/api-lab/pods" | jq '.metadata.resourceVersion'`
  - Learn: the list's `resourceVersion` is the point you can start watching from
    with **no gap and no duplicate**. That LIST-then-WATCH handshake is the entire
    basis of informers, and getting it wrong is how you lose or double-count state.

- [ ] **Task 1.4 — Make a watch fail with 410 Gone**
  - Do: watch with a deliberately ancient resourceVersion:
    ```bash
    curl -s "localhost:8001/api/v1/namespaces/api-lab/pods?watch=1&resourceVersion=1"
    ```
  - Verify: `"reason":"Expired"`, status 410.
  - Learn: etcd compacts history (~5 min by default). If your client falls behind
    or disconnects for too long, **its position no longer exists**. The only
    recovery is to LIST again and rebuild. This is why controllers must be able to
    reconstruct state from scratch at any moment — which is level-triggering,
    forced on you by the storage layer.

- [ ] **Task 1.5 — Bookmarks**
  - Do: add `&allowWatchBookmarks=true` to the Task 1.2 watch and leave it running.
  - Verify: periodic `"type":"BOOKMARK"` events with a fresh resourceVersion and
    an otherwise empty object.
  - Learn: the server is saying "nothing you care about changed, but here's a
    current position." Without these, a client watching a quiet resource holds a
    stale position and gets a 410 on reconnect for no reason.

---

## Level 2 — spec, status, and who owns which

- [ ] **Task 2.1 — The split**
  - Do: `kubectl get pod w1 -n api-lab -o json | jq 'keys'`
  - Learn: `spec` is **desired state, written by users**. `status` is **observed
    state, written by controllers**. Confusing the two is the most common design
    error in a first CRD.

- [ ] **Task 2.2 — The status subresource is a different endpoint**
  - Do: `kubectl get --raw /api/v1/namespaces/api-lab/pods/w1/status | jq '.status.phase'`
  - Learn: `/status` is a separate REST path with separate RBAC. A controller can
    be allowed to write status while forbidden from touching spec — which is
    exactly the permission split you want, and it's why CRDs should almost always
    enable the status subresource.

- [ ] **Task 2.3 — Prove the split is enforced**
  - Do: try to change status through the main endpoint:
    ```bash
    kubectl patch pod w1 -n api-lab --type=merge -p '{"status":{"phase":"Succeeded"}}'
    kubectl get pod w1 -n api-lab -o jsonpath='{.status.phase}{"\n"}'
    ```
  - Verify: still `Running`. The write was silently dropped.
  - Learn: **silently.** No error. When a subresource is enabled, writes to that
    field through the main endpoint are ignored, not rejected. Budget an hour of
    confusion for this the first time it bites you in a controller.

- [ ] **Task 2.4 — Conditions are the standard status vocabulary**
  - Do: `kubectl get pod w1 -n api-lab -o jsonpath='{range .status.conditions[*]}{.type}{"\t"}{.status}{"\n"}{end}'`
  - Learn: `type` + `status` (True/False/**Unknown**) + `reason` + `message` +
    `lastTransitionTime`. Unknown is a real, meaningful third state — "I haven't
    observed this yet" is different from "it's false." Use this shape in your CRDs;
    tooling everywhere expects it.

---

## Level 3 — Ownership, cascading deletion, finalizers

- [ ] **Task 3.1 — Read an ownerReference**
  - Do:
    ```bash
    kubectl create deploy own-demo --image=nginx -n api-lab
    kubectl get rs -n api-lab -o jsonpath='{.items[0].metadata.ownerReferences}' | jq
    kubectl get pod -n api-lab -l app=own-demo -o jsonpath='{.items[0].metadata.ownerReferences}' | jq
    ```
  - Learn: Deployment → ReplicaSet → Pod, each child pointing *up* at its parent
    with `uid`, `controller: true`, `blockOwnerDeletion: true`. There is no
    downward list. The hierarchy exists only as back-pointers, and garbage
    collection walks them.

- [ ] **Task 3.2 — Orphan a child on purpose**
  - Do: `kubectl delete deploy own-demo -n api-lab --cascade=orphan`
  - Verify: the ReplicaSet and Pods survive.
  - Learn: cascading deletion is a **policy applied by the garbage collector**, not
    an intrinsic property of ownership. `--cascade=background` (default),
    `foreground`, `orphan` are three genuinely different behaviours.
  - Clean up: `kubectl delete rs -n api-lab --all`

- [ ] **Task 3.3 — The uid trap**
  - Do: create a ConfigMap, note its `uid`. Delete and recreate it with the same
    name. Compare uids.
  - Learn: **name is not identity — uid is.** An ownerReference with a stale uid
    makes the GC delete the child immediately, because it resolves the owner as
    "gone." Same name, different object.

- [ ] **Task 3.4 — Block a deletion with a finalizer**
  - Do:
    ```bash
    kubectl create cm fin-demo -n api-lab --from-literal=k=v
    kubectl patch cm fin-demo -n api-lab --type=merge \
      -p '{"metadata":{"finalizers":["example.com/hold"]}}'
    kubectl delete cm fin-demo -n api-lab &      # hangs
    kubectl get cm fin-demo -n api-lab -o jsonpath='{.metadata.deletionTimestamp}{"\n"}'
    ```
  - Verify: the object still exists, but now has a `deletionTimestamp`.
  - Learn: **delete is not delete.** It sets `deletionTimestamp` and waits for
    every finalizer to be removed. The object is now in "terminating" — readable,
    un-updatable in spec, and yours to clean up after.

- [ ] **Task 3.5 — Release it**
  - Do: `kubectl patch cm fin-demo -n api-lab --type=merge -p '{"metadata":{"finalizers":null}}'`
  - Verify: it vanishes immediately.
  - Learn: this is the *entire* protocol your operator implements for external
    cleanup — "if deletionTimestamp is set, do my teardown, then remove my
    finalizer." Forget the removal and you've built a resource nobody can delete
    without manual patching. This is the single most common way an operator
    wedges a production cluster.

---

## Level 4 — Patching, and why there are four kinds

- [ ] **Task 4.1 — Strategic merge vs JSON merge on a list**
  - Do:
    ```bash
    kubectl create deploy patch-demo --image=nginx -n api-lab
    kubectl patch deploy patch-demo -n api-lab --type=merge \
      -p '{"spec":{"template":{"spec":{"containers":[{"name":"sidecar","image":"busybox"}]}}}}'
    kubectl get deploy patch-demo -n api-lab -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{" "}{end}{"\n"}'
    ```
  - Verify: **only `sidecar`** — nginx was replaced, not appended.
  - Do the same with `--type=strategic` on a fresh deployment.
  - Verify: **both** containers.
  - Learn: strategic merge knows Kubernetes' schema (it has `patchMergeKey: name`
    on the containers list); JSON merge is generic and replaces arrays wholesale.
    Using the wrong one silently destroys sibling entries.

- [ ] **Task 4.2 — JSON patch for precision**
  - Do:
    ```bash
    kubectl patch deploy patch-demo -n api-lab --type=json \
      -p '[{"op":"replace","path":"/spec/replicas","value":3}]'
    ```
  - Learn: RFC 6902 — explicit ops (`add`/`remove`/`replace`/`test`) against
    paths. `test` gives you compare-and-swap. Verbose, and the only one that can
    express "remove exactly this array index."

- [ ] **Task 4.3 — Server-side apply and field managers**
  - Do:
    ```bash
    kubectl apply --server-side --field-manager=me -f - <<'EOF'
    apiVersion: apps/v1
    kind: Deployment
    metadata: {name: ssa-demo, namespace: api-lab}
    spec:
      replicas: 2
      selector: {matchLabels: {app: ssa}}
      template:
        metadata: {labels: {app: ssa}}
        spec: {containers: [{name: c, image: nginx}]}
    EOF
    kubectl get deploy ssa-demo -n api-lab --show-managed-fields -o yaml | grep -A20 managedFields
    ```
  - Learn: the server now records **which manager owns which field**. This is how
    a controller and a human can both edit one object without silently clobbering
    each other.

- [ ] **Task 4.4 — Cause a field-management conflict**
  - Do: apply the same object with a different manager and a different replica count:
    ```bash
    kubectl apply --server-side --field-manager=other -f - <<'EOF'
    apiVersion: apps/v1
    kind: Deployment
    metadata: {name: ssa-demo, namespace: api-lab}
    spec: {replicas: 5, selector: {matchLabels: {app: ssa}}, template: {metadata: {labels: {app: ssa}}, spec: {containers: [{name: c, image: nginx}]}}}
    EOF
    ```
  - Verify: `Apply failed with 1 conflict: conflict with "me"`.
  - Learn: this is the *good* failure. Add `--force-conflicts` to take ownership.
    In a controller, always set a stable field manager name and use SSA — it's the
    only patch type that makes concurrent writers safe by construction.

- [ ] **Task 4.5 — Optimistic concurrency**
  - Do: get an object's resourceVersion, have something else modify it, then
    update using your stale copy:
    ```bash
    kubectl get deploy ssa-demo -n api-lab -o json > /tmp/d.json
    kubectl scale deploy ssa-demo -n api-lab --replicas=7
    kubectl replace -f /tmp/d.json
    ```
  - Verify: `the object has been modified; please apply your changes to the latest version`.
  - Learn: `metadata.resourceVersion` in a write is a **compare-and-swap token**.
    This is why controllers must tolerate conflict errors and simply requeue —
    a conflict is normal, not exceptional.

---

## Level 5 — Efficiency and the API server's limits

- [ ] **Task 5.1 — Pagination**
  - Do: `kubectl get --raw '/api/v1/pods?limit=2' | jq '.metadata.continue'`
  - Then: `kubectl get --raw '/api/v1/pods?limit=2&continue=<token>' | jq '.items[].metadata.name'`
  - Learn: the continue token embeds a resourceVersion, so a paged LIST is
    consistent across pages. It also expires — a slow consumer gets a 410 mid-list.

- [ ] **Task 5.2 — Field and label selectors are server-side**
  - Do:
    ```bash
    kubectl get pods -A --field-selector spec.nodeName=<node> -v=6 2>&1 | grep GET
    ```
  - Learn: the filter went in the URL. Filtering client-side means transferring
    every object and discarding most — which is how a controller melts an API
    server at scale. Only some fields are indexed for field selectors; the rest
    are rejected.

- [ ] **Task 5.3 — API Priority and Fairness**
  - Do:
    ```bash
    kubectl get flowschemas
    kubectl get prioritylevelconfigurations
    ```
  - Learn: the server classifies requests into flows and fair-shares them. A
    badly-behaved controller gets throttled rather than taking the cluster down —
    but it also means **your** controller can be the one getting throttled. Check
    `apiserver_flowcontrol_rejected_requests_total` when a controller mysteriously
    stalls.

- [ ] **Task 5.4 — Watch cache vs etcd**
  - Do: `kubectl get --raw '/api/v1/namespaces/api-lab/pods?resourceVersion=0' | jq '.metadata.resourceVersion'`
  - Learn: `resourceVersion=0` means "any version, served from the watch cache,
    possibly stale." Empty means "quorum read from etcd, guaranteed fresh."
    Informers use `0` deliberately — cheap and eventually correct. If your
    controller needs read-your-own-write, `0` will burn you.

---

## Level 6 — Extending the API surface

- [ ] **Task 6.1 — Define a CRD by hand**
  - Do:
    ```bash
    kubectl apply -f - <<'EOF'
    apiVersion: apiextensions.k8s.io/v1
    kind: CustomResourceDefinition
    metadata: {name: widgets.lab.example.com}
    spec:
      group: lab.example.com
      names: {plural: widgets, singular: widget, kind: Widget, shortNames: [wd]}
      scope: Namespaced
      versions:
      - name: v1alpha1
        served: true
        storage: true
        subresources: {status: {}}
        schema:
          openAPIV3Schema:
            type: object
            properties:
              spec:
                type: object
                required: [size]
                properties:
                  size: {type: integer, minimum: 1, maximum: 10}
              status:
                type: object
                properties:
                  observedSize: {type: integer}
        additionalPrinterColumns:
        - {name: Size, type: integer, jsonPath: .spec.size}
        - {name: Observed, type: integer, jsonPath: .status.observedSize}
    EOF
    ```
  - Verify: `kubectl api-resources | grep widgets`, then `kubectl get widgets`.
  - Learn: you just added a first-class type. It gets the same eight verbs, the
    same watch stream, the same RBAC, the same `kubectl` support as a Pod.

- [ ] **Task 6.2 — Validation is real**
  - Do: create a Widget with `size: 50`.
  - Verify: rejected by OpenAPI schema validation, `spec.size ... should be less
    than or equal to 10`.
  - Learn: structural schemas are enforced by the API server before storage. Push
    as much validation here as possible — it's free, it's synchronous, and it
    doesn't need a webhook.

- [ ] **Task 6.3 — CEL validation rules**
  - Add to the `spec` schema:
    ```yaml
    x-kubernetes-validations:
    - rule: "self.size % 2 == 0"
      message: "size must be even"
    ```
  - Verify: odd sizes now rejected.
  - Learn: CEL covers cross-field and transition rules (`oldSelf`) that OpenAPI
    can't express. This removed the need for a validating webhook in a large
    fraction of real operators — reach for it before writing one.

- [ ] **Task 6.4 — Confirm the status subresource behaves**
  - Do: create a valid Widget, then try `kubectl patch ... -p '{"status":{"observedSize":3}}'`
    without `--subresource=status`.
  - Verify: dropped silently (Task 2.3, now on your own type).
  - Then: `kubectl patch widget <n> -n api-lab --subresource=status --type=merge -p '{"status":{"observedSize":3}}'`
  - Learn: this is the endpoint your controller will write to, and the only one.

---

## Level 7 — Edge Cases & Production Nuances

### EC-1 — Watch events are hints, not facts

- **Trap:** your controller increments a counter on each `DELETED` event and the
  number drifts.
- **Why:** events can be coalesced (two rapid updates → one event), replayed after
  a relist, or lost entirely across a 410. The watch stream guarantees *eventual
  convergence of state*, never *exactly-once delivery of transitions*.
- **Rule:** never derive state from the event. Re-read and recompute. If your
  logic can't be expressed as "given the current world, make it right," it's wrong.

---

### EC-2 — `resourceVersion=0` reads can be stale, including your own write

- **Trap:** you create an object, immediately list with a cached client, and don't
  see it. You add a `sleep`. It "fixes" it.
- **Why:** informer-backed reads come from a local cache populated by a watch.
  Your write went to etcd; the cache hasn't caught up. controller-runtime's
  default client reads from cache.
- **Fix:** don't read back what you just wrote — you already have the object the
  API returned. Where you genuinely need freshness, use an uncached reader.
- **Rule:** a `sleep` that fixes a controller bug means you have a consistency
  misunderstanding, not a timing problem.

---

### EC-3 — Finalizers outlive the controller that added them

- **Trap:** you uninstall your operator. Every CR with your finalizer is now
  undeletable, forever, and so is the namespace containing them.
- **Diagnose:** `kubectl get ns stuck -o jsonpath='{.spec.finalizers}'` and
  `kubectl get <cr> -o json | jq '.metadata.finalizers'`.
- **Escape hatch:** `kubectl patch <cr> -p '{"metadata":{"finalizers":null}}' --type=merge`
- **Rule:** ship your operator's uninstall path *and test it*. A finalizer is a
  promise that something will run later; if the something is gone, the promise
  deadlocks. This is the most common way an operator ruins someone's afternoon.

---

### EC-4 — Deleting a namespace does not delete cluster-scoped children

- **Trap:** operator cleanup leaks ClusterRoleBindings, CRDs, webhooks.
- **Why:** ownerReferences **cannot** cross from a namespaced owner to a
  cluster-scoped dependent. The GC ignores such references (and may treat the
  object as orphaned).
- **Rule:** cluster-scoped resources need explicit cleanup via a finalizer, not
  ownership. Ownership only flows namespace→same-namespace, or cluster→anything.

---

### EC-5 — Strategic merge patch doesn't exist for CRDs

- **Trap:** `kubectl patch widget ... --type=strategic` fails with
  `strategic merge patch format is not supported`.
- **Why:** strategic merge needs Go struct tags (`patchStrategy`, `patchMergeKey`)
  that only built-in types have. CRDs are schema-driven, not struct-driven.
- **Rule:** for custom resources use **JSON merge** (arrays replaced) or
  **server-side apply** (the right answer). This surprises everyone once.

---

### EC-6 — A controller that lists without a selector will hurt you

- **Trap:** `client.List(ctx, &podList)` in a large cluster pulls every pod into
  memory on every reconcile.
- **Why:** it's cache-backed, so it won't hit the API server — but the *informer*
  is caching every pod in the cluster, and your memory footprint is now the
  cluster's pod count.
- **Fix:** scope the manager's cache by namespace or label
  (`cache.Options{ByObject: ...}`), and always pass selectors.
- **Rule:** the cost of a controller is what its informers cache, not what its
  reconcile loop reads.

---

### EC-7 — `metadata.generation` vs `status.observedGeneration`

- **Trap:** you can't tell whether a controller has seen the latest spec.
- **Why:** `generation` increments **only on spec changes** (not status, not
  labels). A controller that records `observedGeneration` in status lets anyone
  ask "is this reconciled?" by comparing the two.
- **Rule:** set `status.observedGeneration` in every controller you write. It costs
  one line and it's the difference between a debuggable operator and a black box.

---

### EC-8 — Two controllers, one object

- **Trap:** yours and someone else's both write `spec.replicas`. They fight, and
  the object flaps forever, generating infinite reconciles and API load.
- **Diagnose:** rapidly incrementing `metadata.generation`, alternating values in
  `managedFields`.
- **Rule:** exactly one controller owns any given field. Use `controller: true` in
  the ownerReference to declare the *managing* controller (only one is permitted),
  and use SSA field managers so a conflict surfaces as an error instead of a war.

---

## Cheat sheet

```bash
kubectl proxy --port=8001 &
kubectl get pod X -v=8                                  # see the raw HTTP
kubectl api-resources --verbs=list -o wide              # every type + verbs
kubectl get --raw /api/v1/namespaces/N/pods/P/status    # subresource endpoint
kubectl get X -o jsonpath='{.metadata.resourceVersion}'
curl -N "localhost:8001/api/v1/namespaces/N/pods?watch=1&resourceVersion=$RV&allowWatchBookmarks=true"
kubectl patch X --type=merge|strategic|json -p '...'
kubectl apply --server-side --field-manager=me [--force-conflicts]
kubectl get X --show-managed-fields -o yaml
kubectl patch X --subresource=status --type=merge -p '{"status":{...}}'
kubectl delete X --cascade=orphan|background|foreground
kubectl patch X -p '{"metadata":{"finalizers":null}}' --type=merge   # unstick
```

## Mental model to lock in

- **Eight verbs, one store.** Everything — kubectl, controllers, the scheduler,
  the kubelet — is a client doing get/list/watch/create/update/patch/delete.
- **LIST gives you a position; WATCH continues from it.** A gap is unrecoverable
  by design, so every client must be able to rebuild from a full LIST. That
  constraint is *why* Kubernetes is level-triggered.
- **spec is yours, status is the controller's**, and they're different endpoints
  with different permissions. Writes to the wrong one fail silently.
- **Identity is uid, not name.** Ownership, finalizers and GC all key on uid.
- **Delete means "mark for deletion."** Finalizers hold it open; forgetting to
  remove one is how you wedge a cluster.
- **resourceVersion on write = compare-and-swap.** Conflicts are routine; requeue.
- **Prefer schema + CEL validation over webhooks.** Free, synchronous, no certs,
  no failure mode.

```text
        WRITE PATH                          READ PATH
  ┌──────────────────┐              ┌────────────────────────┐
  │ authn → authz    │              │ resourceVersion=""     │→ etcd quorum (fresh)
  │ mutating webhook │              │ resourceVersion="0"    │→ watch cache (stale)
  │ schema + CEL     │              │ LIST → RV=N            │
  │ validating hook  │              │ WATCH from N ──────────│→ ADDED/MODIFIED/
  │ → etcd (RV=N+1)  │              │   gap → 410 → re-LIST  │  DELETED/BOOKMARK
  └──────────────────┘              └────────────────────────┘
```
