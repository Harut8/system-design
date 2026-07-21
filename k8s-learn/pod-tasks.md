# Pod Tasks — Beginner → Advanced

A hands-on ladder for learning Kubernetes **Pods** using `pod.yaml` in this folder.
Work top to bottom. Each task has a **Goal**, **Do**, and **Verify/Learn**.

> Setup: you need a cluster. Easiest local options:
> - `minikube start`  (or)  `kind create cluster`  (or) Docker Desktop's built-in k8s.
> - `kubectl version` should show both Client and Server.
> - `kubectl config current-context` shows which cluster you're pointed at.

---

## Level 0 — Orientation (know your tools)

1. **What is a Pod?** In one sentence: the smallest deployable unit — one or more
   containers that share a network IP and can share storage, always scheduled together.
2. Run `kubectl api-resources | grep -i pod` — note that `pods` is in the core (`v1`) group.
3. `kubectl explain pod` then `kubectl explain pod.spec.containers` — this built-in
   docs command is your best friend. Use `--recursive` to see the whole tree.

---

## Level 1 — Beginner: create, inspect, delete

- [ ] **Task 1.1 — Apply the Pod**
  - Do: `kubectl apply -f pod.yaml`
  - Verify: `kubectl get pods` → status should move `Init → PodInitializing → Running`.
  - Learn: the `Init` phase is the init container running first.

- [ ] **Task 1.2 — Read the details**
  - Do: `kubectl describe pod nginx-pod`
  - Learn: find the **Events** at the bottom — pulled image, created container, etc.
    Events are the first place to look when something is broken.

- [ ] **Task 1.3 — Wide + labels**
  - Do: `kubectl get pod nginx-pod -o wide --show-labels`
  - Learn: `-o wide` shows the node + Pod IP; labels are what selectors match on.

- [ ] **Task 1.4 — See the YAML the cluster actually stored**
  - Do: `kubectl get pod nginx-pod -o yaml | less`
  - Learn: the cluster ADDS fields (status, defaults, `nodeName`). Compare to your file.

- [ ] **Task 1.5 — Logs (both containers)**
  - Do: `kubectl logs nginx-pod -c nginx` then `kubectl logs nginx-pod -c log-sidecar`
  - Learn: a multi-container Pod needs `-c <container>` to disambiguate.

- [ ] **Task 1.6 — Clean up**
  - Do: `kubectl delete -f pod.yaml`  (or `kubectl delete pod nginx-pod`)

---

## Level 2 — Interacting with a running Pod

- [ ] **Task 2.1 — Exec a shell**
  - Do: `kubectl exec -it nginx-pod -c nginx -- sh` then `ls /usr/share/nginx/html`
  - Learn: you should see `index.html` written by the **init container**. Type `exit`.

- [ ] **Task 2.2 — Port-forward and hit it**
  - Do: `kubectl port-forward pod/nginx-pod 8080:80` then in another terminal `curl localhost:8080`
  - Verify: you see `<h1>Hello from init container</h1>`.

- [ ] **Task 2.3 — Prove containers share localhost**
  - Do: exec into `log-sidecar`, run `wget -qO- localhost:80`.
  - Learn: the sidecar reaches nginx over `localhost` — they share one network namespace.

- [ ] **Task 2.4 — Copy files in/out**
  - Do: `kubectl cp nginx-pod:/usr/share/nginx/html/index.html ./copied.html -c nginx`

---

## Level 3 — Configuration & the Downward API

- [ ] **Task 3.1 — Create the ConfigMap referenced by the Pod**
  - Do: `kubectl create configmap app-config --from-literal=GREETING=hello`
  - Re-apply the Pod and `kubectl exec ... -- env | grep GREETING`.
  - Learn: `envFrom` + `configMap` mount config without rebuilding the image.

- [ ] **Task 3.2 — Create the Secret referenced by the Pod**
  - Do: `kubectl create secret generic app-secret --from-literal=password=s3cr3t`
  - Verify: `kubectl exec ... -- printenv DB_PASSWORD` → `s3cr3t`.
  - Learn: Secrets are base64 (NOT encrypted at rest by default) — treat as config, not vault.

- [ ] **Task 3.3 — Downward API**
  - Do: `kubectl exec nginx-pod -c nginx -- printenv MY_POD_IP`
  - Learn: `fieldRef: status.podIP` injects runtime facts about the Pod into env vars.

- [ ] **Task 3.4 — Mount config as files**
  - Add a key to `app-config`, mount it, and read the file inside the container.
  - Learn: ConfigMap keys become filenames under the mount path.

---

## Level 4 — Health, probes & self-diagnosis

- [ ] **Task 4.1 — Watch the probes**
  - Do: `kubectl describe pod nginx-pod` → find `Liveness`, `Readiness`, `Startup` lines.
  - Learn: **startup** gates the others; **liveness** restarts; **readiness** gates traffic.

- [ ] **Task 4.2 — Break liveness on purpose**
  - Edit `pod.yaml`: set `livenessProbe.httpGet.path: /does-not-exist`, re-apply.
  - Verify: `kubectl get pod -w` → `RESTARTS` count climbs. Read the events.
  - Learn: a failing liveness probe = repeated container restarts (`CrashLoopBackOff` if fast).
  - Revert the change.

- [ ] **Task 4.3 — Break readiness on purpose**
  - Point `readinessProbe` at a bad path. Verify the Pod stays `Running` but `READY 1/2`.
  - Learn: readiness failure does **not** restart — it just withholds traffic.

- [ ] **Task 4.4 — Diagnose a real failure**
  - Change `image:` to `nginx:doesnotexist`, re-apply.
  - Verify: `kubectl get pod` → `ErrImagePull` / `ImagePullBackOff`; confirm via events.
  - Revert.

---

## Level 5 — Resources, QoS & scheduling

- [ ] **Task 5.1 — Identify the QoS class**
  - Do: `kubectl get pod nginx-pod -o jsonpath='{.status.qosClass}{"\n"}'`
  - Learn: requests==limits ⇒ `Guaranteed`; some set ⇒ `Burstable`; none ⇒ `BestEffort`.
    QoS decides who gets evicted first under node pressure.

- [ ] **Task 5.2 — Trigger an OOMKill**
  - Add a container that allocates memory past its limit (e.g. `stress`), watch it get
    `OOMKilled` in `kubectl describe`. Learn: memory limit is a HARD ceiling.

- [ ] **Task 5.3 — Make it unschedulable**
  - Set `resources.requests.cpu: "1000"` (1000 cores). Re-apply.
  - Verify: Pod stuck `Pending`; `describe` shows `FailedScheduling / Insufficient cpu`.
  - Learn: the **scheduler** places pods by *requests*; no node fits ⇒ Pending. Revert.

- [ ] **Task 5.4 — nodeSelector / affinity**
  - `kubectl label node <node> disktype=ssd`, uncomment `nodeSelector` in `pod.yaml`, re-apply.
  - Learn: labels + selectors also drive *scheduling*, not just service routing.

---

## Level 6 — Advanced

- [ ] **Task 6.1 — Static / imperative creation & dry-run**
  - Do: `kubectl run tmp --image=nginx:1.25-alpine --dry-run=client -o yaml`
  - Learn: fastest way to scaffold YAML; `--dry-run=server` validates against the API.

- [ ] **Task 6.2 — Security context hardening**
  - Confirm non-root: `kubectl exec nginx-pod -c nginx -- id` → uid should be `101`.
  - Set `readOnlyRootFilesystem: true` and observe what breaks; fix with an `emptyDir` for
    the paths nginx must write. Learn: least-privilege containers.

- [ ] **Task 6.3 — Graceful shutdown**
  - `kubectl delete pod nginx-pod` and watch timing. The `preStop` hook + 
    `terminationGracePeriodSeconds` control the SIGTERM→SIGKILL window.
  - Learn: how to drain connections cleanly on shutdown.

- [ ] **Task 6.4 — Ephemeral debug container** (k8s ≥ 1.25)
  - Do: `kubectl debug -it nginx-pod --image=busybox --target=nginx -- sh`
  - Learn: debug a distroless/minimal container without rebuilding it.

- [ ] **Task 6.5 — Why bare Pods are fragile**
  - `kubectl delete pod nginx-pod` — it's gone forever, nothing recreates it.
  - Learn: this is the motivation for controllers → continue in **replica-tasks.md**.

---

## Level 7 — Edge Cases & Production Nuances

The gotchas that bite you in real clusters. Each is a self-contained lesson: the
**trap**, how to **reproduce**, how to **diagnose**, and the **fix/rule**.

---

### EC-1 — initContainer issues (a stuck init blocks the whole Pod)

- **Trap:** init containers run **in order, to completion, before any app container
  starts**. If one fails or hangs, the Pod is stuck in `Init:0/1` / `Init:Error` /
  `Init:CrashLoopBackOff` and your app never even begins.
- **Reproduce:** edit `pod.yaml` `initContainers[0].command` to fail:
  `["sh","-c","echo starting; exit 1"]`, then re-create the Pod.
- **Diagnose:**
  ```bash
  kubectl get pod nginx-pod                       # STATUS shows Init:Error / Init:CrashLoopBackOff
  kubectl logs nginx-pod -c init-html             # logs of THIS init container by name
  kubectl logs nginx-pod -c init-html --previous  # if it already restarted
  kubectl describe pod nginx-pod                   # Init Containers section + events
  ```
- **Rules:**
  - Init containers obey `restartPolicy`: with `Always`, a failed init **retries with backoff** (looks like a crashloop but in the Init phase).
  - `kubectl logs <pod>` alone won't show init logs — you MUST pass `-c <initName>`.
  - A hanging init (e.g. `wait-for-db` that never connects) makes the Pod sit in `Init` forever — check the init's logs, not the app's.
  - Keep init work idempotent; it can re-run on retry.

---

### EC-2 — Debugging an already-killed / crashlooping container

- **Trap:** you can't `exec` into a container that keeps dying — it's never up long enough (`container not found`). The logs you need belong to the **dead** instance.
- **The key commands:**
  ```bash
  kubectl logs nginx-pod -c nginx --previous      # logs of the PREVIOUS (dead) container
  kubectl describe pod nginx-pod                    # Last State: Terminated → Reason + Exit Code
  ```
- **Exit code cheat table:**
  | Exit code / reason | Meaning |
  |--------------------|---------|
  | `OOMKilled` (137)  | hit memory **limit** → raise limit or fix leak |
  | `Error` (1)        | app threw on startup (bad config, missing env/secret, failed dependency) |
  | `137` (not OOM)    | SIGKILL — often a liveness probe killing a slow starter |
  | `143`              | SIGTERM — normal shutdown unless it can't finish in the grace period |
  | `CreateContainerConfigError` | missing ConfigMap/Secret the Pod references |
- **When it dies too fast to inspect** — keep a copy alive:
  ```bash
  # ephemeral debug container sharing the pod's namespaces (k8s >= 1.25):
  kubectl debug -it nginx-pod --image=busybox --target=nginx -- sh
  # or a copy with the entrypoint overridden so it stays up:
  kubectl debug nginx-pod -it --copy-to=debug-pod --container=nginx -- sh
  ```
  Classic trick: override `command` to `["sleep","3600"]` in a copy, then inspect env/config/network.

---

### EC-3 — CrashLoopBackOff runbook (it's a symptom, not a cause)

- **What it is:** the container keeps exiting, so kubelet restarts it with **exponential
  backoff** (10s → 20s → 40s … capped at 5 min). Find *why it exits*.
- **Prod order of operations:**
  1. **New deploy? Roll back first, debug after** (with a Deployment: `kubectl rollout undo deployment/<name>`). Restore service, then investigate the bad version elsewhere.
  2. `kubectl logs <pod> --previous` — what did it say as it died?
  3. `kubectl describe pod <pod>` — `Last State` exit code (use EC-2 table).
  4. `kubectl get events --field-selector involvedObject.name=<pod> --sort-by=.lastTimestamp` (grab before TTL expiry — see EC-5).
- **Most common causes → fix:**
  - Liveness probe too aggressive on a slow starter → add/raise a **`startupProbe`** or `initialDelaySeconds`/`failureThreshold`.
  - Missing Secret/ConfigMap → verify it exists in the right namespace.
  - `OOMKilled` → raise memory limit *and* investigate the leak.
  - Can't reach a dependency at boot → retry-with-backoff in the app or gate with an initContainer, don't crash-on-boot.
  - Wrong image/entrypoint (`exec format error`) → check tag and `command`/`args`.
- **Reproduce:** point `livenessProbe.httpGet.path` at `/nope` (Task 4.2) and watch `RESTARTS` climb into `CrashLoopBackOff`.

---

### EC-4 — preStop hook: observability & failure semantics

- **Trap:** a successful `preStop` is **silent** — there's no "preStop succeeded" field.
- **How to know it ran:**
  - Failure emits an event: `kubectl get events --field-selector reason=FailedPreStopHook`.
  - Success: build in evidence — write to PID 1's stdout so it lands in logs:
    `["sh","-c","echo preStop@$(date) > /proc/1/fd/1; sleep 5; nginx -s quit"]`
    then `kubectl logs nginx-pod -c nginx` **before the Pod is deleted** (logs die with the Pod).
- **Failure semantics (important):** preStop is **best-effort, non-blocking**:
  - Hook errors / not found → `FailedPreStopHook` warning, shutdown **continues anyway** (SIGTERM still fires).
  - Hook slower than `terminationGracePeriodSeconds` → it's **cut off**; the grace budget covers preStop + SIGTERM combined, then SIGKILL.
  - `httpGet` preStop ignores response codes — even a 500 counts as "done."
- **Rule:** never rely on preStop for must-happen cleanup; also handle SIGTERM in the app. Keep preStop fast, idempotent, and shorter than the grace period.

---

### EC-5 — Events are ephemeral (don't treat them as an audit log)

- **Trap:** events are among the shortest-lived objects in k8s.
- **Two ways they vanish:**
  1. **TTL** — API server `--event-ttl` defaults to **1h**; the event is GC'd after that even if the problem persists.
  2. **Object deleted** — delete the Pod and its events are cleaned up (why `describe` on a deleted Pod shows nothing).
- **Also:** identical repeated events are **deduplicated** — one row with a `count` field (e.g. "restarted 200×" = 1 event, `count: 200`), not 200 rows.
- **Diagnose live:**
  ```bash
  kubectl get events --sort-by=.lastTimestamp
  kubectl get events --field-selector involvedObject.name=nginx-pod
  kubectl get events -w
  ```
- **For permanence:** ship events to a log system (event exporter → Loki/ES/Datadog); use the **API server audit log** for "who did what"; use `kubectl logs --previous` for crash forensics.

---

### EC-6 — `kubectl delete pod` is graceful termination, not a kill

- **Sequence:** DELETE request → API server sets `deletionTimestamp` (status `Terminating`,
  object still exists) → `preStop` runs → **SIGTERM** → grace-period countdown
  (`terminationGracePeriodSeconds`) → **SIGKILL** if still alive → object removed
  (`kubectl get pod` → `NotFound`).
- **Flags:**
  | Flag | Effect |
  |------|--------|
  | *(none)* | graceful; blocks until done |
  | `--wait=false` | fire-and-forget; returns immediately (can cause an `apply` conflict against a still-terminating Pod — we hit this) |
  | `--now` | grace period 1s |
  | `--grace-period=0 --force` | skip graceful shutdown, remove from API now (data-loss risk; last resort) |
  | `--cascade=orphan` | for controllers: delete controller, **keep** Pods |
- **Rule:** a **bare Pod deleted = gone forever** (no self-healing). A Pod owned by a ReplicaSet/Deployment gets **recreated** — that's the whole point of controllers.

---

### EC-7 — Pods are (almost) immutable after creation

- **Trap:** re-`apply`ing a changed Pod fails with
  `Forbidden: pod updates may not change fields other than spec.containers[*].image, ...`.
- **Only these are mutable in place:** container/init `image`, `activeDeadlineSeconds`,
  `tolerations` (additions only), and `terminationGracePeriodSeconds` (narrow case).
- **Everything else** (volumes, env, resources, probes) requires **delete + recreate**.
- **Rule:** if `apply` refuses, `kubectl delete pod <name> --wait=true` then `apply` again — and note this is *another* reason to use Deployments (they recreate Pods for you on template changes).

---

### EC-8 — Volume mounts SHADOW whatever the image had at that path

- **Trap:** mounting a volume onto `/etc/nginx/conf.d` **hides** the image's built-in
  `default.conf` — nginx then has no server block. (This is a bug we hit: an *optional,
  missing* ConfigMap mounted as an empty dir wiped the config.)
- **Rule:** mounting onto a populated image directory replaces its contents. Only mount
  config dirs when your ConfigMap actually contains the needed files; or mount a single
  file with `subPath` to avoid hiding siblings.

---

### EC-9 — Non-root containers need writable dirs handed to them

- **Trap:** `runAsNonRoot: true` + stock nginx = `open("/var/run/nginx.pid") Permission
  denied` → CrashLoop. Image dirs like `/var/run`, `/var/cache/nginx` are root-owned.
- **Fix (already applied in `pod.yaml`):** mount `emptyDir` volumes at each writable path
  and set `fsGroup` so the non-root user can write:
  ```yaml
  securityContext: { fsGroup: 101 }
  volumeMounts:
    - { name: var-run,   mountPath: /var/run }
    - { name: var-cache, mountPath: /var/cache/nginx }
  ```
- **Rule:** when hardening (non-root and/or `readOnlyRootFilesystem: true`), enumerate
  every path the process writes and back each with a writable volume. Alternatively use a
  purpose-built unprivileged image (e.g. `nginxinc/nginx-unprivileged`).

---

### EC-10 — Pending vs Failed: scheduling problems look different from crashes

- **Trap:** a Pod stuck in `Pending` never ran a container — so `logs` is empty and there's
  nothing to `exec` into. The problem is placement, not the app.
- **Diagnose:** `kubectl describe pod <pod>` → events like `FailedScheduling: Insufficient
  cpu` / `node(s) had untolerated taint` / `didn't match node selector`.
- **Common causes:** requests larger than any node, unsatisfiable affinity/nodeSelector,
  missing tolerations for a tainted node, or an unbound PersistentVolumeClaim.
- **Rule:** `Pending` = scheduler/resources/volumes; `CrashLoopBackOff`/`Error` = the app.
  Don't hunt for app bugs when the Pod never scheduled.

---

## Cheat sheet

```bash
kubectl apply -f pod.yaml              # create/update
kubectl get pods -o wide --show-labels # list
kubectl describe pod nginx-pod         # events + config (debug here first)
kubectl logs -f nginx-pod -c nginx     # follow logs of a container
kubectl exec -it nginx-pod -c nginx -- sh
kubectl port-forward pod/nginx-pod 8080:80
kubectl explain pod.spec.containers.resources   # built-in docs
kubectl delete -f pod.yaml
```

## Mental model to lock in
- Pod = **atomic unit** of scheduling; containers in it share network + can share volumes.
- **initContainers** run first, in order, to completion. **containers** run together.
- **requests** = scheduling/guarantee, **limits** = hard ceiling.
- **liveness** restarts, **readiness** gates traffic, **startup** protects slow boots.
- A bare Pod has **no self-healing** — that's what ReplicaSets/Deployments add.
