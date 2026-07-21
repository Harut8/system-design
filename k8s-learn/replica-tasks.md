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
