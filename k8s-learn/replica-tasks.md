# ReplicaSet Tasks — Beginner → Advanced

A hands-on ladder for learning **ReplicaSets** using `replica.yaml` in this folder.
Do these AFTER `pod-tasks.md` — a ReplicaSet manages Pods, so Pods come first.

> The one idea: a ReplicaSet is a **control loop** that keeps
> `count(pods matching selector) == spec.replicas`, forever.

---

## Level 0 — Orientation

1. `kubectl explain replicaset.spec` — note the three key fields: `replicas`,
   `selector`, `template`.
2. Understand the reconcile loop: *observe* current pods → *compare* to desired
   `replicas` → *act* (create/delete). This "desired vs actual" pattern is the
   heart of ALL Kubernetes controllers.
3. Note `apiVersion: apps/v1` (not core `v1` like a Pod).

---

## Level 1 — Beginner: create & observe self-healing

- [ ] **Task 1.1 — Apply**
  - Do: `kubectl apply -f replica.yaml`
  - Verify: `kubectl get rs nginx-rs` → `DESIRED 3  CURRENT 3  READY 3`.

- [ ] **Task 1.2 — See the Pods it created**
  - Do: `kubectl get pods -l app=nginx --show-labels`
  - Learn: pod names look like `nginx-rs-abcde` — the RS generates a random suffix.

- [ ] **Task 1.3 — SELF-HEALING (the money demo)**
  - Do: delete one pod: `kubectl delete pod <name>` and immediately `kubectl get pods -w`.
  - Verify: a replacement appears within seconds. Count returns to 3.
  - Learn: this is reconciliation — the RS noticed actual(2) < desired(3) and acted.

- [ ] **Task 1.4 — Who owns this Pod?**
  - Do: `kubectl get pod <name> -o jsonpath='{.metadata.ownerReferences}{"\n"}'`
  - Learn: each Pod has an `ownerReference` back to `nginx-rs`. That's the link the
    garbage collector uses to clean up Pods when the RS is deleted.

---

## Level 2 — Scaling

- [ ] **Task 2.1 — Scale up imperatively**
  - Do: `kubectl scale rs nginx-rs --replicas=5`
  - Verify: two new Pods appear. `kubectl get rs` shows DESIRED 5.

- [ ] **Task 2.2 — Scale down**
  - Do: `kubectl scale rs nginx-rs --replicas=1`
  - Learn: the RS *deletes* extra pods. (Note: imperative scale and the file now
    disagree — see Task 2.4.)

- [ ] **Task 2.3 — Scale via the file (declarative)**
  - Edit `replica.yaml` → `replicas: 4`, then `kubectl apply -f replica.yaml`.
  - Learn: **declarative** (file is source of truth) vs **imperative** (`scale` command).
    Prefer declarative in real projects (GitOps).

- [ ] **Task 2.4 — Watch drift**
  - After a `kubectl scale`, run `kubectl apply -f replica.yaml` again — it snaps back
    to the file's value. Learn: whatever you `apply` wins; keep the file authoritative.

---

## Level 3 — Selectors & adoption (the tricky part)

- [ ] **Task 3.1 — Label surgery: orphan a Pod**
  - Do: `kubectl label pod <one-pod> app=notnginx --overwrite`
  - Verify: `kubectl get rs` → the RS immediately creates a NEW pod (its owned count
    dropped below 3). The relabeled pod is now orphaned and keeps running.
  - Learn: an RS owns pods **purely by label match**, not by who created them.

- [ ] **Task 3.2 — Adoption: give it back**
  - Relabel that orphan back to `app=nginx,tier=frontend`.
  - Verify: the RS now has one too many and DELETES one to get back to `replicas`.
  - Learn: RS will **adopt** any matching pod it finds — and cull extras.

- [ ] **Task 3.3 — matchExpressions**
  - Our selector requires `app=nginx` AND `tier In (frontend)`. Create a bare pod with
    only `app=nginx` (no `tier`) — the RS will NOT adopt it. Learn: all selector terms are ANDed.

- [ ] **Task 3.4 — The classic mismatch bug**
  - Temporarily make `template.metadata.labels` NOT match `selector.matchLabels`
    (e.g. change template label to `app: nginxx`). `kubectl apply`.
  - Verify: the API server **rejects** it (`selector does not match template labels`).
  - Learn: this guardrail prevents an RS from endlessly spawning pods it can't recognize.

---

## Level 4 — Failure & resilience

- [ ] **Task 4.1 — Node/pod failure simulation**
  - Delete pods in a loop: `for i in 1 2 3; do kubectl delete pod -l app=nginx --wait=false; done`
  - Learn: the RS keeps recreating — it always chases `replicas`.

- [ ] **Task 4.2 — minReadySeconds in action**
  - We set `minReadySeconds: 5`. Watch `kubectl get pods -w`: a new pod is `Running`
    but the RS's `READY`/available count lags ~5s. Learn: guards against flapping pods.

- [ ] **Task 4.3 — Delete the RS but KEEP the pods**
  - Do: `kubectl delete rs nginx-rs --cascade=orphan`
  - Verify: pods survive; only the controller is gone. Then re-apply the RS and watch
    it **adopt** the orphans instead of creating new ones.
  - Learn: `--cascade=orphan` vs default `--cascade=foreground` (deletes children too).

---

## Level 5 — Advanced: the leap to Deployments

- [ ] **Task 5.1 — Prove RS does NOT do rolling updates**
  - Change `template...image` to `nginx:1.27-alpine`, `kubectl apply -f replica.yaml`.
  - Verify: `kubectl get pods -o jsonpath='{.items[*].spec.containers[*].image}'` — the
    RUNNING pods still show the OLD image!
  - Learn: an RS only reconciles *count*, not *pod template changes* for existing pods.
    New image only appears on pods created AFTER the change (e.g. ones you delete/recreate).

- [ ] **Task 5.2 — Do it the right way with a Deployment**
  - Create `deployment.yaml`: copy `replica.yaml`, change `kind: Deployment`, keep the
    rest. `kubectl apply`, then change the image and apply again.
  - Verify: `kubectl rollout status deployment/nginx-deploy` — pods replaced gradually.
  - Learn: a **Deployment manages ReplicaSets** and orchestrates rolling updates by
    creating a new RS and scaling the old one down. `kubectl get rs` shows both.

- [ ] **Task 5.3 — Rollback**
  - `kubectl rollout undo deployment/nginx-deploy`; inspect `kubectl rollout history`.
  - Learn: Deployments keep revision history; RS alone has none. THIS is why you use
    Deployments in production and rarely touch ReplicaSets directly.

- [ ] **Task 5.4 — HorizontalPodAutoscaler (optional)**
  - `kubectl autoscale rs nginx-rs --min=2 --max=6 --cpu-percent=50` (needs metrics-server).
  - Learn: HPA edits `replicas` for you based on load — dynamic scaling.

---

## Level 6 — Edge Cases & Production Nuances

Same format as `pod-tasks.md` Level 7: **trap → reproduce → diagnose → fix/rule**.
These are the ReplicaSet-specific surprises.

---

### EC-1 — Cross-manifest adoption: an RS steals a standalone Pod (WE HIT THIS)

- **Trap:** you `kubectl get rs` and see `DESIRED 3 CURRENT 3 READY 3`, but only **2**
  `nginx-rs-xxxxx` pods exist. Where's the third? The RS **adopted a pod it never
  created** because that pod's labels match its selector.
- **What happened to us:** `pod.yaml`'s `nginx-pod` carries `app=nginx, tier=frontend`.
  The RS selector requires `app=nginx` AND `tier In (frontend)`. `nginx-pod` satisfies
  both → the RS counts it as one of its three and only creates 2 new pods:
  ```
  DESIRED 3 = nginx-pod (adopted) + nginx-rs-aaaaa + nginx-rs-bbbbb
  ```
- **Reproduce:** `kubectl apply -f pod.yaml` then `kubectl apply -f replica.yaml` — the RS
  creates only 2 pods.
- **Diagnose:**
  ```bash
  kubectl get pods -l 'app=nginx,tier=frontend' --show-labels   # everything the RS matches
  kubectl get pod nginx-pod -o jsonpath='{.metadata.ownerReferences[*].name}{"\n"}'  # -> nginx-rs
  ```
  The adopted pod gets an `ownerReference` pointing at the RS.
- **Danger:** delete the RS with the default cascade and it may delete your "standalone"
  pod too — because the RS now owns it.
- **Fix/rule:** selectors are cluster-wide label queries with **no respect for which file
  or object created a pod**. Give standalone pods distinct labels (or give the RS a more
  specific selector) so their label sets don't overlap. In production, prefer unique
  label keys per workload (e.g. `app.kubernetes.io/name` + `app.kubernetes.io/instance`).

---

### EC-2 — Overlapping selectors between two controllers = a tug-of-war

- **Trap:** two ReplicaSets (or an RS and a Deployment's RS) whose selectors both match the
  same pods will each try to drive the count — creating/deleting each other's pods forever.
- **Rule:** selectors across controllers must be **disjoint**. This is exactly why a
  Deployment auto-injects a unique `pod-template-hash` label into every pod and into its
  RS selector — so revisions never fight. Bare ReplicaSets have no such protection; you
  must keep their selectors unique yourself.

---

### EC-3 — The RS ignores template changes for existing pods

- **Trap:** edit `template...image` and `kubectl apply` — running pods keep the OLD image.
  `kubectl get rs` even shows the new template, yet nothing rolls.
- **Diagnose:** `kubectl get pods -o jsonpath='{.items[*].spec.containers[*].image}'` still
  shows the old image; only pods created AFTER the change (delete one to force it) get new.
- **Rule:** a ReplicaSet reconciles **count only**, never re-templates live pods. This one
  limitation is the entire reason Deployments exist → use a Deployment for anything you'll
  update.

---

### EC-4 — `--cascade=orphan` and re-adoption

- **Trap:** `kubectl delete rs nginx-rs --cascade=orphan` leaves the pods running; you
  think they're now independent. Re-apply the same RS and it **adopts them right back**
  (matching labels), possibly culling extras to hit `replicas`.
- **Rule:** orphaned pods are only "free" until another matching controller appears.
  Adoption is automatic and based solely on labels + an empty/again-matching ownerRef.

---

### EC-5 — Label surgery mid-flight triggers instant reconciliation

- **Trap:** `kubectl label pod <rs-pod> app=notnginx --overwrite` — the RS's owned count
  drops, so it **immediately creates a replacement**; the relabeled pod keeps running,
  orphaned. Net effect: you now have MORE pods than `replicas`.
- **Rule:** changing a pod's labels can move it into or out of a selector at any moment.
  The controller reacts in seconds. Relabel deliberately, not casually.

---

### EC-6 — Scaling drift: imperative `scale` vs the manifest

- **Trap:** `kubectl scale rs nginx-rs --replicas=7`, then someone runs
  `kubectl apply -f replica.yaml` (which says `replicas: 3`) — it snaps back to 3.
- **Rule:** whatever you last `apply` wins. Keep ONE source of truth (the file / GitOps);
  don't mix imperative `scale` with declarative `apply` on the same object.

---

## Cheat sheet

```bash
kubectl apply -f replica.yaml
kubectl get rs nginx-rs -o wide
kubectl get pods -l app=nginx --show-labels
kubectl scale rs nginx-rs --replicas=5
kubectl describe rs nginx-rs                 # events: created/deleted pods
kubectl delete rs nginx-rs --cascade=orphan  # keep pods, drop controller
kubectl delete -f replica.yaml
```

## Mental model to lock in
- ReplicaSet = **reconcile loop**: keep `count(selector matches) == replicas`.
- It owns Pods by **label selector**, via `ownerReferences` (adopts & culls to match).
- `selector.matchLabels` **must** be satisfied by `template.metadata.labels`.
- A ReplicaSet gives **self-healing + scaling**, but **no rolling updates/rollback**.
- That gap is exactly why **Deployments** exist — a Deployment drives ReplicaSets.
```
Pod  ──owned by──▶  ReplicaSet  ──owned by──▶  Deployment
(atomic unit)      (keeps N copies)          (rolling updates + rollback)
```
