# Scheduling Constraints Tasks — where your Pod lands and why

Track A module 8, and the bridge into Track B/C. Do after `resources-tasks.md`.
Read alongside: `../kubernetes/09-kube-scheduler-internals.md` §1–4.

This is the last consumer-level module and the most important one for GPU work —
every GPU workload is a placement problem before it is anything else.

> The one idea: **scheduling is filter then score.** Predicates eliminate nodes
> that *cannot* work; priorities rank the survivors. Everything in this file is
> either a hard filter or a soft score, and knowing which is which explains every
> "why is my pod Pending" and every "why did it land there."
>
> ```
>   all nodes
>      │ FILTER (hard) — resources, nodeSelector, requiredAffinity, taints,
>      │                  volume topology, node conditions
>      ▼
>   feasible nodes
>      │ SCORE (soft)  — preferredAffinity, spread, image locality, balance
>      ▼
>   highest score ──▶ BIND (write spec.nodeName)
>
>   zero feasible nodes ──▶ Pending + FailedScheduling
> ```

Setup: `kubectl create ns sched-lab`. A multi-node cluster helps a lot:
`kind create cluster --config` with 3 workers.

---

## Level 0 — Orientation

1. `kubectl get nodes --show-labels` — note the built-in
   `kubernetes.io/hostname`, `topology.kubernetes.io/zone`, `node-role...` labels.
2. `kubectl explain pod.spec.affinity`
3. Scheduling is just a **write to `spec.nodeName`**:
   ```bash
   kubectl get pod X -o jsonpath='{.spec.nodeName}{"\n"}'
   ```
   Everything else is deciding what to put there. An unscheduled Pod has it empty.
4. Label some nodes to work with:
   ```bash
   kubectl label node <n1> accelerator=gpu tier=premium
   kubectl label node <n2> accelerator=none tier=standard
   ```

---

## Level 1 — nodeSelector and nodeName

- [ ] **Task 1.1 — nodeSelector is a hard AND**
  ```yaml
  spec:
    nodeSelector: {accelerator: gpu}
  ```
  - Verify: lands only on `n1`. Add `tier: nonexistent` → Pending forever.
  - Learn: all keys must match. Exact equality only — no operators, no negation.
    Simple, and the reason `nodeAffinity` exists.

- [ ] **Task 1.2 — nodeName bypasses the scheduler entirely**
  - Do: set `spec.nodeName: <n2>` directly with resources larger than the node has.
  - Verify: the kubelet accepts it and then fails it with `OutOfcpu` — there was
    no scheduling decision to prevent it.
  - Learn: `nodeName` skips all filtering. Never use it outside debugging; it's how
    you overcommit a node past its capacity.

---

## Level 2 — Node affinity

- [ ] **Task 2.1 — required (hard filter)**
  ```yaml
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchExpressions:
          - {key: accelerator, operator: In, values: [gpu, tpu]}
  ```
  - Learn: operators `In`, `NotIn`, `Exists`, `DoesNotExist`, `Gt`, `Lt`. This is
    nodeSelector with real expressions, including negation.

- [ ] **Task 2.2 — nodeSelectorTerms are OR, matchExpressions are AND**
  - Do: add a second entry to `nodeSelectorTerms`.
  - Learn: **terms OR together, expressions within a term AND together.** Getting
    this backwards is the most common affinity bug and it fails as "Pending" with
    no hint.

- [ ] **Task 2.3 — preferred (soft score)**
  ```yaml
  preferredDuringSchedulingIgnoredDuringExecution:
  - weight: 100
    preference:
      matchExpressions: [{key: tier, operator: In, values: [premium]}]
  ```
  - Verify: prefers `n1`, but still schedules elsewhere when `n1` is full.
  - Learn: weight 1–100, summed across matching terms into the node's score.
    **Preferred never causes Pending.** If you're Pending, a preferred rule is
    never the cause.

- [ ] **Task 2.4 — `IgnoredDuringExecution` means what it says**
  - Do: schedule a Pod with required affinity on `accelerator=gpu`, then
    `kubectl label node <n1> accelerator=none --overwrite`.
  - Verify: the Pod keeps running.
  - Learn: affinity is evaluated **at scheduling time only**. Nothing re-evaluates
    or evicts. There is no `RequiredDuringExecution` — it has been "planned" for
    years. If you need enforcement over time, that's a controller you write
    (Track B) or the Descheduler.

---

## Level 3 — Taints and tolerations

- [ ] **Task 3.1 — Taint a node**
  ```bash
  kubectl taint node <n1> dedicated=gpu:NoSchedule
  ```
  - Verify: new Pods avoid `n1`; existing Pods stay.
  - Learn: **taints repel, tolerations permit.** The inverse of affinity — affinity
    is the Pod choosing nodes, taints are the node rejecting Pods.

- [ ] **Task 3.2 — The three effects**
  - `NoSchedule` — won't place new Pods; existing untouched
  - `PreferNoSchedule` — soft, a scoring penalty
  - `NoExecute` — won't place **and evicts** running Pods that don't tolerate it
  - Do: apply `NoExecute` to a node with Pods on it and watch them go.

- [ ] **Task 3.3 — Tolerate it**
  ```yaml
  tolerations:
  - {key: dedicated, operator: Equal, value: gpu, effect: NoSchedule}
  ```
  - Learn: `operator: Exists` with no value tolerates any value for that key.
    A toleration with **no key and `Exists`** tolerates *everything* — that's what
    system DaemonSets use.

- [ ] **Task 3.4 — A toleration is permission, not attraction**
  - Do: give a Pod the toleration but no affinity.
  - Verify: it may land anywhere, including untainted nodes.
  - Learn: **the classic dedicated-hardware mistake.** To *reserve* GPU nodes you
    need both: taint + toleration (keeps others off) **and** nodeSelector/affinity
    (keeps yours on). One without the other doesn't work.

- [ ] **Task 3.5 — tolerationSeconds**
  ```yaml
  - {key: node.kubernetes.io/not-ready, operator: Exists, effect: NoExecute, tolerationSeconds: 300}
  ```
  - Do: `kubectl get pod X -o jsonpath='{.spec.tolerations}' | jq` on any Pod.
  - Learn: Kubernetes **injects** these automatically — 300s of tolerance for
    `not-ready` and `unreachable`. That's why Pods don't move the instant a node
    blips, and why failover takes ~5 minutes by default.

- [ ] **Task 3.6 — Built-in taints**
  - Do: `kubectl describe node <control-plane> | grep -i taint`
  - Learn: `node-role.kubernetes.io/control-plane:NoSchedule` is why workloads
    avoid control-plane nodes. Others appear automatically under pressure:
    `disk-pressure`, `memory-pressure`, `pid-pressure`, `unschedulable` (cordon).

---

## Level 4 — Pod affinity and anti-affinity

- [ ] **Task 4.1 — Spread replicas across nodes**
  ```yaml
  affinity:
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
      - labelSelector:
          matchExpressions: [{key: app, operator: In, values: [web]}]
        topologyKey: kubernetes.io/hostname
  ```
  - Do: scale to more replicas than nodes.
  - Verify: extras go Pending — one per node is a hard rule.
  - Learn: **`topologyKey` defines what "together" means.** `hostname` = same node;
    `topology.kubernetes.io/zone` = same zone.

- [ ] **Task 4.2 — Prefer, don't require**
  - Do: switch to `preferred` with weight 100.
  - Verify: spreads when it can, doubles up when it must. Almost always what you
    actually want.

- [ ] **Task 4.3 — Co-location with podAffinity**
  - Learn: the inverse — schedule *near* Pods matching a selector. Used for cache
    locality, or keeping a workload in the same zone as its data.

- [ ] **Task 4.4 — The cost**
  - Learn: pod affinity is **O(pods × nodes)** to evaluate — the scheduler must
    check every candidate node against every matching Pod. At thousands of nodes
    it measurably slows scheduling. `09-kube-scheduler-internals.md` covers the
    performance work here; this is why `topologySpreadConstraints` was introduced.

---

## Level 5 — Topology spread and priority

- [ ] **Task 5.1 — topologySpreadConstraints**
  ```yaml
  topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: ScheduleAnyway     # or DoNotSchedule
    labelSelector:
      matchLabels: {app: web}
  ```
  - Learn: `maxSkew` bounds the difference between the most and least populated
    topology domain. Strictly more expressive than anti-affinity — which can only
    say "never together" — and much cheaper to evaluate.

- [ ] **Task 5.2 — DoNotSchedule vs ScheduleAnyway**
  - Do: set `DoNotSchedule` and scale beyond what the skew permits.
  - Verify: Pending.
  - Learn: `DoNotSchedule` is a hard filter, `ScheduleAnyway` is a score. Same
    hard/soft split as everything else in this file.

- [ ] **Task 5.3 — PriorityClass**
  ```bash
  kubectl create priorityclass high --value=1000000
  kubectl get priorityclass
  ```
  - Learn: two ship by default — `system-cluster-critical` and
    `system-node-critical`. Higher value wins.

- [ ] **Task 5.4 — Watch a preemption**
  - Do: fill a node with low-priority Pods, then create a high-priority Pod that
    doesn't fit.
  - Verify: `kubectl get events -n sched-lab` → `Preempted` on the victims.
  - Learn: the scheduler picks victims to make room, deletes them **gracefully**,
    and the pending Pod gets a nomination. It's not instant — victims get their
    termination grace period.

- [ ] **Task 5.5 — preemptionPolicy: Never**
  - Learn: high priority for *queue position* without evicting anything. Right for
    important-but-not-urgent batch work — it jumps the queue but never kills a
    running job. Directly relevant to GPU batch scheduling.

- [ ] **Task 5.6 — PodDisruptionBudget**
  ```bash
  kubectl create pdb web-pdb -n sched-lab --selector=app=web --min-available=2
  kubectl drain <node> --ignore-daemonsets
  ```
  - Verify: the drain blocks rather than violating the budget.
  - Learn: PDBs constrain **voluntary** disruption (drain, eviction API) only.
    A node crash ignores them entirely — they are not a reliability guarantee.

---

## Level 6 — Edge Cases & Production Nuances

### EC-1 — "0/5 nodes are available" — read the whole line

- **Diagnose:** `kubectl describe pod X | tail -20`. The message enumerates every
  reason with counts:
  ```
  0/5 nodes are available: 2 Insufficient cpu, 2 node(s) had untolerated taint
  {dedicated: gpu}, 1 node(s) didn't match Pod's node affinity/selector.
  ```
- **Rule:** the counts must sum to the node total. That breakdown tells you exactly
  which constraint to relax — it's the single most useful diagnostic in scheduling
  and most people skim past it.

---

### EC-2 — Toleration without affinity doesn't reserve anything

- **Trap:** you taint GPU nodes and tolerate the taint, then find your GPU job on a
  CPU node while a cheap job sits on the GPU node.
- **Why:** the taint keeps *others* off; nothing pulls *you* on. And the cheap job
  presumably also tolerates it, or landed before the taint.
- **Fix:** taint + toleration **and** nodeSelector/affinity. Both, always.
- **Rule:** this is the most expensive misunderstanding in this file — it wastes
  the most expensive hardware you own.

---

### EC-3 — Pending because of volume topology, not CPU

- **Trap:** `1 node(s) had volume node affinity conflict`.
- **Why:** the PVC is already bound to a PV in zone A; the Pod can only fit in
  zone B. Storage pinned the Pod before the scheduler saw it.
- **Fix:** `WaitForFirstConsumer` (`config-storage-tasks.md` Task 4.2) so binding
  happens *after* placement.
- **Rule:** storage constrains scheduling. On stateful workloads it is usually the
  real reason, and it never mentions CPU.

---

### EC-4 — Anti-affinity silently caps your replica count

- **Trap:** an HPA scales to 20 and only 5 Pods run.
- **Why:** required anti-affinity on `hostname` with 5 nodes means a hard ceiling
  of 5.
- **Diagnose:** Pending Pods with `didn't match pod anti-affinity rules`.
- **Fix:** `preferred`, or `topologySpreadConstraints` with `maxSkew`.
- **Rule:** required anti-affinity on hostname sets `maxReplicas = nodeCount`. Very
  few people notice until autoscaling exposes it.

---

### EC-5 — Preemption cascade

- **Trap:** one high-priority Pod evicts several, which reschedule and evict
  others, and the cluster churns for minutes.
- **Fix:** few, well-separated priority tiers; `preemptionPolicy: Never` for batch;
  PDBs on anything that matters.
- **Rule:** priority is a *global ordering*. Inventing many closely-spaced classes
  makes cluster behaviour unpredictable — three tiers is usually enough.

---

### EC-6 — A cordoned node still runs its Pods

- **Trap:** you `cordon` a node and expect it to empty.
- **Why:** cordon adds `node.kubernetes.io/unschedulable:NoSchedule` — new Pods
  only. `drain` is what evicts.
- **Rule:** cordon = stop incoming. Drain = cordon + evict, honouring PDBs.

---

### EC-7 — DaemonSet Pods ignore your PDB during drain

- **Trap:** `drain` refuses to proceed without `--ignore-daemonsets`.
- **Why:** DaemonSet Pods are immediately recreated on the same node, so evicting
  them is pointless.
- **Rule:** always `--ignore-daemonsets`, and remember DaemonSets provide no
  drain-time safety — which is why node agents need to tolerate everything
  (`workload-controllers-tasks.md` EC-3).

---

### EC-8 — Extended resources can't be preferred or fractional

- **Trap:** you try `nvidia.com/gpu: 0.5`, or a soft preference for GPU capacity.
- **Why:** extended resources are **integers, and request must equal limit**
  (`resources-tasks.md` Task 6.6). There is no burst, no fraction, no soft ask.
- **Fix:** fractional GPU needs MIG, time-slicing or MPS — configured at the
  device-plugin layer, which then advertises more integer devices. That's
  `device-plugin-tasks.md`, and it's the mechanism behind every "fractional GPU"
  product on the market.

---

## Cheat sheet

```bash
kubectl get nodes --show-labels
kubectl label node N key=value [--overwrite]
kubectl taint node N key=value:NoSchedule        # add
kubectl taint node N key-                        # remove (trailing dash)
kubectl describe node N | grep -i taint
kubectl describe pod P | tail -20                # the FailedScheduling breakdown
kubectl get pod P -o jsonpath='{.spec.nodeName}{"\n"}'
kubectl get pod P -o jsonpath='{.spec.tolerations}' | jq
kubectl get priorityclass
kubectl create pdb NAME --selector=app=x --min-available=2
kubectl cordon N        # stop new pods
kubectl drain N --ignore-daemonsets --delete-emptydir-data
kubectl uncordon N
kubectl get events -n NS --field-selector reason=Preempted
```

## Mental model to lock in

- **Filter then score.** Hard constraints make Pods Pending; soft ones only change
  preference. Identify which kind you're looking at and the behaviour follows.
- **Affinity = Pod chooses node. Taint = node rejects Pod.** Opposite directions.
- **Toleration is permission, not attraction.** Reserving hardware needs *both*
  taint/toleration and affinity.
- **`nodeSelectorTerms` OR; `matchExpressions` AND.**
- **`IgnoredDuringExecution` is the only option.** Nothing re-checks placement
  after binding — enforcement over time is a controller you write.
- **`topologySpreadConstraints` supersedes anti-affinity** — more expressive,
  cheaper to evaluate.
- **PDBs only constrain voluntary disruption.** A crashed node ignores them.
- **Extended resources are integer, request == limit, no soft form.**
- **Read the whole FailedScheduling message.** The counts sum to the node total and
  tell you precisely what to fix.

```text
   Pod spec                          Node
   ├── nodeSelector      ─hard─▶     labels
   ├── nodeAffinity
   │    ├── required     ─hard─▶     labels (In/NotIn/Exists/Gt/Lt)
   │    └── preferred    ─soft─▶     score += weight
   ├── tolerations       ─hard─◀     taints (NoSchedule/PreferNoSchedule/NoExecute)
   ├── podAffinity       ─both─▶     other pods, grouped by topologyKey
   ├── topologySpread    ─both─▶     maxSkew across a topology domain
   └── priorityClassName ─────▶      preemption order

   RESERVE DEDICATED HARDWARE = taint + toleration  AND  nodeSelector/affinity
                                (keeps others off)      (keeps yours on)
```
