# Config & Storage Tasks — ConfigMaps, Secrets, Volumes, PV/PVC

Track A module 4. Do after `env-config-secrets-tasks.md` (module 3b), which covers
the same ConfigMaps and Secrets from the **developer/env** side — Levels 1–2 here
are the short version of it. This file is the **storage** side.
Read alongside: `../kubernetes/19-storage-csi-pv-pvc.md`.

> The one idea: **a Pod's filesystem is assembled at start time from sources with
> different lifetimes.** The container image is immutable and per-container.
> `emptyDir` dies with the Pod. A PVC outlives the Pod. Confusing those three is
> the cause of nearly every "where did my data go" incident.
>
> ```
>   image layers   ─ lifetime: forever, read-only
>   emptyDir       ─ lifetime: the Pod          (deleted with it)
>   configMap/secret ─ lifetime: the object      (projected, read-only)
>   PVC → PV       ─ lifetime: independent       (survives the Pod)
>   hostPath       ─ lifetime: the node          (and a security hole)
> ```

Setup: `kubectl create ns cfg-lab`. All commands assume `-n cfg-lab`.

---

## Level 0 — Orientation

1. `kubectl explain pod.spec.volumes` — one volume list per Pod, mounted per
   container. Volumes belong to the **Pod**, mounts belong to the **container**.
2. `kubectl explain pod.spec.containers.volumeMounts`
3. `kubectl api-resources | grep -E 'persistentvolume|storageclass|configmap|secret'`
4. Mental split: **ConfigMap/Secret are API objects projected as files or env.
   PV/PVC are real storage.** They share the volume mechanism and nothing else.

---

## Level 1 — ConfigMaps

- [ ] **Task 1.1 — Three ways to create one**
  ```bash
  kubectl create cm literal-cm -n cfg-lab --from-literal=LOG_LEVEL=debug --from-literal=REGION=eu
  printf 'key=value\nother=thing\n' > app.properties
  kubectl create cm file-cm -n cfg-lab --from-file=app.properties
  kubectl create cm env-cm  -n cfg-lab --from-env-file=app.properties
  ```
  - Do: `kubectl get cm -n cfg-lab -o yaml | grep -A5 '^  data'`
  - Learn: `--from-file` makes **one key whose value is the whole file**.
    `--from-env-file` makes **one key per line**. Same input, completely different
    object.

- [ ] **Task 1.2 — As environment variables**
  ```yaml
  env:
    - name: LOG_LEVEL
      valueFrom: {configMapKeyRef: {name: literal-cm, key: LOG_LEVEL}}
  envFrom:
    - configMapRef: {name: literal-cm}
  ```
  - Learn: `env` picks keys explicitly; `envFrom` injects all of them. `envFrom`
    silently skips keys that aren't valid env var names.

- [ ] **Task 1.3 — Env vars are a snapshot**
  - Do: run a pod with the env above, then `kubectl edit cm literal-cm` and change
    `LOG_LEVEL`. `kubectl exec` into the pod and `echo $LOG_LEVEL`.
  - Verify: **unchanged.**
  - Learn: env is resolved once, at container start. There is no mechanism to
    update it. Changing a ConfigMap consumed as env does nothing until the Pod is
    replaced — and nothing replaces it automatically.

- [ ] **Task 1.4 — As a mounted volume**
  ```yaml
  volumes:
    - name: cfg
      configMap: {name: file-cm}
  volumeMounts:
    - name: cfg
      mountPath: /etc/app
  ```
  - Do: `kubectl exec ... -- ls -l /etc/app`
  - Verify: `app.properties` is a **symlink** into `..data/`.
  - Learn: the double-symlink indirection is how the kubelet swaps content
    atomically — you never see a half-written file.

- [ ] **Task 1.5 — Mounted volumes DO update**
  - Do: edit the ConfigMap, then poll `kubectl exec ... -- cat /etc/app/app.properties`
  - Verify: it changes, after up to ~60–90s (kubelet sync period + cache TTL).
  - Learn: **mounted = eventually updated, env = never updated.** This asymmetry
    is the single most useful fact in this file. If your app needs live config,
    mount it and watch the file.

- [ ] **Task 1.6 — subPath breaks updates**
  ```yaml
  volumeMounts:
    - {name: cfg, mountPath: /etc/app/app.properties, subPath: app.properties}
  ```
  - Verify: the file appears at the exact path — and **never updates again**.
  - Learn: `subPath` copies rather than symlinks, so it opts out of the atomic-swap
    mechanism. Use it when you must land a file into a directory that already has
    content, and accept that it's frozen.

- [ ] **Task 1.7 — Immutable ConfigMaps**
  - Do: add `immutable: true` and try to edit.
  - Learn: rejected. Also removes the kubelet's watch, which is a real scale win
    on large clusters. Roll by creating a new name.

---

## Level 2 — Secrets

- [ ] **Task 2.1 — Base64 is not encryption**
  ```bash
  kubectl create secret generic db -n cfg-lab --from-literal=password=hunter2
  kubectl get secret db -n cfg-lab -o jsonpath='{.data.password}' | base64 -d
  ```
  - Learn: anyone with `get secret` has the plaintext. Secrets differ from
    ConfigMaps in RBAC conventions, `tmpfs` mounting, and etcd-at-rest encryption
    *if configured* — not in the object itself.

- [ ] **Task 2.2 — Secrets mount as tmpfs**
  - Do: mount a secret, then `kubectl exec ... -- df -h /etc/secret`
  - Verify: `tmpfs`.
  - Learn: never written to node disk. Also means it counts against the
    container's memory (`resources-tasks.md` EC-8).

- [ ] **Task 2.3 — Types matter**
  ```bash
  kubectl create secret docker-registry regcred -n cfg-lab \
    --docker-server=x --docker-username=u --docker-password=p
  kubectl get secret regcred -n cfg-lab -o jsonpath='{.type}{"\n"}'
  ```
  - Learn: `kubernetes.io/dockerconfigjson`, `kubernetes.io/tls`,
    `kubernetes.io/service-account-token` are structurally validated and consumed
    by specific machinery (`imagePullSecrets`, Ingress TLS). `Opaque` is the
    unvalidated default.

- [ ] **Task 2.4 — Check at-rest encryption**
  - Do (kind): `docker exec -it op-control-plane sh -c "ETCDCTL_API=3 etcdctl --cacert /etc/kubernetes/pki/etcd/ca.crt --cert /etc/kubernetes/pki/etcd/server.crt --key /etc/kubernetes/pki/etcd/server.key get /registry/secrets/cfg-lab/db"`
  - Verify: on a default cluster you can read the value.
  - Learn: `EncryptionConfiguration` on the API server is opt-in. Most clusters
    don't have it. Assume plaintext in etcd unless you verified otherwise.

---

## Level 3 — Ephemeral volumes

- [ ] **Task 3.1 — emptyDir shares between containers**
  - Do: a Pod with two containers, both mounting one `emptyDir`. Write from one,
    read from the other.
  - Learn: this is the sidecar pattern's data channel. Scoped to the Pod, deleted
    with it — **including on eviction and rescheduling**.

- [ ] **Task 3.2 — emptyDir survives container restart, not Pod deletion**
  - Do: write a file, `kubectl exec ... -- kill 1`, wait for restart, read it.
  - Verify: still there. Then delete the Pod and recreate — gone.
  - Learn: the boundary is the **Pod sandbox**, not the container. Same lesson as
    `pod-tasks.md` on restarts.

- [ ] **Task 3.3 — Memory-backed emptyDir**
  ```yaml
  emptyDir: {medium: Memory, sizeLimit: 64Mi}
  ```
  - Learn: tmpfs, charged to the container's memory limit. Always set `sizeLimit`
    (`resources-tasks.md` EC-8).

- [ ] **Task 3.4 — hostPath, and why it's a red flag**
  - Do: mount `hostPath: {path: /}` and browse the node's filesystem from the pod.
  - Learn: this is a full node compromise in one YAML field. It also breaks
    scheduling assumptions — the pod is now pinned to whatever node happens to
    have the right data. Pod Security Standards `baseline` blocks it.

- [ ] **Task 3.5 — The projected volume**
  ```yaml
  volumes:
    - name: all-in-one
      projected:
        sources:
          - configMap: {name: literal-cm}
          - secret: {name: db}
          - serviceAccountToken: {path: token, expirationSeconds: 3600, audience: vault}
  ```
  - Learn: combines sources into one directory. The `serviceAccountToken` source is
    how modern **bound, expiring** SA tokens work — audience-scoped, auto-rotated,
    and tied to the Pod's lifetime. Unlike the legacy forever-tokens in Secrets.

---

## Level 4 — PersistentVolumes and Claims

- [ ] **Task 4.1 — The three objects**
  ```bash
  kubectl get storageclass
  kubectl apply -f - <<'EOF'
  apiVersion: v1
  kind: PersistentVolumeClaim
  metadata: {name: data, namespace: cfg-lab}
  spec:
    accessModes: [ReadWriteOnce]
    resources: {requests: {storage: 1Gi}}
  EOF
  kubectl get pvc,pv -n cfg-lab
  ```
  - Learn: **PVC is the request (namespaced), PV is the resource (cluster-scoped),
    StorageClass is the factory.** The PVC is the only one an application author
    should ever write.

- [ ] **Task 4.2 — WaitForFirstConsumer**
  - Do: `kubectl get sc standard -o jsonpath='{.volumeBindingMode}{"\n"}'`
  - Verify: on kind, `WaitForFirstConsumer`, and the PVC sits `Pending` with no PV.
  - Do: create a Pod using it → it binds.
  - Learn: binding is deferred until scheduling, so the volume is provisioned in
    the **same zone as the Pod**. With `Immediate`, a volume can be created in
    zone A while the Pod can only fit in zone B — permanently unschedulable.

- [ ] **Task 4.3 — Access modes are node-level, not pod-level**
  - Learn: `ReadWriteOnce` = one **node**, and multiple Pods on that node may share
    it. `ReadWriteOncePod` (1.29+) is the real "exactly one Pod." `ReadWriteMany`
    needs a filesystem backend (NFS, CephFS) — most block storage cannot do it, no
    matter what you request.

- [ ] **Task 4.4 — Prove persistence**
  - Do: Pod writes a file to the PVC → delete Pod → new Pod mounts the same PVC.
  - Verify: file is there.
  - Learn: this is the whole point. Contrast with Task 3.2.

- [ ] **Task 4.5 — Reclaim policy**
  - Do: `kubectl get pv -o custom-columns='NAME:.metadata.name,RECLAIM:.spec.persistentVolumeReclaimPolicy,STATUS:.status.phase'`
  - Learn: `Delete` (dynamic default) destroys real data when the PVC is deleted.
    `Retain` keeps the PV in `Released` state — safe, and it will **not** rebind to
    a new PVC without manual intervention.

- [ ] **Task 4.6 — Expand a PVC**
  - Do: check `allowVolumeExpansion` on the SC, then edit the PVC's storage request
    upward.
  - Verify: `kubectl describe pvc data` → `FileSystemResizePending`, resolved on Pod
    restart.
  - Learn: expansion only. **Shrinking is not supported by any driver.** Size up
    cautiously.

---

## Level 5 — Advanced

- [ ] **Task 5.1 — Ephemeral PVCs**
  ```yaml
  volumes:
    - name: scratch
      ephemeral:
        volumeClaimTemplate:
          spec:
            accessModes: [ReadWriteOnce]
            resources: {requests: {storage: 1Gi}}
  ```
  - Learn: a real PVC created and deleted with the Pod. Right answer for large
    scratch space that shouldn't be charged to memory or the node's ephemeral
    storage — GPU training checkpoints, for example.

- [ ] **Task 5.2 — fsGroup and permissions**
  ```yaml
  securityContext: {fsGroup: 2000}
  ```
  - Learn: the kubelet chowns the volume to that GID on mount. Without it, a
    non-root container commonly can't write to its own PVC — the most common
    "permission denied" in Kubernetes. On big volumes the recursive chown is slow;
    `fsGroupChangePolicy: OnRootMismatch` fixes that.

- [ ] **Task 5.3 — Where CSI actually is**
  - Do: `kubectl get csidrivers && kubectl get pods -n kube-system | grep csi`
  - Learn: a controller Deployment (provision/attach) plus a node DaemonSet
    (mount). `19-storage-csi-pv-pvc.md` covers the full attach/mount sequence —
    read it now, it'll land.

---

## Level 6 — Edge Cases & Production Nuances

### EC-1 — ConfigMap change did nothing

- **Trap:** you update a ConfigMap and the app doesn't notice.
- **Why:** consumed as **env** (Task 1.3), or mounted with **subPath** (Task 1.6),
  or the app read the file once at boot.
- **Fix:** roll the Deployment. The idiomatic trigger is a checksum annotation on
  the pod template, so a config change produces a new template hash and a rollout:
  ```yaml
  annotations:
    checksum/config: "{{ sha256sum of the configmap }}"
  ```
- **Rule:** Kubernetes does not restart Pods when a ConfigMap changes. Nothing
  does, unless you build it.

---

### EC-2 — Pod stuck `ContainerCreating` forever

- **Diagnose:** `kubectl describe pod` events, in this order:
  - `configmap "x" not found` → the object doesn't exist *in this namespace*
  - `FailedAttachVolume` / `Multi-Attach error` → RWO volume still attached to
    another node, usually a Pod that hasn't fully terminated
  - `FailedMount ... timed out waiting for the condition` → CSI driver problem
- **Rule:** missing ConfigMaps/Secrets block startup **silently and forever** — no
  crash, no restart count, nothing in logs. Always check events first.

---

### EC-3 — Multi-Attach on a StatefulSet rollout

- **Trap:** RWO volume, node dies, Pod reschedules and hangs in
  `ContainerCreating` with `Multi-Attach error`.
- **Why:** the old Pod is unreachable but not confirmed deleted, so the volume is
  still attached to the dead node. Kubernetes cannot safely force-detach — it
  might corrupt the filesystem.
- **Fix:** delete the old Pod with `--force --grace-period=0`, and only once you
  are certain the node is genuinely gone.
- **Rule:** RWO plus node failure equals manual intervention. This is why real
  stateful systems replicate at the application layer instead of relying on volume
  failover.

---

### EC-4 — Deleting a PVC deletes the data

- **Trap:** `kubectl delete pvc` on a `Delete`-reclaim SC, and the real volume is
  gone. There is no undo.
- **Protection:** the `kubernetes.io/pvc-protection` finalizer only prevents
  deletion **while a Pod is using it** — it does nothing once the Pod is gone.
- **Rule:** production data gets `Retain`, plus backups. `Delete` is right for CI
  and scratch, wrong for anything you'd miss.

---

### EC-5 — Secrets in env vars leak

- **Trap:** `kubectl describe pod` shows env var *names*, crash dumps and logging
  frameworks often show *values*, and child processes inherit the whole
  environment.
- **Rule:** mount secrets as files. `tmpfs`, not inherited, not in `/proc/1/environ`,
  and rotatable without a restart.

---

### EC-6 — A namespace won't delete because of a PVC

- **Trap:** namespace stuck `Terminating`.
- **Diagnose:** `kubectl get pvc -n <ns> -o json | jq '.items[].metadata.finalizers'`
- **Why:** the pvc-protection finalizer is waiting for a Pod that no longer exists,
  or a CSI driver that's been uninstalled.
- **Rule:** same shape as `api-machinery-tasks.md` EC-3 — a finalizer whose owner
  is gone. Uninstalling a CSI driver before deleting its volumes wedges things.

---

### EC-7 — `subPath` with a changing Secret breaks TLS rotation

- **Trap:** cert-manager rotates a TLS secret; your pod serves the expired cert
  until restarted.
- **Why:** `subPath` froze it (Task 1.6).
- **Rule:** never `subPath` anything expected to rotate. Mount the directory.

---

## Cheat sheet

```bash
kubectl create cm NAME --from-literal=K=V --from-file=f --from-env-file=f
kubectl create secret generic NAME --from-literal=K=V
kubectl create secret tls NAME --cert=c.pem --key=k.pem
kubectl get secret S -o jsonpath='{.data.password}' | base64 -d
kubectl get sc                                        # volumeBindingMode, reclaim
kubectl get pvc,pv -n NS
kubectl describe pvc NAME                             # binding + resize events
kubectl exec POD -- ls -l /etc/app                    # see the ..data symlinks
kubectl exec POD -- df -h /etc/secret                 # tmpfs?
kubectl get csidrivers
```

## Mental model to lock in

- **Env = snapshot at start, never updates. Mounted volume = eventually updates.
  subPath = frozen forever.** Three behaviours from one ConfigMap.
- **Volumes belong to the Pod; mounts belong to the container.** One volume, many
  mounts, different paths.
- **PVC = request, PV = resource, StorageClass = factory.** Apps write PVCs only.
- **`WaitForFirstConsumer` exists because storage has topology.** Immediate binding
  plus zones equals unschedulable pods.
- **Access modes are per-node, not per-pod** — except `ReadWriteOncePod`.
- **`Delete` reclaim destroys real data.** Production gets `Retain`.
- **Secrets are base64, not encrypted**, and at-rest encryption is opt-in.
- **Missing ConfigMap/Secret = stuck in ContainerCreating, silently.**

```text
   Pod
    ├── volumes[]                       ← Pod-scoped, defined once
    │    ├── configMap  ──▶ /etc/app     symlink swap, ~60s update
    │    ├── secret     ──▶ tmpfs        never on node disk
    │    ├── emptyDir   ──▶ node disk    dies with the Pod
    │    └── pvc        ──▶ PV           outlives the Pod
    └── containers[].volumeMounts[]     ← per container, mountPath + optional subPath
                                              └── subPath = copy, no updates
```
