# Deployment Tasks — Beginner → Advanced

A hands-on ladder for learning **Deployments** using `deployment.yaml` in this folder.
Do these AFTER `pod-tasks.md` and `replica-tasks.md` — a Deployment manages
ReplicaSets, which manage Pods, so understand those layers first.

> The one idea: a Deployment is a **ReplicaSet manager** that adds **rolling
> updates + rollback**. It never touches Pods directly — it creates/scales
> ReplicaSets, and each ReplicaSet keeps its Pods alive.
>
> ```
> Deployment ──manages──▶ ReplicaSet(s) ──manage──▶ Pods
> (rollouts + history)   (keep N copies)          (atomic unit)
> ```

---

## Level 0 — Orientation

1. `kubectl explain deployment.spec` — note the extra fields a ReplicaSet lacks:
   `strategy`, `revisionHistoryLimit`, `progressDeadlineSeconds`, `paused`.
2. Mental model: **desired vs actual**, but one level up — the Deployment
   controller reconciles *ReplicaSets*, each RS reconciles *Pods*.
3. Why it exists: a bare ReplicaSet does NOT roll out template changes to running
   pods (you proved this in `replica-tasks.md` Task 5.1). The Deployment fixes that.

---

## Level 1 — Beginner: create & see the layers

- [ ] **Task 1.1 — Apply**
  - Do: `kubectl apply -f deployment.yaml`
  - Verify: `kubectl get deploy nginx-deploy` → `READY 3/3  UP-TO-DATE 3  AVAILABLE 3`.

- [ ] **Task 1.2 — See ALL three layers at once**
  - Do: `kubectl get deploy,rs,pods -l app=nginx`
  - Learn: one Deployment → one ReplicaSet (random suffix like `nginx-deploy-7c9f`) →
    3 Pods (`nginx-deploy-7c9f-xxxxx`). The Deployment owns the RS; the RS owns the Pods.

- [ ] **Task 1.3 — Rollout status & history**
  - Do: `kubectl rollout status deployment/nginx-deploy`
  - Do: `kubectl rollout history deployment/nginx-deploy` → revision 1, with the
    `change-cause` annotation you set.

- [ ] **Task 1.4 — Ownership chain**
  - Do: `kubectl get rs -l app=nginx -o jsonpath='{.items[*].metadata.ownerReferences[*].name}{"\n"}'`
  - Learn: the RS's owner is the Deployment; a Pod's owner is the RS. Two levels of `ownerReferences`.

---

## Level 2 — The whole point: rolling updates

- [ ] **Task 2.1 — Roll out a new image (watch it happen)**
  - In one terminal: `kubectl get rs -w -l app=nginx`
  - In another: `kubectl set image deployment/nginx-deploy nginx=nginx:1.27-alpine`
  - Verify: a NEW ReplicaSet appears and scales up while the OLD one scales down.
  - Learn: this is a rolling update — new RS created, pods shifted gradually.

- [ ] **Task 2.2 — Confirm running pods actually changed**
  - Do: `kubectl get pods -l app=nginx -o jsonpath='{.items[*].spec.containers[*].image}{"\n"}'`
  - Learn: all pods now run `1.27` — the exact thing a bare ReplicaSet could NOT do.

- [ ] **Task 2.3 — Declarative version**
  - Instead of `set image`, edit `deployment.yaml` (`image:` + the `change-cause`
    annotation) and `kubectl apply -f deployment.yaml`. Same rollout, but the file
    stays the source of truth (prefer this / GitOps).

- [ ] **Task 2.4 — maxSurge / maxUnavailable in action**
  - We set `maxSurge: 1, maxUnavailable: 0` (zero-downtime). During a rollout,
    `kubectl get pods -l app=nginx` briefly shows **4** pods (3 + 1 surge), never
    fewer than 3 Ready. Try `maxUnavailable: 1, maxSurge: 0` and watch it dip to 2.

---

## Level 3 — Rollback & history

- [ ] **Task 3.1 — Undo the last rollout**
  - Do: `kubectl rollout undo deployment/nginx-deploy`
  - Verify: pods go back to the previous image; a rollback is just another rollout
    (it re-scales the OLD ReplicaSet back up — RS objects are reused, not recreated).

- [ ] **Task 3.2 — Roll back to a specific revision**
  - Do: `kubectl rollout history deployment/nginx-deploy` then
    `kubectl rollout undo deployment/nginx-deploy --to-revision=1`

- [ ] **Task 3.3 — Why old ReplicaSets stick around**
  - Do: `kubectl get rs -l app=nginx` — you'll see old RSes at `0` replicas. They're
    kept for rollback, bounded by `revisionHistoryLimit` (we set 5). Learn: scaled-to-zero
    RSes are your rollback history.

- [ ] **Task 3.4 — change-cause labels**
  - Set `metadata.annotations.kubernetes.io/change-cause` before each apply so
    `rollout history` reads meaningfully instead of `<none>`.

---

## Level 4 — Scaling, pausing, autoscaling

- [ ] **Task 4.1 — Scale**
  - Do: `kubectl scale deployment/nginx-deploy --replicas=5` (or edit the file + apply).
  - Learn: scaling changes replica count but does NOT create a new revision — it's not
    a template change.

- [ ] **Task 4.2 — Pause / resume (batch several edits into one rollout)**
  - Do: `kubectl rollout pause deployment/nginx-deploy`
  - Make several changes (`set image`, `set resources` ...). Nothing rolls yet.
  - Do: `kubectl rollout resume deployment/nginx-deploy` → ONE rollout applies them all.
  - Learn: avoids N separate rollouts when you're making N related changes.

- [ ] **Task 4.3 — HorizontalPodAutoscaler**
  - Do: `kubectl autoscale deployment/nginx-deploy --min=2 --max=6 --cpu-percent=50`
    (needs metrics-server). Learn: HPA edits `replicas` for you based on load.
  - Gotcha: if HPA owns replicas, DON'T also hard-code `replicas` in your applied file
    or they fight — omit `replicas` from the manifest when an HPA manages it.

---

## Level 5 — Advanced: strategies & failed rollouts

- [ ] **Task 5.1 — Recreate strategy**
  - Set `strategy.type: Recreate` (remove the `rollingUpdate` block), apply, then change
    the image. Verify: ALL old pods terminate first, THEN new ones start (brief downtime).
  - Learn: use when two versions can't coexist (exclusive locks, incompatible schema).

- [ ] **Task 5.2 — A stuck rollout (bad image)**
  - Do: `kubectl set image deployment/nginx-deploy nginx=nginx:doesnotexist`
  - Verify: `kubectl rollout status` hangs; `kubectl get pods` shows new pods in
    `ImagePullBackOff` while OLD pods keep serving (because `maxUnavailable: 0`!).
  - Learn: a good rollout config means a broken new version does NOT take down the old
    one. Fix with `kubectl rollout undo`.

- [ ] **Task 5.3 — progressDeadlineSeconds**
  - After a stuck rollout, wait past `progressDeadlineSeconds` (120s). Check
    `kubectl describe deployment nginx-deploy` → condition `Progressing=False,
    reason=ProgressDeadlineExceeded`. Learn: it REPORTS failure; it does NOT auto-rollback.

- [ ] **Task 5.4 — Readiness gates the rollout**
  - Point `readinessProbe.httpGet.path` at `/nope`, apply. New pods never become Ready,
    so the rollout stalls with old pods still serving. Learn: readiness is the rollout's
    "is the new version healthy?" signal — a core safety mechanism. Revert.

---

## Level 6 — Edge Cases & Production Nuances

Same format as `pod-tasks.md` Level 7: **trap → reproduce → diagnose → fix/rule**.

---

### EC-1 — Deployment vs ReplicaSet vs Pod: know which to touch

- **Trap:** editing a ReplicaSet the Deployment owns gets **reverted** — the Deployment
  controller reconciles the RS back to match its template.
- **Rule:** with a Deployment, you manage the **Deployment only**. Never edit its child
  RSes or Pods directly; changes there are transient. `kubectl edit deploy` / `apply`,
  never `kubectl edit rs`.

---

### EC-2 — Selector is IMMUTABLE

- **Trap:** you cannot change `spec.selector` after creation — `apply` fails with
  `field is immutable`.
- **Reproduce:** change `selector.matchLabels.app` to something new, `apply`.
- **Fix/rule:** to change the selector you must **delete and recreate** the Deployment.
  Choose labels carefully up front. (This is also why selector and template labels are
  usually kept minimal and stable.)

---

### EC-3 — Orphaned ReplicaSets from a selector/label mismatch

- **Trap:** if `template.metadata.labels` stops satisfying `spec.selector`, the API
  rejects it (`selector does not match template labels`) — same guardrail as a bare RS.
- **Rule:** keep template labels ⊇ selector labels. Adding EXTRA template labels is fine;
  removing a selector-required one is not.

---

### EC-4 — A rollout that "hangs" is usually readiness or image, not the Deployment

- **Diagnose (in order):**

  ```bash
  kubectl rollout status deployment/nginx-deploy   # is it progressing?
  kubectl get pods -l app=nginx                     # ImagePullBackOff? CrashLoop? not Ready?
  kubectl describe deployment nginx-deploy          # conditions: Progressing / Available
  kubectl describe pod <new-pod>                     # events for the failing new pod
  ```

- **Rule:** the Deployment is fine; a new pod can't get Ready. With `maxUnavailable: 0`
  the OLD version keeps serving — so you have time. Fix forward or `rollout undo`.

---

### EC-5 — Scaling is not a revision; template change is

- **Trap:** you expect `kubectl scale` to show up in `rollout history` — it doesn't.
- **Rule:** only **pod-template** changes create a new revision/ReplicaSet. `replicas`
  changes just resize the current RS. Two different kinds of change; don't conflate them.

---

### EC-6 — `revisionHistoryLimit: 0` deletes your rollback ability

- **Trap:** set it to 0 to "clean up" old RSes and you lose ALL rollback history.
- **Rule:** keep a sane limit (default 10; we use 5). Old scaled-to-zero RSes are cheap
  and ARE your undo button.

---

### EC-7 — HPA and a hard-coded `replicas` fight each other

- **Trap:** an HPA manages `replicas`, but your Git manifest also sets `replicas: 3`.
  Every `apply` resets the count and the HPA re-adjusts — flapping.
- **Fix/rule:** when an HPA owns scaling, **omit `replicas` from the manifest** (or use
  a server-side-apply field manager that yields it). Let one owner control the count.

---

### EC-8 — Rollout doesn't restart pods when only a ConfigMap/Secret changes

- **Trap:** you update a ConfigMap/Secret the pods consume, but the Deployment template
  didn't change → **no rollout**, pods keep the old config (env vars are injected at
  start; mounted files update eventually but the process may not re-read them).
- **Fix:** force a rollout: `kubectl rollout restart deployment/nginx-deploy`, or bump a
  template annotation (e.g. a checksum of the config) so the template genuinely changes.
- **Rule:** config changes are invisible to the Deployment unless the **template** changes.

---

### EC-9 — `kubectl rollout restart` ≠ delete/recreate

- **What it does:** patches the template with a timestamp annotation, triggering a normal
  **rolling** restart (respects `maxSurge`/`maxUnavailable`, zero-downtime).
- **Rule:** prefer it over `kubectl delete pod` loops to cycle pods (e.g. to pick up a
  rotated Secret or clear stuck state) — it's graceful and observable via `rollout status`.

---

### EC-10 — Deleting a Deployment cascades to RSes and Pods

- **Trap:** `kubectl delete deployment nginx-deploy` removes the RSes AND all Pods
  (foreground cascade) — the whole tree.
- **Keep the pods:** `kubectl delete deployment nginx-deploy --cascade=orphan` leaves the
  current RS + Pods running (rarely what you want, but useful in migrations).
- **Rule:** deleting the top of the tree deletes everything under it by default.

---

### EC-11 — A Deployment ADOPTS a hand-written ReplicaSet and drains it to 0 (WE HIT THIS)

- **Trap:** you have a standalone `nginx-rs` (from `replica.yaml`) running 3 pods. You then
  `kubectl apply -f deployment.yaml`. Suddenly `kubectl get rs` shows your `nginx-rs` at
  `DESIRED 0` — and you never scaled it. The Deployment did.
- **Why:** a Deployment finds "its" ReplicaSets by matching **its selector against RS
  labels** (not just pods). Both objects here use bare `app=nginx`:

  ```text
  Deployment nginx-deploy   selector: {app: nginx}
  ReplicaSet nginx-rs       labels:   {app: nginx}     ← matches → adopted
  ```

  The Deployment adopts `nginx-rs`, sees its pod template lacks the current
  `pod-template-hash`, treats it as an **old revision**, and scales it to 0 — exactly what
  it does to any superseded revision.
- **Diagnose (the smoking gun is in events):**

  ```bash
  kubectl get rs nginx-rs -o jsonpath='{.metadata.ownerReferences[*].name}{"\n"}'  # -> nginx-deploy
  kubectl get events --field-selector involvedObject.name=nginx-deploy | grep nginx-rs
  #   deployment/nginx-deploy  Scaled down replica set nginx-rs from 3 to 2
  #   ... from 2 to 1 ... from 1 to 0
  ```

- **Danger:** `nginx-rs` is now **owned by the Deployment**. `kubectl delete deployment
  nginx-deploy` will **cascade-delete your hand-written `nginx-rs` too**.
- **Why the Deployment's OWN RSes are safe:** it appends a unique `pod-template-hash` to
  its generated RSes' selectors (`app=nginx,pod-template-hash=cd4d84b57`), so revisions
  never collide. Your bare `nginx-rs` had no hash, so it looked like a stray old revision.
- **Fix/rule:** never let a Deployment's selector overlap the labels of a ReplicaSet (or
  Pod) you manage separately. Give each workload a **unique, stable label set**
  (`app.kubernetes.io/name` + `app.kubernetes.io/instance`), or don't run a standalone RS
  and a Deployment with the same `app` label in the same namespace. This is the same
  selector-collision family as `replica-tasks.md` EC-1/EC-2, seen from the Deployment side.

---

### EC-12 — Old ReplicaSets stay at 0, they are NOT deleted (this is rollback history)

- **Trap:** after changing the image you see two ReplicaSets and wonder why the old one
  wasn't cleaned up:

  ```text
  nginx-deploy-79d497f6b7   3   3   3   ← new revision (new image), serving
  nginx-deploy-cd4d84b57    0   0   0   ← old revision, kept at 0 (NOT deleted)
  ```

- **Why:** a rollout doesn't delete the old RS — it **scales it to 0** and keeps it. That
  parked, empty RS still holds the previous pod template (old image), so `rollout undo`
  can restore it by simply scaling it back up. Rollback = re-scale an old RS, not a rebuild.
- **Diagnose:**

  ```bash
  kubectl rollout history deployment/nginx-deploy      # each old RS = one revision
  kubectl get rs -l app=nginx -o custom-columns=\
  'RS:.metadata.name,DESIRED:.spec.replicas,IMAGE:.spec.template.spec.containers[0].image'
  ```

- **When old RSes DO get deleted:** bounded by `revisionHistoryLimit` (we set 5; default
  10). Once you exceed it, the Deployment garbage-collects the OLDEST parked RSes. Setting
  `revisionHistoryLimit: 0` deletes them all immediately → no rollback (see EC-6).
- **Rule:** an empty RS is cheap (no pods, just a small API object) and IS your undo
  button. Kubernetes trades a little `kubectl get rs` clutter for instant rollback. Don't
  `kubectl delete` old RSes by hand — you're deleting revision history.

---

## Cheat sheet

```bash
kubectl apply -f deployment.yaml
kubectl get deploy,rs,pods -l app=nginx          # see all three layers
kubectl rollout status  deployment/nginx-deploy  # is the rollout done?
kubectl rollout history deployment/nginx-deploy  # revisions + change-cause
kubectl set image deployment/nginx-deploy nginx=nginx:1.27-alpine
kubectl rollout undo deployment/nginx-deploy [--to-revision=N]
kubectl rollout restart deployment/nginx-deploy  # graceful cycle (pick up new config)
kubectl rollout pause|resume deployment/nginx-deploy
kubectl scale deployment/nginx-deploy --replicas=5
kubectl delete -f deployment.yaml
```

## Mental model to lock in

- Deployment = **ReplicaSet manager**; RS = **Pod manager**; Pod = **atomic unit**.
- Rolling update = **new RS up, old RS down**, paced by `maxSurge` / `maxUnavailable`.
- Rollback = re-scale an **old RS** back up (history bounded by `revisionHistoryLimit`).
- Only **template** changes make a revision; `replicas` changes are just scaling.
- **readiness** is the rollout's health signal — a bad new version stalls instead of
  taking down the old one (with `maxUnavailable: 0`).
- Config (ConfigMap/Secret) changes need `rollout restart` — they don't trigger one.

```text
Deployment ──▶ ReplicaSet v2 (new)  ──▶ Pods (new image)
     │
     └──────▶ ReplicaSet v1 (old, scaled to 0) ── kept for rollback
```
