# Workload Controllers Tasks — StatefulSet, DaemonSet, Job, CronJob

Track A module 6. Do after `service-networking-tasks.md`.
Read alongside: `../kubernetes/12-workload-controllers.md`, `../kubernetes/13-statefulset-deep-dive.md`.

Deployment/ReplicaSet you already have. These four cover everything else, and two
of them matter directly for GPU work: **Jobs are how batch and training workloads
run**, and **DaemonSets are how device plugins and node agents run**.

> The one idea: **every workload controller is the same reconcile loop with a
> different notion of identity.**
>
> ```
>   Deployment   identity: none      pods are interchangeable, random names
>   StatefulSet  identity: ordinal   pod-0,1,2 — stable name, DNS, and volume
>   DaemonSet    identity: node      exactly one pod per matching node
>   Job          identity: none      run to completion, then stop
>   CronJob      identity: time      creates Jobs on a schedule
> ```

Setup: `kubectl create ns wl-lab`.

---

## Level 0 — Orientation

1. `kubectl api-resources --api-group=apps` and `--api-group=batch`
2. All five own their Pods via ownerReferences — verify with
   `kubectl get pod <p> -o jsonpath='{.metadata.ownerReferences}'`
   (`api-machinery-tasks.md` Level 3).
3. Only Deployment has the extra ReplicaSet layer. StatefulSet and DaemonSet own
   Pods directly, which is why their rollouts behave differently.

---

## Level 1 — StatefulSet

- [ ] **Task 1.1 — Ordinals and the headless Service**
  ```yaml
  apiVersion: v1
  kind: Service
  metadata: {name: db, namespace: wl-lab}
  spec:
    clusterIP: None
    publishNotReadyAddresses: true
    selector: {app: db}
    ports: [{port: 5432, name: pg}]
  ---
  apiVersion: apps/v1
  kind: StatefulSet
  metadata: {name: db, namespace: wl-lab}
  spec:
    serviceName: db
    replicas: 3
    selector: {matchLabels: {app: db}}
    template:
      metadata: {labels: {app: db}}
      spec:
        containers:
        - name: c
          image: busybox
          command: ["sh","-c","sleep 3600"]
          volumeMounts: [{name: data, mountPath: /data}]
    volumeClaimTemplates:
    - metadata: {name: data}
      spec:
        accessModes: [ReadWriteOnce]
        resources: {requests: {storage: 100Mi}}
  ```
  - Verify: `db-0`, `db-1`, `db-2` — created **in order**, each waiting for the
    previous to be Ready.
  - Learn: `serviceName` is required and must point at a headless Service. It's
    what makes per-Pod DNS work.

- [ ] **Task 1.2 — Per-Pod DNS**
  ```bash
  kubectl run tmp -n wl-lab --rm -it --image=nicolaka/netshoot -- \
    dig +short db-0.db.wl-lab.svc.cluster.local
  ```
  - Learn: `<pod>.<service>.<ns>.svc.cluster.local`. Stable across restarts and
    reschedules. This is how every clustered database does peer discovery — and
    why `publishNotReadyAddresses` matters (`service-networking-tasks.md` EC-8).

- [ ] **Task 1.3 — One PVC per Pod, and it survives**
  ```bash
  kubectl get pvc -n wl-lab      # data-db-0, data-db-1, data-db-2
  kubectl exec -n wl-lab db-1 -- sh -c 'echo hello > /data/f'
  kubectl delete pod db-1 -n wl-lab
  kubectl exec -n wl-lab db-1 -- cat /data/f
  ```
  - Verify: `hello`. Same ordinal → same PVC.
  - Learn: `volumeClaimTemplates` creates one PVC per ordinal, named
    `<template>-<sts>-<ordinal>`. It binds by name, so identity survives.

- [ ] **Task 1.4 — Deleting the StatefulSet does NOT delete the PVCs**
  - Do: delete the StatefulSet, then `kubectl get pvc -n wl-lab`.
  - Verify: all three still there. Recreate the StatefulSet — it reattaches.
  - Learn: deliberate, and a footgun. Data is preserved by default;
    `persistentVolumeClaimRetentionPolicy` (1.27+) lets you opt into deletion.

- [ ] **Task 1.5 — Rolling update is reverse-ordinal**
  - Do: change the image, then `kubectl get pods -n wl-lab -w`
  - Verify: `db-2` first, then `db-1`, then `db-0`.
  - Learn: highest ordinal first, one at a time, waiting for Ready. For a
    primary/replica database this updates replicas before the primary — deliberate.

- [ ] **Task 1.6 — Partitioned rollout = canary**
  ```yaml
  updateStrategy: {rollingUpdate: {partition: 2}}
  ```
  - Learn: only ordinals **≥ partition** update. Set it to 2, verify the image
    change, then lower it to roll the rest. This is the built-in staged rollout.

- [ ] **Task 1.7 — Parallel pod management**
  - Learn: `podManagementPolicy: Parallel` drops the ordered guarantee for
    *creation and deletion* (not for updates). Right for genuinely peer-to-peer
    systems where startup order is irrelevant; wrong for anything with a bootstrap
    sequence.

---

## Level 2 — DaemonSet

- [ ] **Task 2.1 — One per node, automatically**
  ```bash
  kubectl create -n wl-lab -f - <<'EOF'
  apiVersion: apps/v1
  kind: DaemonSet
  metadata: {name: agent, namespace: wl-lab}
  spec:
    selector: {matchLabels: {app: agent}}
    template:
      metadata: {labels: {app: agent}}
      spec:
        containers: [{name: c, image: busybox, command: ["sh","-c","sleep 3600"]}]
  EOF
  kubectl get ds,pods -n wl-lab -o wide
  ```
  - Learn: **no `replicas` field.** The count is derived from the node set. Add a
    node and a Pod appears; drain one and it goes.

- [ ] **Task 2.2 — The scheduler still schedules it**
  - Do: `kubectl get pod -n wl-lab -l app=agent -o jsonpath='{.items[0].spec.affinity}' | jq`
  - Verify: a `nodeAffinity` pinning it to one specific node name.
  - Learn: modern Kubernetes has the DaemonSet controller create Pods with node
    affinity and lets the **default scheduler** place them. It does not bypass
    scheduling — so a DaemonSet Pod can go Pending for insufficient resources like
    anything else.

- [ ] **Task 2.3 — Reaching tainted nodes**
  - Do: `kubectl get ds -n kube-system kube-proxy -o jsonpath='{.spec.template.spec.tolerations}' | jq`
  - Learn: system DaemonSets tolerate nearly everything — including
    `node.kubernetes.io/not-ready` and control-plane taints. A monitoring agent
    that doesn't tolerate them has blind spots exactly where you need visibility.
    See `scheduling-constraints-tasks.md` Level 3.

- [ ] **Task 2.4 — Targeting a subset**
  - Do: label a node `accelerator=gpu` and add `nodeSelector: {accelerator: gpu}`.
  - Verify: Pods only on labelled nodes.
  - Learn: **this is exactly how the NVIDIA device plugin ships** — a DaemonSet
    restricted to GPU nodes, advertising `nvidia.com/gpu` to the kubelet. Your
    `device-plugin-tasks.md` endgame is a DaemonSet.

---

## Level 3 — Job

- [ ] **Task 3.1 — Run to completion**
  ```bash
  kubectl create job pi -n wl-lab --image=perl:5.34 -- perl -Mbignum=bpi -wle 'print bpi(200)'
  kubectl get job,pods -n wl-lab
  kubectl logs -n wl-lab job/pi
  ```
  - Learn: the Pod ends `Completed`, not `Running`, and is **not restarted**. Job
    Pods must use `restartPolicy: OnFailure` or `Never` — `Always` is rejected.

- [ ] **Task 3.2 — completions and parallelism**
  ```yaml
  spec: {completions: 6, parallelism: 2}
  ```
  - Verify: two at a time until six succeed.
  - Learn: `completions` = how many must succeed. `parallelism` = how many at once.
    This is the batch primitive underneath most ML training jobs.

- [ ] **Task 3.3 — Indexed jobs**
  ```yaml
  spec: {completionMode: Indexed, completions: 4, parallelism: 4}
  ```
  - Do: read `JOB_COMPLETION_INDEX` from the env inside a Pod.
  - Learn: each Pod gets a stable index 0..N-1 — how you shard work without a queue.
    This is the shape of distributed training rank assignment.

- [ ] **Task 3.4 — backoffLimit and failure**
  - Do: create a Job with `command: ["sh","-c","exit 1"]` and `backoffLimit: 2`.
  - Verify: retries with exponential backoff (10s, 20s, 40s…), then
    `type: Failed, reason: BackoffLimitExceeded`.
  - Learn: `backoffLimit` counts **Pod failures across the whole Job**, not per
    index. Default 6.

- [ ] **Task 3.5 — activeDeadlineSeconds and TTL**
  - Learn: `activeDeadlineSeconds` kills the Job regardless of retries — it beats
    `backoffLimit`. `ttlSecondsAfterFinished` deletes the finished Job and its
    Pods, and **without it, completed Jobs accumulate forever**. Those Pods still
    hold `spec.nodeName` and resource requests, which is exactly
    `controller-tasks.md` EC-7 — the reason naive capacity dashboards over-report.

- [ ] **Task 3.6 — Pod failure policy**
  ```yaml
  podFailurePolicy:
    rules:
    - action: FailJob
      onExitCodes: {operator: In, values: [42]}
    - action: Ignore
      onPodConditions: [{type: DisruptionTarget}]
  ```
  - Learn: distinguishes "my code is broken, stop retrying" from "the node was
    preempted, that shouldn't count." Essential on spot/preemptible GPU capacity,
    where infrastructure churn would otherwise burn your whole backoff budget.

---

## Level 4 — CronJob

- [ ] **Task 4.1 — Schedule**
  ```bash
  kubectl create cronjob tick -n wl-lab --schedule='*/1 * * * *' --image=busybox -- date
  kubectl get cronjob,jobs -n wl-lab -w
  ```
  - Learn: a CronJob creates **Jobs**, which create Pods. Three levels of ownership.

- [ ] **Task 4.2 — concurrencyPolicy**
  - Learn: `Allow` (default, overlapping runs), `Forbid` (skip if still running),
    `Replace` (kill the old one). A slow job on `Allow` piles up until the cluster
    is full — the classic CronJob outage.

- [ ] **Task 4.3 — startingDeadlineSeconds and missed runs**
  - Learn: if the controller is down past the deadline, runs are skipped. Miss 100
    schedules with no deadline set and the CronJob **stops permanently** with
    `Cannot determine if job needs to be started`. Always set it.

- [ ] **Task 4.4 — History limits**
  - Learn: `successfulJobsHistoryLimit` (3) and `failedJobsHistoryLimit` (1). This
    is the CronJob's own garbage collection — separate from
    `ttlSecondsAfterFinished` on the Job.

- [ ] **Task 4.5 — Timezones**
  - Learn: `spec.timeZone: "Europe/Yerevan"` (1.27+). Without it, schedules use the
    **controller manager's** timezone, usually UTC. Every DST bug traces here.

---

## Level 5 — Edge Cases & Production Nuances

### EC-1 — StatefulSet stuck because Pod 0 won't start

- **Trap:** `db-0` is CrashLooping, so `db-1` and `db-2` are never created.
- **Why:** ordered startup waits for Ready. One broken Pod blocks the whole set.
- **Fix:** debug `db-0`, or switch to `podManagementPolicy: Parallel` if ordering
  isn't genuinely required.
- **Rule:** ordering is a guarantee *and* a serial dependency chain.

---

### EC-2 — StatefulSet PVC keeps stale data

- **Trap:** you delete a broken Pod expecting a clean start; it comes back with the
  same corrupted volume.
- **Why:** identity binds Pod ordinal to PVC name. Deleting the Pod changes nothing.
- **Fix:** delete the PVC too, then the Pod.
- **Rule:** "delete the pod and see" doesn't work for StatefulSets. That instinct
  comes from Deployments and it will mislead you here.

---

### EC-3 — DaemonSet Pods Pending forever

- **Diagnose:** `kubectl describe pod` → `Insufficient cpu`, or
  `node(s) had untolerated taint`.
- **Why:** DaemonSets are scheduled normally (Task 2.2). If nodes are already
  fully booked by requests, the agent doesn't fit.
- **Fix:** small requests plus a high `priorityClassName` (e.g.
  `system-node-critical`) so it can preempt.
- **Rule:** node agents must be tiny and high-priority, or they'll be absent from
  exactly the overloaded nodes you most need to observe.

---

### EC-4 — Completed Job Pods inflate capacity numbers

- **Trap:** a namespace shows high GPU/CPU allocation with nothing running.
- **Why:** terminal Pods keep `spec.nodeName` and `spec.resources` until deleted.
  They hold no real capacity but appear in naive queries.
- **Fix:** `ttlSecondsAfterFinished` on every Job, and filter on
  `status.phase` when summing.
- **Rule:** the same bug as `controller-tasks.md` EC-7, from the workload side.
  You'll meet this in real capacity work.

---

### EC-5 — Job retries a broken image forever

- **Trap:** `ImagePullBackOff` and the `backoffLimit` never trips.
- **Why:** the Pod never *ran*, so depending on version it may not count as a Pod
  failure — it just sits there.
- **Fix:** `activeDeadlineSeconds` as a hard stop.
- **Rule:** `backoffLimit` bounds failures; only `activeDeadlineSeconds` bounds
  *time*. Set both.

---

### EC-6 — CronJob stopped silently weeks ago

- **Diagnose:** `kubectl describe cronjob` →
  `Cannot determine if job needs to be started: too many missed start times`.
- **Fix:** set `startingDeadlineSeconds` (e.g. 200) and recreate.
- **Rule:** a CronJob that misses 100 schedules disables itself permanently. Alert
  on `status.lastScheduleTime` age — nothing else will tell you.

---

### EC-7 — Two CronJob runs overlap and corrupt state

- **Trap:** a job that usually takes 30s occasionally takes 5 minutes; on a
  `*/1` schedule with default `Allow`, five copies run concurrently.
- **Fix:** `concurrencyPolicy: Forbid`.
- **Rule:** the default is the unsafe one. Change it unless overlap is genuinely fine.

---

## Cheat sheet

```bash
kubectl get sts,ds,job,cronjob -n NS
kubectl rollout status sts/db -n NS
kubectl patch sts db -n NS -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":2}}}}'
kubectl get pvc -n NS                                  # data-db-0, data-db-1, ...
dig +short db-0.db.NS.svc.cluster.local                # per-pod DNS
kubectl create job NAME --image=IMG -- cmd
kubectl create job manual --from=cronjob/tick -n NS    # trigger a CronJob now
kubectl get pods -n NS --field-selector status.phase=Succeeded
kubectl delete pods -n NS --field-selector status.phase==Succeeded
kubectl describe cronjob tick -n NS                    # missed start times
```

## Mental model to lock in

- **Identity is the only real difference.** None (Deployment), ordinal
  (StatefulSet), node (DaemonSet), completion (Job), time (CronJob).
- **StatefulSet = stable name + stable DNS + stable volume**, in creation order,
  reverse update order. Needs a headless Service.
- **StatefulSet PVCs outlive the StatefulSet.** Deliberately.
- **DaemonSets are scheduled like anything else** — they need tolerations,
  small requests and high priority to be genuinely everywhere.
- **Jobs need `ttlSecondsAfterFinished`**, or terminal Pods pollute capacity views
  forever.
- **`backoffLimit` bounds failures, `activeDeadlineSeconds` bounds time.** Set both.
- **CronJob defaults are unsafe:** `Allow` concurrency, no `startingDeadlineSeconds`,
  controller timezone.

```text
  CronJob ──schedule──▶ Job ──completions/parallelism──▶ Pods ──▶ Completed
                         │
                         └── backoffLimit · activeDeadlineSeconds · ttlSecondsAfterFinished

  StatefulSet ──▶ pod-0 ─┐   headless Service ──▶ pod-0.svc, pod-1.svc, ...
                  pod-1 ─┼── each with data-<sts>-<n>  (PVC survives deletion)
                  pod-2 ─┘   create: 0→N   update: N→0

  DaemonSet ──▶ one pod per matching node (nodeSelector + tolerations)
```
