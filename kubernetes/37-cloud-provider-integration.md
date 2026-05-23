# Cloud Provider Integration: A Staff-Level Deep Dive

How Kubernetes plugs into the cloud underneath it. For most of its history, Kubernetes contained a special-cased fork for every cloud that mattered — AWS, GCP, Azure, OpenStack, vSphere, Equinix Metal, plus a handful of regional providers — and shipped that code in the same binary as `kube-controller-manager` and `kubelet`. Every patch to the AWS ELB tagging logic forced a rebuild of the core. Every CVE in `cloud-provider-vsphere` shipped through `kubernetes/kubernetes` releases. Every cloud team had to negotiate merge windows with the SIG-Release calendar. KEP-2395 (and the dozen earlier KEPs it followed) finally cut that knot: cloud code moved *out of tree* into per-provider repositories, and Kubernetes itself only contains a generic interface (`pkg/cloudprovider/cloud.go`) plus the controllers that consume it.

This chapter is the staff-level reference for that boundary. It walks the Cloud Controller Manager (CCM) — what it is, what it owns, how it interlocks with `kube-controller-manager` and `kubelet` — and then drills into the three big clouds: AWS (with IRSA, EKS Pod Identity, the AWS Load Balancer Controller, VPC CNI, EBS/EFS CSI, Karpenter), GCP (with Workload Identity, GKE Ingress, Dataplane v2, Autopilot, GCE/Filestore CSI), and Azure (with Workload Identity, the App Gateway Ingress Controller, Disk/File CSI, AKS Virtual Nodes). It then covers the cloud-agnostic glue that staff engineers always end up wiring: ExternalDNS for DNS records, cert-manager and ACME for certificates (vs cloud-managed ACM-style certs), the External Secrets Operator and Secrets Store CSI Driver for secret material, KMS provider plugins for at-rest envelope encryption, and the multi-cloud abstractions (Crossplane, Cluster API). It closes with a pitfall catalog — the failure modes that show up in real outages, in roughly the frequency they show up in postmortems.

If you have already read chapter 03 (architecture — where CCM sits in the control plane), chapter 07 (workload identity — where SA tokens become cloud creds), chapter 14 (Services and LoadBalancer — what the service controller actually reconciles), chapter 19 (CSI — the storage interface that replaced in-tree volume drivers), chapter 22 (Karpenter — the autoscaler that calls cloud APIs to launch nodes), and chapter 26 (multi-cluster — CAPI providers and fleet patterns), this chapter is where all of those meet the cloud underneath. Everything here is the consequence of one design choice: *the cloud is a separate process, talking to the apiserver over the same watch/reconcile API as everything else, with its own credentials, its own release cadence, and its own failure domain*. Internalize that and the rest is implementation detail.

---

## Table of Contents

1. [TL;DR](#1-tldr)
2. [The Old In-Tree World](#2-the-old-in-tree-world)
3. [Why In-Tree Had To Die](#3-why-in-tree-had-to-die)
4. [The Out-Of-Tree Migration: KEP-2395 and Friends](#4-the-out-of-tree-migration-kep-2395-and-friends)
5. [Cloud Controller Manager: What It Is](#5-cloud-controller-manager-what-it-is)
6. [The `cloud.Interface` Go Contract](#6-the-cloudinterface-go-contract)
7. [The Node Controller (Cloud Half)](#7-the-node-controller-cloud-half)
8. [`providerID` and Why It Matters](#8-providerid-and-why-it-matters)
9. [The `node.cloudprovider.kubernetes.io/uninitialized` Taint](#9-the-nodecloudproviderkubernetesiouninitialized-taint)
10. [The Route Controller](#10-the-route-controller)
11. [The Service Controller: LoadBalancer Provisioning](#11-the-service-controller-loadbalancer-provisioning)
12. [Cloud LB Types: NLB vs ALB vs Internal vs Global](#12-cloud-lb-types-nlb-vs-alb-vs-internal-vs-global)
13. [Service Annotations: The De-Facto API](#13-service-annotations-the-de-facto-api)
14. [The Legacy Volume Controller and Why It Is Gone](#14-the-legacy-volume-controller-and-why-it-is-gone)
15. [Running CCM: Deployment, RBAC, Leader Election](#15-running-ccm-deployment-rbac-leader-election)
16. [AWS: Cloud Controller Manager](#16-aws-cloud-controller-manager)
17. [AWS Load Balancer Controller](#17-aws-load-balancer-controller)
18. [AWS VPC CNI and Pod IP = VPC IP](#18-aws-vpc-cni-and-pod-ip--vpc-ip)
19. [EBS CSI and EFS CSI](#19-ebs-csi-and-efs-csi)
20. [IRSA: IAM Roles for Service Accounts](#20-irsa-iam-roles-for-service-accounts)
21. [EKS Pod Identity](#21-eks-pod-identity)
22. [Karpenter and the Node Lifecycle](#22-karpenter-and-the-node-lifecycle)
23. [GCP: Cloud Controller Manager](#23-gcp-cloud-controller-manager)
24. [GCE PD CSI and Filestore CSI](#24-gce-pd-csi-and-filestore-csi)
25. [GKE Workload Identity](#25-gke-workload-identity)
26. [GKE Ingress and the Managed HTTP(S) LB](#26-gke-ingress-and-the-managed-https-lb)
27. [GKE Dataplane v2](#27-gke-dataplane-v2)
28. [GKE Autopilot](#28-gke-autopilot)
29. [Azure: Cloud Provider Azure](#29-azure-cloud-provider-azure)
30. [Azure Disk CSI and Azure File CSI](#30-azure-disk-csi-and-azure-file-csi)
31. [Application Gateway Ingress Controller](#31-application-gateway-ingress-controller)
32. [Azure AD Workload Identity](#32-azure-ad-workload-identity)
33. [AKS Virtual Nodes](#33-aks-virtual-nodes)
34. [External DNS](#34-external-dns)
35. [Cloud Certificates: ACM vs cert-manager](#35-cloud-certificates-acm-vs-cert-manager)
36. [External Secrets Operator](#36-external-secrets-operator)
37. [Secrets Store CSI Driver](#37-secrets-store-csi-driver)
38. [KMS Provider Plugin and At-Rest Encryption](#38-kms-provider-plugin-and-at-rest-encryption)
39. [Cross-AZ Networking Costs and Topology-Aware Routing](#39-cross-az-networking-costs-and-topology-aware-routing)
40. [Cost Optimization Patterns](#40-cost-optimization-patterns)
41. [Managed Kubernetes Upgrade Mechanics](#41-managed-kubernetes-upgrade-mechanics)
42. [Multi-Cloud Abstractions: Crossplane, CAPI, Portable CSI/CNI](#42-multi-cloud-abstractions-crossplane-capi-portable-csicni)
43. [Pitfalls: The Long Catalogue](#43-pitfalls-the-long-catalogue)
44. [Operator's Cheat Sheet](#44-operators-cheat-sheet)
45. [Further Reading and Source Pointers](#45-further-reading-and-source-pointers)

---

## 1. TL;DR

- **Cloud Controller Manager (CCM)** is a separate binary that owns every cloud-touching control loop. It replaces the cloud-specific paths that used to live inside `kube-controller-manager` and `kubelet`.
- To enable it, both `kubelet` and `kube-controller-manager` get `--cloud-provider=external`. CCM gets the actual cloud SDK and credentials. This is non-negotiable on any cluster created since Kubernetes 1.31 — the in-tree code was removed.
- CCM owns three controllers: the **node controller** (cloud half — addresses, labels, `providerID`, init-taint removal, instance-gone cleanup), the **route controller** (VPC routes for non-overlay clusters — mostly obsolete in 2026), and the **service controller** (LoadBalancer provisioning and reconciliation). A fourth, the volume controller, was deleted as in-tree volume plugins were removed in 1.26 in favour of CSI.
- The interface CCM implements lives in [kubernetes/cloud-provider](https://github.com/kubernetes/cloud-provider) (`cloud.Interface`, `InstancesV2`, `LoadBalancer`, `Zones`, `Routes`, `Clusters`). Each cloud has its own repo: [kubernetes/cloud-provider-aws](https://github.com/kubernetes/cloud-provider-aws), [kubernetes/cloud-provider-gcp](https://github.com/kubernetes/cloud-provider-gcp), [kubernetes-sigs/cloud-provider-azure](https://github.com/kubernetes-sigs/cloud-provider-azure), plus dozens of community providers.
- **`providerID`** has the form `<cloud>://<region/zone>/<instance-id>` (with cloud-specific shapes — `aws:///us-east-1a/i-0abc...`, `gce://my-project/us-central1-a/gke-cluster-...`, `azure:///subscriptions/.../virtualMachines/...`). It is the durable join key between a Node and a cloud VM; controllers that mismatch it leak resources or orphan billing.
- **`node.cloudprovider.kubernetes.io/uninitialized:NoSchedule`** is applied by kubelet at registration when running with `--cloud-provider=external`. It blocks all scheduling until the CCM has decorated the Node with addresses, region/zone labels, and providerID. Forgetting to deploy CCM means *every node stays unschedulable*. This is the #1 outage in DIY clusters.
- **Workload identity** is the cloud-side mirror of ServiceAccount identity. AWS uses **IRSA** (OIDC-based federation via STS `AssumeRoleWithWebIdentity`) or the newer **EKS Pod Identity** (an agent + a simpler trust model). GCP uses **GKE Workload Identity** (KSA ↔ GSA via `roles/iam.workloadIdentityUser`, served by `gke-metadata-server`). Azure uses **Azure AD Workload Identity** (federated credentials on Azure AD apps, replacing the deprecated AAD Pod Identity).
- **Secrets out of cloud KMS** come into the cluster two ways: **External Secrets Operator** materializes them as K8s `Secret` objects (sync-driven, cache-friendly, but writes to etcd); **Secrets Store CSI Driver** mounts them as files in pods (per-volume isolation, no etcd footprint, auto-rotation). Pick one, not both.
- **At-rest encryption of secrets in etcd** uses the **KMS provider plugin** (`--encryption-provider-config` on the apiserver). KMS v2 (GA in 1.29) does per-DEK encryption with the cloud KMS as the KEK, replacing the v1 single-key model that bottlenecked on KMS API calls.
- **Cloud LB → pod** routing has two patterns: (a) cloud LB → node IP → kube-proxy → pod (the legacy NodePort dance), or (b) cloud LB → pod IP directly (AWS ALB IP target mode, GCP NEG, Azure backend pool with pod IPs). Pattern (b) cuts a hop, eliminates SNAT, and preserves client IP. Use it.
- **Cross-AZ traffic** costs $0.01–$0.02 per GB in every direction on every major cloud. Topology-aware routing (`service.kubernetes.io/topology-mode: Auto`, `internalTrafficPolicy: Local`) is not a performance optimization, it is a cost optimization that often pays for the cluster.
- **The pitfall list is long.** Wrong `providerID`, missing CCM, missing init-taint removal, IRSA trust policy typos, ALB target group misconfig, ExternalDNS not authoritative for the zone, cert-manager HTTP-01 on a private cluster, KMS plugin degraded, NAT gateway as SPOF, CCM upgrade during leader-held lease — all of these recur in postmortems. Section 43 catalogues them with diagnosis.

The one sentence: **cloud-provider integration is not magic — it is a separate process, with its own credentials, that watches the apiserver and writes to the cloud.** Once you can articulate which controller is responsible for which annotation, every "why isn't my LoadBalancer provisioning?" stops being a mystery.

---

## 2. The Old In-Tree World

Up to roughly Kubernetes 1.10, every cloud lived inside `kubernetes/kubernetes`. The package was `pkg/cloudprovider/providers/`, with subdirectories `aws`, `gce`, `azure`, `openstack`, `vsphere`, `cloudstack`, `ovirt`, `photon`, and so on. Each subdirectory implemented the same Go interface, and at process start `kube-controller-manager` consulted the `--cloud-provider=<name>` flag, instantiated the matching provider, and handed it to the cloud-touching control loops. The same was true of `kubelet`, which used the cloud provider to discover its own metadata (instance ID, instance type, hostname, zone) before registering with the apiserver.

The architecture looked like this:

```
                       in-tree world (pre-1.10)

  ┌─────────────────────────────────────────────────────────────────┐
  │  kube-controller-manager binary                                 │
  │                                                                 │
  │  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐    │
  │  │ Node ctrl      │  │ Service ctrl   │  │ Route ctrl     │    │
  │  │  (addresses,   │  │  (LB lifecycle)│  │  (VPC routes)  │    │
  │  │   taints, GC)  │  │                │  │                │    │
  │  └───────┬────────┘  └───────┬────────┘  └───────┬────────┘    │
  │          │                   │                   │              │
  │          ▼                   ▼                   ▼              │
  │  ┌────────────────────────────────────────────────────────┐    │
  │  │     cloudprovider.Interface (Go)                       │    │
  │  └────────────────────────────────────────────────────────┘    │
  │       │           │           │           │          │          │
  │       ▼           ▼           ▼           ▼          ▼          │
  │  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐    │
  │  │ aws/   │  │ gce/   │  │ azure/ │  │openstack│ │vsphere/│    │
  │  └────────┘  └────────┘  └────────┘  └────────┘  └────────┘    │
  │                                                                 │
  │  All linked into one Go binary. ~600 MB of cloud SDKs.         │
  └─────────────────────────────────────────────────────────────────┘
                  │
                  ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  kubelet binary (same problem at smaller scale)                 │
  │  Uses cloudprovider to read NodeAddresses, InstanceType, Zone   │
  │  Same --cloud-provider=<name> flag, same set of providers       │
  └─────────────────────────────────────────────────────────────────┘
```

The relevant in-tree controllers were:

- **Node controller (cloud half).** When a Node registered, this controller called `cloud.Instances().NodeAddresses(ctx, nodeName)` to populate `status.addresses`, called `Instances().InstanceType(ctx, nodeName)` to label, and called `Zones().GetZoneByNodeName(ctx, nodeName)` to set `topology.kubernetes.io/region` and `topology.kubernetes.io/zone`. Periodically it called `Instances().InstanceExistsByProviderID(ctx, providerID)` and, on `false`, deleted the Node object.
- **Service controller.** Watched Services of type LoadBalancer. On create, called `LoadBalancer().EnsureLoadBalancer(ctx, clusterName, service, nodes)`; on update, `UpdateLoadBalancer`; on delete, `EnsureLoadBalancerDeleted`. The provider returned a `LoadBalancerStatus` (a list of `ingress` entries with hostname/IP), which the controller wrote back to `service.status.loadBalancer.ingress[]`.
- **Route controller.** For non-overlay networking (i.e., before VPC-aware CNIs were the default), this controller programmed cloud VPC routing tables so that pod CIDR ranges per node were reachable across the underlay. AWS ran it for non-VPC-CNI clusters; GCE ran it for cluster-native routing; Azure ran it for kubenet.
- **Volume controller (legacy).** Provisioned, attached, and detached cloud block volumes. AWS EBS, GCE PD, Azure Disk, Cinder, and vSphere VMDK all had in-tree implementations. The PV provisioner watched PVCs with a matching StorageClass; the attach/detach controller watched Pod schedules and called cloud APIs to attach the underlying volume to the chosen node.

The kubelet used the provider for self-discovery: `NodeAddresses(nodeName)` to set `status.addresses` at registration, `InstanceID` to set `spec.providerID`, and `Zones` to label itself. This is why old clusters had `--cloud-provider=aws` on both `kube-controller-manager` and `kubelet`. The same SDK, in two processes, with two sets of cloud credentials, doing parallel work.

Everything cloud-related was glued in this way. It mostly worked. It was also, in retrospect, a disaster.

---

## 3. Why In-Tree Had To Die

The problems were structural, and SIG-Cloud-Provider documented them at length in KEP-2395 and the predecessor KEP-0002. The short list:

**Release coupling.** Every patch to AWS ELB tagging logic — or any cloud-specific bug fix — required cutting a Kubernetes release. The Kubernetes release train runs three minors per year, with patch releases roughly monthly. A bug in `cloud-provider-aws` that needed an emergency fix had to wait for the next patch release window, get sign-off from SIG-Release, and ride a kube release out the door. Cloud teams shipping urgent fixes against AWS API changes were on a release cadence they did not control.

**Bloated binary.** `kube-controller-manager` linked the AWS SDK, the GCP SDK, the Azure SDK, the OpenStack client, the vSphere client, the CloudStack client, and so on. The binary was approaching 300 MB even with tree-shaking. Every cluster shipped code for every cloud, used or not. The image pull cost on slow networks was non-trivial; the memory footprint was material.

**Compiled-in cloud bugs.** A panic in `pkg/cloudprovider/providers/openstack` could crash `kube-controller-manager` and take down the cluster, even if the cluster didn't use OpenStack. Static linking meant every cloud's bugs were every cluster's risk.

**No private cloud forks.** Companies running private clouds (Yahoo's flavour, Alibaba's, etc.) had to fork `kubernetes/kubernetes` to add their provider. Maintaining a long-lived patch series against a fast-moving upstream is a full-time job for at least one engineer. Several companies did it anyway, badly.

**Test surface.** SIG-Release had to gate kube on tests for every cloud's provider. AWS, GCE, Azure, OpenStack, and vSphere all had e2e suites that ran on every PR. The CI fleet was huge, expensive, and frequently flaky in cloud-specific ways that had nothing to do with the code being reviewed.

**Velocity asymmetry.** Cloud APIs evolve at the speed of the cloud vendor. Kubernetes' API evolves at the speed of SIG-Architecture. Forcing them to share a release cadence was always going to chafe. AWS adding a new instance type, GCP adding a new disk type, Azure renaming a resource group — all of these are routine for the cloud teams and untenable for SIG-Release as in-tree changes.

The fix was obvious in retrospect: split the cloud code out of tree, define a stable Go interface, and let each cloud ship its own binary on its own cadence. This is the path KEP-2395 codified, and what got executed across roughly five years of careful migration.

---

## 4. The Out-Of-Tree Migration: KEP-2395 and Friends

The out-of-tree migration was not one KEP. It was a sequence:

- **KEP-0002 (2016):** the original "External Cloud Provider" proposal. Introduced the `--cloud-provider=external` flag and built the CCM scaffolding.
- **KEP-2392 / KEP-2395 / KEP-2440 / KEP-2452:** per-provider removal KEPs for AWS, GCE, Azure, and OpenStack. Each one tracked the in-tree code's deprecation, the parallel out-of-tree implementation, and the eventual removal.
- **KEP-625 (CSI migration):** moved each in-tree volume plugin behind a translation shim that proxied to the CSI driver, then deleted the in-tree plugin once the CSI driver was the default.

The deprecation timeline was conservative. The flag `--cloud-provider=<name>` (non-external) printed deprecation warnings starting around 1.19. The in-tree CSI plugins were deprecated in 1.21 and removed in 1.26 (replaced entirely by CSIMigration). The in-tree cloud-provider controllers were removed in 1.31; from that release, running `kube-controller-manager` with anything other than `--cloud-provider=external` (or an empty value for non-cloud clusters) fails to start.

The migration shape:

```
                      out-of-tree world (1.31+)

  ┌──────────────────────────────────┐         ┌──────────────────────────────────┐
  │  kube-controller-manager         │         │  kubelet                         │
  │  --cloud-provider=external       │         │  --cloud-provider=external       │
  │                                  │         │  (does NOT load any cloud SDK)   │
  │  No cloud SDKs linked.           │         │                                  │
  │  Cloud-touching controllers      │         │  Reads its own metadata from     │
  │  are SKIPPED at startup.         │         │  cloud IMDS (169.254.169.254),   │
  │                                  │         │  registers with                  │
  │                                  │         │  status.addresses=[InternalIP],  │
  │                                  │         │  spec.providerID="" (initially), │
  │                                  │         │  and the init taint applied.    │
  └──────────────────────────────────┘         └──────────────────────────────────┘
                  │                                            │
                  │                                            │
                  ▼                                            ▼
       ┌──────────────────────────────────────────────────────────────┐
       │  kube-apiserver  (the only shared substrate)                │
       └──────────────────────────────────────────────────────────────┘
                  ▲
                  │ watch Nodes, Services, ...
                  │
       ┌──────────────────────────────────────────────────────────────┐
       │  cloud-controller-manager   (separate Deployment/DaemonSet) │
       │                                                              │
       │  Linked against ONE cloud SDK (this binary is per-cloud).   │
       │                                                              │
       │  ┌────────────┐  ┌────────────┐  ┌────────────┐              │
       │  │ Node ctrl  │  │ Service    │  │ Route ctrl │              │
       │  │ (cloud half│  │  ctrl      │  │            │              │
       │  └────────────┘  └────────────┘  └────────────┘              │
       │                                                              │
       │  Auth to cloud:                                              │
       │   - AWS:  IMDSv2 + IAM role on the control-plane nodes,      │
       │           OR IRSA on EKS,                                    │
       │           OR static keys (anti-pattern).                     │
       │   - GCP:  Workload Identity for the CCM SA,                  │
       │           OR the node's GCE service account.                 │
       │   - Az:   Managed Identity on the control-plane VM(SS),      │
       │           OR Workload Identity, OR client secret.            │
       └──────────────────────────────────────────────────────────────┘
                  │
                  ▼
            (cloud API)
```

Two things changed visibly. First, the cloud SDK is in *one* process: the CCM. Neither kubelet nor kube-controller-manager links it. Second, the credentials live with the CCM — usually a tightly-scoped IAM role/service account/managed identity granted exactly the rights to read instance metadata, manage load balancers, and (on some clouds) manipulate routes. The principle of least privilege applies at the binary level, not just at the API level.

A subtle but important consequence: **the cluster boots in a degraded state**. Until the CCM is up and processing nodes, every kubelet registers with the init taint, and no workload schedules. On managed Kubernetes (EKS/GKE/AKS) this is invisible because the cloud bootstraps the CCM before any user workloads. On self-managed clusters, the CCM has to be among the first things deployed — usually via a static manifest on the control-plane nodes, or via a Daemon that tolerates the init taint and the control-plane taint. Get the bootstrap ordering wrong and every node sits with `NoSchedule` forever.

---

## 5. Cloud Controller Manager: What It Is

The Cloud Controller Manager is a single Go binary, built per-cloud, that runs a set of controllers backed by the `cloud-provider` interface. Conceptually:

```
cloud-controller-manager
├── shared infrastructure (from kubernetes/cloud-provider)
│   ├── leader election (Lease in kube-system)
│   ├── informer factory
│   ├── workqueue scaffolding
│   └── shared client-go client
│
├── cloudprovider.Interface implementation (per-cloud)
│   ├── Instances / InstancesV2     ← node metadata
│   ├── LoadBalancer                ← service controller backend
│   ├── Zones                       ← (deprecated; folded into InstancesV2)
│   ├── Routes                      ← route controller backend
│   └── Clusters                    ← rarely used
│
└── controllers (from kubernetes/cloud-provider, generic)
    ├── cloud-node-controller       (uses Instances/InstancesV2)
    ├── cloud-node-lifecycle-ctrl   (uses Instances/InstancesV2)
    ├── service-controller          (uses LoadBalancer)
    └── route-controller            (uses Routes)  [optional]
```

The split is deliberate. The *controllers* are generic — they live in [kubernetes/cloud-provider](https://github.com/kubernetes/cloud-provider) and know nothing about any specific cloud. They consume the Go interface. The *implementations* are per-cloud and live in their own repositories. AWS's CCM glues these together in [kubernetes/cloud-provider-aws](https://github.com/kubernetes/cloud-provider-aws/blob/master/cmd/aws-cloud-controller-manager/main.go), which imports the controllers from `k8s.io/cloud-provider` and the AWS-specific implementation from its own `pkg/`.

A simplified `main.go` looks like:

```go
package main

import (
    "k8s.io/cloud-provider/app"
    cloudcontrolleroptions "k8s.io/cloud-provider/options"
    "k8s.io/cloud-provider-aws/pkg/providers/v1"   // registers "aws" provider
)

func main() {
    opts, _ := cloudcontrolleroptions.NewCloudControllerManagerOptions()
    fss := opts.Flags( /* allControllers */ )
    command := app.NewCloudControllerManagerCommand(
        opts,
        app.DefaultInitFuncConstructors,
        fss,
        wait.NeverStop,
    )
    command.Execute()
}
```

The package import (`_ "k8s.io/cloud-provider-aws/pkg/providers/v1"`) calls `cloudprovider.RegisterCloudProvider("aws", factory)` via its `init()`, and the generic `cloudcontrollermanager` looks up the provider by name (`--cloud-provider=aws`). The CCM startup then:

1. Calls the factory to instantiate the cloud provider.
2. Calls `cloud.Initialize(clientBuilder, stopCh)` so the provider can spin up its own clients and metadata caches.
3. Starts the controllers, each of which is given the provider, the informer factory, and a leader-elected go-routine.

The CCM uses leader election aggressively. Even though it has a small number of controllers, running two CCMs simultaneously would cause duplicate cloud API calls (and, worse, races on `EnsureLoadBalancer` reconciliation). The lease lives at `kube-system/cloud-controller-manager` and rotates every ~15 seconds with a 30-second lease duration by default. Only the leader runs the controllers; non-leader replicas idle in leader-election waiting state.

---

## 6. The `cloud.Interface` Go Contract

The interface itself is small and worth reading. From [`pkg/cloud-provider/cloud.go`](https://github.com/kubernetes/cloud-provider/blob/master/cloud.go):

```go
type Interface interface {
    // Initialize provides the cloud with a kubernetes client builder and may spawn goroutines
    // to perform housekeeping or run custom controllers specific to the cloud provider.
    // Any tasks started here should be cleaned up when the stop channel closes.
    Initialize(clientBuilder ControllerClientBuilder, stop <-chan struct{})

    // LoadBalancer returns a balancer interface. Also returns true if the interface is supported, false otherwise.
    LoadBalancer() (LoadBalancer, bool)

    // Instances returns an instances interface. Also returns true if the interface is supported.
    // Deprecated in favour of InstancesV2.
    Instances() (Instances, bool)

    // InstancesV2 is an implementation for instances and should only be implemented by external cloud providers,
    // implementing InstancesV2 is recommended. If both Instances and InstancesV2 are implemented, InstancesV2 will be used.
    InstancesV2() (InstancesV2, bool)

    // Zones returns a zones interface. Also returns true if the interface is supported.
    // Deprecated in favour of InstancesV2.
    Zones() (Zones, bool)

    // Clusters returns a clusters interface. Also returns true if the interface is supported.
    Clusters() (Clusters, bool)

    // Routes returns a routes interface along with whether the interface is supported.
    Routes() (Routes, bool)

    // ProviderName returns the cloud provider ID.
    ProviderName() string

    // HasClusterID returns true if a ClusterID is required and set.
    HasClusterID() bool
}
```

The two-tier `Instances` vs `InstancesV2` split is the most consequential evolution. The legacy `Instances` interface had separate methods keyed on either `NodeName` or `ProviderID` — `NodeAddresses(nodeName)`, `NodeAddressesByProviderID(providerID)`, `InstanceID(nodeName)`, `InstanceType(nodeName)`, `InstanceTypeByProviderID(providerID)`, `InstanceExistsByProviderID(providerID)`, `InstanceShutdownByProviderID(providerID)`, and so on. Each call was a separate cloud API trip. With ten thousand nodes and a node-controller resync, this was thousands of API calls per cycle, hammering EC2's `DescribeInstances` rate limit (1000 RPS per region, but shared across the account).

`InstancesV2` collapses this into:

```go
type InstancesV2 interface {
    InstanceExists(ctx context.Context, node *v1.Node) (bool, error)
    InstanceShutdown(ctx context.Context, node *v1.Node) (bool, error)
    InstanceMetadata(ctx context.Context, node *v1.Node) (*InstanceMetadata, error)
}

type InstanceMetadata struct {
    ProviderID     string
    InstanceType   string
    NodeAddresses  []v1.NodeAddress
    Zone           string
    Region         string
    AdditionalLabels map[string]string  // added later for arbitrary cloud-side labels
}
```

One call, one round-trip, all the metadata. Providers internally batch via the cloud's `DescribeInstances(filters)` API. AWS's provider batches by InstanceID per page (200 at a time). GCP's provider uses `instances.list` with a filter. Azure pages through VMSS members.

The `LoadBalancer` interface is similar in spirit:

```go
type LoadBalancer interface {
    GetLoadBalancer(ctx context.Context, clusterName string, service *v1.Service) (status *v1.LoadBalancerStatus, exists bool, err error)
    GetLoadBalancerName(ctx context.Context, clusterName string, service *v1.Service) string
    EnsureLoadBalancer(ctx context.Context, clusterName string, service *v1.Service, nodes []*v1.Node) (*v1.LoadBalancerStatus, error)
    UpdateLoadBalancer(ctx context.Context, clusterName string, service *v1.Service, nodes []*v1.Node) error
    EnsureLoadBalancerDeleted(ctx context.Context, clusterName string, service *v1.Service) error
}
```

`EnsureLoadBalancer` is idempotent: given a Service and the list of healthy Nodes, ensure the cloud LB exists, has the right listeners, the right target group / backend pool, the right SecurityGroups / NSG / firewall rules, and the right health checks. Return the status (hostname or IP). The service controller calls this every time the Service changes, every time the Node set changes (membership delta), and on every full resync (every 5 minutes by default). The provider's `EnsureLoadBalancer` is therefore the most-called, most-load-bearing cloud API path in the CCM, and every cloud provider obsesses over making it as cheap and idempotent as possible.

---

## 7. The Node Controller (Cloud Half)

The node controller in CCM is responsible for the cloud-aware portions of a Node's lifecycle. There are actually two controllers, both running inside CCM:

- **`cloud-node-controller`**: handles the *addition* path. On Node create, fetch metadata, fill in addresses/labels/providerID, remove the init taint.
- **`cloud-node-lifecycle-controller`**: handles the *deletion* path. Periodically check whether each node's underlying cloud instance still exists; if not, taint with `node.cloudprovider.kubernetes.io/shutdown` (intermediate) and eventually delete the Node object so pods get rescheduled.

The state machine for a single Node, from the cloud-side perspective:

```
                kubelet starts
                     │
                     │  POST /api/v1/nodes (registration)
                     ▼
   ┌─────────────────────────────────────────────────────────┐
   │ Node object exists                                      │
   │   spec.providerID = ""    (kubelet didn't set it)       │
   │   status.addresses = [InternalIP, Hostname]             │
   │   spec.taints += node.cloudprovider.k8s.io/uninitialized│
   │   no region/zone labels                                 │
   └─────────────────────────────────────────────────────────┘
                     │  CCM cloud-node-controller observes
                     │  the new Node via informer
                     ▼
   ┌─────────────────────────────────────────────────────────┐
   │ Resolve identity:                                       │
   │  - if spec.providerID != "", use it                     │
   │  - else: ask the cloud for a providerID matching        │
   │          status.addresses[*].InternalIP / hostname      │
   │          (cloud-specific lookup, e.g.,                  │
   │           DescribeInstances filter by private-ip-address│
   │           on AWS)                                       │
   └─────────────────────────────────────────────────────────┘
                     │
                     ▼
   ┌─────────────────────────────────────────────────────────┐
   │ Call InstancesV2.InstanceMetadata(node)                 │
   │  returns InstanceMetadata{                              │
   │    ProviderID, InstanceType, Zone, Region,              │
   │    NodeAddresses, AdditionalLabels                      │
   │  }                                                      │
   └─────────────────────────────────────────────────────────┘
                     │
                     ▼
   ┌─────────────────────────────────────────────────────────┐
   │ PATCH Node:                                             │
   │   spec.providerID = <resolved>                          │
   │   metadata.labels:                                      │
   │     topology.kubernetes.io/region = <Region>            │
   │     topology.kubernetes.io/zone   = <Zone>              │
   │     node.kubernetes.io/instance-type = <InstanceType>   │
   │     + any cloud-specific labels                         │
   │   status.addresses = merged with metadata               │
   │   spec.taints -= node.cloudprovider.k8s.io/uninitialized│
   └─────────────────────────────────────────────────────────┘
                     │
                     │  Node is now schedulable.
                     │  scheduler sees it on next watch event.
                     ▼
   ┌─────────────────────────────────────────────────────────┐
   │ Node lifecycle controller polls periodically:           │
   │   InstanceExists(node) ? continue : shutdown path       │
   │   InstanceShutdown(node) ? taint shutdown : continue    │
   └─────────────────────────────────────────────────────────┘
                     │
       instance gone │ instance shutdown
                     ▼
   ┌─────────────────────────────────────────────────────────┐
   │ Tainted with node.cloudprovider.k8s.io/shutdown:NoExec  │
   │  → pods evicted                                         │
   │  → DELETE Node object (final)                           │
   └─────────────────────────────────────────────────────────┘
```

The lookup path on AWS — when kubelet hasn't pre-populated `spec.providerID` — calls `ec2:DescribeInstances` filtered by `private-ip-address` and the cluster tag, returning at most one instance. The cluster tag is the cluster identity marker; without it, two clusters in the same VPC could resolve each other's nodes by IP and corrupt each other. This is the *first* reason the cluster tag matters; the LoadBalancer reconciliation logic is the second (it also filters by tag).

The kubelet *can* pre-populate `spec.providerID` if started with `--provider-id=<value>`. AWS EKS does this from user-data. GKE does it from the instance metadata. Self-managed clusters frequently forget; the CCM does the lookup-by-IP fallback, but it's flaky in dual-stack networks and when nodes have many secondary IPs. Always set `--provider-id`. (Pitfall: on AWS, the EC2 `instance-id` is *not* the providerID by itself; the providerID is `aws:///<az>/<instance-id>`, e.g., `aws:///us-east-1a/i-0abcdef0123456789`.)

The shutdown path is most visible during spot instance reclamation, ASG scale-in, or graceful VM shutdown. When the cloud reports the instance as "terminated" or "shutting-down", the lifecycle controller adds `node.cloudprovider.kubernetes.io/shutdown:NoExecute`, which triggers immediate pod eviction. Critical: this is *not* a graceful drain. Pods get the standard `terminationGracePeriodSeconds` window, but workloads expecting clean drain (`PreStop` hooks running to completion, PDB respected) need a different mechanism — typically a node-termination handler (AWS's `aws-node-termination-handler`, GCP's preemption-notification webhook handler, or Karpenter's interruption controller).

---

## 8. `providerID` and Why It Matters

`Node.spec.providerID` is the durable identifier joining a Kubernetes Node to a cloud VM. It has a cloud-specific shape:

| Cloud      | `providerID` format                                                                        | Example                                                                              |
|------------|--------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------|
| AWS        | `aws:///<availability-zone>/<instance-id>`                                                  | `aws:///us-east-1a/i-0123456789abcdef0`                                              |
| AWS Fargate| `aws:///<availability-zone>/<fargate-pod-name>` (pseudo-instance)                          | `aws:///us-west-2b/fargate-ip-10-0-1-23.us-west-2.compute.internal`                  |
| GCP        | `gce://<project>/<zone>/<instance-name>`                                                    | `gce://my-project/us-central1-a/gke-cluster-default-pool-abc-xyz`                    |
| Azure VM   | `azure:///subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Compute/virtualMachines/<vm-name>` | `azure:///subscriptions/abc.../resourceGroups/my-rg/providers/Microsoft.Compute/virtualMachines/my-vm` |
| Azure VMSS | `azure:///subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Compute/virtualMachineScaleSets/<vmss>/virtualMachines/<idx>` | `azure:///subscriptions/abc.../resourceGroups/MC_aks/providers/Microsoft.Compute/virtualMachineScaleSets/aks-default-12345/virtualMachines/0` |
| OpenStack  | `openstack:///<instance-id>`                                                                | `openstack:///e4f1c0...`                                                              |
| vSphere    | `vsphere://<uuid>`                                                                          | `vsphere://4218d1...`                                                                 |
| Equinix    | `equinixmetal://<uuid>`                                                                     | `equinixmetal://xyz...`                                                               |

Why so much complexity? Because the CCM uses `providerID` for:

1. **Cloud lookup.** `InstanceExists(node)` parses `providerID` to extract the cloud ID, then calls the cloud-specific API. AWS goes to `DescribeInstances`. GCP goes to `instances.get`. Azure goes to ARM.
2. **LoadBalancer membership.** The service controller passes the list of Nodes to `EnsureLoadBalancer(nodes)`; the provider extracts `providerID` to determine which cloud VMs to add as targets/backends. On AWS, this becomes the EC2 instance ID for classic ELB target registration; on GCP, it becomes the GCE instance for the unmanaged instance group.
3. **Volume topology binding.** CSI drivers use the node's zone label (set from `providerID` lookup) to decide which volumes can attach to which nodes. EBS in `us-east-1a` cannot attach to a node in `us-east-1b`.
4. **CSI node ID translation.** The CSI driver's `NodeGetInfo` returns a node ID that the driver uses for attach calls. For drivers like `ebs.csi.aws.com`, the node ID is the EC2 instance ID parsed from `providerID`.

**Pitfall: a wrong `providerID` is silent.** If a self-managed cluster bootstraps nodes with the wrong providerID (e.g., `aws://i-0123...` instead of `aws:///us-east-1a/i-0123...`), the CCM may still resolve the instance (depending on the provider's parsing leniency), but downstream consumers will reject it. EBS attach fails with cryptic errors. LB target registration leaves zombie targets. The fix is *always* the canonical, three-slash, AZ-prefixed form. Never fewer slashes, never a missing AZ.

---

## 9. The `node.cloudprovider.kubernetes.io/uninitialized` Taint

When kubelet runs with `--cloud-provider=external`, it applies this taint at Node registration:

```yaml
spec:
  taints:
  - key: node.cloudprovider.kubernetes.io/uninitialized
    value: "true"
    effect: NoSchedule
```

The effect is `NoSchedule`: existing pods running on the node (none, because the node just registered) are unaffected, but new scheduling decisions skip this node. This taint blocks workload scheduling until the CCM has decorated the Node. The CCM removes the taint as the *last step* of its node-controller reconciliation, after `providerID`, addresses, and labels are set.

There are exactly three reasons this taint can stay forever:

1. **CCM not running.** This is the most common reason — clusters bootstrapped without CCM, or CCM crashing/CrashLooping, or CCM never elected leader (leader-election lease misconfigured).
2. **CCM running but lacks IAM/RBAC.** The CCM can't call the cloud (`UnauthorizedOperation` on AWS, `PERMISSION_DENIED` on GCP, `AuthorizationFailed` on Azure), so `InstanceMetadata` fails and the taint never gets removed. Check CCM logs for cloud-side errors.
3. **CCM running but providerID lookup fails.** The kubelet didn't set `--provider-id`, the cloud-side lookup by IP/hostname fails (e.g., the cluster tag is wrong, or the IP isn't in `DescribeInstances`), and the controller can't resolve which cloud instance this Node maps to. Fix: set `--provider-id` explicitly on kubelet from user-data.

System-critical DaemonSets need to tolerate this taint so they can start before CCM finishes. For example, the AWS VPC CNI's DaemonSet and the CCM itself have:

```yaml
spec:
  template:
    spec:
      tolerations:
      - key: node.cloudprovider.kubernetes.io/uninitialized
        value: "true"
        effect: NoSchedule
      - key: node-role.kubernetes.io/control-plane
        operator: Exists
        effect: NoSchedule
      - key: node-role.kubernetes.io/master
        operator: Exists
        effect: NoSchedule
```

If you write a node-level operator that needs to come up before the CCM, you need this toleration. Forgetting it is a classic chicken-and-egg: your CSI node driver can't start because the CCM hasn't initialized the node, but the CCM can't talk to the cloud because the CSI driver hasn't provided a token...

---

## 10. The Route Controller

The route controller is the simplest of the CCM controllers, and also the most often skipped. It exists for the case where the cluster's pod CIDR is *not* part of the cloud's VPC routing fabric. In that scenario, when a pod on Node A sends a packet to a pod on Node B, the underlay has no idea where to send it. The route controller fixes this by installing VPC routes: "pod CIDR `10.244.1.0/24` → instance i-A", "pod CIDR `10.244.2.0/24` → instance i-B", and so on.

This was load-bearing in early Kubernetes on GCE, when the default networking model was "host-gw"-style routing through the VPC. Each Node was allocated a `/24` from the cluster CIDR (`spec.podCIDR`), and the route controller called `compute.routes.insert` to add a route per Node into the VPC routing table.

In 2026, the route controller is mostly obsolete:

- **AWS:** the VPC CNI (`amazon-vpc-cni-k8s`) assigns pods *real VPC IPs* from secondary ENIs. No routing needed; the VPC already knows how to route every pod IP because it's a regular VPC address.
- **GCP:** GKE clusters with "VPC-native" mode (the default since 2018) use alias IP ranges. Each Node is assigned a secondary range from the VPC, and pods get IPs from that range. The VPC routes them natively.
- **Azure:** Azure CNI assigns VNet IPs to pods directly. Same story.
- **Cilium / Calico in eBPF / BGP mode:** the CNI installs routes itself (BGP-advertised, or eBPF-redirected). The CCM's route controller is bypassed.

The route controller is still relevant for:

- Legacy `kubenet` networking on Azure (deprecated).
- GCE "routes-based" clusters (legacy, pre-VPC-native).
- Self-managed clusters using Flannel `host-gw` on a cloud VPC.

When you do enable it, the controller watches Nodes, reads `spec.podCIDRs`, and calls `Routes().CreateRoute(ctx, clusterName, routeName, route)` per Node. On deletion, `DeleteRoute`. The cloud's route quota becomes a hard cluster size limit: AWS VPC route tables are capped at 50 routes by default (raisable to 1000); GCP at 200; Azure at 400 per route table. Hence the move to native VPC IPs — it lets you ignore route quotas entirely.

---

## 11. The Service Controller: LoadBalancer Provisioning

The service controller is the most user-visible CCM controller. It watches `Service` objects of type `LoadBalancer` and reconciles them to cloud load balancers.

```
                                  Service watch
                                       │
   apply Service:LoadBalancer ─────────┤
                                       ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │ Service controller workqueue                                        │
   │                                                                     │
   │ for each Service of type LoadBalancer:                              │
   │   collect ready Nodes                                               │
   │     (filtered by Service.spec.externalTrafficPolicy:                │
   │        Cluster  → all Ready nodes                                   │
   │        Local    → only nodes hosting matching pods)                 │
   │                                                                     │
   │   call cloud.LoadBalancer.EnsureLoadBalancer(                       │
   │      ctx, clusterName, service, nodes)                              │
   │                                                                     │
   │   provider does cloud-specific magic:                               │
   │     - lookup or create LB                                           │
   │     - lookup or create target group / backend pool                  │
   │     - register nodes as targets                                     │
   │     - configure listeners (port, protocol, TLS cert)                │
   │     - configure health checks                                       │
   │     - configure SG/NSG/firewall rules                               │
   │     - return LoadBalancerStatus{Ingress: [{IP/Hostname}]}           │
   │                                                                     │
   │   PATCH service.status.loadBalancer.ingress = [...]                 │
   └─────────────────────────────────────────────────────────────────────┘
```

A small but important detail: the reconciliation is *list-driven*, not delta-driven. Every reconcile passes the *current* list of Ready nodes. If a node becomes NotReady, the next reconcile drops it from the list and the provider deregisters it from the target group. There is no "node became NotReady" event the controller listens for — it's all level-triggered.

The full provisioning flow for a Service of type `LoadBalancer: nlb`:

```
   user: kubectl apply -f svc.yaml          (LoadBalancer type=nlb)
                       │
                       ▼
   apiserver:  store Service v1 in etcd
                       │
                       ▼  watch
   service-controller (in CCM, leader):
       compute nodes: [node-A, node-B, node-C]
       call provider.EnsureLoadBalancer(ctx, "prod-cluster", svc, nodes)
                       │
                       ▼   (AWS provider)
   AWS provider:
     1. ec2:DescribeInstances on providerIDs of [A,B,C]
     2. elbv2:DescribeLoadBalancers (name matching kubernetes.io/cluster/prod-cluster=owned)
     3. if not exists: elbv2:CreateLoadBalancer
     4. elbv2:DescribeTargetGroups (kubernetes.io/service-name=ns/svc)
     5. if not exists: elbv2:CreateTargetGroup
     6. elbv2:RegisterTargets (instance IDs of A,B,C, port nodePort)
     7. if not exists: elbv2:CreateListener (port, protocol, target group)
     8. elbv2:DescribeLoadBalancerAttributes / ModifyLoadBalancerAttributes (idle timeout, etc.)
     9. SG dance: ec2:DescribeSecurityGroups, AuthorizeSecurityGroupIngress
    10. return LoadBalancerStatus{Hostname: "<lb-name>.elb.us-east-1.amazonaws.com"}
                       │
                       ▼
   service-controller:
       PATCH /api/v1/namespaces/ns/services/svc/status
       service.status.loadBalancer.ingress = [{hostname: "...elb..."}]
                       │
                       ▼
   apiserver fan-out: kubectl get svc shows EXTERNAL-IP populated.
                      ExternalDNS controller sees the change, creates Route 53 A record.
                      cert-manager (if HTTP-01) starts ACME challenge through the LB.
```

The reconciler is called on every Service update *and* every Node membership change. For a cluster with 200 LoadBalancer Services and 1000 nodes that scale up/down constantly, this is a non-trivial workqueue. The default rate limit is `5 QPS, burst 10` for the cloud client; tune via `--kube-api-qps` and `--cloud-provider-gce-l4-ilb-loadbalancer-controller-qps` (or per-cloud equivalents). Postmortems on slow LB reconciliation almost always trace to this: either the cloud's rate limit, or the CCM's internal rate limit, or the `kube-controller-manager` workqueue rate limit (the service controller used to live there and still inherits some of those settings on older clouds).

---

## 12. Cloud LB Types: NLB vs ALB vs Internal vs Global

Each cloud has multiple LB types with different feature/latency/cost tradeoffs:

| Cloud | L4 (TCP/UDP) | L7 (HTTP/S) | Internal | Global |
|-------|--------------|-------------|----------|--------|
| AWS   | Network Load Balancer (NLB) | Application Load Balancer (ALB) | Internal NLB / Internal ALB | Global Accelerator (anycast in front of NLB) |
| GCP   | External Network LB (Passthrough) | External HTTP(S) LB (Premium tier = global, Standard = regional) | Internal TCP/UDP LB / Internal HTTP(S) LB | Cross-region External HTTP(S) LB (Premium) |
| Azure | Standard Load Balancer (L4) | Application Gateway (L7) / Front Door (global L7) | Internal Load Balancer | Front Door / Traffic Manager |

The native `Service: type=LoadBalancer` always provisions an **L4** LB (because `Service` is an L4 abstraction). To get L7, you use an Ingress controller or Gateway API implementation.

**Annotations** select the LB type within a cloud:

```yaml
# AWS NLB (the default for "LoadBalancer" type in newer EKS clusters)
apiVersion: v1
kind: Service
metadata:
  name: my-app
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: "nlb"
    service.beta.kubernetes.io/aws-load-balancer-scheme: "internet-facing"   # or "internal"
    service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: "ip"      # or "instance"
    service.beta.kubernetes.io/aws-load-balancer-cross-zone-load-balancing-enabled: "true"
    service.beta.kubernetes.io/aws-load-balancer-ssl-cert: "arn:aws:acm:us-east-1:123456789012:certificate/abc-def-..."
    service.beta.kubernetes.io/aws-load-balancer-backend-protocol: "tcp"
spec:
  type: LoadBalancer
  selector:
    app: my-app
  ports:
  - port: 443
    targetPort: 8443
    protocol: TCP
```

```yaml
# GCP Internal TCP/UDP LB
apiVersion: v1
kind: Service
metadata:
  name: my-app
  annotations:
    networking.gke.io/load-balancer-type: "Internal"
    networking.gke.io/internal-load-balancer-subnet: "my-subnet"
    cloud.google.com/l4-rbs: "enabled"  # use Regional Backend Service variant
spec:
  type: LoadBalancer
  loadBalancerClass: "networking.gke.io/internal"
  ...
```

```yaml
# Azure Internal Standard LB
apiVersion: v1
kind: Service
metadata:
  name: my-app
  annotations:
    service.beta.kubernetes.io/azure-load-balancer-internal: "true"
    service.beta.kubernetes.io/azure-load-balancer-internal-subnet: "internal-subnet"
    service.beta.kubernetes.io/azure-load-balancer-resource-group: "my-rg"
spec:
  type: LoadBalancer
  ...
```

The `service.spec.loadBalancerClass` field (GA in 1.24) lets you have multiple LB controllers in the same cluster — e.g., the CCM service controller for the default class, and the AWS Load Balancer Controller for `service.k8s.aws/nlb`. Without `loadBalancerClass`, every Service goes to the default provider, which makes mixed setups impossible.

---

## 13. Service Annotations: The De-Facto API

Annotations are how cloud LB configuration was historically expressed in Kubernetes. They are the *de facto* API, even though SIG-Network keeps wishing it weren't so. The Gateway API is the long-term replacement, but every cloud's service-controller integration in 2026 is still annotation-driven.

The reason annotations won and remained the API surface is structural. When the in-tree cloud providers shipped, there was no good way to extend `Service.spec` per cloud — `Service` is a core type, owned by SIG-Network, and adding `service.spec.aws.targetType` would have required a Kubernetes release and a public review. Annotations bypassed all of that: any controller could read its own annotation namespace, and clouds could ship new features without touching `kube/kube`. The downside was no schema, no validation, no documentation in the core. Each cloud's annotation set diverged in surprising ways: AWS uses `service.beta.kubernetes.io/aws-load-balancer-*`, GCP uses `cloud.google.com/*` and `networking.gke.io/*` mixed, Azure uses `service.beta.kubernetes.io/azure-*`. There is no portable spelling for "make this LB internal".

Gateway API was designed to fix exactly this. The `Gateway` and `GatewayClass` resources have proper schema, validation, and per-implementation parameters via `parametersRef`. Cloud providers ship `GatewayClass` controllers that consume Gateway API resources instead of annotation-decorated Services. The migration is happening — AWS, GCP, Azure all ship Gateway API implementations as of 2024 — but the long tail of operators, Helm charts, and tooling continues to use annotations. Expect both to coexist for a long time.

A non-exhaustive AWS annotation reference (consult [`pkg/controllers/service/load_balancer.go` in cloud-provider-aws](https://github.com/kubernetes/cloud-provider-aws) for the live list):

| Annotation | Purpose |
|---|---|
| `service.beta.kubernetes.io/aws-load-balancer-type` | `nlb` (NLB), `external` (delegate to AWS LB Controller) |
| `service.beta.kubernetes.io/aws-load-balancer-scheme` | `internet-facing` or `internal` |
| `service.beta.kubernetes.io/aws-load-balancer-internal` | (legacy) `true` for internal |
| `service.beta.kubernetes.io/aws-load-balancer-subnets` | comma-separated subnet IDs / names |
| `service.beta.kubernetes.io/aws-load-balancer-security-groups` | SG IDs to attach |
| `service.beta.kubernetes.io/aws-load-balancer-additional-resource-tags` | extra resource tags |
| `service.beta.kubernetes.io/aws-load-balancer-cross-zone-load-balancing-enabled` | `true`/`false` |
| `service.beta.kubernetes.io/aws-load-balancer-target-group-attributes` | k=v list, e.g., `deregistration_delay.timeout_seconds=30` |
| `service.beta.kubernetes.io/aws-load-balancer-healthcheck-protocol` | `tcp`/`http`/`https` |
| `service.beta.kubernetes.io/aws-load-balancer-healthcheck-path` | URL path for HTTP health |
| `service.beta.kubernetes.io/aws-load-balancer-healthcheck-interval` | seconds |
| `service.beta.kubernetes.io/aws-load-balancer-ssl-cert` | ACM cert ARN |
| `service.beta.kubernetes.io/aws-load-balancer-ssl-ports` | comma-separated TCP ports that terminate TLS |
| `service.beta.kubernetes.io/aws-load-balancer-proxy-protocol` | `*` enables proxy protocol v2 |
| `service.beta.kubernetes.io/aws-load-balancer-access-log-enabled` | `true`/`false` |
| `service.beta.kubernetes.io/aws-load-balancer-access-log-s3-bucket-name` | bucket for logs |
| `service.beta.kubernetes.io/aws-load-balancer-eip-allocations` | EIP allocation IDs (NLB only) |
| `service.beta.kubernetes.io/aws-load-balancer-private-ipv4-addresses` | private IPs to pin (NLB only) |
| `service.beta.kubernetes.io/aws-load-balancer-ipv6-addresses` | IPv6 addresses (dual-stack) |
| `service.beta.kubernetes.io/aws-load-balancer-attributes` | LB-level attributes (idle timeout, etc.) |

GCP and Azure have similar tables. The takeaways:

1. The set of supported annotations is *the API surface* of each cloud provider. Treat it like a versioned API even though it's strings.
2. The annotations are *level-triggered*: change the annotation, the service controller re-reconciles and updates the cloud LB. Some changes are non-disruptive (target group attributes), some require LB recreation (subnet changes), some are silently rejected (NLB type changes).
3. **Different controllers consume different annotations.** On AWS specifically, the CCM consumes `service.beta.kubernetes.io/aws-*`, while the AWS Load Balancer Controller consumes `service.beta.kubernetes.io/aws-load-balancer-*` *and* its own `alb.ingress.kubernetes.io/*`. Some annotation names overlap; pay attention.

---

## 14. The Legacy Volume Controller and Why It Is Gone

The in-tree volume controller used to live in `kube-controller-manager`, with the cloud-aware portions implemented per-provider:

- **AWS EBS** (`kubernetes.io/aws-ebs`): `attach-detach-controller` calls EC2 `AttachVolume` and `DetachVolume`; the persistent volume controller calls `CreateVolume` and `DeleteVolume`.
- **GCE PD** (`kubernetes.io/gce-pd`): GCE Compute `attachDisk` / `detachDisk`, `disks.insert` / `disks.delete`.
- **Azure Disk** (`kubernetes.io/azure-disk`): Azure compute `AttachVirtualMachineDataDisk`, etc.
- Plus Cinder, vSphere VMDK, Photon, Quobyte, ScaleIO, StorageOS, Flocker, …

These were all replaced by CSI drivers under the **CSIMigration** umbrella (KEP-625). The migration shape was:

1. **Phase 1: Out-of-tree CSI driver becomes available.** AWS, GCP, Azure ship CSI drivers in parallel to in-tree code. Users can opt in via StorageClass.
2. **Phase 2: CSI Migration translation shim.** With `--feature-gates=CSIMigration=true,CSIMigrationAWS=true`, calls to the in-tree provisioner/attacher get *translated* to CSI calls under the hood. Users see no API change; PVs with `pv.spec.awsElasticBlockStore` continue to work, but the actual work is done by the CSI driver.
3. **Phase 3: Deprecation.** In-tree volume plugins are deprecated and marked for removal.
4. **Phase 4: Removal.** In Kubernetes 1.26, the AWS EBS, GCE PD, Azure Disk, and Azure File in-tree plugins were removed. From that version on, you *must* have the CSI driver installed; bare in-tree volume specs no longer work.

The user-visible takeaways:

- StorageClass `provisioner: kubernetes.io/aws-ebs` is dead. Use `ebs.csi.aws.com`.
- PV `spec.awsElasticBlockStore` in new manifests is dead. Use `spec.csi.driver: ebs.csi.aws.com`.
- The CCM no longer has a `volume-controller` running.
- Cloud-specific in-tree migration paths are *complete* — no remaining in-tree volume plugins as of 1.31. The relevant chapter is [19 — Storage](./19-storage-csi-pv-pvc.md), not this one.

A small forensic note for upgrades: clusters that ran a long time with in-tree plugins often have PVs in etcd whose `spec` still references the old fields (`spec.awsElasticBlockStore.volumeID`, `spec.gcePersistentDisk.pdName`, `spec.azureDisk.diskName`). After CSI migration, the apiserver translates these on the fly into CSI calls; the etcd-stored PV does not change shape. This is fine — the migration shim handles it — but it means an audit of `kubectl get pv -o yaml` against modern manifests will show "legacy"-shaped PVs. They still work as long as the CSI driver is installed; do not rewrite them gratuitously, as the rewrite forces a re-bind and brief unavailability.

A separate gotcha: the *resize* path. With in-tree plugins, volume expansion went through the apiserver → controller-manager → cloud API. With CSI, expansion goes apiserver → external-resizer sidecar → CSI controller. The external-resizer is deployed as part of the CSI driver Deployment. If the CSI driver is installed without the resizer (or the resizer's RBAC is incomplete), PVC resize requests silently get stuck in the `Resizing` state. Always verify the external-resizer is running and has `patch persistentvolumeclaims/status` permissions.

---

## 15. Running CCM: Deployment, RBAC, Leader Election

A canonical self-managed CCM Deployment looks like this (this is the AWS form; GCP and Azure are structurally identical):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aws-cloud-controller-manager
  namespace: kube-system
  labels:
    k8s-app: aws-cloud-controller-manager
spec:
  replicas: 2     # leader-elected; non-leaders idle
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels:
      k8s-app: aws-cloud-controller-manager
  template:
    metadata:
      labels:
        k8s-app: aws-cloud-controller-manager
    spec:
      nodeSelector:
        node-role.kubernetes.io/control-plane: ""
      tolerations:
      - key: node-role.kubernetes.io/control-plane
        operator: Exists
        effect: NoSchedule
      - key: node.cloudprovider.kubernetes.io/uninitialized
        value: "true"
        effect: NoSchedule
      serviceAccountName: cloud-controller-manager
      priorityClassName: system-cluster-critical
      hostNetwork: true
      containers:
      - name: aws-cloud-controller-manager
        image: registry.k8s.io/provider-aws/cloud-controller-manager:v1.31.0
        args:
        - --v=2
        - --cloud-provider=aws
        - --cluster-name=prod-cluster
        - --cluster-cidr=10.244.0.0/16
        - --allocate-node-cidrs=false              # CNI handles this on AWS
        - --configure-cloud-routes=false           # VPC CNI: no routes needed
        - --use-service-account-credentials=true
        - --leader-elect=true
        - --leader-elect-lease-duration=137s
        - --leader-elect-renew-deadline=107s
        - --leader-elect-retry-period=26s
        - --bind-address=127.0.0.1
        - --secure-port=10258
        resources:
          requests:
            cpu: 200m
            memory: 256Mi
        livenessProbe:
          httpGet:
            scheme: HTTPS
            host: 127.0.0.1
            port: 10258
            path: /healthz
          initialDelaySeconds: 15
          timeoutSeconds: 15
```

Important details:

- **`replicas: 2`** with leader election. One leader does all work; the other(s) wait. Replication is for failover, not scale. You can run replicas=1 in single-AZ clusters, but for HA, always 2 or 3.
- **`hostNetwork: true`** is common because the CCM needs to reach the cloud's IMDS endpoint (`169.254.169.254`) without going through the pod network, especially on bootstrap when CNI may not be up yet.
- **`nodeSelector` + `tolerations`** keep CCM on control-plane nodes. On AWS, this is critical because the IAM role attached to the control-plane EC2 instance is what authorizes the CCM's cloud calls (when not using IRSA).
- **`--use-service-account-credentials=true`** makes each per-controller goroutine use a distinct ServiceAccount token — better audit trail than a single CCM-wide identity.
- **Leader election lease settings:** `137s/107s/26s` are the documented stable defaults. Don't tune below these unless you really know what you're doing; a flaky lease causes leader churn and duplicated cloud API calls.
- **`livenessProbe` on HTTPS port 10258** is the standard `/healthz`. If the CCM hangs (e.g., cloud API timeout in the watch loop), kubelet restarts it.

RBAC scaffolding (a subset of what's needed; see [cloud-provider rbac.yaml](https://github.com/kubernetes/cloud-provider/blob/master/manifests/rbac.yaml) for the canonical list):

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: system:cloud-controller-manager
rules:
- apiGroups: [""]
  resources: ["events"]
  verbs: ["create", "patch", "update"]
- apiGroups: [""]
  resources: ["nodes"]
  verbs: ["get", "list", "watch", "patch", "update", "delete"]
- apiGroups: [""]
  resources: ["nodes/status"]
  verbs: ["patch", "update"]
- apiGroups: [""]
  resources: ["services"]
  verbs: ["get", "list", "watch", "patch", "update"]
- apiGroups: [""]
  resources: ["services/status"]
  verbs: ["patch", "update"]
- apiGroups: [""]
  resources: ["serviceaccounts"]
  verbs: ["create", "get"]
- apiGroups: [""]
  resources: ["serviceaccounts/token"]
  verbs: ["create"]
- apiGroups: ["coordination.k8s.io"]
  resources: ["leases"]
  verbs: ["get", "create", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: system:cloud-controller-manager
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:cloud-controller-manager
subjects:
- kind: ServiceAccount
  name: cloud-controller-manager
  namespace: kube-system
```

Cloud-side IAM is separate. On AWS, the CCM's instance profile (or IRSA role) needs at minimum:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeRegions",
        "ec2:DescribeRouteTables",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSubnets",
        "ec2:DescribeVolumes",
        "ec2:DescribeAvailabilityZones",
        "ec2:CreateSecurityGroup",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupIngress",
        "ec2:CreateTags",
        "elasticloadbalancing:*",
        "kms:DescribeKey"
      ],
      "Resource": "*"
    }
  ]
}
```

The "minimum" is more permissive than you'd like for the legacy in-tree CCM behavior (it manipulates SGs in-place). Newer setups using the AWS Load Balancer Controller scope these much tighter.

---

## 16. AWS: Cloud Controller Manager

The AWS CCM is in [kubernetes/cloud-provider-aws](https://github.com/kubernetes/cloud-provider-aws). It implements the standard CCM contract for AWS, with EC2 + ELB classic + ELBv2 (NLB) as the cloud backends. Notably, **it does not implement the modern ALB or fancy NLB features**; those are delegated to the separate AWS Load Balancer Controller (next section). The CCM handles:

- Node initialization (EC2 `DescribeInstances` → addresses, type, AZ, providerID).
- Node lifecycle (poll instance state → taint on shutdown → delete on terminate).
- Service controller for Service type `LoadBalancer` (provisions Classic ELB or, with annotation, NLB).
- Configurable cluster-tag-aware behaviour. Every cloud resource it creates gets tags `kubernetes.io/cluster/<name>: owned` and `kubernetes.io/service-name: <ns>/<svc>`. Without these tags, garbage collection doesn't work — orphaned ELBs accumulate forever.

The `Cluster Identity` problem is acute on AWS. If you run two clusters in the same VPC, the CCMs *must* be scoped by `--cluster-name` (and the IAM policy must allow only the matching cluster tag) or they will trip over each other. Specifically:

- Cluster A's CCM sees a Service in its own cluster, calls `EnsureLoadBalancer`, which calls `DescribeLoadBalancers` filtered by `kubernetes.io/cluster/A=owned`. Good.
- But the SG manipulation routine, when adding inbound rules to node SGs, scans *all* SGs in the VPC that match the cluster filter. If the filter is wrong, it might modify Cluster B's SGs.

The fix is rigorous tagging discipline plus IAM conditions like:

```json
{
  "Effect": "Allow",
  "Action": "ec2:AuthorizeSecurityGroupIngress",
  "Resource": "*",
  "Condition": {
    "StringEquals": {
      "ec2:ResourceTag/kubernetes.io/cluster/prod-A": "owned"
    }
  }
}
```

The AWS CCM is "legacy" in the sense that EKS itself doesn't use it for ALB/NLB anymore — EKS-managed clusters provision LoadBalancer-type Services through the AWS Load Balancer Controller when installed, and fall back to the CCM otherwise. But the CCM is still required (and EKS installs it) for the node-controller and node-lifecycle paths.

---

## 17. AWS Load Balancer Controller

The [AWS Load Balancer Controller](https://github.com/kubernetes-sigs/aws-load-balancer-controller) (henceforth "AWS LBC") is the modern, feature-complete LB controller for EKS. It is *not* the CCM; it is a separate controller deployed by users (or by an EKS add-on). It owns two kinds of resources:

- **Ingress** of class `alb` → provisions an Application Load Balancer.
- **Service** with annotation `service.beta.kubernetes.io/aws-load-balancer-type: external` → provisions a Network Load Balancer.

The CCM never sees these objects (because the LBC takes them over via `loadBalancerClass` or annotation-based opt-in). The split is clean: the CCM does the node-side work; the LBC does the LB-side work.

Why use the LBC instead of CCM's NLB support?

1. **IP target mode.** The LBC can register *pod IPs directly* as targets in the target group, instead of registering EC2 instance IDs as targets (which then NodePort-DNAT through kube-proxy). This eliminates a hop, preserves client source IP, and avoids the kube-proxy iptables churn. Requires the VPC CNI (so pod IPs are routable from the LB).
2. **ALB support.** The CCM does not provision ALBs; only the LBC does.
3. **Modern features.** WAF integration, Cognito auth, Shield, ALB target group sticky sessions per target group, etc.
4. **Better tagging and scoping.** The LBC uses CRD-driven config (`IngressClassParams`, `TargetGroupBinding`) rather than free-form annotations.

A canonical ALB Ingress:

```yaml
apiVersion: networking.k8s.io/v1
kind: IngressClass
metadata:
  name: alb
  annotations:
    ingressclass.kubernetes.io/is-default-class: "true"
spec:
  controller: ingress.k8s.aws/alb
  parameters:
    apiGroup: elbv2.k8s.aws
    kind: IngressClassParams
    name: alb-default-params
---
apiVersion: elbv2.k8s.aws/v1beta1
kind: IngressClassParams
metadata:
  name: alb-default-params
spec:
  scheme: internet-facing
  ipAddressType: dualstack
  loadBalancerAttributes:
  - key: idle_timeout.timeout_seconds
    value: "60"
  - key: routing.http2.enabled
    value: "true"
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
  annotations:
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/healthcheck-path: /healthz
    alb.ingress.kubernetes.io/healthcheck-protocol: HTTP
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTP":80},{"HTTPS":443}]'
    alb.ingress.kubernetes.io/ssl-redirect: "443"
    alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:us-east-1:123456789012:certificate/abc-def-123
    alb.ingress.kubernetes.io/group.name: shared-prod
    alb.ingress.kubernetes.io/group.order: "100"
spec:
  ingressClassName: alb
  rules:
  - host: web.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: web
            port:
              number: 80
```

Notice `alb.ingress.kubernetes.io/group.name: shared-prod`. This is the LBC's "IngressGroup" feature — multiple Ingress resources can share a single ALB, with rules merged in `group.order` order. Without this, every Ingress = a new ALB, which is expensive at scale ($0.025/hr per ALB, plus LCU-hours).

For NLBs through the LBC:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-tcp-app
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: "external"
    service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: "ip"
    service.beta.kubernetes.io/aws-load-balancer-scheme: "internet-facing"
spec:
  type: LoadBalancer
  loadBalancerClass: service.k8s.aws/nlb
  selector:
    app: my-tcp-app
  ports:
  - port: 5432
    targetPort: 5432
    protocol: TCP
```

`loadBalancerClass: service.k8s.aws/nlb` opts this Service out of the default CCM service controller; only the LBC handles it. This is the clean way to mix.

---

## 18. AWS VPC CNI and Pod IP = VPC IP

The AWS VPC CNI ([amazon-vpc-cni-k8s](https://github.com/aws/amazon-vpc-cni-k8s)) assigns each pod a *real VPC IP* from secondary ENIs (Elastic Network Interfaces) attached to the EC2 instance. This is unique among major cloud CNIs — most overlays (Calico, Flannel, Cilium without VPC-native mode) assign pods IPs from a separate CIDR routed via encapsulation or BGP.

The implication is enormous:

1. **No encapsulation.** Pod-to-pod traffic is normal VPC traffic. No VXLAN, no IP-in-IP. Lowest possible latency and full bandwidth.
2. **VPC IPs are routable from anywhere in the VPC.** ALB IP target mode works. Security groups can be applied to pods directly (via SecurityGroupPolicy CRDs). VPC flow logs see pod IPs.
3. **VPC IP exhaustion.** Pod count is bounded by ENI count × IPs per ENI, which depends on instance type. An `m5.large` supports 3 ENIs × 10 IPs = 30 IPs total (minus 1 reserved), so ~29 pods. An `m5.24xlarge` supports 15 × 50 = 750. **Pod-per-node limits are instance-type-bound.** See [AWS ENI limits doc](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-eni.html#AvailableIpPerENI).
4. **Subnet sizing.** Pods consume VPC IPs from the subnets, so subnets fill up. A /24 subnet has 251 usable IPs. Mixing nodes (each taking 1) and pods (each taking 1) in the same subnet means a /24 fits maybe 200 pods. Plan subnet sizing carefully.

Prefix delegation (CNI feature flag) helps: the CNI requests /28 prefixes instead of individual IPs, allowing up to 110 pods per ENI on m5.large+ instances. Tune via `ENABLE_PREFIX_DELEGATION=true`.

---

## 19. EBS CSI and EFS CSI

The [AWS EBS CSI Driver](https://github.com/kubernetes-sigs/aws-ebs-csi-driver) is the modern, in-cluster, CSI-spec implementation that replaced the in-tree `kubernetes.io/aws-ebs` plugin (removed in 1.26). It's installed as a Deployment (controller plugin) + DaemonSet (node plugin). The controller plugin needs IAM rights to call `ec2:CreateVolume`, `AttachVolume`, etc.; the node plugin needs nothing cloud-side but needs `NodeStageVolume` / `NodePublishVolume` permissions on the host.

Critical settings:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer    # critical for multi-AZ
allowVolumeExpansion: true
reclaimPolicy: Delete
parameters:
  type: gp3
  iops: "3000"
  throughput: "125"
  encrypted: "true"
  kmsKeyId: "arn:aws:kms:us-east-1:123456789012:key/abc-def-..."
```

`volumeBindingMode: WaitForFirstConsumer` defers provisioning until a pod is scheduled, ensuring the EBS volume lands in the same AZ as the pod's node. Without this, you provision in AZ A, then the scheduler can only place the pod in AZ A, then a node failure leaves the pod permanently stuck waiting for an AZ-A node.

The [AWS EFS CSI Driver](https://github.com/kubernetes-sigs/aws-efs-csi-driver) handles EFS (NFS-backed shared filesystem). EFS is cross-AZ, so the `WaitForFirstConsumer` discipline matters less, but EFS performance modes (`generalPurpose` vs `maxIO`) and throughput modes (`bursting`, `provisioned`, `elastic`) are configured per-filesystem, not per-PVC. Plan ahead.

---

## 20. IRSA: IAM Roles for Service Accounts

**IRSA** (IAM Roles for Service Accounts) is AWS's workload identity primitive. It lets a Kubernetes pod assume an AWS IAM role via the standard AWS SDK, with no static AWS credentials anywhere in the cluster. It was introduced in 2019 and remained the canonical EKS pattern until EKS Pod Identity arrived in 2023.

The trust chain:

```
   ┌────────────────────────────────────────────────────────────────────┐
   │  EKS cluster has an OIDC issuer:                                  │
   │    e.g., https://oidc.eks.us-east-1.amazonaws.com/id/ABCD1234...  │
   │  Serves /.well-known/openid-configuration and /keys.json (JWKS).  │
   └─────────────────────────────────┬──────────────────────────────────┘
                                     │
                          configured once at cluster creation
                                     ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  AWS account has an IAM OIDC Identity Provider:                   │
   │    arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.    │
   │       amazonaws.com/id/ABCD1234...                                │
   │  Trust IAM roles to identities signed by this issuer.             │
   └─────────────────────────────────┬──────────────────────────────────┘
                                     │
                                     ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  IAM Role 'app-role' has trust policy:                            │
   │    Principal: oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/  │
   │               ABCD1234                                             │
   │    Action: sts:AssumeRoleWithWebIdentity                          │
   │    Condition: StringEquals on:                                    │
   │      oidc...:aud = sts.amazonaws.com                              │
   │      oidc...:sub = system:serviceaccount:prod:app-sa              │
   └─────────────────────────────────┬──────────────────────────────────┘
                                     │
              ServiceAccount annotation `eks.amazonaws.com/role-arn`
                                     ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  ServiceAccount 'app-sa' in namespace 'prod':                     │
   │    annotations:                                                    │
   │      eks.amazonaws.com/role-arn:                                  │
   │        arn:aws:iam::123456789012:role/app-role                    │
   └─────────────────────────────────┬──────────────────────────────────┘
                                     │
              Pod admission: eks-pod-identity-webhook (mutating webhook)
                                     ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  Pod spec gets mutated:                                            │
   │    - projected ServiceAccount token volume mounted at              │
   │        /var/run/secrets/eks.amazonaws.com/serviceaccount/token     │
   │      Token audience = "sts.amazonaws.com", expirationSeconds=3600  │
   │    - env vars added to every container:                            │
   │        AWS_ROLE_ARN=arn:aws:iam::...:role/app-role                 │
   │        AWS_WEB_IDENTITY_TOKEN_FILE=/var/run/.../token              │
   │        AWS_DEFAULT_REGION=us-east-1                                │
   │        AWS_STS_REGIONAL_ENDPOINTS=regional                         │
   └─────────────────────────────────┬──────────────────────────────────┘
                                     │
                                     ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  Application starts. AWS SDK auto-detects the env vars.            │
   │  SDK credential provider chain:                                    │
   │   1. env vars (AWS_ACCESS_KEY_ID/...)  ← not set, skip             │
   │   2. shared credentials file           ← not present, skip         │
   │   3. WebIdentityTokenCredentials       ← matches!                  │
   └─────────────────────────────────┬──────────────────────────────────┘
                                     │
                                     ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  SDK calls sts:AssumeRoleWithWebIdentity:                          │
   │    RoleArn = AWS_ROLE_ARN                                          │
   │    WebIdentityToken = contents of AWS_WEB_IDENTITY_TOKEN_FILE      │
   │    RoleSessionName = pod name (or webhook-injected default)        │
   │  STS validates JWT signature against EKS OIDC JWKS                 │
   │  STS validates aud, sub claims against role trust policy           │
   │  STS returns temp credentials (1h default, configurable up to 12h) │
   └─────────────────────────────────┬──────────────────────────────────┘
                                     │
                                     ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  SDK caches temp creds in memory.                                  │
   │  SDK uses them for AWS API calls.                                  │
   │  SDK auto-refreshes ~5min before expiry by re-reading the          │
   │     projected token file (kubelet auto-rotates the SA token).      │
   └────────────────────────────────────────────────────────────────────┘
```

A canonical IRSA setup:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: app-sa
  namespace: prod
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/app-role
    # Optional:
    # eks.amazonaws.com/audience: sts.amazonaws.com
    # eks.amazonaws.com/sts-regional-endpoints: "true"
    # eks.amazonaws.com/token-expiration: "3600"
```

The IAM role's trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABCD1234"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "oidc.eks.us-east-1.amazonaws.com/id/ABCD1234:aud": "sts.amazonaws.com",
          "oidc.eks.us-east-1.amazonaws.com/id/ABCD1234:sub": "system:serviceaccount:prod:app-sa"
        }
      }
    }
  ]
}
```

A few subtle points:

- The pod-identity webhook ([aws/amazon-eks-pod-identity-webhook](https://github.com/aws/amazon-eks-pod-identity-webhook)) is what injects the projected token volume and env vars. If the webhook isn't installed (or its failurePolicy is wrong), pods get the SA annotation but no projection, and the SDK falls through to the next provider in the chain (usually the IMDS-based instance role, which has different perms). Result: silent escalation to the wrong identity.
- The token's `aud` claim must match `sts.amazonaws.com` (default) or whatever the SA annotation overrides. Many IRSA failures trace to a wrong audience.
- The token's `sub` claim is `system:serviceaccount:<namespace>:<sa-name>`. The trust policy's `StringEquals` condition on `sub` is what binds an IAM role to a specific SA, *and only that SA*. Using `StringLike` with wildcards is the classic mistake that lets any pod in the cluster assume the role.

---

## 21. EKS Pod Identity

**EKS Pod Identity** (launched late 2023) is the successor to IRSA. It removes the OIDC dance entirely. The trust model is:

- An **agent** (`eks-pod-identity-agent`) runs as a DaemonSet on every node.
- It listens on a link-local IP (`169.254.170.23`) and implements the AWS container credentials provider protocol.
- An **EKS Pod Identity Association** binds an IAM role to a ServiceAccount inside the cluster. This is an EKS API call, not a Kubernetes object.
- The agent intercepts pod credential requests (via the AWS SDK env vars it sets) and exchanges them for STS creds using a *cluster-scoped* IAM role's pass-through.

Setup:

```bash
# Create the association (one-time, via aws CLI or IaC)
aws eks create-pod-identity-association \
  --cluster-name prod-cluster \
  --namespace prod \
  --service-account app-sa \
  --role-arn arn:aws:iam::123456789012:role/app-role
```

The IAM role's trust policy is simpler:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "pods.eks.amazonaws.com" },
    "Action": ["sts:AssumeRole", "sts:TagSession"]
  }]
}
```

No OIDC provider in the AWS account. No per-cluster OIDC issuer configuration. No `sub` / `aud` claim conditions. The agent does the binding inside the cluster, via the EKS API.

When should you use which?

- **EKS Pod Identity** when you're starting fresh, are on EKS, and want simpler setup. Works for ~98% of cases.
- **IRSA** when you need: cross-account access (Pod Identity can do this too but with extra steps), workloads on non-EKS clusters (self-managed AWS clusters), or you have an existing IRSA setup you don't want to migrate.

Both can coexist in the same cluster. The agent only intercepts pods whose SA has an association; everything else uses IRSA injection (if installed).

A subtle architectural point: with Pod Identity, the *cluster* (specifically the EKS control plane) holds the trust relationship; with IRSA, the *role* itself trusts the OIDC provider. The implication: revoking access in Pod Identity is "delete the association" (an EKS API call), and the next token-mint attempt by the agent fails. Revoking access in IRSA requires either modifying the role's trust policy (slow, can affect other pods using the role) or deleting the SA annotation. Pod Identity's revocation is therefore much cleaner from a security-operations standpoint — important for incident response. Furthermore, IRSA tokens are valid for their projected lifetime (default 1 hour; configurable to 12), so even if you remove the SA annotation, existing pods continue to assume the role until their next token rotation. Pod Identity's tokens are shorter-lived (15 minutes) and re-fetched on every credential refresh through the agent — revoke-by-association takes effect within minutes.

The data-plane cost of the two models also differs. IRSA: each pod's SDK does its own STS `AssumeRoleWithWebIdentity` call on startup (and on each rotation). With 5000 pods, you have a brief burst of 5000 STS calls; STS handles this fine, but it's visible in CloudTrail. Pod Identity: the agent on each node makes one call to `pods.eks.amazonaws.com` per credential fetch and serves results to local pods. Fewer STS calls overall, and centralized through the EKS service principal — easier to audit. Neither is "expensive" in absolute terms, but at very high pod churn (CI clusters spinning up 10k pods/day), Pod Identity's batching is gentler on STS rate limits.

---

## 22. Karpenter and the Node Lifecycle

[Karpenter](https://karpenter.sh) is a node autoscaler that talks directly to the cloud (currently AWS, with Azure and other providers in development) to launch and terminate VMs based on pending pod resource requirements. It does *not* go through the Cluster Autoscaler's ASG abstraction.

Key behaviours relevant to this chapter:

1. **Node provisioning bypasses the ASG.** Karpenter calls `ec2:RunInstances` directly, with a user-data script that bootstraps the node via the EKS bootstrap script or your custom AMI.
2. **`providerID` set at bootstrap.** Karpenter's user-data sets kubelet's `--provider-id` to the canonical form before the kubelet registers. The node never goes through the "no providerID, CCM lookup by IP" path.
3. **Consolidation.** Karpenter periodically evaluates whether running pods could fit on fewer or cheaper nodes, drains the candidate node (respecting PDBs), terminates it, and replaces with the cheaper option. This is the big economic feature.
4. **Spot integration.** Karpenter mixes spot and on-demand based on `NodePool` weights. When spot is reclaimed, Karpenter's interruption controller (watching SQS for the spot reclamation notice) cordon-and-drains pre-emptively.

A `NodePool` + `EC2NodeClass`:

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: default
spec:
  template:
    metadata:
      labels:
        billing-team: platform
    spec:
      requirements:
      - key: kubernetes.io/arch
        operator: In
        values: ["amd64", "arm64"]
      - key: karpenter.k8s.aws/instance-category
        operator: In
        values: ["c", "m", "r"]
      - key: karpenter.k8s.aws/instance-cpu
        operator: In
        values: ["4", "8", "16", "32"]
      - key: karpenter.sh/capacity-type
        operator: In
        values: ["spot", "on-demand"]
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      expireAfter: 720h    # rotate nodes after 30d for AMI freshness
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 30s
  limits:
    cpu: 1000
    memory: 4000Gi
---
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: default
spec:
  amiFamily: AL2023
  role: KarpenterNodeRole-prod-cluster
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: prod-cluster
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: prod-cluster
  amiSelectorTerms:
    - alias: al2023@latest
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 100Gi
        volumeType: gp3
        encrypted: true
        deleteOnTermination: true
  detailedMonitoring: false
  userData: |
    #!/bin/bash
    # Karpenter sets the bootstrap stanza automatically.
```

The Karpenter controller itself needs IRSA/Pod-Identity with permissions to call `ec2:RunInstances`, `ec2:TerminateInstances`, `ec2:CreateLaunchTemplate`, `ec2:CreateTags`, plus `iam:PassRole` on the node role and `sqs:ReceiveMessage` on the interruption queue.

A subtle but high-impact interaction: **the CCM's node-lifecycle-controller and Karpenter's termination flow can race**. When Karpenter terminates an instance, both Karpenter (which drains then deletes the Node) and the CCM (which polls and sees `InstanceExists=false`) want to delete the Node. The race is mostly benign — both end up deleting the same Node, and the second deletion is a no-op — but it can produce confusing log lines like "Node was deleted while we were trying to delete it" in CCM logs. This is normal.

A second interaction with Cluster Autoscaler vs Karpenter worth understanding: **you should run one or the other, not both.** Cluster Autoscaler (CA) works at the ASG level — it bumps the desired count of an ASG up or down, and the ASG launches/terminates instances. Karpenter works at the instance level, bypassing ASGs entirely. If both are enabled, CA will be confused by Karpenter's "missing" ASG-owned nodes, and Karpenter will be confused by CA's "phantom" pending pods. Pick one. The migration path from CA to Karpenter is usually: deploy Karpenter alongside, set CA's max size to current size (so CA stops adding nodes), let Karpenter take over new provisioning, then drain and decommission the CA-managed ASGs.

A third subtlety: **Karpenter and Pod Disruption Budgets**. Karpenter respects PDBs during consolidation, but the PDB must accurately reflect the cost of disruption. A PDB with `minAvailable: 1` on a Deployment with 2 replicas means Karpenter can drain at most one pod at a time. On a 50-node cluster with 200 pods, consolidation can be slow if PDBs are restrictive. Solutions: scale replicas higher so PDB slack increases; use `maxUnavailable` in absolute terms (e.g., `maxUnavailable: 10%`); or annotate workloads with `karpenter.sh/do-not-disrupt: "true"` if they truly cannot tolerate any disruption (e.g., singleton stateful jobs).

---

## 23. GCP: Cloud Controller Manager

The GCP CCM lives in [kubernetes/cloud-provider-gcp](https://github.com/kubernetes/cloud-provider-gcp). For GKE clusters, Google operates it on your behalf — you never see the binary. For self-managed clusters on GCE, you deploy it yourself.

What it owns:

- Node initialization via GCE Compute `instances.get` (addresses, machine type, zone).
- Service-controller for `Service: type=LoadBalancer` → provisions Network LBs (passthrough L4) by default, with annotations for the new "Regional Backend Service" variants and Internal LBs.
- Route controller for legacy routes-based clusters (rare in 2026).

GCP's LoadBalancer story is more fragmented than AWS's:

- **External Passthrough Network LB** (the default for Service: type=LoadBalancer): regional, L4, preserves client IP. Uses a "target pool" or, with `cloud.google.com/l4-rbs: "enabled"`, a regional backend service. Free until you hit the per-rule limit.
- **External HTTP(S) LB**: global (Premium tier) or regional (Standard). L7. Provisioned via Ingress with class `gce`, not via Service.
- **Internal TCP/UDP LB**: regional, L4, private VIP. Annotation `networking.gke.io/load-balancer-type: "Internal"`.
- **Internal HTTP(S) LB**: regional, L7. Provisioned via Ingress class `gce-internal`.

GKE clusters get the GCP CCM bundled, plus the GKE-specific "Cloud Provider Compatibility" mode that disables the route controller (because VPC-native clusters don't need it) and tunes lease durations for GKE's expected churn rate.

---

## 24. GCE PD CSI and Filestore CSI

The [GCE Persistent Disk CSI Driver](https://github.com/kubernetes-sigs/gcp-compute-persistent-disk-csi-driver) replaced the in-tree `kubernetes.io/gce-pd` plugin. It's installed by default on GKE; on self-managed clusters, you install it as a Deployment + DaemonSet.

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: pd-ssd
provisioner: pd.csi.storage.gke.io
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
reclaimPolicy: Delete
parameters:
  type: pd-ssd
  replication-type: none           # or regional-pd for zonal HA
  disk-encryption-kms-key: projects/my-project/locations/us-central1/keyRings/my-kr/cryptoKeys/my-key
```

A GCP-specific niceness: **Regional Persistent Disks**. With `replication-type: regional-pd`, a PD is replicated synchronously across two zones in the same region. PVC binding uses topology constraints to ensure the pod can be scheduled to *either* zone; on node failure, the pod can be rescheduled to the other zone and remount the same PD. This is unique to GCP — AWS EBS is strictly zonal, and Azure Disks are zone-redundant only for managed disks with ZRS (a different model).

[Filestore CSI](https://github.com/kubernetes-sigs/filestore-csi-driver) handles NFS-backed shared filesystems (analogous to AWS EFS). Performance tiers: `BASIC_HDD`, `BASIC_SSD`, `HIGH_SCALE_SSD`, `ENTERPRISE`. Tier choice is per-instance, not per-PVC; plan capacity ahead of time.

---

## 25. GKE Workload Identity

GKE Workload Identity is GCP's pod-to-GSA binding. It's structurally simpler than IRSA — there's no OIDC dance visible to the user, because GCP handles it inside the project boundary.

```
                Cluster has Workload Identity enabled:
                  --workload-pool=PROJECT_ID.svc.id.goog
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  Project has GSA: app-gsa@PROJECT.iam.gserviceaccount.com       │
   │                                                                 │
   │  IAM policy on the GSA includes:                                │
   │    member:  serviceAccount:PROJECT.svc.id.goog[NS/KSA]          │
   │    role:    roles/iam.workloadIdentityUser                      │
   └─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  KSA in namespace NS, name KSA:                                 │
   │    metadata:                                                    │
   │      annotations:                                                │
   │        iam.gke.io/gcp-service-account:                          │
   │          app-gsa@PROJECT.iam.gserviceaccount.com                │
   └─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  Pod uses the KSA.                                              │
   │  The GKE metadata server (gke-metadata-server, a DaemonSet)     │
   │  intercepts requests to 169.254.169.254 (the GCE metadata IP)   │
   │  and:                                                            │
   │   1. Identifies the calling pod via its source IP.              │
   │   2. Reads the pod's KSA from the apiserver.                    │
   │   3. Sees the iam.gke.io/gcp-service-account annotation.        │
   │   4. Calls iamcredentials.googleapis.com to mint a short-lived  │
   │      access token for the GSA.                                  │
   │   5. Returns it to the pod in the GCE metadata format.          │
   └─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  Google Cloud SDK in the pod thinks it's on a GCE VM with a     │
   │  service account, reads the token from metadata, and uses it.   │
   └─────────────────────────────────────────────────────────────────┘
```

The KSA setup:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: app-ksa
  namespace: prod
  annotations:
    iam.gke.io/gcp-service-account: app-gsa@my-project.iam.gserviceaccount.com
```

The GSA-side IAM binding (via `gcloud`):

```bash
gcloud iam service-accounts add-iam-policy-binding \
  app-gsa@my-project.iam.gserviceaccount.com \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:my-project.svc.id.goog[prod/app-ksa]"
```

Three things that bite people:

1. **`iam.gke.io/gcp-service-account` annotation must exactly match the GSA email.** Typo = silent failure. The metadata server returns "no GSA bound" and the SDK falls through to whatever credential is available (often the node's GCE SA, which has different perms).
2. **The metadata server's source-IP lookup depends on Dataplane v2's IP-tracking.** If you've replaced the GKE CNI with something else, the metadata server can't identify the pod.
3. **Workload Identity blocks node-level metadata access by default.** Pods cannot reach `169.254.169.254` for the node's GSA token; they only see what the metadata server emulates for their own KSA. This is *good* (security boundary) but breaks code that expected the node's SA.

---

## 26. GKE Ingress and the Managed HTTP(S) LB

GKE Ingress (controller name: `gce`, GA since GKE has existed) provisions a Google Cloud HTTP(S) Load Balancer in response to an Ingress object. The LB is a multi-tier construct:

```
                ┌────────────────────────────────┐
                │ Global anycast IP (Premium tier)│
                └─────────────┬──────────────────┘
                              │
                ┌─────────────▼──────────────────┐
                │   Forwarding rule + Target HTTPS proxy │
                │   (TLS termination via Google-managed cert) │
                └─────────────┬──────────────────┘
                              │
                ┌─────────────▼──────────────────┐
                │   URL map  (host/path routing)         │
                └─────────────┬──────────────────┘
                              │
                ┌─────────────▼──────────────────┐
                │   Backend Services (per route)         │
                │   Health checks, sessionAffinity, CDN  │
                └─────────────┬──────────────────┘
                              │
                ┌─────────────▼──────────────────┐
                │   Network Endpoint Groups (NEGs)       │
                │     - GCE_VM_IP_PORT (instance NEGs)   │
                │     - GCE_VM_IP / GCE_VM_IP_PORT zonal │
                │     - Internet NEG, Serverless NEG…    │
                └────────────────────────────────┘
```

GKE supports **Container-Native Load Balancing** via *NEGs* (Network Endpoint Groups). When a Service has annotation `cloud.google.com/neg: '{"ingress": true}'`, the GKE ingress controller creates one zonal NEG per zone where the Service has endpoints. The NEG members are pod IPs (analogous to ALB IP target mode). The LB sends traffic directly to pods, bypassing kube-proxy and the node.

Without NEG, the LB forwards to instance NEGs (node IPs), which then DNAT through kube-proxy. NEG mode is dramatically better: lower latency, preserved client IP, no SNAT, and the LB's health checks check the pod directly. **Always enable NEG.**

A GKE Ingress with managed cert:

```yaml
apiVersion: networking.gke.io/v1
kind: ManagedCertificate
metadata:
  name: web-cert
spec:
  domains:
  - web.example.com
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
  annotations:
    kubernetes.io/ingress.class: "gce"
    networking.gke.io/managed-certificates: "web-cert"
    kubernetes.io/ingress.global-static-ip-name: "web-ip"
spec:
  rules:
  - host: web.example.com
    http:
      paths:
      - path: /*
        pathType: ImplementationSpecific
        backend:
          service:
            name: web
            port:
              number: 80
---
apiVersion: v1
kind: Service
metadata:
  name: web
  annotations:
    cloud.google.com/neg: '{"ingress": true}'
spec:
  type: ClusterIP
  selector:
    app: web
  ports:
  - port: 80
    targetPort: 8080
```

`ManagedCertificate` is GKE-specific. Google provisions and renews a public TLS cert via Let's Encrypt for the listed domains. DNS must already point at the LB IP (the cert challenge is HTTP-01 over the LB itself).

---

## 27. GKE Dataplane v2

GKE Dataplane v2 (GA since 2021) replaces the legacy kubenet/calico dataplane with Cilium. It runs Cilium in "ENI-native"-style mode (using GCP's alias IP ranges for pod IPs), with eBPF replacing kube-proxy entirely.

Implications relevant to this chapter:

- **No kube-proxy.** Service routing is via eBPF in Cilium's `lb` map. iptables rules for Services are gone. Significant CPU savings at high Service counts.
- **NetworkPolicy.** Enforced by Cilium's eBPF program. The L7 features (HTTP rules) are not available in Dataplane v2; for those, you'd need a service mesh.
- **Observability.** Hubble is included; you get flow visibility out of the box.
- **No metadata-server change.** Workload Identity continues to work; the metadata server is still a separate DaemonSet.

You enable Dataplane v2 at cluster creation (`--enable-dataplane-v2`). You cannot enable it on an existing cluster without recreating it. This is the standard for new GKE clusters since ~2023.

---

## 28. GKE Autopilot

GKE Autopilot is GKE's "serverless" mode. Google chooses the nodes; you only declare workloads. Charged per pod-second based on requested CPU/memory.

For cloud-provider integration, Autopilot:

- Hides the Node API from users (you can't `kubectl describe node` on Autopilot).
- Enforces Pod Security and a curated set of allowed images/configurations.
- Disables `hostNetwork`, `hostPID`, privileged containers (mostly).
- Routes most cloud integration through Google's managed components — you don't deploy CCM, CSI drivers, etc.

The trade-off is rigidity: many operators and DaemonSets don't work on Autopilot because they need node-level access. For "boring" workloads (web apps, APIs, batch jobs), Autopilot is genuinely simpler. For platform teams who need to ship DaemonSets and operators, stick with Standard.

---

## 29. Azure: Cloud Provider Azure

The Azure CCM lives in [kubernetes-sigs/cloud-provider-azure](https://github.com/kubernetes-sigs/cloud-provider-azure). AKS uses it; self-managed clusters on Azure deploy it themselves.

Azure adds complexity that AWS/GCP don't:

- **Resource Groups.** Every Azure resource lives in a Resource Group. The CCM has to know which RG to put LBs, public IPs, etc. into — usually a separate "MC_xxx" managed RG for AKS clusters.
- **VMSS vs Availability Sets.** Nodes can be in a Virtual Machine Scale Set (VMSS, the modern default) or an Availability Set (legacy). The CCM's instance lookup differs between the two.
- **Two LB SKUs.** Basic (deprecated, being retired) and Standard. Standard is mandatory for new clusters and supports zone-redundancy.
- **Network Security Groups.** Each subnet/NIC can have an NSG. The CCM modifies the NSG attached to the agent node subnet when provisioning Services.

A typical AKS-side cloud-config (mostly Azure-managed; relevant for self-managed):

```json
{
  "cloud": "AzurePublicCloud",
  "tenantId": "abc...",
  "subscriptionId": "def...",
  "aadClientId": "ghi...",
  "aadClientSecret": "<from KeyVault>",
  "resourceGroup": "MC_my-cluster_my-cluster_eastus",
  "location": "eastus",
  "vmType": "vmss",
  "loadBalancerSku": "Standard",
  "loadBalancerName": "kubernetes",
  "primaryAvailabilitySetName": "",
  "primaryScaleSetName": "aks-nodepool-12345",
  "vnetName": "aks-vnet-abc",
  "vnetResourceGroup": "MC_my-cluster_my-cluster_eastus",
  "subnetName": "aks-subnet",
  "securityGroupName": "aks-agentpool-nsg-12345",
  "routeTableName": "aks-agentpool-rt-12345",
  "useInstanceMetadata": true,
  "useManagedIdentityExtension": true,
  "userAssignedIdentityID": "/subscriptions/.../userAssignedIdentities/aks-identity"
}
```

The cloud-config is mounted as a secret into the CCM pod and pointed to via `--cloud-config=/etc/kubernetes/azure.json`. On AKS, Microsoft manages this file.

---

## 30. Azure Disk CSI and Azure File CSI

The Azure Disk CSI Driver (`disk.csi.azure.com`) replaced the in-tree `kubernetes.io/azure-disk` (removed in 1.26). The Azure File CSI Driver (`file.csi.azure.com`) replaced `kubernetes.io/azure-file` (also removed).

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: managed-premium
provisioner: disk.csi.azure.com
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
reclaimPolicy: Delete
parameters:
  skuName: Premium_LRS              # or Premium_ZRS for zone-redundant
  cachingMode: ReadOnly
  diskEncryptionSetID: /subscriptions/.../diskEncryptionSets/my-des
```

Azure has unique disk attributes:

- **`Premium_ZRS`**: zone-redundant managed disks. The disk itself is replicated across AZs; a pod can move between AZs without losing the disk. AWS doesn't offer this on EBS.
- **`UltraSSD_LRS`**: high-performance disks with configurable IOPS/throughput per disk. Requires the VM to be in an "Ultra disk enabled" mode.
- **Shared disks.** Multiple VMs can attach the same disk (for clustered apps that need block-level sharing). Limited use cases.

Azure Files supports both SMB and NFS protocols; SMB is the default. For cross-AZ pod portability, prefer Azure Files (which is regional) over Azure Disk (which is zonal).

---

## 31. Application Gateway Ingress Controller

The [Application Gateway Ingress Controller (AGIC)](https://github.com/Azure/application-gateway-kubernetes-ingress) lets Azure Application Gateway (a managed L7 LB with WAF) be driven by Kubernetes Ingress objects. AKS optionally installs it as an add-on.

The architecture: AGIC watches Ingress resources, computes a desired Application Gateway configuration (listeners, backend pools, routing rules), and applies it via ARM. The Application Gateway sits outside the cluster, in the same VNet, and forwards to pod IPs (via the VNet integration) or node IPs (via NodePort).

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
  annotations:
    kubernetes.io/ingress.class: azure/application-gateway
    appgw.ingress.kubernetes.io/ssl-redirect: "true"
    appgw.ingress.kubernetes.io/backend-protocol: "https"
    appgw.ingress.kubernetes.io/health-probe-path: /healthz
    appgw.ingress.kubernetes.io/appgw-ssl-certificate: my-keyvault-cert
spec:
  rules:
  - host: web.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: web
            port:
              number: 443
```

AGIC alternatives: the Azure Gateway API implementation (newer, Gateway API CRDs), or running NGINX/HAProxy/Traefik in-cluster behind an Azure LB Service.

---

## 32. Azure AD Workload Identity

Azure AD Workload Identity replaces the deprecated AAD Pod Identity (which used a NodePool-level identity-binding controller; deprecated in 2022, retired 2024).

The new model uses **federated identity credentials** on Azure AD Applications:

```
                AKS cluster has OIDC issuer:
                  https://eastus.oic.prod-aks.azure.com/TENANT_ID/CLUSTER_ID/
                             │
                             ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  Azure AD App Registration 'app-aad':                           │
   │   clientId = abc-def-...                                        │
   │   Federated Identity Credentials:                               │
   │     issuer = https://eastus.oic.prod-aks.azure.com/.../         │
   │     subject = system:serviceaccount:prod:app-ksa                │
   │     audience = api://AzureADTokenExchange                       │
   └─────────────────────────────────────────────────────────────────┘
                             │
                             ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  KSA in namespace prod:                                         │
   │    name: app-ksa                                                │
   │    annotations:                                                  │
   │      azure.workload.identity/client-id: abc-def-...             │
   │      azure.workload.identity/tenant-id: tenant-...              │
   │  AND pod has label:                                              │
   │    azure.workload.identity/use: "true"                          │
   └─────────────────────────────────────────────────────────────────┘
                             │
                             ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  Mutating webhook (azure-wi-webhook) injects:                   │
   │   - projected SA token volume (audience = api://AzureADTokenExchange) │
   │   - env vars:                                                    │
   │       AZURE_CLIENT_ID, AZURE_TENANT_ID,                         │
   │       AZURE_FEDERATED_TOKEN_FILE, AZURE_AUTHORITY_HOST           │
   └─────────────────────────────────────────────────────────────────┘
                             │
                             ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  Pod's Azure SDK reads env vars, exchanges federated token      │
   │  at AAD's /oauth2/v2.0/token endpoint for an access token.      │
   │  Uses access token for Azure API calls.                         │
   └─────────────────────────────────────────────────────────────────┘
```

The setup is structurally identical to IRSA but with Azure AD instead of AWS IAM. The biggest gotcha: the pod must have the `azure.workload.identity/use: "true"` *label*, not just the SA annotation. Without the label, the webhook skips the injection.

---

## 33. AKS Virtual Nodes

AKS Virtual Nodes connects AKS to Azure Container Instances (ACI). When you scale beyond your node-pool capacity, Virtual Nodes "burst" pods into ACI without provisioning a real VM.

The mechanism: a virtual-kubelet (the [Virtual Kubelet](https://github.com/virtual-kubelet/virtual-kubelet) project) registers a fake "virtual-node-aci" Node in the cluster. Pods scheduled to that node are actually launched in ACI by the virtual-kubelet's ACI provider.

Use cases: bursty workloads, batch jobs, sudden traffic spikes. Drawbacks: ACI doesn't support all pod features (no DaemonSets, limited networking, slower startup than a hot Node).

The same Virtual Kubelet pattern is used by AWS Fargate on EKS (the `fargate-scheduler` controller plus the per-pod Fargate launch via the cluster's `aws-fargate-profile` resources) and by GKE for "GKE Autopilot" workloads that exceed node-pool capacity. The common abstraction is valuable: any cloud burst-compute can integrate with Kubernetes by exposing itself as a virtual node and accepting pod specs through the kubelet API. Operators consuming the cluster see only "another Node"; operators of the cluster see a fan-out of `kubectl get nodes` that includes synthetic entries.

The pitfall: monitoring and observability often break on virtual nodes. cAdvisor doesn't run there (no node OS to introspect). Logs go to the cloud provider's log service, not your in-cluster collector. Network policy may or may not be enforced depending on the integration. Treat virtual nodes as a separate failure domain and plan observability accordingly.

---

## 34. External DNS

[ExternalDNS](https://github.com/kubernetes-sigs/external-dns) is a controller that synchronizes DNS records in cloud DNS providers (Route 53, Cloud DNS, Azure DNS, plus many others) from Kubernetes Service and Ingress resources.

Its operating model:

```
                Service / Ingress with annotation
                external-dns.alpha.kubernetes.io/hostname: web.example.com
                             │
                             ▼
              ExternalDNS watches and discovers desired records:
                web.example.com  A  <Service LB IP / Ingress LB IP>
                             │
                             ▼
              Compares with current zone state (from cloud DNS API).
                             │
                             ▼
              Creates/updates/deletes records.
              Manages TXT-based ownership markers
                ("heritage=external-dns,external-dns/owner=cluster-prod")
              so multiple clusters don't fight over the same zone.
```

A canonical ExternalDNS deployment:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: external-dns
  namespace: external-dns
spec:
  replicas: 1
  selector:
    matchLabels:
      app: external-dns
  template:
    metadata:
      labels:
        app: external-dns
    spec:
      serviceAccountName: external-dns
      containers:
      - name: external-dns
        image: registry.k8s.io/external-dns/external-dns:v0.14.0
        args:
        - --source=service
        - --source=ingress
        - --domain-filter=example.com
        - --provider=aws                # or google, azure, cloudflare, ...
        - --policy=upsert-only          # or sync (also deletes records)
        - --aws-zone-type=public
        - --registry=txt
        - --txt-owner-id=prod-cluster
        - --interval=1m
```

And a Service that requests a record:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: web
  annotations:
    external-dns.alpha.kubernetes.io/hostname: web.example.com
    external-dns.alpha.kubernetes.io/ttl: "60"
spec:
  type: LoadBalancer
  selector:
    app: web
  ports:
  - port: 443
    targetPort: 8443
```

Pitfalls:

- **`--policy=upsert-only` is the safe default.** With `sync`, ExternalDNS deletes records it doesn't recognize, which can include manual entries. New users almost always start with `sync` and lose records.
- **`--txt-owner-id` must be unique per cluster.** Otherwise two clusters fight over the same zone.
- **Zone-level IAM.** The IAM role must allow `Route53:ChangeResourceRecordSets` on the specific zone, not `*`. Tight scoping prevents one cluster from poisoning another zone in the same account.
- **TTL.** Default is 300s; for LBs that change rarely, raise it. For Services that come and go via CI, lower it.

---

## 35. Cloud Certificates: ACM vs cert-manager

Two patterns for TLS certificates:

**Pattern A: Cloud-managed cert referenced by LB.**
- AWS: provision via ACM, reference ARN in `service.beta.kubernetes.io/aws-load-balancer-ssl-cert` (NLB via CCM) or `alb.ingress.kubernetes.io/certificate-arn` (ALB via LBC).
- GCP: provision via ManagedCertificate CRD or upload to Cloud Certificate Manager, reference in Ingress annotation.
- Azure: store in Key Vault, reference in App Gateway listener (AGIC: `appgw.ingress.kubernetes.io/appgw-ssl-certificate`).
- Cert never enters the cluster. Renewal is the cloud's problem.

**Pattern B: cert-manager + ACME (Let's Encrypt).**
- Install [cert-manager](https://cert-manager.io). Define `ClusterIssuer` for Let's Encrypt.
- cert-manager solves ACME challenges (HTTP-01 via an Ingress, DNS-01 via a cloud DNS provider).
- Issues `Certificate` objects → backed by `Secret` in the cluster.
- Ingress controller picks up the Secret reference and serves the cert.

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ops@example.com
    privateKeySecretRef:
      name: letsencrypt-prod-key
    solvers:
    - selector:
        dnsZones:
        - example.com
      dns01:
        route53:
          region: us-east-1
          # IRSA on the cert-manager SA for Route 53 access
```

For DNS-01 challenges from a private cluster (or any setup where the LB isn't reachable from the internet), `dns01` is required. HTTP-01 only works if Let's Encrypt's servers can hit the cluster's ingress.

Choose ACM-style when:
- You're tightly coupled to one cloud.
- You want zero-touch renewal.
- The LB type supports the cert binding (e.g., ALB or App Gateway).

Choose cert-manager when:
- You want a single mechanism across clouds.
- You need certs for mTLS (sidecar-to-sidecar) or other non-LB use cases.
- You want SPIFFE/SPIRE-style identity certs alongside ACME.

---

## 36. External Secrets Operator

[External Secrets Operator (ESO)](https://github.com/external-secrets/external-secrets) syncs secrets from external systems (AWS Secrets Manager, Parameter Store, GCP Secret Manager, Azure Key Vault, HashiCorp Vault, Bitwarden, …) into Kubernetes `Secret` objects.

The CRDs:

- **`SecretStore`** / **`ClusterSecretStore`**: defines a connection to a backend (with auth — usually via workload identity).
- **`ExternalSecret`**: defines a single secret to fetch and where to materialize it.

```yaml
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: aws-secrets-manager
  namespace: prod
spec:
  provider:
    aws:
      service: SecretsManager
      region: us-east-1
      auth:
        jwt:
          serviceAccountRef:
            name: eso-sa            # IRSA-annotated SA
---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: app-db-creds
  namespace: prod
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: SecretStore
    name: aws-secrets-manager
  target:
    name: app-db-creds              # the Secret to create
    creationPolicy: Owner
    deletionPolicy: Delete
    template:
      data:
        DATABASE_URL: "postgresql://{{ .username }}:{{ .password }}@{{ .host }}/{{ .dbname }}"
  data:
  - secretKey: username
    remoteRef:
      key: prod/app/db
      property: username
  - secretKey: password
    remoteRef:
      key: prod/app/db
      property: password
  - secretKey: host
    remoteRef:
      key: prod/app/db
      property: host
  - secretKey: dbname
    remoteRef:
      key: prod/app/db
      property: dbname
```

The sync flow:

```
   ┌─────────────────────────────────────────────────────────────────┐
   │ ESO controller (1 replica, optionally HA)                       │
   │                                                                 │
   │ Watches ExternalSecret CRs.                                     │
   │ For each, runs reconciliation every `refreshInterval`:          │
   │  1. Resolve SecretStore → connect to backend (using IRSA cred)  │
   │  2. Fetch the remote secret (e.g., AWS SecretsManager:GetSecret)│
   │  3. Apply template to assemble Secret data                      │
   │  4. Compute checksum; compare with existing K8s Secret          │
   │  5. If changed: PATCH the Secret object (annotations track hash)│
   │  6. Update ExternalSecret.status                                │
   └─────────────────────────────────────────────────────────────────┘
                  │
                  ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │ Kubernetes Secret app-db-creds exists in etcd, mounted into     │
   │ pods normally (as env var or file).                             │
   └─────────────────────────────────────────────────────────────────┘
```

Tradeoffs vs Secrets Store CSI:
- ESO writes secrets to etcd. Etcd is encrypted at rest (KMS plugin), but the secret material does pass through kube-apiserver. Auditable, cacheable, and works with everything that consumes `Secret` natively.
- ESO is RPS-bounded by the backend's GetSecret rate limit (Secrets Manager: 5000 RPS soft, but per-secret throttles apply). At scale, batch via `refreshInterval` carefully.
- Rotation latency = `refreshInterval`. If you rotate a backend secret and need pods to see it immediately, you also need to restart the pods (they cache the env value or file at startup).

---

## 37. Secrets Store CSI Driver

The [Secrets Store CSI Driver](https://github.com/kubernetes-sigs/secrets-store-csi-driver) is the alternative: instead of materializing secrets into K8s `Secret` objects, it mounts them as files into pods via a CSI volume.

```yaml
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: app-secrets
  namespace: prod
spec:
  provider: aws
  parameters:
    objects: |
      - objectName: "prod/app/db"
        objectType: "secretsmanager"
        jmesPath:
          - path: "username"
            objectAlias: "db-username"
          - path: "password"
            objectAlias: "db-password"
  secretObjects:                       # optional: also create a K8s Secret
  - secretName: app-db-creds
    type: Opaque
    data:
    - objectName: db-username
      key: username
    - objectName: db-password
      key: password
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
spec:
  template:
    spec:
      serviceAccountName: app-sa   # IRSA-annotated
      containers:
      - name: app
        image: my-app:1.0
        volumeMounts:
        - name: secrets
          mountPath: /mnt/secrets
          readOnly: true
      volumes:
      - name: secrets
        csi:
          driver: secrets-store.csi.k8s.io
          readOnly: true
          volumeAttributes:
            secretProviderClass: app-secrets
```

The driver runs as a DaemonSet, with per-provider plugins (AWS, GCP, Azure, Vault) loaded as separate DaemonSets. When a pod with a SecretProviderClass-referencing volume starts, the node driver fetches the secrets and writes them to the pod's tmpfs mount.

Advantages over ESO:
- **No etcd footprint** (unless you opt into `secretObjects` for compatibility).
- **Per-volume isolation.** Each pod gets its own copy. Compromise of one pod doesn't leak to others.
- **Auto-rotation.** With `enableSecretRotation: true` in the driver, mounted files are refreshed in-place (and pods can `inotify` for changes if they want).
- **Audit trail at the volume level.**

Disadvantages:
- Secrets aren't `Secret` objects natively, so anything that expects `valueFrom.secretKeyRef` (most Helm charts) needs the `secretObjects` compatibility layer, which puts you back in etcd.
- Per-pod fetch can hammer the backend at scale (no caching across pods on the same node by default).

Pick ESO when: you want centralized secret materialization, low complexity, and your apps expect `Secret` objects.
Pick Secrets Store CSI when: you want minimum etcd exposure, per-pod isolation, and live rotation matters.

---

## 38. KMS Provider Plugin and At-Rest Encryption

By default, Kubernetes stores Secret objects in etcd as base64-encoded plaintext. The `--encryption-provider-config` apiserver flag enables transparent at-rest encryption.

The history:
- **No encryption** (early K8s). Secrets in etcd are plaintext. Anyone with etcd access has every secret.
- **`aescbc` / `aesgcm` provider.** Apiserver-local symmetric key in a config file. Better than nothing, but the key is on disk on the apiserver hosts; the encryption boundary is etcd-only.
- **KMS provider v1** (deprecated). Each encryption operation calls the cloud KMS. Throttles badly at scale.
- **KMS provider v2** (GA in 1.29). Per-DEK encryption, KMS provides the KEK (key encryption key). Each Secret has its own DEK, encrypted by the KEK. Vastly reduces KMS API calls.

Layers:

```
                  cleartext Secret
                       │
                       ▼  (apiserver, in-process)
              Encrypt(DEK, ChaCha20-Poly1305)
                       │
                       ▼
              Encrypted blob + EncryptedDEK
                       │
                       ▼  (apiserver → KMS, via kms-plugin)
              Encrypt(KEK, KMS-side)
                       │
                       ▼
              {EncryptedSecret, KMS-wrapped-DEK}  → etcd
```

The KMS plugin is a small process running alongside the apiserver, communicating over a UNIX socket. The cloud-specific plugins:

- **AWS:** [aws-encryption-provider](https://github.com/kubernetes-sigs/aws-encryption-provider) — uses AWS KMS for KEK.
- **GCP:** [k8s-cloudkms-plugin](https://github.com/GoogleCloudPlatform/k8s-cloud-provider) — uses Cloud KMS.
- **Azure:** [kubernetes-kms](https://github.com/Azure/kubernetes-kms) — uses Key Vault.

```yaml
# EncryptionConfiguration (referenced by --encryption-provider-config)
apiVersion: apiserver.config.k8s.io/v1
kind: EncryptionConfiguration
resources:
- resources:
  - secrets
  providers:
  - kms:
      apiVersion: v2
      name: aws-encryption-provider
      endpoint: unix:///var/run/kmsplugin/socket.sock
      timeout: 3s
  - identity: {}      # fallback for reads of legacy plaintext
```

The fallback `identity` is *critical* during migration: when you first turn on encryption, existing Secrets are still plaintext. The identity provider reads them. Then a `kubectl get secrets --all-namespaces -o json | kubectl replace -f -` rotates every Secret through the encryption path. After that, you can remove the identity provider (but most clusters leave it for read-safety).

Things that go wrong:
- **KMS plugin dies → apiserver can't decrypt secrets → many things fail.** The plugin needs to be as available as the apiserver. Run it as a static pod alongside the apiserver, with restartPolicy=Always.
- **KMS rate limits.** With KMS v1, every Secret read/write was a KMS call. At scale (5000 secrets, controllers polling) you'd hit AWS KMS's 30,000 RPS quota. KMS v2 fixes this by caching the KEK and using local DEK encryption.
- **Cross-region KMS.** The KMS key is regional. A cluster in `us-east-1` encrypting against a KMS key in `us-west-2` works but adds latency and a cross-region dependency. Pin KMS keys to the cluster's region.

---

## 39. Cross-AZ Networking Costs and Topology-Aware Routing

Every major cloud charges for traffic crossing Availability Zones, even within the same VPC/VNet:

| Cloud | Per-AZ egress cost (intra-region) |
|-------|------------------------------------|
| AWS   | $0.01/GB out + $0.01/GB in = $0.02/GB total |
| GCP   | $0.01/GB (intra-region, inter-AZ) |
| Azure | $0.01/GB (intra-region, between AZs) |

For a chatty microservice cluster, this dominates. A 50 Gbps stream of cross-AZ traffic 24/7 costs ~$130K/month on AWS. The fixes:

**`internalTrafficPolicy: Local`** on a Service routes traffic only to endpoints on the *same node*. Cuts cross-AZ to zero — but only useful if every node hosts at least one replica, which usually means a DaemonSet-style service.

**Topology-aware routing** (formerly "topology-aware hints", now `service.kubernetes.io/topology-mode: Auto`) tells `EndpointSlice` controllers to set zone hints, and `kube-proxy` (or eBPF dataplane) to prefer endpoints in the local zone. The algorithm: for each Service, compute how many CPUs are available per zone; for each endpoint, set a "Hints.ForZones" annotation; kube-proxy preferentially routes to hinted endpoints.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-service
  annotations:
    service.kubernetes.io/topology-mode: Auto    # 1.27+
spec:
  selector:
    app: my-app
  ports:
  - port: 80
```

Caveats:
- Only kicks in if the Service has enough endpoints to satisfy each zone's "fair share". For small Services (3 endpoints, 3 zones), hints are usually applied. For tiny Services (1 endpoint), no hints — traffic stays default.
- Hints can be wrong if your workload is bursty: a sudden traffic spike to a zone with few replicas will fail fast. Tune `topologySpreadConstraints` on the Deployment to keep replicas balanced.

**`externalTrafficPolicy: Local`** on LoadBalancer Services: the cloud LB sends traffic only to nodes that have a matching pod. Avoids the second-hop NodePort SNAT, preserves client IP, and reduces cross-AZ when combined with NLB/ALB zone-affinity. The downside: nodes without a matching pod return `connectionRefused` to the LB's health check, so they're never picked — but if your spread is uneven, you can hot-spot one zone.

---

## 40. Cost Optimization Patterns

A staff engineer's checklist for cloud-cost hygiene in Kubernetes:

1. **Karpenter (or CA) with spot.** Mix spot and on-demand. Spot prices fluctuate 70–90% below on-demand. Karpenter handles graceful migration on reclamation.
2. **Right-size requests.** VPA in recommend-only mode + manual review. Most workloads request 5–10× their actual usage.
3. **Consolidation.** Karpenter's `consolidationPolicy: WhenEmptyOrUnderutilized` drains underutilized nodes and packs pods more tightly. Saves 20–40% on most clusters.
4. **Reserved instance / Savings Plan coverage on baseline.** The committed-utilization tier should match your floor. Karpenter on top for elasticity.
5. **Cross-AZ traffic audit.** Use VPC Flow Logs (AWS), VPC Flow Logs (GCP), or NSG flow logs (Azure) to find heavy cross-AZ flows. Topology-aware routing or service co-location.
6. **NAT Gateway costs.** Each GB through a NAT Gateway costs $0.045 on AWS. Outbound traffic from pods to public APIs (S3, DynamoDB, etc.) should use VPC endpoints (Gateway endpoints free, Interface endpoints ~$0.01/hr + ~$0.01/GB) to avoid NAT.
7. **EBS gp3 over gp2.** gp3 separates IOPS/throughput from size. Cheaper at most workload profiles.
8. **Idle resource cleanup.** Old PVs (especially gp2 from deleted PVCs with `reclaimPolicy: Retain`), unattached EBS volumes, orphaned ELBs (from misconfigured CCM tagging), idle EIPs. Tag-based scanning + automation.
9. **Image GC tuning.** Kubelet's `--image-gc-high-threshold=85` and `--image-gc-low-threshold=80` (defaults) keep disk usage tight, but on bursty image-pull workloads you may pull the same image repeatedly. Cache aggressively.
10. **Log/metric volume.** Cloud-managed logging (CloudWatch, Cloud Logging, Log Analytics) bills per GB ingested. A noisy pod can burn through $10K/month in logs. Configure log levels in apps, rate-limit at the agent.
11. **PrivateLink / Private Service Connect / Private Link Service.** For cross-VPC service consumption, these are cheaper than transit gateway + NAT and faster than VPC peering with public endpoints.
12. **Region selection.** Egress to the internet from `us-east-1` is cheaper than from `eu-west-1`. For data-egress-heavy workloads (video, ML training data), region matters.
13. **Inter-pod TLS overhead.** mTLS sidecars (Istio, Linkerd) add latency and CPU; on a 1000-pod mesh, that's measurable cost. Move to ambient mesh (Istio's ztunnel) or skip mTLS on intra-cluster paths where the CNI already provides authenticated transport (Cilium's WireGuard or Calico's WireGuard).
14. **Snapshot lifecycle.** EBS/PD/Disk snapshots accumulate; Velero with sensible retention and an actual TTL controller saves significant storage cost. Set `--default-volume-snapshot-locations` and `--default-backup-ttl=720h`.
15. **Karpenter `expireAfter`.** Forcing nodes to be replaced periodically (e.g., 30d) rotates AMIs for security but also means short-lived spot opportunities get used. Don't set it too long (stale AMIs accumulate CVEs) or too short (constant disruption).

There is one more pattern worth highlighting: **scheduled scaling**. For workloads with predictable diurnal patterns (web apps that peak at lunchtime, batch jobs at night), KEDA cron-scaler or HPA's `behavior.scaleUp`/`scaleDown.policies` can pre-warm capacity before traffic arrives, and aggressively scale down off-peak. On a 24/7 baseline of 100 nodes, scaling to 30 nodes overnight for 8 hours every day saves 23% of node-hours. Combined with spot, the savings compound.

---

## 41. Managed Kubernetes Upgrade Mechanics

Each managed offering has a different upgrade philosophy:

**EKS.**
- Control plane: in-place; you pick the target minor version, AWS upgrades the apiserver/controller-manager/scheduler in place (rolling, ~30 min).
- Node groups: you upgrade separately; either managed node groups (rolling replacement) or self-managed (you handle).
- Skew: kubelet must be within one minor of apiserver. Upgrading apiserver to 1.31 requires kubelets at 1.30 or 1.31.
- Releases: AWS supports the latest 4 minors. Old minors are "deprecated" with extended support (paid). You must upgrade ~yearly.

**GKE.**
- Release channels: Rapid, Regular, Stable. Each gets new versions at different cadences (Rapid = day 0, Regular = ~3 months later, Stable = ~6 months later).
- Auto-upgrade for both control plane and nodes (configurable maintenance windows).
- Surge upgrades configurable (`maxSurge`, `maxUnavailable`).
- GKE handles all the cloud-side glue (CCM, CSI drivers, addons).

**AKS.**
- Auto-upgrade or manual; control plane and nodes upgraded separately.
- LTS channel for security patches on supported minors.
- Maintenance windows configurable.

**Universal rules:**
- **One minor at a time.** You cannot skip from 1.28 to 1.30. Go 1.28 → 1.29 → 1.30.
- **kubelet ≤ apiserver in minor version.** Never run a kubelet ahead of the apiserver.
- **kube-proxy/CCM within two minors.** More slack here.
- **Test in non-prod first.** Every minor has subtle behavior changes (defaults flipping, gates GAing, deprecations becoming removals).
- **CRDs are forever.** A CRD installed in 1.27 still works in 1.31, but its conversion webhook needs to stay alive.

---

## 42. Multi-Cloud Abstractions: Crossplane, CAPI, Portable CSI/CNI

If "one Kubernetes cluster per cloud" isn't enough — you want to provision cloud resources *from* Kubernetes, or you want to spin up clusters declaratively across clouds — you reach for:

**Cluster API (CAPI)** ([kubernetes-sigs/cluster-api](https://github.com/kubernetes-sigs/cluster-api)). A set of CRDs (Cluster, MachineDeployment, KubeadmControlPlane, etc.) and per-provider implementations (CAPA for AWS, CAPG for GCP, CAPZ for Azure, CAPV for vSphere, ...) that turn "create a cluster" into a Kubernetes object you `kubectl apply`. The CAPI controller plus the provider controller bootstrap a new cluster via cloud APIs. See chapter 26 for depth.

**Crossplane** ([crossplane.io](https://crossplane.io)). Provider CRDs for cloud services (S3 buckets, RDS databases, GCS buckets, Cloud SQL, Storage Accounts, etc.). You write a YAML that says "I want a database", Crossplane provisions it via the cloud's API, exposes credentials as Secrets, and reconciles drift. The architecture is identical to a CCM extended to all cloud services, not just compute/LB/storage.

**Portable CNI/CSI.**
- Cilium runs on every cloud (AWS, GCP, Azure, bare-metal) with mostly-portable configuration. Per-cloud quirks (e.g., AWS ENI vs Azure CNI mode vs GCP alias IPs) are abstracted by Cilium's "datapath mode" settings.
- Calico runs everywhere; BGP mode for bare-metal, VXLAN or eBPF elsewhere.
- For CSI: no portable backend exists (you can't mount AWS EBS on GCP), but the CSI *spec* is universal. Operators that depend only on the StorageClass abstraction can move clusters.

The hard truth: **fully portable cloud-native applications are mostly aspirational.** You can move workloads between clouds if you accept some friction. You cannot move LoadBalancer annotations, IAM trust policies, or KMS key ARNs without a translation layer (Crossplane is closest).

A pragmatic multi-cloud strategy that works in 2026:

1. **Standardize the workload manifests.** Use the most-portable subset: `Deployment`, `Service: ClusterIP`, `Ingress` via Gateway API (not cloud-specific annotations), `PersistentVolumeClaim` with a generic `StorageClass` name (`fast`, `standard`) that maps to the appropriate cloud provisioner in each cluster.
2. **Hide the cloud-specific layer behind cluster-level configuration.** A platform team maintains a Helm chart or Kustomize overlay per cloud that fills in the cloud-specific annotations, IAM bindings, and storage classes. Workload teams write portable manifests; the platform translates.
3. **Use Crossplane for non-K8s cloud resources.** Buckets, queues, databases — these are the long pole of multi-cloud. Crossplane Compositions let you define a `XPostgres` claim that resolves to RDS, Cloud SQL, or Azure Database for PostgreSQL depending on the target cloud.
4. **Workload identity per cloud, mediated by SPIFFE/SPIRE.** SPIRE issues SPIFFE IDs to workloads; per-cloud trust setups (IRSA, GKE WI, Azure WI) map SPIFFE IDs to cloud identities. Workloads see only their SPIFFE SVID; the per-cloud mapping is platform concern.
5. **GitOps to drive everything.** ArgoCD/Flux apply the same Git source to each cluster, with overlays diverging only at the cloud-translation seam.

The chapter on multi-cluster (26) goes deeper on the cluster-management side; this chapter cares about the cloud-translation seam — the place where "the same workload" becomes "two different cloud configurations" — and the tools that minimize the seam's surface area.

---

## 43. Pitfalls: The Long Catalogue

The accumulated wisdom from real outages. Each item lists the symptom, the cause, and the fix.

1. **In-tree volume plugin expected post-1.26.** Symptom: PV stuck in `Pending`, events say "no volume plugin matched". Cause: PV spec uses `spec.awsElasticBlockStore` instead of `spec.csi`. Fix: install CSI driver, recreate PV using CSI spec, or rely on CSI migration in pre-1.26 clusters.

2. **`--cloud-provider=aws` (deprecated form) still in flags.** Symptom: apiserver/controller-manager fails to start after upgrade to 1.31+. Cause: the in-tree provider was removed in 1.31. Fix: change to `--cloud-provider=external` everywhere, deploy CCM separately.

3. **Wrong `providerID`.** Symptom: EBS attach fails with "instance not found", or LB target registration succeeds but the LB never sees traffic. Cause: kubelet bootstrapped with bare instance ID instead of canonical `aws:///<az>/<id>` form. Fix: correct the user-data bootstrap script; restart kubelet; re-register the Node.

4. **Cloud LB stuck Pending forever.** Symptom: `kubectl get svc` shows `<pending>` in EXTERNAL-IP. Cause: CCM not running, or CCM's IAM role lacks `elasticloadbalancing:*`. Fix: check CCM logs (`kubectl -n kube-system logs ds/cloud-controller-manager`); cloud-side errors are explicit.

5. **IRSA trust policy wrong.** Symptom: pod logs show `AccessDenied: Not authorized to perform sts:AssumeRoleWithWebIdentity`. Cause: trust policy's `Condition` mismatches the SA. Fix: verify the exact OIDC issuer URL, the `sub` claim (`system:serviceaccount:NS:NAME`), and the `aud` (typically `sts.amazonaws.com`).

6. **IRSA projected audience missing.** Symptom: pod gets the env vars but the token file is missing or empty. Cause: the eks-pod-identity-webhook is not running or has wrong failurePolicy. Fix: install/restart the webhook; check its logs for admission errors.

7. **SA annotation typo.** Symptom: pod doesn't get IRSA env vars at all (no `AWS_ROLE_ARN`). Cause: typo in `eks.amazonaws.com/role-arn` (most often `eks.amazonws.com`). Fix: the webhook silently ignores unknown annotations; verify spelling.

8. **LB security group blocking health check.** Symptom: LB shows targets as `unhealthy` even though pods are fine. Cause: the LB's security group doesn't allow inbound on the health check port to the node SG. Fix: explicitly allow the LB SG → node SG on the health check port (kubelet usually figures this out, but with custom SGs you must do it).

9. **ALB IP target group label mismatch.** Symptom: pods come up Ready but the ALB target group is empty. Cause: AWS LBC's TargetGroupBinding selects on pod labels; if the Deployment changes labels mid-rollout, old targets stay registered, new pods don't get added. Fix: align label selectors; check the LBC controller logs.

10. **ExternalDNS not authoritative.** Symptom: ExternalDNS log says "zone not found" or records don't appear. Cause: the cloud DNS zone isn't actually authoritative for the domain (delegation issue at the registrar). Fix: verify NS records at the registrar match the zone's name servers.

11. **cert-manager HTTP-01 challenge on a private cluster.** Symptom: ACME challenge times out, certificate stuck `Issuing`. Cause: Let's Encrypt's validation servers can't reach the cluster ingress. Fix: switch to DNS-01 challenge via cloud DNS provider; or use ACM/managed certs instead.

12. **ESO rate-limited.** Symptom: ExternalSecret status shows `SecretSyncedError: rate exceeded`. Cause: too many ExternalSecrets refreshing too often. Fix: raise `refreshInterval`; batch unrelated secrets; consider provider-side caching.

13. **KMS provider degraded.** Symptom: `kubectl get secrets` returns 500 errors; apiserver logs show "failed to decrypt". Cause: KMS plugin process died, KMS key disabled, IAM lost. Fix: restart plugin; check KMS key state; verify the apiserver's identity has `kms:Decrypt`.

14. **Reading workload creds before sidecar injection.** Symptom: init container fails with "no credentials available" while the main container works. Cause: the AWS pod-identity webhook injects env vars into all containers, but if your init container reads the token file at startup, it may race with the projected-token volume becoming available. Fix: retry in the init container, or move credential reads to the main container.

15. **Pod-identity webhook missing.** Symptom: every SA-annotated pod gets no IRSA injection. Cause: the webhook was uninstalled or its certificate expired. Fix: redeploy the webhook; cert-manager-managed certs renew, but custom-CA setups need monitoring.

16. **Azure legacy AAD Pod Identity still installed.** Symptom: pods get `azure.workload.identity/use: true` but no token; also see legacy `aadpodidentity.k8s.io` CRDs. Cause: leftover from migration. Fix: uninstall AAD Pod Identity components; only Azure AD Workload Identity should remain.

17. **Karpenter without proper IAM.** Symptom: Karpenter logs say `UnauthorizedOperation` when calling `ec2:RunInstances`. Cause: Karpenter's controller IRSA role lacks the needed perms, *or* the node role isn't passable. Fix: ensure both Karpenter controller role (with `ec2:*`, `iam:PassRole`) and node role exist and are correctly bound.

18. **CCM not removing init taints.** Symptom: nodes Ready but with `node.cloudprovider.kubernetes.io/uninitialized:NoSchedule` permanently. Cause: CCM crashed during node init, or doesn't have RBAC to patch Nodes. Fix: check CCM logs; restart; verify ClusterRoleBinding.

19. **Cloud LB stickiness misconfig.** Symptom: requests for the same client end up on different pods, breaking session state. Cause: stickiness annotation is wrong scope (ALB supports per-target-group, NLB doesn't). Fix: enable stickiness at the target group level via `aws-load-balancer-target-group-attributes`; or switch to L7 if you need it.

20. **ALB Ingress without subnets annotation.** Symptom: ALB provisioning fails with "no subnets found". Cause: cluster has tagged subnets with `kubernetes.io/role/elb: 1` (for internet-facing) or `internal-elb: 1` (for internal), but at least one subnet per AZ is missing. Fix: tag at least one subnet per AZ in the desired type.

21. **NAT gateway as SPOF.** Symptom: outbound traffic from all pods fails when one AZ goes down. Cause: single NAT gateway in one AZ, all subnets route through it. Fix: one NAT gateway per AZ; route table per AZ pointing at the local NAT.

22. **Cilium native routing on cloud requiring encap.** Symptom: pod-to-pod traffic across nodes drops. Cause: Cilium configured in native-routing mode but the cloud doesn't propagate pod CIDR. Fix: switch to VXLAN encap, or use cloud-native CNI integration (AWS ENI mode, Azure mode).

23. **Cluster running before CCM init.** Symptom: nodes register but stay tainted, workloads never schedule. Cause: bootstrap order wrong — kubelets started before CCM Deployment was applied. Fix: ensure CCM is a static pod or pre-bootstrapped; tolerate the init taint on the CCM itself.

24. **Upgrading cloud-provider binary while leader.** Symptom: cloud reconciliation pauses for 30+ seconds during CCM upgrade. Cause: leader-elected CCM is upgraded; new replica must wait for the lease to expire. Fix: use `maxSurge: 1, maxUnavailable: 0` so the new pod is up before the old one is killed; lease handoff is then graceful.

25. **Cluster identity tag missing on resources.** Symptom: orphaned LBs/SGs/volumes from deleted clusters accumulate, billing climbs. Cause: CCM-created resources lacked `kubernetes.io/cluster/<name>: owned` because the cluster name was misconfigured. Fix: rigorous `--cluster-name` flag review; periodic orphan-cleanup audits via the cloud's tagging API.

26. **Cross-account IAM trust for IRSA.** Symptom: IRSA works for in-account roles but fails for cross-account. Cause: the in-account role's trust policy is correct, but the cross-account role's trust policy must explicitly trust both the OIDC provider *and* `sts:AssumeRole` from the in-account role. Fix: use the "AssumeRole via in-account-IRSA, then AssumeRole into cross-account" two-step pattern, or add a trust relationship to the cross-account role.

27. **GKE Workload Identity broken after node-pool upgrade.** Symptom: pods on new node pool can't get GSA tokens; metadata server returns 403. Cause: the node pool wasn't created with `--workload-pool` enabled, or the `iam.workloadIdentityUser` binding's `member` references a stale namespace. Fix: recreate node pool with WI enabled; re-bind GSA to the KSA.

28. **AKS managed identity vs Workload Identity confusion.** Symptom: pod tries to read from Azure Key Vault, gets "no managed identity available" even though SA is annotated. Cause: pod's namespace/SA isn't matched by a federated identity credential on the AAD App; OR the AAD App's federated cred uses wrong audience (`api://AzureADTokenExchange` is the canonical value). Fix: verify federated cred params match SA exactly.

29. **Topology-aware routing not kicking in.** Symptom: enabled `service.kubernetes.io/topology-mode: Auto` but cross-AZ traffic continues. Cause: Service has too few endpoints relative to zones (algorithm needs slack). Fix: scale up replicas (3 per zone minimum), or use `topologyKeys` (deprecated) / explicit zone hints.

30. **`externalTrafficPolicy: Local` with no local pods.** Symptom: LB sends traffic to nodes that have no matching pod; those nodes return connectionRefused. Cause: nodes without a matching pod fail the health check, but if your pods drift (e.g., during rolling update), there can be moments when no pod is local to any node. Fix: ensure DaemonSet-shape distribution, or use `topologySpreadConstraints` to guarantee per-zone presence.

31. **CSI driver crash-looping on a node.** Symptom: pods on that node can't mount PVs. Cause: CSI driver's IRSA role missing on that node (custom Karpenter node class without proper IRSA setup). Fix: ensure all node groups have the CSI driver's IRSA role bound.

32. **Karpenter consolidation churning pods.** Symptom: nodes constantly come and go; PDBs frequently violated. Cause: `consolidateAfter` too low + workloads with strict PDBs. Fix: raise `consolidateAfter`; review PDBs.

33. **`Service` annotation typos.** Symptom: LB provisions but lacks expected feature (e.g., TLS not enabled). Cause: typo in annotation key (e.g., `aws-load-balancer-sll-cert` instead of `ssl-cert`). The CCM silently ignores unknown annotations. Fix: validate against the provider's annotation reference.

34. **GKE Ingress + Service without NEG.** Symptom: latency higher than expected; client IP not preserved. Cause: Service lacks `cloud.google.com/neg: '{"ingress": true}'`; LB forwards to instance NEGs (node IPs). Fix: add the annotation.

35. **Old TLS cert in ACM lingering.** Symptom: ALB serves old cert after cert-manager renewal. Cause: ALB annotation pinned to a specific ACM ARN; the ACM-managed cert renews to a different ARN. Fix: use ACM with the same ARN auto-renewal; or use `auto-cert-mapping` annotation.

36. **VPC IP exhaustion on AWS.** Symptom: pods stuck in `ContainerCreating` with CNI error "no IP addresses available". Cause: secondary IP pool depleted; subnet is full. Fix: enable prefix delegation; add more subnets to the cluster.

37. **CCM `ConfigMap`-driven configuration changes not applied.** Symptom: changed CCM config (e.g., new cluster CIDR), pods unaffected. Cause: CCM doesn't watch its config file. Fix: restart CCM pods (rolling restart).

38. **Service controller race during node taint flap.** Symptom: LB members oscillate (node added/removed/added). Cause: node briefly NotReady due to kubelet flake; service controller deregisters; node comes back; re-registered. Fix: usually benign; ensure target group deregistration delay is shorter than node Ready-flapping period.

39. **ExternalDNS deleting records it shouldn't.** Symptom: a manually-created A record disappears. Cause: `--policy=sync` plus owner-id mismatch. Fix: `--policy=upsert-only`; or set unique txt-owner-id.

40. **`kms` provider in EncryptionConfiguration v1 vs v2.** Symptom: cluster apiserver fails to read existing secrets after upgrade. Cause: encryption config written for v1 (single key) loaded into a v2-only apiserver. Fix: keep `identity` as a fallback during migration; rotate secrets through new provider.

---

## 44. Operator's Cheat Sheet

A short reference card for the common scenarios:

**"I created a Service: type=LoadBalancer and EXTERNAL-IP is `<pending>`."**
1. `kubectl get events -A | grep -i loadbalancer` — look for explicit cloud errors.
2. `kubectl -n kube-system logs -l app=cloud-controller-manager --tail=200` — same.
3. Verify CCM is leader-elected: `kubectl -n kube-system get lease cloud-controller-manager`.
4. Verify cloud IAM/role has LB perms.
5. Check Service annotations against the provider's supported list.

**"A new Node won't schedule anything."**
1. `kubectl describe node <name>` — look for `node.cloudprovider.kubernetes.io/uninitialized:NoSchedule`.
2. Check CCM logs for that Node's name.
3. Check kubelet was started with `--cloud-provider=external` and ideally `--provider-id=<canonical>`.

**"My pod can't reach AWS APIs even though IRSA is set up."**
1. `kubectl exec <pod> -- env | grep AWS_` — verify env vars are injected.
2. `kubectl exec <pod> -- cat $AWS_WEB_IDENTITY_TOKEN_FILE` — should be a JWT.
3. Decode the JWT (`jq` + base64); check `aud` and `sub` match the role's trust policy.
4. Check the role's trust policy in IAM — typo in OIDC provider ARN or `sub` condition is the usual cause.

**"GKE Workload Identity isn't working."**
1. `kubectl get sa -n <ns> <name> -o yaml` — verify `iam.gke.io/gcp-service-account` annotation.
2. `gcloud iam service-accounts get-iam-policy <gsa-email>` — verify `roles/iam.workloadIdentityUser` member is `serviceAccount:<project>.svc.id.goog[<ns>/<ksa>]`.
3. `kubectl -n kube-system logs ds/gke-metadata-server` — look for the pod's IP.
4. Cluster has `--workload-pool` enabled? Node pool too?

**"Azure WI not minting tokens."**
1. Pod has label `azure.workload.identity/use: "true"`?
2. SA has `azure.workload.identity/client-id` annotation matching the AAD App?
3. AAD App has federated cred with the right issuer/subject/audience?
4. Webhook running? `kubectl -n azure-workload-identity-system get pods`.

**"KMS encryption is broken."**
1. `kubectl -n kube-system logs -l component=kms-plugin` — most diagnostic.
2. `kubectl get secrets -A` returns 500 → apiserver-side fail; check kube-apiserver logs.
3. Cloud-side: is the KMS key enabled? Does the apiserver's IAM have `Decrypt`?

**"ExternalDNS is not creating records."**
1. `kubectl -n external-dns logs deploy/external-dns` — verbose.
2. Verify `--domain-filter` covers the requested hostname.
3. Verify IAM/SA has zone-edit perms.
4. Check the txt registry — is there a stale owner-id record blocking ownership?

---

## 45. Further Reading and Source Pointers

The chapter has cross-referenced many repositories; here is a consolidated list of the canonical sources.

**Kubernetes core:**
- [kubernetes/cloud-provider](https://github.com/kubernetes/cloud-provider) — the generic interface and controllers.
- [kubernetes/kubernetes](https://github.com/kubernetes/kubernetes) `cmd/kube-controller-manager`, `cmd/kubelet` — where the `--cloud-provider=external` machinery lives.
- KEPs: [KEP-2395](https://github.com/kubernetes/enhancements/tree/master/keps/sig-cloud-provider/2395-removing-in-tree-cloud-providers), [KEP-625 CSI migration](https://github.com/kubernetes/enhancements/tree/master/keps/sig-storage/625-csi-migration).

**Per-cloud CCM:**
- [kubernetes/cloud-provider-aws](https://github.com/kubernetes/cloud-provider-aws)
- [kubernetes/cloud-provider-gcp](https://github.com/kubernetes/cloud-provider-gcp)
- [kubernetes-sigs/cloud-provider-azure](https://github.com/kubernetes-sigs/cloud-provider-azure)
- [kubernetes/cloud-provider-openstack](https://github.com/kubernetes/cloud-provider-openstack), [kubernetes/cloud-provider-vsphere](https://github.com/kubernetes/cloud-provider-vsphere), [equinix/cloud-provider-equinix-metal](https://github.com/equinix/cloud-provider-equinix-metal).

**AWS:**
- [aws/amazon-vpc-cni-k8s](https://github.com/aws/amazon-vpc-cni-k8s) — the VPC CNI.
- [kubernetes-sigs/aws-load-balancer-controller](https://github.com/kubernetes-sigs/aws-load-balancer-controller) — ALB/NLB controller.
- [kubernetes-sigs/aws-ebs-csi-driver](https://github.com/kubernetes-sigs/aws-ebs-csi-driver), [kubernetes-sigs/aws-efs-csi-driver](https://github.com/kubernetes-sigs/aws-efs-csi-driver).
- [aws/amazon-eks-pod-identity-webhook](https://github.com/aws/amazon-eks-pod-identity-webhook) — IRSA injector.
- [aws/eks-pod-identity-agent](https://github.com/aws/eks-pod-identity-agent) — Pod Identity agent.
- [aws/karpenter-provider-aws](https://github.com/aws/karpenter-provider-aws).

**GCP:**
- [kubernetes-sigs/gcp-compute-persistent-disk-csi-driver](https://github.com/kubernetes-sigs/gcp-compute-persistent-disk-csi-driver).
- [kubernetes-sigs/filestore-csi-driver](https://github.com/kubernetes-sigs/filestore-csi-driver).
- [GoogleCloudPlatform/k8s-cloud-provider](https://github.com/GoogleCloudPlatform/k8s-cloud-provider).
- [kubernetes/ingress-gce](https://github.com/kubernetes/ingress-gce) — the GKE Ingress controller.

**Azure:**
- [kubernetes-sigs/azuredisk-csi-driver](https://github.com/kubernetes-sigs/azuredisk-csi-driver), [kubernetes-sigs/azurefile-csi-driver](https://github.com/kubernetes-sigs/azurefile-csi-driver).
- [Azure/application-gateway-kubernetes-ingress](https://github.com/Azure/application-gateway-kubernetes-ingress) — AGIC.
- [Azure/azure-workload-identity](https://github.com/Azure/azure-workload-identity).
- [Azure/kubernetes-kms](https://github.com/Azure/kubernetes-kms).

**Cloud-agnostic glue:**
- [kubernetes-sigs/external-dns](https://github.com/kubernetes-sigs/external-dns).
- [external-secrets/external-secrets](https://github.com/external-secrets/external-secrets).
- [kubernetes-sigs/secrets-store-csi-driver](https://github.com/kubernetes-sigs/secrets-store-csi-driver), with [aws](https://github.com/aws/secrets-store-csi-driver-provider-aws), [gcp](https://github.com/GoogleCloudPlatform/secrets-store-csi-driver-provider-gcp), [azure](https://github.com/Azure/secrets-store-csi-driver-provider-azure) providers.
- [cert-manager/cert-manager](https://github.com/cert-manager/cert-manager).
- [crossplane/crossplane](https://github.com/crossplane/crossplane).
- [kubernetes-sigs/cluster-api](https://github.com/kubernetes-sigs/cluster-api) with provider repositories.

**Specifications:**
- [Container Storage Interface (CSI) spec](https://github.com/container-storage-interface/spec).
- [Container Network Interface (CNI) spec](https://github.com/containernetworking/cni/blob/main/SPEC.md).
- [Gateway API](https://gateway-api.sigs.k8s.io).

---

**The closing thought.** Cloud-provider integration in Kubernetes used to be a giant tangle of compiled-in code, single-binary credentials, and per-cloud surprise. The out-of-tree split made it boring in the best sense: every cloud is now a separate, swappable controller, with its own credentials, its own bug curve, and its own release. The pieces are still complex — IRSA, NEG, ACM, KMS — but each one is *factored*. Once you can name the controller responsible for a given annotation, every "why isn't the cloud doing X?" stops being magic and starts being a tractable debugging task: identify the controller, read its logs, check its IAM, check its CRDs. That mental shift — from "Kubernetes does it" to "this specific controller does it, here is its source" — is the whole point of the migration, and the staff-engineer competence this chapter aims to build.
