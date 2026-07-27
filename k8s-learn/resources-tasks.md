# Resources Tasks — Beginner → Advanced

A hands-on ladder for learning **CPU/memory resources, QoS and quotas** using
`resources.yaml` in this folder. Do these AFTER `pod-tasks.md` (Level 5 is a
preview of this) and ideally after `deployment-tasks.md` — real sizing lives in
a Deployment's pod template, not in bare Pods.

> The one idea: **`requests` is for the scheduler, `limits` are for the kernel.**
> Requests decide *where* your pod lands and what it's guaranteed. Limits decide
> what happens when it misbehaves — and CPU and memory misbehave *differently*.
>
> ```
> requests ──▶ scheduler: "reserve this much on a node"   (a booking)
> limits   ──▶ kubelet/cgroups: "never exceed this"       (a cap)
>
>   over CPU limit    → THROTTLED  (slowed, alive)   ← compressible
>   over memory limit → OOMKilled  (killed, restarted) ← incompressible
> ```

---

## Level 0 — Orientation

1. `kubectl explain pod.spec.containers.resources` — only two children:
   `requests` and `limits`. That's the entire developer-facing API surface.
2. Mental model: **requests = what you're guaranteed, limits = what you're
   allowed**. The gap between them is *hope*, not a promise.
3. `kubectl explain pod.status.qosClass` — note it is under **status**. QoS is
   **derived** from your numbers, never set by you.
4. Know your node's ceiling before you start:
   ```bash
   kubectl get nodes -o custom-columns=\
   'NODE:.metadata.name,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory'
   ```
   Note **allocatable**, not `capacity` — the kubelet and OS reserve a slice off
   the top, so allocatable is always smaller. You schedule against allocatable.

---

## Level 1 — Beginner: the three QoS classes

- [ ] **Task 1.1 — Apply**
  - Do: `kubectl apply -f resources.yaml`
  - Note: two namespaces are created (`res-lab`, `res-lab-guarded`). Everything
    below is namespaced — add `-n res-lab` or you'll query `default` and see nothing.

- [ ] **Task 1.2 — See all three classes side by side**
  - Do:
    ```bash
    kubectl get pods -n res-lab -o custom-columns=\
    'NAME:.metadata.name,QOS:.status.qosClass,PHASE:.status.phase'
    ```
  - Verify: `qos-guaranteed → Guaranteed`, `qos-burstable → Burstable`,
    `qos-besteffort → BestEffort`.
  - Learn: you never typed the word "Guaranteed" anywhere. The class is computed
    from the *shape* of your requests/limits.

- [ ] **Task 1.3 — Break Guaranteed on purpose**
  - Edit `qos-guaranteed`: change its memory limit to `128Mi` (so request ≠ limit).
  - Do: `kubectl delete pod qos-guaranteed -n res-lab && kubectl apply -f resources.yaml`
  - Verify: it is now **Burstable**. One mismatched field on one resource on one
    container demotes the whole pod. Revert.

- [ ] **Task 1.4 — Limits imply requests**
  - Create a pod with **only** `limits: {cpu: 100m, memory: 64Mi}`, no requests.
  - Do: `kubectl get pod <name> -n res-lab -o jsonpath='{.spec.containers[0].resources}{"\n"}'`
  - Learn: Kubernetes **copied limits into requests** for you → still Guaranteed.
    The reverse is not true: setting only requests leaves limits empty (Burstable).

- [ ] **Task 1.5 — Read a node's booking ledger**
  - Do: `kubectl describe node <node> | sed -n '/Allocated resources/,/^Events/p'`
  - Learn: the percentages shown are of **requests**, not actual usage. A node can
    read "95% CPU requested" while sitting idle — that is a *booking* number.

---

## Level 2 — Limits in action: throttle vs kill

- [ ] **Task 2.1 — Exceed a MEMORY limit**
  - Do: `kubectl get pod mem-oomkill -n res-lab -w`
  - Verify: it cycles `Running → Error/OOMKilled → CrashLoopBackOff`.
  - Do:
    ```bash
    kubectl get pod mem-oomkill -n res-lab \
      -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}{"\t"}{.status.containerStatuses[0].lastState.terminated.exitCode}{"\n"}'
    ```
  - Verify: `OOMKilled` and exit code `137` (128 + SIGKILL 9).
  - Learn: the container wrote past 64Mi into a RAM-backed `emptyDir`; tmpfs bytes
    are charged to the container's memory cgroup exactly like heap allocations.

- [ ] **Task 2.2 — Exceed a CPU limit**
  - Do: `kubectl get pod cpu-throttle -n res-lab` — it is **Running**, and stays
    Running. Restart count never moves.
  - Do (needs metrics-server): `kubectl top pod cpu-throttle -n res-lab`
  - Verify: it reports ~`100m` even though the busy-loop wants a full core.
  - Learn: **the same "over the limit" produces opposite outcomes.** CPU is
    compressible so the kernel just hands out less; memory is not, so it kills.

- [ ] **Task 2.3 — Prove OOMKilled ≠ "the node ran out of memory"**
  - Do: `kubectl describe node <node> | grep -i pressure` → `MemoryPressure False`.
  - Learn: `mem-oomkill` died against **its own cgroup limit** while the node was
    perfectly healthy. Two different OOMs exist — container-limit OOM (your bug)
    and node-pressure eviction (a capacity problem). Task 4.2 covers the second.

- [ ] **Task 2.4 — Requests are not enforced**
  - `qos-burstable` requests `50m` but may burst to `500m`. Nothing stops it from
    using more than its request when the node is idle.
  - Learn: a request is a **reservation for scheduling**, not a runtime floor or
    ceiling. Only `limits` are enforced at runtime.

---

## Level 3 — Requests in action: scheduling

- [ ] **Task 3.1 — A pod that can never be scheduled**
  - Do: `kubectl get pod unschedulable -n res-lab` → `Pending`, forever, `0/1 Ready`.
  - Do: `kubectl describe pod unschedulable -n res-lab | tail -20`
  - Verify: `FailedScheduling ... 0/1 nodes are available: 1 Insufficient cpu`.
  - Learn: **Pending + FailedScheduling = a requests/capacity problem**, never a
    crash. There is no container to debug — nothing was ever started.
    (Same lesson as `pod-tasks.md` EC-10, seen from the resources side.)

- [ ] **Task 3.2 — Find the real ceiling**
  - Lower `unschedulable`'s cpu request by halves (`100` → `8` → `4` → `2` → `1`)
    and re-apply until it schedules.
  - Learn: the binding constraint is `allocatable − sum(requests of pods already
    on the node)`, not the node's total cores.

- [ ] **Task 3.3 — Scale a Deployment until it stops fitting**
  - Do: `kubectl scale deploy/sized-app -n res-lab --replicas=50`
  - Verify: some pods Run, the rest sit Pending with `Insufficient cpu`.
  - Learn: a Deployment does **not** guarantee its replica count — it guarantees
    it keeps *asking*. Capacity is a separate, external constraint.
  - Reset: `kubectl scale deploy/sized-app -n res-lab --replicas=2`

- [ ] **Task 3.4 — Total cost arithmetic**
  - Do:
    ```bash
    kubectl get pods -n res-lab -o jsonpath=\
    '{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[0].resources.requests.cpu}{"\n"}{end}'
    ```
  - Learn: cluster cost = `replicas × sum(container requests)`. This is the exact
    number a ResourceQuota checks and the exact number your cloud bill tracks.

---

## Level 4 — QoS under pressure: who dies first

- [ ] **Task 4.1 — Read the eviction ranking**
  - Order: **BestEffort → Burstable (furthest over its request first) → Guaranteed**.
  - Do: `kubectl get pods -n res-lab -o custom-columns='NAME:.metadata.name,QOS:.status.qosClass'`
    and write down the order in which these pods would be sacrificed.

- [ ] **Task 4.2 — Trigger real node memory pressure (destructive-ish)**
  - Create a Burstable pod requesting `32Mi` with a limit of `4Gi`, running a
    memory hog, on a small node. It stays *under its own limit* while dragging the
    **node** low on memory.
  - Verify: `kubectl get events -A --field-selector reason=Evicted` and
    `kubectl describe node <node> | grep -i pressure` → `MemoryPressure True`.
  - Learn: eviction is **node-driven and graceful** (pod status `Failed`,
    reason `Evicted`); OOMKill is **kernel-driven and abrupt** (container
    restarted in place). Different mechanism, different fix.
  - On a laptop cluster (kind/minikube) this can wedge the node — do it in a
    throwaway cluster, and `kubectl delete -f resources.yaml` afterwards.

- [ ] **Task 4.3 — Why memory request ≈ memory limit is the rule**
  - Reason it through: with `request 32Mi, limit 4Gi`, the scheduler books 32Mi
    and then packs more pods onto the node. When your pod actually takes 4Gi, the
    node is oversubscribed and *something* gets evicted — possibly a neighbour.
  - **Rule:** overcommit CPU freely (worst case: latency). Do not overcommit
    memory (worst case: someone dies, and not necessarily you).

- [ ] **Task 4.4 — Priority beats QoS**
  - Do: `kubectl get priorityclass`
  - Learn: `priorityClassName` affects **preemption** (who gets kicked out to make
    room at *scheduling* time) and is factored into eviction ranking. QoS alone
    isn't the whole story once PriorityClasses are in play.

---

## Level 5 — Cluster-side policy: LimitRange & ResourceQuota

- [ ] **Task 5.1 — Same YAML, different outcome**
  - Do:
    ```bash
    kubectl get pod qos-besteffort   -n res-lab         -o jsonpath='{.status.qosClass}{"\n"}'
    kubectl get pod inherits-defaults -n res-lab-guarded -o jsonpath='{.status.qosClass}{"\n"}'
    ```
  - Verify: `BestEffort` vs `Burstable` — from **identical** container specs.
  - Do: `kubectl get pod inherits-defaults -n res-lab-guarded -o jsonpath='{.spec.containers[0].resources}{"\n"}'`
  - Learn: the LimitRange **mutated your object at admission**. What you applied
    is not what got stored.

- [ ] **Task 5.2 — Get rejected by `max`**
  - Create a pod in `res-lab-guarded` requesting `cpu: "2"` (the LimitRange caps
    containers at `1`).
  - Verify: rejected with `maximum cpu usage per Container is 1`.
  - Learn: `min`/`max` **reject**; `default`/`defaultRequest` **inject**. Two jobs,
    one object.

- [ ] **Task 5.3 — Get rejected by `maxLimitRequestRatio`**
  - Create a pod with `requests.cpu: 10m` and `limits.cpu: 900m` (ratio 90 vs the
    allowed 4).
  - Verify: rejected — `cpu max limit to request ratio per Container is 4`.
  - Learn: this is the anti-lying knob. It stops "request nothing, cap at a core"
    pods that make node capacity planning fiction.

- [ ] **Task 5.4 — Watch the quota ledger**
  - Do: `kubectl describe resourcequota team-budget -n res-lab-guarded`
  - Verify: `Used` vs `Hard` for each resource, already charged for the pod above.
  - Do: scale a Deployment in that namespace past `requests.cpu: "1"`.
  - Verify: `exceeded quota: team-budget, requested: requests.cpu=..., used: ...,
    limited: requests.cpu=1`.

- [ ] **Task 5.5 — Where a quota rejection actually surfaces**
  - The quota error above does **not** appear on `kubectl scale` — that succeeds.
  - Do: `kubectl describe rs -n res-lab-guarded` and look at events.
  - Learn: the Deployment's controller is the one being rejected, so the failure
    lands on the **ReplicaSet**, not on your command. Look there when replicas
    silently never appear.

- [ ] **Task 5.6 — Quota forces explicitness**
  - Delete the LimitRange (`kubectl delete limitrange container-defaults -n
    res-lab-guarded`), then create a pod with no `resources:` block.
  - Verify: rejected — `must specify limits.cpu, limits.memory, requests.cpu,
    requests.memory`.
  - Learn: a quota on cpu/memory makes those fields **mandatory** namespace-wide.
    That's why LimitRange and ResourceQuota are deployed as a pair — the
    LimitRange supplies defaults so the mandate isn't painful. Re-apply it.

---

## Level 6 — Advanced

- [ ] **Task 6.1 — Right-size from real usage**
  - Do: `kubectl top pods -n res-lab --containers` (needs metrics-server).
  - Method: set `requests` ≈ **P50–P90 observed usage**, `limits` ≈ **P99 with
    headroom** (memory: close to the request; CPU: generous or omitted).
  - Learn: sizing is an *observation* problem, not a guessing problem. Ship
    something reasonable, measure, adjust.

- [ ] **Task 6.2 — Resources are a template change**
  - Do: `kubectl set resources deploy/sized-app -n res-lab --requests=cpu=80m --limits=cpu=300m`
  - Verify: `kubectl rollout history deploy/sized-app -n res-lab` → a **new
    revision**, a new ReplicaSet, a rolling update.
  - Learn: resizing a Deployment replaces every pod. Contrast with `kubectl scale`,
    which creates no revision (`deployment-tasks.md` EC-5).

- [ ] **Task 6.3 — In-place resize (if your cluster supports it)**
  - Check: `kubectl explain pod.spec.containers.resizePolicy` (beta / on by
    default from k8s 1.33; absent on older clusters).
  - Do: `kubectl patch pod storage-and-resize -n res-lab --subresource resize --patch \
    '{"spec":{"containers":[{"name":"app","resources":{"requests":{"cpu":"100m"},"limits":{"cpu":"300m"}}}]}}'`
  - Verify: `kubectl get pod storage-and-resize -n res-lab` — same pod, **no
    restart** (our `resizePolicy` says cpu is `NotRequired`).
  - Learn: this is the one crack in "pods are immutable" (`pod-tasks.md` EC-7).
    Memory is `RestartContainer` here because shrinking a memory cgroup below
    current usage can't be done live.

- [ ] **Task 6.4 — Ephemeral storage: the third failure mode**
  - In `storage-and-resize`, fill `/scratch` past its `128Mi` limit:
    `kubectl exec -n res-lab storage-and-resize -- sh -c 'dd if=/dev/zero of=/scratch/f bs=1M count=200'`
  - Verify: the pod is **Evicted** — not OOMKilled, not throttled.
  - Learn: three distinct resource failures now: **throttled** (cpu limit),
    **OOMKilled** (memory limit), **Evicted** (ephemeral-storage limit or node
    pressure). Unbounded application logs are the usual real-world cause.

- [ ] **Task 6.5 — initContainers don't add up the way you think**
  - Add an initContainer requesting `cpu: "2"` to any pod here.
  - Learn: a pod's effective request is
    `max(largest init request, sum of app-container requests)` — init containers
    run sequentially, so they're a **max**, not a sum. A fat init container can
    make an otherwise-small pod unschedulable. (Sidecars declared as
    `restartPolicy: Always` init containers **do** count toward the sum.)

- [ ] **Task 6.6 — Extended resources**
  - Do: `kubectl describe node <node> | sed -n '/Capacity/,/Allocatable/p'`
  - Learn: `nvidia.com/gpu`-style extended resources are **integers only**, and
    **request must equal limit** — no fractions, no bursting. Devices are not
    time-sliced the way CPU is.

---

## Level 7 — Edge Cases & Production Nuances

Same format as `pod-tasks.md` Level 7: **trap → reproduce → diagnose → fix/rule**.

---

### EC-1 — CPU limits cause throttling long before you hit 100%

- **Trap:** a container with `cpu: 500m` shows 30% average CPU in your dashboard,
  yet p99 latency is terrible. "It's not even using its limit."
- **Why:** the limit is enforced by the CFS quota over a **100ms window** — 50ms
  of CPU per 100ms period. A request that needs 80ms of CPU *in one burst* is
  paused until the next period, no matter how idle the previous second was.
  Averages hide this completely.
- **Diagnose:** look at `container_cpu_cfs_throttled_periods_total` /
  `..._periods_total` in Prometheus, or inside the container:
  `cat /sys/fs/cgroup/cpu.stat` → a climbing `nr_throttled` / `throttled_usec`.
- **Fix/rule:** raise or **remove** the CPU limit for latency-sensitive services
  and rely on requests for fair-share scheduling. Keep memory limits always;
  CPU limits are the genuinely debatable one.

---

### EC-2 — OOMKilled reports the *container*, and `describe` hides the reason

- **Trap:** `kubectl describe pod` shows `State: Running` and you conclude it's
  fine — the OOM is in the **previous** state.
- **Diagnose (the reliable one-liner):**
  ```bash
  kubectl get pod <pod> -n <ns> -o jsonpath=\
  '{range .status.containerStatuses[*]}{.name}{"\t"}{.restartCount}{"\t"}{.lastState.terminated.reason}{"\n"}{end}'
  ```
  Then `kubectl logs <pod> -c <container> --previous` for the app's dying words.
- **Gotcha:** exit `137` alone is ambiguous — it's any SIGKILL, including a
  `terminationGracePeriodSeconds` timeout. Trust `reason: OOMKilled`, not the code.
- **Rule:** a non-zero `restartCount` with `lastState.terminated.reason:
  OOMKilled` means **undersized memory limit or a leak** — nothing else.

---

### EC-3 — The JVM / Go / Node runtime doesn't see your limit

- **Trap:** you set `memory: 512Mi`; the JVM inspects the *host's* 64GB, sizes its
  heap accordingly, and gets OOMKilled almost immediately.
- **Why:** limits are cgroup settings. Older runtimes read `/proc/meminfo`, which
  reports the **node**, not the cgroup.
- **Fix:** modern JVMs are container-aware (`-XX:MaxRAMPercentage=75`); Node needs
  `--max-old-space-size`; Go needs `GOMEMLIMIT` (and `GOMAXPROCS` matched to the
  CPU limit — otherwise it spawns threads for every host core and throttles hard).
- **Rule:** setting a limit is half the job. The **process inside** has to be told
  about it too. Use the Downward API to pass it (`pod-tasks.md` Level 3):
  ```yaml
  env:
    - name: MEM_LIMIT
      valueFrom:
        resourceFieldRef: { containerName: app, resource: limits.memory }
  ```

---

### EC-4 — `kubectl top` and `requests` measure different things

- **Trap:** "`top` says 20m, so I'll request 20m" — then everything gets evicted
  under load.
- **Why:** `kubectl top` is **actual usage right now**; `requests` is the
  **reservation for the worst moment you care about**. Sizing against an idle
  sample guarantees you are undersized at peak.
- **Rule:** size from a percentile over a representative window (a full traffic
  cycle, including deploys and cron spikes), not a point sample.

---

### EC-5 — A namespace with a ResourceQuota rejects pods that omit resources

- **Trap:** your manifests work in `dev`, then fail in `prod` with
  `must specify limits.cpu` — same YAML, different namespace.
- **Reproduce:** Task 5.6.
- **Fix/rule:** always declare resources explicitly. Relying on a LimitRange's
  defaults means your pod's real size depends on **which namespace it lands in** —
  invisible in your Git repo and different per environment.

---

### EC-6 — A LimitRange makes BestEffort impossible (and that's the point)

- **Trap:** you deliberately want a BestEffort batch pod, but it comes out
  Burstable — because the namespace has a LimitRange with `defaultRequest`.
- **Also:** a LimitRange only affects pods created **after** it. Adding one does
  not retrofit existing pods; they keep their old (or absent) resources until
  recreated.
- **Rule:** LimitRange is *mutating admission*. What you `apply` is not
  necessarily what is stored — always verify with `kubectl get -o yaml` after
  creation, not by re-reading your own file.

---

### EC-7 — Requests are booked even when the pod is idle

- **Trap:** the cluster reports 90% CPU "utilization" and refuses to schedule
  anything, while `kubectl top nodes` shows 12% real usage.
- **Why:** the scheduler counts **requests**, not usage. Oversized requests are
  pure waste — you pay for capacity nobody uses and block real work.
- **Diagnose:** compare the two views directly.
  ```bash
  kubectl describe node <node> | sed -n '/Allocated resources/,/^Events/p'  # requests
  kubectl top node <node>                                                    # actual
  ```
- **Rule:** the gap between those two numbers is your cluster's waste budget.
  Over-requesting is as much a bug as under-requesting — it just fails quietly,
  as spend instead of as an incident.

---

### EC-8 — `emptyDir: {medium: Memory}` is charged to your memory limit

- **Trap:** you mount a tmpfs "for speed", write 500Mi to it, and the container is
  OOMKilled — even though the *process* uses 50Mi.
- **Why:** tmpfs pages live in the container's memory cgroup. Files on it are RAM.
  This is exactly how `mem-oomkill` in `resources.yaml` works.
- **Rule:** budget `memory limit ≥ process peak + tmpfs contents`, and always set
  `sizeLimit` on a memory-medium `emptyDir` so it can't grow unbounded.

---

### EC-9 — Sidecars are invisible in your sizing math

- **Trap:** you size `app` at `100m/128Mi`, but an injected service-mesh sidecar
  adds `100m/128Mi`. Your pod's real footprint doubled and your quota math is off
  by 2×.
- **Diagnose:**
  ```bash
  kubectl get pod <pod> -n <ns> -o jsonpath=\
  '{range .spec.containers[*]}{.name}{"\t"}{.resources.requests.cpu}{"\t"}{.resources.requests.memory}{"\n"}{end}'
  ```
- **Rule:** a pod's request is the **sum over all containers** (plus the largest
  init container — see Task 6.5). Always check the whole pod, never just your own
  container, when reconciling against a quota or a node's capacity.

---

### EC-10 — Changing resources replaces the pod (unless in-place resize is on)

- **Trap:** you `kubectl edit pod` to bump memory and get
  `Pod updates may not change fields other than ...`.
- **Why:** `spec.containers[*].resources` is immutable on a running Pod on
  clusters without in-place resize — the classic "pods are almost immutable" rule
  (`pod-tasks.md` EC-7).
- **Fix:** change it in the **Deployment's template** and let a rolling update
  replace the pods (Task 6.2). Where in-place resize is available, use the
  `resize` subresource (Task 6.3) — but note that `kubectl edit` still won't do
  it; it needs the dedicated subresource.

---

### EC-11 — Requests protect you; limits protect everyone else

- **Framing to internalise:**
  - Drop your **request** → you get scheduled onto crowded nodes and starve.
  - Drop your **limit** → you can starve your neighbours (or the kubelet).
- **Production defaults that hold up well:**

  | | request | limit |
  |---|---|---|
  | **memory** | = expected peak | = request (or +10–20%) |
  | **cpu** | = steady-state P50–P90 | generous, or omit (see EC-1) |

- **Rule:** always set **both** memory values and a **cpu request**. The cpu limit
  is the only genuinely optional one, and omitting it is a defensible choice for
  latency-sensitive services.

---

### EC-12 — Quota failures are silent at the surface you're watching

- **Trap:** `kubectl scale deploy/x --replicas=10` returns success. Ten minutes
  later there are still 3 pods and no error anywhere you looked.
- **Why:** the Deployment updated fine. Its **ReplicaSet** is the thing being
  rejected by the quota admission plugin, over and over.
- **Diagnose (in order):**
  ```bash
  kubectl describe deploy <name> -n <ns>   # conditions: ReplicaFailure=True
  kubectl describe rs -n <ns>              # events: "exceeded quota: ..."
  kubectl describe resourcequota -n <ns>   # Used vs Hard
  ```
- **Rule:** when replicas never materialise and there are **no pods to inspect**,
  the failure is upstream of the pod — quota, admission webhook, or scheduling.
  Walk *down* the ownership chain, from Deployment to RS to Pod.

---

## Cheat sheet

```bash
kubectl apply -f resources.yaml
kubectl get pods -n res-lab -o custom-columns='NAME:.metadata.name,QOS:.status.qosClass'
kubectl top pods  -n res-lab --containers          # actual usage (metrics-server)
kubectl top nodes                                   # actual node usage
kubectl describe node <node> | sed -n '/Allocated resources/,/^Events/p'   # requests
kubectl get pod <p> -n <ns> -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}{"\n"}'
kubectl set resources deploy/sized-app -n res-lab --requests=cpu=80m --limits=cpu=300m
kubectl describe limitrange    container-defaults -n res-lab-guarded
kubectl describe resourcequota team-budget        -n res-lab-guarded
kubectl delete -f resources.yaml                    # removes both namespaces
```

## Mental model to lock in

- **`requests` → scheduler** (a booking, never enforced at runtime).
  **`limits` → kernel** (enforced every 100ms).
- **CPU is compressible → throttled. Memory is not → OOMKilled.** Same violation,
  opposite consequence. This single asymmetry explains most of the topic.
- **QoS is derived, not declared**, and it's an *eviction ranking*:
  BestEffort dies first, Guaranteed last.
- **Three distinct failure modes:** throttled (cpu limit), OOMKilled (memory
  limit), Evicted (ephemeral-storage limit or node pressure). Different symptoms,
  different fixes — never conflate them.
- **Overcommit CPU, don't overcommit memory.** Latency is recoverable; a SIGKILL
  is not.
- **LimitRange = per-container policy** (inject + reject).
  **ResourceQuota = per-namespace budget.** Deploy them together.
- **Over-requesting is a bug too** — it just shows up on the invoice instead of
  in an incident channel.

```text
       you write                 kubernetes derives              kernel enforces
  ┌────────────────┐          ┌───────────────────┐          ┌──────────────────┐
  │ requests ──────┼─────────▶│ scheduling + QoS  │          │ cpu   → throttle │
  │ limits   ──────┼──────────┼───────────────────┼─────────▶│ mem   → OOMKill  │
  └────────────────┘          │ Guaranteed        │          │ disk  → Evict    │
                              │ Burstable         │          └──────────────────┘
                              │ BestEffort ← dies first
                              └───────────────────┘
```
