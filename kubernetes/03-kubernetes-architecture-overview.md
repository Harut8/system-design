# Kubernetes Architecture Overview

The architectural map. Kubernetes is not a container orchestrator — it is **etcd plus a swarm of controllers that watch etcd**. Containers are incidental. Everything else — pods, services, deployments, autoscaling, ingress, secrets, GitOps, multi-cluster — is the same loop applied recursively to new object types. This chapter establishes that mental model, walks every component, draws the communication graph, lays out the HA topologies you will actually deploy, and sizes a 5000-node cluster end-to-end. Later chapters (04 etcd, 05 apiserver, 08 client-go, 09 scheduler, 10 kubelet, 14 services, 15 CNI, 19 CSI, 37 cloud provider) zoom into each box. This is the map; consult it whenever you forget which component owns what.

---

## Table of Contents

1. [The Core Axiom: etcd + N Controllers](#1-the-core-axiom-etcd--n-controllers)
2. [Control Plane Components](#2-control-plane-components)
3. [Data Plane Components](#3-data-plane-components)
4. [The Communication Graph: Who Talks to Whom](#4-the-communication-graph-who-talks-to-whom)
5. [HA Topologies: Stacked vs External etcd](#5-ha-topologies-stacked-vs-external-etcd)
6. [Quorum, Write Latency, and Blast Radius: 3-node vs 5-node](#6-quorum-write-latency-and-blast-radius-3-node-vs-5-node)
7. [Leader Election via Lease Objects](#7-leader-election-via-lease-objects)
8. [Sizing Guidance: Small, Medium, Large, XL](#8-sizing-guidance-small-medium-large-xl)
9. [The Scaling SIG SLOs and What Breaks First](#9-the-scaling-sig-slos-and-what-breaks-first)
10. [The Everything-Is-an-API-Object Axiom](#10-the-everything-is-an-api-object-axiom)
11. [GVR, Kind, and the kubectl Discovery Workflow](#11-gvr-kind-and-the-kubectl-discovery-workflow)
12. [The Watch-Everything Principle](#12-the-watch-everything-principle)
13. [The kubernetes/kubernetes Source Tree Map](#13-the-kuberneteskubernetes-source-tree-map)
14. [Reference Architecture: A 5000-Node Cluster](#14-reference-architecture-a-5000-node-cluster)
15. [What Kubernetes Is NOT](#15-what-kubernetes-is-not)
16. [A First End-to-End Trace](#16-a-first-end-to-end-trace)
17. [The "Everything Is a Controller" Recursion](#17-the-everything-is-a-controller-recursion)
18. [Distributions: Vanilla, Managed, Opinionated, Minimal](#18-distributions-vanilla-managed-opinionated-minimal)
19. [The Compatibility Skew Policy](#19-the-compatibility-skew-policy)
20. [TL;DR](#20-tldr)

---

## 1. The Core Axiom: etcd + N Controllers

Strip away the kubectl wrappers, the YAML, the helm charts, the operators, the dashboards. What remains is a single sentence:

> **Kubernetes is a strongly-consistent watchable key-value store (etcd), wrapped by a typed REST API (kube-apiserver), with N independent processes (controllers, schedulers, kubelets) that each watch some subset of the store and reconcile real-world state toward the declared state.**

That's it. Containers are an implementation detail of one such controller (the kubelet). If you replaced containerd with QEMU microVMs tomorrow, the rest of the architecture wouldn't notice. If you replaced Pods with "BareMetalServer" objects tomorrow, the same pattern would still hold. The thing that makes Kubernetes *Kubernetes* is not the container — it's the **watch loop**.

```
                    ┌────────────────────────────────────────────┐
                    │              THE CORE AXIOM                │
                    └────────────────────────────────────────────┘

       ┌────────────────────────┐
       │         etcd           │   Raft-replicated, MVCC KV store
       │  (the only stateful    │   Watch streams = the nervous system
       │   component anywhere)  │   Lease + TTL = the heartbeat
       └───────────┬────────────┘
                   │ gRPC (private)
                   ▼
       ┌────────────────────────┐
       │     kube-apiserver     │   The ONLY process that talks to etcd.
       │  (REST + watch fan-out)│   Stateless. Horizontally scalable.
       └───────────┬────────────┘   AuthN, AuthZ, admission, conversion.
                   │ HTTPS / watch
       ┌───────────┴────────────────────────────────────────────┐
       │                                                        │
       ▼                                                        ▼
  ┌─────────┐ ┌─────────┐ ┌──────────┐ ┌─────────┐ ┌─────────┐ ...N
  │scheduler│ │ ctrl-mgr│ │cloud-ctrl│ │ kubelet │ │  your   │
  │         │ │         │ │  -mgr    │ │(per node│ │operator │
  │(watches │ │(watches │ │(watches  │ │ — also a│ │(watches │
  │ Pods,   │ │ ~30     │ │ Services,│ │watcher) │ │ a CRD)  │
  │ Nodes)  │ │ types)  │ │ Nodes…)  │ │         │ │         │
  └─────────┘ └─────────┘ └──────────┘ └─────────┘ └─────────┘

  All of them implement the same loop:
       List + Watch  →  diff(desired, actual)  →  PATCH apiserver
       (never write to etcd directly; never call other controllers directly)
```

**Three corollaries fall out immediately.**

1. **The apiserver is a cache and a fan-out.** Read traffic is served from an in-memory *watch cache*; only the leader-elected components that need a transaction touch etcd. The whole system's read throughput is bounded by apiserver CPU and network, not by etcd. (This is why managed Kubernetes providers can run small etcds behind large apiserver fleets.)
2. **Components never communicate peer-to-peer.** The scheduler does not call the kubelet. The deployment controller does not call the replicaset controller. They all write to the apiserver; the next interested watcher sees the change. *The communication topology is a star, not a mesh.* (One exception: apiserver → kubelet for `exec`/`logs`/`port-forward`. We'll come back to this.)
3. **Adding a new component means adding a new watcher.** Want HPA? Write a controller that watches Pods and PATCHes Deployment.spec.replicas. Want a service mesh control plane? Write a controller that watches Pods and Services and emits xDS. Want GitOps? Write a controller that watches a Git repo and PATCHes everything. The pattern composes infinitely because nothing assumes a fixed set of controllers.

The mental model to carry into every later chapter: **the apiserver is the bus, etcd is the log, and every other process is a subscriber that also publishes back.** If you can describe a feature in those terms, you understand it; if you can't, you don't yet.

---

## 2. Control Plane Components

The control plane is everything that does *not* run on a worker node by definition. (Many distributions run control-plane components as Pods themselves, which is conceptually recursive but doesn't change the role assignment.) There are five canonical processes.

### 2.1 kube-apiserver — the front door (deep dive: ch 05)

**What it does.** Terminates TLS, authenticates the caller, authorizes the request against RBAC, runs mutating admission (defaulters + webhooks), runs schema + CEL validation, runs validating admission (webhooks + in-process ValidatingAdmissionPolicy), persists the object to etcd via an optimistic-concurrency CAS on `resourceVersion`, and finally fans the change out to every interested watcher over a streaming HTTP/2 connection. It also serves discovery (`/apis`, `/api`), OpenAPI v3 schemas, the aggregation layer (mounting custom apiservers behind a single endpoint), and the API Priority & Fairness fair queuing layer that prevents one client from drowning the rest.

**What it owns.** The REST surface. Everything you can `kubectl get` is shaped, validated, stored, and broadcast by the apiserver. It owns *no business logic* — it doesn't know what a Deployment means, only how to validate and store one.

**What it talks to.** Inward: etcd (gRPC, the only writer to etcd in the entire cluster). Outward: admission webhooks (HTTPS), kubelet (HTTPS, for exec/logs/port-forward and health), aggregated apiservers (HTTPS), and every controller and kubelet as *clients* that initiate watches against it. The apiserver itself initiates very few outbound calls; it is mostly a server.

It is **stateless and horizontally scalable**. Production clusters run 3–5 apiservers behind a TCP load balancer. The only shared state is the etcd cluster behind them.

### 2.2 etcd — the heart (deep dive: ch 04)

**What it does.** Strongly-consistent, Raft-replicated, MVCC key-value store with a streaming watch API and TTL'd leases. Every Kubernetes object lives at a key like `/registry/pods/default/nginx-abc123`. Every write goes through Raft (leader appends, replicates to followers, commits when a quorum of followers ack). Every read can be linearizable (through the leader) or serializable (any member, slightly stale).

**What it owns.** Durable state. *All* of it. If your apiserver dies, you lose nothing; if your etcd dies and the backups are bad, you lose your cluster. There is no other source of truth in Kubernetes — events, leases, configmaps, secrets, custom resources, leader-election leases, all of it lives in etcd.

**What it talks to.** Other etcd members (Raft over gRPC, mutual TLS) and the apiserver (gRPC over a separate client TLS profile). Nothing else. *No controller, kubelet, or operator ever talks to etcd directly.* If you see a Kubernetes component with an etcd client library compiled in, it's a bug or a backdoor.

Etcd is **strongly consistent and quorum-bound**, which means you sacrifice availability under partition (CP in CAP terms). A 3-node etcd survives 1 failure; a 5-node survives 2. We'll come back to the 3 vs 5 tradeoff in §6.

### 2.3 kube-scheduler — the pod placement engine (deep dive: ch 09)

**What it does.** Watches Pods with `spec.nodeName == ""` and Nodes; for each unscheduled pod, runs a **scheduling cycle** (PreFilter → Filter → PostFilter → PreScore → Score → Reserve → Permit) followed by a **binding cycle** (PreBind → Bind → PostBind). The output is a single PATCH that sets `spec.nodeName` on the Pod. Everything else (image pull, network setup, container start) is the kubelet's problem.

**What it owns.** The Pod-to-Node mapping decision. It does not own pod lifecycle, image pulling, container runtime semantics, network setup, or volume provisioning. The scheduler is *pure assignment logic*.

**What it talks to.** Only the apiserver. Reads Pods, Nodes, PVCs, StorageClasses, CSIStorageCapacity (for volume-aware scheduling), and writes Pod bindings. It uses leader election (Lease object) so only one scheduler is active at a time per `--leader-elect-resource-name` — you can run two replicas for HA but only one schedules.

The scheduler is **pluggable via the Scheduling Framework**: every extension point can be customized by registering Go plugins compiled into a binary, configured via a scheduler profile. Custom schedulers (Volcano, Yunikorn, KubeRay) reuse the framework and replace specific plugins for batch/gang scheduling. (Ch 34 goes deep.)

### 2.4 kube-controller-manager — the bag of built-in controllers (deep dive: ch 08)

**What it does.** A single binary that runs ~30 independent reconcile loops, each watching some set of objects and reconciling. The full list includes the Deployment controller, ReplicaSet controller, DaemonSet controller, StatefulSet controller, Job controller, CronJob controller, Node controller (lifecycle and health), Endpoint(Slice) controller, ServiceAccount controller, ResourceQuota controller, Namespace controller, PV/PVC binder, GC controller (cascading delete + owner references), TTL controller, Lease controller, and more. Each runs in its own goroutine and is functionally independent.

**What it owns.** Built-in workload semantics. "What does it mean for a Deployment to do a rolling update?" — answered here. "What happens when a Node goes NotReady for 5 minutes?" — answered here. "Who creates Endpoints for a Service?" — here. "Who runs the garbage collector?" — here.

**What it talks to.** Only the apiserver. Like the scheduler, it uses leader election (a single Lease for the whole bag, so only one kube-controller-manager process is active across the cluster, even if you run 3 for HA).

A subtle point: **your custom controllers are architecturally identical to the built-in ones**. The only differences are (a) the built-ins ship in the same binary, (b) they share a single leader-election lease, and (c) they have privileged kubeconfig access by default. There is nothing magic about a built-in controller — you could rewrite the Deployment controller in 500 lines of Go using client-go and it would interoperate seamlessly.

### 2.5 cloud-controller-manager — the cloud-provider edge (deep dive: ch 37)

**What it does.** Originally these controllers lived inside `kube-controller-manager`, but they were *out-of-tree-d* during the great cloud-provider extraction (2018–2023). The CCM runs four cloud-specific controllers: the **Node controller** (set provider IDs, populate addresses from the cloud API, taint nodes that no longer exist in the cloud), the **Route controller** (program VPC route tables for Pod CIDR ranges on bare CNIs), the **Service controller** (provision cloud load balancers for `type: LoadBalancer` services), and the legacy **volume controller** (now mostly migrated to CSI).

**What it owns.** Every integration point where Kubernetes touches a cloud API. EKS, GKE, AKS each ship their own CCM binary; on-prem clusters typically omit it entirely (and use MetalLB + a bare-metal CNI for LoadBalancer / routing).

**What it talks to.** Apiserver (for Kubernetes objects) and the cloud provider's API (AWS EC2/ELB, GCP Compute, Azure ARM). It is the *only* in-cluster Kubernetes component that legitimately makes outbound calls to a third-party API. (Outside ingress controllers and operators that intentionally do so.)

### 2.6 The Control Plane Stack, Visualized

```
┌────────────────────────────────────────────────────────────────────────┐
│                          CONTROL PLANE NODE                            │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  kube-apiserver  (stateless, behind LB, N replicas)              │  │
│  │  ─────────────────────────────────────────────────────────────   │  │
│  │  AuthN → AuthZ → Mutating Admission → Validation                 │  │
│  │       → Validating Admission → Conversion → Storage              │  │
│  │       → Watch Cache → Watch Fan-out                              │  │
│  │  Also: Discovery, OpenAPI, Aggregation, APF, Audit               │  │
│  └─────────┬─────────────────────────────┬──────────────────────────┘  │
│            │ gRPC (mTLS)                 │ HTTPS (clients)             │
│            ▼                             ▼                             │
│  ┌──────────────────┐         ┌──────────────────────────────────┐     │
│  │      etcd        │         │  Leader-elected controllers:     │     │
│  │  (3 or 5 member  │         │  ┌────────────────────────────┐  │     │
│  │   Raft cluster)  │         │  │  kube-scheduler            │  │     │
│  │                  │         │  │  (Pod → Node binding)      │  │     │
│  │  /registry/...   │         │  └────────────────────────────┘  │     │
│  │  MVCC + watch    │         │  ┌────────────────────────────┐  │     │
│  │  Lease + TTL     │         │  │  kube-controller-manager   │  │     │
│  │                  │         │  │  (~30 built-in loops)      │  │     │
│  │  STACKED:        │         │  └────────────────────────────┘  │     │
│  │   colocated      │         │  ┌────────────────────────────┐  │     │
│  │   with CP nodes  │         │  │  cloud-controller-manager  │  │     │
│  │  EXTERNAL:       │         │  │  (cloud LB, Route, Node)   │  │     │
│  │   separate cluster│         │  └────────────────────────────┘  │     │
│  └──────────────────┘         └──────────────────────────────────┘     │
└────────────────────────────────────────────────────────────────────────┘
```

**Key invariant:** every control-plane component except etcd and the apiserver is *stateless and replaceable at any time*. The scheduler can crash, restart, lose its leader lease, and the worst that happens is a few seconds of delay before unscheduled pods get placed. The controller manager same. Only etcd losing quorum is catastrophic.

---

## 3. Data Plane Components

The data plane is everything on a worker node: the agent that runs containers, the agent that programs Service VIPs, the container runtime that does the syscalls, and the plugins for networking and storage. Five components, one node-agent process plus a runtime plus three pluggable interfaces.

### 3.1 kubelet — the node agent (deep dive: ch 10)

**What it does.** The kubelet is one process per node. It watches the apiserver for Pods bound to its node (`spec.nodeName == ${MY_NODENAME}`), reconciles the local container state to match each Pod's spec via the **CRI** (Container Runtime Interface, gRPC), mounts volumes via the **CSI** (Container Storage Interface, gRPC + filesystem-level plumbing), sets up pod networking via the **CNI** (Container Network Interface, exec-based), runs probes (liveness/readiness/startup), reports pod status back to the apiserver, handles evictions when the node is under resource pressure, and emits events. Inside it runs the syncLoop, the PLEG (Pod Lifecycle Event Generator), the pod workers, the probe manager, the status manager, the volume manager, the device manager, the CPU/memory/topology managers, the eviction manager, and image/container garbage collection.

**What it owns.** The pod-to-process bridge on a single node. Everything below the Pod spec — which container ID is running, what cgroup limits are applied, which veth is attached, which volume is mounted, what the OOM score is — is the kubelet's domain.

**What it talks to.** *Up*: the apiserver (HTTPS watch + PATCH for status). *Down*: the container runtime (`/run/containerd/containerd.sock` or `/run/crio/crio.sock` via CRI gRPC); the CNI plugin (exec, with JSON on stdin/stdout, governed by `/etc/cni/net.d/`); CSI drivers (Unix socket per driver, gRPC); the device plugin daemons (gRPC over `/var/lib/kubelet/device-plugins/`). It also exposes its own HTTPS server on port 10250 that the apiserver calls *back* for exec/logs/port-forward and `/metrics`.

The kubelet is the *only* node-level process that needs apiserver credentials. Everything else (runtime, CNI, CSI) speaks to the kubelet over a local Unix socket.

### 3.2 kube-proxy — the Service VIP programmer (deep dive: ch 14)

**What it does.** Watches Services and Endpoints/EndpointSlices, programs the local node's packet-rewrite tables so that traffic to a Service's ClusterIP gets DNATed to one of the backing Pod IPs. Four modes exist: `iptables` (legacy, O(N) rule matching per packet, doesn't scale past ~5k services), `ipvs` (kernel-native L4 load balancer with hash-based dispatch, scales to ~10k services), `nftables` (modern replacement for iptables, available 1.31+, comparable to ipvs at scale), and *replacement* by eBPF (Cilium's kube-proxy-replacement mode bypasses kube-proxy entirely by attaching socket-level eBPF programs to the cgroup).

**What it owns.** Service VIP → Pod IP rewriting on this node. It does *not* own pod-to-pod connectivity (that's CNI), DNS (that's CoreDNS), L7 routing (that's Ingress/Gateway), or session affinity beyond the simple ClientIP mode.

**What it talks to.** Only the apiserver (watches Services, EndpointSlices, Nodes). It is a DaemonSet on most clusters; it has no leader election because every node programs its own dataplane independently.

### 3.3 Container Runtime — runc + the CRI shim (deep dive: ch 01)

**What it does.** Two layers in one slot. The **CRI shim** (containerd or CRI-O) is a long-running daemon that speaks the Container Runtime Interface (gRPC) to the kubelet on one side and manages images, snapshots, and per-pod runtime shims on the other. The **OCI runtime** (runc, kata, gvisor, youki) is what actually creates the namespaces, sets cgroup limits, drops capabilities, applies seccomp/AppArmor profiles, chroots into the rootfs, and execs the entrypoint. The shim daemon hands a configured `config.json` + bundle directory to the OCI runtime; the runtime does the syscalls.

**What it owns.** Image management (pull, store, GC), container lifecycle (create, start, stop, remove), exec-into-container, and (via the OCI runtime) the actual kernel-level isolation primitives.

**What it talks to.** *Up*: the kubelet (CRI gRPC over `/run/containerd/containerd.sock`). *Sideways*: container registries (HTTPS). *Down*: the OCI runtime (exec) and the Linux kernel (syscalls: `clone3`, `unshare`, `mount`, `pivot_root`, `prctl`, …). It does *not* talk to the apiserver, ever. The kubelet is its only Kubernetes-aware peer.

### 3.4 CNI Plugin — pod IP and connectivity (deep dive: ch 15)

**What it does.** When the kubelet creates a Pod, it invokes the configured CNI plugin (an *executable*, not a daemon — though most plugins also run a long-running daemon for IPAM and policy) with `CNI_COMMAND=ADD`, a JSON config on stdin, and environment variables identifying the netns. The plugin allocates an IP from its IPAM, creates a veth pair, places one end in the pod netns, attaches the host end to whatever dataplane it uses (Linux bridge, eBPF, BGP-routed interface), installs routes, and returns the assigned IP to the kubelet. On pod delete it gets `CNI_COMMAND=DEL`. Many CNIs (Calico, Cilium, Flannel, AWS VPC CNI, Azure CNI) also implement NetworkPolicy enforcement and observability.

**What it owns.** Pod IP allocation, pod-to-pod connectivity across the cluster, and (usually) NetworkPolicy. It does *not* own Service VIPs — that's kube-proxy or a kube-proxy-replacement.

**What it talks to.** *Up*: the kubelet (via the exec protocol). *Sideways*: its own control-plane (if any — Calico's `calico-node` DaemonSet talks to a `calico-typha` aggregator; Cilium's agent talks to its own operator). The CNI plugin's long-running daemon also watches the apiserver for NetworkPolicy, Service, EndpointSlice, and (for some CNIs) Node and Pod objects.

### 3.5 CSI Plugin — block and file storage (deep dive: ch 19)

**What it does.** Two-process architecture: the **controller plugin** (a Deployment, usually on the control plane) handles cluster-wide volume operations — create the cloud volume, attach it to a node, snapshot it, expand it. The **node plugin** (a DaemonSet) handles per-node operations — `NodeStageVolume` (format if needed, mount to a global staging path), `NodePublishVolume` (bind-mount into the pod). The kubelet talks to the node plugin over a Unix socket per driver under `/var/lib/kubelet/plugins/`. External "sidecar" containers (external-provisioner, external-attacher, external-resizer, external-snapshotter) bridge the CSI gRPC API to Kubernetes objects (PV, VolumeAttachment, VolumeSnapshot).

**What it owns.** The full volume lifecycle: provision → attach → stage → publish → unpublish → unstage → detach → delete. Snapshot, restore, expand, clone. Access modes (RWO, ROX, RWX, RWOP).

**What it talks to.** *Up*: the kubelet (Unix socket gRPC) and the apiserver (via sidecar containers in the controller-plugin Deployment). *Sideways*: the storage backend's API (AWS EBS, GCP PD, Ceph, NetApp, vSphere, …).

### 3.6 The Data Plane Stack, Visualized

```
┌────────────────────────────────────────────────────────────────────────┐
│                            WORKER NODE                                 │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  kubelet  (one per node, single source of truth on this node)    │  │
│  │  ────────────────────────────────────────────────────────────    │  │
│  │  syncLoop · PLEG · pod workers · probe mgr · status mgr          │  │
│  │  volume mgr · device mgr · CPU/memory/topology mgr · eviction    │  │
│  └────┬─────────────────┬───────────────────┬─────────────────────┘    │
│       │ CRI gRPC        │ CNI exec          │ CSI gRPC (Unix sock)     │
│       ▼                 ▼                   ▼                          │
│  ┌──────────┐    ┌──────────────┐    ┌────────────────┐               │
│  │container │    │ CNI plugin   │    │ CSI node       │               │
│  │ runtime  │    │ (Calico,     │    │  plugin        │               │
│  │ (cntrd / │    │  Cilium,     │    │  (DaemonSet,   │               │
│  │  CRI-O)  │    │  Flannel,    │    │   per driver)  │               │
│  │          │    │  VPC-CNI…)   │    │                │               │
│  └────┬─────┘    └──────────────┘    └────────────────┘               │
│       │ OCI runtime invocation                                         │
│       ▼                                                                │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  runc / kata / gvisor / youki                                    │  │
│  │  clone3() + unshare() + mount() + pivot_root() + execve()        │  │
│  └────┬─────────────────────────────────────────────────────────────┘  │
│       │                                                                │
│       ▼                                                                │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  LINUX KERNEL                                                    │  │
│  │  namespaces · cgroups v2 · netfilter / nftables · veth · eBPF    │  │
│  │  overlayfs · seccomp · capabilities · AppArmor / SELinux         │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  kube-proxy  (DaemonSet, programs Service VIPs)                  │  │
│  │   iptables / ipvs / nftables / replaced-by-eBPF                  │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

Notice what is *not* on the worker node: etcd, the apiserver, the scheduler, the controller manager. None of them. The only thing reaching back into the worker node from the control plane is the apiserver opening an HTTPS connection to the kubelet's `:10250` for exec/logs/port-forward. Everything else is the kubelet (and kube-proxy, and CNI/CSI daemons) initiating outbound watches to the apiserver.

---

## 4. The Communication Graph: Who Talks to Whom

This is the most important diagram in the chapter. Internalize it. Most "Kubernetes mystery failures" reduce to a violation of one of these arrows or a misunderstanding about who initiates which connection.

```
                            ┌─────────────┐
                            │    etcd     │
                            │  (3 or 5    │
                            │   member)   │
                            └──────▲──────┘
                                   │ gRPC (the only client)
                                   │
                            ┌──────┴───────┐
                            │ kube-apiserver│  (N replicas behind a TCP LB)
                            └──┬───────┬───┘
                ┌──────────────┘       └────────────────────┐
                │ HTTPS (server)                            │ HTTPS (initiated by apiserver,
                │ all clients open watch streams TO it      │  the ONE outbound exception:
                │                                            │  exec/logs/port-forward → kubelet)
                │                                            │
   ┌────────────┼─────────────────────────────┐              │
   │            │                             │              │
   ▼            ▼                             ▼              │
┌────────┐ ┌────────┐ ┌────────┐ ┌──────────┐ ┌────────┐    │
│scheduler│ │ctrl-mgr│ │ cloud- │ │  your    │ │kubelet │    │
│         │ │        │ │ctrl-mgr│ │ operator │ │ (each  │◄───┘
│ watches │ │ watches│ │ watches│ │          │ │ node)  │
│ Pods,   │ │ ~30    │ │Services│ │ watches  │ │        │
│ Nodes   │ │ types  │ │ Nodes  │ │ your CRD │ │ watches│
│         │ │        │ │        │ │          │ │ Pods   │
└─────────┘ └────────┘ └─────┬──┘ └──────────┘ │ bound  │
                             │                  │ to me  │
                             │ HTTPS            └───┬────┘
                             ▼                       │
                       ┌──────────┐                  │ Unix socket (CRI, CSI)
                       │ Cloud API│                  │ exec (CNI)
                       │ (AWS/GCP/│                  ▼
                       │  Azure)  │            ┌──────────┐
                       └──────────┘            │container │
                                               │ runtime  │
                                               │ + CNI    │
                                               │ + CSI    │
                                               └────┬─────┘
                                                    │ syscalls
                                                    ▼
                                              ┌──────────┐
                                              │  kernel  │
                                              └──────────┘
```

**The hard rules, listed out so you can quote them in incident reviews.**

1. **Only the apiserver talks to etcd.** No exceptions in mainline Kubernetes. Custom apiservers behind the aggregation layer can use *their own* storage; CRDs go through the main apiserver and thus through main etcd.
2. **Every other Kubernetes component talks only to the apiserver.** The scheduler does not call the kubelet. The Deployment controller does not call the ReplicaSet controller. They communicate by reading and writing the *same objects* and noticing each other's writes via watch.
3. **kubelet → apiserver is the only "node → control plane" direction.** The node initiates an outbound HTTPS connection (long-lived watch + periodic PATCH for pod status + Lease heartbeat). The control plane does not push to the node — even pod assignments are *pulled* by the kubelet's watch.
4. **apiserver → kubelet is the only "control plane → node" direction.** Used exclusively for: (a) `exec` / `attach` / `logs` / `port-forward` proxying (the apiserver upgrades the client's HTTPS to a SPDY/WebSocket stream and proxies to the kubelet's `:10250`), (b) the kubelet's `/metrics` endpoint scraping, (c) some health/probe endpoints. **This is the connection that breaks in network-partitioned clusters and explains why `kubectl logs` is the first thing to fail when nodes are NATed behind firewalls.** It's also why managed Kubernetes providers use a tunnel daemon (Konnectivity in upstream, `aws-eks-pod-identity-agent`-style sidecars in EKS, GKE's connect agent) to reverse the connection direction.
5. **Controllers do not talk to each other.** When the Deployment controller "creates a ReplicaSet" it actually issues a POST to the apiserver, the apiserver stores it in etcd, the watch event fires, and the ReplicaSet controller's informer wakes up. The Deployment controller never has any awareness of *which* ReplicaSet controller will process the event — there may be one, two, or zero (during a controller-manager outage). The system is fully asynchronous and message-passing through etcd.
6. **CNI / CSI / CRI talk only to the kubelet.** They are *local* services on the node. The kubelet is their only client. Their long-running daemons may independently watch the apiserver for their own object types (NetworkPolicy for CNI, VolumeAttachment for CSI), but their *configuration* and *invocation* always come through the kubelet via the standard interface.
7. **Cloud APIs are touched only by the cloud-controller-manager (and operators that intentionally do so).** Kubernetes core does not call AWS/GCP/Azure directly anymore — that lived in the in-tree cloud providers, which have all been deleted.

A consequence worth highlighting: **the apiserver is a SPOF for cluster *operations* but not for cluster *runtime*.** If every apiserver is down, you can't `kubectl apply`, the scheduler can't bind new pods, controllers can't reconcile, and the kubelet can't push status updates. But every pod that was already running keeps running. The kernel doesn't care that the API is down. This is why a 30-minute apiserver outage is survivable in production and a 30-minute etcd outage might mean data loss.

---

## 5. HA Topologies: Stacked vs External etcd

A single-node control plane is fine for `minikube`, `kind`, `k3d`, learning environments, and CI. Anything you don't want to recreate from scratch needs HA. There are two production-grade topologies.

### 5.1 Stacked etcd: etcd colocated with control-plane nodes

```
┌─────────────────────────────────────────────────────────────────┐
│                       CLIENT LOAD BALANCER                      │
│                  (cloud LB, HAProxy, or kube-vip)               │
└─────┬────────────────────────┬────────────────────────┬─────────┘
      │                        │                        │
      ▼                        ▼                        ▼
┌──────────────┐        ┌──────────────┐        ┌──────────────┐
│  CP NODE 1   │        │  CP NODE 2   │        │  CP NODE 3   │
│ ──────────── │        │ ──────────── │        │ ──────────── │
│ kube-apiserver        │ kube-apiserver        │ kube-apiserver
│ kube-scheduler        │ kube-scheduler        │ kube-scheduler
│ kube-cm               │ kube-cm               │ kube-cm
│ etcd member ←────────►│ etcd member ←────────►│ etcd member  │
│ (Raft)                │ (Raft)                │ (Raft)       │
└──────────────┘        └──────────────┘        └──────────────┘
```

**Used by:** `kubeadm` by default, most on-prem / smaller-cloud installs, EKS/GKE/AKS for *small* clusters (they don't expose this distinction to you).

**Pros:** Fewer machines. Simpler bootstrap. One IP range to firewall. Lower latency between apiserver and its local etcd (the apiserver prefers its localhost etcd if configured that way).

**Cons:** etcd shares CPU, RAM, disk, and network with apiserver, scheduler, and controller-manager. Under load, the apiserver can drown its colocated etcd by being too aggressive on watch cache rebuilds; an etcd compaction can starve scheduler latency. **Losing one CP node loses one etcd member**, so the blast radius of a single failure is doubled.

### 5.2 External etcd: separate etcd cluster

```
┌──────────────────────────────────────────────────────────┐
│                  CLIENT LOAD BALANCER                    │
└─────┬───────────────────┬────────────────────┬───────────┘
      │                   │                    │
      ▼                   ▼                    ▼
┌──────────┐        ┌──────────┐        ┌──────────┐
│ CP NODE  │        │ CP NODE  │        │ CP NODE  │
│ apiserver│        │ apiserver│        │ apiserver│
│ scheduler│        │ scheduler│        │ scheduler│
│ ctrl-mgr │        │ ctrl-mgr │        │ ctrl-mgr │
└─────┬────┘        └─────┬────┘        └─────┬────┘
      │                   │                    │
      └─────────┬─────────┴─────────┬──────────┘
                │                   │
                ▼                   ▼
       ┌────────────────────────────────────────┐
       │     EXTERNAL etcd CLUSTER              │
       │  ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌────┐    │
       │  │ m1 │ │ m2 │ │ m3 │ │ m4 │ │ m5 │    │  5-member Raft
       │  └────┘ └────┘ └────┘ └────┘ └────┘    │
       │  Dedicated NVMe, dedicated NICs        │
       └────────────────────────────────────────┘
```

**Used by:** GKE and EKS for large clusters (they manage etcd separately behind the scenes), every operator who has been bitten by stacked etcd performance, and any cluster above ~500 nodes.

**Pros:** etcd has dedicated hardware, dedicated NICs, dedicated NVMe — no interference. You can size etcd independently (e.g., 5 nodes with fast SSDs even though you only have 3 small apiservers). You can replace control-plane nodes without touching etcd, and vice versa. Easier to back up and restore.

**Cons:** More machines. More firewall rules (mTLS between apiserver and etcd over a separate network). Higher operational complexity. Bootstrap is more involved (the etcd cluster has to exist before kubeadm runs).

**The rule of thumb.** Below ~50 nodes, stacked etcd is fine. Between 50 and 500, either works; pick stacked for simplicity. Above 500, go external. Above 1500, *definitely* external with dedicated NVMe per etcd member.

### 5.3 The Load Balancer in Front of Apiservers

In both topologies, clients (kubectl, kubelets, controllers running outside the CP) connect through a load balancer. Options:

- **Cloud TCP LB** (AWS NLB, GCP TCP LB, Azure Standard LB): pass-through, preserves client IP if PROXY protocol is enabled, handles failover transparently.
- **HAProxy / Nginx-stream**: software TCP LB, often on a dedicated pair with VRRP.
- **kube-vip**: a clever VIP-floating daemon that runs as a static pod on the CP nodes themselves, eliminating the external LB requirement (popular for bare-metal).
- **DNS round-robin**: bad idea. Caches lie, failover is slow, and kubectl will pick a stale answer.

The LB must be L4 (TCP), not L7. The apiserver uses HTTP/2 streaming for watches; L7 LBs that buffer or split streams will break watches mysteriously.

---

## 6. Quorum, Write Latency, and Blast Radius: 3-node vs 5-node

etcd uses Raft. Every write requires a *quorum* (majority) of members to acknowledge before committing. With N members, quorum is ⌊N/2⌋ + 1, and the cluster tolerates ⌊(N-1)/2⌋ failures.

| Members | Quorum | Failures tolerated | Write fanout | Typical write latency |
|---------|--------|-------------------|--------------|----------------------|
| 1       | 1      | 0                 | 1            | ~5 ms (local NVMe)   |
| 3       | 2      | 1                 | 3            | ~10–15 ms            |
| 5       | 3      | 2                 | 5            | ~15–25 ms            |
| 7       | 4      | 3                 | 7            | ~25–40 ms            |
| 9       | 5      | 4                 | 9            | ~40–70 ms            |

**Even-numbered clusters are strictly worse than the next-smaller odd number.** A 4-member cluster still requires quorum=3, so it tolerates only 1 failure (same as a 3-member cluster) but pays more network and disk I/O for every write. **Always use 3, 5, or 7.**

The 3-vs-5 tradeoff:

- **3 nodes** is the sweet spot for clusters up to ~500 worker nodes. Survives one CP failure. Lowest write latency. Cheapest. The "default" for kubeadm and most managed offerings.
- **5 nodes** is the right answer for clusters above ~500 worker nodes, multi-AZ deployments where you want to survive losing an entire AZ (3 AZs × 2 members is wrong; 3 AZs × {2,2,1} or 5 single-AZ members spread across 3 AZs is right), and any cluster where the cost of a 30-minute outage is high. Tolerates 2 failures.
- **7 nodes** is rarely the right answer. The write fanout cost is high (every commit waits for 4 acks), the blast radius is no better than 5 in practice (you almost never lose 3 etcd members simultaneously unless your network has collapsed, in which case you're done anyway), and the operational complexity grows.

**Blast radius interpretation.** "Tolerates K failures" means K simultaneous member outages without losing quorum. It does *not* mean K consecutive outages — if member A dies, you replace it, and then B dies, you've still only experienced sequential single failures. The hard case is correlated failures: same AZ goes down, same rack loses power, same upgrade bricks all nodes. *That* is what you spread across AZs to defend against.

**Write latency dominates everything.** Every API write — every pod status update, every Lease renewal, every controller reconcile — pays the Raft commit cost. If your etcd nodes are on remote disks (EBS gp3 with high IOPS but ~1ms latency), you've added a millisecond to every write. Multiplied by tens of thousands of writes per second in a busy cluster, this is the difference between a healthy and a wedged control plane. **Use local NVMe.** EKS/GKE do this under the hood; on bare metal, *insist* on local NVMe with a dedicated disk for etcd.

---

## 7. Leader Election via Lease Objects

The scheduler and controller-manager are *replicated for HA* but *active-passive* — only one replica processes events at a time, to avoid two schedulers binding the same pod to different nodes. The mechanism is **leader election via Lease objects**.

A `coordination.k8s.io/v1` Lease looks like this:

```yaml
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata:
  name: kube-scheduler
  namespace: kube-system
spec:
  holderIdentity: "scheduler-7d8c9-abc12_a1b2c3d4-..."
  leaseDurationSeconds: 15
  acquireTime: "2026-05-23T12:00:00Z"
  renewTime: "2026-05-23T12:00:14Z"
  leaseTransitions: 42
```

The algorithm (implemented in `k8s.io/client-go/tools/leaderelection`):

1. On startup, every replica tries to `UPDATE` the Lease with itself as the `holderIdentity`, using optimistic concurrency on `resourceVersion`.
2. The first to succeed becomes leader. Everyone else gives up and enters a *waiting* state.
3. The leader renews the Lease every `RenewDeadline` seconds (default ~10s) by PATCHing `renewTime`.
4. Followers poll the Lease (via watch) every `RetryPeriod` (default ~2s) and check if `renewTime + LeaseDuration > now`. If not — the leader has died or is partitioned — they race to take over.
5. The dethroned leader, if it ever comes back, sees a different `holderIdentity` and gracefully shuts down its controllers.

**Why a Lease object instead of an external service like ZooKeeper or Consul?** Because etcd is *already* a consensus store, and the apiserver already speaks to it. Adding ZooKeeper would mean a second source of truth — exactly what Kubernetes is designed to avoid. The Lease object pattern reuses the existing infrastructure: every component already has an apiserver client and the apiserver already does CAS on `resourceVersion`. Leader election becomes a trivial application of optimistic concurrency.

Components that use Lease-based leader election: `kube-scheduler`, `kube-controller-manager`, `cloud-controller-manager`, most operators built with controller-runtime (it's the default), the cluster autoscaler, Karpenter's controller, and many CSI controller plugins.

Components that do *not* use leader election: kubelet (one per node, no contention), kube-proxy (one per node, each programs its own dataplane), CoreDNS (every replica answers identical queries), Ingress controllers (each instance independently watches and configures its own dataplane).

The Lease is also used for **node heartbeats** — `node-lease` namespace contains one Lease per node, renewed by the kubelet every 10 seconds. This replaced the old "PATCH the Node status every 10 seconds" approach because Leases are far cheaper to update (smaller object, no status subresource).

---

## 8. Sizing Guidance: Small, Medium, Large, XL

There is no universal "right size" for a Kubernetes control plane; it depends on workload churn, watch fan-out, and the specific resource types in heavy use (e.g., a cluster with 100k Secrets is differently loaded than one with 100k Pods). But there are widely-used checkpoints.

| Scale | Nodes | Pods | API Reqs/sec | Control Plane Sizing | Etcd Sizing |
|-------|-------|------|--------------|---------------------|-------------|
| **Small** | 1–10 | up to ~500 | <100 | 1 CP node, 2 vCPU / 4 GiB. Single etcd OK. | Stacked, 1 member. |
| **Medium** | 10–100 | up to ~5k | 100–500 | 3 CP nodes, 4 vCPU / 16 GiB each. | Stacked, 3 members. |
| **Large** | 100–1000 | up to ~30k | 500–3000 | 3–5 CP nodes, 8–16 vCPU / 32–64 GiB. Apiserver behind LB. | Stacked OK; external preferred. 3 members on local NVMe. |
| **XL** | 1000–5000 | up to ~150k | 3000–10000 | 5+ CP nodes, 16–32 vCPU / 64–128 GiB. Multiple apiservers behind L4 LB. Separate read-only apiserver pool optional. | External, 5 members, dedicated NVMe, dedicated NICs. |
| **Hyperscale** | 5000–15000+ | up to ~500k | 10k–100k | Custom. Sharded by tenant or by namespace. Specialized APF tuning. Etcd compaction tuned aggressively. | External, 5–7 members, NVMe RAID, often shared etcd or per-resource etcds. |

**What "Nodes" really means.** Most Kubernetes installs are bounded not by nodes but by total objects, watch fan-out, and event churn. A 100-node cluster with 50k CronJobs creates more apiserver load than a 5000-node cluster with 10k long-running Pods. Use these tiers as a starting point and measure.

### 8.1 The "kubelet is also a watcher" multiplier

Every node runs a kubelet that opens an apiserver watch on Pods (filtered to its own nodename), Secrets and ConfigMaps mounted in any pod on the node, Nodes (for its own status writes), and a few others. The apiserver maintains an in-memory **watch cache** that broadcasts events. With 5000 nodes, you have ≥5000 long-lived watch connections, each receiving every event for every Pod scheduled on its node, plus all Secret/ConfigMap changes mounted on it, plus its own Node updates. *The apiserver memory and CPU for watch fan-out scales linearly with the number of nodes.* This is why "5000 nodes" needs apiservers with 32+ vCPU and 128 GiB of RAM — the watch cache alone can consume 20+ GiB.

### 8.2 What you actually buy when you scale up

```
Resource axis:           Bottleneck order as you scale (roughly):
─────────────────         ──────────────────────────────────────────
small ⇒ medium            1. etcd disk IOPS (small NVMe is fine)
medium ⇒ large            2. apiserver CPU (watch fan-out CPU dominates)
large ⇒ XL                3. scheduler throughput (~100–300 binds/sec ceiling)
XL ⇒ hyperscale           4. kube-proxy iptables rules (O(N) match per packet)
hyperscale ⇒ ???          5. etcd Raft commit latency (you've run out of physics)
```

---

## 9. The Scaling SIG SLOs and What Breaks First

The Kubernetes Scalability SIG publishes a set of SLOs that the upstream community guarantees up to **5000 nodes / 150,000 pods / 300,000 containers** per cluster. The most-cited:

1. **API call latency (read, single object)**: p99 ≤ 1s.
2. **API call latency (list, namespaced)**: p99 ≤ 30s (yes, 30s; lists are expensive and Kubernetes wants you to use watch).
3. **Pod startup time (scheduling + image pull + container start, image already cached)**: p99 ≤ 5s.
4. **Pod scheduling latency (apiserver-create → scheduler-bind)**: p99 ≤ 1s.
5. **In-cluster DNS programming latency (Service create → resolvable everywhere)**: p99 ≤ 5s.

If your cluster violates these and you're under the 5k/150k/300k thresholds, something is misconfigured. If you're *above* those thresholds, you're off-the-map and need to shard, tune, or accept worse SLOs.

### 9.1 What breaks first, in order

1. **etcd disk I/O** is the *first* limit you hit and the one that produces the worst symptoms. Every write goes through Raft → fsync. If your etcd is on EBS gp2 (or worse, gp3 without provisioned IOPS), you'll see write latency spikes that turn into apiserver request queueing, which turns into watch cache backpressure, which turns into pods stuck in `ContainerCreating`. Symptoms: `etcdserver: request timed out`, `apply entries took too long`. Fix: local NVMe, defrag schedule, snapshot retention tuning. (Ch 04, ch 35.)
2. **Apiserver CPU**, specifically the JSON/protobuf serialization of watch events and the watch cache event broadcasting. Under-provisioned apiservers manifest as `429 Too Many Requests` (APF rejecting), bursts of `context deadline exceeded` in client logs, and informer relists (which create thundering herd loads). Fix: scale apiserver replicas horizontally, increase `--max-requests-inflight`, tune APF flowschemas, use protobuf clients. (Ch 05, ch 35.)
3. **Scheduler throughput** ceilings at roughly 100–300 binds/sec for the default scheduler. If you create 10000 pods at once (e.g., scale a Deployment from 0 to 10000), you'll see a multi-minute backlog in `kube-scheduler`. Fix: use scheduling profiles to enable parallel binding, run multiple schedulers with non-overlapping pod selectors, or for batch workloads use Volcano/Yunikorn. (Ch 09, ch 34.)
4. **kube-proxy iptables rules**: each Service adds rules per endpoint per node, and packet matching is O(N) in iptables mode. At ~5k Services or ~50k endpoints, kube-proxy reconcile cycles take seconds and SYN packets get noticeable latency. Symptoms: connection establishment time grows with cluster size; `kube-proxy` CPU pinned. Fix: switch to IPVS, nftables, or replace with eBPF (Cilium). (Ch 14, ch 16.)
5. **Etcd Raft commit latency** at the very top end. If your inter-node ping is 1ms and you're committing 10k writes/sec, you're paying 10s of latency per write just on the network. Geographically distributed etcd is an antipattern. Keep etcd in a single AZ (use cluster-level HA, not etcd-level cross-region replication, to handle AZ failure).

---

## 10. The Everything-Is-an-API-Object Axiom

Kubernetes commits, deeply and without exception, to representing every piece of cluster state as an API object. This is not a stylistic choice — it is the foundation that makes the watch-everything pattern work. *Everything* is an object:

- **Workloads.** Pods, Deployments, StatefulSets, DaemonSets, Jobs, CronJobs, ReplicaSets.
- **Networking.** Services, Endpoints, EndpointSlices, NetworkPolicies, Ingresses, Gateways, HTTPRoutes.
- **Storage.** PersistentVolumes, PersistentVolumeClaims, StorageClasses, VolumeAttachments, VolumeSnapshots.
- **Config and secrets.** ConfigMaps, Secrets.
- **Identity.** ServiceAccounts, Roles, RoleBindings, ClusterRoles, ClusterRoleBindings.
- **Cluster topology.** Nodes (yes, a Node is an API object!), Namespaces, PriorityClasses, RuntimeClasses.
- **Internal plumbing.** Leases (used for leader election and node heartbeats), Events (kubectl describe shows these), CertificateSigningRequests, APIServices, MutatingWebhookConfigurations, ValidatingWebhookConfigurations, ValidatingAdmissionPolicies.
- **Custom anything.** CustomResourceDefinitions — themselves objects! — register new object types.

**A Node is an object.** Read that twice. When you spin up a new worker, the kubelet on it *creates a Node object* via the apiserver (subject to the Node authorizer's restrictions). When you `kubectl delete node`, you're deleting that object — and the kubelet on the actual machine doesn't know or care; it'll just re-register on its next heartbeat unless you've also turned the machine off. The Node object is the cluster's *view* of the node, not the node itself.

**An Event is an object.** Every "Scaled deployment from 1 to 3 replicas", every "FailedScheduling: 0/3 nodes available", every "BackOff: image pull error" is a real object in etcd with a TTL (default 1 hour). When you `kubectl describe pod`, the events you see are the result of a list-by-involvedObject query against the apiserver. This is why a misbehaving controller emitting events in a tight loop can fill etcd in minutes — the same etcd that stores your actual workloads.

**A Lease is an object.** Used both for leader election (`kube-system/kube-scheduler`) and for node heartbeats (`kube-node-lease/<node-name>`). Replacing the old "kubelet PATCHes Node every 10s" with a tiny Lease object cut control-plane write volume by an order of magnitude in large clusters.

**This consistency is load-bearing.** Because *everything* is an object, the same six verbs (get, list, watch, create, update, patch, delete) apply to *everything*. The same RBAC system gates *everything*. The same admission chain validates *everything*. The same audit log records *everything*. The same `kubectl explain` documents *everything*. The same `client-go` library reads and writes *everything*. There are no privileged side channels.

When you write an operator and create a CustomResourceDefinition, your custom type immediately gets watch, RBAC, admission, audit, OpenAPI, and `kubectl explain`, for free. That free integration is the entire reason Kubernetes won the orchestrator wars.

---

## 11. GVR, Kind, and the kubectl Discovery Workflow

Every API object is identified by a triple: **Group / Version / Resource** (GVR). Plus a fourth thing, **Kind**, which is the Go struct name. They're not the same.

- **Group**: the API group, like `apps`, `batch`, `networking.k8s.io`, `cilium.io`. The empty group `""` is the legacy "core" group containing Pod, Service, ConfigMap, Secret, Node, Namespace.
- **Version**: like `v1`, `v1beta1`, `v1alpha2`. Multiple versions can coexist; one is the "storage version" (what's actually persisted in etcd) and the others are converted on the fly via either built-in conversion or a conversion webhook.
- **Resource**: the URL-friendly plural name, like `pods`, `deployments`, `replicasets`. This is what you put in URLs: `/apis/apps/v1/namespaces/default/deployments`.
- **Kind**: the Go struct name (and the YAML `kind:` field), like `Pod`, `Deployment`, `ReplicaSet`. Capitalized, singular.

So a Deployment is:
- GVR: `apps/v1/deployments`
- GVK: `apps/v1/Deployment`
- URL: `/apis/apps/v1/namespaces/<ns>/deployments/<name>`

And a core Pod is:
- GVR: `/v1/pods` (the empty group; sometimes written `core/v1/pods`)
- GVK: `/v1/Pod`
- URL: `/api/v1/namespaces/<ns>/pods/<name>` — note `/api` not `/apis` for the legacy core group!

The URL difference for the core group is one of those historical warts that never gets fixed. You'll see it every time you read raw kubectl logs.

### 11.1 Namespaced vs Cluster-scoped

- **Namespaced** resources live inside a Namespace and have URLs like `/apis/<group>/<version>/namespaces/<ns>/<resource>/<name>`. Pods, Deployments, Services, ConfigMaps, Secrets, RoleBindings, PVCs.
- **Cluster-scoped** resources do not have a namespace and have URLs like `/apis/<group>/<version>/<resource>/<name>`. Nodes, Namespaces (themselves), PersistentVolumes, StorageClasses, ClusterRoles, ClusterRoleBindings, CRDs, APIServices, MutatingWebhookConfigurations, PriorityClasses.

CRDs declare their own scope at creation time (`spec.scope: Namespaced` or `Cluster`). Choosing wrong is a permanent decision — you can't change scope without deleting and recreating the CRD (and migrating data).

### 11.2 The kubectl Discovery Workflow

When you type `kubectl get deploy`, kubectl doesn't know what "deploy" is. The workflow:

```
$ kubectl get deploy nginx
   │
   ▼ first, kubectl needs to know: what GVR is "deploy"?
   │
   ▼ kubectl hits  GET /api  and  GET /apis  (discovery endpoints)
   │   returns the full list of API groups and resources, including aliases
   │   ("deploy" → "deployments", "po" → "pods", "svc" → "services")
   │
   ▼ kubectl resolves "deploy" → apps/v1/deployments
   │
   ▼ kubectl hits  GET /openapi/v3/apis/apps/v1
   │   to fetch the OpenAPI schema for column rendering and dry-run validation
   │
   ▼ kubectl hits  GET /apis/apps/v1/namespaces/default/deployments/nginx
   │
   ▼ apiserver returns the object; kubectl renders it
```

Discovery is cached locally in `~/.kube/cache/discovery/`. This is why your first `kubectl get` after talking to a new cluster is slow and subsequent calls are fast. It's also why CRD installations can take a few seconds to "appear" in kubectl — you need to refresh discovery (`kubectl api-resources` does this).

```bash
$ kubectl api-resources --verbs=list --namespaced -o name   # enumerate everything
$ kubectl explain deployment.spec.template.spec.containers   # OpenAPI in your terminal
$ kubectl explain deployment --recursive | less              # the whole schema
$ kubectl explain $(kubectl api-resources -o name | head)    # explain works on CRDs too
```

`kubectl explain` is one of the most underused tools in the cluster. It pulls the OpenAPI schema directly from the apiserver, so it's always correct for the running version (no version drift between docs and reality). Use it instead of grepping examples.

---

## 12. The Watch-Everything Principle

Every component in Kubernetes uses **informers** to maintain a local cache of the objects it cares about, kept fresh by a streaming **watch** connection to the apiserver. **Nothing polls.** This single architectural decision dictates how the apiserver and etcd are built.

```
Component startup:
  1. LIST all objects of the watched type (one snapshot at a time T0,
     returns resourceVersion R0)
  2. Populate local cache (the "indexer")
  3. WATCH from resourceVersion=R0
     → apiserver streams ADD/MODIFY/DELETE events for every change
     → events arrive as soon as etcd commits them
  4. Local cache is now always within milliseconds of authoritative state
  5. Reconcile loop reads from the cache, not the apiserver
     → zero apiserver load for reads
     → reconcile latency is dominated by Go code, not network
```

The apiserver's watch implementation:

```
  apiserver process
  ┌────────────────────────────────────────────────────────────────┐
  │  Watch Cache (in-memory ring buffer per resource type)         │
  │  ┌─────────────────────────────────────────────────────────┐   │
  │  │ [R0 ADD pod-a] [R1 MOD pod-a] [R2 ADD pod-b] ...        │   │
  │  │  ring buffer holds last N events (default ~1000)        │   │
  │  └─────────────────────────────────────────────────────────┘   │
  │      ▲                                                          │
  │      │ events appended as etcd watch fires                      │
  │      │                                                          │
  │  ┌────────────────────────────────────────────────────────────┐ │
  │  │  etcd watch (single, multiplexed gRPC stream)              │ │
  │  └────────────────────────────────────────────────────────────┘ │
  │      │                                                          │
  │      │ events fanned out to N clients in parallel               │
  │      ▼                                                          │
  │  ┌────────────────────────────────────────────────────────────┐ │
  │  │  Watcher 1  Watcher 2  Watcher 3  ...  Watcher N           │ │
  │  │  (each filtered by namespace/labelSelector/fieldSelector)  │ │
  │  └────────────────────────────────────────────────────────────┘ │
  └────────────────────────────────────────────────────────────────┘
```

Key properties:

- **Single etcd watch per resource type, multiplexed to N clients.** The apiserver doesn't open one etcd watch per client; it opens *one* and fans the events out. This is what makes 5000 kubelets watching Pods feasible.
- **Ring buffer for replay.** If a watcher reconnects with `resourceVersion=R`, the apiserver can replay events from R if R is still in the ring buffer. If not, the client gets `410 Gone` and must do a full relist — the "thundering herd" risk that operators learn to dread.
- **Filtering at the apiserver.** Clients can subscribe with a `labelSelector` or `fieldSelector` (most importantly `spec.nodeName=<me>` for kubelets, so a node only receives events for its own pods). Filtering happens server-side, but events still go through the full deserialize/match path — heavy use of label selectors with high churn can spike apiserver CPU.
- **Watch bookmarks.** The apiserver periodically sends a `BOOKMARK` event with the latest resourceVersion, so clients can advance their cursor without an actual data change. This makes reconnects cheaper.

The consequence: **the apiserver's primary CPU cost is encoding events for watch fan-out, not handling individual GETs.** A scaled-up apiserver fleet doesn't necessarily mean a scaled-up etcd — most of the load is read fan-out, not write throughput. (Ch 04 and ch 05 go very deep on this.)

This architecture is why you should *never* write a controller that polls the apiserver. Use an informer. Calling `apiserver.Get()` inside a tight loop is one of the single most common ways to destabilize a large cluster.

---

## 13. The kubernetes/kubernetes Source Tree Map

The Kubernetes monorepo lives at `https://github.com/kubernetes/kubernetes`. It is enormous (~3M lines of Go) but very organized. Knowing where to look is half the battle when you want to read the real code.

```
kubernetes/
├── cmd/                              # one directory per binary
│   ├── kube-apiserver/               # the apiserver entrypoint (main.go)
│   ├── kube-scheduler/
│   ├── kube-controller-manager/
│   ├── cloud-controller-manager/
│   ├── kubelet/
│   ├── kube-proxy/
│   ├── kubectl/                      # well, the entrypoint; bulk of kubectl is in staging
│   ├── kubeadm/                      # cluster bootstrapper
│   └── kubemark/                     # fake kubelets for scale testing
│
├── pkg/                              # internal-to-k8s implementation packages
│   ├── controller/                   # built-in controllers (deployment, replicaset,
│   │                                   nodelifecycle, endpoint, gc, ttl, …)
│   ├── kubelet/                      # syncLoop, PLEG, podworkers, prober, status,
│   │                                   volume, eviction, cm (container manager),
│   │                                   network/dns, cri/, qos/
│   ├── scheduler/                    # framework, plugins, extender, queue
│   ├── proxy/                        # iptables, ipvs, nftables modes
│   ├── apis/                         # internal API types (different from staging
│   │                                   versioned types)
│   ├── registry/                     # apiserver storage layer per resource type
│   ├── volume/                       # legacy in-tree volume plugins (deprecated)
│   └── ...
│
├── staging/src/k8s.io/               # *published* libraries — staging is mirrored
│   │                                   to k8s.io/* repos on every commit, so external
│   │                                   consumers import them
│   ├── api/                          # versioned API types (apps/v1, batch/v1, …)
│   │                                   the canonical struct definitions; what kubectl
│   │                                   and every controller imports
│   ├── apimachinery/                 # runtime, schema, conversion, meta, GVK/GVR
│   │                                   helpers; the type system underneath the APIs
│   ├── client-go/                    # the client library: informers, listers,
│   │                                   workqueue, leader-election, RESTClient,
│   │                                   kubernetes/ (typed clientset),
│   │                                   discovery/, dynamic/, tools/
│   ├── apiserver/                    # generic apiserver framework — what kube-apiserver
│   │                                   is built on top of, and what aggregated
│   │                                   apiservers reuse (APF, admission, audit, storage)
│   ├── apiextensions-apiserver/      # the CRD apiserver, mounted as an aggregation
│   ├── kube-aggregator/              # the aggregation layer that fronts everything
│   ├── kube-openapi/                 # OpenAPI v3 plumbing
│   ├── component-base/               # shared bootstrap (logging, metrics, version,
│   │                                   featuregate, leaderelection wrappers)
│   ├── code-generator/               # the magic that generates clientset, informers,
│   │                                   listers, conversion, deepcopy from API types
│   └── kubectl/                      # the actual kubectl implementation
│
├── vendor/                           # vendored deps (etcd client, gRPC, prometheus,
│                                       cobra, etc.)
├── test/                             # e2e, integration, scalability
├── hack/                             # build, lint, codegen scripts
└── plugin/                           # admission plugin implementations
                                        (PodSecurity, ResourceQuota, NodeRestriction,
                                         DefaultTolerationSeconds, …)
```

### 13.1 Where to look for what

| You want to read... | Look in... |
|---------------------|------------|
| How the apiserver dispatches a request | `staging/src/k8s.io/apiserver/pkg/server/handler.go`, `pkg/genericapiserver` |
| The admission chain | `staging/src/k8s.io/apiserver/pkg/admission/` and `plugin/pkg/admission/*` |
| The watch cache | `staging/src/k8s.io/apiserver/pkg/storage/cacher/` |
| How etcd is wrapped | `staging/src/k8s.io/apiserver/pkg/storage/etcd3/` |
| Server-side apply | `staging/src/k8s.io/apimachinery/pkg/util/managedfields/` and `apiserver/pkg/endpoints/handlers/fieldmanager/` |
| The scheduler framework | `pkg/scheduler/framework/` and `pkg/scheduler/framework/plugins/` |
| The kubelet's main loop | `pkg/kubelet/kubelet.go` (look for `syncLoop`) |
| PLEG | `pkg/kubelet/pleg/` |
| The CRI client wrapper | `pkg/kubelet/cri/remote/` |
| The deployment controller | `pkg/controller/deployment/` |
| The garbage collector | `pkg/controller/garbagecollector/` |
| The leader election library | `staging/src/k8s.io/client-go/tools/leaderelection/` |
| Informer/Reflector/Indexer | `staging/src/k8s.io/client-go/tools/cache/` |
| Workqueue | `staging/src/k8s.io/client-go/util/workqueue/` |
| The Pod API type (the struct) | `staging/src/k8s.io/api/core/v1/types.go` |
| Defaulting and conversion code | generated into `zz_generated_*.go` files next to the type |
| RBAC enforcement | `plugin/pkg/auth/authorizer/rbac/` |
| kube-proxy iptables mode | `pkg/proxy/iptables/proxier.go` |
| kube-proxy IPVS mode | `pkg/proxy/ipvs/proxier.go` |
| The CRD apiserver | `staging/src/k8s.io/apiextensions-apiserver/` |

The `staging/` directory is special: it contains code that *will* be published as standalone Go modules (`k8s.io/api`, `k8s.io/client-go`, etc.) on every commit. When you `import "k8s.io/client-go/tools/cache"`, you're actually importing code that lives in `kubernetes/kubernetes/staging/src/k8s.io/client-go/tools/cache/`, mirrored out. This is why bumping `k8s.io/client-go` to a new version is exactly the same as upgrading Kubernetes — they're the same code.

### 13.2 Related repos you'll bounce to

- `kubernetes/enhancements` — KEPs (Kubernetes Enhancement Proposals). When you want to know *why* something was designed a certain way, find the KEP.
- `kubernetes/community` — SIG charters, working group docs, the contributing guide.
- `kubernetes-sigs/controller-runtime` — the controller framework that operators are built on (used by every kubebuilder/operator-sdk project).
- `kubernetes-sigs/cluster-api` — declarative cluster lifecycle.
- `etcd-io/etcd` — the etcd repo itself.
- `containerd/containerd`, `cri-o/cri-o` — the container runtimes.

---

## 14. Reference Architecture: A 5000-Node Cluster

This is the design you'd write if someone gave you a credit card and said "build me a single Kubernetes cluster that can run 5000 nodes, 150k pods, and survive a single-AZ outage". It bumps against the Scalability SIG's upper SLO ceiling.

```
                            ┌────────────────────────────────────┐
                            │   Cloud L4 Network Load Balancer   │
                            │   (multi-AZ, preserves client IP)  │
                            │   target: kube-apiserver:6443      │
                            └─────────────┬──────────────────────┘
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              │                           │                           │
              ▼                           ▼                           ▼
  ┌───────────────────────┐   ┌───────────────────────┐   ┌───────────────────────┐
  │ CP-NODE-1 (AZ-A)      │   │ CP-NODE-2 (AZ-B)      │   │ CP-NODE-3 (AZ-C)      │
  │  32 vCPU / 128 GiB    │   │  32 vCPU / 128 GiB    │   │  32 vCPU / 128 GiB    │
  │  Local NVMe (audit)   │   │  Local NVMe (audit)   │   │  Local NVMe (audit)   │
  │ ─────────────────     │   │ ─────────────────     │   │ ─────────────────     │
  │  kube-apiserver       │   │  kube-apiserver       │   │  kube-apiserver       │
  │  kube-scheduler       │   │  kube-scheduler       │   │  kube-scheduler       │
  │  kube-controller-mgr  │   │  kube-controller-mgr  │   │  kube-controller-mgr  │
  │  cloud-controller-mgr │   │  cloud-controller-mgr │   │  cloud-controller-mgr │
  └──────────┬────────────┘   └──────────┬────────────┘   └──────────┬────────────┘
             │                           │                           │
             └─────────────────┬─────────┴────────┬──────────────────┘
                               │                  │
                               ▼                  ▼
                  ┌────────────────────────────────────────────┐
                  │  EXTERNAL ETCD CLUSTER (5 members)         │
                  │  ┌────┐ AZ-A   ┌────┐ AZ-A                 │
                  │  │ e1 │        │ e2 │                      │
                  │  └────┘        └────┘                      │
                  │  ┌────┐ AZ-B   ┌────┐ AZ-B                 │
                  │  │ e3 │        │ e4 │                      │
                  │  └────┘        └────┘                      │
                  │                ┌────┐ AZ-C                 │
                  │                │ e5 │                      │
                  │                └────┘                      │
                  │  Each: 16 vCPU / 64 GiB, dedicated         │
                  │  local NVMe (1+ TiB), dedicated NIC,       │
                  │  no other workloads                        │
                  └────────────────────────────────────────────┘

           ┌──────────────────────────────────────────────────────────────┐
           │                                                              │
           ▼                                                              ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │  WORKER NODES (5000)                                                   │
  │                                                                        │
  │  ┌────────────────────────────────────────────────────────────────┐    │
  │  │  Each worker:                                                  │    │
  │  │   kubelet (cgroups v2, systemd cgroup driver)                  │    │
  │  │   containerd (CRI shim, runc OCI runtime)                      │    │
  │  │   Cilium (CNI + kube-proxy replacement, eBPF dataplane)        │    │
  │  │     - host routing, no overlay (BGP to ToR if bare-metal,      │    │
  │  │       VPC-native if cloud)                                     │    │
  │  │     - NetworkPolicy, L7 policy, mTLS via Cilium                │    │
  │  │     - Hubble for flow observability                            │    │
  │  │   AWS EBS CSI (or equivalent) for block storage                │    │
  │  │   No kube-proxy DaemonSet (replaced by Cilium)                 │    │
  │  └────────────────────────────────────────────────────────────────┘    │
  │                                                                        │
  └────────────────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────────────┐
  │  AUDIT LOG SINK (separate, write-once)                               │
  │   apiserver --audit-webhook-config → fluent-bit → S3 + SIEM          │
  │   never colocated with cluster state; outlives the cluster           │
  └──────────────────────────────────────────────────────────────────────┘
```

### 14.1 Why each choice

**5 control-plane nodes, not 3.** Three CP nodes can survive one failure. Five can survive two — and during a rolling upgrade, you're already *down one* by design. Five lets you cordon-and-upgrade without going below 3 healthy. Each is 32 vCPU / 128 GiB because watch fan-out to 5000 kubelets needs CPU and the watch cache alone consumes tens of GiB.

**External 5-member etcd on dedicated hardware.** At 5000 nodes you're firmly in territory where stacked etcd will lose to noisy-neighbor effects. Dedicated NVMe is non-negotiable; etcd's Raft commit is fsync-bound. Spread 5 members across 3 AZs (2-2-1) so you survive the loss of an entire AZ without losing quorum.

**Local NVMe on CP nodes for audit logs.** Audit log write volume scales with API request rate. Writing to network storage adds latency to every audited request (most of them, in a security-conscious cluster). Local NVMe absorbs the spike; ship asynchronously to S3.

**Multiple apiservers behind a cloud L4 LB.** L4 (TCP) only — L7 LBs break HTTP/2 watch streams. Use the cloud's NLB to get cross-AZ failover and source-IP preservation. Each apiserver runs full-throated; no sharding.

**Cilium for CNI + kube-proxy replacement.** At 5000 nodes and likely 10k+ Services, iptables kube-proxy is dead in the water (reconcile takes seconds, packet latency suffers). Cilium's eBPF socket-LB bypasses iptables entirely; lookup cost is O(1), not O(N). It also gives you L7 NetworkPolicy, mTLS (via WireGuard or IPsec), and observability via Hubble. Same agent handles CNI and proxy.

**Separate audit log sink.** If the cluster is compromised, the audit log is your forensic record. It cannot live *only* on the cluster it audits. Ship every audit event to S3 + SIEM in a different account/project with strict IAM.

**What's not shown but you also need:** an Ingress / Gateway tier (Envoy-based, separate node pool), a metrics stack (Prometheus / Thanos / VictoriaMetrics), a logging stack (Loki / Vector / OpenSearch), GitOps (ArgoCD), policy (Kyverno or Gatekeeper), and a backup/restore tool (Velero with CSI snapshots). All of those are *also* just controllers watching apiserver objects.

---

## 15. What Kubernetes Is NOT

Kubernetes is a control plane for declarative resource management. It is *not* the things people sometimes hope it is, and assuming it is leads to disappointment and bad architecture.

**Kubernetes is NOT a PaaS.** It doesn't build your code, manage your application configuration lifecycle, render dashboards for non-experts, or hide infrastructure. It is *substrate* for a PaaS. Heroku is a PaaS; OpenShift is a PaaS built on Kubernetes; CloudFoundry-on-K8s is a PaaS. Kubernetes is the foundation.

**Kubernetes is NOT a CI/CD system.** It can *run* CI jobs (Tekton, Argo Workflows, Jenkins-on-K8s) but it doesn't define a pipeline language or trigger on git pushes. The watch loop deploys *what is in the cluster's desired state* — getting that desired state into the cluster (compile, test, build image, push, update manifests) is a CI/CD pipeline's job.

**Kubernetes is NOT a service mesh.** Pods get IPs; Services get VIPs; that's it. mTLS, retries, circuit breakers, traffic splitting, observability — all of that is a service mesh's job (Istio, Linkerd, Cilium service mesh, Consul Connect). Kubernetes provides the *substrate* (Services, EndpointSlices, sidecars or eBPF) that meshes plug into.

**Kubernetes is NOT a secrets manager.** Secrets are base64-encoded (not encrypted) by default. They're stored in etcd alongside everything else. Encryption at rest is opt-in (`--encryption-provider-config`) and is just at the apiserver/etcd boundary — not key-managed by HSM, not rotated by Kubernetes, not auditable per-access. Production secrets management uses Vault, AWS Secrets Manager, GCP Secret Manager, Azure Key Vault, or SOPS, surfaced into pods via CSI Secrets Store or operator-managed Secrets.

**But Kubernetes provides the extension points to be all of these.** The genius of the design is that you can build any of the above *on top of* Kubernetes by adding controllers, CRDs, admission webhooks, and aggregated APIs. (Ch 23 covers CRDs and operators, ch 24 covers aggregated APIs, ch 06 covers admission webhooks.)

```
              ┌──────────────────────────────────────────────┐
              │            Kubernetes Core                   │
              │  (apiserver + etcd + scheduler + controllers │
              │   + kubelet + kube-proxy + extension points) │
              └──────────────────────────────────────────────┘
                              ▲
                              │ extension points
                              │
   ┌──────────┬──────────┬────┴─────┬──────────┬───────────┐
   │          │          │          │          │           │
   ▼          ▼          ▼          ▼          ▼           ▼
┌────────┐ ┌──────┐ ┌──────────┐ ┌─────────┐ ┌──────┐ ┌─────────┐
│ CRDs   │ │ Adm. │ │ Aggreg.  │ │ Operator│ │ CSI/ │ │ Custom  │
│ + ops  │ │ web- │ │ apiserver│ │ pattern │ │ CNI/ │ │ sched-  │
│        │ │ hooks│ │          │ │         │ │ CRI  │ │ ulers   │
└───┬────┘ └──┬───┘ └────┬─────┘ └────┬────┘ └──┬───┘ └────┬────┘
    │         │          │            │         │           │
    ▼         ▼          ▼            ▼         ▼           ▼
 PaaS    Policy/      metrics-     Postgres   Storage    Batch/
 (e.g.   security    server,       operator,  vendors,   gang
 OpenShift)(Kyverno,  custom-      Istio,     custom     scheduling
          OPA)       metrics       Cilium,    NICs       (Volcano,
                                   ArgoCD,                Yunikorn)
                                   Vault op.
```

Every "Kubernetes is not X" complaint becomes "Kubernetes provides the extension point to build X." The CNCF landscape is the visible result.

---

## 16. A First End-to-End Trace

The end-to-end trace from `kubectl run` to a running container, in just enough detail to connect every component you've now met. Ch 08 in the ROADMAP has the long version; this is the glue.

```
$ kubectl run nginx --image=nginx

T+0    [kubectl on your laptop]
       Discovery: GET /api → "pods" lives in core/v1
       Build Pod object: {kind: Pod, name: nginx, image: nginx}
       POST /api/v1/namespaces/default/pods

T+10ms [apiserver — ch 05]
       TLS handshake done. AuthN: client cert → user=alice. AuthZ: RBAC
       permits POST on pods in default. Mutating admission: defaulters
       fill in serviceAccountName=default, restartPolicy=Always, dnsPolicy
       =ClusterFirst, etc. ServiceAccount admission injects the projected
       token volume + mount. Validation passes (image string non-empty,
       container has a name). Validating admission: PodSecurity baseline
       check passes.

T+25ms [apiserver → etcd — ch 04]
       Compute storage version (core/v1). Marshal to protobuf. etcd Txn:
       PUT /registry/pods/default/nginx (Compare-And-Swap on createRevision).
       Raft: leader appends, replicates to 2 followers, commits when both ack.

T+40ms [apiserver watch cache]
       Event arrives from etcd watch. Append to ring buffer with new
       resourceVersion. Fan out to all subscribers.

T+45ms [kube-scheduler — ch 09]
       Informer event: ADD Pod nginx with spec.nodeName=="" → enqueue.
       Worker dequeues. Scheduling cycle:
         PreFilter: nothing special.
         Filter:  walk all Nodes, reject those without resources, with
                  taints, with anti-affinity. 3 of 5 feasible.
         Score:   NodeResourcesFit + ImageLocality + InterPodAffinity →
                  node-2 wins.
         Reserve → Permit.
       Binding cycle:
         Bind: PATCH /api/v1/namespaces/default/pods/nginx/binding
         (the binding subresource sets spec.nodeName = node-2)

T+70ms [apiserver]
       Apply binding: PUT to etcd, watch event fires for the updated Pod.

T+75ms [kubelet on node-2 — ch 10]
       Informer event: MOD Pod nginx, spec.nodeName==node-2. syncLoop wakes.
       Pod worker created for the new pod's UID. Reconcile loop:

T+80ms   Volume manager: only projected SA token volume. Mount it under
         /var/lib/kubelet/pods/<uid>/volumes/.

T+85ms   CNI plugin invoked (ADD command). Cilium allocates pod IP
         10.244.5.42, creates veth pair, attaches eBPF programs to the
         pod's netns. Returns IP to kubelet.

T+90ms   CRI: RunPodSandbox → containerd creates the pause container in
         a fresh set of namespaces (net, ipc, uts) — pid namespace is
         per-container by default.

T+150ms  CRI: PullImage nginx:latest. Already cached? If not, containerd
         pulls layers from docker.io (parallel layer downloads, unpacks
         via overlayfs snapshotter). [ch 02]

T+850ms  CRI: CreateContainer for the nginx container — config.json
         generated, OCI runtime (runc) invoked.

T+860ms  runc clone3() → child process with the right namespaces,
         applies cgroup limits (cpu.max, memory.max), drops capabilities,
         loads seccomp BPF program, pivot_root into the image rootfs,
         execve("/docker-entrypoint.sh"). [ch 00, ch 01]

T+900ms  Container is running. PLEG (Pod Lifecycle Event Generator) polls
         CRI every second, observes the new container state, emits a sync
         event to the pod worker.

T+910ms  Status manager: build PodStatus (phase=Running, podIP=10.244.5.42,
         conditions=[Ready=False until readinessProbe passes, Initialized=True]).
         PATCH /api/v1/namespaces/default/pods/nginx/status

T+930ms  [apiserver → etcd]
         Status update committed. Watch event fires.

T+940ms  [Your kubectl get pod nginx -w]
         Sees status update, prints "Running".

If this Pod were behind a Service:
T+940ms  [endpointslice-controller — ch 14] sees Ready Pod with matching
         labels, adds 10.244.5.42 to EndpointSlice for the Service.
T+950ms  [kube-proxy / Cilium agent on every node] reconciles dataplane.
         Service VIP now resolves to the new endpoint.
```

The point of this trace, beyond showing you the time budget: **count the components touched.** apiserver, etcd, scheduler, kubelet, container runtime, CNI plugin, status manager, endpointslice controller, kube-proxy. They all communicate exclusively through the apiserver via watch events. Nothing was directly RPC'd from one component to another. The "everything is a watcher" principle is what makes this dance possible at all.

---

## 17. The "Everything Is a Controller" Recursion

A Deployment doesn't directly create Pods. It creates a *ReplicaSet*, and the ReplicaSet creates Pods. And the Pods register Endpoints (via a controller). And the Endpoints get sliced into EndpointSlices (via another controller). And kube-proxy watches EndpointSlices and programs iptables. And on it goes.

```
                       USER applies a Deployment
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  apiserver stores Deployment in etcd                         │
   └────────────────────────────┬─────────────────────────────────┘
                                │ watch event
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  [deployment-controller]                                     │
   │   compute PodTemplateHash; reconcile to ensure a ReplicaSet  │
   │   with that hash exists; scale old/new RSes (rolling update) │
   │   → CREATE / PATCH ReplicaSet                                 │
   └────────────────────────────┬─────────────────────────────────┘
                                │ watch event
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  [replicaset-controller]                                     │
   │   ensure spec.replicas Pods exist with matching labels +     │
   │   ownerRef pointing at this ReplicaSet                       │
   │   → CREATE Pod (×N)                                           │
   └────────────────────────────┬─────────────────────────────────┘
                                │ watch event
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  [kube-scheduler]                                            │
   │   for each Pod with spec.nodeName=="", find a node, bind     │
   │   → PATCH Pod.spec.nodeName                                  │
   └────────────────────────────┬─────────────────────────────────┘
                                │ watch event (on the right node only)
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  [kubelet on node-X]                                         │
   │   reconcile actual containers to match Pod spec              │
   │   → PATCH Pod.status                                         │
   └────────────────────────────┬─────────────────────────────────┘
                                │ watch event
              ┌─────────────────┼──────────────────┐
              ▼                 ▼                  ▼
   ┌──────────────────┐ ┌───────────────┐ ┌──────────────────────┐
   │ [endpointslice-  │ │ [your sidecar │ │ [horizontal-pod-     │
   │  controller]     │ │  injector     │ │  autoscaler controller]
   │  ─────────────── │ │  webhook —    │ │  ──────────────────  │
   │  pod is Ready,   │ │  doesn't see  │ │  reads pod metrics,  │
   │  matches a Svc → │ │  events; runs │ │  computes desired    │
   │  add to slice    │ │  at admission]│ │  replicas, PATCHes   │
   │                  │ │               │ │  Deployment.replicas │
   └────────┬─────────┘ └───────────────┘ └──────────┬───────────┘
            │                                        │
            ▼                                        │ recursion! the
   ┌──────────────────┐                              │ HPA writes back
   │ [kube-proxy on   │                              │ to the Deployment
   │  every node]     │                              │ that started this
   │  reconciles      │                              │ whole chain.
   │  iptables/IPVS/  │                              │
   │  eBPF rules      │                              │
   └──────────────────┘                              ▼
                                          ┌─────────────────────┐
                                          │ [deployment-        │
                                          │  controller]        │
                                          │  ...starts the      │
                                          │  whole loop again   │
                                          └─────────────────────┘
```

**This is the recursion that makes Kubernetes alive.** You apply a Deployment; a cascade of controllers each handle one layer of detail; the system converges; metrics flow back; the HPA re-engages the top-level controller; the cycle continues. **No central orchestrator coordinates this.** Each controller does one job. The shared bus (etcd via the apiserver) lets them compose.

When you add a new controller — say, a Postgres operator — it joins the chain exactly the same way. It watches its CRD, creates StatefulSets, watches the StatefulSets, observes Pod status via the StatefulSet's children, and PATCHes its CR's status. From the system's perspective, the operator is indistinguishable from a built-in controller.

The pathological case: **two controllers fight over the same field.** If your operator sets `spec.replicas: 3` and the HPA sets `spec.replicas: 10`, they will write to the field forever, each "correcting" the other. The solution is **server-side apply with field managers**: each controller declares which fields it owns, and the apiserver tracks ownership. The HPA owns `spec.replicas`; your operator should `kubectl apply --server-side` without touching that field. (Ch 05.)

---

## 18. Distributions: Vanilla, Managed, Opinionated, Minimal

The word "Kubernetes" covers a wide range of actual binaries. They all share the same API surface; they differ in what's bundled, what's hidden, who runs the control plane, and what extension points are encouraged. (Ch 33 goes deep on distributions; this is the preview.)

### 18.1 Vanilla / Reference

- **upstream `kubeadm`-based.** What you get if you compile `kubernetes/kubernetes` and bootstrap it. Stacked etcd by default. You own everything: CP nodes, etcd backups, OS, CNI choice, ingress choice, monitoring, upgrades. Maximum control, maximum operational burden. Used by on-prem teams, by people who want to learn, and as the substrate underneath many other distributions.

### 18.2 Managed (the cloud providers)

- **EKS** (AWS). Managed control plane (no SSH to apiservers; cloud manages etcd, scheduler, controller-manager). You manage worker node groups (EC2) or use Fargate (managed pods). IAM-integrated auth (IRSA / Pod Identity), VPC-native networking (AWS VPC CNI by default; Calico/Cilium optional). Upgrade is `aws eks update-cluster-version` plus rolling node groups.
- **GKE** (Google). Tightest integration with the cloud (regional control planes spanning AZs by default, autoscaling node pools, Anthos for multi-cluster, native Workload Identity). GKE Autopilot hides nodes entirely — you only see Pods.
- **AKS** (Azure). Managed control plane (free tier or Uptime SLA tier), AAD integration, virtual node (ACI) integration for serverless pods.

The pattern: **the cloud provider runs and is on the hook for the control plane.** You pay them per cluster (sometimes free), and your operational scope shrinks to workloads and node pools. Most production Kubernetes today is one of these three.

### 18.3 Opinionated / Enterprise

- **OpenShift** (Red Hat). Kubernetes plus a curated set of additions: integrated container build pipeline (BuildConfig / Tekton), Source-to-Image, OAuth-based authentication out of the box, a default service mesh (Istio), a default ingress (HAProxy router), a default monitoring stack (Prometheus + Grafana), strict security (SCCs — SecurityContextConstraints — that wrap and extend PodSecurity), and the Operator Lifecycle Manager (OLM) for vetted operator distribution. Aims to be a full PaaS on top of Kubernetes. Heavier than upstream; opinionated about every choice.
- **Rancher / RKE2**. CNCF-aligned distribution with multi-cluster management (the Rancher Manager UI). RKE2 is the underlying single-binary distribution (similar to K3s lineage); Rancher is the fleet UI layered on top.
- **Tanzu** (VMware). Kubernetes deeply integrated with vSphere; positions itself as enterprise-Kubernetes for traditional VMware shops.

The pattern: **bundled choices, vendor support contracts, opinionated UX.** Useful for organizations that want one number to call and don't want to debate Helm-vs-Kustomize at every layer.

### 18.4 Minimal / Edge

- **K3s**. A single static binary (~70 MB). Embedded SQLite by default (or external etcd / Postgres for HA). Removes alpha features, deprecated APIs, in-tree cloud providers. Targets edge, IoT, CI, dev environments. Astonishingly featureful for its size.
- **MicroK8s**. Canonical's distribution; a snap package, single command install, opt-in addons (DNS, storage, ingress, registry).
- **k0s**. Single binary, no host dependencies, kine-backed storage option, designed for embedded / edge.
- **Talos** (Sidero Labs). An immutable Linux OS designed specifically for Kubernetes. No SSH, no shell — all management is via a gRPC API. The OS itself is a Kubernetes node and nothing else. Production-grade for people who want a hardened, immutable substrate.
- **Kind** / **K3d** / **Minikube**. Not for production; for development. Kind runs a cluster inside Docker containers (one container per node); K3d wraps K3s the same way; Minikube runs a single-node cluster in a VM or container.

### 18.5 Picking

Decision tree (compressed; ch 33 has the full version):

```
Are you running on a public cloud, single team?
  → Managed (EKS / GKE / AKS).
Many teams, shared platform?
  → Managed + Kyverno + ResourceQuotas + vCluster for hard tenant isolation.
On-prem, traditional infra?
  → kubeadm or OpenShift, depending on appetite for opinionatedness.
On-prem, immutable infrastructure?
  → Talos.
Edge / IoT / low resources?
  → K3s, MicroK8s, or k0s.
Multi-cluster across providers?
  → Any per-cluster choice + ClusterAPI + Karmada/Fleet + Crossplane.
```

The good news: **the API surface is identical across all of them.** A manifest you write for `kind` runs unchanged on EKS, on OpenShift, on K3s, on Talos. Distribution choice changes operations, not application code.

---

## 19. The Compatibility Skew Policy

You will upgrade Kubernetes. You will not upgrade everything at once. The **version skew policy** defines which components may legally be at different versions, by how much, and in which direction. Getting this wrong produces baffling failures. Ch 32 goes deep; here is the rule you need to remember.

```
                ┌──────────────────────────────────────────────┐
                │ kube-apiserver (N)                           │
                └──────────────────────────────────────────────┘
                  ▲              ▲              ▲
                  │              │              │
   ┌──────────────┘              │              └──────────────┐
   │                             │                             │
   ▼                             ▼                             ▼
┌──────────────┐         ┌──────────────────┐          ┌────────────────┐
│ kubelet      │         │ kube-controller- │          │ kube-scheduler │
│ N, N-1,      │         │   manager        │          │  N or N-1      │
│ N-2, or N-3  │         │  N or N-1        │          │ (same as KCM)  │
│ (3 versions  │         │ (same as KCM)    │          │                │
│  back!)      │         └──────────────────┘          └────────────────┘
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ kube-proxy   │
│ N, N-1,      │
│ N-2, or N-3  │
│ (same as     │
│  kubelet on  │
│  that node)  │
└──────────────┘

kubectl: N-1, N, or N+1 (the client can be slightly newer than server)
```

**The rules, version by version:**

1. **kube-apiserver** is the reference point. The version of *every* apiserver in an HA cluster must be the same minor (during an upgrade, they can transiently differ by one minor).
2. **kube-controller-manager, kube-scheduler, cloud-controller-manager**: at most **one minor behind** the apiserver. Never ahead. (They can be N, never N+1; can be N-1 transiently during upgrade.)
3. **kubelet**: at most **three minors behind** the apiserver as of recent versions (was two; expanded by KEP-3935). Never ahead. *This is the rule that matters most* — you typically have many more nodes than CP components, and rolling all of them quickly is expensive.
4. **kube-proxy**: must match the kubelet on the same node. So same range: up to three minors behind apiserver.
5. **kubectl**: within one minor of apiserver in either direction (N-1, N, N+1).

**Upgrade order** (this is critical):

1. Upgrade etcd first (compatible with all supported apiserver versions, but verify).
2. Upgrade apiservers (one at a time, behind the LB).
3. Upgrade controller-manager, scheduler, cloud-controller-manager.
4. Upgrade kubelets (rolling, node by node, with proper drain + PDBs).
5. Upgrade kube-proxy (usually a DaemonSet, rolls automatically with the cluster).
6. Update kubectl on operator workstations and CI runners.

**Why these rules.** The apiserver is the only component that can speak old AND new API versions of resources (via conversion). So newer components can talk to an older apiserver (the apiserver knows how to interpret new fields), but older components can't talk to a newer apiserver if the API has changed structurally. The kubelet has the loosest rule because we want operators to be able to upgrade the control plane *first* (where the new features live) and then trickle the kubelet upgrade through the fleet over weeks.

**What breaks when you violate this.**
- kubelet too far behind: new Pod spec fields it doesn't understand get silently ignored; the Pod runs but without the requested feature (sidecars without sidecar lifecycle, missing native sidecar restart semantics, etc.).
- controller-manager ahead of apiserver: tries to use API verbs the apiserver doesn't yet expose. Reconciles fail with 404 or 422. Hot upgrades become impossible.
- Two apiservers at different minors during a long-running upgrade: ResourceVersion semantics can get confusing, watch streams may drop and require relist; brief degradation is normal, prolonged differ is dangerous.

The pragmatic version: **never skip a minor.** If you're at 1.28 and 1.31 is out, upgrade through 1.29 → 1.30 → 1.31, not 1.28 → 1.31 directly. This keeps you inside the skew envelope at every step.

---

## 20. TL;DR

**Kubernetes is etcd + N controllers.** A Raft-replicated KV store wrapped by a typed REST API (the apiserver), with N stateless processes (the scheduler, controller-manager, cloud-controller-manager, kubelets, your operators) that each watch some subset of the store and reconcile real-world state toward declared state.

**The communication graph is a star, not a mesh.** Only the apiserver talks to etcd. Every other component talks only to the apiserver. The kubelet is the only "node → control plane" outbound; the apiserver proxies exec/logs/port-forward back the other way, and that's the only exception. Controllers never call each other; they all read and write the same objects and notice each other's changes via watch.

**Five control-plane components** (apiserver, etcd, scheduler, controller-manager, cloud-controller-manager) and **five data-plane components** (kubelet, kube-proxy, container runtime, CNI plugin, CSI plugin). Three pluggable interfaces (CRI, CNI, CSI) keep the data plane interchangeable. Every component except etcd and the apiserver is stateless and replaceable.

**HA is etcd quorum + apiserver behind an L4 LB + leader election via Lease objects** for active-passive scheduling/controllers. Three etcd members tolerate one failure; five tolerate two; even numbers are strictly worse than odd. Stacked etcd is fine up to ~500 nodes; external dedicated etcd above that. Below 50 nodes, one CP node is fine; above 1000, you want 5 CP nodes and a 5-member external etcd on local NVMe across three AZs.

**Everything is an API object.** Pods, Services, Nodes, Events, Leases, even CRDs themselves. The same six verbs, the same RBAC, the same admission, the same audit. CRDs let you add new object types that get all of that for free. This is the load-bearing axiom that makes Kubernetes extensible.

**Nothing polls.** Every component uses informers — list-then-watch — to maintain an in-memory cache of the objects it cares about. The apiserver fans events out from a single etcd watch to N client connections via its watch cache. Polling is a bug; tight `apiserver.Get()` loops in custom controllers destabilize large clusters.

**The 5000-node reference architecture**: 5 CP nodes (32 vCPU / 128 GiB), external 5-member etcd on dedicated NVMe across 3 AZs, apiservers behind a cloud L4 NLB, Cilium for CNI + kube-proxy replacement (eBPF dataplane), separate audit log sink to S3. This hits the Scalability SIG's upper SLO ceiling (5000 nodes / 150k pods / 300k containers).

**What breaks first as you scale**: etcd disk IOPS → apiserver CPU → scheduler binding throughput → kube-proxy iptables rules → etcd Raft commit latency. Each has a known mitigation; ch 35 covers the tuning.

**Kubernetes is NOT** a PaaS, a CI/CD system, a service mesh, or a secrets manager. It is the substrate that you build all of those on, via CRDs + controllers + admission webhooks + aggregated APIs. The CNCF landscape is the proof.

**Distributions** range from upstream `kubeadm` (you own everything), to managed (EKS/GKE/AKS — the cloud owns the control plane), to opinionated (OpenShift, Rancher, Tanzu — vendor curates choices), to minimal (K3s, MicroK8s, k0s, Talos — designed for edge/embedded/immutable). The API surface is the same across all of them; only operations differ.

**Compatibility skew**: kubelet may be up to three minors behind apiserver, scheduler/controller-manager one minor behind, kubectl ±1 minor either side. Upgrade etcd → apiservers → CP controllers → kubelets → kube-proxy. **Never skip a minor.**

**The recursion**: a Deployment controller makes ReplicaSets; the ReplicaSet controller makes Pods; the scheduler binds Pods; the kubelet runs Pods; the endpoint-slice controller observes Ready Pods; kube-proxy programs Service VIPs; the HPA observes metrics and writes back to the Deployment. No central coordinator orchestrates this — each controller does one job, communicates only by reading and writing the shared bus, and the system composes. Every future feature, every operator, every extension you build is *another controller on the same loop*.

**If you can hold this map in your head**, every later chapter slots into one of these boxes. Etcd internals (04), apiserver internals (05), admission (06), AuthN/Z (07), controller pattern (08), scheduler (09), kubelet (10), workloads (11–13), services (14), networking (15–18), storage (19), policy (20), QoS (21), autoscaling (22), CRDs and operators (23), aggregation (24), tenancy (25), multi-cluster (26), supply chain (27), runtime security (28), sandboxing (29), observability (30), GitOps (31), lifecycle (32), distributions (33), custom schedulers (34), perf (35), GC (36), cloud (37), build-from-scratch (38) — each one is a single box on the diagram you now carry around.

The map is the territory. Keep reading.
