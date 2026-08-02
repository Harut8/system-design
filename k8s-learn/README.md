# k8s-learn — Roadmap & Reading Map

Practice lives here (`k8s-learn/`). Theory lives in `../kubernetes/` — 44 documents,
~110k lines. This file is the bridge: what to do, in what order, and what to read
alongside each thing.

> **How to use the two folders.** Read the theory doc *first* for the mental model,
> then do the tasks, then re-skim the doc's later sections — they'll mean something
> different afterwards. Reading `../kubernetes/` end-to-end without touching a
> cluster produces confident-sounding knowledge that collapses under one follow-up
> question. That's the rung-3 trap.

```
TRACK A — consumer            TRACK B — tool builder        TRACK C — deep systems
"run my workload here"        "extend the cluster"          "how does it actually work"

 pod                           api-machinery                 kube-scheduler internals
 replica                       controller (client-go)        kubelet internals
 deployment                    operator (CRD + runtime)      etcd / apiserver internals
 config-storage        ─────▶  admission (webhooks)   ─────▶ CNI / eBPF
 service-networking            scheduling (framework)        performance & scaling
 workload-controllers          device-plugin                 building k8s from scratch
 resources
 scheduling-constraints
 rbac · autoscaling
        │                              │                            │
        └──────────────────────────────┴────────────────────────────┘
                                       ▼
                         the GPUBudget operator you actually ship
```

---

## Track A — consumer level

Do these in order. Each row lists the practice file and what to read from
`../kubernetes/` around it.

| # | Practice file | Read alongside | Status |
|---|---|---|---|
| 1 | `pod-tasks.md` | `11-pod-internals.md`, `00-linux-primitives-for-containers.md` | ✅ exists |
| 2 | `replica-tasks.md` | `12-workload-controllers.md` §ReplicaSet | ✅ exists |
| 3 | `deployment-tasks.md` | `12-workload-controllers.md` §Deployment | ✅ exists |
| 3b | `env-config-secrets-tasks.md` | `19-storage-csi-pv-pvc.md` §ConfigMap/Secret projection | ✅ **new** |
| 4 | `config-storage-tasks.md` | `19-storage-csi-pv-pvc.md` | ✅ **new** |
| 5 | `service-networking-tasks.md` | `14-services-and-kube-proxy.md`, `18-dns-and-coredns.md` | ✅ **new** |
| 6 | `workload-controllers-tasks.md` | `12-workload-controllers.md`, `13-statefulset-deep-dive.md` | ✅ **new** |
| 7 | `resources-tasks.md` | `21-resource-management-and-qos.md` | ✅ exists |
| 8 | `scheduling-constraints-tasks.md` | `09-kube-scheduler-internals.md` §1–4 | ✅ **new** |
| 9 | `rbac-tasks.md` | `07-authentication-authorization.md` | ⬜ todo |
| 10 | `autoscaling-tasks.md` | `22-autoscaling.md` | ⬜ todo |
| 11 | `netpol-tasks.md` | `20-network-policy-and-segmentation.md` | ⬜ todo |

**If you're short on time, 4→5→8 is the critical path.** Storage and networking are
where developer-level knowledge is most often thin, and scheduling constraints is
the direct on-ramp to Track B and everything GPU.

---

## Track B — tool builder

The order matters here more than anywhere else. See "the one idea" at the bottom.

| # | Practice file | Read alongside | Status |
|---|---|---|---|
| 1 | `api-machinery-tasks.md` | `05-kube-apiserver-internals.md`, `36-garbage-collection-and-object-lifecycle.md` | ✅ exists |
| 2 | `controller-tasks.md` | `08-controller-pattern-and-client-go.md` | ✅ exists |
| 3 | `operator-tasks.md` | `23-crds-operators-and-controller-runtime.md` | ✅ exists |
| 4 | `admission-tasks.md` | `06-admission-control-deep-dive.md` | ⬜ todo |
| 5 | `scheduling-framework-tasks.md` | `34-custom-schedulers-and-scheduler-framework.md` | ⬜ todo |
| 6 | `device-plugin-tasks.md` | `10-kubelet-internals.md` §device manager, `21-resource-management-and-qos.md` §extended resources | ⬜ todo — **your endgame** |

---

## Track C — deep systems

Not tasks — reading, done once you have hands on the layers above. These are the
documents that make you dangerous in an interview, and they're wasted if read
first.

| Read | Why it matters to you |
|---|---|
| `09-kube-scheduler-internals.md` | The scheduling cycle, plugins, preemption. Prerequisite for anything GPU-placement. |
| `10-kubelet-internals.md` | Device manager, cgroups, eviction. Where GPU allocation physically happens. |
| `05-kube-apiserver-internals.md` | Watch cache, priority & fairness. Explains every controller performance problem. |
| `04-etcd-internals.md` | Revisions, compaction, watch. Explains *why* the 410 in `api-machinery-tasks.md` Level 1 exists. |
| `35-performance-scaling-and-tuning.md` | Where clusters break. Closest doc to your capacity day job. |
| `30-observability-internals.md` | Your strongest existing area — read it to connect what you know to how k8s exposes it. |
| `38-building-a-kubernetes-from-scratch.md` | The capstone. Only meaningful after Track B. |

### Read when the need arises, not on a schedule

`15-cni-and-pod-networking.md` · `16-cilium-and-ebpf-deep-dive.md` ·
`17-ingress-gateway-and-service-mesh.md` · `24-api-aggregation-and-extension-apiservers.md` ·
`25-multi-tenancy.md` · `26-multi-cluster-and-fleet.md` · `27-supply-chain-security.md` ·
`28-runtime-security-and-policy.md` · `29-pod-sandboxing.md` · `31-gitops-helm-kustomize.md` ·
`32-cluster-lifecycle-and-day2.md` · `33-edge-and-special-distributions.md` ·
`37-cloud-provider-integration.md`

### Container-level foundation

`00-linux-primitives-for-containers.md` · `01-container-runtimes-cri-oci.md` ·
`02-container-images-and-registries.md` · `39`–`43` (Docker, Compose, Python containers)

Read `00` early — namespaces, cgroups and capabilities are the substrate under
every resource limit and security context in Track A. The Docker files (39–43) are
independent and useful whenever you touch a Dockerfile.

---

## Suggested sequence

**Phase 1 — close Track A (3–5 weeks).** Modules 4, 5, 6, 8 above. Read
`19`, `14`, `18`, `12`, `13`, `09` §1–4 alongside. You'll stop guessing about
networking and storage, which is most of what "confident with Kubernetes" means
day to day.

**Phase 2 — Track B core (6–8 weeks).** `api-machinery` → `controller` →
`operator`, reading `05`, `08`, `23`. Ends with a working GPUBudget operator.

**Phase 3 — specialise toward GPU (6–8 weeks).** `admission` →
`scheduling-framework` → `device-plugin`, reading `06`, `34`, `10`. Feed DCGM in.
Compare requested vs. actually used. Publish it.

**Throughout — Track C**, one document a week, in the order listed.

---

## The one idea that separates the tracks

> **Kubernetes is level-triggered, not edge-triggered.**

A controller does not react to events. It receives a *hint* that something may have
changed, re-reads the world, and makes it match the spec. The event carries nothing
you may trust — it can be duplicated, delayed, reordered, coalesced, or lost and
replaced by a resync.

```
edge-triggered (wrong)              level-triggered (correct)
"pod deleted → decrement counter"   "reconcile(key) → count pods, set counter"

  miss one event → wrong forever      miss any number → self-heals
```

Every bug in a first controller is a violation of that sentence. And it isn't a
design preference — `api-machinery-tasks.md` Level 1 shows *why*: etcd compacts
history, so any client can lose its position permanently and must be able to
rebuild from a full LIST. Level-triggering is forced by the storage layer.

---

## Prerequisites

```bash
kind create cluster            # throwaway; you will break things
go version                     # 1.22+, needed from Track B onward
kubectl krew install ctx ns    # optional, saves a lot of typing
```

Track B also needs `kubebuilder` (installed in `operator-tasks.md`).

Some Track A tasks need `metrics-server`:
```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl patch deploy metrics-server -n kube-system --type=json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```

## Conventions in every task file

- **Level 0** orientation → **Levels 1–6** ladder → **Level 7** edge cases
- Tasks are `Do:` / `Verify:` / `Learn:` — do all three, the `Learn` is the point
- Level 7 is `Trap → Why → Diagnose → Fix/rule`, written as things that will
  actually happen to you
- Every file ends with a cheat sheet and a mental model worth memorising
- Cross-references like `resources-tasks.md EC-7` are deliberate — the same idea
  recurs at increasing depth, and seeing it again is the point
