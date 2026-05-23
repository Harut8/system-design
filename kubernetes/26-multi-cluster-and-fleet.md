# Multi-Cluster and Fleet Management: A Staff-Level Deep Dive

A staff-engineer reference for the moment one Kubernetes cluster stops being enough. This chapter is to clusters what chapter 12 of the databases series is to a single database: the systematic survey of *what happens when you can no longer pretend the singleton exists*. Every assumption from chapters 03–22 — one apiserver, one etcd, one Service DNS, one set of NetworkPolicies, one scheduler — gets re-evaluated when you have N of each, possibly in different regions, possibly under different administrative trust boundaries, possibly running different Kubernetes minor versions.

This chapter sits next to chapter 23 (operators, several of which are *multi-cluster operators*), opposite chapter 25 (multi-tenancy — the in-cluster alternative to spinning up another cluster), and downstream of chapter 31 (GitOps — which is, in practice, the connective tissue of every real fleet). It reads forward into chapter 32 (lifecycle, upgrades, DR), chapter 33 (edge distributions), and chapter 34 (Karmada's scheduler as a custom-scheduler instance).

We cover the four axes of the multi-cluster problem (lifecycle, propagation, connectivity, federation), Cluster API (CAPI) as the de-facto provisioning operator, ClusterClass + Topology, the workload-propagation systems (Karmada, Rancher Fleet, Argo CD ApplicationSet, the corpse of `kubefed`), the cross-cluster networking stack (Submariner, Cilium ClusterMesh, Istio multi-primary, the MCS API), the meta-operator paradigm (Crossplane), the workspaces-instead-of-clusters paradigm (KCP), and the operational realities — secrets distribution, observability, DR, cost models, edge fleets — that determine whether a fleet design survives contact with production. A long pitfalls list closes the chapter; most of those pitfalls were learned the hard way by people you can find on the SIG-Cluster-Lifecycle and SIG-Multicluster mailing lists.

---

## Table of Contents

1.  [Why Multiple Clusters](#1-why-multiple-clusters)
2.  [The Multi-Cluster Taxonomy](#2-the-multi-cluster-taxonomy)
3.  [Cluster API (CAPI): The Provisioning Operator](#3-cluster-api-capi-the-provisioning-operator)
4.  [CAPI Core CRDs: Cluster, MachineDeployment, KubeadmControlPlane](#4-capi-core-crds-cluster-machinedeployment-kubeadmcontrolplane)
5.  [Infrastructure and Bootstrap Providers](#5-infrastructure-and-bootstrap-providers)
6.  [The Nested-Cluster Pattern and the Bootstrap Chicken-and-Egg](#6-the-nested-cluster-pattern-and-the-bootstrap-chicken-and-egg)
7.  [ClusterClass and Topology: Fleet-Wide Templates](#7-clusterclass-and-topology-fleet-wide-templates)
8.  [Workload Propagation: The Four Models](#8-workload-propagation-the-four-models)
9.  [Argo CD ApplicationSet](#9-argo-cd-applicationset)
10. [Rancher Fleet](#10-rancher-fleet)
11. [Karmada Architecture](#11-karmada-architecture)
12. [Karmada Propagation Flow](#12-karmada-propagation-flow)
13. [Karmada Scheduling: Replicas Across Clusters](#13-karmada-scheduling-replicas-across-clusters)
14. [`kubefed` and the Lessons of Federation v2](#14-kubefed-and-the-lessons-of-federation-v2)
15. [Cross-Cluster Service Discovery: The Problem](#15-cross-cluster-service-discovery-the-problem)
16. [Submariner: IPsec Tunnels + Lighthouse](#16-submariner-ipsec-tunnels--lighthouse)
17. [Cilium ClusterMesh](#17-cilium-clustermesh)
18. [Istio Multi-Primary and Primary-Remote](#18-istio-multi-primary-and-primary-remote)
19. [The Multi-Cluster Services (MCS) API](#19-the-multi-cluster-services-mcs-api)
20. [Crossplane: The Meta-Operator Paradigm](#20-crossplane-the-meta-operator-paradigm)
21. [Crossplane vs CAPI](#21-crossplane-vs-capi)
22. [KCP: Kubernetes-Like Control Plane Without Nodes](#22-kcp-kubernetes-like-control-plane-without-nodes)
23. [Hub-and-Spoke vs Mesh Topologies](#23-hub-and-spoke-vs-mesh-topologies)
24. [The Control-Plane-of-Control-Planes Problem](#24-the-control-plane-of-control-planes-problem)
25. [Cluster Identity, Trust, and OIDC Federation](#25-cluster-identity-trust-and-oidc-federation)
26. [GitOps as the Fleet Driver](#26-gitops-as-the-fleet-driver)
27. [Cluster Lifecycle: Upgrade, Scale, Drain, Decommission](#27-cluster-lifecycle-upgrade-scale-drain-decommission)
28. [Multi-Cluster Autoscaling](#28-multi-cluster-autoscaling)
29. [Multi-Cluster Secrets Distribution](#29-multi-cluster-secrets-distribution)
30. [Cost and Latency Models](#30-cost-and-latency-models)
31. [Cluster Discovery: kubeconfig, contexts, registries](#31-cluster-discovery-kubeconfig-contexts-registries)
32. [Observability Across Clusters](#32-observability-across-clusters)
33. [Disaster Recovery](#33-disaster-recovery)
34. [Hybrid and Edge Fleets](#34-hybrid-and-edge-fleets)
35. [Real-World Multi-Cluster Designs](#35-real-world-multi-cluster-designs)
36. [Pitfalls: The Long List](#36-pitfalls-the-long-list)
37. [TL;DR](#37-tldr)

---

## 1. Why Multiple Clusters

A single Kubernetes cluster is, in practice, the most expressive multi-tenant system the industry has ever shipped. It has namespaces, RBAC, NetworkPolicy, ResourceQuota, PriorityClass, PodSecurity admission, and (chapter 25) virtual clusters on top of all of it. So why does the multi-cluster industry exist?

There are six honest reasons, and an honest design rejects "multiple clusters" until at least one applies. The cost — covered in §30 — is enormous and easy to underestimate.

### 1.1 Blast radius

A cluster is a single etcd, a single apiserver fleet, a single set of controllers, a single CNI, and (until recently) a single set of admission webhooks. When any of these fails badly, *everything in the cluster goes with it*. The classic incidents:

- etcd disk fills (defrag missed, snapshots not pruned, watch backpressure, etc., chapter 04). Apiserver returns 5xx; scheduler stops; every reconcile loop in every operator stalls.
- A misconfigured mutating webhook with `failurePolicy: Fail` and a dead webhook backend. Every CREATE in the cluster fails; nothing can self-heal.
- A bad NetworkPolicy + CNI controller bug partitions pod-to-pod traffic.
- A node-image upgrade that breaks the container runtime (CRI mismatch with kubelet version skew, chapter 32).
- Someone runs `kubectl delete namespace kube-system`. Don't laugh; it happens.

In every case the only meaningful boundary that doesn't share fate is *another cluster*. Namespaces don't help — they share etcd. PriorityClasses don't help — they share the scheduler. RBAC doesn't help — it shares the apiserver. The blast-radius argument is the *strongest* argument for multi-cluster, and the one that's the hardest to argue against once your business depends on the workload.

The math is uncomfortable: if a single cluster has a P(outage/month) = p, then N independent clusters reduce the probability that *all* of them are down simultaneously to p^N. With p = 0.5% (a generous 99.5% monthly availability) and N = 3, you're at p^N = 1.25 × 10^-7 — six 9s. That math only works if the clusters truly don't share fate. Most don't, but some do (see §24).

### 1.2 Geographic distribution

Latency from Tokyo to a Virginia cluster is ~150ms RTT. For interactive traffic, that's a deal-breaker. A single Kubernetes cluster cannot span continents in any practical sense: etcd's Raft heartbeat budget (chapter 04) collapses, the scheduler's view of node distance is naïve, kube-proxy can route traffic to a pod in another continent for a Service that has endpoints worldwide. Multi-cluster gives you *one cluster per region*, with the application — or a multi-cluster service mesh — responsible for routing users to their nearest cluster.

```
GEO-DISTRIBUTED FLEET

            ┌────────┐                       ┌────────┐
   users    │  EU    │   ───── replication ──│  US-E  │   users
   (EU) ───>│cluster │ <───── async/CDC ─────│cluster │<─── (US-East)
            └────────┘                       └────────┘
                ▲                                ▲
                │                                │
                └─────── ┌────────┐ ─────────────┘
                         │  APAC  │
                  users  │cluster │ ─── users (APAC)
                  (APAC) └────────┘
```

The database in each cluster is usually a *separate* replica (chapter 12 of the databases series); cross-region writes use a globally-replicated database (Spanner, CockroachDB, YugabyteDB) that itself spans the regions independently of Kubernetes. Or you accept eventual consistency and use CDC.

### 1.3 Regulatory and data-sovereignty boundaries

GDPR says EU citizen data lives in the EU. China's PIPL says Chinese data lives in China. HIPAA says PHI lives in HIPAA-compliant environments. India's DPDP says you maintain a copy of personal data in India. Russian Federal Law 242-FZ requires Russian personal data to be stored on servers in Russia.

You cannot satisfy these with namespaces; the data has to be on hardware in the right *jurisdiction*. Multi-cluster, with one cluster per regulated region, is the only honest architecture. Inside each cluster, you can use namespaces and RBAC for the further isolation needed by compliance frameworks (PCI-DSS, FedRAMP, ISO 27001).

### 1.4 Kubernetes version diversity

Kubernetes deprecates APIs. PodSecurityPolicy → Pod Security Admission. `policy/v1beta1` PDBs → `policy/v1`. CRD `v1beta1` → `v1`. Every minor release breaks something for someone. The Kubernetes version-skew policy (chapter 32) is `+/-1 minor` between kubelet and apiserver, and *all* control-plane components must be within one minor of each other; you can't keep a 1.24 apiserver running while you slowly migrate to 1.30.

So if you have a workload that depends on a behavior that was removed in 1.27, and a workload that requires a feature added in 1.30, those workloads live in different clusters. Period.

### 1.5 Hard multi-tenancy

Chapter 25 makes the argument: namespaces are a *soft* boundary. Anyone with cluster-admin (and anyone capable of exploiting a container escape) can reach anything in any namespace. For genuinely adversarial tenants — a SaaS where one tenant must never see another tenant's data, even if a CVE drops — the only safe boundary is a separate cluster. Or a separate VM. Or a separate physical machine.

This is "cluster-per-tenant" architecture. It's expensive (§30) and operationally heavy, but it's the only design that holds up to a *zero-day in containerd*. The same logic drove the design of vCluster (chapter 25) as a cheaper-than-real-cluster compromise.

### 1.6 Scaling limits

A single Kubernetes cluster has hard ceilings (the SIG-Scalability SLOs, chapter 35):

| Metric | Ceiling (well-tuned, 1.30) |
|---|---|
| Nodes | 5,000 (default scheduler), 15,000 with tuning |
| Pods per cluster | 150,000 |
| Pods per node | 110 (default), 250 (configurable) |
| Services | ~10,000 before EndpointSlices dominate |
| API request QPS | ~3000/s sustained with APF |

If your workload exceeds these — Google internal Borg cells are explicitly cited as the reason Kubernetes capped at ~5000 nodes — you have no choice but to shard across clusters. Most teams hit a *soft* ceiling much earlier: at ~1000 nodes, the etcd watch fan-out, the controller-manager work queues, and the scheduler's cache invalidation all start to wobble (chapter 04, 08, 09).

### 1.7 When *not* to go multi-cluster

A few teams reach for multi-cluster too early:

- "Each team gets a cluster." If you have 30 teams and they cooperate on shared platform infrastructure, you almost certainly want one big cluster with namespaces and RBAC, not 30 small clusters with their own etcd, their own controllers, their own observability, their own bills. Chapter 25 is the answer here.
- "Each environment gets a cluster." Prod / staging / dev as separate clusters is reasonable for blast-radius. *Per-feature* clusters are not — use namespaces (or vCluster) for ephemeral environments.
- "Each microservice gets a cluster." This is cargo-cult. A service is a workload, not a control plane.

The general rule: multi-cluster is for *boundaries that the cluster boundary cannot fake*. If a namespace boundary suffices, use a namespace.

---

## 2. The Multi-Cluster Taxonomy

The "multi-cluster" word covers four orthogonal problems. A given tool addresses one or two of them; no tool addresses all four. Confusing the axes is the most common mistake.

```
┌────────────────────────────────────────────────────────────────────────┐
│                  Four Axes of the Multi-Cluster Problem                 │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  AXIS 1: LIFECYCLE                                                     │
│    "How do I create, upgrade, scale, destroy clusters?"                │
│    Tools: Cluster API, Crossplane (for clusters), eksctl, gke,         │
│           hcloud-k8s, Talos, kubeadm                                   │
│                                                                        │
│  AXIS 2: WORKLOAD PROPAGATION                                          │
│    "How do I deploy this Deployment to all 50 clusters?"               │
│    Tools: Argo CD ApplicationSet, Flux + clusters, Rancher Fleet,      │
│           Karmada, kubefed (deprecated)                                │
│                                                                        │
│  AXIS 3: SERVICE DISCOVERY / CONNECTIVITY                              │
│    "Pod-in-cluster-A talks to pod-in-cluster-B"                        │
│    Tools: Submariner, Cilium ClusterMesh, Istio multi-primary,         │
│           Linkerd multi-cluster, AWS VPC Lattice, GCP MCS              │
│                                                                        │
│  AXIS 4: FEDERATION OF RESOURCES                                       │
│    "Single API surface over many clusters"                             │
│    Tools: KCP (workspaces), kubefed v2 (deprecated), Open Cluster      │
│           Management (OCM), Karmada (partial)                          │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

A real fleet usually combines:

```
   Cluster API           ──── creates clusters
   Argo CD ApplicationSet ─── deploys workloads
   Cilium ClusterMesh    ──── connects pods
   External-Secrets      ──── distributes secrets
   Thanos                ──── unifies metrics
   Velero                ──── backs everything up
   GitOps (Flux/Argo)    ──── is the source of truth
```

Pick a tool *per axis*. Don't try to make CAPI propagate workloads (it doesn't), don't try to make Karmada provision clusters (it doesn't), and don't try to make Submariner do federation (it doesn't).

### 2.1 Why no unified tool

It's tempting to ask why nobody's built "the one multi-cluster system". They've tried — `kubefed` was the most prominent attempt, and §14 covers why it didn't work. The honest answer is that the four axes have wildly different operational profiles:

- Lifecycle is *infrastructure*: cloud APIs, certs, base images, kubeadm bootstrapping. Reconcile cadence is minutes-to-hours.
- Propagation is *application*: Helm charts, Kustomize overlays, drift detection. Reconcile cadence is seconds-to-minutes.
- Connectivity is *network plumbing*: BGP, IPsec, eBPF, service meshes. Reconcile cadence is event-driven, in milliseconds.
- Federation is *API surface*: type aggregation, schema versioning, cross-cluster authz. The right reconcile cadence is "never reconcile, it's a read-through proxy".

These don't fit in one operator. The fact that you need *separate tools* per axis is the design, not a failure.

---

## 3. Cluster API (CAPI): The Provisioning Operator

Cluster API is "Kubernetes-style provisioning *of* Kubernetes clusters". It's a SIG-Cluster-Lifecycle project (`kubernetes-sigs/cluster-api`), the de-facto standard for declarative cluster lifecycle on every infrastructure that's not a vendor-specific managed service (and even some that are — EKS, AKS, and GKE all have CAPI providers, though most teams use the vendor CLI).

The core insight: a Kubernetes cluster is *itself* a resource that can be reconciled by a Kubernetes controller, if you put that controller in a *different* (management) cluster. Once you accept that, the entire chapter-08 controller pattern applies to clusters themselves: declarative spec, observed status, reconcile loop, finalizers, owner references, the works.

```
CLUSTER API: THE MANAGEMENT CLUSTER PROVISIONS WORKLOAD CLUSTERS

   ┌────────────────────────────────────────────────────────────┐
   │              MANAGEMENT CLUSTER                            │
   │  (often a small kind/k3d/EKS cluster, runs CAPI itself)    │
   │                                                            │
   │  ┌───────────────────┐  ┌────────────────────────────┐    │
   │  │ capi-controller-  │  │ Infrastructure provider:   │    │
   │  │   manager         │  │  cluster-api-provider-aws  │    │
   │  │ (cluster.x-k8s.io)│  │  cluster-api-provider-gcp  │    │
   │  └─────────┬─────────┘  │  cluster-api-provider-     │    │
   │            │            │   vsphere                  │    │
   │            │            │  ...                       │    │
   │            │            └─────────────┬──────────────┘    │
   │            │                          │                   │
   │            ▼                          ▼                   │
   │   ┌──────────────────────────────────────────────┐        │
   │   │  Cluster CR, MachineDeployment CR,           │        │
   │   │  KubeadmControlPlane CR, AWSCluster CR,      │        │
   │   │  AWSMachineTemplate CR, ...                  │        │
   │   └──────────────────────────────────────────────┘        │
   └───────────────────┬────────────────────────────────────────┘
                       │ creates EC2 instances, ELBs,
                       │ VPC routes, joins kubeadm, etc.
                       ▼
   ┌────────────────────────────────────────────────────────────┐
   │            WORKLOAD CLUSTER #1                             │
   │            (a real, separate K8s cluster)                  │
   │  control plane (3 nodes), workers (N nodes),               │
   │  its own etcd, its own apiserver, its own CNI              │
   └────────────────────────────────────────────────────────────┘

   ┌────────────────────────────────────────────────────────────┐
   │            WORKLOAD CLUSTER #2  ...                        │
   └────────────────────────────────────────────────────────────┘
```

CAPI is, by design, *not* opinionated about:

- What runs in your workload clusters (it's none of CAPI's business; that's chapter 31's job).
- What CNI you use (you install one after CAPI brings up the cluster).
- What cloud you're on (the InfrastructureProvider abstracts that).
- What bootstrap tool you use (the BootstrapProvider abstracts that — Kubeadm is the default, but RKE2, Talos, MicroK8s all exist).

This separation is what makes CAPI work where federation v2 didn't: it solves *one* problem (lifecycle) and refuses to solve the others.

### 3.1 The pieces

The CAPI core CRDs live under the `cluster.x-k8s.io` group:

- `Cluster` — the top-level CR.
- `Machine`, `MachineSet`, `MachineDeployment` — the node abstractions, intentionally mirroring `Pod`/`ReplicaSet`/`Deployment`.
- `MachineHealthCheck` — node-level liveness, like a probe for nodes.
- `MachinePool` — for cloud-native node groups (AWS ASGs, GCP MIGs, Azure VMSS) where the cloud, not CAPI, decides node counts.
- `KubeadmControlPlane` — the control plane (kubeadm-managed; SIG-CL has alternatives in `controlplane/*`).
- `ClusterClass`, `ClusterResourceSet` — fleet-wide templating and add-on installation.

Each is paired with infrastructure-specific equivalents:

- `AWSCluster`, `AWSMachineTemplate`, `AWSMachineDeployment` (from `cluster-api-provider-aws`)
- `GCPCluster`, `GCPMachineTemplate` (`cluster-api-provider-gcp`)
- `VSphereCluster`, `VSphereMachineTemplate` (`cluster-api-provider-vsphere`)
- `AzureCluster`, `AzureMachineTemplate` (`cluster-api-provider-azure`)
- `Metal3Cluster`, `Metal3MachineTemplate` (`cluster-api-provider-metal3`, for bare-metal)

The pattern is consistent: the *generic* CR holds the desired state, the *infra* CR holds the cloud-specific implementation, and a reference connects them.

---

## 4. CAPI Core CRDs: Cluster, MachineDeployment, KubeadmControlPlane

Here is a real CAPI definition of a small AWS workload cluster — control plane of 3 nodes, two worker pools. Read this slowly; the layering is the whole point.

### 4.1 The `Cluster` CR

```yaml
apiVersion: cluster.x-k8s.io/v1beta1
kind: Cluster
metadata:
  name: prod-eu-west-1
  namespace: clusters
spec:
  clusterNetwork:
    pods:
      cidrBlocks:
        - 10.244.0.0/16
    services:
      cidrBlocks:
        - 10.96.0.0/12
    serviceDomain: cluster.local
  controlPlaneRef:
    apiVersion: controlplane.cluster.x-k8s.io/v1beta1
    kind: KubeadmControlPlane
    name: prod-eu-west-1-cp
  infrastructureRef:
    apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
    kind: AWSCluster
    name: prod-eu-west-1
```

The `Cluster` is a *coordinator*: it points at a `controlPlaneRef` (the actual control plane, which is provider-agnostic — `KubeadmControlPlane`, `RKE2ControlPlane`, `TalosControlPlane`) and an `infrastructureRef` (the cloud-specific resources — VPC, subnets, security groups, the load balancer in front of the control plane).

The `Cluster` CR does *not* itself create anything. Its controller waits for the infra and control-plane controllers to report `Ready=True`, then aggregates the status.

### 4.2 The `AWSCluster` CR

```yaml
apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
kind: AWSCluster
metadata:
  name: prod-eu-west-1
  namespace: clusters
spec:
  region: eu-west-1
  sshKeyName: capi-deploy
  network:
    vpc:
      cidrBlock: 10.0.0.0/16
    subnets:
      - cidrBlock: 10.0.0.0/20
        availabilityZone: eu-west-1a
      - cidrBlock: 10.0.16.0/20
        availabilityZone: eu-west-1b
      - cidrBlock: 10.0.32.0/20
        availabilityZone: eu-west-1c
  controlPlaneLoadBalancer:
    scheme: internet-facing
    healthCheckProtocol: HTTPS
```

The `cluster-api-provider-aws` controller reconciles this into a real VPC, three subnets, an NLB for the apiserver, route tables, security groups, IAM roles. The actual API calls happen via AWS-SDK calls inside that controller's reconcile loop.

### 4.3 The `KubeadmControlPlane` CR

```yaml
apiVersion: controlplane.cluster.x-k8s.io/v1beta1
kind: KubeadmControlPlane
metadata:
  name: prod-eu-west-1-cp
  namespace: clusters
spec:
  replicas: 3
  version: v1.30.4
  machineTemplate:
    infrastructureRef:
      apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
      kind: AWSMachineTemplate
      name: prod-eu-west-1-cp
  kubeadmConfigSpec:
    clusterConfiguration:
      apiServer:
        extraArgs:
          audit-log-maxage: "30"
          audit-log-maxbackup: "10"
          audit-log-maxsize: "100"
          audit-log-path: /var/log/audit.log
          enable-admission-plugins: NodeRestriction,ResourceQuota,PodSecurity
      controllerManager:
        extraArgs:
          cloud-provider: external
      etcd:
        local:
          dataDir: /var/lib/etcd
          extraArgs:
            quota-backend-bytes: "8589934592"
    initConfiguration:
      nodeRegistration:
        kubeletExtraArgs:
          cloud-provider: external
    joinConfiguration:
      nodeRegistration:
        kubeletExtraArgs:
          cloud-provider: external
  rolloutStrategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
```

This is *the* control plane spec: 3 replicas, kubeadm-managed, version 1.30.4, with stacked etcd (`etcd.local`), with the cloud-provider configured for an external CCM (chapter 37). Rolling updates with `maxSurge: 1` — one extra control-plane node at a time, then drain the old one.

`KubeadmControlPlane`'s reconcile loop is one of the most complex in the CAPI ecosystem: it has to bootstrap the *first* control-plane node (running `kubeadm init`), then join subsequent nodes (`kubeadm join --control-plane`), wait for etcd to converge, manage certs, handle the apiserver advertise address, and rotate the control plane on version changes. The kubeadm bootstrap data (cloud-init / Ignition) is generated and stored in a `Secret` referenced by the BootstrapProvider; the InfrastructureProvider then picks it up and feeds it to the cloud's user-data mechanism.

### 4.4 The `AWSMachineTemplate` for the control plane

```yaml
apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
kind: AWSMachineTemplate
metadata:
  name: prod-eu-west-1-cp
  namespace: clusters
spec:
  template:
    spec:
      instanceType: m6i.xlarge
      iamInstanceProfile: control-plane.cluster-api-provider-aws.sigs.k8s.io
      sshKeyName: capi-deploy
      ami:
        id: ami-0123456789abcdef0  # custom Kubernetes AMI built by image-builder
      rootVolume:
        size: 100
        type: gp3
```

The template is immutable — to change instance type or AMI you create a new template and update the `KubeadmControlPlane.spec.machineTemplate.infrastructureRef` to point at it. CAPI then performs a rolling replacement.

### 4.5 The `MachineDeployment` for workers

```yaml
apiVersion: cluster.x-k8s.io/v1beta1
kind: MachineDeployment
metadata:
  name: prod-eu-west-1-md-general
  namespace: clusters
spec:
  clusterName: prod-eu-west-1
  replicas: 10
  selector:
    matchLabels:
      cluster.x-k8s.io/cluster-name: prod-eu-west-1
      pool: general
  template:
    metadata:
      labels:
        cluster.x-k8s.io/cluster-name: prod-eu-west-1
        pool: general
    spec:
      clusterName: prod-eu-west-1
      version: v1.30.4
      bootstrap:
        configRef:
          apiVersion: bootstrap.cluster.x-k8s.io/v1beta1
          kind: KubeadmConfigTemplate
          name: prod-eu-west-1-general
      infrastructureRef:
        apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
        kind: AWSMachineTemplate
        name: prod-eu-west-1-general
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 2
      maxUnavailable: 0
```

Note the deliberate symmetry with `Deployment`:

| Workload object (in cluster) | CAPI object (about cluster) |
|---|---|
| `Pod` | `Machine` |
| `ReplicaSet` | `MachineSet` |
| `Deployment` | `MachineDeployment` |
| `pod.spec.containers[0].image` | `Machine.spec.infrastructureRef → AWSMachine.spec.ami` |
| rolling update via `RollingUpdate` | rolling replacement via `RollingUpdate` |

If you understand `Deployment`, you understand `MachineDeployment`. The same `revision`/`maxSurge`/`maxUnavailable` semantics apply. The difference is that "rolling out" a machine takes 3–8 minutes (EC2 boot + kubeadm join + node Ready), whereas a Pod takes 5 seconds.

### 4.6 `MachineHealthCheck`

```yaml
apiVersion: cluster.x-k8s.io/v1beta1
kind: MachineHealthCheck
metadata:
  name: prod-eu-west-1-mhc
  namespace: clusters
spec:
  clusterName: prod-eu-west-1
  selector:
    matchLabels:
      pool: general
  unhealthyConditions:
    - type: Ready
      status: Unknown
      timeout: 300s
    - type: Ready
      status: "False"
      timeout: 300s
  maxUnhealthy: 40%
```

This is the equivalent of a liveness probe for nodes. If a `Machine`'s underlying `Node` has been `NotReady` for 5 minutes, the MHC marks the machine for replacement: cordon, drain, delete the EC2 instance, the MachineSet brings up a fresh one. `maxUnhealthy` is a circuit breaker: if too many nodes are bad simultaneously (likely a cluster-wide problem, not individual node failures), MHC stops replacing them to avoid making it worse.

### 4.7 The reconcile graph

These objects reference each other in a specific order:

```
   Cluster ─────────────────────────────────────────┐
     │                                              │
     ├──► AWSCluster   (creates VPC, subnets, NLB)  │
     │                                              │
     ├──► KubeadmControlPlane                       │
     │      │                                       │
     │      ├──► Machine #1 (cp)                    │
     │      │      └─► AWSMachine #1                │
     │      │             └─► EC2 instance          │
     │      │      └─► KubeadmConfig #1             │
     │      │             └─► bootstrap Secret      │
     │      │                  (cloud-init)         │
     │      ├──► Machine #2 (cp)                    │
     │      └──► Machine #3 (cp)                    │
     │                                              │
     └──► MachineDeployment                         │
            └──► MachineSet                         │
                  ├──► Machine (worker) ×N          │
                  └──► [each → AWSMachine,          │
                        KubeadmConfig, EC2]         │
```

The owner references run *down* the tree. Deleting the `Cluster` cascades all the way through to deleting EC2 instances. Finalizers ensure that cloud resources are cleaned up before the CRs disappear — drop a finalizer too early and you leak NLBs.

---

## 5. Infrastructure and Bootstrap Providers

CAPI's pluggability comes from two interfaces.

### 5.1 InfrastructureProvider

Implements: how to turn an abstract `Machine` into a real compute instance, and how to manage the cluster-level infrastructure (network, load balancer).

Required CRDs per provider:

- `<Infra>Cluster` (e.g., `AWSCluster`, `GCPCluster`)
- `<Infra>ClusterTemplate` (for ClusterClass)
- `<Infra>Machine`, `<Infra>MachineTemplate`
- `<Infra>MachinePool` (optional)

Required behavior: the provider's reconciler reads `cluster.spec.infrastructureRef` → `<Infra>Cluster`, provisions the network, sets `<Infra>Cluster.status.ready = true` and writes the apiserver endpoint into `<Infra>Cluster.status.controlPlaneEndpoint`. CAPI's main controller sees this and proceeds.

Production-grade providers:

| Provider | Repo | Notes |
|---|---|---|
| AWS | `kubernetes-sigs/cluster-api-provider-aws` | Most mature; supports EKS via `AWSManagedControlPlane` |
| GCP | `kubernetes-sigs/cluster-api-provider-gcp` | Supports GKE via `GCPManagedControlPlane` |
| Azure | `kubernetes-sigs/cluster-api-provider-azure` | Supports AKS via `AzureManagedControlPlane` |
| vSphere | `kubernetes-sigs/cluster-api-provider-vsphere` | The on-prem workhorse |
| OpenStack | `kubernetes-sigs/cluster-api-provider-openstack` | Telecom, sovereign clouds |
| Metal3 | `metal3-io/cluster-api-provider-metal3` | Bare-metal via Ironic (Redfish, IPMI) |
| Hetzner | `syself/cluster-api-provider-hetzner` | EU-friendly, cheap |
| Hivelocity, Equinix, etc. | various | Bare-metal-as-a-service |
| Docker | `kubernetes-sigs/cluster-api-provider-docker` | For testing only; runs nodes as containers |

The Docker provider is the secret weapon for CAPI development. You can spin up a full CAPI test cluster on your laptop in `kind`, with workload clusters running as Docker containers, in 30 seconds.

### 5.2 BootstrapProvider

Implements: how to turn a `Machine` into a configured Kubernetes node. Generates the cloud-init / Ignition / Talos-config / RKE2-config that the InfrastructureProvider feeds to the instance.

Production-grade bootstrap providers:

| Provider | Repo | Notes |
|---|---|---|
| Kubeadm | `kubernetes-sigs/cluster-api` (bundled) | The default; uses `kubeadm init/join` |
| RKE2 | `rancher/cluster-api-provider-rke2` | Rancher's hardened distro; single-binary |
| MicroK8s | `canonical/cluster-api-bootstrap-provider-microk8s` | Canonical's small-K8s distro |
| Talos | `siderolabs/cluster-api-bootstrap-provider-talos` | Immutable, API-driven OS; very secure |
| K3s | `cluster-api-provider-k3s/cluster-api-k3s` | Edge / IoT distro |

Talos is particularly interesting in the CAPI context: there's no SSH, no shell, no package manager on a Talos node. The bootstrap provider produces a Talos-machine-config (a single YAML), the infra provider boots a Talos AMI, and the cluster is up in 2 minutes. There's nothing to drift, nothing to ssh into, nothing to patch with apt. This is the future of immutable node OSes for production fleets.

### 5.3 ControlPlaneProvider

Less commonly customized, but exists as a third interface. `KubeadmControlPlane` is the reference implementation; `TalosControlPlane`, `RKE2ControlPlane`, `EKSControlPlane`, `AKSControlPlane`, `GKEControlPlane` (`AKS/GKE/EKSManagedControlPlane`) are the production alternatives.

The managed-K8s control-plane providers (EKS / AKS / GKE) are interesting: there's no `Machine` for the control plane because the cloud manages it. The provider just calls the cloud's API (`CreateCluster` etc.) and reports status. CAPI then provisions the worker MachineDeployments normally.

---

## 6. The Nested-Cluster Pattern and the Bootstrap Chicken-and-Egg

The CAPI pattern requires a *management cluster*: a Kubernetes cluster that runs the CAPI controllers and holds all the Cluster/Machine CRs. The workload clusters created by CAPI are completely separate clusters with their own etcd, their own apiserver, their own everything.

```
   ┌─────────────────────────────────────────────────────────────┐
   │                  MANAGEMENT CLUSTER                          │
   │  (small, 3 nodes, runs only CAPI controllers + a few addons) │
   │                                                              │
   │   apiserver: holds the source-of-truth CRs                   │
   │   etcd: holds Cluster, Machine, MachineDeployment for ALL    │
   │         workload clusters (this is precious data)            │
   │   controllers:                                               │
   │     - capi-controller-manager                                │
   │     - cluster-api-provider-aws (CAPA)                        │
   │     - cluster-api-bootstrap-provider-kubeadm                 │
   │     - cluster-api-controlplane-provider-kubeadm              │
   └─────────────┬──────────────────────────────────────┬─────────┘
                 │                                      │
                 ▼                                      ▼
   ┌────────────────────────┐              ┌────────────────────────┐
   │  WORKLOAD CLUSTER A    │              │  WORKLOAD CLUSTER B    │
   │  (production)          │              │  (staging)             │
   │  3 cp + 50 workers     │              │  3 cp + 10 workers     │
   │  own etcd, own apiserver, own CNI, own workloads             │
   └────────────────────────┘              └────────────────────────┘
```

### 6.1 The chicken-and-egg problem

Where does the *first* management cluster come from? It can't be CAPI-provisioned; CAPI hasn't been installed yet.

CAPI solves this with a documented **pivot** workflow:

1. Spin up a temporary "bootstrap cluster" — usually `kind` on your laptop, or a tiny single-node k3s on a VM. This takes 30 seconds.
2. `clusterctl init` installs CAPI + the chosen providers into the bootstrap cluster.
3. `clusterctl generate cluster mgmt --kubernetes-version v1.30.4 --control-plane-machine-count 3 --worker-machine-count 3 | kubectl apply -f -` — CAPI in the bootstrap cluster creates a new cluster called `mgmt`. The bootstrap cluster is the parent.
4. Once the `mgmt` cluster is up, install CAPI *inside* the `mgmt` cluster: `clusterctl init` against the `mgmt` kubeconfig.
5. **Pivot**: `clusterctl move --to-kubeconfig=mgmt.kubeconfig`. This is the magical step. It walks the resource graph in the bootstrap cluster, recreates every CAPI CR (with finalizers, annotations preserved) in the `mgmt` cluster, then deletes the originals from the bootstrap cluster. The mgmt cluster now manages itself.
6. Tear down the `kind` bootstrap cluster. The mgmt cluster persists, and now manages all future workload clusters.

```
   STEP 1-2: bootstrap (kind) cluster
   ┌──────────────┐
   │ kind cluster │ ◄── CAPI installed here
   └──────────────┘

   STEP 3-4: kind creates mgmt cluster
   ┌──────────────┐
   │ kind cluster │ ──► creates ──► ┌──────────────┐
   │ (parent)     │                 │ mgmt cluster │
   └──────────────┘                 │ + CAPI       │
                                    └──────────────┘

   STEP 5: pivot
   ┌──────────────┐                 ┌──────────────┐
   │ kind cluster │     CRs ──►     │ mgmt cluster │
   │ (parent)     │     move        │ (now manages │
   │              │                 │  itself)     │
   └──────────────┘                 └──────────────┘

   STEP 6: delete kind
                                    ┌──────────────┐
                                    │ mgmt cluster │
                                    │ (standalone) │
                                    └──────────────┘
```

The pivot is a stateful migration; if it fails partway through (network blip, controller crash) you have CRs in *both* clusters and a delicate cleanup. Best practice: do the pivot from a stable network, with the bootstrap cluster on the same VPC as the mgmt cluster.

### 6.2 What if the management cluster dies?

You have an etcd backup of the management cluster (chapter 32). Restore it to a fresh cluster, the CAPI controllers reconcile, and they discover that the workload clusters already exist and are healthy (status comes from real cloud queries). Nothing dies — but you cannot *create new* workload clusters until the management cluster is back.

This is the **control-plane-of-control-planes** problem (§24). The management cluster's etcd is the most precious data you have, because losing it means manually re-importing every workload cluster's state, which is a multi-week incident.

### 6.3 Self-managed cluster

A particularly clean pattern: the management cluster is *also* a workload cluster managed by itself. The pivot creates a `Cluster` CR for `mgmt` that points at the mgmt cluster's own infrastructure. From then on, you upgrade and scale the management cluster the same way you'd upgrade any other CAPI cluster: edit `KubeadmControlPlane.spec.version`. This is the recommended pattern for production CAPI.

---

## 7. ClusterClass and Topology: Fleet-Wide Templates

ClusterClass + Topology (often called "managed topologies") is CAPI's answer to the YAML-explosion problem. Without it, every workload cluster needs its own `Cluster`, `<Infra>Cluster`, `KubeadmControlPlane`, `MachineDeployment`, `<Infra>MachineTemplate`, `KubeadmConfigTemplate` — six CRs per cluster, with lots of copy-paste. With 50 clusters, that's 300 YAML files, all of which must drift-update in lockstep when you change AMI or Kubernetes version.

ClusterClass is a *template*. Cluster topology is a *thin reference* to that template plus per-cluster variables.

### 7.1 The ClusterClass

```yaml
apiVersion: cluster.x-k8s.io/v1beta1
kind: ClusterClass
metadata:
  name: aws-prod-class
  namespace: clusters
spec:
  controlPlane:
    metadata: {}
    ref:
      apiVersion: controlplane.cluster.x-k8s.io/v1beta1
      kind: KubeadmControlPlaneTemplate
      name: aws-prod-cp-template
    machineInfrastructure:
      ref:
        apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
        kind: AWSMachineTemplate
        name: aws-prod-cp-machine
  infrastructure:
    ref:
      apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
      kind: AWSClusterTemplate
      name: aws-prod-cluster-template
  workers:
    machineDeployments:
      - class: general
        template:
          bootstrap:
            ref:
              apiVersion: bootstrap.cluster.x-k8s.io/v1beta1
              kind: KubeadmConfigTemplate
              name: aws-prod-general-bootstrap
          infrastructure:
            ref:
              apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
              kind: AWSMachineTemplate
              name: aws-prod-general-machine
  variables:
    - name: region
      required: true
      schema:
        openAPIV3Schema:
          type: string
    - name: workerCount
      required: true
      schema:
        openAPIV3Schema:
          type: integer
          minimum: 1
          maximum: 100
    - name: kubernetesVersion
      required: true
      schema:
        openAPIV3Schema:
          type: string
          pattern: "^v1\\.\\d+\\.\\d+$"
  patches:
    - name: region
      definitions:
        - selector:
            apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
            kind: AWSClusterTemplate
            matchResources:
              infrastructureCluster: true
          jsonPatches:
            - op: replace
              path: /spec/template/spec/region
              valueFrom:
                variable: region
```

### 7.2 The per-cluster Cluster CR using a Topology

```yaml
apiVersion: cluster.x-k8s.io/v1beta1
kind: Cluster
metadata:
  name: prod-eu-west-1
spec:
  clusterNetwork:
    pods:    { cidrBlocks: [10.244.0.0/16] }
    services:{ cidrBlocks: [10.96.0.0/12] }
  topology:
    class: aws-prod-class
    version: v1.30.4
    controlPlane:
      replicas: 3
    workers:
      machineDeployments:
        - class: general
          name: general
          replicas: 10
    variables:
      - name: region
        value: eu-west-1
      - name: workerCount
        value: 10
      - name: kubernetesVersion
        value: v1.30.4
```

That's it. One CR per cluster. CAPI's topology controller expands the ClusterClass + variables into all six underlying CRs, applies the JSON patches per cluster, and reconciles. A fleet of 50 clusters becomes 50 `Cluster` CRs.

When you change the ClusterClass — for example, switching to a newer AMI — every Cluster that references it begins a rolling replacement, *in lockstep*. You get fleet-wide rollouts for free, controlled by ClusterResourceSet pause annotations and by phased rollout patterns implemented at the GitOps layer.

### 7.3 What ClusterClass doesn't solve

ClusterClass templates *infrastructure*. It doesn't template:

- Workloads (that's chapter 31's GitOps).
- Add-ons (cert-manager, ingress, CSI drivers — see ClusterResourceSet or the new ClusterClass `runtimeExtensions` for hooks; in practice everyone uses Argo CD ApplicationSet).
- Cluster-specific config (kubeconfig generation, OIDC trust, etc.).

A real fleet uses ClusterClass for "what is this cluster's shape" and GitOps for "what runs on it".

---

## 8. Workload Propagation: The Four Models

Once you have N clusters, the question becomes: how do I get the same workload — `Deployment frontend`, `Service frontend`, `ConfigMap config` — into each? There are four production-grade models, and they make different tradeoffs.

```
                         WORKLOAD PROPAGATION MODELS
   ┌───────────────────────────────────────────────────────────────────┐
   │                                                                   │
   │  MODEL 1: GITOPS-PER-CLUSTER (Argo CD ApplicationSet, Flux)       │
   │   ┌────────┐    ┌────────┐    ┌────────┐                          │
   │   │ Git    │ ─► │ ArgoCD │ ─► │ N appsets, one per cluster        │
   │   │ repo   │    │ in mgmt│    │ each pushes to one cluster        │
   │   └────────┘    └────────┘                                        │
   │   Each cluster gets its own rendered manifest; no "federation".   │
   │                                                                   │
   │  MODEL 2: FLEET-NATIVE (Rancher Fleet)                            │
   │   ┌────────┐    ┌────────┐    ┌────────┐                          │
   │   │ Git    │ ─► │ Fleet  │ ─► │ Bundles to ClusterGroups          │
   │   │ repo   │    │ ctrl   │    │ (canary rollout built-in)         │
   │   └────────┘    └────────┘                                        │
   │                                                                   │
   │  MODEL 3: FEDERATION-STYLE (Karmada)                              │
   │   ┌────────┐    ┌──────────────┐                                  │
   │   │ User   │ ─► │ karmada-     │ ─► PropagationPolicy schedules   │
   │   │ kubectl│    │ apiserver    │     to N member clusters         │
   │   └────────┘    └──────────────┘     with replica weighting       │
   │                                                                   │
   │  MODEL 4: API-SURFACE (KCP workspaces, kubefed v2 — deprecated)   │
   │   ┌────────┐    ┌──────────────┐                                  │
   │   │ User   │ ─► │ KCP          │ ─► syncer per cluster pulls      │
   │   │ kubectl│    │ workspace    │     APIBinding'd resources       │
   │   └────────┘    └──────────────┘                                  │
   └───────────────────────────────────────────────────────────────────┘
```

The right model depends on:

- **Symmetric vs asymmetric workloads.** Do all clusters get exactly the same thing (symmetric — ApplicationSet, Fleet, kubefed) or do clusters get different shares of replicas (asymmetric — Karmada)?
- **Drift behavior.** What happens when someone edits the workload directly in a cluster? (GitOps overwrites it; Karmada overwrites it; kubefed used to fight back endlessly.)
- **Single API surface.** Do you want `kubectl get pods --all-clusters` to work? (Karmada partial, KCP yes, GitOps no.)
- **Cluster auto-discovery.** Do new clusters automatically get all the workloads? (ApplicationSet yes via Cluster generator; Fleet yes via ClusterGroup labels; Karmada yes via ClusterPropagationPolicy.)

### 8.1 The push vs pull spectrum

Orthogonal to the model is the question of who initiates the propagation:

- **Push** — the mgmt cluster has a controller that holds kubeconfigs to all member clusters and calls their apiservers directly. Karmada, ArgoCD (default), kubefed worked this way. Simple but requires the mgmt cluster to have network access to all members.
- **Pull** — the member cluster has an agent that reaches *out* to the mgmt cluster, polls/watches for what it should run. Fleet, ArgoCD-agent (newer), Flux (when configured with multi-cluster), Open Cluster Management's klusterlet. Works through firewalls; required for air-gapped or edge.

A modern fleet often mixes: ArgoCD in pull mode for "the central fleet" (where mgmt sees all), Flux on each member for "agent that pulls Git" (where each cluster is its own root of trust).

---

## 9. Argo CD ApplicationSet

ApplicationSet is the most widely deployed workload-propagation pattern. It's an Argo CD controller (`argo-cd/applicationset-controller`) that generates `Application` CRs from a template + a generator. Each generated `Application` is just a normal Argo CD `Application`, so you get all of ArgoCD's machinery: sync waves, hooks, drift detection, RBAC, the UI.

### 9.1 The shape

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: frontend
  namespace: argocd
spec:
  generators:
    - clusters:
        selector:
          matchLabels:
            env: prod
  template:
    metadata:
      name: 'frontend-{{name}}'
    spec:
      project: frontend
      source:
        repoURL: https://github.com/acme/k8s-manifests
        targetRevision: main
        path: 'apps/frontend/overlays/{{metadata.labels.region}}'
      destination:
        server: '{{server}}'
        namespace: frontend
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
        syncOptions:
          - CreateNamespace=true
```

The `clusters` generator iterates over every Cluster Secret in the `argocd` namespace that matches the label selector (Argo CD represents each cluster as a Secret with type `cluster`, containing the kubeconfig + name + server URL). For each one, it instantiates the template, substituting `{{name}}`, `{{server}}`, and any cluster labels into the template. The output: N `Application` CRs, one per matching cluster.

### 9.2 The generators

ApplicationSet supports several generators that compose well:

| Generator | What it iterates over |
|---|---|
| `list` | Static list of values (for early prototypes) |
| `clusters` | Argo CD's known clusters (Secrets) |
| `git` | Directories or files in a Git repo |
| `scmProvider` | All repos in a GitHub/GitLab org |
| `pullRequest` | Open PRs (for preview environments) |
| `matrix` | Cartesian product of two generators |
| `merge` | Inner-join of two generators by a key |
| `clusterDecisionResource` | A CR that says "these clusters" (for custom scheduling) |
| `plugin` | A custom HTTP plugin |

The `matrix` generator is the magic for multi-environment fleets. Iterate the cross product of {regions} × {apps} to generate one Application per (region, app) pair:

```yaml
spec:
  generators:
    - matrix:
        generators:
          - clusters:
              selector:
                matchLabels:
                  env: prod
          - git:
              repoURL: https://github.com/acme/k8s-manifests
              revision: main
              directories:
                - path: apps/*
  template:
    metadata:
      name: '{{path.basename}}-{{name}}'
    spec:
      source:
        repoURL: https://github.com/acme/k8s-manifests
        targetRevision: main
        path: 'apps/{{path.basename}}/overlays/{{metadata.labels.region}}'
      destination:
        server: '{{server}}'
        namespace: '{{path.basename}}'
      ...
```

For 5 clusters × 30 apps, you now have 150 `Application` CRs, generated by 1 ApplicationSet, kept up to date as either side changes.

### 9.3 Progressive rollouts

ApplicationSet itself doesn't roll out across clusters one-at-a-time; for that you use the **Progressive Syncs** feature (or its successor in newer ArgoCD versions). You order clusters into waves:

```yaml
spec:
  strategy:
    type: RollingSync
    rollingSync:
      steps:
        - matchExpressions:
            - key: env
              operator: In
              values: [canary]
        - matchExpressions:
            - key: env
              operator: In
              values: [staging]
          maxUpdate: 100%
        - matchExpressions:
            - key: env
              operator: In
              values: [prod]
          maxUpdate: 25%
```

This rolls out to canary first, then staging in parallel, then 25% of prod at a time. Combined with Argo Rollouts (in-cluster blue/green or canary), you get cross-cluster *and* within-cluster progressive delivery.

### 9.4 Tradeoffs

ApplicationSet excels at:

- Symmetric workloads ("every cluster needs the platform stack").
- GitOps-native teams that already use ArgoCD.
- Per-cluster overrides via Kustomize overlay path.

It struggles with:

- Asymmetric replica placement (it doesn't *split* a Deployment across clusters; each cluster gets its own copy). Use Karmada for that.
- Single API surface (`kubectl get pods --all-clusters`). Use KCP / kubectl plugins for that.
- Real-time cluster-to-cluster choreography. Use Argo Workflows + ApplicationSet, or step outside Kubernetes for orchestration.

---

## 10. Rancher Fleet

Fleet (`rancher/fleet`) is Rancher's purpose-built workload propagation system, designed especially for *edge* fleets — large numbers of small downstream clusters that may have intermittent connectivity. It uses a pull model: each downstream cluster runs a `fleet-agent` that connects out to the Fleet controller and reconciles desired state from Git.

### 10.1 Core objects

- **GitRepo** — points at a Git repository and a branch/tag.
- **Bundle** — the unit of deployment, a set of Kubernetes manifests. Auto-generated from a GitRepo, or created manually.
- **Cluster** — a downstream cluster registered with Fleet.
- **ClusterGroup** — a label selector that groups clusters.
- **BundleDeployment** — the per-cluster instance of a Bundle.
- **GitRepoRestriction** — RBAC for which GitRepos can target which clusters.

### 10.2 Example: deploying to all edge clusters

```yaml
apiVersion: fleet.cattle.io/v1alpha1
kind: ClusterGroup
metadata:
  name: edge-stores
  namespace: fleet-default
spec:
  selector:
    matchLabels:
      env: edge
      region: us
---
apiVersion: fleet.cattle.io/v1alpha1
kind: GitRepo
metadata:
  name: store-app
  namespace: fleet-default
spec:
  repo: https://github.com/acme/store-app
  branch: main
  paths:
    - manifests
  targets:
    - clusterGroup: edge-stores
```

Fleet creates a Bundle per matching path, the agent on each `edge-stores` cluster pulls it down, and applies it. New clusters added with the right labels automatically get the bundle.

### 10.3 The killer feature: canary rollouts

Fleet has first-class staged rollouts that target percentages of clusters:

```yaml
spec:
  targets:
    - name: canary
      clusterGroup: edge-stores
      doneWaiting: 5m
      maxUnavailable: 1
    - name: rest
      clusterGroup: edge-stores
      maxUnavailable: 50%
```

The Fleet controller deploys to one cluster in `canary`, waits 5 minutes, checks status, then proceeds. If the canary fails health checks, it stops. This is the cleanest cluster-by-cluster rollout primitive in the ecosystem.

### 10.4 Where Fleet wins, where it loses

Fleet wins:
- Edge / disconnected clusters (the agent works through NAT).
- 1000+ clusters (it's designed for thousands).
- Built-in canary semantics.
- Single binary, simple to run.

Fleet loses:
- Less UI than ArgoCD.
- No `matrix` generator equivalent for cross-product templating.
- Rancher-flavored (works standalone but evolves with Rancher).

---

## 11. Karmada Architecture

Karmada (`karmada-io/karmada`) takes a fundamentally different approach. Instead of "deploy the same manifest to N clusters", Karmada says: *one apiserver to which you submit workloads; Karmada decides which clusters they run on, with what replica distribution.* It's the closest thing in the ecosystem to "Borg cells, federated".

```
KARMADA: AGGREGATED APISERVER + SCHEDULER + AGENTS

   ┌─────────────────────────────────────────────────────────────┐
   │           KARMADA CONTROL PLANE (in a host cluster)         │
   │                                                             │
   │   ┌───────────────────────┐                                 │
   │   │  karmada-apiserver    │ ◄── kubectl --kubeconfig karmada│
   │   │  (separate apiserver, │                                 │
   │   │  its own etcd)        │                                 │
   │   └──────────┬────────────┘                                 │
   │              │                                              │
   │              ▼                                              │
   │   ┌───────────────────────┐                                 │
   │   │ karmada-controller-   │  reconciles Policy CRs:         │
   │   │   manager             │   - PropagationPolicy           │
   │   └──────────┬────────────┘   - OverridePolicy              │
   │              │                                              │
   │              ▼                                              │
   │   ┌───────────────────────┐                                 │
   │   │ karmada-scheduler     │  picks clusters and             │
   │   │ (chapter 09 inspired) │  replica distribution           │
   │   └──────────┬────────────┘                                 │
   │              │                                              │
   │              ▼                                              │
   │   ┌───────────────────────┐                                 │
   │   │ karmada-webhook       │  validates/mutates              │
   │   └───────────────────────┘                                 │
   └──────────────┬──────────────────────────────────────────────┘
                  │ each member cluster connected via Cluster CR
                  │
       ┌──────────┴──────────┬──────────────────┐
       ▼                     ▼                  ▼
   ┌──────────┐         ┌──────────┐        ┌──────────┐
   │ MEMBER 1 │         │ MEMBER 2 │        │ MEMBER 3 │
   │          │         │          │        │          │
   │ karmada- │         │ karmada- │        │ karmada- │
   │ agent    │ <────── │ agent    │ <───── │ agent    │
   │          │ pull    │          │  pull  │          │
   │ kube-    │         │ kube-    │        │ kube-    │
   │ apiserver│         │ apiserver│        │ apiserver│
   └──────────┘         └──────────┘        └──────────┘
```

### 11.1 The pieces

- **karmada-apiserver** — a real `kube-apiserver` binary, running in front of its own etcd. It serves the standard Kubernetes API (Pods, Deployments, Services, etc.) plus Karmada CRDs (PropagationPolicy, OverridePolicy, ClusterPropagationPolicy, etc.). You point `kubectl` at this and submit workloads as if it were a normal cluster.
- **karmada-aggregated-apiserver** — answers `kubectl get pods --clusters=...` queries by aggregating from member apiservers (an aggregated API service, chapter 24).
- **karmada-controller-manager** — runs the controllers for the Karmada-specific CRs: PropagationPolicy, ResourceBinding, Work, ClusterStatus.
- **karmada-scheduler** — Borg-style scheduler that decides, for each (workload, PropagationPolicy) tuple, which clusters and how many replicas each.
- **karmada-webhook** — mutating/validating admission for Karmada CRs.
- **karmada-agent** — runs in each member cluster, syncs `Work` CRs (the per-cluster manifest bundles) down into the member's apiserver, reports status up.
- **karmada-descheduler** — moves replicas around as cluster utilization changes.

### 11.2 The Cluster CR

Every member cluster is registered as a `cluster.karmada.io/v1alpha1` `Cluster` CR with its kubeconfig in a Secret:

```yaml
apiVersion: cluster.karmada.io/v1alpha1
kind: Cluster
metadata:
  name: member1
spec:
  syncMode: Push      # or Pull
  apiEndpoint: https://member1.example.com:6443
  secretRef:
    namespace: karmada-cluster
    name: member1
  zones: [us-east-1a, us-east-1b]
  region: us-east-1
  provider: AWS
  taints: []
```

Push mode: Karmada controllers directly call the member's apiserver. Pull mode: the member runs a `karmada-agent` that talks out to Karmada and applies its assigned work.

The `region`, `zones`, `provider` labels are used by the scheduler. `taints` work like node taints — a workload with a tolerance can land on a tainted cluster, otherwise not.

---

## 12. Karmada Propagation Flow

When a user submits a `Deployment` to the Karmada apiserver, here's what happens:

```
   1. User:
      kubectl --kubeconfig=karmada apply -f deployment.yaml
                  │
                  ▼
   2. karmada-apiserver stores Deployment (in karmada etcd) ─┐
      But there are NO real Pods yet — this is a "template". │
                  │                                          │
                  ▼                                          │
   3. PropagationPolicy says "send this to clusters X,Y,Z".  │
      karmada-controller-manager creates:                    │
         - ResourceBinding (links resource to policy)        │
                  │                                          │
                  ▼                                          │
   4. karmada-scheduler looks at ResourceBinding,            │
      decides per-cluster placement, writes back:            │
         resourceBinding.spec.clusters: [X, Y, Z]            │
                  │                                          │
                  ▼                                          │
   5. binding controller creates per-cluster Work CRs:       │
         - Work in namespace karmada-es-X (for member X)     │
         - Work in namespace karmada-es-Y                    │
         - Work in namespace karmada-es-Z                    │
      Each Work contains the manifest for that cluster.      │
                  │                                          │
                  ▼                                          │
   6. execution controller (push) OR karmada-agent (pull)    │
      delivers the manifest to the member apiserver.         │
                  │                                          │
                  ▼                                          │
   7. In member cluster X: Deployment → ReplicaSet → Pods    │
      (the normal in-cluster path runs)                      │
                  │                                          │
                  ▼                                          │
   8. status flows back: member apiserver → karmada-agent    │
      → Work.status → ResourceBinding.status → Karmada       │
      surfaces an aggregated status on the original          │
      Deployment object.                                     │
```

### 12.1 The PropagationPolicy

```yaml
apiVersion: policy.karmada.io/v1alpha1
kind: PropagationPolicy
metadata:
  name: frontend-propagation
  namespace: default
spec:
  resourceSelectors:
    - apiVersion: apps/v1
      kind: Deployment
      name: frontend
  placement:
    clusterAffinity:
      labelSelector:
        matchLabels:
          env: prod
    spreadConstraints:
      - spreadByLabel: region
        maxGroups: 3
        minGroups: 2
      - spreadByField: cluster
        maxGroups: 5
        minGroups: 3
    replicaScheduling:
      replicaSchedulingType: Divided
      replicaDivisionPreference: Weighted
      weightPreference:
        staticWeightList:
          - targetCluster:
              clusterNames: [eu-west-1]
            weight: 1
          - targetCluster:
              clusterNames: [us-east-1]
            weight: 2
          - targetCluster:
              clusterNames: [ap-southeast-1]
            weight: 1
```

For a Deployment with `replicas: 8`:
- `Divided` with weights 1:2:1 means us-east-1 gets 4, eu-west-1 gets 2, ap-southeast-1 gets 2.
- `Duplicated` would mean each of the 3 clusters runs its own 8 replicas (24 pods total).

`spreadConstraints` enforces topological spread — minimum 2 regions, maximum 3 regions, etc.

### 12.2 The OverridePolicy

OverridePolicy patches the resource per-cluster, so each member runs a slightly customized version:

```yaml
apiVersion: policy.karmada.io/v1alpha1
kind: OverridePolicy
metadata:
  name: frontend-override
  namespace: default
spec:
  resourceSelectors:
    - apiVersion: apps/v1
      kind: Deployment
      name: frontend
  overrideRules:
    - targetCluster:
        clusterNames: [eu-west-1]
      overriders:
        plaintext:
          - path: /spec/template/spec/containers/0/env/-
            operator: add
            value:
              name: REGION
              value: eu-west-1
          - path: /spec/template/spec/containers/0/image
            operator: replace
            value: registry.eu-west.acme.com/frontend:v1.2.3
    - targetCluster:
        clusterNames: [us-east-1]
      overriders:
        plaintext:
          - path: /spec/template/spec/containers/0/env/-
            operator: add
            value:
              name: REGION
              value: us-east-1
```

This is what makes Karmada *not* a pure "deploy same manifest" tool: it has first-class per-cluster patching. The patch is JSON-Patch, applied after scheduling but before delivery to the member.

### 12.3 ClusterPropagationPolicy and ClusterOverridePolicy

The cluster-scoped variants apply to cluster-scoped resources (ClusterRole, Namespace, etc.) and to namespaced resources across all namespaces. They're the "platform team" interface — used to propagate things like cert-manager CRDs, ingress controllers, observability stacks.

---

## 13. Karmada Scheduling: Replicas Across Clusters

karmada-scheduler is, in essence, a custom kube-scheduler (chapter 34) where the "nodes" are clusters and the "pods" are workloads. It runs the same Filter / Score / Bind pattern.

### 13.1 The scheduling types

| `replicaSchedulingType` | Behavior |
|---|---|
| `Duplicated` | Each selected cluster gets the full replica count. Workload runs 3× if 3 clusters. |
| `Divided` | Total replicas split across clusters. |
| (with `replicaDivisionPreference: Weighted`) | Split by static weights or by available resources. |
| (with `replicaDivisionPreference: Aggregated`) | Pack into as few clusters as possible. |

For `Weighted/Dynamic`, the scheduler queries each cluster's `ResourceSummary` (a Karmada concept that tracks each cluster's available resources) and assigns weights proportional to free capacity. This is a real *cross-cluster scheduler*: it can place a workload where there's room, just like the kube-scheduler places pods where there's room on a node.

### 13.2 Filter and Score plugins

- **API installed**: filter out clusters that don't have the required CRDs installed.
- **Cluster locales**: filter by region/zone/provider.
- **Taint toleration**: filter by taints.
- **SpreadConstraint**: enforce min/max groups across regions/providers.
- **ClusterEviction**: deprioritize clusters that are draining.

Score plugins assign weights:

- **AvailableResources**: more free CPU/mem = higher score.
- **AffinityPriority**: cluster-affinity match = higher score.
- **TaintToleration**: matching tolerations = higher score.

### 13.3 Preemption and rescheduling

karmada-descheduler periodically evaluates if existing placements are still optimal. If cluster A drops below capacity and cluster B has spare, the descheduler can move replicas. This is *very* heavy when done wrong — every move involves draining a Pod and starting one in another cluster, which can take minutes. In practice, descheduler is run conservatively, often only when a cluster goes unreachable.

### 13.4 Failover

If a member cluster goes unreachable for longer than `clusterFailureThreshold`, Karmada marks it `NotReady` and the failover controller moves its workloads to other clusters. The original placement is preserved as `previousClusters` so when the cluster returns, Karmada knows to (optionally) move them back.

---

## 14. `kubefed` and the Lessons of Federation v2

Federation v2 — `kubernetes-sigs/kubefed` — was the SIG-Multicluster's earlier attempt at "one apiserver to rule them all". It's deprecated as of 2023, but understanding why is essential to understanding the design decisions of everything that came after.

### 14.1 What kubefed tried to do

`kubefed` created `Federated<Type>` CRDs (FederatedDeployment, FederatedService, FederatedConfigMap, ...) — each containing a `template` (the standard resource spec) and an `overrides` block (per-cluster patches) and a `placement` block (which clusters). A controller per type reconciled the federated objects into per-cluster objects.

```yaml
apiVersion: types.kubefed.io/v1beta1
kind: FederatedDeployment
metadata:
  name: frontend
  namespace: default
spec:
  template:
    metadata: { name: frontend }
    spec:
      replicas: 3
      selector: { matchLabels: { app: frontend } }
      template: ...
  placement:
    clusters:
      - name: cluster1
      - name: cluster2
  overrides:
    - clusterName: cluster2
      clusterOverrides:
        - path: "/spec/replicas"
          value: 5
```

On paper this is fine. In practice it failed for several reasons:

### 14.2 Why it failed

1. **Type explosion.** Every workload type needed a `Federated<Type>` CRD. New CRDs in the ecosystem (Istio VirtualService, ArgoCD Application, etc.) didn't have federated equivalents. The Federation needed to either generate them dynamically (which it tried, with `FederatedTypeConfig`) or stay perpetually behind.

2. **Wrong abstraction for users.** Users wanted to submit a *Deployment* and have it propagate, not a *FederatedDeployment*. The wrapper-CR pattern made every user re-author their manifests.

3. **Controller fan-out cost.** Every federated controller had to reconcile against N member apiservers from a central spot. Latency, error handling, partial-failure semantics, the half-applied-on-3-of-5-clusters problem — all hard, all kubefed-specific.

4. **No real scheduler.** Federation v2 had a `ReplicaSchedulingPreference` CRD for splitting replicas, but it was simplistic compared to Karmada's. The hard problem of "which cluster should this workload run on" was punted.

5. **Drift recovery was brittle.** If a member cluster's Deployment was edited directly, kubefed would fight back, but the convergence was racy. Karmada explicitly took a *push* approach with `Work` CRs that simplifies this.

6. **It was a SIG project, not a Rancher/Red Hat product.** Nobody owned the long-term support; the SIG ran out of contributors around 2020. Karmada (Huawei-led) and Open Cluster Management (Red Hat-led) grew out of the gap.

### 14.3 What the successor systems took from kubefed

- **Karmada**: kept the per-cluster override idea (as OverridePolicy), but dropped the `Federated<Type>` wrapper — users submit regular Deployments, and a separate PropagationPolicy handles federation.
- **Open Cluster Management** (`open-cluster-management-io/ocm`): kept the placement abstraction (`Placement` CR), but split it from workload distribution (ApplicationSet-like `ApplicationSet`).
- **ApplicationSet**: kept the *generator* pattern (kubefed v2 had cluster selectors), but pivoted to "generate one Application per cluster" rather than "one federated object spanning clusters".

The lesson, broadly: don't try to make the *single* API surface that hides clusters. Either make a clear federated control plane (Karmada) or stay GitOps-native (ApplicationSet). The hybrid kubefed attempted is the worst of both worlds.

---

## 15. Cross-Cluster Service Discovery: The Problem

Workload propagation lets you *deploy* the same app to N clusters. But how does `frontend` in cluster A find `payments` in cluster B?

Inside one cluster, the answer is "the cluster DNS resolves `payments.default.svc.cluster.local` to a ClusterIP, kube-proxy NATs it to a pod IP" (chapters 14, 18). Across clusters, none of that works:

- ClusterIPs are not routable across clusters (they're per-cluster).
- Pod IPs are not routable across clusters (different CIDRs, or the same CIDR but only locally meaningful).
- DNS doesn't know about other clusters.
- NetworkPolicies don't cross cluster boundaries.

There are four production approaches:

| Approach | Connectivity | Service discovery | Effort | Use case |
|---|---|---|---|---|
| Submariner | IPsec tunnels gateway-to-gateway | Lighthouse (CoreDNS plugin), MCS API | Low-med | General multi-cluster |
| Cilium ClusterMesh | Direct (overlay or routing) | Cilium identity sharing + CoreDNS | Med | Cilium-native fleets |
| Istio multi-primary | East-west gateway, mTLS | Istio's xDS shared | High | Already on Istio |
| Cloud-managed (AWS VPC Lattice, GCP MCS, AKS Service Fabric) | Cloud-routed | Cloud DNS | Low-med | Single-cloud |

All four eventually expose the **Multi-Cluster Services (MCS) API** (§19) — `ServiceExport` + `ServiceImport` — as the user-visible surface, while differing in the underlying datapath.

---

## 16. Submariner: IPsec Tunnels + Lighthouse

Submariner (`submariner-io/submariner`) is the most established cross-cluster networking project. It's vendor-neutral, CNI-agnostic, and works on-prem and across clouds.

### 16.1 The components

```
SUBMARINER TOPOLOGY: GATEWAY-PER-CLUSTER + BROKER

   ┌──────────────────────────────┐
   │     SUBMARINER BROKER        │  (a designated cluster's apiserver,
   │  (Endpoint/Cluster/EP CRDs)  │   often runs on the mgmt cluster)
   └──────────────┬───────────────┘
                  │
       ┌──────────┴───────────┐
       │ each cluster watches │
       │  the broker for      │
       │  endpoints / IPs     │
       │  of other clusters   │
       ▼                      ▼
   ┌───────────────┐    ┌───────────────┐
   │   CLUSTER A   │    │   CLUSTER B   │
   │ ┌───────────┐ │    │ ┌───────────┐ │
   │ │ gateway   │ │    │ │ gateway   │ │
   │ │ pod       │◄┼────┼►│ pod       │ │
   │ │ (libreswan│ │    │ │ (libreswan│ │
   │ │ /wireguard│ │    │ │ /wireguard│ │
   │ │ /vxlan)   │ │    │ │ /vxlan)   │ │
   │ └─────┬─────┘ │    │ └─────┬─────┘ │
   │       │       │    │       │       │
   │  ┌────┴────┐  │    │  ┌────┴────┐  │
   │  │route    │  │    │  │route    │  │
   │  │agents on│  │    │  │agents on│  │
   │  │each node│  │    │  │each node│  │
   │  └────┬────┘  │    │  └────┬────┘  │
   │       │       │    │       │       │
   │   pod CIDR     │   IPsec    │  pod CIDR  │
   │   10.42.0.0/16│◄──tunnel──►│ 10.43.0.0/16│
   └───────────────┘    └───────────────┘
```

Components:

- **submariner-operator**: bootstraps everything via CRDs.
- **submariner-broker**: the central coordination point; it's just a separate set of CRDs (`Endpoint`, `Cluster`) on a designated apiserver. Member clusters watch the broker.
- **gateway**: the IPsec/WireGuard tunnel endpoint, runs as a single Pod (with a backup) on one labeled node per cluster.
- **route-agent**: DaemonSet that installs iptables/eBPF rules to route inter-cluster traffic through the gateway.
- **globalnet** (optional): handles overlapping pod CIDR ranges by NAT'ing into a non-conflicting `global` CIDR. Required when clusters were built without coordinating CIDRs.
- **Lighthouse**: the multi-cluster DNS. A CoreDNS plugin that resolves `<svc>.<ns>.svc.clusterset.local` to an endpoint in some other cluster.
- **lighthouse-agent**: per cluster, exports services (via `ServiceExport` CRs) to the broker.

### 16.2 The flow

1. User creates a `ServiceExport` for `payments` in cluster B.
2. lighthouse-agent in B writes a `ServiceImport` (MCS API) to the broker.
3. lighthouse-agent in A sees the broker's `ServiceImport`, creates a local `ServiceImport` in A.
4. CoreDNS in A (with Lighthouse plugin) sees the import, answers `payments.default.svc.clusterset.local` with the endpoint IP.
5. Traffic to that IP is routed by route-agent → local gateway → IPsec tunnel → remote gateway → remote pod.

### 16.3 Tradeoffs

- **Pro**: works anywhere; doesn't require a specific CNI.
- **Pro**: encrypted in transit (IPsec).
- **Pro**: handles overlapping CIDRs (globalnet).
- **Con**: gateway pods are the bottleneck. All cross-cluster traffic from cluster A funnels through one or two gateway nodes' NICs.
- **Con**: gateway failover (active/passive) drops connections during election.
- **Con**: latency is the IPsec tunnel + extra hop (gateway → gateway → node → pod). Often 1–3ms overhead beyond raw network.

For low-throughput service-to-service traffic (most microservice fleets), Submariner is fine. For data-plane traffic (replicating a database across clusters), the gateway is a real bottleneck.

---

## 17. Cilium ClusterMesh

Cilium ClusterMesh (chapter 16) is the eBPF-native alternative. There's no gateway: every node in cluster A can talk directly to every node in cluster B, using the existing CNI (Cilium) to handle encapsulation and identity.

### 17.1 The topology

```
CILIUM CLUSTERMESH: NO GATEWAY, DIRECT POD-TO-POD

   ┌─────────────────────────────────────────────────────────────┐
   │  CLUSTER A (Cilium)                                         │
   │                                                             │
   │   node a1 ──┐                                              │
   │   node a2 ──┼── all nodes can reach all pods               │
   │   node a3 ──┘   (overlay VXLAN or native routing)          │
   │                                                             │
   │   etcd-cilium / kvstore exposes pod identities             │
   └──────────────────┬──────────────────────────────────────────┘
                      │ mTLS, identity sharing,
                      │ direct routing via underlay or VXLAN
                      │
   ┌──────────────────┴──────────────────────────────────────────┐
   │  CLUSTER B (Cilium)                                         │
   │                                                             │
   │   node b1 ──┐                                              │
   │   node b2 ──┼── all nodes know A's pods                    │
   │   node b3 ──┘   via shared kvstore                         │
   └─────────────────────────────────────────────────────────────┘
```

A `cilium-clustermesh-apiserver` per cluster exposes pod identity, service identity, and node IP information to other clusters. Each cluster's Cilium agent watches the other clusters' clustermesh apiservers and learns about remote pods.

### 17.2 Service discovery

By default, services are *not* cross-cluster. To make a service visible:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: payments
  annotations:
    service.cilium.io/global: "true"   # exposed to other clusters
    service.cilium.io/affinity: "local" # prefer local endpoints
spec:
  selector: { app: payments }
  ports: [{ port: 8080 }]
```

In all clusters with the `global` annotation, `payments.default.svc.cluster.local` now resolves to *combined* endpoints — local pods in your cluster plus the remote pods in other clusters. The `affinity: local` hint tells Cilium to prefer local pods unless they're all down.

Cilium also implements the MCS API natively in recent versions, so `ServiceExport`/`ServiceImport` works.

### 17.3 NetworkPolicy across clusters

CiliumClusterwideNetworkPolicy can reference remote pods by their identity labels. The eBPF program on the local node enforces the policy by checking the remote pod's identity (delivered via VXLAN metadata or extracted from the IP-to-identity mapping).

### 17.4 Tradeoffs

- **Pro**: no gateway bottleneck. Pod-to-pod, full bandwidth.
- **Pro**: low latency (no extra encapsulation beyond what Cilium already does).
- **Pro**: NetworkPolicy works across clusters.
- **Pro**: built on Cilium identity, so policies are powerful.
- **Con**: requires Cilium in every cluster (not CNI-agnostic).
- **Con**: clusters need IP connectivity at the node level — if they're in different VPCs without VPC peering, you'll need WireGuard transparent encryption (Cilium supports it).
- **Con**: pod CIDRs *must* be non-overlapping. There's no globalnet equivalent (yet).

---

## 18. Istio Multi-Primary and Primary-Remote

Istio (chapter 17) has two multi-cluster topologies, both built on the east-west gateway concept: a dedicated Envoy gateway per cluster that handles cross-cluster traffic.

### 18.1 Multi-primary

Each cluster has its own istiod (control plane). They share a root CA so workloads in different clusters can establish mTLS. They learn about each other's services by reading each other's apiservers via remote `kubeconfig` Secrets.

```
ISTIO MULTI-PRIMARY

   ┌─────────────────────────────────┐
   │       CLUSTER A                 │
   │  istiod-A (control plane)       │
   │  apps-A (sidecar Envoys)        │
   │  east-west-gateway-A (port 15443│
   │  ◄─── istiod-B reads A's        │
   │       svc/endpoints             │
   └──────────┬──────────────────────┘
              │
              │ mTLS (shared root CA)
              │ tunneled through east-west gateways
              │
   ┌──────────┴──────────────────────┐
   │       CLUSTER B                 │
   │  istiod-B (control plane)       │
   │  apps-B (sidecar Envoys)        │
   │  east-west-gateway-B            │
   │  ◄─── istiod-A reads B's        │
   │       svc/endpoints             │
   └─────────────────────────────────┘
```

When an Envoy in cluster A wants to reach `payments` in cluster B, istiod-A has the endpoint list (learned from B's apiserver) and tells the Envoy to send to cluster B's east-west gateway IP. The east-west gateway terminates and re-originates the mTLS connection internally.

### 18.2 Primary-remote

A "primary" cluster runs istiod; one or more "remote" clusters run only data plane (Envoys) and use the primary's istiod. Saves on control-plane cost but creates a control-plane dependency.

### 18.3 Tradeoffs

- **Pro**: full L7 features cross-cluster (retries, timeouts, traffic split, locality LB).
- **Pro**: mTLS enforced end-to-end.
- **Pro**: ambient mode (ztunnel + waypoints) supports multi-cluster too.
- **Con**: operational complexity is high (CA management, gateway certificates, network reachability to gateway).
- **Con**: east-west gateway is a bottleneck (similar to Submariner gateway, but at L7).
- **Con**: if you weren't on Istio already, this isn't where you'd start.

---

## 19. The Multi-Cluster Services (MCS) API

The MCS API (`sigs.k8s.io/mcs-api`) is the SIG-Multicluster's attempt at a *standard* for cross-cluster service discovery. It defines two CRDs that all the systems above are converging on:

### 19.1 ServiceExport

```yaml
apiVersion: multicluster.x-k8s.io/v1alpha1
kind: ServiceExport
metadata:
  name: payments
  namespace: default
```

Created by the *owner* of the Service. Says "make this Service available to other clusters in the ClusterSet". The controller (Submariner, Cilium, Istio, whoever) sees the export and propagates a corresponding ServiceImport everywhere else.

### 19.2 ServiceImport

```yaml
apiVersion: multicluster.x-k8s.io/v1alpha1
kind: ServiceImport
metadata:
  name: payments
  namespace: default
spec:
  type: ClusterSetIP
  ports:
    - port: 8080
      protocol: TCP
  ips: [10.97.0.42]   # cluster-set-wide IP (optional)
```

Created by the cross-cluster controller in each consuming cluster. Says "there's a ServiceExport named payments somewhere in the ClusterSet; here's the local handle". The handle resolves via DNS at `payments.default.svc.clusterset.local`.

### 19.3 The ClusterSet concept

A ClusterSet is a group of clusters that mutually trust each other for service discovery — analogous to a single namespace within a federation. Membership is currently informal; SIG-Multicluster is working on a `ClusterProperty` and `About` CRD to standardize cluster identity.

### 19.4 What MCS doesn't define

- *How* traffic routes from cluster A to cluster B (that's the implementation's job).
- mTLS or encryption (each implementation chooses).
- NetworkPolicy interaction (in flux).
- Health-checking and EndpointSlice semantics across clusters (partially defined; implementations differ).

This is by design: MCS is the user-facing contract, not the data plane.

---

## 20. Crossplane: The Meta-Operator Paradigm

Crossplane (`crossplane/crossplane`) takes a very different stance: *the Kubernetes apiserver is the universal control plane*. Not just for K8s clusters, but for cloud-provider resources too. RDS databases, S3 buckets, GKE clusters, IAM roles, SNS topics, Kafka clusters on Confluent Cloud — all of it represented as Kubernetes CRs, reconciled by Crossplane providers.

### 20.1 The pieces

- **Crossplane core**: the runtime, runs in a "control plane cluster" (could be the management cluster).
- **Providers**: per-cloud (provider-aws, provider-gcp, provider-azure, provider-terraform, provider-kubernetes) — each provides hundreds of MR (Managed Resource) CRDs corresponding to that cloud's primitives (`Database`, `Bucket`, `Cluster`, `Role`).
- **Compositions** (XR / XRC): the abstraction layer. A `CompositeResourceDefinition` (XRD) defines a composite type (e.g., `XPostgreSQLInstance`); a `Composition` maps that composite to a bag of MRs (VPC + subnet + RDS + Secret + IAM).
- **Configurations**: bundles of XRDs + Compositions distributed as OCI images.
- **Claims**: namespaced user-facing CRs that point at composite XRs.

### 20.2 An example: PostgreSQL claim

```yaml
# What the platform team defines (admin)
apiVersion: apiextensions.crossplane.io/v1
kind: CompositeResourceDefinition
metadata:
  name: xpostgresqls.acme.io
spec:
  group: acme.io
  names:
    kind: XPostgreSQL
    plural: xpostgresqls
  claimNames:
    kind: PostgreSQL
    plural: postgresqls
  versions:
    - name: v1alpha1
      served: true
      referenceable: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                storageGB:
                  type: integer
                  minimum: 20
                region:
                  type: string
              required: [storageGB, region]
---
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: xpostgresql.aws
spec:
  compositeTypeRef:
    apiVersion: acme.io/v1alpha1
    kind: XPostgreSQL
  resources:
    - name: vpc
      base:
        apiVersion: ec2.aws.crossplane.io/v1beta1
        kind: VPC
        spec:
          forProvider:
            cidrBlock: 10.0.0.0/16
    - name: subnet-group
      base:
        apiVersion: rds.aws.crossplane.io/v1beta1
        kind: DBSubnetGroup
        spec: { forProvider: { description: managed } }
    - name: instance
      base:
        apiVersion: rds.aws.crossplane.io/v1beta1
        kind: DBInstance
        spec:
          forProvider:
            dbInstanceClass: db.t3.medium
            engine: postgres
            engineVersion: "15"
            masterUsername: root
            publiclyAccessible: false
      patches:
        - fromFieldPath: spec.storageGB
          toFieldPath: spec.forProvider.allocatedStorage
        - fromFieldPath: spec.region
          toFieldPath: spec.forProvider.region
```

```yaml
# What a user creates (namespaced)
apiVersion: acme.io/v1alpha1
kind: PostgreSQL
metadata:
  name: orders-db
  namespace: orders
spec:
  storageGB: 100
  region: eu-west-1
```

The user submits a 6-line YAML. Crossplane creates a VPC + subnet group + RDS instance + secrets, all reconciled, all with finalizers and deletion cascading. Drift in AWS console is reconciled back.

### 20.3 What this has to do with multi-cluster

Crossplane can also provision Kubernetes clusters. provider-aws has `EKSCluster`; provider-gcp has `Cluster` (GKE); provider-azure has `AKSCluster`. So you can write a Composition that creates "a cluster" by composing: VPC + subnets + EKS + node groups. Crossplane then takes the cluster's kubeconfig as an output Secret.

The Crossplane composition that creates a cluster is the *Crossplane equivalent of CAPI*. Which leads to the inevitable question:

---

## 21. Crossplane vs CAPI

Both provision Kubernetes clusters. Both use CRs and reconcile loops. Both run from a management cluster. Which do you use?

| | CAPI | Crossplane |
|---|---|---|
| Domain | Kubernetes clusters only | Any cloud resource |
| Native concepts | Cluster, Machine, MachineDeployment, KubeadmControlPlane | XR, XRD, Composition, MR (Managed Resource) |
| Provider model | InfrastructureProvider + BootstrapProvider per cloud | Provider per cloud (one provider per cloud, covering all services) |
| Customization | ClusterClass + variables + patches | Composition + patches + transforms |
| Day-2 ops | First-class (upgrades, scale, drain via spec.version, etc.) | Has it, less polished |
| Hybrid/edge | Strong (Talos, Metal3, K3s) | Possible but uncommon |
| Multi-cluster *of the cloud resources* | n/a | Native (each cluster is just another MR) |
| Mature SIG | SIG-Cluster-Lifecycle, very active | Crossplane is its own community (CNCF, ex-Upbound) |

The pragmatic answer:

- **CAPI** is the right choice when your job is *"manage Kubernetes clusters"*. It has more polish around K8s-specific operations: control-plane rollouts, kubeadm cert rotation, version upgrades, machine pools.
- **Crossplane** is the right choice when your job is *"manage cloud resources, of which clusters are a few"*. If you're already provisioning RDS, S3, IAM, and Lambda via Crossplane, adding a cluster as just-another-MR is consistent.

The most common production pattern is *both*: Crossplane provisions cloud resources (VPC, IAM, RDS), CAPI provisions the cluster control plane + workers (because CAPI's lifecycle for clusters is more mature), and ArgoCD bootstraps the workloads. Each tool stays in its zone of competence.

### 21.1 Crossplane composition functions

Recent Crossplane (v1.14+) introduced **Composition Functions** — pluggable runtime transformations written in Go/Python/KCL/Crossplane Function Language. This is Crossplane's answer to the "patches are limited" criticism. Functions get called during composition reconcile with the desired state and produce the next state, like Argo CD plugins or Kustomize functions.

This makes Crossplane meaningfully more general than CAPI: you can express provisioning logic as a *program* (in any language with a Crossplane SDK), where CAPI's ClusterClass patches are limited to JSON Patch.

---

## 22. KCP: Kubernetes-Like Control Plane Without Nodes

KCP (`kcp-dev/kcp`) is the most experimental of the multi-cluster paradigms, and the most ambitious. Instead of "many clusters, each with its own apiserver", KCP says: *one apiserver, partitioned into many logical control planes called workspaces. Workloads, if any, are synced down to physical clusters separately.*

The shape of KCP:

```
KCP: WORKSPACES INSTEAD OF CLUSTERS

   ┌──────────────────────────────────────────────────────────────┐
   │                        KCP APISERVER                         │
   │  (single binary, single etcd, like kube-apiserver but        │
   │  workspace-aware)                                            │
   │                                                              │
   │  ┌──────────────────────────────────────────────────────┐    │
   │  │  Workspace: root                                     │    │
   │  │   ├─ Workspace: customers                            │    │
   │  │   │   ├─ Workspace: acme                             │    │
   │  │   │   │   ├─ APIBinding: pgaas (DBaaS API)           │    │
   │  │   │   │   ├─ Database "orders-db"                    │    │
   │  │   │   │   ├─ Namespace "orders"                      │    │
   │  │   │   │   │   ├─ Deployment "frontend"               │    │
   │  │   │   │   │   └─ Service "frontend"                  │    │
   │  │   │   │   └─ Workspace: prod                         │    │
   │  │   │   └─ Workspace: globex                           │    │
   │  │   └─ Workspace: providers                            │    │
   │  │       └─ Workspace: pgaas-team                       │    │
   │  │           └─ APIExport: pgaas                        │    │
   │  └──────────────────────────────────────────────────────┘    │
   └──────────────────────┬───────────────────────────────────────┘
                          │ syncer agents in physical
                          │ clusters pull "Deployment"
                          │ etc. from KCP workspaces
            ┌─────────────┴──────────────┐
            ▼                            ▼
       ┌──────────┐                ┌──────────┐
       │ physical │                │ physical │
       │ cluster  │                │ cluster  │
       │   #1     │                │   #2     │
       │  (nodes) │                │  (nodes) │
       └──────────┘                └──────────┘
```

### 22.1 Core concepts

- **Workspace** — a namespace-like logical container, but for the entire API surface. Each workspace has its own CRDs, its own RBAC, its own quotas. Workspaces nest hierarchically.
- **APIExport** — a workspace publishes its CRDs to other workspaces.
- **APIBinding** — a workspace consumes an APIExport from another workspace.
- **Syncer** — an agent in a physical cluster that pulls workloads from a KCP workspace and applies them locally.
- **Placement** — defines where workloads in a workspace get scheduled (which physical cluster).

### 22.2 Why this design

KCP is designed for *SaaS-like multi-tenant platforms*. The classic problem: you want to offer your customers a Kubernetes-like API for their custom resources (PostgreSQL, Pipelines, ML-Models) without giving them a real cluster. KCP gives each customer a workspace, where their CRs live independently of other customers, all in one apiserver — *much* cheaper than one cluster per customer.

The workloads, if any (most KCP use cases don't have many Pods — they have CRs that represent things like databases), can be synced to a small pool of physical clusters via the Syncer. Or KCP can be used as a *pure control plane* — submitted resources are reconciled by operators that themselves talk to clouds (think Crossplane on top of KCP).

### 22.3 Status

KCP is alpha-ish. It works, it has a community, but it's not the path most teams will take for typical "multi-cluster" needs. It's more interesting as a *platform-building primitive* for companies that need to offer a Kubernetes-style API to thousands of tenants. Think Confluent Cloud, Aiven, Datastax Astra — services that look like "give me a Postgres" but are implemented as Kubernetes CRs underneath.

---

## 23. Hub-and-Spoke vs Mesh Topologies

Almost every multi-cluster system in this chapter is **hub-and-spoke**:

```
   HUB-AND-SPOKE                         MESH
   ─────────────                         ────

         ┌─────┐                    ┌─────┐ ─── ┌─────┐
         │ hub │                    │  A  │     │  B  │
         └──┬──┘                    └──┬──┘     └──┬──┘
            │                          │  X        │
   ┌────────┼────────┐                 │ /│\       │
   ▼        ▼        ▼                 │/ │ \      │
 ┌───┐    ┌───┐    ┌───┐             ┌─┴──┴─┐    ┌─┴───┐
 │ A │    │ B │    │ C │             │  C   │────│  D  │
 └───┘    └───┘    └───┘             └──────┘    └─────┘

   1 → N control flow                 N×N or N²/2 paths
   single failure domain at the       no SPOF, but coordination
   hub, simple security model         is harder (Byzantine,
                                      gossip, etc.)
```

| | Hub-and-spoke | Mesh |
|---|---|---|
| Lifecycle | CAPI (mgmt → clusters) | Rare |
| Propagation | ArgoCD, Karmada, Fleet | Rare |
| Connectivity | Submariner broker → gateways | Cilium ClusterMesh (peer-to-peer) |
| Federation | KCP (single apiserver = hub) | Distinctly absent |
| Discovery | MCS broker | Rare |

The reason: hub-and-spoke is *operationally simple*. One source of truth, one place to query "what's the fleet doing", one place to enforce policy. Mesh is theoretically more available but in practice harder to debug (which cluster is the source of truth? what if two disagree?).

The exception is the **data path**: pod-to-pod traffic is often pure mesh (every cluster connects directly to every other cluster). Cilium ClusterMesh is the canonical example. This is because the data plane has very different concerns than the control plane: latency matters, bandwidth matters, and a hub gateway is a real bottleneck.

So the typical production fleet is:

- **Control plane**: hub-and-spoke (mgmt cluster + N member clusters).
- **Data plane**: mesh (any-to-any pod connectivity).

---

## 24. The Control-Plane-of-Control-Planes Problem

If everything depends on the management cluster, the management cluster is your single point of failure. When it dies, you can't:

- Create new clusters (CAPI is down).
- Roll out new workloads (ApplicationSet is down).
- Modify policies (Karmada apiserver is down).
- Distribute secrets (External Secrets Operator might be running per-cluster, but its source of truth lives somewhere).

Strategies to mitigate:

### 24.1 HA management cluster

The mgmt cluster itself should have a multi-replica control plane (3 nodes), backed by stacked or external etcd with regular backups (chapter 32). Treat its etcd as the most precious data in your fleet — back it up *frequently* (every 15 minutes), test restore quarterly.

### 24.2 Workload clusters keep running

A subtle but important point: when the mgmt cluster dies, the workload clusters *keep running*. They have their own apiserver and their own etcd. Existing Pods stay alive. New Pods can't be scheduled centrally, but each workload cluster's own scheduler continues to function.

This is the *failure isolation* CAPI gives you almost for free, and it's not what you'd get from a true federated single-apiserver design (KCP, kubefed). It's a major reason hub-and-spoke beat federation v2.

### 24.3 DR for the management cluster

- **Cold standby**: an etcd backup + the procedure to restore into a fresh cluster, runbooked.
- **Warm standby**: a second mgmt cluster, kept in sync via cross-cluster etcd replication. Rare; complex.
- **Active-passive across regions**: the mgmt cluster runs in region A; backups stored in region B; in disaster, you restore in region B and re-issue kubeconfigs to point at the new mgmt cluster.

The practical reality: **the mgmt cluster is rarely the most urgent thing to recover**. A 4-hour outage of mgmt means "no new clusters, no fleet-wide changes" but workload clusters are fine. So your RTO for mgmt is hours, not minutes — much more relaxed than for workloads.

### 24.4 GitOps as backup

If your fleet is GitOps-driven, the *Git repo is the real source of truth*, and the mgmt cluster is just a cache. You can rebuild it from scratch by:

1. Spinning up a new bootstrap cluster.
2. Installing CAPI, ArgoCD, etc.
3. Pointing them at the Git repo.
4. Letting them re-reconcile the world.

This pattern — "the cluster is cattle, the Git repo is the pet" — is the architectural ideal. The mgmt cluster has no irreplaceable state; everything important lives in Git.

The exception: secrets. They live in Vault / AWS Secrets Manager / a secrets backend, not in Git. So your real source of truth is *Git + secrets backend*.

---

## 25. Cluster Identity, Trust, and OIDC Federation

How does the mgmt cluster trust a member cluster, and vice versa?

### 25.1 Static kubeconfig

The simplest pattern: when a member cluster is provisioned, its kubeconfig is exported (containing a client cert + apiserver URL + CA bundle) and stored as a Secret in the mgmt cluster. Controllers (Karmada, ArgoCD, etc.) read this Secret to talk to the member.

This works, but:

- The client cert expires (usually 1 year).
- Rotating it is manual.
- The Secret is a juicy target if the mgmt cluster is compromised.

### 25.2 ServiceAccount-based

Each member cluster has a ServiceAccount (typically named after the controller, e.g., `karmada-controller`), bound to a `ClusterRole` like `cluster-admin` or a restricted set. The mgmt cluster uses the token from this SA.

Better than client certs because tokens can rotate; still has the "all eggs in one Secret" problem.

### 25.3 Bound projected tokens

Modern Kubernetes (1.22+) supports projected SA tokens with audience and TTL. The mgmt cluster runs short-lived tokens (1 hour TTL, refreshed automatically). Less juicy if leaked.

### 25.4 OIDC federation

The most elegant pattern: the member cluster's apiserver is configured to trust an OIDC issuer (e.g., the mgmt cluster's apiserver, or an external IdP like Dex / Keycloak). The mgmt cluster's controllers obtain JWTs from the IdP and present them to the member.

```
OIDC FEDERATION BETWEEN CLUSTERS

  ┌──────────────────┐     ┌──────────────────┐
  │   mgmt cluster   │     │  member cluster  │
  │                  │     │                  │
  │  controller pod  │     │  apiserver       │
  │   │              │     │   │              │
  │   │ get JWT      │     │   │ OIDC issuer  │
  │   │              │     │   │ trusts       │
  │   ▼              │     │   │ mgmt's       │
  │  IdP (Dex,       │     │   │ JWT issuer   │
  │   apiserver-OIDC)│     │   │              │
  └────────┬─────────┘     └─────────┬────────┘
           │                         │
           └────── JWT (short-lived) ┘
                  presented to member apiserver
```

This is how GKE Workload Identity, EKS IRSA, and AKS Workload Identity work at the cloud layer; the same mechanism applies between clusters. The advantage: no long-lived secrets cross cluster boundaries.

### 25.5 SPIFFE / SPIRE

For the most stringent security needs, SPIFFE IDs (`spiffe://acme.io/cluster/A/sa/karmada-controller`) plus SPIRE-issued X.509-SVIDs provide cryptographically-attested workload identity across clusters. This is the foundation of most service-mesh mTLS and is the trust system you want for a high-security fleet.

---

## 26. GitOps as the Fleet Driver

In practice, *the* most-deployed multi-cluster architecture is "Argo CD ApplicationSet (or Flux) drives N clusters from one Git repo". Chapter 31 covers the GitOps internals; this section is specifically about the fleet pattern.

### 26.1 The repo structure

A typical multi-cluster GitOps repo:

```
manifests/
├── clusters/
│   ├── prod-eu-west-1.yaml         # Cluster CR + kubeconfig info
│   ├── prod-us-east-1.yaml
│   ├── prod-ap-southeast-1.yaml
│   └── staging-eu-west-1.yaml
├── platform/
│   ├── base/                       # all clusters get this
│   │   ├── cert-manager.yaml
│   │   ├── ingress-nginx.yaml
│   │   ├── external-dns.yaml
│   │   ├── prometheus.yaml
│   │   └── kustomization.yaml
│   └── overlays/
│       ├── prod/
│       │   ├── kustomization.yaml
│       │   └── prometheus-patch.yaml
│       └── staging/
│           └── kustomization.yaml
├── apps/
│   ├── frontend/
│   │   ├── base/
│   │   ├── overlays/
│   │   │   ├── eu-west-1/
│   │   │   ├── us-east-1/
│   │   │   └── ap-southeast-1/
│   └── ...
└── applicationsets/
    ├── platform.yaml
    └── apps.yaml
```

### 26.2 The platform ApplicationSet

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: platform
  namespace: argocd
spec:
  generators:
    - clusters: {}   # all clusters
  template:
    metadata:
      name: 'platform-{{name}}'
    spec:
      project: platform
      source:
        repoURL: https://github.com/acme/k8s-manifests
        targetRevision: main
        path: 'platform/overlays/{{metadata.labels.env}}'
      destination:
        server: '{{server}}'
        namespace: kube-system
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
```

This deploys the platform overlay matching each cluster's `env` label to that cluster. New cluster shows up with `env: prod` label → it automatically gets the prod platform stack.

### 26.3 The apps ApplicationSet

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: apps
  namespace: argocd
spec:
  generators:
    - matrix:
        generators:
          - clusters: {}
          - git:
              repoURL: https://github.com/acme/k8s-manifests
              revision: main
              directories:
                - path: apps/*
  template:
    metadata:
      name: '{{path.basename}}-{{name}}'
    spec:
      project: '{{path.basename}}'
      source:
        repoURL: https://github.com/acme/k8s-manifests
        targetRevision: main
        path: 'apps/{{path.basename}}/overlays/{{metadata.labels.region}}'
      destination:
        server: '{{server}}'
        namespace: '{{path.basename}}'
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
```

`matrix` produces (cluster × app) pairs. 10 clusters × 30 apps = 300 Applications, kept in sync.

### 26.4 Cluster Bootstrap

When CAPI creates a new cluster, who installs ArgoCD into it? The "app of apps" pattern, plus a `ClusterResourceSet`:

```yaml
apiVersion: addons.cluster.x-k8s.io/v1beta1
kind: ClusterResourceSet
metadata:
  name: argocd-bootstrap
  namespace: clusters
spec:
  clusterSelector:
    matchLabels:
      env: prod
  strategy: ApplyOnce
  resources:
    - kind: ConfigMap
      name: argocd-install
    - kind: ConfigMap
      name: argo-cluster-register
```

The `ClusterResourceSet` controller, when the cluster is Ready, applies the bundled manifests. Those manifests can include "register-this-cluster-with-mgmt-argocd" logic, closing the loop.

Alternatively (the modern best practice): the mgmt cluster's ArgoCD has a `cluster-secrets` ApplicationSet that watches the CAPI-generated kubeconfig Secrets and registers each new cluster automatically.

---

## 27. Cluster Lifecycle: Upgrade, Scale, Drain, Decommission

CAPI makes the entire cluster lifecycle declarative. Each operation is "edit the spec and let the controller reconcile."

### 27.1 Upgrade

To upgrade a cluster from 1.30.4 to 1.30.5:

```bash
kubectl edit kubeadmcontrolplane prod-eu-west-1-cp
# change spec.version: v1.30.4 → v1.30.5
```

The KubeadmControlPlane controller performs a rolling replacement of the control-plane Machines: a fresh one with 1.30.5 is brought up, kubeadm joins it, the old one is drained and removed. Repeated for each of the 3 control-plane nodes.

For minor-version upgrades (1.30 → 1.31), edit the workers too:

```bash
kubectl edit machinedeployment prod-eu-west-1-md-general
# change spec.template.spec.version: v1.30.4 → v1.31.1
```

The MachineDeployment rolls out new MachineSets the same way Deployment rolls out new ReplicaSets.

Skew rules: control plane goes first, then nodes. Skew between control plane and nodes is at most +/- 2 minors (1.31 → 1.30 → 1.29 nodes OK; 1.31 control plane with 1.28 nodes not OK). CAPI does *not* enforce these — you must orchestrate the upgrade order yourself, usually via the same GitOps pipeline that owns the manifests.

### 27.2 Scale

```bash
kubectl scale machinedeployment prod-eu-west-1-md-general --replicas=20
```

That's it. The MachineDeployment controller spins up 10 more Machines, the InfrastructureProvider provisions EC2 instances, the BootstrapProvider generates kubeadm-join cloud-init, and the new nodes join the cluster. End-to-end takes 3–10 minutes per node depending on the cloud.

For autoscaling, you set `spec.replicas` *to nothing* (or use the autoscaling-from-zero MachinePool) and let cluster-autoscaler (which has CAPI awareness) or Karpenter scale.

### 27.3 Drain

CAPI handles drain automatically when removing a Machine. The flow:

1. The Machine is marked for deletion (annotation or `kubectl delete machine ...`).
2. The Machine controller cordons the underlying Node.
3. Drains the Node respecting PDBs (PodDisruptionBudgets) and graceful termination.
4. Once the Node is empty, the Machine is removed (kubeadm reset, then EC2 terminate).
5. The MachineSet brings up a replacement (if part of a Deployment).

`nodeDrainTimeout` on the Machine spec is a useful safety: don't get stuck if a Pod refuses to terminate.

### 27.4 Decommission

Decommissioning a cluster:

```bash
kubectl delete cluster prod-eu-west-1
```

The Cluster controller cascades:

- Deletes all Machines (which drain + remove EC2 instances).
- Deletes the KubeadmControlPlane.
- Deletes the AWSCluster (which destroys VPC, NLB, security groups).

Finalizers ensure cloud resources are cleaned up before CRs disappear. If a finalizer hangs (common: the NLB's target group is in a weird state), you may need to manually clean up the cloud side and force-remove finalizers — *carefully*, because leaked AWS resources cost money.

---

## 28. Multi-Cluster Autoscaling

There are three layers:

1. **Workload autoscaling within a cluster** — HPA, VPA (chapter 22).
2. **Node autoscaling within a cluster** — cluster-autoscaler, Karpenter (chapter 22).
3. **Cluster-level autoscaling across clusters** — much rarer, several patterns.

### 28.1 Karmada cross-cluster autoscaling

Karmada's `FederatedHorizontalPodAutoscaler` (FHPA) and `HorizontalPodAutoscalerOverride` let a single autoscaling decision result in replica adjustments *across clusters*. Combined with Karmada's `Divided` replica scheduling and weighted preferences, you get cluster-aware HPA.

Use case: a workload runs in 3 regions, target is 70% CPU. Region A is at 90%, B at 50%, C at 60%. FHPA scales up A, leaves B and C alone. This requires Karmada's view of per-cluster metrics (it has a metrics adapter) and a configured ResourceBinding.

### 28.2 Adding clusters dynamically

What if the *fleet* needs more clusters, not just more nodes? This is rare but real: a managed-K8s SaaS provider might add a cluster when usage in a region exceeds some threshold. Tools to do this: a custom controller that watches some metric, creates a new `Cluster` CR (provisioning a new CAPI cluster), and updates the Karmada/ApplicationSet selectors to include it.

In practice, most teams add clusters manually. Adding a cluster takes ~10 minutes (CAPI) and is a deliberate operational event.

### 28.3 Karpenter vs cluster-autoscaler in multi-cluster context

Both run *within* a cluster. The choice doesn't change much across clusters — pick the one that fits the cloud and CNI (Karpenter is AWS-native and recent versions support Azure, GCP). The multi-cluster aspect is just that each cluster runs its own autoscaler instance, with its own NodePools/NodeGroups, oblivious to what's happening in other clusters.

---

## 29. Multi-Cluster Secrets Distribution

A 30-cluster fleet has thousands of secrets that need to be in the right cluster, the right namespace, rotated consistently. Three patterns:

### 29.1 External Secrets Operator (ESO) with shared backend

The dominant pattern. ESO runs in each cluster, watches `ExternalSecret` CRs, and fetches the actual values from a shared backend (Vault, AWS Secrets Manager, GCP Secret Manager, Azure Key Vault, 1Password, Doppler, ...). The CR is in Git (safe to commit, contains no secrets); the actual secret material lives in the backend.

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: db-credentials
  namespace: orders
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend
    kind: ClusterSecretStore
  target:
    name: db-credentials
  data:
    - secretKey: username
      remoteRef:
        key: secret/data/orders/db
        property: username
    - secretKey: password
      remoteRef:
        key: secret/data/orders/db
        property: password
```

Combined with GitOps: the `ExternalSecret` CR is in Git, ApplicationSet propagates it to every cluster, ESO in each cluster fetches the real values. Rotation: rotate in Vault, ESO picks up the new value on next refresh (or via webhook).

### 29.2 Sealed Secrets

Bitnami SealedSecrets is the alternative. Plaintext secrets are encrypted with a per-cluster public key and committed to Git as `SealedSecret` CRs. The cluster's controller decrypts them with the per-cluster private key.

Less appealing for multi-cluster because each cluster has its own key — secrets must be re-sealed per cluster, breaking the "one Git repo, N clusters" symmetry. Some teams use a *shared* cluster key, which works but is operationally fragile (rotating a key means re-sealing everything).

### 29.3 Vault sync agents

The Vault Agent or Vault Secrets Operator in each cluster, authenticating via cluster-specific Vault roles (often bound to ServiceAccount JWTs via Vault's Kubernetes auth method). This is the most flexible and what most large fleets eventually adopt.

---

## 30. Cost and Latency Models

Multi-cluster is expensive. Honest cost model for a 10-cluster fleet on AWS:

| Item | Per cluster | × 10 clusters |
|---|---|---|
| Control plane (3 × m6i.xlarge) | ~$525/mo | $5,250/mo |
| EKS managed control plane (alternative) | ~$73/mo + 3 × $0.40/h cp | $730/mo + $9,000/mo |
| NAT gateway (per AZ) | $33/mo × 3 = $99/mo | $990/mo |
| NLB for apiserver | $20/mo | $200/mo |
| Cross-AZ data transfer | varies | typically $500–2000/mo |
| Cross-region data transfer (if multi-region) | $0.02/GB | depends, can be huge |
| etcd snapshots (S3) | $1/mo | $10/mo |
| Observability stack per cluster (Prometheus, Loki) | ~$50–500/mo | $500–5000/mo |

Even without workloads, a 10-cluster fleet costs $7000–15000/mo on infrastructure alone. The cross-region data transfer often *dominates*: pulling logs/metrics from edge clusters back to a central observability store can cost as much as the clusters themselves.

### 30.1 Latency

- Same AZ pod-to-pod: ~0.1 ms
- Cross-AZ pod-to-pod: ~1 ms
- Cross-cluster same VPC (Cilium ClusterMesh): ~1-2 ms
- Cross-cluster Submariner gateway: ~2-5 ms
- Cross-region (AWS regions): 20–250 ms RTT
- Submariner cross-region: + 5ms IPsec overhead

For interactive workloads, cross-region is borderline. For replication / batch / queue, cross-region is fine. For real-time service-to-service, keep traffic within a region — design with regional service meshes, not global ones.

### 30.2 The fragmentation tax

Each cluster has its own buffer (spare nodes for headroom). 10 clusters × 20% headroom = 2 extra clusters' worth of idle compute. In one big cluster, you'd need maybe 10% headroom. So multi-cluster *costs more in compute waste*, on top of the control-plane overhead.

The honest pitch for multi-cluster economics: you pay 30–80% more in infrastructure than a single cluster, in exchange for blast-radius isolation, regulatory compliance, and the ability to scale past one cluster's limits. Whether that trade is worth it depends entirely on your business.

---

## 31. Cluster Discovery: kubeconfig, contexts, registries

How does an operator find the right cluster?

### 31.1 kubeconfig contexts

The basic mechanism: one `~/.kube/config` (or many) with multiple contexts:

```yaml
apiVersion: v1
kind: Config
clusters:
  - name: prod-eu-west-1
    cluster: { server: https://..., certificate-authority-data: ... }
  - name: prod-us-east-1
    cluster: { server: https://..., certificate-authority-data: ... }
contexts:
  - name: prod-eu-west-1
    context: { cluster: prod-eu-west-1, user: deploy-bot }
  - name: prod-us-east-1
    context: { cluster: prod-us-east-1, user: deploy-bot }
users:
  - name: deploy-bot
    user:
      exec:
        apiVersion: client.authentication.k8s.io/v1
        command: aws
        args: [eks, get-token, --cluster-name, prod-eu-west-1]
current-context: prod-eu-west-1
```

`kubectl --context=prod-us-east-1 get pods` switches the target. Tools like `kubectx` and `kubens` make this less painful interactively.

### 31.2 Per-cluster kubeconfig files

For automation, the cleaner pattern is one kubeconfig file per cluster, with `KUBECONFIG` pointing at the right one:

```bash
KUBECONFIG=~/.kube/clusters/prod-eu-west-1 kubectl get pods
```

Pipelines do `KUBECONFIG=...; kubectl apply -f manifest.yaml`. Multi-context kubeconfigs are an interactive convenience; automation almost always uses per-cluster files.

### 31.3 Cluster registries

For *programmatic* discovery, the fleet needs a registry — "give me the list of all prod clusters in eu-west-1". Three options:

- **ArgoCD's Cluster Secrets**: each cluster is a Secret with type `cluster` in the `argocd` namespace. List by label. This is the most common in practice.
- **CAPI Cluster CRs**: list `Cluster` resources in the mgmt cluster, each has labels and a kubeconfig Secret.
- **OCM ManagedCluster CRs**: Open Cluster Management's primitive.
- **KCP workspaces**: each workspace is a discoverable unit.

### 31.4 Krew plugins

`krew` (kubectl plugin manager) hosts a long list of multi-cluster plugins:

- `kubectx` / `kubens` — interactive context/namespace switching.
- `view-utilization` — utilization across contexts.
- `iexec` — interactive exec selector across clusters.
- `multicluster` — run a command across all contexts in parallel.
- `kcc` — quick context switch.

For day-to-day operations on a 5–50 cluster fleet, these plugins are essential.

---

## 32. Observability Across Clusters

Each cluster has its own Prometheus, its own logs, its own traces. Aggregation is non-trivial.

### 32.1 Metrics: Thanos / Cortex / Mimir

The dominant pattern: each cluster runs Prometheus (chapter 30); a global aggregation layer (Thanos / Cortex / Mimir) provides a federated query interface.

```
GLOBAL METRICS WITH THANOS

   ┌─────────────────────────────────┐
   │  Grafana / Alertmanager         │
   │  query: rate(http_requests_total[5m])
   └────────────┬────────────────────┘
                │ PromQL
                ▼
   ┌─────────────────────────────────┐
   │  Thanos Query (querier)         │
   │  fans out to all clusters       │
   └─────┬────────┬────────┬─────────┘
         │        │        │
   ┌─────▼──┐ ┌──▼────┐ ┌──▼────┐
   │ Thanos │ │Thanos │ │Thanos │
   │Sidecar │ │Sidecar│ │Sidecar│
   │+ Prom  │ │+ Prom │ │+ Prom │
   │cluster1│ │cluster2│ │cluster3│
   └────────┘ └───────┘ └───────┘
       │           │           │
       └───────────┴───────────┘
                   ▼
           ┌──────────────┐
           │ S3 / GCS /   │  long-term storage
           │ Azure Blob   │  (blocks per cluster,
           │              │   queryable via Store API)
           └──────────────┘
```

Each cluster's Thanos Sidecar uploads Prometheus blocks to object storage. The Thanos Query layer reads from sidecars (fresh data) + Store gateways (historical, from S3). One query spans all clusters.

Cortex / Mimir use a different ingestion model (push from Prometheus remote-write, multitenant storage backend, queryable as one big logical Prometheus) but the user experience is similar.

### 32.2 Logs

Loki (with multi-tenancy by `X-Scope-OrgID` per cluster) or Elasticsearch / OpenSearch with cluster labels. Loki Operator + ApplicationSet is the common path.

Fluentd / Fluent Bit / Vector deploy as DaemonSets in each cluster, collecting from `/var/log/containers/`, attaching cluster + namespace labels, shipping to the central Loki / Elasticsearch.

### 32.3 Traces

OpenTelemetry Collector in each cluster, configured to send to a central OTLP receiver (Jaeger / Tempo / Honeycomb / etc.). Trace context propagates across cluster boundaries naturally — the trace ID is just a header.

### 32.4 The per-cluster label discipline

Every metric, log, and trace must carry a `cluster` label. Without it, you cannot disambiguate "deployments named frontend in three clusters". This is a discipline thing — enforce via Prometheus relabeling and the OTel resource processor.

---

## 33. Disaster Recovery

Three layers of DR:

### 33.1 Workload backup: Velero

Velero (`vmware-tanzu/velero`) backs up entire namespaces (manifests + PV snapshots). It supports cross-cluster restore: backup in cluster A, restore in cluster B (the manifests transfer; the PVs are restored via CSI VolumeSnapshot in the new cluster's storage).

```yaml
apiVersion: velero.io/v1
kind: Schedule
metadata:
  name: daily
  namespace: velero
spec:
  schedule: "0 2 * * *"
  template:
    includedNamespaces:
      - orders
      - inventory
    storageLocation: aws-s3
    volumeSnapshotLocations:
      - aws-ebs
    ttl: 720h
```

Velero is the workhorse for "we lost a cluster, restore everything to a new one". The catch: PV restores depend on the storage class being compatible. EBS snapshots restore to EBS. NFS to NFS. Cross-cloud restore is *hard*.

### 33.2 etcd snapshot for cluster control plane

Each cluster's etcd should be snapshotted (chapter 32). For self-managed clusters, this is the only way to recover from a control-plane disaster. For managed K8s (EKS/AKS/GKE), the cloud does this for you.

### 33.3 GitOps DR: rebuild from scratch

The ideal: there's no DR procedure for a workload cluster, you just delete it and let GitOps rebuild it. CAPI provisions a fresh cluster from `Cluster` CR. ApplicationSet deploys all workloads from Git. External Secrets restores secrets from Vault. Velero restores PV data.

End-to-end "rebuild a cluster from scratch" should be < 1 hour for a stateless cluster, ~half a day for a stateful one. The most important thing: *practice it*. Run an exercise quarterly where you delete a non-prod cluster and rebuild it. The first time you try this, things will be broken — that's why you practice.

---

## 34. Hybrid and Edge Fleets

Edge clusters are the multi-cluster pattern at its most extreme: thousands of small clusters, in retail stores / factories / cell towers, often with intermittent connectivity.

### 34.1 The distributions

- **K3s** (chapter 33): single-binary Kubernetes, runs in 512MB, SQLite or etcd, designed for edge.
- **MicroK8s**: Canonical's snap-based distro, ARM-friendly.
- **KubeEdge**: extends Kubernetes specifically for the edge — has a "cloud-side" controller and an "edge-side" node component that tolerates offline operation, plus device twin abstractions for IoT.
- **OpenYurt**: Alibaba's edge distro, tunnels back to a central control plane.
- **k0s**: another single-binary distro.

### 34.2 The hub: Rancher Fleet, OCM, KubeEdge

Fleet (§10) is purpose-built for this. The fleet-agent on each edge cluster connects out to the central Fleet controller; new manifests are pulled, applied, and ack'd. When the edge is offline, the cluster keeps running with the last known config; when it reconnects, it resyncs.

OCM (Open Cluster Management) has the same general pattern with `klusterlet` instead of fleet-agent.

KubeEdge takes it further: the edge cluster has a *partial* set of Kubernetes components, with the rest replaced by edge-specific ones (EdgeCore instead of kubelet, EdgeMesh instead of kube-proxy), and explicit support for device twin (IoT sensor state synced bidirectionally between cloud and edge).

### 34.3 Edge constraints

- Connectivity is intermittent. Push won't work; pull is mandatory.
- Hardware is small (ARM, 1-4 CPUs, 512MB-8GB RAM). The cluster must fit.
- Updates ship via OTA, must be atomic, must be revertible. Talos + image-based updates are the gold standard.
- Security is harder (physical access possible). Disk encryption, secure boot, attestation.

The "central + N edge" pattern is fundamentally a *very* asymmetric hub-and-spoke. The hub has all the smarts; the edges are mostly delivery surfaces.

---

## 35. Real-World Multi-Cluster Designs

A taxonomy of the most common production fleet designs, with the tools that fit each.

### 35.1 Prod / staging / dev

```
   ┌─────┐    ┌─────────┐    ┌─────┐
   │ dev │    │ staging │    │ prod│
   └─────┘    └─────────┘    └─────┘

   3 clusters, same region, different environments.
   GitOps: one ApplicationSet, env labels select target.
   Use case: every team with strict prod isolation.
```

Tools: ArgoCD ApplicationSet + CAPI (or managed K8s like EKS). The simplest multi-cluster setup; ~80% of teams operate at this scale.

### 35.2 Region-per-cluster

```
   ┌─────┐  ┌─────┐  ┌─────┐  ┌─────┐
   │ EU  │  │ US  │  │APAC │  │ME-1 │
   └─────┘  └─────┘  └─────┘  └─────┘

   1 cluster per geo region.
   Same workload deployed everywhere (Karmada Divided
   or ApplicationSet per-region overlay).
   Cross-cluster service discovery via Cilium ClusterMesh
   or Submariner (rare; usually each region is independent).
```

Tools: ArgoCD or Karmada + region-specific data stores. Used by global SaaS companies, content platforms, anything with users on multiple continents.

### 35.3 Tenant-per-cluster

```
   ┌───────────────┐ ┌───────────────┐ ┌───────────────┐
   │ Tenant Acme   │ │ Tenant Globex │ │ Tenant ...    │
   │ (their own    │ │ (their own    │ │               │
   │ cluster)      │ │ cluster)      │ │               │
   └───────────────┘ └───────────────┘ └───────────────┘

   Strong isolation between customers.
   Each cluster provisioned via CAPI or Crossplane.
   Workload (the customer's app) deployed independently.
```

Tools: CAPI (mass-provisioning) or Crossplane Compositions; ArgoCD ApplicationSet to deploy the per-tenant platform stack; cluster-per-tenant gives bulletproof isolation but is operationally heavy. Used by enterprise SaaS, regulated industries, vCluster as a cheaper alternative.

### 35.4 Workload-per-cluster

```
   ┌──────────┐ ┌────────────┐ ┌──────────┐ ┌─────────┐
   │ web      │ │ batch      │ │ ML       │ │ CI/CD   │
   │ cluster  │ │ cluster    │ │ cluster  │ │ cluster │
   │ (HPA-y)  │ │ (queue,    │ │ (GPUs,   │ │ (heavy  │
   │          │ │  spot-only)│ │  static  │ │  builds)│
   │          │ │            │ │  CPU)    │ │         │
   └──────────┘ └────────────┘ └──────────┘ └─────────┘

   Clusters optimized per workload.
   Each cluster has different node types, scheduler config,
   admission policies.
```

Tools: CAPI with per-cluster MachineDeployments tuned to workload (GPUs, spot, ARM, FPGAs); separate ArgoCD/Karmada policies for each. Used by data-platform teams.

### 35.5 Edge + central

```
                    ┌──────────────┐
                    │ central      │
                    │ control      │ ◄── ArgoCD / Fleet / OCM
                    │ cluster      │
                    └──────┬───────┘
                           │ pull
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
        ┌───────┐      ┌───────┐      ┌───────┐
        │ edge1 │      │ edge2 │      │ edgeN │  (1 → 10000)
        │ K3s   │      │ K3s   │      │ K3s   │
        └───────┘      └───────┘      └───────┘

   Massive asymmetry: 1 central cluster, N edge clusters.
   Edge runs lightweight K8s. Central manages everything.
```

Tools: Rancher Fleet + K3s + KubeEdge / OpenYurt. Used by retail, IoT, telco edge.

---

## 36. Pitfalls: The Long List

Multi-cluster pitfalls. Each of these has bitten a real team in production.

1. **CAPI management cluster as SPOF.** No HA, no backups, no DR runbook. One day someone deletes the management namespace and the entire fleet's lifecycle controller is gone. Workload clusters keep running, but no upgrades, no scaling, no new clusters until you restore. Always run the mgmt cluster HA + back up its etcd every 15 minutes.

2. **CRD version drift across clusters.** ApplicationSet deploys CRD v1 to cluster A. Someone manually applies v2 in cluster B. CRs that conform to v2 in B don't fit v1 in A. Cross-cluster propagation breaks subtly. Enforce CRD versions via GitOps, never edit them in-cluster.

3. **Secrets per cluster getting out of sync.** Team A rotates a Vault credential in cluster X but forgets cluster Y. Cluster Y's workload fails after the next refresh. Use ESO with `refreshInterval` short enough that drift is bounded; alert on stale secret age.

4. **Cross-cluster service discovery broken by NetworkPolicy.** You enable a default-deny NetworkPolicy in cluster A. Submariner / ClusterMesh traffic from cluster B is now blocked at the namespace boundary in A. Allow the multicluster gateway's source IPs (or use Cilium's identity-based policies that recognize remote-cluster identities).

5. **Submariner gateway pods evicted.** Node pressure → kubelet evicts the gateway → cluster mesh down for 30 seconds while the gateway reschedules. Pin gateway pods with high `priorityClass` and node selectors.

6. **Workload propagation conflict with HPA.** Karmada propagates a Deployment with `replicas: 8`. HPA in member cluster scales it to 20. Karmada's controller reconciles it back to 8. Fight forever. Solution: use Karmada's HPA propagation, or annotate the Deployment to exempt the replicas field from sync.

7. **Karmada scheduler decisions not respecting local capacity.** Karmada places 50 replicas in cluster A based on a ResourceSummary that's stale. cluster A doesn't have room. Pods are Pending. Karmada doesn't reschedule promptly. Tune Karmada's scheduler resync interval, use realistic resource summaries, set ResourceQuotas as guardrails.

8. **OIDC issuer URL mismatch on federated workloads.** Workload Identity from cluster A → service in cluster B. The JWT's `iss` claim is `cluster-A.example.com/oidc`. Cluster B's apiserver doesn't trust that issuer. Authentication fails with cryptic 401s. Configure issuer trust correctly; document the cross-cluster trust matrix.

9. **Restoring Velero with wrong storage class.** Backup was on `gp2`. Restore happens in a cluster where the default is `gp3`. PVC requests `gp2`, fails to bind. Use a Velero restore hook to remap storage classes.

10. **ApplicationSet generators producing invalid output.** A bug in your template references `{{metadata.labels.region}}` but the cluster has no region label → "region" string is empty → path becomes `apps/frontend/overlays/` (trailing slash, invalid). ApplicationSet creates an unsyncable Application. Set `goTemplate: true` and use defaults: `{{ default "default" .metadata.labels.region }}`.

11. **Manual changes on member clusters drifting from GitOps.** Someone runs `kubectl edit deployment frontend` on cluster A in a hurry. ArgoCD `selfHeal` reverts it 60 seconds later. A debug change is lost. Solution: lock down kubectl access via RBAC; use ephemeral debug namespaces.

12. **Multi-region latency in operator reconcile loops.** An operator running in the mgmt cluster watches resources via watch streams to member clusters in another region. Watch reconnect takes 100ms+ over the WAN. Reconcile latency spikes; the operator looks broken. Use the OCM agent pattern: run the operator IN the member cluster, not centrally.

13. **etcd snapshot from wrong cluster restored.** Two clusters' snapshots in the same S3 bucket. Operator restores cluster A's snapshot into cluster B. Now cluster B thinks it's cluster A. Catastrophe. Use distinct buckets / prefixes; tag snapshots with cluster names; require manual confirmation of source/dest.

14. **Per-cluster cert rotation drift.** kubeadm certs in cluster A expired and were renewed manually 6 months ago. In cluster B nobody renewed them. Cluster B silently stops working when certs expire. Use `kubeadm certs check-expiration` or CAPI's auto-rotation (`KubeadmControlPlane.spec.rolloutBefore.certificatesExpiryDays`).

15. **Over-fragmentation.** 50 clusters for 30 teams when 3 (prod/staging/dev) with namespaces would suffice. Costs explode, observability is fragmented, operational burden is 10× higher. Periodically review: does this cluster *need* to exist as a separate cluster? If you can't articulate the blast-radius / regulatory / scale reason, consolidate.

16. **CIDR conflicts.** Cluster A's pod CIDR is `10.0.0.0/16`. Cluster B's pod CIDR is `10.0.0.0/16`. You enable cross-cluster routing. Packets routed to `10.0.42.5` go to *both* clusters' pods. Use Submariner globalnet, or (better) plan non-overlapping CIDRs from day one — keep a CIDR registry.

17. **Pivot during CAPI install fails partway.** `clusterctl move` crashes after moving half the CRs. CRs exist in both bootstrap and mgmt clusters. You delete from one side, the other still has finalizers, the cleanup is nightmarish. Practice the pivot in a sandbox first; do the real pivot on a quiet network.

18. **MachineHealthCheck thrashing on a cluster-wide problem.** Your CNI is briefly broken; nodes go NotReady; MHC replaces them all simultaneously; the replacements also fail because the CNI issue persists; cluster is now empty of nodes. Set `maxUnhealthy` low (10–30%); MHC stops replacing when too many are bad simultaneously.

19. **Cluster autoscaler vs Karpenter conflict.** Both running in the same cluster. CA creates a node, Karpenter sees it as "drifted" and deletes it. Forever loop. Disable one; production fleets pick Karpenter for new clusters.

20. **ApplicationSet pruning a critical Application during cluster removal.** You remove a cluster label; ApplicationSet's `clusters` generator no longer matches; ApplicationSet *prunes* the Application; the Application has `prune: true` which deletes the workload; production is gone. Use `preserveResourcesOnDeletion: true` or carefully manage cluster lifecycle ordering.

21. **Cross-cluster mTLS cert expiry.** Istio multi-primary uses a shared root CA. Intermediate CA expires in 1 year. You forgot. All inter-cluster mTLS fails simultaneously across the fleet. Monitor cert expiry; automate rotation via cert-manager or Spire.

22. **`clusterset.local` DNS bypassed by service mesh.** Submariner's Lighthouse handles `clusterset.local`, but your Istio sidecar's DNS-proxy doesn't. Workload's request to `payments.default.svc.clusterset.local` fails. Configure mesh DNS proxy to delegate `clusterset.local` to CoreDNS.

23. **Workload Identity not federated to remote cluster.** A workload in cluster A uses IRSA / GKE Workload Identity. In cluster B, the OIDC issuer is different. The same workload doesn't authenticate. Use a federated identity setup (e.g., Workload Identity Pools that trust multiple cluster issuers).

24. **Karmada's resourceInterpreter for a custom CRD missing.** Karmada by default knows about built-in K8s types. For a custom CRD (e.g., `Rollout`), you need to register a ResourceInterpreter. Without it, replica scheduling for that CRD is wrong. Register a Lua interpreter or a webhook.

25. **CAPI ClusterClass changes triggering fleet-wide rollout.** You update the ClusterClass AMI. All 50 clusters begin rolling. Aggregate disruption is unacceptable. Pause ClusterClass propagation; roll out via annotations on subsets of clusters; use Argo CD progressive syncs to throttle.

26. **vCluster as the multi-cluster alternative being used wrong.** vCluster (chapter 25) gives virtual control planes inside one host. It's not a *real* cluster boundary for blast-radius; if the host cluster's apiserver dies, all vClusters die. Don't sell vCluster as multi-cluster DR.

27. **Crossplane composition drift.** You change a Composition; existing XRs do *not* automatically reconcile to it (depending on revision strategy). Two XRs created at different times have different effective configs. Use `CompositionRevision` and explicit revision selection.

28. **CAPI MachinePool replicas drift from cloud ASG.** A MachinePool says `replicas: 10`. AWS ASG was manually edited to 15. CAPI reconciles back to 10. Or, depending on settings, leaves it. Drift detection is settings-dependent. Always change via CAPI, never the cloud console.

29. **Fleet bundle that doesn't fit edge constraints.** You bundle a 200MB Helm chart for a Fleet edge rollout. The edge has 100MB free disk. Bundle fails. Tune your manifests for edge sizing.

30. **Observability cardinality explosion.** A `cluster` label times a `pod` label times a `route` label = millions of unique series. Prometheus OOMs. Use recording rules to pre-aggregate, drop high-cardinality labels at ingestion (relabeling), or use Mimir/Cortex with sharding.

---

## 37. TL;DR

- Multiple clusters exist because the cluster boundary is the only one that doesn't share fate. Use it for blast radius, geography, regulation, K8s version skew, hard tenancy, and scale ceilings — not for "every team gets a cluster". When in doubt, prefer namespaces (chapter 25).
- The multi-cluster problem has four orthogonal axes: lifecycle (CAPI / Crossplane), workload propagation (ArgoCD ApplicationSet / Fleet / Karmada), connectivity (Submariner / Cilium ClusterMesh / Istio multi-primary), federation (KCP / kubefed-legacy). Use a separate tool per axis.
- **CAPI** is the de-facto cluster lifecycle operator: `Cluster` → `KubeadmControlPlane` + `<Infra>Cluster`, `MachineDeployment` → `Machine` → `<Infra>Machine`. Mirrors Deployment/ReplicaSet/Pod for nodes. Bootstrap via `clusterctl move` (the pivot). ClusterClass + Topology kills YAML duplication at fleet scale.
- **Karmada** federates *workloads*: submit a Deployment to the karmada-apiserver, PropagationPolicy + OverridePolicy + karmada-scheduler send it to member clusters with weighted replica distribution. Avoids kubefed v2's wrapper-CRD trap.
- **Argo CD ApplicationSet** is the GitOps-native alternative: one ApplicationSet, N Applications generated via cluster/git/matrix generators. Most common in practice.
- **Rancher Fleet** wins for edge fleets: pull-mode agent, canary rollouts built in, scales to thousands of small clusters.
- Cross-cluster service discovery converges on the **MCS API** (`ServiceExport` + `ServiceImport`). Underlying implementations: **Submariner** (IPsec gateway), **Cilium ClusterMesh** (pod-direct, lower overhead), **Istio multi-primary** (L7 mesh), cloud-native (VPC Lattice, GCP MCS).
- **Crossplane** is a meta-operator: any cloud resource (RDS, S3, EKS) as a CRD. Overlaps CAPI for cluster provisioning; in practice the two are combined (Crossplane for cloud infra, CAPI for cluster lifecycle).
- **KCP** is the workspaces-instead-of-clusters approach — one apiserver, hierarchical workspaces, syncers to physical clusters. Aimed at SaaS platforms, not typical multi-cluster fleets.
- Most multi-cluster systems are **hub-and-spoke** (CAPI, ArgoCD, Karmada, Fleet, KCP); **mesh** is mostly the data plane (Cilium ClusterMesh). Hub-and-spoke is simpler; the mgmt cluster becomes the precious SPOF — keep it HA, back up its etcd, and treat the Git repo as the real source of truth.
- **GitOps + External Secrets + CAPI + ArgoCD ApplicationSet** is the modal production stack. A cluster is rebuildable from scratch in < 1 hour by applying the Git repo to a fresh CAPI Cluster CR.
- **Costs** are real: 10 clusters costs 5–10× more in infra than one big cluster, plus cross-region data transfer plus per-cluster observability plus operational overhead. Multi-cluster is the right answer only when the cluster boundary buys you something nothing else can.
- **Pitfalls** are everywhere: CRD drift, secrets drift, CIDR overlap, OIDC mismatch, ClusterClass fleet-wide rollouts, MHC thrashing, gateway eviction, vCluster mistaken for real isolation, cert expiry, observability cardinality. Practice DR. Lock everything behind GitOps. When you can't articulate why a cluster needs to exist as its own cluster, consolidate it.
