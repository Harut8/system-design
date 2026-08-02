# Env / ConfigMap / Secret Tasks — Beginner → Advanced

A hands-on ladder for learning how configuration reaches a container, using
`config.yaml` in this folder. Do these AFTER `deployment-tasks.md` — you need
something that rolls before "how do I roll out a config change" means anything.

Track A module 3b. Do before `config-storage-tasks.md`, which picks up the same
ConfigMaps and Secrets from the **storage/volume** side (symlink swaps, subPath,
projected volumes, PV/PVC). This file is the **developer** side: what my process
sees in `env`, and what happens when I change it.

> The one idea: **env vars are resolved once, at container start, and never
> change. Mounted files do change.** Every configuration incident you will ever
> have is downstream of that one sentence.

Setup: `kubectl apply -f config.yaml` (creates namespace `env-lab`).
All commands assume `-n env-lab`.

---

## Level 0 — Orientation

1. `kubectl explain pod.spec.containers.env` — a **list** of `{name, value}`, not
   a map. Order matters (Task 1.3).
2. `kubectl explain pod.spec.containers.env.valueFrom` — the four sources:
   `configMapKeyRef`, `secretKeyRef`, `fieldRef`, `resourceFieldRef`.
3. `kubectl explain pod.spec.containers.envFrom` — bulk import of a whole
   ConfigMap or Secret.
4. The split to hold in your head: **ConfigMap and Secret are just API objects.**
   `env`/`envFrom` and volume mounts are two independent ways of *consuming*
   them, with completely different runtime behaviour.

```text
   ConfigMap ─┬─ envFrom / configMapKeyRef ──▶ process environment  (frozen)
              └─ volumes[].configMap       ──▶ files in the container (live)
   Secret    ─┴─ same two paths, plus tmpfs and different RBAC conventions
```

---

## Level 1 — Beginner: plain env vars

- [ ] **Task 1.1 — Apply and look**
  - Do: `kubectl apply -f config.yaml` then
    `kubectl exec -n env-lab env-basics -- env | sort`
  - Learn: you see your own variables, plus `HOSTNAME`, plus variables the image
    itself declared (`PATH`), plus `KUBERNETES_SERVICE_HOST` and friends that
    nobody asked for (Task 1.6).

- [ ] **Task 1.2 — Numbers must be quoted**
  - Do: edit `config.yaml`, change `value: "8080"` to `value: 8080`, apply.
  - Verify: rejected — `cannot unmarshal number into Go struct field EnvVar.value
    of type string`.
  - Learn: `env.value` is a string, always. Same for every value in a ConfigMap's
    `data`. YAML's helpfulness (unquoted `true`, `25`, `8.0`) is a constant source
    of this error.

- [ ] **Task 1.3 — `$(VAR)` expansion, and where it breaks**
  - Do: `kubectl exec -n env-lab env-basics -- env | grep -E 'ADDRESS|LITERAL|BROKEN_REF'`
  - Verify: `ADDRESS=checkout:8080`, `LITERAL=$(PORT)`, and
    **`BROKEN_REF=$(NOT_YET)`** — unexpanded.
  - Learn: Kubernetes expands `$(VAR)` using only variables defined **earlier in
    the same `env` list**. A forward reference is silently left as literal text.
    `$$` escapes the expansion. Variables from `envFrom` are **not** available to
    expansion at all.

- [ ] **Task 1.4 — Duplicates: last one wins**
  - Do: `kubectl exec -n env-lab env-basics -- printenv DUP`
  - Verify: `second`.
  - Learn: the API server does not reject duplicate names. In a Helm chart where
    a base template and an override both set `LOG_LEVEL`, position in the list
    decides — not which one you meant.

- [ ] **Task 1.5 — Expansion works in `command` and `args` too**
  - Do: add `command: ["sh","-c","echo $(SERVICE_NAME)"]` to a scratch pod.
  - Learn: Kubernetes substitutes `$(SERVICE_NAME)` **before** the shell runs, so
    the shell never sees it. If the variable is undefined, the literal
    `$(SERVICE_NAME)` reaches `sh`, which interprets it as *command substitution*
    and tries to run `SERVICE_NAME` as a program. Confusing failure, memorable
    once.

- [ ] **Task 1.6 — Service link variables**
  - Do: `kubectl exec -n env-lab env-basics -- env | grep KUBERNETES_`
  - Then: `kubectl exec -n env-lab deploy/configured-app -- env | grep -c KUBERNETES_`
  - Verify: the Deployment (which sets `enableServiceLinks: false`) has none.
  - Learn: by default the kubelet injects `<SVCNAME>_SERVICE_HOST` /
    `<SVCNAME>_SERVICE_PORT` for every Service that existed in the namespace
    **before** the pod started. It is a Docker-links relic. It bloats large
    namespaces and can collide with your own names (EC-6). Turn it off.

- [ ] **Task 1.7 — Env is per-container**
  - Do: `kubectl get pod env-basics -n env-lab -o jsonpath='{.spec.containers[*].env[*].name}'`
  - Learn: `env` lives on the **container**, not the pod. Sidecars and
    initContainers get nothing automatically — every container repeats its own
    block. (Contrast with `volumes`, which are pod-scoped.)

---

## Level 2 — ConfigMaps as configuration

- [ ] **Task 2.1 — Create one three ways**
  ```bash
  kubectl create cm demo -n env-lab --from-literal=A=1 --from-literal=B=2
  printf 'X=1\nY=2\n' > demo.env
  kubectl create cm demo-file -n env-lab --from-file=demo.env       --dry-run=client -o yaml
  kubectl create cm demo-env  -n env-lab --from-env-file=demo.env   --dry-run=client -o yaml
  ```
  - Verify: `--from-file` makes **one key whose value is the whole file**;
    `--from-env-file` makes **one key per line**.
  - Learn: same input, two completely different objects. `--dry-run=client -o yaml`
    is how you check before you commit — use it constantly.

- [ ] **Task 2.2 — `envFrom` vs `configMapKeyRef`**
  - Do: `kubectl exec -n env-lab env-from-config -- env | sort`
  - Verify: `LOG_LEVEL` (bulk, via `envFrom`) **and** `APP_LOG_LEVEL` (explicit,
    renamed) are both present.
  - Learn: `envFrom` is convenient and opaque — you cannot tell what variables a
    container has by reading its YAML. `configMapKeyRef` is verbose and greppable.
    Use `envFrom` for a map you own end to end, explicit refs for anything a
    reviewer needs to see.

- [ ] **Task 2.3 — Precedence**
  - Do: `kubectl exec -n env-lab env-from-config -- printenv REGION`
  - Verify: `overridden-by-env`, not `eu-central-1`.
  - Learn: **`envFrom` is applied first, then `env` overrides it.** Within
    `envFrom`, later entries in the list override earlier ones.

- [ ] **Task 2.4 — `prefix`**
  - Do: `kubectl exec -n env-lab env-from-config -- env | grep FF_`
  - Verify: `FF_FEATURE_A=true`.
  - Learn: `prefix` namespaces an imported map. The only sane way to `envFrom`
    two maps that might share a key.

- [ ] **Task 2.5 — Invalid names disappear**
  - Do: `kubectl exec -n env-lab env-from-config -- env | grep -i beta` → nothing.
  - Do: `kubectl describe pod env-from-config -n env-lab | grep -A3 Events`
  - Verify: an event with reason `InvalidEnvironmentVariableNames`.
  - Learn: `feature.beta` is a legal ConfigMap key and an illegal env var name.
    The kubelet **skips it and starts the container anyway**. Your flag is just
    missing. Keys destined for env must match `[A-Za-z_][A-Za-z0-9_]*`.

- [ ] **Task 2.6 — THE BIG ONE: env is a snapshot**
  - Do:
    ```bash
    kubectl exec -n env-lab env-from-config -- printenv LOG_LEVEL      # debug
    kubectl patch cm app-config -n env-lab --type merge -p '{"data":{"LOG_LEVEL":"error"}}'
    sleep 90
    kubectl exec -n env-lab env-from-config -- printenv LOG_LEVEL      # still debug
    ```
  - Verify: **unchanged, forever.**
  - Learn: env is resolved once, at container start. There is no mechanism to
    update it and nothing will restart the pod for you.

- [ ] **Task 2.7 — …but the mounted file DOES change**
  - Do: in the same pod,
    ```bash
    kubectl exec -n env-lab env-from-config -- cat /etc/app/app.properties
    kubectl patch cm app-config -n env-lab --type merge \
      -p '{"data":{"app.properties":"server.port=9090\nserver.threads=32\n"}}'
    # poll for ~90s
    kubectl exec -n env-lab env-from-config -- cat /etc/app/app.properties
    ```
  - Verify: it changes, after up to ~60–90s (kubelet sync period + cache TTL).
  - Learn: **mounted = eventually updated, env = never updated.** One ConfigMap,
    two behaviours, decided entirely by how you consumed it. Your app still has
    to re-read the file — Kubernetes changes the bytes, not your process's
    understanding of them.

- [ ] **Task 2.8 — `kubectl set env`**
  ```bash
  kubectl set env deploy/configured-app -n env-lab --list
  kubectl set env deploy/configured-app -n env-lab EXTRA=1
  kubectl set env deploy/configured-app -n env-lab --from=cm/feature-flags --prefix=FF2_
  kubectl set env deploy/configured-app -n env-lab EXTRA-        # trailing dash removes
  ```
  - Learn: each of these edits the pod template → new revision → rolling update.
    Great for debugging, and exactly the imperative-vs-declarative drift trap from
    `replica-tasks.md` EC-6. The file is still the source of truth.

---

## Level 3 — Secrets

- [ ] **Task 3.1 — base64 is not encryption**
  ```bash
  kubectl get secret app-secret -n env-lab -o jsonpath='{.data.DB_PASSWORD}' | base64 -d; echo
  ```
  - Learn: anyone with `get secret` in this namespace has the plaintext. Secrets
    differ from ConfigMaps in RBAC convention, tmpfs mounting, and at-rest
    encryption *if the cluster enables it* — not in the object itself.

- [ ] **Task 3.2 — `stringData` is write-only**
  - Do: `kubectl get secret app-secret -n env-lab -o yaml`
  - Verify: no `stringData` field; only `data`, base64-encoded.
  - Learn: `stringData` is a convenience the API server consumes at write time.
    Handy: `kubectl create secret generic x --from-literal=k=v --dry-run=client -o yaml`.

- [ ] **Task 3.3 — Both consumption paths at once**
  - Do: `kubectl logs -n env-lab env-secret`
  - Verify: the value appears via `$DB_PASSWORD`, via `/etc/secret/DB_PASSWORD`,
    and `df` reports **tmpfs**.
  - Learn: the file form is never written to node disk, and it counts against the
    container's memory (`resources-tasks.md` EC-8).

- [ ] **Task 3.4 — Watch a secret leak**
  ```bash
  kubectl describe pod env-secret -n env-lab | grep -i -A2 password   # names, not values
  kubectl exec -n env-lab env-secret -- sh -c 'cat /proc/1/environ | tr "\0" "\n" | grep DB_'
  ```
  - Learn: every child process inherits the whole environment; crash handlers and
    logging frameworks routinely dump it; `/proc/<pid>/environ` is readable inside
    the container. A mounted file has none of those properties.

- [ ] **Task 3.5 — Rotation**
  - Do: patch `app-secret`, then re-check the env var and the file, as in 2.6/2.7.
  - Verify: env frozen, file updated (~60–90s).
  - Learn: **a rotated secret consumed as env is still the old secret.** This is
    the outage: cert-manager or Vault rotates a credential, every pod keeps the
    expired one until something unrelated restarts them, and then it fails at 3am.

- [ ] **Task 3.6 — Immutable**
  - Do: add `immutable: true` to a copy of the secret, then try to edit it.
  - Learn: rejected. It also drops the kubelet's watch on that object, a real
    scale win. Roll by creating a new name — which is what
    `kustomize`'s `secretGenerator` hash suffix does for you.

---

## Level 4 — The downward API

- [ ] **Task 4.1 — Identity**
  - Do: `kubectl logs -n env-lab env-downward`
  - Verify: `POD_NAME`, `POD_NAMESPACE`, `POD_IP`, `NODE_NAME`, `SA_NAME`.
  - Learn: this is how a process learns its own coordinates for log tagging,
    metrics labels, leader election and peer discovery. No API call, no
    ServiceAccount permissions needed.

- [ ] **Task 4.2 — Labels are NOT available as env vars**
  - Do: try adding
    ```yaml
    - name: TEAM
      valueFrom: { fieldRef: { fieldPath: metadata.labels['team'] } }
    ```
  - Verify: rejected by the API server.
  - Learn: env `fieldRef` accepts only a short whitelist (`metadata.name`,
    `metadata.namespace`, `metadata.uid`, `spec.nodeName`,
    `spec.serviceAccountName`, `status.hostIP`, `status.podIP`, `status.podIPs`).
    Labels and annotations reach a container **only via a downwardAPI volume** —
    already mounted at `/etc/podinfo` in this pod.

- [ ] **Task 4.3 — …and the volume form updates**
  - Do:
    ```bash
    kubectl exec -n env-lab env-downward -- cat /etc/podinfo/labels
    kubectl label pod env-downward -n env-lab team=payments --overwrite
    # wait ~60s
    kubectl exec -n env-lab env-downward -- cat /etc/podinfo/labels
    ```
  - Verify: the file reflects the new label.
  - Learn: same asymmetry as Task 2.6/2.7, now for pod metadata. Volume = live,
    env = frozen. Third time you have seen this; that is deliberate.

- [ ] **Task 4.4 — `resourceFieldRef`: the container's own limits**
  - Do: `kubectl exec -n env-lab env-downward -- env | grep -E 'CPU_|MEM_'`
  - Verify: `CPU_LIMIT_MILLI=200`, `MEM_LIMIT_MI=64`, `CPU_REQUEST_MILLI=50`.
  - Learn: from inside a container, `/proc/cpuinfo` and `nproc` show the **node's**
    CPUs, not your cgroup quota. Runtimes that size thread pools from that number
    (older JVMs, Go's default `GOMAXPROCS`, anything calling `nproc`) will spawn
    64 workers inside a 200m limit and throttle themselves into the ground
    (`resources-tasks.md`). `resourceFieldRef` is the portable fix.

- [ ] **Task 4.5 — The dangerous default**
  - Do: create a pod using `resourceFieldRef: limits.cpu` with **no `limits` set**.
  - Verify: it does not fail — the value becomes the **node's allocatable CPU**.
  - Learn: a silent 64× oversizing on a big node. Always pair `resourceFieldRef`
    with an actual limit.

---

## Level 5 — Advanced: rolling out a config change

- [ ] **Task 5.1 — Prove nothing happens**
  - Do: `kubectl patch cm app-config -n env-lab --type merge -p '{"data":{"REGION":"us-east-1"}}'`
    then `kubectl get pods -n env-lab -w` for a minute.
  - Verify: **no restarts, no new ReplicaSet, no rollout.**
  - Learn: a Deployment watches its **pod template**, and a ConfigMap change does
    not touch the template. Kubernetes has no built-in "restart on config change."

- [ ] **Task 5.2 — `kubectl rollout restart`**
  - Do: `kubectl rollout restart deploy/configured-app -n env-lab`
  - Do: `kubectl get deploy configured-app -n env-lab -o jsonpath='{.spec.template.metadata.annotations}'`
  - Verify: a `kubectl.kubernetes.io/restartedAt` annotation appeared.
  - Learn: it works by **changing the template**, which changes the
    `pod-template-hash`, which creates a new ReplicaSet — a normal rolling update
    (`replica-tasks.md` EC-2 explains that hash). Nothing magic, and nothing
    automatic.

- [ ] **Task 5.3 — The checksum annotation**
  - Do:
    ```bash
    SUM=$(kubectl get cm app-config -n env-lab -o yaml | shasum -a 256 | cut -d' ' -f1)
    kubectl patch deploy configured-app -n env-lab --type merge \
      -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"checksum/config\":\"$SUM\"}}}}}"
    ```
  - Verify: a rollout starts.
  - Learn: this is the idiomatic pattern, and it is what Helm's
    `sha256sum` annotation does. Because the checksum is derived from the
    ConfigMap, a config-only commit produces a template change automatically.

- [ ] **Task 5.4 — The other idiom: hashed names**
  - Learn: `kustomize`'s `configMapGenerator` appends a content hash to the name
    (`app-config-7f9c2b`) and rewrites references. The template changes because
    the *name* changed. Bonus: rollback actually works, because the old ConfigMap
    still exists. With the checksum approach, `rollout undo` restores the old pod
    template pointing at a ConfigMap whose **content is already the new one**.

- [ ] **Task 5.5 — Decide per value**
  - Learn, and write it down for your own services:
    | Value | Consume as | Why |
    |---|---|---|
    | service name, region, feature flags at deploy time | `env` | never changes within a pod's life |
    | log level you want to flip live | mounted file + app watches it | env cannot change |
    | DB password, API key | mounted file (or env if you must) | rotation + leak surface |
    | TLS cert | mounted **directory**, never `subPath` | rotates; `subPath` freezes it |
    | own cpu/memory limit | `resourceFieldRef` | `nproc` lies inside a container |
    | pod name / IP | `fieldRef` | free, no API call |
    | labels/annotations | downwardAPI volume | not available as env at all |

---

## Level 6 — Edge Cases & Production Nuances

Same format as `replica-tasks.md` Level 6: **trap → diagnose → fix/rule**.

---

### EC-1 — `CreateContainerConfigError` (and why there are no logs)

- **Trap:** the pod is scheduled, never starts, `kubectl logs` says
  `container "app" in pod ... is waiting to start`.
- **Reproduce:** it is already running — `kubectl describe pod env-missing -n env-lab`.
- **Diagnose:** the events say
  `Error: couldn't find key DOES_NOT_EXIST in ConfigMap env-lab/app-config`.
- **Why:** a missing ConfigMap/Secret **or a missing key** referenced without
  `optional: true` blocks container creation. The kubelet retries forever.
- **Fix/rule:** `CreateContainerConfigError` = env/config reference problem,
  always. Compare the two shapes:
  - env reference broken → `CreateContainerConfigError`
  - volume reference broken → `ContainerCreating` + `FailedMount`
  Both are invisible in logs, because no container ever ran. Read events first.

---

### EC-2 — The config changed and nothing happened

- **Trap:** you patched the ConfigMap, confirmed the new value with
  `kubectl get cm`, and the app still behaves the old way.
- **Why:** one of three, in order of likelihood — consumed as **env** (frozen),
  mounted with **`subPath`** (frozen, `config-storage-tasks.md` EC-7), or the app
  read the file once at boot and never again.
- **Fix:** `kubectl rollout restart`, or the checksum annotation (Task 5.3) so it
  happens without you.
- **Rule:** "I changed the ConfigMap" is never sufficient. Ask *how it is
  consumed*, then *whether the process re-reads it*.

---

### EC-3 — A rotated secret keeps working, then suddenly does not

- **Trap:** credentials rotate on schedule; everything is fine for days; a node
  drain replaces some pods and half your fleet starts failing auth — or the
  reverse, the old credential expires and everything fails at once.
- **Why:** secrets consumed as env are pinned to whenever each pod last started.
  Your fleet is running a mix of credential vintages and you cannot tell which.
- **Diagnose:** `kubectl get pods -n NS --sort-by=.status.startTime` next to the
  secret's `metadata.resourceVersion` / last-modified time.
- **Rule:** anything with an expiry gets **mounted**, and the app reloads it.
  Env vars are for values that outlive the pod.

---

### EC-4 — `envFrom` silently dropped half my keys

- **Trap:** a ConfigMap with `feature.beta`, `feature-gamma`, `1st_flag` — none of
  them appear in the container.
- **Diagnose:** `kubectl describe pod` → reason `InvalidEnvironmentVariableNames`.
- **Rule:** ConfigMap keys allow `.` and `-`; env var names do not. Keys used with
  `envFrom` must match `[A-Za-z_][A-Za-z0-9_]*`. If you need dotted keys (Spring,
  Java properties), consume the map as a **file**, not as env.

---

### EC-5 — Precedence surprises in a Helm chart

- **Trap:** the chart's `envFrom` pulls a shared platform ConfigMap; your values
  file sets the same variable in `env`; a teammate swears the ConfigMap is wrong.
- **Rule:** `envFrom` first, then `env` wins; within a list, later wins;
  duplicates are legal. When in doubt do not reason about it —
  `kubectl exec POD -- printenv NAME` is the ground truth, and
  `kubectl set env deploy/x --list` shows the resolved template.

---

### EC-6 — A Service name collided with my config

- **Trap:** your app reads `DB_PORT` expecting `5432` and gets
  `tcp://10.96.0.42:5432`.
- **Why:** a Service named `db` exists in the namespace, and the kubelet injects
  Docker-link-style `DB_PORT`, `DB_PORT_5432_TCP_ADDR`, `DB_SERVICE_HOST`
  variables for every Service created **before** your pod. Restart order decides
  whether you get them — so it works locally and breaks in staging.
- **Fix:** `enableServiceLinks: false` in the pod spec.
- **Rule:** set it on every workload. In a namespace with hundreds of Services it
  is also a measurable memory and startup cost per container.

---

### EC-7 — The 1 MiB wall

- **Trap:** `kubectl apply` fails with `ConfigMap ... is invalid: too long: must
  have at most 1048576 bytes`, or a pod fails to exec with `argument list too long`.
- **Why:** ConfigMaps and Secrets are etcd objects and are capped at ~1 MiB. The
  process environment has its own OS-level ceiling as well.
- **Rule:** config objects hold config, not data. TLS bundles, ML model files and
  seed datasets belong in a volume or an image layer. If a ConfigMap is near the
  limit, the design is already wrong.

---

### EC-8 — Cross-namespace references do not exist

- **Trap:** a pod in `staging` referencing a Secret in `shared` sits in
  `CreateContainerConfigError` with "not found", and the Secret is right there.
- **Rule:** `configMapKeyRef` / `secretKeyRef` / `envFrom` are **namespace-local,
  always**. There is no cross-namespace form and there never will be — it would
  be an RBAC hole. Replicate the object (External Secrets Operator, a
  reflector controller, or your CD pipeline).

---

### EC-9 — `optional: true` hides the problem instead of solving it

- **Trap:** someone "fixed" a `CreateContainerConfigError` by adding
  `optional: true`. The pod now starts happily — with no `DB_PASSWORD` — and
  fails much later, deeper, with a worse error.
- **Rule:** `optional` is correct for genuinely optional tunables and for maps
  that legitimately may not exist yet. For anything the app requires, a pod that
  refuses to start is the **better** failure: it is loud, immediate, and stops
  the rollout instead of taking traffic.

---

## Cheat sheet

```bash
kubectl create cm NAME --from-literal=K=V --from-file=f --from-env-file=f --dry-run=client -o yaml
kubectl create secret generic NAME --from-literal=K=V --dry-run=client -o yaml
kubectl get secret S -o jsonpath='{.data.KEY}' | base64 -d

kubectl exec POD -- env | sort                 # ground truth for a running pod
kubectl exec POD -- printenv NAME
kubectl set env deploy/D --list                # resolved template, incl. envFrom
kubectl set env deploy/D FOO=bar               # -> new revision -> rollout
kubectl set env deploy/D --from=cm/app-config --prefix=APP_
kubectl set env deploy/D FOO-                  # trailing dash = remove

kubectl describe pod POD | grep -A15 Events    # CreateContainerConfigError lives here
kubectl rollout restart deploy/D               # the only supported "reload config"
kubectl patch cm app-config --type merge -p '{"data":{"LOG_LEVEL":"error"}}'
```

## Mental model to lock in

- **`env` = a photograph taken at exec(). Mounted file = a live feed.** Never
  updates vs. updates in ~60–90s. This one asymmetry explains this entire file.
- **Kubernetes never restarts a pod because config changed.** You trigger it, by
  changing the pod *template* — `rollout restart`, a checksum annotation, or a
  hashed ConfigMap name.
- **`envFrom` then `env`; later wins; duplicates are legal.** Verify with
  `printenv`, do not reason about it.
- **Broken env ref = `CreateContainerConfigError`. Broken volume ref =
  `ContainerCreating` + `FailedMount`.** Neither produces logs.
- **ConfigMap keys ⊅ env var names** — dots and dashes are dropped silently.
- **Secrets are base64, not encrypted**, and as env they leak into `describe`,
  `/proc/<pid>/environ`, child processes and crash dumps.
- **Everything is namespace-local**, and capped at ~1 MiB.
- **From inside a container, `nproc` lies.** `resourceFieldRef` tells the truth —
  but only if you actually set a limit.

```text
                      ┌──────────────── ConfigMap / Secret (API object) ───────────┐
                      │                                                            │
        envFrom ──────┤                                                            ├────── volumes[]
   configMapKeyRef    │                                                            │      configMap/secret
    secretKeyRef      ▼                                                            ▼
          ┌──────────────────────────┐                          ┌──────────────────────────┐
          │  process environment     │                          │  files in the container  │
          │  resolved once at exec() │                          │  kubelet re-syncs ~60-90s│
          │  NEVER changes           │                          │  (subPath freezes it)    │
          └──────────────────────────┘                          └──────────────────────────┘
                      │                                                            │
                      └──── to change it: change the POD TEMPLATE ──▶ rollout ──────┘
                                                                    (app must still re-read)
```
