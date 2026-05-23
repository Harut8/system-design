# Kubernetes Architecture Overview

The architectural map. Kubernetes is not a container orchestrator — it is **etcd plus a swarm of controllers that watch etcd**. Containers are incidental. Everything else — pods, services, deployments, autoscaling, ingress, secrets, GitOps, multi-cluster — is the same loop applied recursively to new object types. This chapter establishes that mental model, walks every component, draws the communication graph, lays out the HA topologies you will actually deploy, and sizes a 5000-node cluster end-to-end. Later chapters (04 etcd, 05 apiserver, 08 client-go, 09 scheduler, 10 kubelet, 14 services, 15 CNI, 19 CSI, 37 cloud provider) zoom into each box. This is the map; consult it whenever you forget which component owns what.

---

## Table of Contents

1. [The Core Axiom: etcd + N Controllers](#1-the-core-axiom-etcd--n-controllers)
2. [Control Plane Components](#2-control-plane-components)
3. [Data Plane Components](#3-data-plane-components)
4. [The Communication Graph: Who Talks to Whom](#4-the-communication-graph-who-talks-to-whom)
5. [Communication Graph Deep-Dive: Failure Scenarios Per Link](#5-communication-graph-deep-dive-failure-scenarios-per-link)
6. [Failure Modes per Component: 1 Minute / 1 Hour / 1 Day](#6-failure-modes-per-component-1-minute--1-hour--1-day)
7. [HA Topologies: Stacked vs External etcd](#7-ha-topologies-stacked-vs-external-etcd)
8. [The HA Topology Trade-Off Matrix](#8-the-ha-topology-trade-off-matrix)
9. [Quorum, Write Latency, and Blast Radius: 3-node vs 5-node](#9-quorum-write-latency-and-blast-radius-3-node-vs-5-node)
10. [Leader Election via Lease Objects](#10-leader-election-via-lease-objects)
11. [Sizing Guidance: Small, Medium, Large, XL](#11-sizing-guidance-small-medium-large-xl)
12. [Worked Sizing Exercise: 1000 Nodes, 50000 Pods](#12-worked-sizing-exercise-1000-nodes-50000-pods)
13. [The Scaling SIG SLOs and What Breaks First](#13-the-scaling-sig-slos-and-what-breaks-first)
14. [A Real Cluster's Traffic Map: Bytes per Minute on Each Link](#14-a-real-clusters-traffic-map-bytes-per-minute-on-each-link)
15. [The Everything-Is-an-API-Object Axiom](#15-the-everything-is-an-api-object-axiom)
16. [GVR, Kind, and the kubectl Discovery Workflow](#16-gvr-kind-and-the-kubectl-discovery-workflow)
17. [The Single-Binary-With-Three-APIServers](#17-the-single-binary-with-three-apiservers)
18. [CRDs vs Aggregated APIs vs Built-Ins](#18-crds-vs-aggregated-apis-vs-built-ins)
19. [The Watch-Everything Principle](#19-the-watch-everything-principle)
20. [The kubernetes/kubernetes Source Tree Map](#20-the-kuberneteskubernetes-source-tree-map)
21. [Reference Architecture: A 5000-Node Cluster](#21-reference-architecture-a-5000-node-cluster)
22. [The Startup Sequence of a Fresh Cluster](#22-the-startup-sequence-of-a-fresh-cluster)
23. [What Kubernetes Is NOT — Battery-Not-Included Reality](#23-what-kubernetes-is-not--battery-not-included-reality)
24. [A First End-to-End Trace](#24-a-first-end-to-end-trace)
25. [Worked Trace 2: kubectl exec, the Streaming Proxy Path](#25-worked-trace-2-kubectl-exec-the-streaming-proxy-path)
26. [The "Everything Is a Controller" Recursion](#26-the-everything-is-a-controller-recursion)
27. [The Controller Dependency Graph: Who Watches What](#27-the-controller-dependency-graph-who-watches-what)
28. [Distributions: Vanilla, Managed, Opinionated, Minimal](#28-distributions-vanilla-managed-opinionated-minimal)
29. [Distribution Comparison Matrix](#29-distribution-comparison-matrix)
30. [The Compatibility Skew Policy](#30-the-compatibility-skew-policy)
31. [Architecture-Level Pitfalls and Misconceptions](#31-architecture-level-pitfalls-and-misconceptions)
32. [TL;DR](#32-tldr)

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

## 5. Communication Graph Deep-Dive: Failure Scenarios Per Link

The summary diagram in §4 is what you draw on a whiteboard. This section is what you reach for at 3 a.m. when one of those arrows is broken and the cluster is misbehaving in a way the dashboards don't quite explain. Every link has a direction, an initiator, an authentication model, a port, and a failure signature. Memorize the table; memorize the failure modes.

### 5.1 Reference Table: Every Link in the Cluster

| # | Link (initiator → target) | Protocol / Port | Who authenticates whom | Credential type | Idle traffic | Loaded traffic |
|---|---------------------------|------------------|------------------------|------------------|---------------|------------------|
| 1 | apiserver → etcd | gRPC over HTTP/2, TCP/2379 | mutual TLS; apiserver presents client cert, etcd presents server cert; both verified against a shared CA | X.509 client cert (`/etc/kubernetes/pki/apiserver-etcd-client.crt`) | ~5 KiB/s (lease + heartbeat) | 5–50 MiB/s (write fan-in + watch deliveries) |
| 2 | etcd → etcd (peer) | gRPC, TCP/2380 | mutual TLS; peer cert with SAN listing all members | X.509 peer cert (`/etc/kubernetes/pki/etcd/peer.crt`) | ~10 KiB/s (Raft heartbeats) | Up to 100 MiB/s (Raft log replication) |
| 3 | kubelet → apiserver | HTTPS, TCP/6443 | apiserver authenticates kubelet via X.509 client cert (`system:node:<nodename>`) or Bootstrap Token + TLS Bootstrap | rotating client cert; kubelet renews via CSR | ~2 KiB/s/node (heartbeat Lease) | 100–500 KiB/s/node (status updates + watch deliveries) |
| 4 | kube-scheduler → apiserver | HTTPS, TCP/6443 | apiserver authenticates via X.509 client cert `system:kube-scheduler` | static client cert | ~1 KiB/s (Lease renewal) | 1–10 MiB/s (Pod/Node watches + Binds) |
| 5 | kube-controller-manager → apiserver | HTTPS, TCP/6443 | apiserver authenticates via X.509 client cert `system:kube-controller-manager` | static client cert | ~10 KiB/s (Leases + watches) | 5–50 MiB/s (~30 controllers worth of watches + writes) |
| 6 | cloud-controller-manager → apiserver | HTTPS, TCP/6443 | same as KCM, different identity | static client cert | ~1 KiB/s | 100 KiB/s – 1 MiB/s |
| 7 | kube-proxy → apiserver | HTTPS, TCP/6443 | apiserver authenticates via ServiceAccount token of `kube-proxy` SA in `kube-system` | projected SA token (~1h rotation) | ~500 B/s (Service/EndpointSlice watches) | 50–500 KiB/s/node |
| 8 | CNI agent → apiserver (Cilium, Calico-typha, etc.) | HTTPS, TCP/6443 | ServiceAccount token | projected SA token | 1 KiB/s | 10 KiB – 1 MiB/s/node |
| 9 | CSI controller plugin → apiserver | HTTPS, TCP/6443 | ServiceAccount token (`csi-provisioner`, `csi-attacher`, etc.) | projected SA token | minimal | bursty (PV/VolumeAttachment events) |
| 10 | Your operator → apiserver | HTTPS, TCP/6443 | ServiceAccount token | projected SA token | depends | depends |
| 11 | apiserver → kubelet | HTTPS, TCP/10250 | kubelet authenticates apiserver via `--kubelet-client-certificate`; *kubelet authorizes via webhook or AlwaysAllow* | apiserver's client cert (`system:apiserver`) | 0 | bursty (`kubectl exec`/`logs`/`port-forward` + metrics scrapes) |
| 12 | apiserver → admission webhook | HTTPS, TCP/443 (typically) | webhook authenticates apiserver via TLS server cert it trusts via CA bundle in the `MutatingWebhookConfiguration`/`ValidatingWebhookConfiguration`; **no client auth by default unless `clientConfig.service` references a webhook that requires it** | optional client cert (configured per webhook) | 0 | every mutating/validating write touches this |
| 13 | apiserver → aggregated apiserver | HTTPS, TCP/443 (typically) | aggregated apiserver authenticates kube-apiserver via the *requestheader-client-ca-file* chain (proxy auth pattern) | requestheader proxy cert | 0 | every request to an aggregated GVR |
| 14 | cloud-controller-manager → cloud API (AWS/GCP/Azure) | HTTPS, TCP/443 | IAM role / Service Account / Managed Identity | cloud-native credential, often via IMDSv2 or workload identity | bursty | bursty (LB/Route/Node reconcile) |
| 15 | kubelet → container runtime | gRPC over Unix socket `/run/containerd/containerd.sock` | filesystem ownership (root) | none (Unix socket) | constant (PLEG polls every 1s) | bursty |
| 16 | kubelet → CNI plugin | exec (no daemon, plugin is a binary) | filesystem ownership | none | 0 | one invocation per pod add/delete |
| 17 | kubelet → CSI node plugin | gRPC over Unix socket | filesystem ownership | none | 0 | bursty (mount/unmount) |
| 18 | kubectl → apiserver | HTTPS, TCP/6443 | apiserver authenticates kubectl via whatever the kubeconfig provides (cert, OIDC, exec plugin, token) | variable: cert, OIDC, AWS IAM, GCP IAM, etc. | 0 | bursty |

The columns most people get wrong: **initiator** (e.g., admission webhooks are *invoked by* the apiserver, not the other way around) and **who authenticates whom** (mTLS means *both* sides authenticate; bearer tokens only authenticate the client to the server).

### 5.2 Scenario A: apiserver ↔ etcd link breaks

```
                  X  <-- this link is broken
   apiserver ─────X───── etcd
        │
        │ (clients keep their watch open — apiserver
        │  serves stale data from watch cache until TTL)
        ▼
   kubelet, scheduler, controllers
```

**Immediate symptoms (seconds):**
- Apiserver `/healthz` flips to unhealthy; LB starts evicting that apiserver from the pool.
- If *all* apiservers lose etcd: every write returns `etcdserver: request timed out` or `context deadline exceeded`. Reads served from watch cache continue to succeed for a window (the cache doesn't immediately expire).

**Minutes:**
- The watch cache stops receiving new events; clients see stale data.
- Lease renewals fail. Leader-elected components (scheduler, controller-manager) lose their lease; failovers cascade; eventually no replica has the lease.
- Node Leases fail to renew → `node-controller` marks nodes `Unknown` after 40s default → after `pod-eviction-timeout` (5m default) it would evict pods, *but* it can't, because writes are failing.

**Hour+:**
- Existing pods keep running. The kernel and kubelet do not care that the API is down.
- Image pulls of *new* pods that the kubelet has already accepted continue; nothing new gets scheduled.
- `kubectl` is dead. So is every controller, including ones that hold sub-second TTLs.

**Recovery:** restore the link, restore etcd quorum if that was the cause. Apiservers reconnect, watch cache rebuilds (which means a full LIST against etcd — *thundering herd risk* for large clusters; ch 04). Leader election resumes within `LeaseDuration` (~15s).

### 5.3 Scenario B: kubelet ↔ apiserver link breaks (single node, e.g., NAT or firewall change)

```
       apiserver  ──── X ────  kubelet (one node)
                                  │
                          [pods keep running locally]
                          [no status reports flow up]
```

**Initiator:** kubelet. The kubelet is the one with the long-lived HTTPS connection outbound; if the network drops, the kubelet retries.

**Seconds:** kubelet's watch streams drop. `client-go` retries with exponential backoff. Status PATCHes queue locally.

**40 seconds:** Node Lease in `kube-node-lease` has not been renewed → `node-controller` sets `NodeReady=Unknown` on the Node object.

**5 minutes (default `pod-eviction-timeout`):** the Node controller adds the `node.kubernetes.io/unreachable:NoExecute` taint. Pods on that node — *as seen by the apiserver* — start being evicted (re-scheduled elsewhere if controlled by a Deployment/ReplicaSet/etc.).

**The split-brain risk.** The kubelet on the partitioned node *is still running pods*. Replacements get scheduled on healthy nodes. **You now have two copies of each pod running** — one orphaned on the partitioned node, one new on a healthy node. For a StatefulSet this can corrupt state if both replicas write to the same volume. (See ch 13 for the StatefulSet-specific protections via storage fencing.)

**Recovery:** restore connectivity. The kubelet sees its old pods are still owned (and the new ones too, sometimes), reports status to the apiserver, and the controllers reconcile (usually by deleting the duplicates). Operators should *manually verify* before declaring the partition resolved, especially for stateful workloads.

### 5.4 Scenario C: apiserver → kubelet link breaks (kubectl exec/logs path)

```
   kubectl ─→ apiserver  ──── X ────  kubelet:10250
              (proxies the
               streaming request)
```

**Initiator:** apiserver. When you `kubectl exec`, the apiserver opens a *new outbound* connection to the kubelet's `:10250` and upgrades it to SPDY/WebSocket.

**Symptoms:** `kubectl exec` hangs and times out. `kubectl logs` returns an error. `kubectl port-forward` fails. **But the cluster continues to function.** Pods run. Schedulers schedule. Controllers reconcile. The kubelet still pushes status *up* the normal direction.

**The fix in managed Kubernetes:** Konnectivity. The apiserver doesn't directly dial the kubelet; it dials a Konnectivity *server* which has a pre-established reverse tunnel from an agent inside the node network. This turns the apiserver→kubelet outbound into a tunnel ride. EKS, GKE, AKS all use variations of this.

```
        apiserver ─────► konnectivity-server ◄═══════ konnectivity-agent
                          (in CP network)              (in worker VPC)
                                                              │
                                                              ▼
                                                          kubelet:10250
```

If you've ever wondered why `kubectl exec` works in EKS even though your nodes are in private subnets behind a NAT with no inbound, that's the answer.

### 5.5 Scenario D: etcd loses quorum (more than ⌊(N-1)/2⌋ members down)

```
   etcd-1 [DOWN]     etcd-2 [DOWN]     etcd-3 [UP, but no quorum]
       X                  X                    ▲
                                               │
                                               └── apiserver retries; reads
                                                    that need linearizability
                                                    block; writes fail.
```

**Symptoms:** apiservers report etcd unhealthy. Reads that are *serializable* (cheap, possibly stale) still work against the surviving member. Reads that are *linearizable* (the default for most apiserver reads) fail. **All writes fail.**

**The minute-by-minute:**
- 0–10s: writes hang. kubectl times out.
- 10–60s: leader election leases expire on schedulers/controllers; they enter waiting state. Nothing reconciles.
- 60s–5m: Node Leases fail to renew. (Same as Scenario A.)
- 5m+: Pod eviction taints would be added, but the writes to add them also fail. The cluster is *frozen* — pods still running but no control-plane action possible.

**Recovery options:**
1. **Bring back enough members to restore quorum.** If etcd-1 just rebooted, it'll rejoin and quorum returns.
2. **Disaster recovery from snapshot.** If multiple members are *gone* (data loss), restore the etcd cluster from a `etcdctl snapshot save`-produced backup. The recovered cluster is a single new member that you can grow back to 3 or 5. (Ch 04 walks through the exact `etcdctl snapshot restore` dance.)
3. **Force-new-cluster on a surviving member.** Risky; can produce split-brain if a "dead" member comes back. Last resort.

### 5.6 Scenario E: admission webhook is unreachable (DNS or service IP broken)

```
   apiserver ─────► admission webhook service
                        X (timeout)
```

**Behavior depends on `failurePolicy`:**

- `failurePolicy: Fail` (the strict default for *most* serious webhooks): every create/update that would route to this webhook fails with an error. **If the webhook covers `pods` and the webhook itself runs as a pod, you have created a chicken-and-egg deadlock.** The webhook can't start because pods need the webhook to admit them; pods need the webhook to start because the webhook isn't ready. This is one of the most common ways a cluster bricks itself.
- `failurePolicy: Ignore`: the webhook is skipped on timeout. Safer for advisory webhooks (e.g., mutation that adds sidecars). Dangerous for security webhooks.

**The fix:** scope your webhook's `namespaceSelector` to *exclude* `kube-system` (and your webhook's own namespace), so the webhook can always start regardless of its own failure state. And: use `objectSelector` plus `matchConditions` (CEL) to skip uninteresting requests entirely. Ch 06 has the canonical bootstrap-safe webhook recipe.

### 5.7 Scenario F: load balancer in front of apiservers is unhealthy

```
   clients ──► [BROKEN LB] ──X──► apiservers (all healthy)
```

**The classic mistake.** People remember to make the apiserver HA but forget the LB itself is a SPOF. Use a managed cloud NLB (multi-AZ, the cloud's responsibility) *or* run a software LB (kube-vip, HAProxy with VRRP keepalived) in pairs.

**The other failure mode:** L7 LB instead of L4. An L7 LB will terminate TLS, possibly re-encrypt, buffer HTTP/2 frames, and break long-lived watch streams in subtle ways. Watches will fail every few minutes ("connection reset by peer" or "watch ended unexpectedly"), causing thundering-herd relist storms. **Always L4.**

### 5.8 Scenario G: aggregation layer apiserver is unreachable

```
   kubectl ─► kube-apiserver ─X─► metrics.k8s.io aggregated apiserver
```

If the metrics-server (or any aggregated apiserver — `apiservice/v1.metrics.k8s.io`, custom-metrics adapters, an aggregated API behind a service mesh control plane) is down, every request to that GVR fails with `503 Service Unavailable`. The rest of the apiserver works fine.

**Confusing symptom:** `kubectl get pods` works, but `kubectl top pod` does not. `kubectl api-resources` lists the metrics resource but `kubectl get apiservice` shows `Available=False` for it.

**The fix:** restart metrics-server, check its CA bundle, check the `APIService` object's `caBundle` and `service` fields. Ch 24 covers the aggregation layer.

### 5.9 Summary: Which Links Are Truly Critical?

```
            Severity of outage  ◄── ─── ── ── ── ── ─►
            (cluster frozen)                       (limited blast)

   etcd ↔ etcd peer ──► etcd ↔ apiserver ──► apiserver ↔ kubelet ──► aggregated apiservers
   (CP can't function)   (writes fail)        (exec/logs only)        (one GVR fails)
                          │
                          │
                          └─► apiserver ↔ admission webhook
                              (can deadlock self-bootstrap)
```

The brutal hierarchy: **break etcd, the cluster freezes; break the apiserver, you lose control but workloads survive; break links from controllers, individual features regress; break kubelet→apiserver on one node, that node goes Unknown but pods often keep running.**

---

## 6. Failure Modes per Component: 1 Minute / 1 Hour / 1 Day

The operator's cheat sheet. For each component, what happens at three time scales when it's down. "Down" means "every replica of that component is gone" (or in the case of single-instance components like a specific kubelet, that single instance).

### 6.1 kube-apiserver (all replicas down)

| Time | What happens |
|------|--------------|
| 1 min | `kubectl` fails with `connection refused`/`context deadline`. Controllers' informers retry with backoff. Workloads run untouched. New pod creates queue locally in controllers but cannot reach the bus. |
| 1 hour | Lease-based leader election lapses across the cluster. Schedulers/controllers all in waiting state. Node Leases expire → after default `node-monitor-grace-period` (40s) Nodes go `Unknown`, but no controller is alive to act on this. **Cluster state freezes.** Image pulls in progress finish, container restarts due to liveness probes continue, but anything that requires apiserver mediation halts. |
| 1 day | If etcd is unaffected and the apiservers eventually come back: full recovery. Watch cache rebuilds (potentially expensive — see "thundering herd" warning, ch 05). If certificates expired during the outage (apiserver cert lifetime is often 1y, etcd-client cert lifetime same), you can't even bring apiservers back without first rotating certs out-of-band. |

**Survivable for hours, not days.** Workloads stay up. Anything triggered by external systems (CI deploys, webhooks, autoscaling) fails. No new pods on existing or new nodes.

### 6.2 etcd (loss of quorum)

| Time | What happens |
|------|--------------|
| 1 min | All apiserver writes fail. Linearizable reads fail. Serializable reads to surviving members may succeed for a few seconds. Watch streams drop. |
| 1 hour | Same as apiserver outage *plus* you must recover or restore. If a member just rebooted, quorum returns. If members are dead, you need snapshots. **Backup hygiene is now load-bearing.** |
| 1 day | If you cannot restore quorum, you must restore from snapshot — and you lose all writes since the snapshot. The cluster is effectively rolled back in time. Hopefully your snapshots are <15 min old; if your last snapshot is from yesterday morning, you've lost a day of state. *Pods and external state (cloud LBs, attached volumes) keep running; the cluster's view of them does not.* |

**Severity: existential.** etcd is the only stateful component. Lose it badly and you're rebuilding the cluster from your manifests in Git. (Hence GitOps. Hence Velero.)

### 6.3 kube-scheduler (all replicas down)

| Time | What happens |
|------|--------------|
| 1 min | Existing pods unaffected. New pods (just created) pile up with `spec.nodeName=""` in `Pending` phase. |
| 1 hour | Same. The backlog grows linearly with pod creation rate. No pods are scheduled to nodes. Autoscaling can't take effect because new pods don't reach Running. |
| 1 day | Backlog continues to grow. Pods that are part of stateful workloads with PDBs blocking eviction may also be impacted indirectly (you can't reschedule replacements). |

**Severity: medium.** Cluster operates; new workloads cannot land. Scheduler is stateless, easy to restart. Leader-elected — running multiple replicas means a single replica's crash is invisible.

### 6.4 kube-controller-manager (all replicas down)

| Time | What happens |
|------|--------------|
| 1 min | Existing pods unaffected. Existing Deployments/ReplicaSets/Jobs are static — no progress. EndpointSlices don't update when pods become Ready or fail. Garbage collection halts (deleted parents don't clean up children). Node Lease *consumer* (`node-controller`) is here — so node health degradation isn't observed; Nodes can be unreachable for 5+ minutes without action. |
| 1 hour | Failed pods aren't replaced (replicaset-controller is here). Completed Jobs don't get their pods cleaned up. Cascading deletes don't propagate. Service-account tokens may not be issued for new namespaces (the SA controller is here). HPA *itself* is sometimes here (or in its own deployment, depending on version). |
| 1 day | Cluster slowly degrades — orphaned pods, stale EndpointSlices pointing at dead pods, Nodes marked Ready that aren't. Workloads receiving traffic via Services start sending to dead backends. |

**Severity: medium-high.** Cluster is alive but rotting. Stateless, easy to restart. Run 3 replicas for HA.

### 6.5 cloud-controller-manager (all replicas down)

| Time | What happens |
|------|--------------|
| 1 min | No new LoadBalancer Services get cloud LBs provisioned. New Nodes don't get their `providerID` / cloud zone labels set. Route table updates for Pod CIDRs (on bare CNIs that need them) stop. |
| 1 hour | Same — backlog grows. Newly registered Nodes are in a weird half-configured state (no cloud-derived labels). |
| 1 day | If you spin up a new Service `type: LoadBalancer`, the EXTERNAL-IP stays `<pending>` forever. Existing LBs keep serving traffic — they're owned by the cloud, not by Kubernetes. |

**Severity: medium.** Cluster runtime fine; new cloud integrations broken.

### 6.6 kubelet on a single node (one node only)

| Time | What happens |
|------|--------------|
| 1 min | Node Lease stops renewing. After 40s `node-controller` marks Node `Unknown`. Pods on that node still running (kubelet was the *coordinator*, not the runner; runtime is still up). Status updates stop flowing — apiserver's view of pod state freezes. |
| 1 hour | Pods on the Node are evicted (re-scheduled to other Nodes) by `node-controller` after `pod-eviction-timeout` (5m default for non-tolerating pods). **The original pods may still be running on the orphaned node** — split brain. Stateful workloads with PVCs are protected from this by the `VolumeAttachment` and storage fencing, *if* the CSI driver and storage backend support it. |
| 1 day | Node remains `Unknown`. Operators wonder why their fleet shows one fewer healthy node. Re-installing/restarting kubelet brings the node back; existing pods get reconciled (replaced by the kubelet's restart with the new IDs it observes from the runtime). |

**Severity: low (one node). Critical if the partition is large.** A single kubelet failure is part of normal operations. A regional kubelet failure (everything in one AZ partitioned from apiservers) means losing the whole AZ from the cluster's view.

### 6.7 kube-proxy on a single node

| Time | What happens |
|------|--------------|
| 1 min | Existing Service VIP rules already in iptables/IPVS *keep working* — kube-proxy programs the rules; the kernel does the forwarding. New Services / new endpoints aren't programmed on this node, so this node won't see them. |
| 1 hour | Drift accumulates. A new Pod backing an existing Service won't receive traffic from this node (because this node's kube-proxy missed the EndpointSlice update). A removed Pod's old IP may still be in this node's iptables — packets get DNATed to a dead address. |
| 1 day | If a Service rolls out a fully new set of backends, this node sends all traffic to dead IPs. Connection failures from pods on this node only. Hard to diagnose without per-node knowledge. |

**Severity: low; easy to spot.** Failed kube-proxy = stale dataplane on one node. Restart kube-proxy (or whatever replaces it) and the next reconcile fixes it.

### 6.8 CNI agent (cluster-wide, e.g., all Cilium agents)

| Time | What happens |
|------|--------------|
| 1 min | Existing Pods keep their connectivity — the dataplane is already programmed (Linux bridge + routes, or eBPF maps). The *agent* is the control plane for the dataplane; without it, no *new* pod gets an IP. |
| 1 hour | Pods scheduled in this window are stuck in `ContainerCreating` because `CNI ADD` fails. NetworkPolicy updates don't propagate. New Services don't get programmed by Cilium's kube-proxy replacement. |
| 1 day | New workloads completely blocked. Existing workloads usually fine. If the CNI agent crash-loops, it might re-program incorrectly on restart — *which is why CNI upgrades are the riskiest cluster upgrades*. |

**Severity: high if cluster-wide.** Always upgrade CNI agents carefully, one node at a time, with canaries.

### 6.9 CSI agent (controller plugin down, then node plugin down)

**Controller plugin** (cluster-wide Deployment):
| Time | What happens |
|------|--------------|
| 1 min | No new PVs provisioned. No new VolumeAttachments processed. Existing volumes stay mounted. |
| 1 hour | New StatefulSet pods can't start (their PVCs can't bind). Existing pods fine. |
| 1 day | Backlog grows. Snapshots/expansions/clones blocked. |

**Node plugin** (DaemonSet, one per node):
| Time | What happens |
|------|--------------|
| 1 min | Volumes already mounted on this node *stay* mounted (the kernel holds them). New pods that need volumes can't mount; they're stuck in `ContainerCreating`. |
| 1 hour | Same; backlog of pending mounts on this node. |
| 1 day | This node effectively can't accept new stateful workloads. Existing ones fine. |

**Severity: medium.** Storage failures are visible (pods in `ContainerCreating` with `MountVolume.SetUp failed` events), recoverable, scoped.

### 6.10 Container runtime (containerd/cri-o on a single node)

| Time | What happens |
|------|--------------|
| 1 min | The kubelet's CRI calls fail. **Pods on this node die** — containerd is the parent of the runtime shims and the shims are the parent of the containers. Loss of containerd usually kills all containers on that node (depending on whether it ran as a systemd unit with proper cgroup parenting). |
| 1 hour | Node is effectively empty of running pods. kubelet reports the failures. Pods rescheduled to other nodes. |
| 1 day | Node is excluded from scheduling until containerd is healthy again. |

**Severity: high (one node).** A runtime crash is one of the worst single-node failures because it can take down all containers on that node simultaneously. *Most managed Kubernetes operations teams monitor containerd as a separate signal from kubelet.*

### 6.11 Survival Quick-Reference

```
Component               1 min       1 hour      1 day
─────────────────────   ─────       ──────      ─────
apiserver (all)         operational  frozen      frozen, certs may expire
etcd (no quorum)        frozen       frozen      may need restore (data loss)
scheduler (all)         backlog      backlog     backlog
kube-controller-mgr     stale        rotting     orphans, dead endpoints
cloud-controller-mgr    no new LB    no new LB   no new LB
kubelet (one node)      Unknown      pods evict  split-brain risk
kube-proxy (one node)   stale rules  stale       dead-IP routing on that node
CNI agent (all)         new pods Pending  same   same; upgrades risky
CSI agent (controller)  no new PV    same        same
CSI agent (node)        no new mounts same       same on that node
container runtime (1)   pods die     evicted     scheduled elsewhere
```

The mnemonic: **etcd is existential; everything else is degradation.** Manage your operational priorities accordingly.

---

## 7. HA Topologies: Stacked vs External etcd

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

## 8. The HA Topology Trade-Off Matrix

The stacked-vs-external dichotomy is the textbook framing, but in the wild there are more options. Most of them are bad. The point of laying out all four and ranking them is so you stop hearing "can't we just put etcd in RDS / DynamoDB / Spanner?" once a quarter.

### 8.1 The Four Topologies

```
   ┌─────────────────────────────────────────────────────────────────────────┐
   │ A. STACKED etcd (default for kubeadm)                                   │
   │    etcd colocated with apiserver, scheduler, controller-manager         │
   │    on the same 3 (or 5) CP nodes.                                       │
   └─────────────────────────────────────────────────────────────────────────┘

   ┌─────────────────────────────────────────────────────────────────────────┐
   │ B. EXTERNAL etcd on dedicated bare-metal / VMs                          │
   │    Separate 3- or 5-node etcd cluster on machines that run NOTHING      │
   │    else. Dedicated local NVMe. Often dedicated NIC.                     │
   └─────────────────────────────────────────────────────────────────────────┘

   ┌─────────────────────────────────────────────────────────────────────────┐
   │ C. EXTERNAL etcd in cloud VMs (gp3/io2 EBS, Premium SSD, pd-ssd)        │
   │    Like B but disks are network-attached block storage. The apparent    │
   │    convenience hides a latency tax — every fsync goes over the network. │
   └─────────────────────────────────────────────────────────────────────────┘

   ┌─────────────────────────────────────────────────────────────────────────┐
   │ D. etcd-in-a-managed-DB-service (NOT SUPPORTED)                         │
   │    "Can't I just point Kubernetes at Aurora / Cloud SQL / Spanner /     │
   │     DynamoDB / Cosmos DB instead of running etcd?"  NO. Here is why.    │
   └─────────────────────────────────────────────────────────────────────────┘
```

### 8.2 Trade-off Matrix

| Dimension | A. Stacked | B. External bare-metal | C. External cloud VMs | D. Managed DB (unsupported) |
|-----------|-----------|------------------------|------------------------|------------------------------|
| Machine count (3-replica) | 3 | 6 (3 CP + 3 etcd) | 6 | depends |
| Bootstrap complexity | low (kubeadm default) | medium (etcd before CP) | medium | infeasible |
| Operational complexity | low | high | medium | n/a |
| Write latency (p50) | 5–10 ms (local NVMe) | 5–10 ms | 15–30 ms (EBS gp3) or 5–10 ms (io2 with high IOPS) | 50+ ms (cross-region SQL semantics, not the right shape) |
| Blast radius of single CP node loss | also loses one etcd member | only loses one CP component | only loses one CP component | n/a |
| Noisy-neighbor risk | high (apiserver thrashes its own etcd) | none | low (other VMs on same hypervisor are out of your control) | n/a |
| Cost (rough) | 1× | 2× | 2× | not applicable |
| Recommended scale | up to 500 nodes | 500+ nodes | 100–2000 nodes | never |
| Backup / restore | etcdctl snapshot, easy | etcdctl snapshot, easy | etcdctl snapshot, easy | n/a |
| Upgrade independence | no (CP upgrade touches etcd) | yes (etcd and CP independent) | yes | n/a |
| Snapshot encryption | apiserver-level | apiserver-level | apiserver-level | n/a |

### 8.3 Why "etcd in a managed DB service" Is Not a Thing

This question comes up because someone has a managed Postgres or a managed DynamoDB and reasonably asks "why am I running my own KV store when the cloud has six of them on tap?" Here is the answer in concrete terms.

**Kubernetes does not talk SQL; it talks etcd-the-API.** The apiserver embeds the etcd v3 client and depends on these features:

1. **Range scans with sub-second consistency.** `LIST` on `/registry/pods/default/` returns all keys with that prefix, at a specific `revision`. The apiserver uses MVCC revisions to implement watch resume.
2. **MVCC with a global revision counter.** Every write atomically increments a single monotonic integer. The apiserver uses these as `resourceVersion` for optimistic concurrency.
3. **Streaming watch.** A single gRPC stream that pushes events for a key range from a starting revision. Reconnecting clients can resume from where they left off, *as long as the revision is still retained* (compaction window).
4. **Multi-key transactions with compare-and-swap.** `etcd Txn { Compare: ..., Then: [Put...], Else: [Get...] }` is how every Kubernetes write happens. Atomic across keys.
5. **Lease objects with server-side TTL eviction.** etcd auto-deletes keys when their associated lease expires (used for Events, leader election holders, etc.).
6. **Quorum reads.** Linearizable reads that go through the leader and see the latest committed state.

You'd have to implement all of these on top of a managed DB. Some are not even expressible in standard SQL semantics. There have been attempts — `kine` (used by K3s) translates etcd v3 API to SQL (SQLite, MySQL, Postgres). It works for small clusters with low write rates because it can fudge MVCC with single-row revisions; **it does not scale**, and the K3s project documents the throughput ceiling honestly.

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │  apiserver  ──► etcd v3 client  ──► etcd                            │
   │                                       MVCC + Raft + lease + watch   │
   │                                                                     │
   │  apiserver  ──► etcd v3 client  ──► kine  ──► Postgres / MySQL /   │
   │                                       │       SQLite                │
   │                                       │                             │
   │  Works for K3s edge clusters; ~ kine emulates etcd API; serializes  │
   │  writes through a single-row revision counter; not for production   │
   │  clusters above tens of nodes with normal churn.                    │
   └─────────────────────────────────────────────────────────────────────┘
```

**Bottom line.** etcd is not arbitrary — its API shape is load-bearing for the apiserver's correctness. Until someone writes a clone with byte-for-byte API compatibility *and* an open-source Raft (or similar) underneath, etcd is etcd. Don't try to swap it out.

### 8.4 Cost / Blast Radius / Complexity Visual

```
                           Cost ───────────────►
                low                            high
    Complexity
        │           ┌──────────────────────────────────────┐
        │           │                                      │
        │   low     │  A. Stacked       C. Cloud VM ext.   │
        │           │     ✓ small         ✓ medium         │
        │           │     ✗ noisy nbr     ✗ EBS latency    │
        │           │                                      │
        │   high    │  D. Managed DB    B. Bare-metal ext. │
        │           │     ✗ DOESN'T      ✓ XL              │
        │           │       WORK         ✓ best perf       │
        │           │                    ✗ ops burden      │
        │           └──────────────────────────────────────┘
        ▼
```

The dominant decision is **scale**. Below 50 nodes, A. Above 1500 nodes, B. In between, C is the most common in cloud-hosted Kubernetes; A is the most common on-prem.

---

## 9. Quorum, Write Latency, and Blast Radius: 3-node vs 5-node

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

## 10. Leader Election via Lease Objects

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

## 11. Sizing Guidance: Small, Medium, Large, XL

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

## 12. Worked Sizing Exercise: 1000 Nodes, 50000 Pods

Sizing tables are useful as bookmarks; understanding *how the numbers come out* makes you actually able to size. Let's pick a concrete target and do the arithmetic, citing the Scalability SIG SLOs (defined in [kubernetes/community/sig-scalability/slos/slos.md](https://github.com/kubernetes/community/blob/master/sig-scalability/slos/slos.md)).

**Target cluster:**
- 1000 nodes.
- 50,000 pods (50 pods per node average — comfortable; the SIG's official upper bound is ~110 pods/node).
- 1000 namespaces, 5000 Services, 50,000 EndpointSlice entries.
- 200,000 ConfigMaps and Secrets total (a busy cluster).
- ~10% pod churn per hour (5000 pods created/deleted per hour, average 1.4/sec).
- Workloads are mostly stateless web services; ~5000 PVCs across the cluster.

### 12.1 etcd Disk Sizing

**State volume.** etcd stores every object as protobuf, plus history. Rule of thumb: a Pod object averages ~10 KiB after compression. ConfigMaps and Secrets vary wildly (small ones are 1 KiB; large ones with TLS certs or large config blobs can be 100s of KiB).

Approximate current state:
```
   50,000 pods       × ~10 KiB = ~500 MiB
   200,000 cm/secret × ~5 KiB  = ~1 GiB
    5,000 services   × ~3 KiB  = ~15 MiB
   50,000 endpoints  × ~2 KiB  = ~100 MiB
       Node objects  ×  20 KiB = ~20 MiB (1000 nodes)
                                  ──────────
                       Total:    ~1.6 GiB of "current" data
```

**Historical revisions.** etcd keeps every revision until compaction. With 5000 pod-churn/hour and ~5 status updates per pod per minute (probes, lease, status), you're seeing roughly:
```
   5000 status updates/sec × 86,400 sec/day = 432M revisions/day
```
That's way too many to keep. Kubernetes runs **auto-compaction every 5 minutes** by default. Compacted revisions are reclaimed by **defragmentation** (which you must schedule explicitly — `etcdctl defrag`). Without defrag, disk grows unbounded.

**Practical disk sizing.**
- Live data + compaction headroom: ~5 GiB.
- WAL + snapshots: ~10 GiB.
- Defrag headroom (etcd needs ~2× free space during defrag): ~10 GiB.
- Audit / ops slack: ~25 GiB.
- **Recommendation: 100 GiB local NVMe per etcd member.** Pricey, but `df` should always show >50% free.

**Disk IOPS sizing.** Every write does an fsync. At 5000 pod-status-writes/sec, plus lease renewals, plus controller-manager reconciles, you're committing ~10,000 writes/sec. Each is a small (few-KiB) write. **IOPS needed: ~10k sustained, 30k burst.** Local NVMe handles this without breathing hard. EBS gp3 with 16,000 provisioned IOPS handles this but each write costs ~1 ms of network. **Use local NVMe.**

### 12.2 Apiserver CPU and Memory

The SIG's *API call latency* SLO: p99 ≤ 1s for object reads, p99 ≤ 30s for namespaced list. These are loose; in practice you want p99 < 200ms for reads and you'll hit it.

**Watch cache memory** is the big one. The apiserver keeps an in-memory cache of every object plus a recent ring buffer of events per resource type. Memory consumption:

```
   Pods:           50,000 × ~10 KiB =  ~500 MiB (per apiserver replica)
   ConfigMaps:    100,000 × ~5 KiB  =  ~500 MiB
   Secrets:       100,000 × ~5 KiB  =  ~500 MiB
   EndpointSlices: 5,000 × ~5 KiB   =   ~25 MiB
   Nodes:           1000 × ~20 KiB  =   ~20 MiB
   Events:        ~50k    × ~1 KiB  =   ~50 MiB
   Leases:        ~2000   × ~1 KiB  =    ~2 MiB
                                       ────────
                                       ~1.6 GiB raw
   Go runtime overhead, indexers, serialization:  ~2-3×
                                       ────────
                                       ~5 GiB working set per apiserver
```

Plus watch ring buffers (default 1000 events × ~10 KiB × N resources ≈ 100 MiB).

**Plus connection state.** With 1000 nodes, each kubelet holds at least 3 long-lived watches (Pods, ConfigMaps mounted, Nodes). With 5000 Services and many controllers, each controller-manager replica holds ~30 watches. Each connection uses ~50 KiB of HTTP/2 buffer space.

```
   1000 kubelets × 3 watches × 50 KiB    = ~150 MiB
   3 controller-mgrs × 30 watches × 50 KiB= ~5 MiB
   schedulers, kube-proxies, operators   = ~500 MiB
                                            ────────
                                            ~700 MiB
```

**Apiserver memory total: ~8 GiB working set + 4 GiB headroom = 12 GiB minimum, 32 GiB recommended.**

**Apiserver CPU.** The dominant cost is serializing watch events. A pod status update fires events to ~50–100 watchers (the kubelet's-own-node-filtered watch, plus the scheduler, plus a couple of controllers). Each event is ~10 KiB serialized.

At 10,000 writes/sec × 50 watchers × 10 KiB = ~5 GiB/sec of serialization. That's not memory bandwidth — that's the CPU cost of marshaling protobuf or JSON. **Empirically: ~1 vCPU per ~2000 writes/sec under realistic watcher fanout.**

For 10,000 writes/sec: **5 vCPU minimum per apiserver replica, scaled out to 3 replicas behind LB**. Use 8 vCPU to leave room for admission, auth, audit overhead. Aim for ~50% CPU utilization at p99 so APF doesn't reject.

### 12.3 Watch Cache Memory (Explicit Calculation)

The apiserver runs a watch cache per `(group, resource)` for high-traffic resources (pods, configmaps, secrets, services, endpointslices, nodes — controlled by `--watch-cache-sizes`). The cache size in entries is:

```
   apiserver --default-watch-cache-size=100      (default)
   apiserver --watch-cache-sizes=pods#1000,configmaps#500,secrets#500,...
```

But the cache *also* holds the live object set. So at 50,000 pods, the pod watch cache is ~50,000 objects + 1000 event ring buffer entries. Memory: ~600 MiB just for pods.

If you wanted ConfigMap data to be served from cache, you must keep all 100,000 of them in memory across each apiserver — at 5 KiB each, that's ~500 MiB. **This is why some operators disable the watch cache for ConfigMaps/Secrets** and pay the latency cost on read for the memory savings.

### 12.4 Scheduler Decision Rate

SIG SLO: **pod startup latency p99 ≤ 5s** (assuming image cached) and **scheduling latency p99 ≤ 1s** (apiserver-receives-Pod-create → scheduler-binds-Pod).

The default scheduler does ~100–300 binds/sec with a single replica. At 5000 pod creations/hour (1.4/sec), this is trivial. **But on a burst** — say, a Deployment scale from 0 to 5000 replicas, or a node failure causing 100 pods to be rescheduled — you need the burst budget.

Scheduler decision cost: roughly 10–30 ms per pod when there are 1000 candidate nodes. So one scheduler can handle 30–100 pods/sec sustained. A burst of 5000 pods takes **50–170 seconds** to clear. Add a `MaxClusterEvents` cap or shard via multiple scheduler profiles if you need faster burst handling.

For our 1000-node, 50k-pod cluster: **one scheduler (with HA replica in standby) is sufficient. Burst capacity 100 binds/sec is the bottleneck**, not steady state.

### 12.5 Network Bandwidth (per apiserver replica)

Watch events flow outward. Inbound POSTs/PATCHes are dwarfed by outbound watch deliveries.

Estimate:
```
   10,000 events/sec × 10 KiB × ~50 watchers each = ~5 GBit/sec outbound
```

Holy. For a 5 GBit/sec sustained network out of each apiserver, you need a 10 GbE NIC at least. **This is real — large cluster apiservers are network-bound, not CPU-bound, once you've sized CPU correctly.** Provision accordingly: NLB and apiserver hosts on 10 GbE or better.

### 12.6 Summary Table for Our 1000-Node, 50k-Pod Cluster

| Resource | Recommendation | Reasoning |
|----------|----------------|-----------|
| Control-plane node count | 3 | Survives 1 failure, handles upgrade roll without dipping below 2 |
| CP node spec | 8 vCPU / 32 GiB / 10 GbE | Apiserver memory + CPU + watch fanout bandwidth |
| etcd member count | 3 | Survives 1 failure; latency stays low |
| etcd disk | 100 GiB local NVMe | Live data + compaction headroom + defrag space |
| etcd IOPS | 10k sustained / 30k burst | Pod status, lease, controller writes |
| Scheduler replicas | 2 (HA, 1 active) | One handles steady state; failover within 15s |
| KCM replicas | 2 (HA, 1 active) | Same |
| Apiserver replicas | 3 behind L4 LB | All active, share watch load |
| CNI | Cilium with eBPF kube-proxy replacement | iptables won't scale past 5k Services efficiently |
| Audit log destination | external sink (S3 + SIEM) | Auditing 10k req/sec creates 50–100 MiB/hour of audit data |

### 12.7 Where the SIG SLOs Bite First in This Cluster

Even on the recommended hardware, here's where you'd first see SLO violations:

1. **Burst load → APF rejection.** If 5000 pods get created at once (CI scale-up), the apiserver's `system` priority level will queue them, but other priority levels may see `429 Too Many Requests`. Tune APF flowschemas. (Ch 05.)
2. **EndpointSlice update storms.** A Deployment rollout that turns over 1000 pods causes 1000 EndpointSlice updates, each fanning out to every kube-proxy. Spike in apiserver CPU. Mitigate by tuning `--endpointslice-updates-batch-period` (default 0; non-zero batches updates, trading staleness for fewer events).
3. **Watch ring buffer exhaustion during slow consumer.** A misbehaving controller that falls behind by >1000 events gets `410 Gone` and forces a full relist. Memory spike. Mitigate with informer rate limits.
4. **Defrag pauses.** etcd defrag freezes writes for ~10s on a 5 GiB DB. Do it during low-traffic windows, rolling one member at a time.

**The takeaway: 1000 nodes is comfortable on modest hardware *if* you tune APF, run defrag on a schedule, use Cilium, and run audit logs externally.** Most cluster pain at this scale is operational, not architectural.

---

## 13. The Scaling SIG SLOs and What Breaks First

The Kubernetes Scalability SIG publishes a set of SLOs that the upstream community guarantees up to **5000 nodes / 150,000 pods / 300,000 containers** per cluster. The most-cited:

1. **API call latency (read, single object)**: p99 ≤ 1s.
2. **API call latency (list, namespaced)**: p99 ≤ 30s (yes, 30s; lists are expensive and Kubernetes wants you to use watch).
3. **Pod startup time (scheduling + image pull + container start, image already cached)**: p99 ≤ 5s.
4. **Pod scheduling latency (apiserver-create → scheduler-bind)**: p99 ≤ 1s.
5. **In-cluster DNS programming latency (Service create → resolvable everywhere)**: p99 ≤ 5s.

If your cluster violates these and you're under the 5k/150k/300k thresholds, something is misconfigured. If you're *above* those thresholds, you're off-the-map and need to shard, tune, or accept worse SLOs.

### 13.1 What breaks first, in order

1. **etcd disk I/O** is the *first* limit you hit and the one that produces the worst symptoms. Every write goes through Raft → fsync. If your etcd is on EBS gp2 (or worse, gp3 without provisioned IOPS), you'll see write latency spikes that turn into apiserver request queueing, which turns into watch cache backpressure, which turns into pods stuck in `ContainerCreating`. Symptoms: `etcdserver: request timed out`, `apply entries took too long`. Fix: local NVMe, defrag schedule, snapshot retention tuning. (Ch 04, ch 35.)
2. **Apiserver CPU**, specifically the JSON/protobuf serialization of watch events and the watch cache event broadcasting. Under-provisioned apiservers manifest as `429 Too Many Requests` (APF rejecting), bursts of `context deadline exceeded` in client logs, and informer relists (which create thundering herd loads). Fix: scale apiserver replicas horizontally, increase `--max-requests-inflight`, tune APF flowschemas, use protobuf clients. (Ch 05, ch 35.)
3. **Scheduler throughput** ceilings at roughly 100–300 binds/sec for the default scheduler. If you create 10000 pods at once (e.g., scale a Deployment from 0 to 10000), you'll see a multi-minute backlog in `kube-scheduler`. Fix: use scheduling profiles to enable parallel binding, run multiple schedulers with non-overlapping pod selectors, or for batch workloads use Volcano/Yunikorn. (Ch 09, ch 34.)
4. **kube-proxy iptables rules**: each Service adds rules per endpoint per node, and packet matching is O(N) in iptables mode. At ~5k Services or ~50k endpoints, kube-proxy reconcile cycles take seconds and SYN packets get noticeable latency. Symptoms: connection establishment time grows with cluster size; `kube-proxy` CPU pinned. Fix: switch to IPVS, nftables, or replace with eBPF (Cilium). (Ch 14, ch 16.)
5. **Etcd Raft commit latency** at the very top end. If your inter-node ping is 1ms and you're committing 10k writes/sec, you're paying 10s of latency per write just on the network. Geographically distributed etcd is an antipattern. Keep etcd in a single AZ (use cluster-level HA, not etcd-level cross-region replication, to handle AZ failure).

---

## 14. A Real Cluster's Traffic Map: Bytes per Minute on Each Link

Numbers, not adjectives. A real 500-node cluster running web workloads idles at very different bandwidth from one in the middle of a rollout. Here are realistic byte-per-minute volumes per link, broken down for an "idle" baseline (no human activity, no rollouts, no node failures) and "loaded" (a Deployment rolling out 200 pods, autoscaling firing, CI deploying a new release).

The numbers are derived from production telemetry and Scalability SIG test runs. They are *approximate* — your cluster will vary by ±2× depending on workload churn — but the *orders of magnitude* and *ratios* are correct.

### 14.1 Idle Baseline (500-node cluster, no human activity)

```
   Link                              Direction         Bytes/minute
   ───────────────────────────────   ────────────────  ────────────
   apiserver  ──► etcd               writes            ~50 MB/min
   apiserver  ──► etcd               watches in        ~10 MB/min
   etcd       ──► etcd peers         Raft replication  ~100 MB/min
   kubelet    ──► apiserver          status + lease    ~3 KB/min/node × 500 = 1.5 MB/min
   kubelet    ◄── apiserver          watch deliveries  ~50 KB/min/node × 500 = 25 MB/min
   scheduler  ──► apiserver          lease renewals    ~50 KB/min
   scheduler  ◄── apiserver          Pod/Node watches  ~5 MB/min
   ctrl-mgr   ──► apiserver          PATCHes etc.      ~2 MB/min
   ctrl-mgr   ◄── apiserver          ~30 watches       ~30 MB/min
   kube-proxy ◄── apiserver          Svc/EPSlice watch ~10 KB/min/node × 500 = 5 MB/min
   CNI agent  ◄── apiserver          NetPol/Pod watch  ~50 KB/min/node × 500 = 25 MB/min
   apiserver  ◄── apiserver          aggregation       ~1 MB/min
```

Total apiserver outbound: **~90 MB/min ≈ 12 Mbit/s** across all watchers, idle.

Total etcd outbound (Raft + apiserver delivery): **~150 MB/min ≈ 20 Mbit/s**, idle.

The dominant idle traffic is **Raft replication** (every etcd write hits all peers) and **watch deliveries** (every state change fans out to every watcher who cares).

### 14.2 Under Load (deploying 200 new pods, HPA scaling, CI active)

```
   Link                              Direction         Bytes/minute   Multiplier
   ───────────────────────────────   ────────────────  ─────────────  ──────────
   apiserver  ──► etcd               writes            ~500 MB/min    10×
   apiserver  ──► etcd               watches in        ~100 MB/min    10×
   etcd       ──► etcd peers         Raft replication  ~1 GB/min      10×
   kubelet    ──► apiserver          status + lease    ~20 MB/min     ~13×
   kubelet    ◄── apiserver          watch deliveries  ~250 MB/min    10×
   scheduler  ──► apiserver          binding PATCHes   ~10 MB/min     200×
   scheduler  ◄── apiserver          Pod/Node watches  ~50 MB/min     10×
   ctrl-mgr   ──► apiserver          PATCHes           ~50 MB/min     25×
   ctrl-mgr   ◄── apiserver          watches           ~300 MB/min    10×
   kube-proxy ◄── apiserver          EPSlice updates   ~50 MB/min     10×
   CNI agent  ◄── apiserver          Pod watch         ~250 MB/min    10×
   kubectl    ──► apiserver          CI deploys        ~5 MB/min      —
```

Total apiserver outbound: **~900 MB/min ≈ 120 Mbit/s**, all watchers, loaded.

Total etcd outbound: **~1.5 GB/min ≈ 200 Mbit/s**, loaded.

**The peaks are real.** During a node failure that re-schedules 100 pods, you can see 10× spikes lasting 30–60 seconds. Provision your apiserver-to-LB and apiserver-to-etcd network at 10× the average sustained rate.

### 14.3 Ratios and Surprises

- **The apiserver's outbound (watch delivery) is ~10× its inbound (writes).** This is the watch fan-out tax. Every write delivers to many watchers.
- **The kubelet→apiserver direction is small; apiserver→kubelet is large.** Kubelets are noisy consumers, not noisy producers — a single Pod-status PATCH is small (~2 KB) but every kubelet's Pod-watch receives a stream of all pod events on that node.
- **etcd peer traffic is the single largest line item** under load. Raft sends the full log entry to every follower. This is why "geographically distributed etcd" (e.g., 1 member each in US, EU, Asia) is so painful — every write pays 100+ ms of cross-region latency.
- **kubectl is *not* a big traffic source.** Humans generate < 1% of cluster API traffic. Controllers generate the rest.

### 14.4 What This Means for Provisioning

Practical guidance for a 500–1000 node cluster:

- **CP nodes:** 10 GbE NIC minimum. They will not saturate it under steady state but will burst close during failure/upgrade events.
- **etcd nodes:** 10 GbE NIC. Raft is bandwidth-hungry under load.
- **L4 LB:** the cloud's NLB handles 1+ Gbit/s easily. If you're using a software LB (HAProxy), confirm it's sized for your apiserver fanout.
- **Worker NICs:** unrelated to control plane — pod-to-pod traffic dominates. Size based on application needs.

The cluster's *baseline* "metabolic rate" is roughly **20 Mbit/s of control-plane traffic per 500 nodes**, scaling roughly linearly. Don't confuse this with workload traffic, which dwarfs it.

---

## 15. The Everything-Is-an-API-Object Axiom

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

## 16. GVR, Kind, and the kubectl Discovery Workflow

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

### 16.1 Namespaced vs Cluster-scoped

- **Namespaced** resources live inside a Namespace and have URLs like `/apis/<group>/<version>/namespaces/<ns>/<resource>/<name>`. Pods, Deployments, Services, ConfigMaps, Secrets, RoleBindings, PVCs.
- **Cluster-scoped** resources do not have a namespace and have URLs like `/apis/<group>/<version>/<resource>/<name>`. Nodes, Namespaces (themselves), PersistentVolumes, StorageClasses, ClusterRoles, ClusterRoleBindings, CRDs, APIServices, MutatingWebhookConfigurations, PriorityClasses.

CRDs declare their own scope at creation time (`spec.scope: Namespaced` or `Cluster`). Choosing wrong is a permanent decision — you can't change scope without deleting and recreating the CRD (and migrating data).

### 16.2 The kubectl Discovery Workflow

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

## 17. The Single-Binary-With-Three-APIServers

When you read about *the* apiserver, you're being lied to a little. The `kube-apiserver` binary is actually **three apiservers stitched together** by a chain of delegation. Knowing this is the difference between understanding why CRDs and aggregated APIs feel different (they are) and being baffled by "why doesn't my CRD show up under `/api/v1` but does under `/apis`?"

### 17.1 The Three Apiservers

```
                            INCOMING REQUEST
                          (HTTPS, mTLS, port 6443)
                                   │
                                   ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │   1. kube-aggregator     (staging/src/k8s.io/kube-aggregator/)      │
   │                                                                     │
   │   Looks at the path:                                                │
   │     /apis/<group>/<version>/...                                     │
   │   For each non-built-in group, checks the APIService registry.      │
   │   If the APIService points to an external service: PROXY there.     │
   │   Otherwise: pass to the next handler in the chain.                 │
   │                                                                     │
   │   Owns: APIService objects, the proxy/delegation logic.             │
   │   Handles: external aggregated apiservers (e.g., metrics.k8s.io).   │
   └───────────────────────────────┬─────────────────────────────────────┘
                                   │ delegate
                                   ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │   2. kube-apiserver (core)   (staging/src/k8s.io/apiserver/ +       │
   │                                  pkg/registry/, pkg/apis/)          │
   │                                                                     │
   │   Handles the BUILT-IN types: Pods, Services, Deployments, etc.     │
   │   Knows the schemas of /api/v1, /apis/apps/v1, /apis/batch/v1, etc. │
   │   Storage path: etcd via storage backend.                           │
   │   If the GVR is not built-in: delegate to the next handler.         │
   │                                                                     │
   │   Owns: built-in API types.                                         │
   └───────────────────────────────┬─────────────────────────────────────┘
                                   │ delegate
                                   ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │   3. apiextensions-apiserver  (staging/src/k8s.io/apiextensions-    │
   │                                    apiserver/)                       │
   │                                                                     │
   │   Handles CRDs and the custom types they define.                    │
   │   Reads CustomResourceDefinition objects, dynamically registers     │
   │   handlers for each one's GVR.                                      │
   │   Same etcd; different storage key prefix per GVR.                  │
   │   Same admission, auth, audit chain — just dynamically dispatched.  │
   │                                                                     │
   │   Owns: CRDs and all CR objects of CRD-defined types.               │
   └───────────────────────────────┬─────────────────────────────────────┘
                                   │ if no handler matches
                                   ▼
                              404 Not Found
```

### 17.2 Who Handles What

| Request path | Handled by |
|--------------|-----------|
| `GET /apis/metrics.k8s.io/v1beta1/nodes/...` | kube-aggregator → external metrics-server pod |
| `GET /api/v1/pods` | kube-apiserver core (built-in) |
| `GET /apis/apps/v1/deployments` | kube-apiserver core (built-in) |
| `GET /apis/cilium.io/v2/ciliumnetworkpolicies` | apiextensions-apiserver (CRD) |
| `GET /apis/myco.com/v1/widgets` | apiextensions-apiserver (CRD) |
| `GET /api` (discovery) | All three contribute; aggregated by core |
| `GET /apis` (discovery) | All three contribute; aggregated |
| `POST /apis/admissionregistration.k8s.io/v1/mutatingwebhookconfigurations` | kube-apiserver core (built-in) |

### 17.3 Three Surfaces for Extension

So when someone says "I want to add a new resource type to Kubernetes," they have three options:

1. **Fork kube-apiserver and add it as a built-in.** Don't do this. (Almost) nobody does.
2. **Add a CRD.** apiextensions-apiserver picks it up. Storage in main etcd. No new processes. 99% of operators.
3. **Run an aggregated apiserver.** A separate process implementing the apiserver framework, advertising itself via an `APIService` object. The aggregator proxies requests to it. Used by `metrics-server`, `custom-metrics-adapter`, and some niche cases (KCP, multi-tenant Kubernetes, etc.). Forward-ref to **ch 24** (aggregation layer) and **ch 23** (CRDs and operators).

```
   ┌──────────────────────────────────────────────────────────────┐
   │  Three ways to extend the API surface                        │
   │                                                              │
   │  1. Built-in        ─── kube-apiserver core                  │
   │     (fork kube;          static schema, in-tree              │
   │      don't)              storage layer, same etcd            │
   │                                                              │
   │  2. CRD             ─── apiextensions-apiserver              │
   │     (the default)        dynamic schema (OpenAPI v3),        │
   │                          same etcd, same admission,          │
   │                          declarative validation              │
   │                                                              │
   │  3. Aggregated API  ─── kube-aggregator → your process       │
   │     (full control)       your storage (could be your own     │
   │                          etcd or anything), your schema,     │
   │                          your validation; the only           │
   │                          requirement is the apiserver        │
   │                          framework's HTTP handler shape      │
   └──────────────────────────────────────────────────────────────┘
```

Ch 05 walks through how the kube-aggregator's delegation chain is constructed in code; ch 23 and 24 cover the surfaces in depth.

---

## 18. CRDs vs Aggregated APIs vs Built-Ins

Picking between the three extension surfaces is one of the first design choices an operator-builder makes. The decision usually goes: "CRD, unless I have a specific reason." Here is the table that explains *why* and *when*.

| Aspect | Built-in | CRD | Aggregated API |
|--------|----------|-----|-----------------|
| Where the code lives | `kubernetes/kubernetes` | a controller image you ship | a separate apiserver process you ship |
| Where data is stored | main etcd | main etcd (under `/registry/<group>/<resource>/...`) | wherever you want (your own etcd, an SQL DB, an in-memory store) |
| How clients discover the type | static, baked into kubectl/client-go | discovery endpoints + OpenAPI v3 | discovery endpoints + OpenAPI v3 |
| Schema definition | Go types + generated OpenAPI | OpenAPI v3 schema in the CRD object, validated server-side | OpenAPI v3 served by your apiserver |
| Validation | Go code + admission chain | OpenAPI + CEL expressions + admission chain | whatever your apiserver implements |
| Conversion between versions | in-tree Go conversion functions | static (none) or conversion webhook | implemented by your apiserver |
| Defaulting | in-tree Go defaults | OpenAPI `default` fields | your apiserver |
| Subresources | `/status`, `/scale` built in | `/status`, `/scale` available (declared in CRD) | whatever you implement |
| RBAC integration | yes | yes | yes |
| Watch support | yes | yes | yes (must implement) |
| Cost to operate | zero (you didn't write it) | one controller deployment | one apiserver deployment + storage |
| Latency impact | none | none (same path as built-ins) | one extra hop through the aggregator |
| Use cases | the standard objects | 99% of operators, custom resources, GitOps configs | metrics-server, custom-metrics, vCluster, KCP, anything needing storage other than main etcd |

**The decision tree:**

```
   Do I need to store this data somewhere other than main etcd?  →  Aggregated API
   No?
       Do I need any of: long-poll streaming, very different auth model,
       schema not expressible in OpenAPI, > 10 MB per object?       →  Aggregated API
       No?
                                                                      →  CRD
```

In practice, you'll write a CRD. Aggregated APIs are rare because the operational overhead of running a separate apiserver — including watching its own state, propagating CA bundles, version-skewing it with kube-apiserver — is significant. The metrics-server is the canonical example because *its* state (metrics) genuinely doesn't belong in etcd; for almost everything else, CRD is correct.

Forward-refs: **ch 23 (CRDs and operators)** for the CRD path in full detail; **ch 24 (aggregation layer)** for aggregated apiservers; **ch 05 (apiserver internals)** for how the three are stitched together inside `kube-apiserver`.

---

## 19. The Watch-Everything Principle

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

## 20. The kubernetes/kubernetes Source Tree Map

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

### 20.2 Per-Directory Walkthrough

A more detailed tour. If you've never opened the source, this is the orientation; if you have, it's the cheat sheet.

#### `cmd/` — one main package per binary

Each subdirectory of `cmd/` is a binary's entry point. They are *tiny* — usually a `main.go` with ~50 lines that imports a `pkg/...` "app" package, builds a cobra Command, and runs it. The real logic lives elsewhere; `cmd/` is just the wiring.

```
   cmd/kube-apiserver/apiserver.go        → 30 lines; calls
                       app.NewAPIServerCommand() in pkg/kubeapiserver/app/server.go
   cmd/kubelet/kubelet.go                 → calls cmd/kubelet/app/server.go which
                       builds the full kubelet from pkg/kubelet/
   cmd/kubeadm/kubeadm.go                 → calls cmd/kubeadm/app/cmd/cmd.go
                       (cobra command tree for `kubeadm init`, `join`, `reset`, etc.)
   cmd/kubemark/                          → fake kubelets for scale testing;
                       hollow-node binary that mimics 1000 real kubelets
```

The pattern: cmd/ knows nothing; pkg/<binary>/app/ assembles configuration; pkg/<binary>/ has the actual implementation.

#### `pkg/` — the implementation that doesn't get exported

Code in `pkg/` is *internal* to kubernetes/kubernetes. External code is not supposed to import it (and Go tooling enforces this for some paths via internal/). The interesting subdirectories:

- `pkg/kubelet/` — the kubelet's core. Look at `kubelet.go` for the main `syncLoop`. Subpackages: `pleg/` (lifecycle event polling), `prober/` (liveness/readiness probes), `volumemanager/`, `cm/` (container manager — cgroups, CPU/memory manager), `eviction/`, `status/`, `network/dns/`, `cri/remote/` (CRI client).
- `pkg/scheduler/` — the scheduler. `framework/` has the extension points, `framework/plugins/` has the built-in plugins (`nodeaffinity`, `nodevolumelimits`, `interpodaffinity`, `tainttoleration`, `volumebinding`, etc.). `internal/queue/` is the priority queue.
- `pkg/proxy/` — kube-proxy modes. Subdirs `iptables/`, `ipvs/`, `nftables/`, `winkernel/`, `userspace/` (deprecated).
- `pkg/controller/` — every built-in controller. Each subdirectory is one controller: `deployment/`, `replicaset/`, `daemon/`, `statefulset/`, `job/`, `cronjob/`, `garbagecollector/`, `endpointslice/`, `nodelifecycle/`, `resourcequota/`, `serviceaccount/`, `namespace/`, `disruption/`, etc.
- `pkg/registry/` — the apiserver's storage layer per resource. `pkg/registry/core/pod/storage/storage.go` defines how a Pod is stored and what its subresources are (`/status`, `/binding`, `/log`, `/exec`, etc.). The registries register themselves with the apiserver's generic framework.
- `pkg/apis/` — *internal* (unversioned) API types. The apiserver converts between external versions (in `staging/src/k8s.io/api/`) and these internal types. The internal types are what flows through admission, defaulting, and validation.
- `pkg/volume/` — legacy in-tree volume plugins (mostly deprecated; CSI is the future).
- `pkg/credentialprovider/` — kubelet's image pull credential plumbing.
- `pkg/security/podsecurity/` — the PodSecurity admission implementation.

#### `staging/src/k8s.io/` — the published Go modules

This is the *most important* directory in the repo because **its contents are what every Kubernetes consumer in the ecosystem imports**. Each subdirectory is mirrored on every commit to a standalone repo at `github.com/kubernetes/<name>`. When you `import "k8s.io/client-go/..."`, you're getting code from here.

- `staging/src/k8s.io/api/` → mirrors to `k8s.io/api`. The versioned API types: `core/v1`, `apps/v1`, `batch/v1`, `networking/v1`, `storage/v1`, `rbac/v1`, `coordination/v1`, etc. Each subdirectory has `types.go` (the struct definitions you read when looking up "what fields does a Deployment have"), plus generated files `zz_generated_deepcopy.go`, `zz_generated_conversion.go`, `zz_generated_defaults.go`.

  > These are *the* type definitions. When kubebuilder generates a CRD's Go types or when client-go marshals an object, they reference structs from here.

- `staging/src/k8s.io/apimachinery/` → mirrors to `k8s.io/apimachinery`. The type system primitives: `schema.GroupVersionKind`, `runtime.Object` interface, `unstructured.Unstructured`, the conversion machinery, JSON merge patch, strategic merge patch, label/field selector parsing.

- `staging/src/k8s.io/client-go/` → mirrors to `k8s.io/client-go`. The client library:
  - `kubernetes/` — typed clientset (`clientset.AppsV1().Deployments(ns).Get(...)`)
  - `tools/cache/` — informer, reflector, indexer (the watch loop machinery)
  - `tools/leaderelection/` — leader election library
  - `tools/clientcmd/` — kubeconfig loading
  - `tools/record/` — event recorder
  - `util/workqueue/` — the rate-limited workqueue
  - `discovery/` — discovery client (GVR ↔ GVK ↔ aliases)
  - `dynamic/` — untyped client for arbitrary GVRs (used by kubectl, controller-runtime, etc.)
  - `rest/` — low-level REST client (where retries, backoff, TLS happen)
  - `transport/` — TLS / token / round-tripper setup

- `staging/src/k8s.io/apiserver/` → mirrors to `k8s.io/apiserver`. The **generic apiserver framework**. Used by `kube-apiserver`, `kube-controller-manager`, `kube-scheduler` (for healthz/metrics), `apiextensions-apiserver`, `kube-aggregator`, and any aggregated apiserver. Contains:
  - `pkg/server/` — the request handler chain
  - `pkg/admission/` — admission framework
  - `pkg/audit/` — audit logging
  - `pkg/authentication/` — auth plumbing
  - `pkg/authorization/` — authz plumbing
  - `pkg/storage/etcd3/` — etcd v3 client wrapper
  - `pkg/storage/cacher/` — the watch cache
  - `pkg/endpoints/` — REST endpoint registration

- `staging/src/k8s.io/apiextensions-apiserver/` → the CRD apiserver. Built on top of `apiserver/`. Implements dynamic schema registration from CRD objects.

- `staging/src/k8s.io/kube-aggregator/` → the aggregation layer. Built on top of `apiserver/`. Implements the `APIService` proxying.

- `staging/src/k8s.io/kube-openapi/` → OpenAPI v3 generation and serving plumbing.

- `staging/src/k8s.io/component-base/` → shared component bootstrap: logging (klog), metrics (Prometheus + custom), version embedding, featuregate management, leaderelection wrappers, config.

- `staging/src/k8s.io/code-generator/` → the magic. Generators that produce:
  - `deepcopy-gen` → `zz_generated_deepcopy.go`
  - `client-gen` → typed clientsets
  - `lister-gen` → listers (cache-reading helpers)
  - `informer-gen` → informers (watch-cache wrappers)
  - `conversion-gen` → internal↔external conversion
  - `defaulter-gen` → defaulting logic
  - `openapi-gen` → OpenAPI schemas

  These are how a new API type written as a Go struct turns into a complete typed client + informer + lister + schema. Most operator frameworks (kubebuilder, operator-sdk, controller-runtime) wrap these.

- `staging/src/k8s.io/kubectl/` → the actual kubectl implementation. `pkg/cmd/` has cobra command implementations; `pkg/cmd/apply/` is `kubectl apply`; `pkg/cmd/exec/` is `kubectl exec`; etc.

- `staging/src/k8s.io/csi-translation-lib/`, `cloud-provider/`, `controller-manager/`, `mount-utils/`, etc. — smaller libraries used by specific components.

#### `vendor/` — vendored third-party dependencies

`go.mod`-driven vendoring of external dependencies: etcd client (`go.etcd.io/etcd/...`), gRPC, Prometheus, cobra, viper, etc. Plus a peculiar thing: **vendored copies of the staging modules themselves**, used by `cmd/` and `pkg/` to import their own staging code as if it were external. This is the trick that lets the monorepo "import its own published libraries" cleanly.

#### `test/` — testing

- `test/e2e/` — the end-to-end test suite. Ginkgo-based. Each test creates real objects in a real cluster and verifies behavior. Many bug investigations start by reading the e2e test for the area.
- `test/integration/` — apiserver-only tests (no kubelet, no real nodes), faster than e2e, slower than unit tests.
- `test/utils/` — common test helpers.

#### `hack/` — build and dev tooling

Shell scripts for codegen (`hack/update-codegen.sh`), linting (`hack/verify-*.sh`), local cluster bootstrap (`hack/local-up-cluster.sh`). When you add a new field to an API type, you run `hack/update-codegen.sh` to regenerate the `zz_generated_*.go` files.

#### `plugin/` — admission plugin implementations

The plugins compiled into `kube-apiserver`: `plugin/pkg/admission/podsecurity/`, `plugin/pkg/admission/resourcequota/`, `plugin/pkg/admission/noderestriction/`, etc. Plus `plugin/pkg/auth/authorizer/` (RBAC, ABAC, Node authorizer implementations).

### 20.3 What Generates What

The repo lives or dies by code generation. The flow:

```
   1. You edit  staging/src/k8s.io/api/<group>/<version>/types.go
              (e.g., add a field to PodSpec)

   2. You run  hack/update-codegen.sh

   3. Generated files updated:
      - zz_generated_deepcopy.go (in same package)
      - zz_generated_conversion.go
      - zz_generated_defaults.go
      - staging/src/k8s.io/client-go/kubernetes/typed/<group>/<version>/<type>.go
      - staging/src/k8s.io/client-go/informers/<group>/<version>/<type>.go
      - staging/src/k8s.io/client-go/listers/<group>/<version>/<type>.go
      - OpenAPI schemas
      - protobuf .pb.go files (some types are protobuf-encoded for performance)

   4. Test  hack/update-openapi-spec.sh   → updates api/openapi-spec/
   5. Commit  generated files alongside your source change.
```

If `hack/update-codegen.sh` produces a diff, your PR must include those diffs. CI verifies this with `hack/verify-codegen.sh`.

### 20.4 Where the Staging Modules Vendor Out To

Every commit to staging is mirrored to per-module repos:

```
   staging/src/k8s.io/api               →  github.com/kubernetes/api
   staging/src/k8s.io/client-go         →  github.com/kubernetes/client-go
   staging/src/k8s.io/apimachinery      →  github.com/kubernetes/apimachinery
   staging/src/k8s.io/apiserver         →  github.com/kubernetes/apiserver
   staging/src/k8s.io/apiextensions-... →  github.com/kubernetes/apiextensions-apiserver
   staging/src/k8s.io/kube-aggregator   →  github.com/kubernetes/kube-aggregator
   ... and ~20 more
```

External code imports those mirror repos directly. **kubebuilder, operator-sdk, controller-runtime, every operator, every CLI tool that talks to Kubernetes** — all of them depend on those mirrors. This is why client-go's version tracks Kubernetes minor versions one-to-one.

### 20.5 Related repos you'll bounce to

- `kubernetes/enhancements` — KEPs (Kubernetes Enhancement Proposals). When you want to know *why* something was designed a certain way, find the KEP.
- `kubernetes/community` — SIG charters, working group docs, the contributing guide.
- `kubernetes-sigs/controller-runtime` — the controller framework that operators are built on (used by every kubebuilder/operator-sdk project).
- `kubernetes-sigs/cluster-api` — declarative cluster lifecycle.
- `etcd-io/etcd` — the etcd repo itself.
- `containerd/containerd`, `cri-o/cri-o` — the container runtimes.

---

## 21. Reference Architecture: A 5000-Node Cluster

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

### 21.1 Why each choice

**5 control-plane nodes, not 3.** Three CP nodes can survive one failure. Five can survive two — and during a rolling upgrade, you're already *down one* by design. Five lets you cordon-and-upgrade without going below 3 healthy. Each is 32 vCPU / 128 GiB because watch fan-out to 5000 kubelets needs CPU and the watch cache alone consumes tens of GiB.

**External 5-member etcd on dedicated hardware.** At 5000 nodes you're firmly in territory where stacked etcd will lose to noisy-neighbor effects. Dedicated NVMe is non-negotiable; etcd's Raft commit is fsync-bound. Spread 5 members across 3 AZs (2-2-1) so you survive the loss of an entire AZ without losing quorum.

**Local NVMe on CP nodes for audit logs.** Audit log write volume scales with API request rate. Writing to network storage adds latency to every audited request (most of them, in a security-conscious cluster). Local NVMe absorbs the spike; ship asynchronously to S3.

**Multiple apiservers behind a cloud L4 LB.** L4 (TCP) only — L7 LBs break HTTP/2 watch streams. Use the cloud's NLB to get cross-AZ failover and source-IP preservation. Each apiserver runs full-throated; no sharding.

**Cilium for CNI + kube-proxy replacement.** At 5000 nodes and likely 10k+ Services, iptables kube-proxy is dead in the water (reconcile takes seconds, packet latency suffers). Cilium's eBPF socket-LB bypasses iptables entirely; lookup cost is O(1), not O(N). It also gives you L7 NetworkPolicy, mTLS (via WireGuard or IPsec), and observability via Hubble. Same agent handles CNI and proxy.

**Separate audit log sink.** If the cluster is compromised, the audit log is your forensic record. It cannot live *only* on the cluster it audits. Ship every audit event to S3 + SIEM in a different account/project with strict IAM.

**What's not shown but you also need:** an Ingress / Gateway tier (Envoy-based, separate node pool), a metrics stack (Prometheus / Thanos / VictoriaMetrics), a logging stack (Loki / Vector / OpenSearch), GitOps (ArgoCD), policy (Kyverno or Gatekeeper), and a backup/restore tool (Velero with CSI snapshots). All of those are *also* just controllers watching apiserver objects.

---

## 22. The Startup Sequence of a Fresh Cluster

Cluster startup is a tangle of mutual dependencies. The kubelet needs the apiserver (to find its Pods); the apiserver needs etcd (storage); the apiserver, scheduler, and controller-manager often *run as Pods* (static pods on the CP nodes) — but who runs the Pods if the kubelet hasn't started? And how does the first kubelet authenticate to an apiserver that doesn't yet have its certs? The bootstrap dance answers all of these. Forward-ref to **ch 32 (lifecycle)** for the exhaustive treatment; here is the orientation.

### 22.1 The Chicken-and-Egg Problem

```
   kubelet      ──needs──►  kube-apiserver  (to find its Pods)
   kube-apiserver ──needs──►  etcd          (storage)
   kube-apiserver, etcd, scheduler, controller-manager
                ─ usually run as ─►  Pods
   Pods         ── managed by ──►   kubelet
   kubelet      ── started by ──►   systemd  (NOT by Kubernetes)
```

The trick that breaks the cycle: **the kubelet is not started by Kubernetes**. It's a systemd unit on the host (or, on immutable OSes like Talos, an init service). It's the only Kubernetes process started by something other than Kubernetes itself.

The control-plane components *are* Pods — but they are **static Pods**: Pod manifests sitting in `/etc/kubernetes/manifests/` on the host filesystem, which the kubelet reads directly and runs without needing the apiserver. The kubelet, once started, sees those manifests and creates the apiserver, etcd, scheduler, and controller-manager. Now the apiserver exists, and the kubelet *also* registers itself with the apiserver as a Node.

### 22.2 The kubeadm Bootstrap Dance

```
   T+0   [operator runs `kubeadm init` on the first CP node]
         kubeadm generates a CA, server cert, client certs, kubeconfigs.
         kubeadm writes static-pod manifests for etcd, apiserver,
         controller-manager, scheduler to /etc/kubernetes/manifests/.

   T+5s  [systemd starts kubelet]
         kubelet starts up. Its --pod-manifest-path points at
         /etc/kubernetes/manifests/. It picks up the four manifests
         and starts the containers via CRI.

   T+10s [etcd container starts]
         etcd boots, becomes the single-member cluster (initial-cluster=
         this-host). Listens on 2379. No data yet.

   T+15s [kube-apiserver container starts]
         apiserver connects to etcd over localhost. Reads its own client
         cert from the host's /etc/kubernetes/pki/. Begins serving on
         6443. Empty cluster: no Pods, no Services, just the
         bootstrap RBAC and the "system:" identities.

   T+20s [controller-manager and scheduler containers start]
         They connect to the apiserver via /etc/kubernetes/admin.conf
         on the host (mounted as a hostPath into the static Pod).
         They begin watching the (empty) cluster.

   T+25s [kubelet registers itself]
         The same kubelet that started the static Pods now creates a
         Node object for itself via the apiserver. (It uses a
         bootstrap-token to authenticate the first time; the apiserver
         issues it a proper client cert via CSR.)

   T+30s [first reconcile loop completes]
         The Node controller sees the new Node. Marks it Ready. The
         scheduler will now consider it for Pods.

   T+30s [kubeadm runs post-install addons]
         kubeadm applies the kube-proxy DaemonSet and the CoreDNS
         Deployment via the now-running apiserver. (CNI is NOT installed
         by kubeadm — operator must apply one before nodes become Ready.)

   T+60s [CNI installed]
         Operator does `kubectl apply -f cilium.yaml` (or calico, or
         flannel). Cilium DaemonSet's pods land on the CP node, set
         up the eBPF dataplane, and signal "Ready" by writing to the
         Node's status.networkConditions.

   T+90s [Node Ready=True]
         CNI is configured; the Node is now fully operational.

   T+5m  [operator joins more nodes with `kubeadm join`]
         Each new node:
           - kubelet starts (systemd)
           - kubelet authenticates via the cluster's bootstrap-token
           - kubelet gets a client cert via CSR
           - Node object created; Node registered as Ready once CNI
             initializes on that node (CNI's DaemonSet schedules a pod
             there via the now-existing scheduler).
```

### 22.3 The Order of Component Startup, Visualized

```
   ┌─────────────────────────────────────────────────────────────┐
   │  systemd                                                    │
   │     │                                                       │
   │     ▼                                                       │
   │  kubelet (host process)                                     │
   │     │  reads static-pod manifests                           │
   │     ▼                                                       │
   │  ┌─────────────────────────────────────────────────────┐    │
   │  │  static Pods (managed by kubelet, NOT by apiserver) │    │
   │  │   etcd                                              │    │
   │  │     │                                               │    │
   │  │     ▼                                               │    │
   │  │   kube-apiserver                                    │    │
   │  │     │                                               │    │
   │  │     ▼                                               │    │
   │  │   scheduler, controller-manager                     │    │
   │  └─────────────────────────────────────────────────────┘    │
   │     │                                                       │
   │     ▼                                                       │
   │  kubelet registers itself as a Node via the apiserver       │
   │     │                                                       │
   │     ▼                                                       │
   │  operator applies CNI (and kube-proxy, CoreDNS, etc.)       │
   │     │                                                       │
   │     ▼                                                       │
   │  Node becomes Ready                                         │
   │     │                                                       │
   │     ▼                                                       │
   │  worker nodes join; same pattern, but without static Pods   │
   └─────────────────────────────────────────────────────────────┘
```

### 22.4 Why Static Pods, Not DaemonSets

Static pods are the answer to "what runs on this node *before* the cluster exists?" They are decoupled from the apiserver entirely:

- The kubelet reads manifests from a host directory (`--pod-manifest-path`, default `/etc/kubernetes/manifests/`).
- These manifests are *Pod* manifests but they are *not* stored in etcd.
- The kubelet creates "mirror Pods" in the apiserver representing them, so `kubectl get pods -n kube-system` shows them — but the apiserver cannot modify them. They are owned by the kubelet, only the kubelet.

This decoupling is what makes the bootstrap *possible*. If the apiserver had to exist before the apiserver could start, you'd be stuck.

Managed Kubernetes (EKS, GKE, AKS) usually doesn't use static Pods because the control plane is hosted by the provider in a separate, pre-existing cluster (or simply on cloud VMs the provider manages directly). Your "kubeadm-like" bootstrap is invisible to you — you start with a Node and a kubeconfig pointing at an already-running apiserver.

### 22.5 Forward-Refs

- The exact `kubeadm init` flow, with every file written and every step. **Ch 32 (lifecycle).**
- The bootstrap-token + TLS bootstrap dance that lets a fresh kubelet authenticate. **Ch 07 (authentication).**
- How static Pods become "mirror Pods" in the apiserver. **Ch 10 (kubelet internals).**
- Managed Kubernetes control-plane provisioning (the cloud's secret sauce). **Ch 33 (distributions).**

---

## 23. What Kubernetes Is NOT — Battery-Not-Included Reality

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

### 23.1 The "Battery-Not-Included" Reality

Out of the box, a freshly bootstrapped Kubernetes cluster is shockingly minimal. Here is a table of every operational concern you'll have within the first week of running a real workload, what Kubernetes ships with, and what you'll end up installing from the ecosystem. The honest answer to "is Kubernetes enough?" is **no**, and the honest follow-up is "but it's the right substrate."

| Concern | Does Kubernetes provide it? | Ecosystem answer |
|---------|----------------------------|------------------|
| Building container images | No | Docker, BuildKit, Kaniko, ko, buildah, Tekton |
| Source repository | No | GitHub, GitLab, Gitea, Bitbucket |
| CI (run tests, build artifacts) | No | GitHub Actions, GitLab CI, Jenkins, Tekton, Argo Workflows, CircleCI |
| CD (deploy artifacts to clusters) | No (kubectl apply is *not* CD) | ArgoCD, Flux, Spinnaker, Helmfile |
| Package management (apps as bundles) | No | Helm, Kustomize, ytt, jsonnet |
| GitOps reconciliation | No | ArgoCD, Flux |
| Secrets management | Barely (base64 in etcd, opt-in encryption-at-rest) | Vault, AWS Secrets Manager, GCP Secret Manager, Azure Key Vault, SOPS + age, Sealed Secrets, External Secrets Operator |
| Service mesh (mTLS, retries, traffic split, L7 policy) | No | Istio, Linkerd, Cilium service mesh, Consul Connect |
| Ingress (HTTP/HTTPS to outside) | API only; no implementation | nginx-ingress, Traefik, HAProxy ingress, Contour, Emissary; Gateway API implementations: Envoy Gateway, Cilium, Istio |
| API gateway (auth, rate limit, transforms) | No | Kong, Ambassador, APISIX, AWS API Gateway |
| Observability — metrics | API only (custom-metrics) | Prometheus, Thanos, VictoriaMetrics, Datadog, New Relic |
| Observability — logs | No | Loki, Vector, fluent-bit, fluentd, Elasticsearch/OpenSearch, Datadog |
| Observability — traces | No | Jaeger, Tempo, OpenTelemetry Collector, Honeycomb |
| Alerting | No | Prometheus AlertManager, PagerDuty, OpsGenie |
| Dashboards | No | Grafana, Kubernetes Dashboard (very minimal, mostly read-only) |
| Backup and restore | No | Velero (with CSI snapshots), Kasten K10, Stash |
| Disaster recovery | No | Velero again, plus etcd snapshot tooling |
| Policy enforcement (security, compliance) | Partial (PodSecurity Standards, RBAC, NetworkPolicy) | Kyverno, OPA Gatekeeper, Cilium policies |
| Image vulnerability scanning | No | Trivy, Snyk, Anchore, Clair, Wiz |
| Image signing and verification | API only (admission webhook surface) | Sigstore/Cosign, Notary v2, Kyverno verify-images |
| Software bill of materials (SBOM) | No | Syft, SPDX/CycloneDX tools |
| Runtime security (detect process abuse, kernel exploits) | No | Falco, Tetragon (Cilium), Tracee, Sysdig |
| Sandboxing (extra isolation for untrusted code) | API only (RuntimeClass) | gVisor, Kata Containers, Firecracker (via Kata) |
| Authentication (humans) | API only (no UI, no IdP) | OIDC providers (Okta, Auth0, Keycloak, Google, Azure AD), Dex |
| Authentication (workloads to cloud) | Projected SA tokens | IRSA (AWS), Workload Identity (GCP), AAD Pod Identity (Azure), SPIFFE/SPIRE |
| Authentication (workload to workload) | Service account tokens (must be configured) | SPIFFE/SPIRE, service mesh mTLS |
| Cluster autoscaling (node count) | No | Cluster Autoscaler, Karpenter, GKE node auto-provisioning |
| Pod autoscaling | Yes (HPA, VPA) | KEDA for event-driven scaling |
| Cost visibility | No | Kubecost, OpenCost, Cast.ai, vendor tools |
| Multi-tenant isolation | Namespaces + RBAC + quotas + NetworkPolicy (weak isolation) | vCluster, Capsule, HNC; or per-tenant clusters |
| Multi-cluster orchestration | No (Federation v1 was deprecated; v2 work continues) | Argo CD ApplicationSets, Karmada, Fleet, Crossplane, Cluster API |
| Infrastructure-as-code for clusters | No | Cluster API, Crossplane, Terraform, Pulumi |
| Database-as-a-service | No | Operators: PostgreSQL (CloudNativePG, Zalando, Crunchy), MySQL (Percona, Oracle), MongoDB, Kafka (Strimzi), Redis, etc. |
| Message queue | No | Kafka via Strimzi, NATS, RabbitMQ via operators |
| Object storage | No | MinIO, Ceph via Rook, Longhorn |
| Persistent block storage on bare metal | No | Longhorn, Rook/Ceph, OpenEBS, Portworx |
| TLS certificate management | No | cert-manager (deeply ubiquitous; effectively part of the platform) |
| DNS for external names | API only (ExternalDNS not built-in) | ExternalDNS |
| Notifications, ChatOps | No | Botkube, custom |

**The pattern is clear:** Kubernetes provides *primitives* and *extension points*. Everything else is the CNCF landscape. A "production-ready cluster" in 2026 is roughly: Kubernetes + cert-manager + ingress (Envoy-based) + Cilium (or other CNI) + Prometheus + Grafana + Loki + ArgoCD + Velero + Kyverno + ExternalDNS + (cloud-specific autoscaler) + Vault + (cloud-specific secrets integration) + Falco. That's a baseline of ~15 components, none of which are "Kubernetes" proper. The CNCF Landscape map shows 1000+ projects in this ecosystem; you'll typically use 20–30.

### 23.2 What This Means for "Choosing Kubernetes"

The decision is not "Kubernetes or not"; it's "Kubernetes plus the curated stack vs. a hosted platform." A small team might be better served by Heroku, Fly.io, Render, Railway, Vercel, or AWS App Runner — those *are* PaaS, with the batteries included. Kubernetes is for organizations that need the substrate's flexibility and can either operate the surrounding stack themselves or pay a managed-Kubernetes provider (EKS / GKE / AKS) plus a platform team to do it.

This is also why **OpenShift exists**: it bundles ~80% of the ecosystem stack into one product with vendor support. The trade-off is opinion ("you will use HAProxy router; you will use Istio") vs. choice. Both are defensible.

---

## 24. A First End-to-End Trace

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

## 25. Worked Trace 2: kubectl exec, the Streaming Proxy Path

The `kubectl run` trace in §24 is the standard "create-an-object → controllers-make-it-real" flow. **`kubectl exec` is different.** It doesn't create a Pod, it doesn't write to etcd, and the apiserver doesn't act as a database client — it acts as a *streaming proxy*. Understanding this trace is what unlocks the rest of the "apiserver → kubelet" link's failure modes.

### 25.1 The High-Level Picture

```
   kubectl       apiserver          kubelet      container runtime
     │             │                  │                 │
     │  HTTPS      │                  │                 │
     │ POST .../exec?command=sh&stdin=1&stdout=1&tty=1  │
     │ Upgrade: SPDY                                    │
     ├────────────►│                  │                 │
     │             │                  │                 │
     │ 101 Switching Protocols (SPDY) │                 │
     │◄────────────┤                  │                 │
     │             │                  │                 │
     │             │  apiserver opens a NEW             │
     │             │  HTTPS connection to kubelet:10250 │
     │             │  POST exec/<container>?command=sh  │
     │             │  Upgrade: SPDY                     │
     │             ├─────────────────►│                 │
     │             │ 101 Switching   │                 │
     │             │◄─────────────────┤                 │
     │             │                  │  CRI:           │
     │             │                  │  ExecRequest    │
     │             │                  ├────────────────►│
     │             │                  │ ExecResponse    │
     │             │                  │  returns a URL   │
     │             │                  │◄────────────────┤
     │             │                  │                 │
     │             │                  │  kubelet opens   │
     │             │                  │  another stream  │
     │             │                  │  to that URL on  │
     │             │                  │  the runtime     │
     │             │                  ├────────────────►│
     │             │                  │                 │
     │             │   AT THIS POINT THERE ARE FOUR     │
     │             │   STACKED STREAMS:                 │
     │             │     kubectl ↔ apiserver            │
     │             │     apiserver ↔ kubelet            │
     │             │     kubelet ↔ runtime              │
     │             │     runtime ↔ container's pty      │
     │             │   Bytes flow end-to-end through    │
     │             │   all of them.                     │
     │             │                  │                 │
```

### 25.2 The Time-Stamped Trace

```
$ kubectl exec -it nginx -- sh

T+0     [kubectl]
        Resolves "exec" subresource of pods/v1. Constructs URL:
          /api/v1/namespaces/default/pods/nginx/exec
            ?command=sh&stdin=true&stdout=true&tty=true&container=nginx
        Opens HTTPS connection to apiserver. Sends a request with
        `Upgrade: SPDY/3.1` (or, in newer versions, WebSocket).

T+10ms  [apiserver — ch 05]
        AuthN: kubectl's client cert → user=alice.
        AuthZ: RBAC checks `create` on `pods/exec` (note: pods/exec, not pods).
        Validation: command and container valid.
        No admission webhook chain — exec is not a CRUD verb on a stored object.
        No etcd write. This is the divergence from the kubectl run path:
        EXEC NEVER TOUCHES ETCD.

T+15ms  [apiserver]
        Looks up the Pod in its watch cache. Finds spec.nodeName=node-2.
        Knows the kubelet on node-2 listens at https://node-2:10250.
        Opens a NEW outbound HTTPS connection to that kubelet.
        Authenticates AS THE APISERVER using its own client cert
        (--kubelet-client-certificate). Kubelet authorizes via webhook
        (or AlwaysAllow on minimal clusters).

T+25ms  [apiserver → kubelet:10250]
        Sends:
          POST /exec/default/nginx/nginx?command=sh&stdin=true&stdout=true
                                         &tty=true
          Upgrade: SPDY/3.1
        Kubelet returns 101 Switching Protocols and the SPDY framing begins.

T+30ms  [apiserver]
        Now apiserver has TWO upgraded streams: one to kubectl, one to kubelet.
        It splices them at the SPDY frame level — stdin frames from kubectl
        flow to kubelet; stdout/stderr frames from kubelet flow to kubectl.
        Apiserver is a DUMB BIDIRECTIONAL PROXY at this point. It does not
        parse the bytes; it just shovels frames.

T+35ms  [kubelet]
        Receives the exec request via SPDY. Calls the container runtime
        via CRI:
          ExecRequest{ container_id: "abc123", cmd: ["sh"], tty: true,
                       stdin/stdout/stderr: true }
        CRI implementations (containerd, CRI-O) return an HTTP URL pointing
        at the runtime's internal streaming server — not the exec stream
        itself, but a URL to fetch it.

T+40ms  [kubelet]
        Opens a streaming connection to that URL (locally, over the
        runtime's Unix socket or a streaming-server port).

T+45ms  [containerd]
        For each requested stream (stdin/stdout/stderr) opens a pipe to
        the container's pty. (If tty=true, both stdout and stderr come
        from the same pty master.)
        Inside the container: runc has set up the namespaces; the entrypoint
        is already running; exec creates an additional process in those
        same namespaces (clone3 with CLONE_NEWPID=false, etc., joining
        the existing namespaces via setns syscall).

T+50ms  [container]
        sh process running inside the container's namespaces, with a pty.
        The pty's stdout/stderr/stdin are connected upward through the
        chain.

T+50ms  [end-to-end byte path]
        keystroke  in kubectl
          → SPDY frame   over kubectl ↔ apiserver TLS connection
          → apiserver splices it into the apiserver ↔ kubelet stream
          → SPDY frame   over apiserver ↔ kubelet TLS connection
          → kubelet splices it into the kubelet ↔ runtime stream
          → runtime writes to pty master
          → kernel delivers to sh's stdin
        each byte traverses 4 processes, 3 TLS connections, and at least
        one Unix socket.
```

### 25.3 Why This Is Architecturally Interesting

Three properties stand out compared to the regular `kubectl run` path:

1. **The apiserver is acting as a streaming proxy, not a database client.** It does not touch etcd. It does not write any object. It is doing what an L7 reverse proxy would do, except gated by RBAC and authenticated to the kubelet.
2. **The kubelet's `:10250` port is the *only* inbound port on a Kubernetes node from the control plane.** Without it, no exec, no logs, no port-forward. This is the link that gets blocked by firewalls in security-sensitive environments and necessitates Konnectivity (§5.4).
3. **It's full-duplex and long-lived.** A `kubectl logs -f` (follow) can stay open for hours. So can `kubectl port-forward`. The apiserver must keep these connections alive without buffering (since the bytes are interactive). This is why **L4 load balancers** in front of the apiserver are critical — an L7 LB might buffer or kill long-lived streams.

### 25.4 The Authentication Chain in Exec

There are four authentications in this single command:

```
   1. kubectl ─► apiserver
        kubectl's client cert (or OIDC token, or AWS IAM exec plugin)
        authenticates user=alice.

   2. apiserver ─► kubelet
        apiserver's --kubelet-client-certificate (system:kube-apiserver-...
        identity) is presented; kubelet trusts that CA.

   3. kubelet ─► runtime
        Unix-socket-level filesystem permissions; the kubelet is root, runs
        as the same user as containerd.

   4. runtime ─► container
        The exec runs as the container's user, dropped capabilities, same
        namespaces, same seccomp/AppArmor profile.
```

If any link breaks, the entire chain breaks — and the symptoms feel weird because most users never think about the *four* authentications behind one command.

### 25.5 Forward-refs

- The SPDY → WebSocket migration is ongoing. `kubectl exec` over WebSocket is now the default in newer versions; SPDY remains for backwards compatibility. **Ch 05.**
- The Konnectivity tunnel (how managed Kubernetes makes apiserver→kubelet work despite NATed nodes). **Ch 05, ch 33.**
- The kubelet's HTTPS server and its authentication/authorization. **Ch 10.**
- The CRI `Exec` API. **Ch 01.**

---

## 26. The "Everything Is a Controller" Recursion

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

## 27. The Controller Dependency Graph: Who Watches What

§26 showed one *trace* through the controller graph. This section shows the **graph itself** — a directed map of all the built-in controllers and what each one watches and writes. Understanding this graph is what turns "Kubernetes is a bunch of controllers" from a slogan into something you can reason about during an incident.

### 27.1 The Big Picture

```
                                         user / GitOps
                                              │
                                              ▼ writes CRs, Deployments,
                                                Services, Jobs, ...
                          ┌─────────────────────────────────────┐
                          │              apiserver              │
                          │       (all reads/writes through)    │
                          └──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬─┬──┘
                             │  │  │  │  │  │  │  │  │  │  │ │
   ┌─────────────────────────┘  │  │  │  │  │  │  │  │  │  │ │
   │  ┌─────────────────────────┘  │  │  │  │  │  │  │  │  │ │
   │  │  ┌─────────────────────────┘  │  │  │  │  │  │  │  │ │
   ▼  ▼  ▼                            │  │  │  │  │  │  │  │ │
 [deployment-                         │  │  │  │  │  │  │  │ │
  controller]                         │  │  │  │  │  │  │  │ │
   │ watches:  Deployment, RS         │  │  │  │  │  │  │  │ │
   │ writes:   RS                     │  │  │  │  │  │  │  │ │
   ▼                                  │  │  │  │  │  │  │  │ │
 [replicaset-                         │  │  │  │  │  │  │  │ │
  controller]                         │  │  │  │  │  │  │  │ │
   │ watches:  RS, Pod                │  │  │  │  │  │  │  │ │
   │ writes:   Pod                    │  │  │  │  │  │  │  │ │
   ▼                                  │  │  │  │  │  │  │  │ │
   .................                  │  │  │  │  │  │  │  │ │
                                      │  │  │  │  │  │  │  │ │
                                      ▼  ▼  ▼  ▼  ▼  ▼  ▼  ▼ ▼
                          [scheduler] [statefulset-]  [job-]   ...
                                       controller     ctrl

     (and a parallel set for cloud-, scheduler, kubelet, and your operators)
```

### 27.2 The Controller-by-Controller Watch/Write Table

A non-exhaustive but representative list of 15 controllers (built-ins only; your operator adds more):

| Controller | Reads / Watches | Writes |
|------------|-----------------|--------|
| **deployment-controller** | Deployment, ReplicaSet | ReplicaSet (creates/scales) |
| **replicaset-controller** | ReplicaSet, Pod | Pod (creates/deletes) |
| **statefulset-controller** | StatefulSet, Pod, PVC, ControllerRevision | Pod (sequentially), PVC, ControllerRevision |
| **daemonset-controller** | DaemonSet, Pod, Node, ControllerRevision | Pod (one per matching Node), ControllerRevision |
| **job-controller** | Job, Pod | Pod (creates up to completions), Job status |
| **cronjob-controller** | CronJob, Job | Job (on schedule), CronJob status |
| **node-lifecycle-controller** | Node, Pod, Lease | Node status (taints, conditions), Pod evictions |
| **endpointslice-controller** | Service, Pod | EndpointSlice |
| **endpoint-controller** (legacy) | Service, Pod | Endpoints (the old API; now mirrored from EndpointSlice) |
| **service-account-controller** | Namespace, ServiceAccount | ServiceAccount (default in each namespace) |
| **service-account-token-controller** | ServiceAccount | Secret (legacy SA tokens; mostly deprecated) |
| **resource-quota-controller** | Namespace, ResourceQuota, every quotaable object | ResourceQuota status |
| **namespace-controller** | Namespace | Namespace status (finalization), deletes all namespaced objects on Namespace delete |
| **garbage-collector** | every object | deletes orphans whose ownerRef parents are gone |
| **persistent-volume-binder** | PV, PVC, StorageClass | PV/PVC binding (matches PVC to PV) |
| **persistent-volume-protection** | PV, PVC | adds/removes `kubernetes.io/pv-protection` finalizer |
| **ttl-after-finished-controller** | Job (with TTL) | deletes Jobs after TTL |
| **horizontal-pod-autoscaler** | HPA, metrics-server, Deployment / StatefulSet | scale subresource of Deployment / StatefulSet |
| **kube-scheduler** | Pod (unbound), Node, PVC, StorageClass | Pod binding subresource (spec.nodeName) |

### 27.3 The Visual Graph (the Important Edges)

```
                ┌──────────────┐
                │  Deployment  │◄────────────HPA writes here
                └──────┬───────┘
        watched by     │ deployment-controller creates / scales
        HPA            ▼
                ┌──────────────┐
                │  ReplicaSet  │
                └──────┬───────┘
                       │ replicaset-controller creates / deletes
                       ▼
                ┌──────────────┐                       ┌─────────────┐
                │     Pod      │◄─────────────────────►│ scheduler   │
                └──────┬───────┘ writes spec.nodeName  └─────────────┘
                       │
                       │ kubelet reads from apiserver, runs container,
                       │ writes Pod.status
                       │
                       ▼
                ┌──────────────┐
                │  Pod.status  │ ── watched by everything downstream
                └──────┬───────┘
                       │
           ┌───────────┼────────────┬────────────────┐
           ▼           ▼            ▼                ▼
   ┌─────────────┐  ┌──────┐  ┌─────────────┐  ┌─────────────┐
   │endpointslice│  │ HPA  │  │   GC        │  │ your        │
   │-controller  │  │      │  │ (owners)    │  │ operator    │
   └──────┬──────┘  └──────┘  └─────────────┘  └─────────────┘
          │
          ▼
   ┌──────────────┐
   │EndpointSlice │  ─────────► watched by kube-proxy on every node
   └──────────────┘             which programs iptables / IPVS / eBPF

   ┌──────────────┐
   │  Namespace   │ ─────► watched by namespace-controller (cleanup)
   │              │ ─────► watched by sa-controller (default SA)
   │              │ ─────► watched by resource-quota-controller
   └──────────────┘

   ┌──────────────┐
   │     Job      │ ─────► creates Pods (job-controller)
   │              │ ─────► watched by ttl-after-finished
   └──────┬───────┘
          ▲
          │ creates Jobs
          │
   ┌──────────────┐
   │  CronJob     │
   └──────────────┘

   ┌──────────────┐    ┌──────────────┐
   │  PVC         │◄──►│   PV         │
   └──────────────┘    └──────────────┘
          ▲                ▲
          │   binder       │
          └────────────────┘
```

### 27.4 The Two Properties That Make This Work

1. **Owner references and the GC.** When a Deployment creates a ReplicaSet, the RS has `ownerReferences: [{ kind: Deployment, name: ..., uid: ... }]`. Same for Pods owned by RSes. The garbage collector watches *everything* and when an owner is deleted, it propagates the delete to all owned objects (cascading delete). When you `kubectl delete deployment X`, you never explicitly delete the RS or Pods — the GC does it. **This is the only "fully connected" controller in the cluster**, by necessity.

2. **Reverse-lookup indexers.** The GC and many other controllers need "given object X, find everything that references it." This is implemented via informer indexers — secondary in-memory hash maps keyed by ownerReference UIDs. Heavy use of indexers is why the controller-manager's memory footprint grows with cluster size.

### 27.5 Where Your Operator Plugs In

When you write an operator (ch 23), you join this graph as a new node. A typical Postgres operator might:

```
                    ┌─────────────────────┐
                    │ PostgresCluster CR  │
                    └──────────┬──────────┘
                               │ your operator watches
                               ▼
   creates: StatefulSet, Service, Secret (passwords),
            ConfigMap (postgres.conf), PVC templates,
            CronJob (backups), PodMonitor (Prometheus)
```

Each of those objects then enters the *built-in* graph: the StatefulSet creates Pods, the Service gets EndpointSlices, the PVCs get bound to PVs by the binder, the CronJob runs backup Jobs, etc. Your operator is one node in a graph it didn't author.

This is why operators *compose* with built-in machinery so cleanly: there is nothing special about being an operator. You watch what you watch, write what you write, and the system processes it the same way it processes any other write.

---

## 28. Distributions: Vanilla, Managed, Opinionated, Minimal

The word "Kubernetes" covers a wide range of actual binaries. They all share the same API surface; they differ in what's bundled, what's hidden, who runs the control plane, and what extension points are encouraged. (Ch 33 goes deep on distributions; this is the preview.)

### 28.1 Vanilla / Reference

- **upstream `kubeadm`-based.** What you get if you compile `kubernetes/kubernetes` and bootstrap it. Stacked etcd by default. You own everything: CP nodes, etcd backups, OS, CNI choice, ingress choice, monitoring, upgrades. Maximum control, maximum operational burden. Used by on-prem teams, by people who want to learn, and as the substrate underneath many other distributions.

### 28.2 Managed (the cloud providers)

- **EKS** (AWS). Managed control plane (no SSH to apiservers; cloud manages etcd, scheduler, controller-manager). You manage worker node groups (EC2) or use Fargate (managed pods). IAM-integrated auth (IRSA / Pod Identity), VPC-native networking (AWS VPC CNI by default; Calico/Cilium optional). Upgrade is `aws eks update-cluster-version` plus rolling node groups.
- **GKE** (Google). Tightest integration with the cloud (regional control planes spanning AZs by default, autoscaling node pools, Anthos for multi-cluster, native Workload Identity). GKE Autopilot hides nodes entirely — you only see Pods.
- **AKS** (Azure). Managed control plane (free tier or Uptime SLA tier), AAD integration, virtual node (ACI) integration for serverless pods.

The pattern: **the cloud provider runs and is on the hook for the control plane.** You pay them per cluster (sometimes free), and your operational scope shrinks to workloads and node pools. Most production Kubernetes today is one of these three.

### 28.3 Opinionated / Enterprise

- **OpenShift** (Red Hat). Kubernetes plus a curated set of additions: integrated container build pipeline (BuildConfig / Tekton), Source-to-Image, OAuth-based authentication out of the box, a default service mesh (Istio), a default ingress (HAProxy router), a default monitoring stack (Prometheus + Grafana), strict security (SCCs — SecurityContextConstraints — that wrap and extend PodSecurity), and the Operator Lifecycle Manager (OLM) for vetted operator distribution. Aims to be a full PaaS on top of Kubernetes. Heavier than upstream; opinionated about every choice.
- **Rancher / RKE2**. CNCF-aligned distribution with multi-cluster management (the Rancher Manager UI). RKE2 is the underlying single-binary distribution (similar to K3s lineage); Rancher is the fleet UI layered on top.
- **Tanzu** (VMware). Kubernetes deeply integrated with vSphere; positions itself as enterprise-Kubernetes for traditional VMware shops.

The pattern: **bundled choices, vendor support contracts, opinionated UX.** Useful for organizations that want one number to call and don't want to debate Helm-vs-Kustomize at every layer.

### 28.4 Minimal / Edge

- **K3s**. A single static binary (~70 MB). Embedded SQLite by default (or external etcd / Postgres for HA). Removes alpha features, deprecated APIs, in-tree cloud providers. Targets edge, IoT, CI, dev environments. Astonishingly featureful for its size.
- **MicroK8s**. Canonical's distribution; a snap package, single command install, opt-in addons (DNS, storage, ingress, registry).
- **k0s**. Single binary, no host dependencies, kine-backed storage option, designed for embedded / edge.
- **Talos** (Sidero Labs). An immutable Linux OS designed specifically for Kubernetes. No SSH, no shell — all management is via a gRPC API. The OS itself is a Kubernetes node and nothing else. Production-grade for people who want a hardened, immutable substrate.
- **Kind** / **K3d** / **Minikube**. Not for production; for development. Kind runs a cluster inside Docker containers (one container per node); K3d wraps K3s the same way; Minikube runs a single-node cluster in a VM or container.

### 28.5 Picking

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

## 29. Distribution Comparison Matrix

The narrative descriptions in §28 are how you'd explain distributions to someone over coffee. This table is what you reach for when you're sizing a project and need to know "who owns what, where does etcd live, what does it cost, what's exposed to me." Cross-distribution decisions are mostly made on these columns, not on feature sparkle.

| Distribution | Who runs the CP? | Where does etcd live? | Who pays for the CP? | What's exposed to you? | SLA on CP availability |
|--------------|-------------------|------------------------|----------------------|-------------------------|------------------------|
| **kubeadm (vanilla)** | You | On your CP nodes (stacked or external) | You (compute + ops time) | Everything: SSH to apiservers, etcd ports, all knobs | None (you operate it) |
| **EKS** | AWS | AWS-managed; you don't see it | AWS charges ~$73/cluster/month + node-hours | The kube-apiserver endpoint (URL); a few config knobs (audit, OIDC, encryption); no SSH, no etcd access | 99.95% (uptime SLA) |
| **GKE Standard** | Google | Google-managed | Google charges ~$73/cluster/month (free for one zonal cluster per project) + node-hours | API endpoint; broader config than EKS; tight integration with GCP IAM, logging, monitoring | 99.95% (regional) / 99.5% (zonal) |
| **GKE Autopilot** | Google | Google-managed | Per-pod billing (no node management) | Pods only; nodes are abstracted away entirely | 99.95% |
| **AKS (free tier)** | Microsoft | Azure-managed | Free for the CP, you pay for nodes | API endpoint, AAD integration, ARM integration | No SLA on free tier |
| **AKS (Uptime SLA)** | Microsoft | Azure-managed | ~$73/cluster/month + node-hours | Same as free + uptime SLA | 99.95% |
| **OpenShift (self-hosted OCP)** | You | On your CP nodes; OCP runs them as DaemonSets/Deployments | You (compute) + Red Hat subscription per node | Everything, plus OpenShift-specific layers (router, OAuth, Operators) | None (you operate; Red Hat provides support) |
| **OpenShift (ROSA / ARO)** | AWS/Azure + Red Hat | Managed by the joint service | Managed pricing | OpenShift surface + managed CP | 99.95% (ROSA) |
| **Rancher (RKE2)** | You | On your CP nodes (stacked) | You + optional Rancher support contract | Multi-cluster management UI on top; cluster-by-cluster you have full access | None (operator runs it) |
| **K3s** | You | Embedded SQLite (default), external etcd / kine (optional) | You; SUSE for support | Single binary; full root access; tight resource footprint | None |
| **Talos** | You | On the CP nodes | You; Sidero for support | gRPC API only (no SSH; no shell). Talos itself is the OS layer | None |
| **k0s** | You | etcd or kine (Postgres/MySQL/SQLite) | You; Mirantis for support | Single binary, minimal footprint, full access | None |

### 29.1 The "Hidden" Differences

A few things the table doesn't capture cleanly but you need to know:

- **Node access on managed K8s.** EKS/GKE/AKS give you SSH to *worker* nodes (and on EKS, you can disable that with bottlerocket/launch-template restrictions). You **never** SSH to control-plane nodes.
- **CP networking surface.** Managed offerings expose only the apiserver endpoint and a few related services (Konnectivity reverse tunnels are internal). On vanilla kubeadm, you choose whether the apiservers are public, private, or both.
- **What you can break.** On managed, the cloud provider validates a lot of config — you can't break the CP through misconfiguration. On vanilla, you can do anything, including making the apiserver unbootable.
- **Upgrades.** Managed: a console click or API call; the cloud rolls it. Vanilla: you run `kubeadm upgrade plan` / `apply` and drain nodes yourself. The risk profile is very different.
- **CP backup.** Managed: not your problem (mostly — some offerings let you snapshot, some don't). Vanilla: you must `etcdctl snapshot save` on a schedule and ship it somewhere durable.
- **Compliance certifications.** EKS/GKE/AKS/OpenShift come with SOC2, PCI, HIPAA paperwork. Vanilla: you do that work yourself.

### 29.2 The Honest Recommendation

For 95% of teams: **pick the managed offering of whichever cloud you're already on.** The CP-management saved time is worth the $73/month many times over, and the SLA gives you an answer to your security/SRE folks. The remaining 5% are: bare-metal, regulatory-restricted (air-gap), edge / IoT, hyperscale (you're bigger than the managed offering allows), or you have explicit reasons for owning the stack (Anthos-style multi-cloud, OpenShift for vendor support).

For learning: kubeadm or kind. For edge: K3s or Talos. For "I want a Linux distro that is a Kubernetes node and nothing else": Talos.

---

## 30. The Compatibility Skew Policy

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
