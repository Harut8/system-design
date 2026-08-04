# Kubernetes Mental Model & Roadmap: From Linux Primitives to Multi-Cluster Federation

This is the **map** of the kubernetes/ folder. The per-topic chapters (not yet written) go deep on each layer; this file shows how every layer connects, the order to build them in, and exactly which chapter will own each piece. Read this first; use it as the index when the per-topic files start landing.

Scope: this is a **staff-level deep dive** roadmap. We don't stop at "what is a Deployment". We go through Linux namespaces, the OCI runtime spec, the kubelet's PLEG state machine, etcd's MVCC watch implementation, the scheduler framework's plugin extension points, CNI dataplanes (iptables vs IPVS vs eBPF), CSI's three-phase volume lifecycle, admission webhooks vs CEL ValidatingAdmissionPolicy, controller-runtime's informer/workqueue/reconcile loop, custom schedulers, custom API servers via aggregation, CRD conversion webhooks, multi-tenancy patterns, multi-cluster control planes, GitOps engines, supply-chain security, microVM sandboxes, and the performance tuning needed to run 5000-node clusters.

If you only ever read one page in this folder, read this one.

---

## Table of Contents

1. [The One-Page Picture](#1-the-one-page-picture)
2. [The Five Universal Pipelines](#2-the-five-universal-pipelines)
3. [The Build Order: Phase 0 → Phase 24](#3-the-build-order-phase-0--phase-24)
4. [Chapter Plan (the roadmap)](#4-chapter-plan-the-roadmap)
5. [Component Responsibility Map](#5-component-responsibility-map)
6. [Cross-Cutting Concerns (the 6 Hard Problems)](#6-cross-cutting-concerns-the-6-hard-problems)
7. [Variant Decision Tree](#7-variant-decision-tree)
8. [End-to-End Trace of `kubectl apply`](#8-end-to-end-trace-of-kubectl-apply)
9. [Linear Reading Order](#9-linear-reading-order)
10. [Common Pitfalls When Building / Running Your Own](#10-common-pitfalls-when-building--running-your-own)

---

## 1. The One-Page Picture

Kubernetes is a **distributed state machine** wrapped around an etcd log, with a single rule: *every component is a controller that watches some objects and reconciles real-world state toward declared state*. If you can hold this diagram in your head, every chapter slots into one of these boxes.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  CLIENTS  (kubectl, client-go, controllers, dashboards, CI/CD, operators)    │
└──────────────────────────────┬───────────────────────────────────────────────┘
                               │  HTTPS · REST · WATCH · protobuf
                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  CONTROL PLANE                                                               │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  kube-apiserver                                            ─── ch 05   │  │
│  │   AuthN → AuthZ → Mutating Admission → Schema/CEL validate →           │  │
│  │   Validating Admission → Conversion → Storage → Watch fan-out          │  │
│  │   (REST + discovery + OpenAPI + aggregation layer)                     │  │
│  └────┬──────────────────────────┬─────────────────────────┬──────────────┘  │
│       │ watch/list               │ watch                   │ watch           │
│       ▼                          ▼                         ▼                 │
│  ┌──────────┐            ┌─────────────────┐        ┌──────────────────┐    │
│  │ kube-    │            │ kube-controller │        │ cloud-controller │    │
│  │ scheduler│            │ -manager        │        │ -manager (CCM)   │    │
│  │  ch 09   │            │  (deployment,   │        │  (LB, route,     │    │
│  │          │            │   replica, gc,  │        │   node, volume)  │    │
│  │          │            │   node, …) ch08 │        │   ch 37          │    │
│  └──────────┘            └─────────────────┘        └──────────────────┘    │
│                                  │                                            │
│                                  ▼                                            │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  etcd  (Raft replicated, MVCC KV, watch, lease, compaction) ─── ch 04 │  │
│  │   The only stateful component. Every other process is a cache+actor.  │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
                               │ pod assignment + watch (per-node)
                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  NODE / DATA PLANE                                                           │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  kubelet                                                  ─── ch 10    │  │
│  │   syncLoop · PLEG · pod workers · probe manager · evictions            │  │
│  │   device/CPU/memory/topology managers · volume manager                 │  │
│  └─────┬──────────────────┬─────────────────────┬──────────────────────┘     │
│        │ CRI gRPC         │ CNI exec            │ CSI gRPC                    │
│        ▼                  ▼                     ▼                             │
│  ┌──────────┐    ┌────────────────┐    ┌─────────────────┐                  │
│  │container │    │ CNI plugin     │    │ CSI driver       │                 │
│  │ runtime  │    │ (Calico/Cilium/│    │ (EBS, Ceph, …)  │                  │
│  │(containerd│   │  Flannel/…)    │    │  ch 19           │                 │
│  │ /CRI-O)  │    │  ch 15, 16     │    │                  │                 │
│  │  ch 01   │    └────────────────┘    └─────────────────┘                  │
│  └─────┬────┘                                                                │
│        │ OCI runtime (runc / kata / gvisor)                                  │
│        ▼                                                                      │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  LINUX KERNEL                                              ─── ch 00   │  │
│  │   namespaces (pid/net/mnt/uts/ipc/user/cgroup/time)                    │  │
│  │   cgroups v2 (cpu/memory/io/pids)                                       │  │
│  │   capabilities · seccomp · AppArmor/SELinux                            │  │
│  │   netfilter/nftables · veth · bridge · VXLAN · eBPF (TC/XDP/cgroup)    │  │
│  │   overlayfs · fuse · loop devices                                       │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  kube-proxy  (iptables / IPVS / nftables / replaced by eBPF) ── ch 14 │  │
│  │   Service VIP → endpoint selection                                     │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘

         ╔══════════════════════════════════════════════════════════════╗
         ║  EXTENSION SURFACE  (everything custom lives here)           ║
         ║  CRDs + conversion webhooks            ─── ch 23             ║
         ║  Mutating / Validating admission       ─── ch 06             ║
         ║  ValidatingAdmissionPolicy (CEL)       ─── ch 06, 28         ║
         ║  Aggregated API servers                ─── ch 24             ║
         ║  Custom schedulers / scheduler plugins ─── ch 34             ║
         ║  Custom controllers / operators        ─── ch 23             ║
         ║  CNI / CSI / CRI / Device plugins      ─── ch 01, 15, 19, 10 ║
         ║  Cloud provider (CCM)                  ─── ch 37             ║
         ╚══════════════════════════════════════════════════════════════╝

         ╔══════════════════════════════════════════════════════════════╗
         ║  MULTI-CLUSTER / FLEET (orthogonal — wraps any cluster)      ║
         ║  ClusterAPI · Karmada · Fleet · Crossplane · Submariner      ║
         ║  GitOps engines (ArgoCD, Flux) drive desired state in        ║
         ║  ─── ch 26, 31                                               ║
         ╚══════════════════════════════════════════════════════════════╝
```

**The key intuition.** Kubernetes is *not* a container orchestrator. It is **etcd + N controllers that watch etcd**. Container orchestration is just one application of that pattern. Every higher concept — autoscaling, service routing, secret rotation, multi-cluster, GitOps, operators — is more controllers reading and writing more objects. Once you internalize that, the API surface stops being intimidating and becomes a Lego set.

---

## 2. The Five Universal Pipelines

Kubernetes has exactly five hot paths. Memorize these flows and you can reason about any feature, any failure.

### 2.1 Apply Path: `kubectl apply -f deploy.yaml`

```
YAML
  │
  ▼  [kubectl client — ch 05]
parse → discovery (find GVR) → openapi schema → server-side-apply patch
  │
  ▼  [apiserver — ch 05]
TLS termination → AuthN (cert/OIDC/SA token/webhook)
  │
  ▼   ch 07
AuthZ (RBAC eval over user→role→verb→resource)
  │
  ▼   ch 06
Mutating admission webhooks (in order, parallel within same stage)
   + built-in mutators (defaulters, ownerRef, SA token injection)
  │
  ▼
Schema + CEL validation (OpenAPI v3 + x-kubernetes-validations)
  │
  ▼   ch 06
Validating admission webhooks + ValidatingAdmissionPolicy (CEL, in-process)
  │
  ▼   ch 05
Storage: etcd transaction (compare-and-swap on resourceVersion)
  │
  ▼   ch 04
etcd Raft: leader appends, replicates to followers, commits when quorum acks
  │
  ▼   ch 05
Watch fan-out: every interested client (controllers, kubelets, schedulers)
  receives the event over their existing watch stream
```

**Two non-negotiable rules.** (1) The apiserver is the **only** writer to etcd; controllers never touch etcd directly. (2) Admission runs *server-side*, not client-side; trusting `kubectl` validation is a security hole.

### 2.2 Scheduling Path: a freshly created Pod becomes a running container

```
[apiserver] Pod object created, spec.nodeName == ""
  │
  ▼  watch event
[kube-scheduler — ch 09]
  Scheduling cycle:
    PreFilter → Filter (NodeAffinity, NodePorts, VolumeBinding, …)
    PostFilter (preemption if no fit)
    PreScore → Score (NodeResourcesFit, ImageLocality, InterPodAffinity, …)
    Reserve → Permit (gating: e.g., gang scheduling)
  Binding cycle:
    PreBind (volume bind) → Bind (PATCH spec.nodeName)
  │
  ▼  watch event
[kubelet on chosen node — ch 10]
  syncLoop sees new pod assignment
  Volume manager: attach/mount PV via CSI       ─── ch 19
  Network: SetUp pod sandbox via CNI            ─── ch 15
  CRI: RunPodSandbox (containerd creates pause container)
  CRI: PullImage (auth via imagePullSecrets)    ─── ch 02
  CRI: CreateContainer + StartContainer for init then app containers
  │
  ▼
[CRI shim → OCI runtime] runc creates namespaces, sets cgroups, execs entrypoint
  │
  ▼
[PLEG — ch 10] observes container state change, emits sync event
[Status manager] PATCH pod.status (Running, podIP, conditions)
  │
  ▼  watch event
[Endpoint(Slice) controller — ch 14] sees Ready pod, adds to matching Services
[kube-proxy on every node — ch 14] reconciles iptables/IPVS/eBPF rules
```

**Where each chapter fits:** scheduling decisions → 09 · CRI lifecycle → 01, 10 · CNI → 15, 16 · CSI → 19 · service propagation → 14.

### 2.3 Reconcile Loop (the heartbeat of every controller)

```
controller startup
  │
  ▼  [client-go — ch 08]
Informer: List + Watch on (GroupVersionResource, namespace, labelSelector)
  │
  ▼
Reflector pushes events into a DeltaFIFO
  │
  ▼
Indexer applies deltas into a thread-safe local store (the cache)
  │
  ▼
Event handlers enqueue object keys ("ns/name") into a rate-limited workqueue
  │
  ▼
Worker pool, each loop:
   key = queue.Get()
   obj := cache.Get(key)              ← never re-fetch from apiserver
   desired := computeDesired(obj)
   actual  := observeWorld(obj)        ← read OTHER objects from THEIR caches
   diff   := desired - actual
   apply(diff) via apiserver           ← server-side-apply preferred
   if err: queue.AddRateLimited(key)   ← exponential backoff
   else:   queue.Forget(key)
   queue.Done(key)
```

**Two non-negotiable rules.** (1) Reconcile must be **idempotent and level-triggered** — if called twice on the same state, the second call must be a no-op. Edge-triggered controllers are the #1 source of split-brain bugs. (2) Always read through the cache; bypassing it kills the apiserver under load.

### 2.4 Service Routing Path (north-south + east-west)

```
client Pod sends packet to Service VIP 10.96.0.42:80
  │
  ▼  [kernel netns of source pod]
veth → cni0 bridge (or eBPF redirect)
  │
  ▼  [host netns, PREROUTING/OUTPUT chain]
  ┌─────────────────────────────────────────────────────────────────┐
  │ kube-proxy mode:                                                 │
  │  iptables: KUBE-SERVICES → KUBE-SVC-XXX → random KUBE-SEP-YYY    │
  │            DNAT to pod IP                                        │
  │  IPVS:     ipvsadm rules, virtual server → real server           │
  │  eBPF (Cilium): cgroup-attached socket-level LB,                 │
  │                 skip iptables entirely                            │
  │  nftables (1.31+): same logic, modern dataplane                  │
  └─────────────────────────────────────────────────────────────────┘   ch 14, 16
  │
  ▼  DNAT'd packet, srcIP=hostIP if external, else podIP
Routed via host routing table → encap (VXLAN/IPinIP) or direct (BGP)  ch 15
  │
  ▼  [destination node]
Decap → bridge → veth → destination pod netns
  │
  ▼
NetworkPolicy enforcement (CNI plugin: Calico Felix / Cilium eBPF)     ch 20
```

For ingress (north-south): packet hits LoadBalancer service (cloud LB), forwarded to node, then identical path. Gateway API / Ingress controller (Envoy/NGINX/HAProxy) terminates at L7 and re-routes by Host/Path. **ch 17.**

### 2.5 Garbage Collection Path

```
Delete a parent object (e.g., Deployment)
  │
  ▼  [apiserver]
If deletion policy = Foreground:
   set deletionTimestamp + foregroundDeletion finalizer
   object stays visible until all dependents (with ownerRef + blockOwnerDeletion) are gone
If Background (default):
   delete parent, GC controller cascades
If Orphan:
   strip ownerRefs from dependents, delete parent
  │
  ▼  [garbage collector — ch 36]
Watches every type, builds an in-memory ownerRef graph
When parent is gone (or being foreground-deleted), enqueues dependents
Issues DELETE to apiserver for each dependent in the right order
  │
  ▼  [finalizers]
Object is NOT removed from etcd until finalizers list is empty
Each finalizer is a contract: some controller must clear it after cleanup
  │
  ▼  [apiserver]
When finalizers empty AND deletionTimestamp set → actual etcd delete
Watch event fires for the deletion
```

**The lesson:** *Delete* in Kubernetes is two-phase — *mark* then *purge*. Finalizers are how external cleanup (cloud LBs, attached volumes, off-cluster resources) hooks into that. Forgotten finalizer = zombie object forever.

---

## 3. The Build Order: Phase 0 → Phase 24

If you sat down to build a Kubernetes-equivalent system from scratch, this is the order. Each phase depends on the previous ones. Skipping is what makes "K8s feels magic" — most magic is just phases 7–10 reconciling each other.

| Phase | What you build | Why now | Chapter |
|---|---|---|---|
| **0** | Linux primitives: namespaces, cgroups, capabilities, seccomp, OverlayFS, veth/bridge, netfilter, eBPF basics | A container is just a process with namespaces + cgroups + LSM profile. You can't reason about anything above without this. | [00](#chapter-plan-the-roadmap) |
| **1** | OCI runtime: take a rootfs + config.json, produce a running process. Reimplement a subset of runc. | This is what "running a container" actually means. K8s never does this directly — it talks to a CRI shim that talks to an OCI runtime. | 01 |
| **2** | OCI image spec + registry: layered tar+json, content-addressable, manifest lists. Pull from a registry, unpack with overlayfs. | Without this, no image distribution. Also: supply-chain security starts here (Sigstore, SBOM). | 02 |
| **3** | A higher-level container runtime (containerd-equivalent): image management + snapshotter + a CRI gRPC server. | Decouples kubelet from runc. This is where CRI lives. | 01, 02 |
| **4** | A Raft-replicated, MVCC KV store with watch + lease (etcd-equivalent). | Every later phase assumes a strongly consistent watchable store. This is the heart. | 04 |
| **5** | An API server: REST over typed resources, OpenAPI discovery, optimistic concurrency on resourceVersion, list+watch over the KV. | All other components are clients of this. | 05 |
| **6** | AuthN/AuthZ + admission chain (mutating, then validating). | The moment multiple tenants can touch the API safely. | 06, 07 |
| **7** | client-go-equivalent: informer + reflector + workqueue + shared cache. Plus leader election. | The reconcile loop pattern. Every controller is built on this. | 08 |
| **8** | A node agent (kubelet-equivalent): watch Pods bound to me, call CRI to run them, report status. | First time a Pod actually runs end-to-end. | 10 |
| **9** | A scheduler: watch unscheduled Pods, run filter+score, patch nodeName. | Cluster becomes useful: workloads land on the right node. | 09 |
| **10** | Built-in controllers: Deployment → ReplicaSet → Pod, plus the GC controller for ownerRefs. | Now you can do rolling updates. This is the "K8s API works" milestone. | 08, 12, 36 |
| **11** | A CNI: assign Pod IPs, give every Pod connectivity to every other Pod (the K8s networking model). | Without this you have isolated pods. Decide: overlay (VXLAN) vs underlay (BGP) vs eBPF. | 15, 16 |
| **12** | kube-proxy-equivalent: Services → load-balance to endpoints. | Stable virtual IPs for ephemeral pods. The killer feature. | 14 |
| **13** | DNS (CoreDNS): Service name → ClusterIP, headless → pod IPs. | Apps stop hard-coding IPs. | 18 |
| **14** | A CSI driver + the in-cluster volume controllers (attach/detach, provisioning). | Stateful workloads. PV/PVC binding lifecycle. | 19 |
| **15** | StatefulSet, DaemonSet, Job, CronJob controllers. | The workload zoo beyond Deployment. | 12, 13 |
| **16** | CRDs + a controller-runtime-equivalent + the operator pattern. | Now users can extend the API without forking your project. This is what made K8s win. | 23 |
| **17** | Webhooks: mutating, validating, conversion. Then in-process CEL via ValidatingAdmissionPolicy. | The full extension surface. Policy engines (OPA, Kyverno) live here. | 06, 28 |
| **18** | API aggregation: a second API server registered behind the main one (metrics-server is the canonical example). | When CRDs aren't enough — you need a different storage backend. | 24 |
| **19** | NetworkPolicy enforcement, Pod Security Admission, runtime security (Falco-style eBPF). | Multi-tenant safety. | 20, 28 |
| **20** | HPA, VPA, cluster-autoscaler, Karpenter, KEDA. | Elasticity. All are just more controllers. | 22 |
| **21** | Ingress + Gateway API + service mesh (Envoy / eBPF). | L7 routing, mTLS, canaries, traffic splitting. | 17 |
| **22** | Cluster lifecycle: bootstrap (kubeadm-style), upgrades, etcd backup/restore. | Day-2 ops. | 32 |
| **23** | GitOps engine (ArgoCD/Flux equivalent), Helm/Kustomize-style packaging. | Declarative deploys at fleet scale. | 31 |
| **24** | Multi-cluster: ClusterAPI for provisioning, Karmada/Fleet for workload propagation, Crossplane for off-cluster. | Beyond a single cluster: governance, geo, blast-radius. | 26 |

**The sentence to remember.** *Phases 0–3 build containers. Phases 4–7 build a declarative API. Phases 8–13 turn it into an orchestrator. Phases 14–17 make it extensible. Phases 18–24 make it production and multi-cluster.* Most production complaints are mis-tuned phase 14 (storage) or 11 (networking). Most outages are phase 4 (etcd) or phase 5 (apiserver). Most security incidents are phase 6 (RBAC/admission) and phase 19 (runtime).

---

## 4. Chapter Plan (the roadmap)

These chapters are placeholders; we'll fill them one by one. Each is sized to match the databases/ folder depth (~1500–3500 lines of staff-level material, with diagrams, kernel-level traces, ASCII state machines, and references to the Kubernetes source tree).

| # | File (planned) | Theme | Depth markers |
|---|---|---|---|
| **00** | `00-linux-primitives-for-containers.md` | Namespaces (pid/net/mnt/uts/ipc/user/cgroup/time), cgroups v1 vs v2, capabilities, seccomp-bpf, AppArmor/SELinux, OverlayFS, veth/bridge/VXLAN, netfilter/nftables, eBPF (kprobes, tc, XDP, cgroup hooks) | unshare + nsenter walkthrough · cgroup-v2 unified hierarchy · seccomp BPF program byte-level · OverlayFS inode whiteouts |
| **01** | `01-container-runtimes-cri-oci.md` | OCI runtime spec, runc internals, containerd architecture (snapshotter, content store, shim v2), CRI-O, the CRI gRPC contract | runc create→start state machine · shim-per-pod model · CRI vs OCI vs CNI separation |
| **02** | `02-container-images-and-registries.md` | OCI image spec, layer tarballs, manifest/index, content-addressable digests, registry API v2, distribution spec, image GC, lazy pulling (stargz/SOCI), Sigstore/cosign | layer dedup math · registry auth (bearer, ECR/GCR/ACR) · supply chain (SLSA, SBOM, in-toto) |
| **03** | `03-kubernetes-architecture-overview.md` | Control plane vs data plane, HA topologies, stacked vs external etcd, control-plane sizing, the everything-is-an-API-object axiom | 5000-node reference architecture · k8s.io repo map · the "watch everything" principle |
| **04** | `04-etcd-internals.md` | Raft, MVCC revisions, watch streams, lease + TTL, transactions, compactions, defrag, snapshots, mvcc-watch backpressure | bbolt page layout · Raft log truncation · watch event coalescing · v3 vs v2 API |
| **05** | `05-kube-apiserver-internals.md` | REST handlers, registry/storage, conversion between API versions, watch cache, server-side apply, OpenAPI, discovery, API Priority and Fairness (APF), audit | request flow per chain · APF flowschema/prioritylevel math · watch cache vs etcd watch · protobuf vs JSON |
| **06** | `06-admission-control-deep-dive.md` | Mutating/validating webhooks, MutatingWebhookConfiguration ordering, conversion webhooks, ValidatingAdmissionPolicy (CEL in-process), CEL language, admission failure modes | webhook latency budget · CEL cost guarding · ordering pitfalls · Kyverno vs Gatekeeper vs VAP |
| **07** | `07-authentication-authorization.md` | x509 client certs, bootstrap tokens, OIDC, ServiceAccount tokens (legacy vs projected vs bound), webhook AuthN, RBAC eval, ABAC, Node authorizer, scope of impersonation | SA token rotation · BoundServiceAccountTokenVolume · IRSA/Workload Identity tie-in · RBAC denormalization for perf |
| **08** | `08-controller-pattern-and-client-go.md` | Informer/Reflector/DeltaFIFO/Indexer, workqueue (rate-limited, delayed), leader election (lease-based), shared informer factory, controller-runtime (Manager, Reconciler, Cache, Client) | every line of a reconcile loop · resync vs relist · event handler pitfalls · leader-election split-brain |
| **09** | `09-kube-scheduler-internals.md` | Scheduling framework v1, extension points (PreFilter→Filter→PostFilter→PreScore→Score→Reserve→Permit→PreBind→Bind), built-in plugins, preemption, topology spread constraints, descheduler, scheduler profiles, scheduling gates | plugin registration · per-node feasibility cache · the binding-cycle separation · gang scheduling extensions |
| **10** | `10-kubelet-internals.md` | syncLoop, pod workers, PLEG (Pod Lifecycle Event Generator), probe manager, status manager, volume manager, device manager, CPU manager (static/none), memory manager, topology manager, eviction manager, image GC, container GC, log rotation | PLEG state machine · soft vs hard eviction thresholds · NUMA-aware allocation · kubelet→CRI gRPC tracing |
| **11** | `11-pod-internals.md` | Pod spec semantics, init containers, native sidecars (1.28+), ephemeral containers, restart policy, probes (startup/readiness/liveness), lifecycle hooks (preStop, postStart), terminationGracePeriod, podIP allocation, pause container | pod startup state diagram · graceful shutdown sequencing · readiness gate semantics |
| **12** | `12-workload-controllers.md` | Deployment + ReplicaSet (rolling update, surge, maxUnavailable, revision history), DaemonSet (node affinity, rolling), Job (parallelism, completions, indexed jobs, suspend), CronJob (concurrency policy, missed runs) | revisioning via PodTemplateHash · Job backoff · Cron timezone & DST · DaemonSet without scheduler |
| **13** | `13-statefulset-deep-dive.md` | Ordered creation/deletion, headless Services, stable network identity, PVC templates, PVC retention policies, partitioned rollouts, parallel pod management | reverse-order teardown · split-brain on rename · DBs (Postgres operator, etcd operator) as case studies |
| **14** | `14-services-and-kube-proxy.md` | Service types (ClusterIP, NodePort, LoadBalancer, ExternalName, Headless), Endpoints vs EndpointSlice, kube-proxy modes (iptables, IPVS, nftables), session affinity, topology-aware hints, internalTrafficPolicy, externalTrafficPolicy | iptables rule explosion math · IPVS scaling curve · EndpointSlice slicing · why kube-proxy can be replaced |
| **15** | `15-cni-and-pod-networking.md` | CNI spec, plugin chains, IPAM, the Pod networking model, overlays (VXLAN/Geneve) vs underlays (BGP) vs eBPF, Calico, Flannel, Weave, AWS VPC CNI, Azure CNI | dual-stack IPv4/IPv6 · MTU rules · cross-AZ traffic costs · CNI plugin failure modes |
| **16** | `16-cilium-and-ebpf-deep-dive.md` | Cilium datapath (kube-proxy replacement, socket LB, host routing), Hubble, Tetragon, BPF maps, BTF, CO-RE, XDP vs TC vs cgroup hooks, eBPF verifier | end-to-end ping path through eBPF · map types · why Cilium beats iptables at scale |
| **17** | `17-ingress-gateway-and-service-mesh.md` | Ingress (NGINX/HAProxy/Traefik/Envoy controllers), Gateway API (Gateway/HTTPRoute/GRPCRoute), L7 routing semantics, Istio (sidecar + ambient), Linkerd, Envoy xDS, mTLS, traffic splitting, canary, retries/timeouts, locality LB | Gateway API vs Ingress · xDS protocol · sidecar startup ordering · ambient (ztunnel + waypoint) architecture |
| **18** | `18-dns-and-coredns.md` | CoreDNS architecture, plugins, the cluster DNS contract (ndots, search paths), headless service resolution, NodeLocalDNSCache, ExternalDNS, DNS-based service discovery pitfalls | ndots=5 latency trap · headless-A vs SRV · negative caching · CoreDNS scaling |
| **19** | `19-storage-csi-pv-pvc.md` | CSI architecture (controller plugin + node plugin), the three-phase lifecycle (provision → attach → mount), PV/PVC binding, StorageClass, dynamic provisioning, volume snapshots, ephemeral volumes (generic + CSI), volume expansion, ReadWriteOncePod | sidecar containers (provisioner, attacher, resizer, snapshotter) · access modes truth table · raw block volumes |
| **20** | `20-network-policy-and-segmentation.md` | NetworkPolicy spec, default-deny patterns, Calico GlobalNetworkPolicy, Cilium L7 policies, AdminNetworkPolicy / BaselineAdminNetworkPolicy (ANP/BANP), egress gateways | semantics of "ingress" vs "egress" · DNS-based egress · zero-trust east-west |
| **21** | `21-resource-management-and-qos.md` | Requests vs limits, QoS classes (Guaranteed/Burstable/BestEffort), cgroup-v2 memory.high/memory.max, CPU throttling vs CFS quotas, OOM scoring, eviction signals (memory.available, nodefs.available, imagefs.inodesFree), pid pressure | static CPU manager binding · throttling-vs-latency curves · NUMA + topology manager interactions |
| **22** | `22-autoscaling.md` | HPA (v2 metrics, behavior config, stabilization windows), VPA (recommender/updater/admission), cluster-autoscaler (expanders, scale-from-zero), Karpenter (NodePool, consolidation, drift), KEDA (event-driven, scalers, scaled jobs) | HPA control loop · VPA conflict with HPA · Karpenter vs CA tradeoffs · scale-from-zero semantics |
| **23** | `23-crds-operators-and-controller-runtime.md` | CRD spec (schema, subresources status/scale, additionalPrinterColumns), conversion strategy (none/webhook), kubebuilder/operator-sdk scaffolding, controller-runtime Manager + Reconciler + Cache, the Operator pattern, OLM, OperatorHub, capability levels | API versioning strategy · status vs spec discipline · finalizer-driven cleanup · multi-cluster operators |
| **24** | `24-api-aggregation-and-extension-apiservers.md` | APIService, the aggregation layer, sample-apiserver, metrics-server, custom-metrics-apiserver, building an apiserver with apiserver-runtime, when to choose aggregation vs CRD | gRPC vs HTTP backend · auth delegation · storage backend choice |
| **25** | `25-multi-tenancy.md` | Namespaces as security boundary (and where they aren't), Hierarchical Namespace Controller (HNC), Capsule, Kiosk, vCluster (virtual control planes), soft vs hard multi-tenancy, the noisy-neighbor problem | RBAC per tenant · ResourceQuota + LimitRange + PriorityClass · vCluster architecture |
| **26** | `26-multi-cluster-and-fleet.md` | ClusterAPI (providers, MachineDeployment, KubeadmControlPlane), Karmada, Fleet, Crossplane, Submariner, KCP (workspaces), federation v2 lessons | ClusterAPI bootstrap chicken-and-egg · workload propagation strategies · cross-cluster service discovery |
| **27** | `27-supply-chain-security.md` | Image signing (Sigstore/cosign/Fulcio/Rekor), SBOM (CycloneDX, SPDX), SLSA levels, in-toto attestations, admission-time verification (policy-controller, Kyverno, Connaisseur), build provenance | keyless signing flow · transparency log · admission policy templates |
| **28** | `28-runtime-security-and-policy.md` | Pod Security Admission (privileged/baseline/restricted), OPA Gatekeeper, Kyverno, ValidatingAdmissionPolicy (CEL) vs webhooks, Falco (sys_enter eBPF), Tetragon, Tracee, runtime detection vs prevention | CEL cookbook · policy enforcement vs audit · seccomp profile generation · audit log analysis |
| **29** | `29-pod-sandboxing.md` | gVisor (sentry + gofer, syscall interception), Kata Containers (lightweight VMs via QEMU/Cloud Hypervisor/Firecracker), Confidential Containers (TDX, SEV-SNP), RuntimeClass | gVisor syscall coverage gaps · Kata cold start budget · attestation flow · when not to use a sandbox |
| **30** | `30-observability-internals.md` | Metrics pipeline (kube-state-metrics, metrics-server, cAdvisor), Prometheus integration, OpenTelemetry Operator, Loki/Tempo, kubelet /metrics endpoints, controller manager metrics, scheduler metrics | the four golden signals per K8s component · cAdvisor cgroup walk · scrape budget math |
| **31** | `31-gitops-helm-kustomize.md` | ArgoCD architecture (application controller, repo server, app-of-apps, ApplicationSet), Flux (source/kustomize/helm controllers), Helm v3 (template engine, hooks, release storage), Kustomize (overlays, patches, generators), drift detection, sync waves | pull vs push GitOps · multi-tenancy in ArgoCD · Helm-vs-Kustomize tradeoffs · render-then-apply pipelines |
| **32** | `32-cluster-lifecycle-and-day2.md` | kubeadm bootstrap, control-plane upgrades (skew policy), node upgrades (drain, surge, PDBs), etcd backup/restore (snapshot, restore-from-snapshot, defrag), disaster recovery, Velero (backup, restic, CSI snapshots) | the +/-1 minor version skew rule · safe drain sequence · etcd member replacement · backup verification |
| **33** | `33-edge-and-special-distributions.md` | K3s (single binary, sqlite/etcd, embedded), MicroK8s, KubeEdge (edge-cloud sync, device twin), Akri (device discovery), OpenYurt | distribution tradeoffs · edge connectivity assumptions · what gets stripped |
| **34** | `34-custom-schedulers-and-scheduler-framework.md` | Building a scheduler plugin, the scheduler framework SDK, multi-scheduler setups, scheduling gates, batch / gang scheduling (Volcano, Yunikorn), capacity scheduling, the Scheduling SIG roadmap | plugin lifecycle · CycleState · multi-cluster scheduling (Karmada scheduler) |
| **35** | `35-performance-scaling-and-tuning.md` | API Priority & Fairness tuning, etcd tuning (heartbeat, election timeout, snapshot count, defrag cadence), watch cache sizing, large-cluster patterns (5k–15k nodes), kube-proxy at scale, scheduler throughput, controller-manager work-queue tuning | scalability SIG SLOs · 110-pods-per-node limit · why etcd defrag is the silent killer · pprof profiles for each component |
| **36** | `36-garbage-collection-and-object-lifecycle.md` | OwnerReferences (controller=true, blockOwnerDeletion), finalizers, cascade policies (Background/Foreground/Orphan), the garbage collector controller's ownership graph, TTL-after-finished controller | finalizer footguns · GC graph cycles · orphan-and-adopt patterns |
| **37** | `37-cloud-provider-integration.md` | Cloud Controller Manager, in-tree → out-of-tree migration, node controller (lifecycle, addresses, taints), route controller, service controller (LoadBalancer provisioning), volume controller (legacy), IRSA / Workload Identity / Azure AD Pod Identity | provider plugin model · cloud LB reconciliation race · cross-zone egress costs |
| **38** | `38-building-a-kubernetes-from-scratch.md` | Capstone: design a minimal K8s-equivalent (we'll call it `minik8s.py` in spirit of `simpledb.py`) that ties chapters 00–37 together. Builds in the order of §3. | the kubelet you can read in one sitting · the apiserver in 500 LoC · why your toy will hit etcd before networking |
| **44** | `44-secrets-and-configmaps-deep-dive.md` | ConfigMap & Secret API objects, etcd Base64 vs KMS v2 envelope encryption, env vs volume mounts, atomic symlink tree swaps, subPath bind-mount traps, immutable: true scalability, Secret Store CSI vs External Secrets Operator, rotation patterns | KMS v2 envelope encryption gRPC flow · atomic symlink renameat(2) tree · subPath static inode trap · immutable watch reduction math · fsnotify reloaders |


---

## 5. Component Responsibility Map

When something breaks, this is how to attribute blame.

| Component | Owns | Doesn't own | Chapter |
|---|---|---|---|
| **etcd** | Replicated, watchable, MVCC KV. Lease+TTL. | Schema, semantics, admission | 04 |
| **kube-apiserver** | REST, AuthN/Z, admission, conversion, watch fan-out, APF | Business logic, scheduling, container lifecycle | 05, 06, 07 |
| **kube-scheduler** | Pod → Node assignment (Bind) | Image pull, runtime, status | 09, 34 |
| **kube-controller-manager** | Built-in reconcile loops (Deployment, ReplicaSet, Node, Endpoints, GC, …) | Custom logic (that's your controllers) | 08, 12, 36 |
| **cloud-controller-manager** | Cloud LB, Routes, Nodes, attached volumes (legacy) | Anything that runs on the node | 37 |
| **kubelet** | Pod lifecycle on this node, status reporting, evictions, volume mount, CNI/CRI/CSI gluing | Scheduling, cross-node networking | 10, 11 |
| **container runtime (containerd/CRI-O)** | Pulling images, running OCI containers, image GC | Networking (that's CNI), storage (that's CSI) | 01, 02 |
| **OCI runtime (runc/kata/gvisor)** | Setting up namespaces/cgroups, exec | Image management | 01, 29 |
| **kube-proxy** | Service VIP → endpoint DNAT | Pod-to-pod connectivity (that's CNI) | 14 |
| **CNI plugin** | Pod IP, veth, cross-node connectivity, NetworkPolicy | Service VIPs (kube-proxy or CNI-replacement) | 15, 16, 20 |
| **CSI driver** | Provision/attach/mount/snapshot/expand volumes | PV/PVC objects (the apiserver) | 19 |
| **CoreDNS** | Cluster DNS | External DNS (ExternalDNS controller) | 18 |
| **Ingress / Gateway controller** | L7 routing, TLS termination | Service VIPs (kube-proxy) | 17 |
| **HPA / VPA / CA / Karpenter** | Scale decisions (replica count, resource size, node count) | Actual pod creation (controllers do that) | 22 |
| **Admission webhook / VAP** | Reject or mutate objects at write time | Continuous enforcement (need a controller) | 06, 28 |
| **CRD controller (operator)** | Reconcile a CR's spec to real-world state | Built-in resource semantics | 23 |
| **Aggregated API server** | Implement a non-CRD API surface backed by anything | Storing into etcd (you choose backend) | 24 |
| **GitOps engine (Argo/Flux)** | Drive cluster state from Git | Authoring desired state (humans do that) | 31 |
| **Policy engine (OPA/Kyverno)** | Evaluate constraints, generate, mutate, audit | Runtime enforcement (need DaemonSet for that) | 28 |

The diagonal observation: each component owns *exactly one* concern. When two seem to overlap (e.g., "do I check pod identity at admission or at runtime?"), production K8s splits it the way the table above does. Crossing that line is the source of most bugs and CVEs.

---

## 6. Cross-Cutting Concerns (the 6 Hard Problems)

Every Kubernetes operator, no matter the workload, hits these six problems. The chapters mostly exist because each problem has many possible solutions.

### 6.1 Identity — "who is this request? what can it do?"

Mechanisms: x509, bearer tokens, OIDC, SA projected tokens, webhook AuthN; then RBAC, ABAC, Node authorizer, webhook AuthZ. Workload identity layered on top (IRSA, GKE Workload Identity, Azure AD Workload Identity, SPIFFE/SPIRE).

- Apiserver requests authenticate ONCE per request; AuthN is stateless
- RBAC is additive; deny is implicit
- ServiceAccount projected tokens are bound (audience, expiration, pod) — the legacy long-lived tokens are a footgun

**Chapters:** 07 (deep), 27 (supply-chain identity), 24 (aggregation auth delegation).

### 6.2 Scheduling & Placement — "which node runs this?"

Choices: default scheduler with affinity/anti-affinity/topology spread, custom scheduler, multi-scheduler, gang scheduling (Volcano), capacity scheduling, descheduler.

- Filter eliminates infeasible nodes; Score ranks among feasible
- Preemption is a last resort and respects PDBs
- Topology spread + pod anti-affinity often conflict — pick one

**Chapters:** 09 (framework), 34 (custom), 22 (autoscaling interaction).

### 6.3 Networking — "how do these pods talk?"

The K8s model: every Pod gets an IP, every Pod can reach every other Pod, every Service is a stable VIP. Implementations vary wildly.

- CNI for Pod-to-Pod (overlay vs BGP vs eBPF)
- kube-proxy (or replacement) for Service-to-Pod
- Ingress / Gateway / Mesh for L7
- NetworkPolicy for segmentation
- DNS for discovery

**Chapters:** 14, 15, 16, 17, 18, 20.

### 6.4 State — "how do I run stateful workloads?"

- CSI for block/file storage
- StatefulSet for stable identity + ordered lifecycle
- Operators for app-aware lifecycle (Postgres, Cassandra, Kafka, etcd)
- Volume snapshots and backup (Velero) for DR

**Chapters:** 13, 19, 23, 32.

### 6.5 Multi-tenancy & Isolation — "how do tenants coexist safely?"

Layers: namespace (soft), RBAC (logical), ResourceQuota (capacity), NetworkPolicy (network), PSA + policy engine (workload), sandbox runtime (kernel), vCluster / separate cluster (hard).

- Namespaces are NOT a security boundary against a hostile root pod
- Hard multi-tenancy = separate clusters or vClusters
- Confidential containers extend isolation against a hostile node

**Chapters:** 25, 28, 29, 20, 21.

### 6.6 Observability — "what is the cluster actually doing?"

- Metrics: cAdvisor (containers), kubelet (node), kube-state-metrics (objects), metrics-server (HPA input), each control-plane component exposes `/metrics`
- Logs: stdout/stderr → CRI log files → DaemonSet shipper
- Traces: API server has OTEL integration; controllers can emit spans
- Events: K8s events (short-lived) + Audit log (apiserver)

**Chapters:** 30, 35 (perf), 32 (audit-for-DR).

---

## 7. Variant Decision Tree

"Run Kubernetes" only makes sense once you've decided which Kubernetes.

```
What's the deployment target?
│
├── Public cloud, single team
│   → managed K8s (EKS/GKE/AKS) + Karpenter + ArgoCD + service mesh optional
│   Chapters: 22, 26 (single-cluster), 31, 37
│
├── Public cloud, many teams, shared platform
│   → managed K8s + multi-tenancy (namespaces + Kyverno + ResourceQuota)
│     OR vCluster per team OR cluster per team via ClusterAPI
│   Chapters: 25, 26, 28
│
├── Many clusters across regions / providers
│   → ClusterAPI for provisioning + Karmada/Fleet for workloads + Crossplane for cloud resources
│   Chapters: 26, 31
│
├── On-prem / bare-metal
│   → kubeadm or Talos + MetalLB / Cilium BGP + Rook/Ceph CSI
│   Chapters: 15, 19, 32
│
├── Edge / IoT
│   → K3s, MicroK8s, KubeEdge; minimize control-plane footprint
│   Chapters: 33
│
├── HPC / batch / ML training
│   → custom scheduler (Volcano/Yunikorn), gang scheduling, device plugins (GPU), KubeRay/Kueue
│   Chapters: 09, 34, 10 (device manager)
│
└── Regulated / confidential workloads
    → Confidential Containers + signed images + Kyverno admission + Falco runtime
    Chapters: 27, 28, 29
```

**Picking is mostly about blast radius, governance model, and how stateful your workloads are.** Everything else (specific cloud, exact mesh, exact policy engine) is implementation detail.

---

## 8. End-to-End Trace of `kubectl apply`

Concrete trace for `kubectl apply -f nginx-deployment.yaml` against a 3-node cluster. Every line ties back to a chapter.

```
T+0ms     User runs: kubectl apply -f nginx-deployment.yaml
T+5ms     kubectl: discovery → resolve "Deployment" → apps/v1
                                                                       [ch 05]
T+10ms    kubectl: load OpenAPI schema, build SSA patch
T+15ms    HTTPS PATCH /apis/apps/v1/namespaces/default/deployments/nginx
          ?fieldManager=kubectl&force=false
T+20ms    apiserver: TLS handshake done, request enters handler chain
T+22ms    AuthN: client cert → user="alice", groups=["ops"]            [ch 07]
T+24ms    AuthZ: RBAC eval → ClusterRoleBinding "ops-deploy" allows PATCH
T+28ms    Mutating admission webhooks: istio-sidecar-injector adds
          sidecar container + initContainer + volumes                  [ch 06]
T+45ms    Schema validation: OpenAPI v3 + x-kubernetes-validations (CEL)
T+48ms    Validating admission: Kyverno checks "no :latest tag"
T+62ms    Storage: etcd txn — compare resourceVersion, put new object  [ch 04]
T+70ms    Raft: leader appends, replicates, commits (3-node quorum)
T+78ms    Watch fan-out: every watcher with matching selector gets event

T+80ms    [deployment-controller — ch 12] sees Deployment update
          compares spec to ReplicaSet hash → creates new ReplicaSet
T+85ms    [replicaset-controller] sees new RS, replicas=3, current=0
          creates 3 Pods with ownerRef=ReplicaSet                       [ch 36]
T+95ms    apiserver stores 3 Pods (no nodeName), watch fires

T+100ms   [kube-scheduler — ch 09] sees 3 unscheduled Pods
          scheduling cycle per pod:
            PreFilter (volume binding check, port collision check)
            Filter   (NodeAffinity, Taints, Resources, …)  → 2 feasible
            Score    (NodeResourcesFit, ImageLocality)     → node-2 wins
            Reserve, Permit, Bind (PATCH spec.nodeName=node-2)
T+115ms   Binding cycle complete for pod-1; pod-2, pod-3 similar
          (parallel scheduling cycles in newer versions)

T+120ms   [kubelet on node-2 — ch 10] syncLoop notices pod-1 bound
T+122ms   Volume manager: emptyDir + projected SA token volume         [ch 19]
T+125ms   CNI: ADD command → Calico/Cilium plugin
            allocates podIP 10.244.1.42
            creates veth pair, sets up routes, applies NetworkPolicy   [ch 15, 20]
T+150ms   CRI: RunPodSandbox → containerd creates pause container
            namespaces created (net, ipc, uts shared across pod;       [ch 00, 01]
            pid optional via shareProcessNamespace)
T+165ms   CRI: PullImage nginx:1.27 (if not cached)                    [ch 02]
            registry auth via imagePullSecret
            layers downloaded, snapshotter unpacks
T+850ms   CRI: CreateContainer + StartContainer for istio-init
            (sidecar from injection) runs, configures iptables
T+950ms   CRI: CreateContainer + StartContainer for nginx + istio-proxy
            cgroup limits applied (cpu.max, memory.max)                [ch 00, 21]
            seccomp + AppArmor profiles loaded
            runc clone3() → child execs nginx                          [ch 00, 01]
T+1100ms  [PLEG — ch 10] observes container Running state
T+1110ms  Status manager: PATCH pod.status (phase=Running, podIP, conditions)

T+1115ms  apiserver watch fan-out fires again
T+1120ms  [endpointslice-controller — ch 14] sees Ready pod
            adds 10.244.1.42:80 to EndpointSlice for service "nginx"
T+1130ms  Watch event reaches every kube-proxy
T+1135ms  [kube-proxy on every node] reconciles iptables/IPVS rules
            Service VIP 10.96.42.10:80 now DNATs to 10.244.1.42:80
T+1140ms  [CoreDNS] no action needed — Service name already resolves to VIP

T+1200ms  Other 2 pods complete the same path (parallelized across nodes)

T+1500ms  User: kubectl get deployment nginx → READY 3/3
```

**What you just watched:**
- The apiserver was hit by ~20 separate components, all reading and writing the same etcd-backed store
- Zero direct communication between components — everything went via apiserver + watch
- 5 distinct gRPC protocols crossed: CRI, CNI exec, CSI gRPC, apiserver REST, etcd gRPC
- The Linux kernel did the actual isolation (namespaces, cgroups, netfilter); K8s only orchestrated which knobs to turn

Now multiply by 10,000 pods/cluster and you understand why each chapter obsesses over the watch cache, the workqueue rate limiter, and the etcd compaction cadence.

---

## 9. Linear Reading Order

If you want to read every chapter once, this order minimizes "wait, what is X?" moments.

1. **ROADMAP.md** ← you are here. Don't skip.
2. **00** — Linux primitives. Boring until it's not. Sets up *why* containers and Pods look the way they do.
3. **01** — Container runtimes (CRI/OCI). The vocabulary of every later chapter that says "runs a container".
4. **02** — Images and registries. Short, foundational; also the supply-chain entrypoint.
5. **03** — Kubernetes architecture overview. The map.
6. **04** — etcd internals. The heart. Re-read after ch 35 (perf) once.
7. **05** — kube-apiserver. The only component every other one talks to.
8. **06** — Admission control. Where most "weird K8s" behavior originates.
9. **07** — AuthN/AuthZ. The other "weird" source.
10. **08** — Controller pattern + client-go. The biggest one. Every later chapter assumes it.
11. **09** — kube-scheduler.
12. **10** — kubelet.
13. **11** — Pod internals. Now you can read 12+ without flipping back.
14. **12, 13** — Workload controllers + StatefulSet.
15. **14** — Services and kube-proxy.
16. **15, 16** — CNI; then Cilium/eBPF deep dive.
17. **17** — Ingress / Gateway / service mesh.
18. **18** — DNS / CoreDNS.
19. **19** — CSI / PV / PVC.
20. **20** — NetworkPolicy.
21. **21** — Resources and QoS.
22. **22** — Autoscaling.
23. **23** — CRDs and operators. (This is when "I can extend K8s" clicks.)
24. **24** — API aggregation.
25. **25** — Multi-tenancy.
26. **26** — Multi-cluster.
27. **27** — Supply-chain security.
28. **28** — Runtime security + policy.
29. **29** — Pod sandboxing.
30. **30** — Observability.
31. **31** — GitOps + Helm + Kustomize.
32. **32** — Cluster lifecycle.
33. **33** — Edge distributions.
34. **34** — Custom schedulers.
35. **35** — Performance and scaling.
36. **36** — Garbage collection.
37. **37** — Cloud provider integration.
38. **38** — Capstone: build it from scratch.

For "I just want to run it" mode, read 03 → 05 → 08 → 09 → 10 → 12 → 14 → 19 → 22 → 31 and skip the rest until something breaks.

For "I just want to extend it" mode, read 05 → 06 → 07 → 08 → 23 → 24 → 28.

For "I just want to operate it at scale" mode, read 04 → 05 → 10 → 14 → 15 → 19 → 22 → 32 → 35.

---

## 10. Common Pitfalls When Building / Running Your Own

The list of mistakes you (and every textbook K8s deployment) will make on the first try.

1. **Treating namespaces as a security boundary.** They aren't — a privileged container in any namespace can escape and own the node. Hard multi-tenancy needs separate clusters or vClusters + sandbox runtimes. → ch 25, 29.
2. **Edge-triggered controllers.** Reconcile must be level-triggered and idempotent. "I'll just react to the Add event" misses events on controller restart, on dropped watches, on resync. → ch 08.
3. **Not using server-side apply.** Multiple controllers writing the same object via JSON merge patch fight each other forever. SSA's fieldManager makes ownership explicit. → ch 05.
4. **Forgetting finalizers on external resources.** Operator creates a cloud LB, user deletes the CR, operator never gets a chance to delete the LB → orphan resource billed for years. → ch 23, 36.
5. **One huge etcd instance.** Default etcd settings (heartbeat 100ms, election 1s, snapshot every 100k writes) fall over at scale. Defrag is not optional; backups are not optional. → ch 04, 32, 35.
6. **kube-proxy iptables at scale.** O(N) rule matching per packet, O(N²) reconcile time. At ~5k Services, switch to IPVS, nftables, or replace kube-proxy entirely with eBPF. → ch 14, 16.
7. **PodIP = identity.** Pods get rescheduled, IPs recycle. Always identify by Service name, label selector, or the StatefulSet's stable DNS. → ch 13, 14, 18.
8. **No requests, only limits.** Without requests the scheduler thinks the pod is free; node gets oversubscribed, kubelet evicts, pager fires at 3am. Requests drive scheduling and QoS; limits drive throttling and OOM. → ch 21.
9. **CPU limits causing throttling.** CFS quotas throttle even when other CPUs are idle. Many workloads run faster without CPU limits. Memory limits, by contrast, are usually mandatory. → ch 21.
10. **Liveness probes that restart healthy pods.** Liveness should detect deadlock, not slowness. Readiness handles slowness. Conflating the two = cascading restarts under load. → ch 11.
11. **Webhook with no timeout / no failurePolicy=Ignore for non-critical paths.** A wedged webhook can take down the entire cluster's writes. → ch 06.
12. **Long-lived ServiceAccount tokens mounted by default.** Switch to BoundServiceAccountTokenVolume / projected tokens; opt-out per pod with automountServiceAccountToken=false. → ch 07.
13. **Trusting :latest.** Image mutability means rollback is impossible, signature verification is meaningless. Pin by digest; enforce at admission. → ch 02, 27.
14. **No PodDisruptionBudget on stateful workloads.** Node drain during upgrades takes down all replicas at once. → ch 32, 13.
15. **HPA + VPA on the same metric.** They fight; pod oscillates between scaling up and getting resized. Use VPA in recommendation-only mode with HPA. → ch 22.
16. **CRD without versioning strategy.** v1alpha1 ships, prod uses it, you can never break it. Plan conversion webhooks from day one; mark alpha as alpha-with-teeth. → ch 23.
17. **Bypassing the informer cache.** Calling apiserver.Get() inside Reconcile() works for one controller; breaks the apiserver when 100 controllers do it. → ch 08.
18. **Ignoring the +/- 1 minor version skew rule.** kubelet must be within one minor of apiserver; kube-proxy within two. Skipping versions during upgrades = mysterious failures. → ch 32.
19. **Believing `kubectl delete --force --grace-period=0` is safe.** It removes the API object but the container may still be running, holding the volume, serving traffic. Use it only when the node is genuinely gone. → ch 11, 19.
20. **Multi-tenant cluster with default NetworkPolicy = allow-all.** Lateral movement is trivial. Start with default-deny per namespace, then allow specifically. → ch 20.
21. **Operator that mutates spec.** Spec belongs to the user; status belongs to the controller. Mutating spec creates infinite reconcile loops with GitOps engines. → ch 23, 31.
22. **GitOps "drift correction" without escape hatches.** Operator needs to set a field at runtime (e.g., HPA owns replicas), GitOps sets it back, fight ensues. Use ignoreDifferences / fieldManagers correctly. → ch 31.
23. **One cluster per environment.** Works for 1 team. At 50 teams, you want one cluster per blast-radius, and a fleet tool. → ch 26.
24. **Running unbounded workloads (no ResourceQuota, no LimitRange, no PriorityClass).** One misconfigured pod (memory request 1Pi) starves the scheduler queue. → ch 21, 25.

---

**TL;DR pipeline.** *YAML → kubectl → apiserver (AuthN → AuthZ → admission → validate → etcd) → watch fan-out → controllers reconcile → scheduler binds → kubelet runs (CRI → CNI → CSI → OCI runtime → Linux namespaces + cgroups) → status flows back through apiserver.* Build it in that order. Every other chapter in this folder is one of those boxes seen up close. The extensions (CRDs, operators, webhooks, custom schedulers, aggregated APIs, GitOps, multi-cluster) are *the same loop applied recursively to new object types*. Once you see that, Kubernetes stops being a 38-chapter intimidation pile and becomes one loop you already understand, repeated.
