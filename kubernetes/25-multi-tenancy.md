# Multi-Tenancy

Multi-tenancy is the question every Kubernetes platform team confronts on day 30: *how do we put N teams (or N customers, or N environments) onto M clusters without N×M operational cost, without one tenant ruining another's day, and without trusting any of them too far?* The answer is never a single feature. It is a *stack* — naming, identity, capacity, network, workload, kernel, and ultimately control-plane — applied with full awareness that the namespace is a logical scope, not a security boundary, and that "soft" multi-tenancy is a polite agreement among friends while "hard" multi-tenancy is a separate cluster.

This chapter is about that stack. We start with the multi-tenancy spectrum (§2), nail the namespace's job description (§3–4), build the five-layer mental model (§5) that every later section refines, walk through soft-tenant patterns the platform team writes for trusted teams (§6–12), introduce the operators that make soft tenancy declarative — Hierarchical Namespace Controller (§13), Capsule (§14), Kiosk (§15) — then spend the longest stretch of the chapter on **vCluster** (§16–22), the standout middle option that runs a complete virtual control plane inside a host-cluster namespace. From there we cover the dimensions that the namespace-and-policy approach can't fully isolate: network (§24), storage (§25), compute and noisy neighbors (§26–27), cost attribution (§28), audit (§29), tenant-aware operators (§30), onboarding/offboarding (§31), the platform/tenant responsibility matrix (§32), and finally the 25+ pitfalls (§33) that show up in every multi-tenant production cluster.

Pre-reqs: chapter 07 (RBAC, ServiceAccounts, impersonation — the identity substrate every tenancy model rides on), chapter 20 (NetworkPolicy, ANP/BANP — the only way to actually segment east-west traffic), chapter 21 (ResourceQuota, LimitRange, PriorityClass — the capacity controls), chapter 06 (admission control — where Kyverno/Gatekeeper/VAP enforce tenant rules). Forward-references: chapter 26 (multi-cluster — the hard-tenancy option when a vCluster isn't enough), chapter 28 (Pod Security Admission and policy engines in depth), chapter 29 (gVisor/Kata/Confidential Containers — the kernel-isolation answer for hostile tenants).

---

## Table of Contents

1.  [What "Multi-Tenant" Actually Means](#1-what-multi-tenant-actually-means)
2.  [The Multi-Tenancy Spectrum](#2-the-multi-tenancy-spectrum)
3.  [The Namespace Is Not a Security Boundary](#3-the-namespace-is-not-a-security-boundary)
4.  [What Namespaces Actually Provide](#4-what-namespaces-actually-provide)
5.  [The Five Layers of Multi-Tenancy](#5-the-five-layers-of-multi-tenancy)
6.  [Soft Tenancy Layer 1: Naming and Namespaces](#6-soft-tenancy-layer-1-naming-and-namespaces)
7.  [Soft Tenancy Layer 2: RBAC Bound to Groups](#7-soft-tenancy-layer-2-rbac-bound-to-groups)
8.  [Soft Tenancy Layer 3: Capacity (Quota, LimitRange, PriorityClass)](#8-soft-tenancy-layer-3-capacity-quota-limitrange-priorityclass)
9.  [Soft Tenancy Layer 4: Default-Deny Network](#9-soft-tenancy-layer-4-default-deny-network)
10. [Soft Tenancy Layer 5: Workload Policy (PSA, Kyverno, VAP)](#10-soft-tenancy-layer-5-workload-policy-psa-kyverno-vap)
11. [Cross-Namespace References: ReferenceGrant](#11-cross-namespace-references-referencegrant)
12. [The Per-Tenant Bundle: A Single YAML That Onboards a Team](#12-the-per-tenant-bundle-a-single-yaml-that-onboards-a-team)
13. [Hierarchical Namespace Controller (HNC)](#13-hierarchical-namespace-controller-hnc)
14. [Capsule: Tenant-as-a-Resource](#14-capsule-tenant-as-a-resource)
15. [Kiosk: The Predecessor](#15-kiosk-the-predecessor)
16. [vCluster: A Virtual Control Plane Per Tenant](#16-vcluster-a-virtual-control-plane-per-tenant)
17. [vCluster Architecture: The Syncer](#17-vcluster-architecture-the-syncer)
18. [vCluster Sync Modes and Object Translation](#18-vcluster-sync-modes-and-object-translation)
19. [vCluster Storage: SQLite, etcd, External](#19-vcluster-storage-sqlite-etcd-external)
20. [When vCluster Wins](#20-when-vcluster-wins)
21. [When vCluster Loses](#21-when-vcluster-loses)
22. [Hard Multi-Tenancy: Separate Clusters](#22-hard-multi-tenancy-separate-clusters)
23. [Confidential / Hostile Tenant Patterns](#23-confidential--hostile-tenant-patterns)
24. [Network Multi-Tenancy: NetworkPolicy + ANP + Egress Gateways](#24-network-multi-tenancy-networkpolicy--anp--egress-gateways)
25. [Storage Multi-Tenancy: Per-Tenant StorageClass](#25-storage-multi-tenancy-per-tenant-storageclass)
26. [Compute Multi-Tenancy: Shared vs Dedicated Nodes](#26-compute-multi-tenancy-shared-vs-dedicated-nodes)
27. [The Noisy Neighbor Problem](#27-the-noisy-neighbor-problem)
28. [Cost Attribution Per Tenant](#28-cost-attribution-per-tenant)
29. [Audit Per Tenant](#29-audit-per-tenant)
30. [Multi-Tenant Operators](#30-multi-tenant-operators)
31. [Tenant Onboarding and Offboarding](#31-tenant-onboarding-and-offboarding)
32. [The Platform/Tenant Responsibility Matrix](#32-the-platformtenant-responsibility-matrix)
33. [Pitfalls](#33-pitfalls)
34. [TL;DR](#34-tldr)

---

## 1. What "Multi-Tenant" Actually Means

Before any technology, the word *tenant* has to be defined. The implementation that follows is downstream of this definition, and platform teams that skip it ship the wrong primitive for the wrong tenant.

A tenant might be any of the following:

| Tenant model | Example | Trust assumption | Implementation bias |
|---|---|---|---|
| **Application team** | "the payments team" | Trusted; same org; same security perimeter | Namespace-per-team + RBAC. PSA `baseline` or `restricted`. |
| **Environment** | "dev / staging / prod" | Fully trusted between envs, but blast-radius must differ | Cluster-per-env *or* namespace-per-env on a non-prod cluster. |
| **Internal product** | "the data platform" hosting many teams' jobs | Trusted humans, untrusted code at runtime | Namespace-per-team with strict PSA `restricted`, policy engine. |
| **Customer (SaaS)** | "tenant ACME-Corp on our hosted product" | Untrusted; their bug must not exfiltrate Tenant-B's data | vCluster *or* cluster-per-tenant. Never a shared namespace. |
| **Fleet member** | "edge cluster #1742" | Trusted physical asset; bandwidth-limited link | Multi-cluster (ch 26), not multi-tenancy. |
| **Hostile/regulated workload** | "the contractor's binary that we MUST run" | Hostile by default | Dedicated cluster + sandbox runtime (ch 29) + dedicated nodes. |

The mistake is to pick the technology before the tenant model. A team that means "we have ten dev teams sharing one cluster" reaches for vCluster when namespaces + Capsule would do. A team that means "we sell SaaS to 10,000 customers" reaches for namespaces when nothing short of separate clusters (or at least vClusters) is defensible. Throughout this chapter, when we say "tenant" we mean *whatever your definition above is* — but every decision rule will note which row of that table it applies to.

### 1.1 The two questions that decide everything

```
                ┌────────────────────────────────────────────────────────┐
                │  Q1: Do I trust the tenant's code with kernel access?  │
                │      (i.e. can a privileged pod here own the node?)    │
                └─────────────────┬──────────────────────────────────────┘
                                  │
                  ┌───────────────┴────────────────┐
                YES                                NO
                  │                                │
                  ▼                                ▼
       Soft multi-tenancy is OK            Hard multi-tenancy required
       (namespaces + policy)                (separate clusters,
                                             or vCluster + sandbox,
                                             or sandbox runtime)
                  │
                  ▼
                ┌────────────────────────────────────────────────────────┐
                │  Q2: Does the tenant need their own CRDs / cluster-    │
                │      scoped APIs / cluster-admin-shaped permissions?   │
                └─────────────────┬──────────────────────────────────────┘
                                  │
                  ┌───────────────┴────────────────┐
                YES                                NO
                  │                                │
                  ▼                                ▼
            vCluster                       Plain namespace tenancy
            (virtual control plane)        (Capsule / HNC / hand-rolled)
```

Q1 is a security question. Q2 is an API-surface question. They are orthogonal. A team that needs CRDs but is trusted gets a vCluster on a shared host. A SaaS customer with no CRDs but hostile code gets a separate cluster anyway, because Q1 dominates.

---

## 2. The Multi-Tenancy Spectrum

There are four points on the spectrum, in order of increasing isolation and increasing cost:

```
       SHARED ──────────────────────────────────────────► ISOLATED
       cheap                                              expensive

   ┌───────────┐   ┌───────────┐   ┌───────────┐   ┌───────────┐
   │ Single-   │   │ Soft MT   │   │ vCluster  │   │ Cluster   │
   │ tenant    │   │ namespace │   │ per       │   │ per       │
   │ cluster   │   │ per team  │   │ tenant    │   │ tenant    │
   └─────┬─────┘   └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
         │               │               │               │
   one team owns     N teams share   N virtual CPs   N real clusters
   the whole         one CP + one    on 1 host CP    (kubeadm/ClusterAPI
   cluster           data plane      + 1 data plane  /managed)
         │               │               │               │
   no tenant logic   namespace,      virtual k8s API   real kernel
   needed            RBAC, NP, RQ,   per tenant;       isolation between
                     LR, PSA, etc    syncer translates clusters; full
                                     to host           HA per tenant

   blast radius:    blast radius:    blast radius:    blast radius:
   whole cluster    whole cluster    whole cluster    one tenant
                    (shared kernel)  (shared kernel)
   control plane    control plane    control plane    control plane
   cost: 1          cost: 1          cost: 1 host +   cost: N
                                     N small vCPs
   tenant gets:     tenant gets:     tenant gets:     tenant gets:
   - everything     - a scoped       - a fake         - a real
                      view             cluster          cluster
                    - shared CRDs    - own CRDs       - own CRDs
                    - shared admin   - own RBAC       - own admin
                                       root inside     including node
                                       the vCluster    access
```

The *cost* axis runs three ways simultaneously: control-plane cost (etcd + apiserver + controllers + scheduler), operational cost (upgrades, monitoring, oncall), and human cost (separate runbooks, separate dashboards, separate identity wiring). The *isolation* axis runs three ways too: API isolation (can tenant A see tenant B's objects?), failure-domain isolation (does tenant A's bad CRD crash tenant B?), and security isolation (can tenant A's pod read tenant B's secrets, or own the node, or the etcd?).

A platform usually lands on *two* points simultaneously: "soft MT for trusted internal teams; vCluster for teams that ship CRDs; separate cluster for the regulated workload." A platform should *not* try to pretend that one point covers all cases — that always ends with either over-provisioned clusters (everyone gets a private one) or a security review finding (the dental hygienist's bookkeeping app shares a kernel with the company's tax records).

### 2.1 What each option actually provides

| Property | Single | Soft MT | vCluster | Cluster |
|---|---|---|---|---|
| Tenant API surface | full | full but **shared** (CRD conflicts, cluster-scoped clashes) | full, **per tenant** | full, per cluster |
| Tenant cluster-admin? | yes (the team is the cluster) | no | yes, *inside* the vCluster | yes |
| CRD versioning per tenant? | n/a | no (one global registry) | yes | yes |
| Node isolation? | n/a (one team owns all nodes) | no | no (host kernel shared) | yes |
| Container escape blast radius | the cluster | the cluster | the cluster | one cluster |
| Cost per tenant | n/a | tiny (objects only) | small (one pod-group control plane) | large (full HA cluster) |
| Upgrade independence | trivial | none (one cluster upgrade) | partial (vCluster can lag host) | full |
| Network egress identity | shared | shared | shared | per cluster |
| Audit per tenant | filter by namespace | filter by namespace | per-vCluster audit | per cluster |
| Operational owners | one team | one platform team | one platform team + tenants inside | per-cluster team |

The lesson buried in this table: **vCluster moves the API-surface and CRD-versioning columns from "no" to "yes," but leaves every kernel-level column unchanged.** That's why it is "soft multi-tenancy with extras," not "hard multi-tenancy."

---

## 3. The Namespace Is Not a Security Boundary

This is the sentence that must be said first, last, and at every quarterly architecture review. A `Namespace` in Kubernetes is a **logical** scope for names, RBAC, quotas, default policies, and network policies. It is *not* an isolation primitive in the kernel sense. A privileged pod in *any* namespace can almost always own the node, and from the node it can read every other pod's secrets, mount every other pod's volume, sniff every other pod's traffic, and reach the kubelet's credentials to talk to the apiserver as the node.

Specifically:

1. **A pod with `hostPath` can mount `/`.** Done — read every other pod's filesystem, drop a binary into a place that runs at boot, exfiltrate every Secret.
2. **A pod with `privileged: true` runs with all capabilities and an unrestricted seccomp.** It can `nsenter` into other containers (it has `CAP_SYS_ADMIN`), `setns()` to the host PID/mount/net namespace, or `mknod` block devices to access raw disks.
3. **A pod with `hostNetwork: true` shares the host's network namespace.** It can sniff every other pod's traffic on the node, bind to the kubelet's loopback API, and reach the cloud metadata service in its raw form.
4. **A pod with `hostPID: true` can see every process on the node, including the kubelet, and inject signals.**
5. **A pod with a too-permissive ServiceAccount** (`cluster-admin`, or "create pods in any namespace") doesn't even need a container escape — it just impersonates by creating a pod in `kube-system` that mounts every secret.
6. **A pod with allowed `CAP_NET_ADMIN`** can rewrite `iptables` on the host and intercept traffic.
7. **A pod that can mount any PV** with `ReadWriteMany` can corrupt another tenant's data without touching the node.

The kernel does not know what a namespace is. The kernel knows mount namespaces, PID namespaces, network namespaces, user namespaces, cgroups, capabilities, seccomp filters, AppArmor profiles, SELinux labels. The Kubernetes *Namespace* object exists only inside the apiserver's etcd. If a pod is configured (or via escape) to talk to the kernel as root, it bypasses the apiserver entirely.

```
   What the Kubernetes Namespace object actually controls:

   ┌───────────────────────────────────────────────────────────────┐
   │  apiserver storage:                                           │
   │    /registry/pods/<ns>/<name>     ← name uniqueness scope     │
   │  RBAC subjects:                                               │
   │    Role/RoleBinding scoped to <ns> ← who can do what here     │
   │  Admission:                                                   │
   │    ResourceQuota matches by <ns>   ← capacity caps            │
   │    LimitRange matches by <ns>      ← per-object defaults      │
   │    PSA labels are on the namespace ← workload policy          │
   │  Controllers:                                                 │
   │    NetworkPolicy selector by <ns>  ← network east-west        │
   └───────────────────────────────────────────────────────────────┘

   What it does NOT control:

   ┌───────────────────────────────────────────────────────────────┐
   │  Linux namespaces (mnt/pid/net/ipc/uts/cgroup/user/time)      │
   │  Linux capabilities                                           │
   │  seccomp / AppArmor / SELinux profiles                        │
   │  cgroup hierarchy                                             │
   │  kernel syscalls                                              │
   │  the host filesystem                                          │
   │  the host network interfaces                                  │
   │  the cloud metadata service                                   │
   │  inter-pod traffic on the same node                           │
   └───────────────────────────────────────────────────────────────┘
```

This is *not* a Kubernetes bug; it's a layering invariant. Kubernetes orchestrates the kernel; it does not replace it. Hardening that closes those gaps lives at the kernel layer: Pod Security Admission profiles (`restricted`), policy-engine constraints (Kyverno/Gatekeeper) that reject `hostPath`/`privileged`/etc., and at the limit sandbox runtimes (gVisor, Kata) that put a syscall barrier between tenant and host kernel (chapter 29). The PSA `restricted` profile is the bare minimum to claim a namespace is even *attempting* isolation.

**A clean way to think about it:** the namespace is the *blame attribution* boundary, not the *blast radius* boundary. Audit by namespace. Quota by namespace. Bill by namespace. But assume any kernel-level compromise within the cluster is a *cluster-wide* incident.

---

## 4. What Namespaces Actually Provide

That said, namespaces are necessary and useful. They are the unit on which every other tenancy mechanism in this chapter is keyed. A non-namespaced multi-tenant cluster is impossible to operate.

Concretely, the namespace provides:

1. **Naming scope.** `Pod foo` in namespace `team-a` does not collide with `Pod foo` in `team-b`. Same for ConfigMaps, Secrets, Services, etc. *Cluster-scoped* resources (Nodes, PersistentVolumes, ClusterRoles, CRDs, ValidatingAdmissionPolicies, RuntimeClasses, IngressClasses, StorageClasses, PriorityClasses) are NOT in any namespace and so can collide across tenants — this is the source of half the headaches that vCluster solves (§16).
2. **RBAC scope.** A `Role` lives in a namespace; a `RoleBinding` binds a Role (or a ClusterRole) *to a namespace*. `cluster-admin` to a namespace via a RoleBinding lets a tenant own everything *in that namespace* but nothing outside. This is the foundation of soft tenancy.
3. **ResourceQuota scope.** A `ResourceQuota` object lives in a namespace and caps aggregate `requests.cpu`, `requests.memory`, `pods`, `services`, `persistentvolumeclaims`, custom resources, and more (§8). Without per-namespace quota, the first tenant to misconfigure starves all others.
4. **LimitRange scope.** A `LimitRange` in a namespace supplies *defaults* and *maxima* for individual containers. This is what catches the "developer forgot to set requests" footgun before it reaches the scheduler.
5. **NetworkPolicy scope.** A `NetworkPolicy` selects pods by labels *within its own namespace*. Cross-namespace flows must be opened with `namespaceSelector`. This is the bulk of east-west segmentation (ch 20).
6. **PSA scope.** Pod Security Admission applies via labels *on the Namespace object* (`pod-security.kubernetes.io/enforce=restricted`). PSA evaluates pod specs on admission and rejects violations.
7. **Default ServiceAccount scope.** Every namespace has a `default` SA. Pods that don't specify one get it; per-namespace SA segregation means a stolen SA token belongs to one tenant's blast radius.
8. **Event scope.** `kubectl get events -n team-a` is one-tenant's view of what just happened.
9. **Quota for object counts.** `count/deployments.apps`, `count/services`, `secrets`, etc. — useful to prevent a tenant from creating a million Secrets and blowing up etcd (a real attack vector at scale).
10. **Finalizer / deletion scope.** Deleting a namespace cascades to every namespaced object in it. Tenant offboarding is `kubectl delete ns team-a` plus cleanup of cluster-scoped resources owned by the tenant.

None of these provide kernel-level isolation. All of them are necessary for a working multi-tenant cluster. The shape of soft tenancy is "use *every one* of these primitives, consistently, with a policy engine to enforce that no tenant can disable them."

---

## 5. The Five Layers of Multi-Tenancy

This is the mental model the rest of the chapter elaborates. A tenant lives inside five concentric rings, and each ring has a specific Kubernetes mechanism. Miss a ring and the rest don't matter.

```
                ┌──────────────────────────────────────────────────────┐
                │  L5 WORKLOAD                                         │
                │  Pod Security Admission, policy engines, RuntimeClass│
                │  → "what can this Pod actually run on the kernel?"   │
                │  ┌────────────────────────────────────────────────┐  │
                │  │  L4 NETWORK                                    │  │
                │  │  NetworkPolicy, ANP/BANP, egress gateways      │  │
                │  │  → "who can this Pod talk to?"                 │  │
                │  │  ┌──────────────────────────────────────────┐  │  │
                │  │  │  L3 CAPACITY                             │  │  │
                │  │  │  ResourceQuota, LimitRange, PriorityClass│  │  │
                │  │  │  → "how much can this tenant consume?"   │  │  │
                │  │  │  ┌────────────────────────────────────┐  │  │  │
                │  │  │  │  L2 AUTHORIZATION                  │  │  │  │
                │  │  │  │  RBAC + admission (Kyverno/VAP)    │  │  │  │
                │  │  │  │  → "who can the tenant act AS?"    │  │  │  │
                │  │  │  │  ┌──────────────────────────────┐  │  │  │  │
                │  │  │  │  │  L1 NAMING / SCOPE           │  │  │  │  │
                │  │  │  │  │  Namespace(s)                │  │  │  │  │
                │  │  │  │  │  → "where do tenant         │  │  │  │  │
                │  │  │  │  │     objects live?"           │  │  │  │  │
                │  │  │  │  └──────────────────────────────┘  │  │  │  │
                │  │  │  └────────────────────────────────────┘  │  │  │
                │  │  └──────────────────────────────────────────┘  │  │
                │  └────────────────────────────────────────────────┘  │
                └──────────────────────────────────────────────────────┘
```

| Layer | Mechanism | What goes wrong if missing |
|---|---|---|
| L1 Naming | `Namespace` (or set of them per tenant) | Tenants stomp on each other's names; impossible to scope anything below |
| L2 AuthZ | RBAC `Role`/`RoleBinding` + `ClusterRoleBinding` for shared bits; admission policy (Kyverno/Gatekeeper/VAP) for the things RBAC can't express | Tenants escalate to other namespaces; create privileged pods; bypass policy by renaming things |
| L3 Capacity | `ResourceQuota` (aggregate caps), `LimitRange` (per-container defaults/maxes), `PriorityClass` (relative scheduling priority) | One tenant starves the cluster; no fairness; eviction lottery |
| L4 Network | `NetworkPolicy` (default-deny per ns), `AdminNetworkPolicy`/`BaselineAdminNetworkPolicy` (cluster-default), egress gateways for source-IP attribution | Lateral movement is trivial; tenant A reaches tenant B's internal API |
| L5 Workload | Pod Security Admission `restricted`, Kyverno/Gatekeeper constraint policies, `RuntimeClass` (gVisor/Kata for hostile workloads) | Privileged pod, hostPath, hostNetwork — game over |

The chapter is organized by these layers. §§6–10 walk through them one at a time at the "platform writes a tenant template" level. §§14–22 introduce systems (Capsule, vCluster) that *bundle* multiple layers into a single declarative object.

---

## 6. Soft Tenancy Layer 1: Naming and Namespaces

The simplest soft-tenancy model is **one namespace per team**, named after the team:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: team-payments
  labels:
    # tenant identity — every other policy keys on this label
    tenant: team-payments
    # PSA enforcement: pods that don't meet 'restricted' are rejected
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: latest
    # audit + warn at the same level (informational; not enforcement)
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
    # cost-attribution label propagated to every workload by Kyverno (§10)
    cost-center: "1234"
  annotations:
    # who owns this namespace; surfaced in dashboards
    owner: payments-team@example.com
    # link to the team's runbook
    runbook: "https://wiki.example.com/teams/payments"
```

Several patterns are at work in those 14 lines:

- **The `tenant:` label is the primary key for every later policy.** Every NetworkPolicy, every Kyverno policy, every monitoring dashboard, every cost query joins on this label. Without it, you cannot distinguish tenants programmatically.
- **PSA labels are baked into the namespace at creation.** Letting a tenant modify those labels (RBAC verb `update` on `namespaces/status` or `namespaces`) is a privilege escalation: they can downgrade to `privileged` and run hostPath pods. Tenants get `get` on the namespace, not `update`. Platform-only.
- **Cost-center is on the namespace** because admission will propagate it (§10). Putting it on individual workloads doesn't scale — half of them won't have it.

For teams that need *multiple* namespaces (e.g., one per environment within their team), the patterns are:

| Pattern | Pros | Cons |
|---|---|---|
| Prefix convention (`team-payments-dev`, `team-payments-staging`, …) | Simple, no operator | RBAC has to bind to each one; quota is per-namespace, not per-tenant; no inheritance |
| HNC subnamespaces (§13) | RBAC, ConfigMaps, NetworkPolicies propagate down from `team-payments` to its children | Requires HNC operator; one extra moving part |
| Capsule Tenant (§14) | Tenant CR declares allowed namespace count, quota across all of them, owner | Requires Capsule operator |
| vCluster (§16) | Tenants create their own namespaces inside the vCluster; host sees only one | Heaviest option |

The choice is downstream of *how many namespaces a tenant needs*. One team, one namespace: prefix convention is fine. One team, ten namespaces (per service, per env): HNC or Capsule. Tenant needs cluster-scoped resources (CRDs) too: vCluster.

---

## 7. Soft Tenancy Layer 2: RBAC Bound to Groups

The single most common multi-tenancy bug is RBAC bound directly to user names. People leave teams; bindings rot; the "removed" engineer still has access through a forgotten `RoleBinding`. The fix is to bind to **groups**, where group membership comes from the IdP (OIDC, SAML, your cloud's IAM), and the cluster never knows individual usernames.

A working pattern for a team that owns its namespace:

```yaml
# A namespaced Role: scoped to team-payments, allows the full set of
# day-to-day verbs a developer needs WITHIN their namespace.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  namespace: team-payments
  name: tenant-developer
rules:
  # core workload objects
  - apiGroups: [""]
    resources: ["pods", "pods/log", "pods/exec", "pods/portforward",
                "services", "configmaps", "secrets",
                "persistentvolumeclaims", "events"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets", "statefulsets", "daemonsets"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["batch"]
    resources: ["jobs", "cronjobs"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["autoscaling"]
    resources: ["horizontalpodautoscalers"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["networking.k8s.io"]
    # tenants may create their own NetworkPolicies (additive to default-deny)
    resources: ["networkpolicies", "ingresses"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["gateway.networking.k8s.io"]
    resources: ["httproutes", "grpcroutes", "tlsroutes"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  # READ-ONLY on the namespace itself (and quota/limitrange/PSA labels)
  - apiGroups: [""]
    resources: ["namespaces"]
    resourceNames: ["team-payments"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["resourcequotas", "limitranges"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  namespace: team-payments
  name: tenant-developer-binding
subjects:
  - kind: Group
    name: "team-payments-developers"      # from OIDC `groups` claim
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: Role
  name: tenant-developer
  apiGroup: rbac.authorization.k8s.io
```

Note what is *missing* from `tenant-developer`:

- **No verbs on `resourcequotas`, `limitranges`, `networkpolicies` admin.** Wait — there *is* `networkpolicies` here. That's deliberate: tenants need to write *additional* NetworkPolicies for their own east-west rules. But the namespace already has a platform-managed default-deny that the tenant cannot delete (§9). The deny is implemented at the policy-engine layer (Kyverno blocks `kubectl delete np default-deny` by namespace selector), not via RBAC, because RBAC can't say "you can delete some NetworkPolicies but not others by name." See §10.
- **No verbs on `serviceaccounts`/`rolebindings`/`roles`.** If tenants could create RoleBindings, they could bind their own SA to `system:masters` via a ClusterRoleBinding (oh wait, that requires cluster-scope; but they could still create per-namespace RoleBindings that bind ClusterRole `cluster-admin` to themselves — RBAC's privilege-escalation rule (`bind` verb) catches this, but only if the binder doesn't already have those rights). The safe default is to deny — tenants can request additional SAs via a platform-managed CR or PR.
- **No verbs on `nodes`, `persistentvolumes`, `storageclasses`, `clusterroles`, `clusterrolebindings`, `customresourcedefinitions`.** These are cluster-scoped and don't belong to a tenant.
- **No `escalate` or `bind` verbs.** RBAC's privilege-escalation protection forbids creating a Role with more permissions than the binder has, *unless* the binder has `escalate`. Tenants must never have `escalate` or `bind` on `roles`/`clusterroles`.

A second Role for a tenant *admin* (the team lead) typically adds `roles`, `rolebindings`, and `serviceaccounts` (so the team can manage its own developer subgroups), but still no cluster-scoped verbs. Bind to a `team-payments-admins` group.

### 7.1 The kubelet-impersonation trap

A subtle RBAC pitfall: the `Node` authorizer (chapter 07) grants the kubelet broad permissions on Secrets, ConfigMaps, and pod-scoped objects *for pods running on the same node*. A pod that obtains the node's kubelet credentials (e.g., via `hostPath` mounting `/var/lib/kubelet`) inherits that authority. PSA `restricted` (which blocks `hostPath`) plus the `NodeRestriction` admission plugin (which forbids the kubelet from acting outside its node) are the two safety nets. Tenants must not be able to create pods that mount `/var/lib/kubelet`, which means PSA `restricted` is non-negotiable.

---

## 8. Soft Tenancy Layer 3: Capacity (Quota, LimitRange, PriorityClass)

Capacity is the second most common multi-tenancy failure (after RBAC). One tenant creates a 100-replica Deployment with `requests.memory: 100Gi` each, and either the scheduler queues every other tenant's pods forever or the autoscaler buys the team a $40,000 unscheduled bill. The fix is in three layers: `ResourceQuota` caps the aggregate, `LimitRange` supplies defaults so developers can't escape by *omitting* requests, and `PriorityClass` decides who survives if the cluster gets squeezed.

### 8.1 ResourceQuota per namespace

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  namespace: team-payments
  name: tenant-quota
spec:
  hard:
    # Capacity caps. Tenant cannot place workloads that, in aggregate,
    # request more than these. Limits give a ceiling on burst capacity.
    requests.cpu: "200"
    requests.memory: "400Gi"
    limits.cpu: "400"
    limits.memory: "800Gi"
    # Storage: tenant gets 10 TiB of provisioned PV claims, plus per-class
    # caps so they can't claim 10 TiB of expensive io2.
    requests.storage: "10Ti"
    fast-ssd.storageclass.storage.k8s.io/requests.storage: "1Ti"
    standard.storageclass.storage.k8s.io/requests.storage: "9Ti"
    persistentvolumeclaims: "100"
    # Object-count caps: protect etcd from a tenant creating
    # a million ConfigMaps in a runaway loop.
    count/deployments.apps: "100"
    count/statefulsets.apps: "20"
    count/jobs.batch: "200"
    count/cronjobs.batch: "50"
    count/services: "100"
    count/configmaps: "500"
    count/secrets: "500"
    services.loadbalancers: "5"     # cloud LB cost cap
    services.nodeports: "0"         # disallow NodePort entirely
    # GPU caps (extended resource)
    requests.nvidia.com/gpu: "8"
    # Pod count cap (the brute-force backstop)
    pods: "500"
```

A few non-obvious points:

- **Both `requests` and `limits` are quota'd.** Quota on requests protects scheduling fairness; quota on limits protects against memory overcommit blowing up the node. Doing only requests is the common mistake — tenant sets `limits: 10x requests` and consumes the node when uncontended.
- **Per-StorageClass storage caps.** The general `requests.storage` cap doesn't differentiate between $0.10/GB-month standard and $1.25/GB-month io2. Use `<storageclass-name>.storageclass.storage.k8s.io/requests.storage` to cap per class.
- **`services.nodeports: 0`.** NodePorts are a cluster-wide resource (port number) and a security hole (anyone on the network can reach them). Tenants should use Service `LoadBalancer` (with the cloud LB cap above) or Gateway/Ingress; never `NodePort`.
- **`count/<resource>.<group>` for any resource you care about.** This is the only way to prevent etcd bloat from a tenant creating millions of CronJobs or Secrets.
- **`requests.nvidia.com/gpu`** — the syntax for extended resources. The kubelet advertises the device, the scheduler tracks the request, and quota does the rest.

### 8.2 ResourceQuota scopes: per-PriorityClass quota

A single `ResourceQuota` puts every pod in the namespace into one bucket. That's fine until the tenant has both "best-effort batch" jobs and "must-not-be-evicted" production replicas. The mistake is letting batch jobs eat into the production budget. The fix is the `scopeSelector` field: separate quotas keyed by `priorityClassName`, `BestEffort`, `NotTerminating`, etc.

```yaml
# Quota that applies ONLY to pods with priorityClassName=tenant-critical
apiVersion: v1
kind: ResourceQuota
metadata:
  namespace: team-payments
  name: tenant-quota-critical
spec:
  hard:
    requests.cpu: "150"
    requests.memory: "300Gi"
    pods: "200"
  scopeSelector:
    matchExpressions:
      - scopeName: PriorityClass
        operator: In
        values: ["tenant-critical"]
---
# Quota that applies ONLY to pods with priorityClassName=tenant-best-effort
apiVersion: v1
kind: ResourceQuota
metadata:
  namespace: team-payments
  name: tenant-quota-best-effort
spec:
  hard:
    requests.cpu: "50"
    requests.memory: "100Gi"
    pods: "300"
  scopeSelector:
    matchExpressions:
      - scopeName: PriorityClass
        operator: In
        values: ["tenant-best-effort"]
```

Two quotas, one namespace, two budgets. The same trick separates `Terminating` (Jobs with `activeDeadlineSeconds`) from `NotTerminating` (Deployments) so a batch surge doesn't eat the always-on budget.

Available scope names: `Terminating`, `NotTerminating`, `BestEffort`, `NotBestEffort`, `PriorityClass`, `CrossNamespacePodAffinity`. The PriorityClass scope is the most useful for multi-tenancy.

### 8.3 LimitRange: defaults and ceilings per container

`ResourceQuota` caps the aggregate; `LimitRange` shapes individual objects. The two combine to make "a developer who forgets to set requests" non-fatal.

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  namespace: team-payments
  name: tenant-limits
spec:
  limits:
    # Container-level defaults: applied if the developer omits the field
    - type: Container
      default:                # default LIMITS for containers without limits
        cpu: "500m"
        memory: "512Mi"
        ephemeral-storage: "2Gi"
      defaultRequest:         # default REQUESTS for containers without
        cpu: "100m"
        memory: "128Mi"
        ephemeral-storage: "200Mi"
      max:                    # maximum any single container may set
        cpu: "8"
        memory: "32Gi"
        ephemeral-storage: "100Gi"
      min:                    # minimum (rejects "cpu: 1m" pathological cases)
        cpu: "10m"
        memory: "32Mi"
      maxLimitRequestRatio:   # ratio cap: limits cannot exceed requests by >4x
        cpu: "4"
        memory: "2"
    # Pod-level cap: aggregate of all containers in a single pod
    - type: Pod
      max:
        cpu: "16"
        memory: "64Gi"
    # PVC-level cap: no single PVC over 1 TiB
    - type: PersistentVolumeClaim
      max:
        storage: "1Ti"
      min:
        storage: "1Gi"
```

The `maxLimitRequestRatio` is the unsung hero. Without it, developers set tiny requests and huge limits ("just in case"), the scheduler over-packs the node, the kernel evicts. With `memory: "2"`, a developer who sets `requests.memory: 128Mi` cannot set `limits.memory` above 256Mi — they have to actually justify the burst by raising the request.

### 8.4 PriorityClass tiers

`PriorityClass` is cluster-scoped (only the platform team creates them). The classes are how the cluster picks who dies first when capacity is short.

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: platform-critical
value: 1000000000
globalDefault: false
description: "Platform components (CoreDNS, ingress, monitoring). Cannot be set by tenants."
preemptionPolicy: PreemptLowerPriority
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: tenant-critical
value: 1000
globalDefault: false
description: "Tenant production workloads. May preempt best-effort."
preemptionPolicy: PreemptLowerPriority
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: tenant-default
value: 100
globalDefault: true             # one default per cluster
description: "Default tenant workload priority."
preemptionPolicy: PreemptLowerPriority
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: tenant-best-effort
value: 10
globalDefault: false
description: "Tenant batch / best-effort. First to be preempted."
preemptionPolicy: Never           # this one cannot preempt anybody
```

The key rule: **tenants must NOT be able to use `system-cluster-critical` (2,000,000,000) or `system-node-critical` (2,000,001,000) or `platform-critical`.** Those are reserved for platform components. The standard way to enforce this is a `PriorityClass`-restricting admission policy (Kyverno or VAP) that rejects tenant pods using priorities above some threshold or with names not in an allowlist:

```yaml
# Kyverno policy fragment
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: restrict-priorityclass
spec:
  validationFailureAction: Enforce
  rules:
    - name: tenant-priority-allowlist
      match:
        any:
        - resources:
            kinds: ["Pod"]
            namespaces: ["team-*"]    # all tenant namespaces
      validate:
        message: "Tenant pods must use tenant-* PriorityClass"
        pattern:
          spec:
            priorityClassName: "tenant-*"
```

Without this, a tenant copies `system-cluster-critical` from somebody's StackOverflow answer and now their unimportant Deployment preempts CoreDNS.

---

## 9. Soft Tenancy Layer 4: Default-Deny Network

Default Kubernetes networking is **allow-all**. Every Pod can reach every other Pod on every port. For multi-tenancy this is a disaster: tenant A's pod can `curl http://team-payments-vault:8200/v1/secret/...` and exfiltrate.

The minimum bar is one `NetworkPolicy` per namespace that denies all ingress and egress except what the tenant explicitly opens:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  namespace: team-payments
  name: default-deny-all
spec:
  podSelector: {}             # matches all pods in this namespace
  policyTypes: ["Ingress", "Egress"]
  # no ingress: rules → deny all ingress
  # no egress:  rules → deny all egress
```

That blocks even DNS and kube-apiserver. Tenants need at least these baseline egresses:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  namespace: team-payments
  name: allow-egress-platform
spec:
  podSelector: {}
  policyTypes: ["Egress"]
  egress:
    # DNS to kube-dns / CoreDNS in kube-system
    - to:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: kube-system
        podSelector:
          matchLabels:
            k8s-app: kube-dns
      ports:
      - protocol: UDP
        port: 53
      - protocol: TCP
        port: 53
    # apiserver via the in-cluster Service (kubernetes.default)
    - to:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: default
        podSelector:
          matchLabels:
            component: apiserver
      ports:
      - protocol: TCP
        port: 443
    # in-namespace: pods may talk to each other
    - to:
      - podSelector: {}
```

And then the tenant *adds* their own NetworkPolicies for "my frontend may call my backend," "my backend may call the platform-postgres-operator in `pg-system`," etc. The platform-team default-deny is *not* deletable by the tenant (Kyverno blocks DELETE on policies whose name matches `default-deny-*` and whose namespace matches `team-*`).

### 9.1 AdminNetworkPolicy and BaselineAdminNetworkPolicy

`NetworkPolicy` is namespace-scoped and additive. It cannot express "across the whole cluster, deny tenant-X from reaching tenant-Y." `AdminNetworkPolicy` (ANP) and `BaselineAdminNetworkPolicy` (BANP), both cluster-scoped and authored only by the platform team, fill that gap.

```yaml
apiVersion: policy.networking.k8s.io/v1alpha1
kind: AdminNetworkPolicy
metadata:
  name: tenant-isolation
spec:
  priority: 10
  subject:
    namespaces:
      matchLabels:
        tenant: ""                # any namespace with a tenant label
  ingress:
    # Allow ingress from same-tenant namespaces
    - action: Allow
      from:
      - namespaces:
          sameLabels: ["tenant"]
    # Allow from platform namespaces explicitly
    - action: Allow
      from:
      - namespaces:
          matchLabels:
            tier: platform
    # Default-deny everything else cluster-wide
    - action: Deny
      from:
      - namespaces:
          notSameLabels: ["tenant"]
```

ANP/BANP requires a CNI that implements it (Calico, Cilium recent versions, Antrea). The advantage over per-namespace NetworkPolicies is that ANP is *authoritative* and *not deletable by tenants*. Tenants who could write a NetworkPolicy `allow-from-anywhere` cannot override an ANP `Deny`.

### 9.2 Egress identity

If the platform has external IP allowlists (a partner API will only accept traffic from specific IPs), every tenant on a shared cluster sees the same node IPs as their source. Two solutions:

- **Calico egress gateways** — a pool of pods in a dedicated namespace whose IPs are allowlisted; tenants annotate workloads to route egress through that pool.
- **Cilium EgressGatewayPolicy** — same shape, eBPF datapath. Selects pods by label and a destination CIDR, routes via a designated egress node.

Per-tenant egress IPs lets the partner enforce per-tenant allowlists and lets the platform attribute external traffic correctly. Without it, "tenant X is hammering the rate-limit at api.partner.com" turns into "the whole cluster is, and we can't tell which tenant."

---

## 10. Soft Tenancy Layer 5: Workload Policy (PSA, Kyverno, VAP)

Workload policy is the layer that says "no, you may not create a pod that mounts `/`." Pod Security Admission is the in-tree minimum; Kyverno / Gatekeeper / ValidatingAdmissionPolicy (CEL) extend it.

### 10.1 Pod Security Admission (PSA)

PSA enforces one of three profiles per namespace, set via labels:

- `privileged` — anything goes (system namespaces only)
- `baseline` — no known privilege escalation (no hostPath, no hostNetwork, no privileged: true, no hostPID/IPC, no CAP_SYS_ADMIN, no procMount=Unmasked, etc.)
- `restricted` — the hardened profile: must run as non-root, must drop `ALL` capabilities, must set `allowPrivilegeEscalation: false`, must use a seccomp profile (`RuntimeDefault` or `Localhost`), must set `readOnlyRootFilesystem` is recommended (and required in pod-security strict modes), no Volume types beyond a safe list.

Tenant namespaces should always be `restricted`. The label was shown in §6. The `audit` and `warn` labels (also set to `restricted`) record violations to audit log and surface them on `kubectl apply` even if `enforce` is at a lower level — useful during migration.

PSA is built into the apiserver (chapter 06) and has zero runtime overhead. It is the floor. Everything below is the ceiling.

### 10.2 Kyverno: declarative policy with mutation, validation, generation

PSA cannot express most multi-tenancy rules. Examples:

- Enforce the `tenant` label on every Pod (so cost attribution works).
- Block pods from pulling from registries other than `ghcr.io/example-org/*`.
- Disallow `:latest` tags.
- Require a `topologySpreadConstraints` for Deployments with `replicas > 1`.
- Generate a default NetworkPolicy when a new namespace is created.
- Mutate annotations onto every pod (e.g., inject the cost-center from the namespace).

Kyverno (kyverno/kyverno) does all of these as `ClusterPolicy` and `Policy` resources. Two examples:

```yaml
# Mutate: every Pod created in any tenant namespace gets a cost-center
# annotation, propagated from its Namespace's label.
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: propagate-cost-center
spec:
  mutateExistingOnPolicyUpdate: false
  rules:
    - name: copy-cost-center
      match:
        any:
        - resources:
            kinds: ["Pod"]
      context:
      - name: namespaceCostCenter
        apiCall:
          urlPath: "/api/v1/namespaces/{{request.namespace}}"
          jmesPath: "metadata.labels.\"cost-center\""
      mutate:
        patchStrategicMerge:
          metadata:
            labels:
              cost-center: "{{namespaceCostCenter}}"
---
# Validate: tenants may NOT delete the platform-managed default-deny
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: protect-default-deny
spec:
  validationFailureAction: Enforce
  background: false
  rules:
    - name: no-delete-default-deny
      match:
        any:
        - resources:
            kinds: ["NetworkPolicy"]
            names: ["default-deny-*"]
            namespaces: ["team-*"]
      preconditions:
        all:
        - key: "{{request.operation}}"
          operator: Equals
          value: DELETE
        - key: "{{request.userInfo.groups | contains(@, 'system:masters')}}"
          operator: NotEquals
          value: true
      validate:
        message: "Platform-managed default-deny NetworkPolicies cannot be deleted by tenants"
        deny: {}
```

Equivalent rules exist in Gatekeeper (Rego policies via OPA constraint templates) and increasingly in `ValidatingAdmissionPolicy` (in-tree, CEL, lower latency, no webhook hop — chapter 06).

### 10.3 The "platform writes policy for tenants" pattern

The mental model is: every soft-multi-tenant cluster has a *policy bundle* maintained by the platform team. It includes:

- A Kyverno/Gatekeeper/VAP policy set enforcing the rules tenants cannot enforce on themselves (image registry, label propagation, default NetworkPolicy generation, blocking deletion of platform objects, blocking unsafe priorities).
- A *generation* policy that, when a new namespace appears with `tenant=*`, creates the default-deny NetworkPolicy, the per-tenant ResourceQuota, and the LimitRange. Tenants then don't have to remember to set them up — the namespace exists and the controls are automatic.
- A *mutation* policy that injects labels (cost-center, tenant id) and annotations (owner, runbook link) onto every workload.

This is the inversion that makes soft tenancy survive: **the platform team writes the rules; tenant teams don't have to remember anything.**

```yaml
# Kyverno generate: on namespace creation with tenant=*, create the
# default-deny NetworkPolicy automatically.
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: generate-default-deny
spec:
  rules:
    - name: gen-deny-on-ns-create
      match:
        any:
        - resources:
            kinds: ["Namespace"]
            selector:
              matchExpressions:
              - key: tenant
                operator: Exists
      generate:
        kind: NetworkPolicy
        apiVersion: networking.k8s.io/v1
        name: default-deny-all
        namespace: "{{request.object.metadata.name}}"
        synchronize: true              # delete the policy if NS labels change
        data:
          spec:
            podSelector: {}
            policyTypes: ["Ingress", "Egress"]
```

`synchronize: true` makes the generated NetworkPolicy a *reconciled* resource: if a tenant deletes it (perhaps with elevated rights), Kyverno re-creates it on the next reconcile loop. This is how the platform keeps invariants.

---

## 11. Cross-Namespace References: ReferenceGrant

A persistent multi-tenancy headache: some objects in one namespace want to *reference* objects in another. Examples:

- A `Gateway` in the platform-managed `ingress-system` namespace wants to route to a `Service` in `team-payments`.
- An `HTTPRoute` in `team-payments` wants to attach to a `Gateway` in `ingress-system`.
- A `TLSRoute` references a `Secret` containing a cert in another namespace.

The default for cross-namespace references in Gateway API is **deny**. The mechanism to grant a specific cross-namespace reference is `ReferenceGrant`:

```yaml
# In team-payments: explicitly grant ingress-system the right to route
# HTTPRoute attachments INTO this namespace's Services.
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  namespace: team-payments
  name: allow-ingress-system-routes
spec:
  from:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      namespace: ingress-system
  to:
    - group: ""
      kind: Service
      # could also list specific service names
```

The shape that matters for multi-tenancy is **tenant-owned routes attaching to a shared Gateway**:

```
        ┌────────────────────────────────────────────────┐
        │  ingress-system  (platform-owned)              │
        │   Gateway "public-https"                       │
        │     listeners: 443 HTTPS *.example.com         │
        │     allowedRoutes:                             │
        │       namespaces:                              │
        │         from: Selector                         │
        │         selector:                              │
        │           matchLabels:                         │
        │             tenant: ""                         │
        └────────────────────────────────────────────────┘
                          ▲
                          │  attach via parentRef
                          │
        ┌─────────────────┴───────────────────────────────┐
        │  team-payments  (tenant-owned)                 │
        │   HTTPRoute "payments-api"                     │
        │     parentRefs:                                │
        │       - name: public-https                     │
        │         namespace: ingress-system              │
        │     hostnames: ["payments.example.com"]        │
        │     rules:                                     │
        │       backendRefs:                             │
        │         - name: payments-svc                   │
        │           port: 8080                           │
        └─────────────────────────────────────────────────┘
```

The platform team's Gateway specifies that any namespace labeled `tenant` may attach HTTPRoutes. The tenant writes the route in their own namespace, points at the platform Gateway by `parentRef`, and the Gateway controller (Envoy/Cilium/whatever) configures the dataplane. This is *the* shape for shared ingress on a multi-tenant cluster: one Gateway, many tenant routes, no shared YAML to edit per tenant onboarding.

`ReferenceGrant` exists in Gateway API for the same reason cross-namespace volume references don't exist: defaulting cross-namespace access to allow is a tenancy hole.

---

## 12. The Per-Tenant Bundle: A Single YAML That Onboards a Team

Pulling §§6–10 together, the onboarding artifact for one tenant looks like this — generated from a template, applied via GitOps:

```yaml
# tenant-payments.yaml — the complete soft-tenancy bundle for one team.
apiVersion: v1
kind: Namespace
metadata:
  name: team-payments
  labels:
    tenant: team-payments
    cost-center: "1234"
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: latest
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
  annotations:
    owner: payments-team@example.com
---
# ResourceQuota (split into critical / best-effort by scope)
apiVersion: v1
kind: ResourceQuota
metadata:
  namespace: team-payments
  name: tenant-quota-critical
spec:
  hard:
    requests.cpu: "150"
    requests.memory: "300Gi"
    pods: "200"
  scopeSelector:
    matchExpressions:
      - scopeName: PriorityClass
        operator: In
        values: ["tenant-critical"]
---
apiVersion: v1
kind: ResourceQuota
metadata:
  namespace: team-payments
  name: tenant-quota-default
spec:
  hard:
    requests.cpu: "50"
    requests.memory: "100Gi"
    persistentvolumeclaims: "50"
    requests.storage: "5Ti"
    services.loadbalancers: "5"
    services.nodeports: "0"
    count/secrets: "200"
    count/configmaps: "200"
    pods: "300"
---
# LimitRange
apiVersion: v1
kind: LimitRange
metadata:
  namespace: team-payments
  name: tenant-limits
spec:
  limits:
    - type: Container
      default:
        cpu: "500m"
        memory: "512Mi"
      defaultRequest:
        cpu: "100m"
        memory: "128Mi"
      max:
        cpu: "8"
        memory: "32Gi"
      maxLimitRequestRatio:
        cpu: "4"
        memory: "2"
    - type: PersistentVolumeClaim
      max:
        storage: "1Ti"
      min:
        storage: "1Gi"
---
# Role (developer)
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  namespace: team-payments
  name: tenant-developer
rules:
  - apiGroups: [""]
    resources: ["pods","pods/log","pods/exec","pods/portforward",
                "services","configmaps","secrets",
                "persistentvolumeclaims","events"]
    verbs: ["get","list","watch","create","update","patch","delete"]
  - apiGroups: ["apps"]
    resources: ["deployments","replicasets","statefulsets","daemonsets"]
    verbs: ["*"]
  - apiGroups: ["batch"]
    resources: ["jobs","cronjobs"]
    verbs: ["*"]
  - apiGroups: ["autoscaling"]
    resources: ["horizontalpodautoscalers"]
    verbs: ["*"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["networkpolicies","ingresses"]
    verbs: ["*"]
  - apiGroups: ["gateway.networking.k8s.io"]
    resources: ["httproutes","grpcroutes"]
    verbs: ["*"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  namespace: team-payments
  name: tenant-developer-binding
subjects:
  - kind: Group
    name: team-payments-developers
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: Role
  name: tenant-developer
  apiGroup: rbac.authorization.k8s.io
---
# Default-deny NetworkPolicy
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  namespace: team-payments
  name: default-deny-all
  annotations:
    platform.example.com/managed: "true"
spec:
  podSelector: {}
  policyTypes: ["Ingress","Egress"]
---
# Baseline egress (DNS, apiserver, in-namespace)
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  namespace: team-payments
  name: allow-egress-platform
  annotations:
    platform.example.com/managed: "true"
spec:
  podSelector: {}
  policyTypes: ["Egress"]
  egress:
    - to:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: kube-system
        podSelector:
          matchLabels:
            k8s-app: kube-dns
      ports:
      - {protocol: UDP, port: 53}
      - {protocol: TCP, port: 53}
    - to:
      - podSelector: {}
```

Add a ReferenceGrant to attach to the platform Gateway, and the tenant is ready to deploy. Tenants creating their team-payments namespace via GitOps is one PR; offboarding is `kubectl delete ns team-payments`.

A platform team that has *not* templated this bundle is doing manual onboarding, which means every new team gets a slightly different policy stack, which means the policy stack drifts, which means audit failures. The bundle should be generated from a template language (Helm, kustomize, ytt, Jsonnet) that takes a single input: the tenant's name and IdP group.

---

## 13. Hierarchical Namespace Controller (HNC)

When a tenant grows beyond one namespace — say, one per environment, or one per service — they want their RBAC, labels, ConfigMaps, NetworkPolicies, and ResourceQuota *inherited* from a parent namespace, not re-declared in each. Hierarchical Namespace Controller (HNC), the multi-tenancy SIG project at `kubernetes-sigs/hierarchical-namespaces`, gives namespaces a parent/child relationship.

A tenant root namespace can have *subnamespaces*, declared by a `SubnamespaceAnchor` object:

```yaml
# Tenant root
apiVersion: v1
kind: Namespace
metadata:
  name: team-payments
---
# Anchor: tells HNC to create team-payments-prod as a child
apiVersion: hnc.x-k8s.io/v1alpha2
kind: SubnamespaceAnchor
metadata:
  namespace: team-payments
  name: team-payments-prod
---
apiVersion: hnc.x-k8s.io/v1alpha2
kind: SubnamespaceAnchor
metadata:
  namespace: team-payments
  name: team-payments-staging
---
apiVersion: hnc.x-k8s.io/v1alpha2
kind: SubnamespaceAnchor
metadata:
  namespace: team-payments
  name: team-payments-dev
```

HNC's controller observes the anchors, creates the child namespaces, and applies inheritance:

```
   team-payments  (parent)
     ├── RoleBinding "tenant-developer-binding"   ┐
     ├── NetworkPolicy "default-deny-all"          │
     ├── NetworkPolicy "allow-egress-platform"     │ propagated to all
     ├── ConfigMap "team-defaults"                 │ children by HNC
     └── Secret "internal-ca"                      ┘
       │
       ├── team-payments-prod  (child)
       │     └── (inherits all of the above; can add prod-specific objects)
       │
       ├── team-payments-staging  (child)
       │     └── (inherits)
       │
       └── team-payments-dev  (child)
             └── (inherits)
```

A few specifics:

- **Inheritance is opt-in per resource kind.** HNC has a `HNCConfiguration` that lists which Kinds propagate (default: RoleBinding, NetworkPolicy, Role, LimitRange; not Secret unless added). The platform team curates this list.
- **`Propagate` vs `Remove` modes.** A parent object marked with annotation `hnc.x-k8s.io/select=...` can be restricted to specific children.
- **Children inherit RBAC.** A tenant who has admin on the parent gets admin on all children — by design, because the parent is the tenant's "root."
- **ResourceQuota is special.** HNC has a `HierarchicalResourceQuota` (HRQ) CR that caps aggregate usage *across the whole subtree*. This is what makes HNC valuable for multi-namespace tenants: one quota across the team's prod+staging+dev.

```yaml
apiVersion: hnc.x-k8s.io/v1alpha2
kind: HierarchicalResourceQuota
metadata:
  namespace: team-payments
  name: total-tenant-quota
spec:
  hard:
    requests.cpu: "300"
    requests.memory: "600Gi"
    pods: "1000"
```

- **Subnamespaces are deletable only by deleting the anchor.** Tenants cannot `kubectl delete ns team-payments-prod` directly; they delete the SubnamespaceAnchor, and HNC handles cleanup. This prevents partial-deletion footguns.

When HNC fits: a team with ≤ 10 namespaces, all under one logical owner, that benefits from inheritance. When HNC doesn't fit: cross-team sharing (HNC inheritance is strictly hierarchical), or tenants who need cluster-scoped objects (HNC doesn't touch those — that's vCluster's job).

---

## 14. Capsule: Tenant-as-a-Resource

Capsule (`projectcapsule/capsule`, formerly `clastix/capsule`) takes a different angle: instead of namespaces and policies as the primary objects, the *Tenant* is a first-class CRD. The platform team writes one `Tenant` object per tenant; Capsule's controller manifests namespaces, RBAC, quotas, network policies, image policies, and admission constraints from that single source.

```yaml
apiVersion: capsule.clastix.io/v1beta2
kind: Tenant
metadata:
  name: team-payments
spec:
  # WHO owns this tenant (one or many)
  owners:
    - kind: Group
      name: team-payments-admins
    - kind: User
      name: payments-lead@example.com
  # How many namespaces the tenant may create
  namespaceOptions:
    quota: 10
    # Required labels on every tenant-owned namespace
    additionalMetadata:
      labels:
        tenant: team-payments
        cost-center: "1234"
        pod-security.kubernetes.io/enforce: restricted
  # Per-tenant resource quotas (applied to EVERY namespace the tenant creates)
  resourceQuotas:
    scope: Tenant            # aggregate across all tenant namespaces
    items:
      - hard:
          requests.cpu: "200"
          requests.memory: "400Gi"
          pods: "500"
          persistentvolumeclaims: "100"
  limitRanges:
    items:
      - limits:
          - type: Container
            default:
              cpu: "500m"
              memory: "512Mi"
            defaultRequest:
              cpu: "100m"
              memory: "128Mi"
            max:
              cpu: "8"
              memory: "32Gi"
  # Network policies applied to every tenant namespace
  networkPolicies:
    items:
      - podSelector: {}
        policyTypes: ["Ingress","Egress"]
        # default-deny baseline
  # Image registry allowlist (enforced via admission)
  imagePullPolicies: ["Always"]
  containerRegistries:
    allowed:
      - ghcr.io/example-org/*
      - registry.example.com/*
    allowedRegex: '^(ghcr\.io|registry\.example\.com)/.*'
  # StorageClass allowlist
  storageClasses:
    allowed: ["standard", "fast-ssd"]
  # IngressClass allowlist
  ingressClasses:
    allowed: ["nginx", "platform-envoy"]
  # PriorityClass allowlist
  priorityClasses:
    allowed: ["tenant-critical", "tenant-default", "tenant-best-effort"]
  # Node selector forced onto every tenant Pod (drives placement)
  nodeSelector:
    tenant-tier: shared
  # ServiceAccount restrictions (which SA may be referenced by Pods)
  serviceAccounts:
    allowed: []                  # tenants make their own; cluster SAs forbidden
```

The Capsule controller does several things from this one object:

- **Namespace creation by the tenant**: the tenant owners can `kubectl create ns team-payments-prod` (Capsule grants the RBAC to do this for their owned tenants only), and Capsule labels/annotates the namespace, applies the quota, the LimitRange, the NetworkPolicy, the admission webhook configuration.
- **Tenant-scoped quota**: the `scope: Tenant` setting means the quota is the *aggregate* across all the tenant's namespaces (similar to HNC's HierarchicalResourceQuota).
- **Admission enforcement**: Capsule has a webhook that rejects pods that pull from disallowed registries, use disallowed StorageClasses, etc. The tenant cannot override.
- **Tenant ownership transitively manifests as RBAC**: owners get cluster-scoped permission to create namespaces *labeled with their tenant*, plus full admin on those namespaces.

When Capsule wins: a platform team with many tenants, each needing multiple namespaces, where the desired policy shape is uniform. The Tenant CR is the single source of truth, the boilerplate is gone, the policy is enforced cluster-wide.

When Capsule loses (relative to vCluster): tenants who need their own CRDs, their own cluster-scoped resources, or "feels like a real cluster" admin powers. Capsule is namespace-tenancy made declarative; vCluster is virtualization.

Capsule pairs naturally with HNC (Capsule owns the *tenant*; HNC owns the *internal hierarchy of the tenant*) and with Kyverno (Capsule's enforcement is in-tree; complex policy goes to Kyverno).

---

## 15. Kiosk: The Predecessor

Kiosk (`kiosk-sh/kiosk`, by loft-sh) was an earlier project in the same space. Its model:

- An `Account` CR identifies a tenant.
- A `Space` CR represents a namespace owned by the account.
- Kiosk enforces space limits, quotas, and templates on the account.

```yaml
apiVersion: config.kiosk.sh/v1alpha1
kind: Account
metadata:
  name: team-payments
spec:
  subjects:
    - kind: Group
      name: team-payments-admins
  space:
    limit: 10
    clusterRole: kiosk-space-admin
    templateInstances:
      - spec:
          template: default-tenant-policies
---
apiVersion: tenancy.kiosk.sh/v1alpha1
kind: Space
metadata:
  name: team-payments-prod
spec:
  account: team-payments
```

Kiosk is largely historical now. Loft Labs (the company) shifted focus to vCluster, and Capsule emerged as the more actively developed declarative-tenant alternative. New designs in 2025+ should reach for Capsule (declarative tenants) or vCluster (virtualization); Kiosk remains in production at sites that adopted it early but is not the recommended path forward.

The lesson worth carrying from Kiosk to the present is the *Account + Space* split: tenant identity is one object, the tenant's namespaces are separate objects, the relationship is explicit. Capsule's `Tenant` + child namespaces is the same pattern, refined.

---

## 16. vCluster: A Virtual Control Plane Per Tenant

vCluster (`loft-sh/vcluster`) is the standout middle option between "shared cluster with namespaces" and "separate clusters." It runs an entire Kubernetes control plane — apiserver, controller-manager, optionally scheduler, virtual etcd or SQLite — *inside a host cluster namespace*. The tenant gets a `kubeconfig` that points to this virtual apiserver. From their perspective, they are cluster-admin of their own cluster. From the host cluster's perspective, the tenant's virtual cluster is just a pod (or a small pod-group) and some translated objects (Pods, Services, PVCs, ConfigMaps, Secrets) in the host namespace.

```
            HOST CLUSTER (one physical etcd, one set of nodes)
   ┌────────────────────────────────────────────────────────────────┐
   │                                                                │
   │   Host apiserver, host etcd, host scheduler, host controllers │
   │                                                                │
   │   ┌────────────────────────────────────────────────────────┐   │
   │   │  Host namespace: vcluster-team-payments                │   │
   │   │                                                        │   │
   │   │   ┌─────────────────────────────────┐                  │   │
   │   │   │  vCluster control plane pod     │                  │   │
   │   │   │  ┌─────────────────────────┐    │                  │   │
   │   │   │  │  virtual apiserver      │    │                  │   │
   │   │   │  │  virtual scheduler      │    │                  │   │
   │   │   │  │  virtual controllers    │    │                  │   │
   │   │   │  │  virtual etcd (or k3s   │    │                  │   │
   │   │   │  │   sqlite, default)      │    │                  │   │
   │   │   │  └─────────────────────────┘    │                  │   │
   │   │   │  ┌─────────────────────────┐    │                  │   │
   │   │   │  │  syncer  (a controller) │ ─► talks to BOTH      │   │
   │   │   │  │  translates objects     │    virtual and host   │   │
   │   │   │  │  vCluster ⇄ host        │    apiserver          │   │
   │   │   │  └─────────────────────────┘    │                  │   │
   │   │   └─────────────────────────────────┘                  │   │
   │   │                                                        │   │
   │   │   Translated objects (created by syncer):              │   │
   │   │     Pod   "frontend-7d5-x8z2k-x-default-x-vc"          │   │
   │   │           ← was "frontend-7d5-x8z2k" in "default"      │   │
   │   │     Service "api-x-default-x-vc"                       │   │
   │   │     PVC    "data-0-x-default-x-vc"                     │   │
   │   │     ConfigMap, Secret (selected; opt-in)               │   │
   │   │                                                        │   │
   │   └────────────────────────────────────────────────────────┘   │
   │                                                                │
   └────────────────────────────────────────────────────────────────┘

            TENANT VIEW (via vcluster kubeconfig)
   ┌────────────────────────────────────────────────────────────────┐
   │   $ kubectl get nodes                                           │
   │   NAME                                STATUS   AGE              │
   │   fake-node-a8d3.virtual-cluster      Ready    7d               │
   │                                                                │
   │   $ kubectl get ns                                              │
   │   default       Active   7d                                    │
   │   kube-system   Active   7d                                    │
   │   kube-public   Active   7d                                    │
   │                                                                │
   │   $ kubectl get crd                                            │
   │   (tenant's own CRDs only — NOT the host's CRDs unless         │
   │    explicitly synced)                                          │
   │                                                                │
   │   $ kubectl auth can-i '*' '*' --all-namespaces                │
   │   yes                  (tenant is real cluster-admin INSIDE)   │
   └────────────────────────────────────────────────────────────────┘
```

The tenant is the cluster-admin of a fake cluster. Their CRDs exist only in the virtual apiserver's storage. Their RoleBindings, ServiceAccounts, namespaces — all stored in the vCluster's virtual etcd/SQLite. They can run `kubectl get crd`, `kubectl get clusterroles`, `kubectl get nodes`, and see only what makes sense for their cluster.

But — and this is the load-bearing word — **the host kernel is still shared**. A privileged Pod inside the vCluster, when synced down to the host, runs on the host kernel like any other Pod. vCluster's isolation is at the *API surface* layer, not the *kernel* layer.

---

## 17. vCluster Architecture: The Syncer

The pieces of a vCluster, in detail:

1. **The vCluster control-plane pod** — typically a StatefulSet (one replica) in the host namespace. Inside the pod, by default:
    - A small Kubernetes API server (vCluster ships flavors: k3s — single binary with embedded SQLite; k0s; k8s — the upstream apiserver + controller-manager + scheduler).
    - A controller-manager (built-in controllers for Deployments, ReplicaSets, etc.).
    - A scheduler (in some configurations the host scheduler is used, in others a virtual scheduler runs in the vCluster pod).
    - Virtual etcd (or, default, SQLite as a single file on a PVC, because etcd is overkill for one-tenant scale and SQLite is dramatically cheaper).

2. **The syncer** — also in the same pod (or a sibling) — is the controller that bridges the two apiservers. It:
    - Watches Pods in the virtual apiserver. When a Pod appears in `default` namespace inside the vCluster, the syncer creates a corresponding Pod in the host namespace (`vcluster-team-payments`), with a translated name (`<pod>-x-<vns>-x-<vcluster-name>`), and writes the *real* Pod spec to the host. The host scheduler schedules it; the host kubelet runs it.
    - Watches the host Pod's status. When the host Pod becomes Running, the syncer copies status back into the virtual Pod. The tenant sees their Pod as Running.
    - Performs the same translation for Services, Endpoints, PVCs, ConfigMaps, Secrets, Events.
    - Translates Service IPs: the virtual cluster's ClusterIP range and the host's overlap only by configuration; the syncer maps between them.

3. **A kubeconfig for the tenant** — points at the vCluster's apiserver, which is exposed inside the host either via a Service (`ClusterIP` for in-cluster tenants, `LoadBalancer` or NodePort for external) or via `vcluster connect` (a port-forward).

4. **Optional: a "fake node"** — the vCluster's virtual apiserver lies to the tenant about Nodes. By default, vCluster shows one synthetic Node whose name and status are constructed; the tenant cannot see the real host nodes. This is essential because the host nodes' names, labels, and taints are the platform team's concern, not the tenant's.

### 17.1 The translation in detail

A Pod created in the vCluster:

```yaml
# Tenant's view (vCluster apiserver)
apiVersion: v1
kind: Pod
metadata:
  namespace: default
  name: frontend-7d5b8d4f8d-x8z2k
spec:
  containers:
    - name: app
      image: ghcr.io/example-org/frontend:1.2.3
  volumes:
    - name: config
      configMap:
        name: app-config
```

What the syncer creates on the host (`vcluster-team-payments` namespace):

```yaml
# Host's view (host apiserver)
apiVersion: v1
kind: Pod
metadata:
  namespace: vcluster-team-payments
  name: frontend-7d5b8d4f8d-x8z2k-x-default-x-vc-team-payments
  labels:
    vcluster.loft.sh/managed-by: vc-team-payments
    vcluster.loft.sh/namespace: default
  annotations:
    vcluster.loft.sh/object-name: frontend-7d5b8d4f8d-x8z2k
    vcluster.loft.sh/object-uid: <virtual-uid>
spec:
  containers:
    - name: app
      image: ghcr.io/example-org/frontend:1.2.3
  volumes:
    - name: config
      configMap:
        name: app-config-x-default-x-vc-team-payments     # translated
```

Note the name suffix `-x-<vns>-x-vc-<vcluster-name>`. That's the convention — a substring unlikely to collide with real names. ConfigMap and Secret names are translated likewise *when referenced by a synced Pod*; otherwise ConfigMaps in the vCluster stay only in the vCluster's etcd.

### 17.2 The scheduling story

By default, vCluster uses the **host scheduler**. The tenant's "scheduler" inside the vCluster doesn't actually schedule — it just marks Pods as scheduled on the fake Node, and the syncer creates a Pod with no `nodeName`, which the *host* scheduler then binds.

This is the right default because: (a) the host scheduler already knows about real Nodes, real capacity, real affinity/anti-affinity at the host level; (b) tenants writing their own scheduler plugins would have to plug into the host's reality. But it does mean: tenants cannot meaningfully change scheduler plugins inside their vCluster; that knob is on the host.

Optionally, the tenant can run their own scheduler *inside* the vCluster, and the syncer respects the resulting nodeName. Used by teams that build batch schedulers (Volcano-style) per-tenant.

### 17.3 Networking

The synced Pods run on the host's CNI. The vCluster doesn't have its own CNI. Service IPs in the vCluster are translated to host Service IPs by the syncer; DNS inside the vCluster is served by a small CoreDNS the vCluster manages. The tenant sees their Services as ClusterIPs in their own range; the syncer maps them to host Services in the `vcluster-team-payments` namespace, which the host CoreDNS resolves.

For tenant-to-tenant network isolation, the host's NetworkPolicy (or ANP) still applies: the host namespace `vcluster-team-payments` is just another namespace from the host CNI's point of view.

---

## 18. vCluster Sync Modes and Object Translation

vCluster's `values.yaml` (Helm chart input) controls which Kinds are synced "from virtual to host" (for the workload to actually run) versus which exist only in the virtual cluster (because the host doesn't need to know).

```yaml
# vcluster.yaml (Helm values for vCluster v0.20+)
controlPlane:
  distro:
    k3s:
      enabled: true
  backingStore:
    etcd:
      embedded:
        enabled: false           # default: use sqlite, much cheaper
    database:
      embedded:
        enabled: true
        # sqlite file persisted on a PVC
  statefulSet:
    persistence:
      volumeClaim:
        size: 5Gi
  proxy:
    extraSANs:
      - vcluster-team-payments.example.com
sync:
  toHost:
    pods:                       enabled: true
    services:                   enabled: true
    endpoints:                  enabled: true
    persistentVolumeClaims:     enabled: true
    configMaps:
      enabled: true
      all: false               # default: only sync CMs referenced by synced Pods
    secrets:
      enabled: true
      all: false               # same: only sync Secrets referenced
    ingresses:                  enabled: true
    serviceAccounts:            enabled: false   # tenant SAs stay virtual
    networkPolicies:            enabled: true
    priorityClasses:            enabled: false   # tenants use host PCs
    poddisruptionbudgets:       enabled: true
  fromHost:
    nodes:
      enabled: true
      selector:
        labels:
          tenant: team-payments   # only show nodes labeled for this tenant
    storageClasses:
      enabled: true
      selector:
        labels:
          shared: "true"          # only show "shared" StorageClasses
    ingressClasses:
      enabled: true
  customResourceDefinitions:
    cert-manager.io.v1.Certificate:
      enabled: true               # opt-in CRD sync
networking:
  advanced:
    clusterDomain: cluster.local
  resolveDNS:
    - hostname: api.example.com
      service: gateway-system/example-gateway
exportKubeConfig:
  context: vcluster-team-payments
  service:
    enabled: true
    type: ClusterIP
plugin:
  generic:
    enabled: false
```

The directionality matters:

- **`sync.toHost.*`** — what virtual objects get materialized as host objects. Pods must always be synced (otherwise nothing runs). ConfigMaps and Secrets are synced *only when referenced by a synced Pod* by default (the `all: false` setting), which keeps the host namespace from bloating with every CM the tenant created.
- **`sync.fromHost.*`** — what host objects appear in the virtual cluster's view. Nodes are filtered by selector (the tenant sees only "their" nodes, even though they're shared). StorageClasses are filtered (tenants see only the classes the platform exposes).
- **CRD sync** — opt-in by GVK. If the host has `cert-manager.io/v1/Certificate` and the platform wants tenants to be able to request certs, the CRD is added to `fromHost`. Without this, the tenant's `Certificate` resources stay in the virtual cluster and never reach the platform-side cert-manager.

### 18.1 The CRD-version-conflict story (the killer feature)

Here is the case vCluster solves better than any of the previous options:

- Tenant A needs `redis.example.com/v1` CRD (their operator).
- Tenant B needs `redis.example.com/v2` CRD (their operator, breaking change).
- These CRDs are cluster-scoped — you cannot have both versions installed on the same host cluster.

With namespace tenancy (HNC, Capsule), one of the tenants loses. With vCluster, each tenant's vCluster has its own CRD registry; the host cluster has no `redis.example.com` CRD at all. The host doesn't care about the API; the tenant runs their own operator inside the vCluster, and the operator's reconcile loop talks to the vCluster's apiserver. The operator's Pods are synced to the host; the CRs are not.

The same logic covers tenants who need their own admission webhooks, their own RBAC ClusterRoles, their own apiserver feature gates (within reason — the apiserver in the vCluster pod can run with different flags than the host).

### 18.2 The cost of running a vCluster

Per vCluster (k3s flavor, SQLite, no HA):

- ~150–300 MiB memory (apiserver + controller-manager + sqlite + syncer)
- ~0.1–0.5 CPU sustained, more on bursts (large list+watch)
- One PVC for SQLite (5 Gi typical)
- One Service (ClusterIP or LoadBalancer)

For 100 tenants on one host cluster: ~15–30 GiB RAM, ~10–50 CPU just for control planes. Cheaper than 100 separate EKS clusters by an order of magnitude, more expensive than 100 namespaces by a factor of ~50. The crossover where vCluster wins on cost-vs-isolation is in the 5–500 tenant range, where you need *some* control-plane independence but not full clusters.

---

## 19. vCluster Storage: SQLite, etcd, External

vCluster's storage layer for its virtual apiserver is configurable:

| Backend | Cost | Failure mode | When |
|---|---|---|---|
| Embedded SQLite (k3s default) | 1 PVC, no quorum | If the vCluster pod dies, sqlite is on the PVC, sqlite is fine | Default for dev/staging tenants |
| Embedded etcd | 3 pods, 3 PVCs, quorum | HA within the vCluster | Tenants whose vCluster is itself "production" |
| External etcd | use the host etcd or a dedicated one | Whatever the external store offers | Rare; usually overkill |
| External database (mysql, postgres, k3s' kine adapter) | a managed DB | Use a managed DB for HA | Niche; teams that want their cluster state in their own RDBMS |

Default is SQLite, and it is the right default. The vCluster's etcd was never the bottleneck — the tenant cluster has small state (single-team workloads, few hundred Pods, few thousand objects). SQLite is faster per-write than etcd at this scale, and it's a single file you can `kubectl cp` out for backup.

---

## 20. When vCluster Wins

The clear cases:

1. **Tenants need their own CRDs that conflict with other tenants' CRDs.** This is the single strongest case. CRD versions are cluster-scoped; you cannot have v1 and v2 of the same CRD in one cluster. vCluster gives every tenant their own CRD namespace.

2. **Tenants need cluster-admin-shaped powers without actually being cluster-admin.** A common pattern: an engineering team wants to install Helm charts that include `ClusterRole`/`ClusterRoleBinding`/`MutatingWebhookConfiguration`. On a shared cluster, those are blocked (the cluster-scoped RBAC and webhook objects are platform property). Inside a vCluster, they can install whatever they want — those objects exist only in the virtual cluster.

3. **Dev / staging on top of one prod cluster.** Instead of running three EKS clusters (dev, staging, prod), run prod as the host and dev+staging as vClusters on the same host. Dev and staging share unused capacity; isolation is enough for non-prod purposes; cost is dramatically lower than three real clusters.

4. **Ephemeral test clusters in CI.** A vCluster spins up in ~30 seconds (SQLite flavor). For every PR that touches K8s manifests, CI can spin up a vCluster, apply the manifests, run integration tests, tear down. Doing this with real clusters takes minutes and costs more.

5. **Per-team admission webhook chains.** Team A wants Kyverno policies; Team B wants OPA Gatekeeper; they conflict. Inside each team's vCluster, each runs their own policy engine without stepping on the other.

6. **Apiserver feature-flag experiments.** Inside the vCluster, a team can run `kube-apiserver --feature-gates=<X>=true` to try a feature without enabling it on the host (which would impact every other tenant).

7. **Compliance: "tenant cluster boundary" as an auditable line.** "Tenant X's CRs never reached the host's etcd; therefore the host's audit log never recorded them; therefore they're confined to the tenant's vCluster's audit log." That separation can satisfy a compliance auditor in ways that "namespace in a shared cluster" cannot.

---

## 21. When vCluster Loses

The non-cases — vCluster is the wrong answer when:

1. **Hostile tenants.** vCluster shares the host kernel. A Pod that escapes a container in the vCluster is on the host node, with access to every other tenant's Pods. The vCluster's apiserver gives no kernel-level protection. For hostile tenants you need separate clusters *or* sandbox runtimes (gVisor/Kata) per Pod *or* dedicated nodes per tenant — discussed in §23 and chapter 29.

2. **Tenants need features the syncer doesn't sync.** If a tenant relies on a CRD that the platform doesn't sync from the host, or on a host controller that doesn't exist, their workload breaks. Every new shared CRD is a coordination cost between platform and tenants.

3. **Workloads requiring the full Kubernetes API surface unmodified.** Some workloads (especially operators that watch Nodes, observe the API server's internal endpoints, or rely on cluster-scoped lookups that vCluster filters) malfunction inside a vCluster. Test the operator inside a vCluster before promising the tenant it'll work.

4. **Tenants that demand SLAs the host can't honor.** A vCluster is a pod on the host. If the host suffers an etcd outage, every vCluster on it goes dark. A tenant who needs four-nines availability needs their own cluster, with their own etcd, not a vCluster.

5. **Tenants whose scale exceeds what one syncer pod can handle.** The syncer is a controller; it watches all synced object kinds in the vCluster. A tenant with 10,000 Pods in their vCluster pushes the syncer hard. Tenants of that scale should have their own cluster — at that point the cost difference vanishes.

6. **Cross-tenant service discovery.** If tenant A's services need to be discoverable by tenant B, vClusters complicate it (each tenant's CoreDNS is private). Easier on a shared cluster with namespaces and ANP.

7. **Heavy use of host's IngressClasses with per-tenant differentiation.** vCluster syncs Ingresses to the host; the host's ingress controller serves them. Per-tenant TLS, per-tenant rate limits, per-tenant routing rules — possible, but requires platform-side machinery (annotations, Gateway listeners per tenant).

The right mental model: **vCluster is "I want my own cluster but I'm OK that it shares a kernel and a CNI and a CSI with everyone else's cluster." If any of those shares is unacceptable, vCluster is the wrong answer.**

---

## 22. Hard Multi-Tenancy: Separate Clusters

Hard multi-tenancy is *cluster-per-tenant*. Period. Anything short of separate clusters shares a kernel; a kernel exploit affects every tenant on it. If the threat model says "tenant code may be hostile" or "regulator requires demonstrated kernel-level isolation," separate clusters is the answer.

The economic argument:

```
                Cost vs Blast Radius
            
   high │                                           ● Cluster-per-tenant
        │                                          ╱  (isolation: max)
        │                                         ╱
        │                                        ╱
   cost │                              ● vCluster
        │                             ╱  (isolation: API+features)
        │                            ╱
        │                       ● Capsule/HNC
        │                      ╱  (isolation: RBAC+quota)
        │                  ● Namespace
        │                 ╱   (isolation: naming only)
   low  │ ● Single cluster
        └──────────────────────────────────────────────────────►
          low         multi-tenant blast radius          high
```

The crossover where cluster-per-tenant pays for itself:

- **Tenant value > control-plane cost.** A SaaS tenant paying $50K/month easily justifies a $500/month managed control plane.
- **Compliance demands.** SOC2/HIPAA/PCI auditors increasingly want kernel-level isolation between customers, not just RBAC.
- **Operational independence.** Tenant A's chaos-engineering experiment shouldn't take out tenant B. Separate clusters; separate blast radii.
- **API drift.** Tenant A is on Kubernetes 1.32; tenant B is on 1.28 (long-term support). Separate clusters, separate upgrade cycles.
- **Regional placement.** Tenant A is EU-only (GDPR); tenant B is US. Separate clusters in separate regions.

The orchestration of many clusters is chapter 26 (ClusterAPI for provisioning, Karmada/Fleet for workload propagation, Crossplane for off-cluster resources). The point of this chapter is: when the above conditions hit, do *not* try to solve it with vClusters; reach for separate clusters.

### 22.1 Hybrid: most tenants soft, some tenants hard

The realistic deployment is a mix. A platform team might run:

- One **shared cluster** for internal trusted teams (50 teams as namespaces).
- One **dev/staging vCluster host cluster** for ephemeral per-PR clusters.
- One **prod-per-customer cluster** for each paying SaaS customer above some tier.
- One **isolated cluster** for the regulated workload that demands gVisor + dedicated nodes.

These are managed as a fleet (chapter 26). The multi-tenancy chapter ends; the multi-cluster chapter begins.

---

## 23. Confidential / Hostile Tenant Patterns

When a tenant is genuinely hostile (could-be-malware tenant code, untrusted contractor builds, regulated workloads where the operator must not see plaintext), the stack hardens further:

1. **Separate cluster.** Always. Non-negotiable. See §22.
2. **Dedicated nodes per tenant.** Even within a separate cluster, tenant workloads run on tenant-only nodes — labeled and tainted so other workloads don't land there. This eliminates same-kernel attacks between tenants on shared nodes. Pattern:
    ```yaml
    # node labeling
    kubectl label node node-tenant-X-1 tenant=hostile-X
    kubectl taint node node-tenant-X-1 tenant=hostile-X:NoSchedule
    # pod
    spec:
      nodeSelector:
        tenant: hostile-X
      tolerations:
      - key: tenant
        operator: Equal
        value: hostile-X
        effect: NoSchedule
    ```
3. **Sandbox runtime.** A `RuntimeClass` selecting gVisor (sentry+gofer; syscall interception) or Kata (lightweight VM via Cloud Hypervisor / QEMU / Firecracker) inserts a second kernel barrier between the workload and the host. Discussed in depth in chapter 29.
    ```yaml
    apiVersion: node.k8s.io/v1
    kind: RuntimeClass
    metadata:
      name: kata
    handler: kata
    ---
    apiVersion: v1
    kind: Pod
    metadata:
      name: hostile-tenant-app
    spec:
      runtimeClassName: kata
      containers: ...
    ```
4. **Confidential Containers (CoCo) / SEV-SNP / TDX** for workloads where the operator itself must not be able to read memory. The Pod runs inside a hardware-encrypted VM; the cloud's hypervisor cannot read the workload's memory. See chapter 29.
5. **No platform-side observability into tenant payloads.** Logs and traces from hostile tenants flow to per-tenant storage (the tenant's own observability stack), not the platform's. Platform metrics see only resource-usage shapes, not content.

This is the maximum-isolation stack. It costs the most (dedicated nodes, sandbox-runtime overhead, dedicated cluster). It applies to the small subset of tenants where the threat model demands it.

---

## 24. Network Multi-Tenancy: NetworkPolicy + ANP + Egress Gateways

Recapping and extending §9 with the multi-tenancy lens:

**East-west (tenant to tenant):**

- Per-namespace default-deny `NetworkPolicy` (the floor).
- `AdminNetworkPolicy` enforcing "tenant N traffic stays within tenant N namespaces, plus explicit platform-namespaces."
- `BaselineAdminNetworkPolicy` for the "if no other policy matches, default behavior" — usually allow-platform, deny-cross-tenant.
- For workload-aware (L7) policy, Cilium's `CiliumNetworkPolicy` adds HTTP methods/paths/headers; useful when "tenant A may call `/health` on tenant B's pod but nothing else."

**North-south (internet ingress to tenant):**

- One shared Gateway per cluster (or per zone) in a platform namespace.
- Per-tenant HTTPRoute objects in tenant namespaces, attached to the shared Gateway via `parentRef`, gated by `ReferenceGrant` (§11).
- Per-tenant TLS via cert-manager Issuer / ClusterIssuer; tenant requests certs via Certificate CR in their namespace; cert-manager provisions and stores in a Secret.

**Egress (tenant to internet/partner):**

- Default egress goes via the node's SNAT (the tenant has no distinct source IP).
- For per-tenant source IP: Calico `EgressGatewayPolicy` or Cilium `CiliumEgressGatewayPolicy`. The egress gateway is a pool of Pods on dedicated nodes; their IPs are allowlisted at the partner; tenant Pods are annotated to route through them.
- Layered: a transparent egress proxy (Squid, Envoy in egress mode) for HTTP-aware allowlisting per tenant.

**DNS:**

- CoreDNS in `kube-system` serves all tenants. For per-tenant DNS isolation (rare), a NodeLocal DNS cache per tenant in their namespace.
- Tenants who need to resolve external names but be denied others: an egress NetworkPolicy that allows DNS only to CoreDNS, plus a CoreDNS plugin (`acl`) that filters per-source-namespace.

A common pitfall: **a tenant pod creates a Service of type `LoadBalancer` with an annotation like `external-dns.alpha.kubernetes.io/hostname=anything.example.com`**, and ExternalDNS dutifully creates the DNS record, opening a new tenant-controlled hostname under the platform's domain. Solution: a Kyverno policy that restricts ExternalDNS annotations to a tenant-specific subdomain (`*.payments.example.com` for team-payments).

---

## 25. Storage Multi-Tenancy: Per-Tenant StorageClass

The most common storage multi-tenancy mistake: one StorageClass shared across tenants. Consequences:

- One IOPS pool serves all tenants; tenant A's batch job starves tenant B's database.
- One encryption key encrypts all tenants' volumes; if it leaks, every tenant is exposed.
- One backup policy means all tenants have the same RPO.
- One CSI driver config (e.g., Cinder, EBS) means all tenants pay the same per-GB.

The fix: **a StorageClass per tenant tier**, restricted to the right tenants via admission policy.

```yaml
# Per-tenant StorageClass: encryption key is tenant-specific
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: team-payments-encrypted
  labels:
    tenant: team-payments
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  iops: "5000"
  throughput: "200"
  encrypted: "true"
  kmsKeyId: "arn:aws:kms:us-east-1:1234:key/team-payments-key-uuid"
  # tenant-specific tags propagate to AWS for cost allocation
  tagSpecification_1: "tenant=team-payments"
  tagSpecification_2: "cost-center=1234"
reclaimPolicy: Retain
allowVolumeExpansion: true
volumeBindingMode: WaitForFirstConsumer
```

A Kyverno/Capsule policy restricts which StorageClasses each tenant can use:

```yaml
# Kyverno: tenant team-payments may use only their classes
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: tenant-storageclass-restriction
spec:
  validationFailureAction: Enforce
  rules:
    - name: payments-storageclass
      match:
        any:
        - resources:
            kinds: ["PersistentVolumeClaim"]
            namespaces: ["team-payments*"]
      validate:
        message: "team-payments may only use team-payments-* StorageClasses"
        pattern:
          spec:
            storageClassName: "team-payments-*"
```

For CSI drivers that support multi-tenancy natively (Ceph, Portworx, some cloud CSIs), the driver itself can offer per-tenant pools / QoS classes / quotas via per-tenant StorageClasses. Use these where available; they push enforcement down to the storage layer.

For volume snapshots / backups: per-tenant `VolumeSnapshotClass`, per-tenant Velero schedules. The blast radius "restored a snapshot to the wrong tenant" needs RBAC + admission + tenant-namespace conventions to prevent.

---

## 26. Compute Multi-Tenancy: Shared vs Dedicated Nodes

The placement question: do all tenants share the same node pool, or does each tenant have their own?

**Pattern 1: shared nodes.** All tenants' Pods may land on any node. The kubelet's QoS and cgroup machinery (chapter 21) keeps them apart. Cheapest, most flexible.

```yaml
# Tenant pod, no node affinity
spec:
  containers:
    - resources:
        requests: {cpu: 500m, memory: 1Gi}
        limits:   {cpu: 1,    memory: 1Gi}
```

Pros: maximum bin-packing, simplest. Cons: noisy neighbor (§27), no kernel isolation, page-cache contention.

**Pattern 2: dedicated node pool per tenant.** A subset of nodes are labeled and tainted for a specific tenant; only that tenant's Pods schedule there.

```yaml
# Node provisioning (Karpenter NodePool / managed-node-group / ClusterAutoscaler ASG)
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: team-payments-pool
spec:
  template:
    metadata:
      labels:
        tenant: team-payments
    spec:
      taints:
        - key: tenant
          value: team-payments
          effect: NoSchedule
      requirements:
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]
---
# Tenant pod
spec:
  nodeSelector:
    tenant: team-payments
  tolerations:
    - key: tenant
      operator: Equal
      value: team-payments
      effect: NoSchedule
```

Pros: no cross-tenant kernel/noisy-neighbor; per-tenant instance types (GPU pool for one tenant, ARM for another); per-tenant cost attribution at the node level. Cons: more nodes (each tenant's pool needs some min); fragmented capacity.

Mid-ground: **tier-based node pools** — system, platform, tenant-default, tenant-batch, tenant-gpu. Tenants share nodes within their tier, but tiers are isolated.

**Pattern 3: hostile-tenant dedicated nodes.** Discussed in §23. Always with a sandbox runtime layered.

### 26.1 The schedulability matrix

| Pattern | Cross-tenant kernel attack risk | Noisy neighbor | Cost | When |
|---|---|---|---|---|
| Shared nodes, all tenants | high (any priv pod owns the kernel) | high | low | Trusted internal teams |
| Shared nodes per tier | medium (tier-level) | medium | low-med | Most internal platforms |
| Dedicated nodes per tenant | low (same as shared if priv pod allowed) | none | high | Tenants paying for it; compliance |
| Dedicated nodes + sandbox runtime | very low | none | very high | Hostile tenants; regulated workloads |

The bias for cost: prefer Pattern 2 (per-tier dedicated nodes) for any platform with more than ~5 tenants, because it removes the noisy-neighbor headaches without exploding cost.

---

## 27. The Noisy Neighbor Problem

Even with QoS classes, ResourceQuota, and cgroup limits, multi-tenant nodes have *several* kinds of contention that don't show up as exceeded `cpu.max` or `memory.max`:

1. **Page-cache contention.** The kernel's page cache is shared across all cgroups on the node. Tenant A's `find / -type f` reads gigabytes from disk; the kernel evicts tenant B's hot pages to make room; tenant B's previously-cached database now sees disk I/O on every read. Solutions:
    - `madvise(MADV_DONTNEED)` patterns in workloads (a workload-side fix).
    - cgroup-v2 `memory.high` per tenant: bounds memory growth, which indirectly limits page-cache footprint.
    - `MemoryQoS` feature gate (chapter 21): kubelet applies `memory.high` based on QoS.
    - Pin sensitive workloads to dedicated nodes (the only complete fix).

2. **CPU throttling spillover.** Tenant A hits `cpu.max` and gets throttled. Throttling is per-cgroup, so tenant B's CPU is not directly affected. *But*: the runqueue effect — when many Pods are throttled and unthrottled together, the scheduler's load is bursty, and tail latency suffers across all Pods. The classic CFS-throttling-tail-latency paper documents this; the fix is usually *removing CPU limits* (chapter 21 §10) and relying on `cpu.weight`.

3. **NUMA-locality loss.** Tenant A's memory was allocated on NUMA node 0 (closer to socket 0). The kernel later migrates tenant A's Pod to socket 1 (rebalancing). Memory access now crosses the interconnect. Mitigations: Topology Manager `single-numa-node` policy; per-tenant nodes (so the platform controls NUMA placement at provisioning time).

4. **Network bandwidth contention.** Two tenants on the same node both saturate the NIC. Linux's TC and the CNI bandwidth plugin can rate-limit per-Pod:
    ```yaml
    spec:
      containers: ...
      # CNI bandwidth plugin (in CNI plugin chain)
      # Pod annotations:
    metadata:
      annotations:
        kubernetes.io/ingress-bandwidth: "10M"
        kubernetes.io/egress-bandwidth: "10M"
    ```
    Or per-tenant on dedicated nodes (cleaner).

5. **Disk I/O contention.** Two tenants both writing to the same local SSD. cgroup-v2 `io.cost` (or `io.max`) per Pod, configured via kubelet, throttles IOPS per cgroup. Most clouds with EBS/persistent-disk have per-volume IOPS, which is per-PVC, which is per-tenant (with per-tenant StorageClasses §25). Local SSDs (instance store) lack this isolation; per-tenant nodes is the fix.

6. **Network connection-tracking exhaustion.** Each Pod's traffic shows up in the host's conntrack table. Tenant A opens 100K connections; the conntrack table fills; tenant B's *new* connections fail. Tunable via `net.netfilter.nf_conntrack_max` (sysctl) on the host; eBPF-based CNIs (Cilium without kube-proxy) eliminate the issue by not using conntrack for in-cluster traffic.

7. **PID exhaustion.** Tenant A forks a fork bomb; the host's PID space fills; the kubelet runs out of PIDs and can't start tenant B's Pod. cgroup-v2 `pids.max` per-Pod (kubelet's `--pod-pids-limit`) caps this; ResourceQuota `count/pods` caps it at the namespace level.

8. **Inode exhaustion.** Tenant A creates millions of tiny files on the node's filesystem. Inodes are a finite resource per filesystem. ResourceQuota on ephemeral-storage by *bytes* doesn't cap inodes. Mitigations: kubelet's `imagefs.inodesFree` eviction signal; xfs (more inodes than ext4); per-tenant local-volume CSIs (with per-tenant inode quotas in xfs).

The pattern: **soft isolation works for the *average* case; the *tail* (P99 latency, transient spikes) is where noisy neighbors bite.** The mitigations are a mix of cgroup tuning, CNI features, and "pin the latency-sensitive workloads to dedicated nodes."

---

## 28. Cost Attribution Per Tenant

Multi-tenant clusters break the cloud bill: every tenant runs on shared nodes, the bill comes back as one line item "EC2 + EBS + LB," and finance demands a per-tenant breakdown.

The pieces:

1. **Label discipline.** Every Pod and PVC must carry a `tenant=<name>` and `cost-center=<id>` label. Enforced by Kyverno mutation (§10): the namespace's labels propagate to every workload created in it.

2. **Cost engine.** `OpenCost` (CNCF, open-source) or `Kubecost` (commercial, OpenCost as the foundation). The engine:
    - Scrapes Prometheus metrics for per-Pod CPU/memory usage (kubelet/cAdvisor).
    - Joins with cloud pricing (EC2 instance prices, EBS per-GB-month, LB hourly, egress per-GB).
    - Joins with labels to attribute costs per tenant.
    - Outputs per-tenant cost-per-day, with breakdown: compute (node-share), storage (PVC), network (LB + egress), idle (unallocated capacity charged proportionally).

3. **Node-share math.** A Pod requesting 0.5 CPU on a 16-CPU $200/day node is attributed $200 × 0.5/16 = $6.25/day. If actual usage is higher (the Pod bursts), Kubecost reports both *requested-cost* and *usage-based-cost*; the platform picks which to bill.

4. **Idle attribution.** Nodes with unused capacity are an overhead. Options: spread idle cost across tenants proportionally to their requests, or charge it to the platform team as "fleet inefficiency."

5. **Network egress.** The hardest to attribute. Per-Pod egress requires CNI metrics (Cilium's `pod_network_egress_bytes_total` + tenant labels) or VPC flow logs joined with Pod IP timelines. Kubecost supports this with cloud-specific add-ons.

6. **Storage cost.** Per-PVC, by labels and StorageClass. Easy when per-tenant StorageClasses are used; harder otherwise.

7. **Showback vs chargeback.** Showback: report costs to tenants, no actual billing. Chargeback: bill the tenant's budget. Chargeback creates the incentive to right-size; showback alone is ignored.

A sample of what good cost attribution looks like in practice:

```
   Tenant     Daily Cost   Compute   Storage   Network   Idle Share
   ────────   ──────────   ───────   ───────   ───────   ──────────
   payments    $1240        $890      $230      $45        $75
   search      $620         $450      $90       $30        $50
   ml-train    $3400        $3100     $200      $80        $20
   web-front   $410         $280      $50       $50        $30
   ────────   ──────────   ───────   ───────   ───────   ──────────
   total       $5670
```

Without this, multi-tenancy collapses politically: the most expensive tenant looks the same as the cheapest tenant in the bill.

---

## 29. Audit Per Tenant

The kube-apiserver audit log records every request: who, what, when, to which resource. For a multi-tenant cluster, the platform team needs both a global view ("what's happening cluster-wide?") and a per-tenant view ("show me everything tenant X did this week").

The audit policy supports two stages and filtering:

```yaml
# Audit policy: capture tenant-scoped activity at request level
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
  # Drop low-value reads
  - level: None
    users: ["system:serviceaccount:kube-system:*"]
    verbs: ["get", "list", "watch"]

  # Log tenant writes at RequestResponse (most detailed)
  - level: RequestResponse
    namespaces: ["team-*"]
    verbs: ["create", "update", "patch", "delete", "deletecollection"]

  # Log tenant reads at Metadata (just headers)
  - level: Metadata
    namespaces: ["team-*"]
    verbs: ["get", "list", "watch"]

  # Always-on: any privilege escalation attempt
  - level: RequestResponse
    resources:
      - group: rbac.authorization.k8s.io
        resources: ["roles","rolebindings","clusterroles","clusterrolebindings"]
    verbs: ["create", "update", "patch", "delete"]

  # Default
  - level: Metadata
    omitStages:
      - RequestReceived
```

The audit log file is then shipped to a tenant-aware sink: each tenant gets a filtered stream by `objectRef.namespace`. Tools:

- **Falco** (`falcosecurity/falco`) sidecar in DaemonSet, with audit-rule plugins.
- **Audit2Logging stack**: Fluent Bit reads `/var/log/audit/`, parses JSON, routes per-tenant to Loki / Elasticsearch indices.
- **Cloud-native**: GKE Audit Logs (Cloud Logging filters), EKS Audit Logs (CloudWatch Insights), AKS Diagnostic Logs.

The output workflow: "show me what tenant X did" = a saved Loki/CloudWatch query filtered by `objectRef.namespace=team-payments` over a time range, exportable to CSV. Compliance teams ask for this regularly.

Tenant access to *their own* audit log is a tenant-friendliness feature: a tenant should be able to see "who in my team did what" without filing a ticket. Usually delivered via a Grafana dashboard with a per-tenant data source.

---

## 30. Multi-Tenant Operators

Operators (chapter 23) running on a shared multi-tenant cluster need to be tenant-aware. A single Postgres operator (Zalando, CloudNativePG, etc.) serving 50 teams' databases is more cost-effective than 50 operators, but it needs:

1. **CR namespacing.** The operator watches its CR (`PostgresCluster`) across *all* namespaces (or a labeled subset). Each tenant creates the CR in their own namespace.
2. **Tenant identity in reconcile.** When reconciling tenant A's CR, the operator must:
    - Create Pods/Services/PVCs in tenant A's namespace (RBAC `Role` in each tenant ns).
    - Apply tenant A's `tenant=` label to every child object.
    - Use tenant A's StorageClass for PVCs.
    - Use tenant A's encryption key, secret store, etc.
3. **Tenant resource limits.** The operator must respect the namespace's ResourceQuota — if creating a 3-replica Postgres exceeds the tenant's quota, the operator should fail the CR's status, not create partial workloads.
4. **Operator isolation from tenants.** Tenant A's CR must not be able to specify `Image: malicious:1.0` and have the operator run it. Operators validate (or pin) images.
5. **Cross-tenant authorization.** Tenant A's `PostgresCluster` referencing `secretRef: <other-namespace>/<secret>` must be denied unless explicitly granted (similar to ReferenceGrant in Gateway API).

The operator's own RBAC is typically a ClusterRole with `get/list/watch` on `PostgresCluster` cluster-wide, plus per-tenant-namespace RoleBindings granting it `create/update/delete` on Pods/PVCs/Services/Secrets in tenant namespaces. The operator runs in a dedicated `postgres-operator` namespace, not in any tenant's.

A common pitfall: an operator that **stores its global state in one namespace** (a leader election Lease, a configuration ConfigMap) but reconciles tenant CRs. Tenant A can't disrupt tenant B's reconcile *unless* tenant A can interfere with the operator's global state. RBAC must keep tenants out of `postgres-operator` namespace entirely.

OperatorHub / OLM make some of this easier (per-tenant operator instances installed via OLM subscriptions), but tend toward "one operator per tenant," which is wasteful at scale.

---

## 31. Tenant Onboarding and Offboarding

A platform with N tenants needs both flows to be repeatable, scripted, auditable.

### 31.1 Onboarding flow

```
                ┌──────────────────────────────────┐
                │  1. Tenant request               │
                │     (PR to a tenants/ repo)      │
                │     specifies: name, owner,      │
                │     quota tier, integrations     │
                └────────────────┬─────────────────┘
                                 │
                                 ▼
                ┌──────────────────────────────────┐
                │  2. Platform team review +       │
                │     merge                         │
                └────────────────┬─────────────────┘
                                 │
                                 ▼
                ┌──────────────────────────────────┐
                │  3. GitOps engine renders        │
                │     the per-tenant bundle (§12)  │
                │     from a template              │
                └────────────────┬─────────────────┘
                                 │
                                 ▼
                ┌──────────────────────────────────┐
                │  4. ArgoCD/Flux applies:         │
                │     - Namespace(s)               │
                │     - Role/RoleBinding           │
                │     - ResourceQuota, LimitRange  │
                │     - default-deny NetworkPolicy │
                │     - baseline egress NP         │
                │     - tenant-specific StorageClass│
                │     - ReferenceGrant for Gateway │
                │     - (optional) Capsule Tenant  │
                │     - (optional) vCluster Helm   │
                │       release                    │
                └────────────────┬─────────────────┘
                                 │
                                 ▼
                ┌──────────────────────────────────┐
                │  5. IdP automation               │
                │     creates `team-X-developers`  │
                │     and `team-X-admins` groups   │
                │     and adds initial members     │
                └────────────────┬─────────────────┘
                                 │
                                 ▼
                ┌──────────────────────────────────┐
                │  6. Cost-engine reload           │
                │     (OpenCost picks up new       │
                │      tenant label)               │
                │  7. Audit-sink reload            │
                │     (filter rule for new ns)     │
                │  8. Monitoring dashboard         │
                │     (auto-discovers via labels)  │
                │  9. Email tenant owner: ready    │
                └──────────────────────────────────┘
```

Time from PR merge to tenant having a working namespace: typically ~5 minutes. Tenant doesn't write any infrastructure code; they get a kubeconfig (or, for vCluster, a vCluster kubeconfig) and a Confluence page.

### 31.2 Offboarding flow

The hard direction. Deletion order matters because finalizers and cross-namespace references can prevent cleanup.

```
                1. Tenant request: deboard X
                                 │
                                 ▼
                2. Verify no shared resources:
                   - PVs (with reclaim=Retain) cleaned manually
                   - cloud LBs released (Service deletion handles this)
                   - external DNS records released
                   - cert-manager Certificates (Secret cleanup)
                                 │
                                 ▼
                3. Quiesce tenant workloads:
                   kubectl scale deployment --all --replicas=0 -n team-X
                   wait for Pods to terminate
                                 │
                                 ▼
                4. Drop tenant access:
                   delete RoleBindings, ClusterRoleBindings
                   (so tenant can't re-create workloads)
                                 │
                                 ▼
                5. Backup tenant data (if SLA mandates):
                   Velero backup of namespace
                   Snapshot PVs
                   archive to cold storage
                                 │
                                 ▼
                6. kubectl delete ns team-X
                   - cascading delete of namespaced objects
                   - watch for stuck finalizers; resolve manually
                                 │
                                 ▼
                7. Cluster-scoped cleanup:
                   - delete ClusterRoles named "team-X-*"
                   - delete tenant-specific StorageClass (if no PVs left)
                   - delete tenant-specific PriorityClass
                   - delete tenant-specific CRDs (caution!)
                                 │
                                 ▼
                8. IdP cleanup:
                   - delete team-X groups (or mark archived)
                                 │
                                 ▼
                9. Cost-engine cleanup:
                   - move historical attribution to archive
                10. Audit log retention:
                    - mark namespace audit logs for compliance retention
                    - schedule deletion per policy (often 7 years for SOX)
```

The pitfalls:

- **Stuck finalizers.** A Namespace with `kubernetes.io/finalizer: <something>` stuck because the controller that owns the finalizer is gone. Manual fix: `kubectl patch namespace team-X -p '{"metadata":{"finalizers":[]}}' --type=merge` — but only after verifying the external resource is truly gone.
- **PVs with `reclaim=Retain`** — the PV stays, the PVC deletes, the disk in the cloud is still billed. Cleanup must explicitly handle PVs.
- **External resources by name.** A cloud LB created by an Ingress; an S3 bucket created by an operator. ExternalDNS records. Cert-manager external secrets. Each needs an explicit cleanup step or a finalizer that the operator must honor.
- **Audit retention.** Even after the namespace is gone, audit records must be retained per compliance policy. The audit sink is *not* tenant-deleted; it's archived.

The fundamentally hard part: ensuring no resource billed to your cloud account survives the tenant's offboarding. Cost-engine reports + cloud-native tagging (every cloud resource tagged with `tenant`) make this auditable.

---

## 32. The Platform/Tenant Responsibility Matrix

Multi-tenancy needs a clear contract: who owns what.

| Concern | Platform team owns | Tenant team owns |
|---|---|---|
| **apiserver, etcd, scheduler, controllers** | Yes | No |
| **Node provisioning, OS, kernel, CRI, kubelet** | Yes | No |
| **CNI (Cilium/Calico/etc.)** | Yes | No (uses what's there) |
| **CSI drivers** | Yes | Selects StorageClass from allowed list |
| **Cluster DNS (CoreDNS)** | Yes | No |
| **Default ingress / Gateway controller** | Yes | Owns HTTPRoute / Ingress objects |
| **Identity (OIDC, IdP wiring)** | Yes | Manages own group membership |
| **Observability (Prometheus, Loki, Tempo)** | Operates the stack | Owns dashboards, alerts, instrumentation |
| **Backup / DR for cluster** | Yes | Owns app-level backup if needed |
| **Cluster upgrade cadence** | Yes | Adapts to deprecations |
| **Namespace, RBAC, quota, NetworkPolicy** | Defaults; protected from tenant edit | Owns *additional* policies within namespace |
| **PSA labels** | Yes (cannot be edited by tenant) | No |
| **PriorityClass definitions** | Yes | Selects from allowed list |
| **StorageClass definitions** | Yes | Selects from allowed list |
| **ImageRegistry allowlist** | Yes | No (must use allowed registries) |
| **CRDs** | Cluster-scoped CRDs are platform-owned; per-tenant CRDs require vCluster | Owns CRDs *inside* vCluster |
| **Tenant workloads (Deployment, Service, etc.)** | No | Yes |
| **Tenant ConfigMaps, Secrets** | No | Yes |
| **Tenant HPA, PDB, NetworkPolicy (additive)** | No | Yes |
| **Tenant cost** | Reports it | Pays / right-sizes |
| **Tenant incidents** | Cluster-level | App-level |
| **Tenant security review** | Provides defaults | Owns app-level vulns |

The contract is documented (a one-page Confluence). When a tenant says "the platform broke our app," the matrix is the first reference. When the platform team plans an upgrade, they communicate per the contract. When a security incident happens, the matrix says who's accountable.

A subtle but important point: **the platform team's policies are *protected* — tenants cannot weaken them, and the policy engine enforces that.** A tenant who deletes their default-deny NetworkPolicy gets it re-created within seconds. A tenant who downgrades their namespace's PSA label gets it rejected by admission. The contract isn't a wiki page; it's enforced by code.

---

## 33. Pitfalls

The collection of mistakes every multi-tenant platform team makes once, then writes a runbook to prevent. Each entry is short on purpose; the body of the chapter is the long version.

1. **Treating namespaces as a security boundary.** They're not. PSA `restricted` + policy engine + sandbox runtime + (for hostile tenants) separate clusters is the security boundary. (§3)
2. **One cluster-wide ResourceQuota.** Tenants starve each other; one bad pod eats the budget. Per-namespace quotas, ideally split by PriorityClass scope. (§8)
3. **RBAC verbs of `["*"]` on `resources: ["*"]`.** Effectively cluster-admin in disguise. Always enumerate; let RBAC's privilege-escalation check work. (§7)
4. **PSA off in non-system namespaces.** A tenant creates a `hostPath: /` pod and reads everything on the node. Default `restricted` everywhere; document the exceptions. (§10)
5. **No default-deny NetworkPolicy per namespace.** Lateral movement is trivial; any compromised pod sees the whole east-west. (§9)
6. **`hostPath` allowed in tenant namespaces.** Owns the node trivially. Blocked by PSA `restricted` (and Kyverno belt-and-suspenders). (§3, §10)
7. **`PriorityClass: system-cluster-critical` on tenant pods.** Tenants preempt platform components; CoreDNS goes down because someone's batch job thinks it's important. Kyverno allowlist on PriorityClass values. (§8.4)
8. **Pulling images from arbitrary registries.** Tenant pulls `attacker/coinminer:latest` from Docker Hub. Capsule's `containerRegistries.allowed` or a Kyverno policy restricts. Pair with signature verification (chapter 27). (§14)
9. **Allowing `allowPrivilegeEscalation: true` via init containers.** Tenants escalate through init containers when main containers are restricted. PSA `restricted` covers initContainers too. (§10)
10. **Sharing `kube-system` ServiceAccount tokens.** A tenant mounts the default ServiceAccount of `kube-system`. Tenants must only mount their own SA tokens; admission rejects cross-namespace SA refs.
11. **vCluster syncer with cluster-admin on the host.** The syncer's RBAC must be tight: namespace-scoped to the host namespace + read on the few cluster-scoped resources it actually needs (Nodes, StorageClasses if synced). Default Helm chart is reasonable; custom configs often over-grant. (§17)
12. **No per-tenant cost attribution.** Cloud bill is one line item; finance demands breakdown; you have no labels; you reverse-engineer from cAdvisor metrics for a week. Bake labels into the onboarding bundle. (§28)
13. **No per-tenant audit.** Compliance asks "what did tenant X do last week"; you grep the apiserver audit log live. Tag audit events by namespace at ingest. (§29)
14. **Allowing tenants to create LimitRange / ResourceQuota.** Tenants disable their own enforcement. RBAC on `quota`/`limitrange` resources is platform-only; reserved CRUD via the platform's RBAC bundle.
15. **Cluster-wide ConfigMaps mounted by all tenants.** A platform "config" ConfigMap in `kube-public` with secrets-shaped data; every tenant's Pod reads it. ConfigMaps don't span namespaces; if you find yourself replicating via tooling, audit what's actually in there.
16. **Cross-tenant Secret references via volumeRef.** A tenant references `secretName: other-tenant-secret`; cross-namespace secret refs are denied by the kubelet (it can only mount secrets from the same namespace), but operators that inject Secrets can leak. Audit operator behavior.
17. **`automountServiceAccountToken: true` (default) for everything.** Every Pod gets the namespace's SA token mounted; many Pods don't need it; compromised Pod has API access. Set `automountServiceAccountToken: false` by default via Kyverno mutation.
18. **Long-lived legacy SA tokens.** A tenant has a `Secret` of type `kubernetes.io/service-account-token` in their namespace; never expires. Move to `BoundServiceAccountTokenVolume` (projected SA tokens, audience-bound, expiring). Chapter 07.
19. **A tenant CRD that conflicts with platform CRDs.** Tenant installs `cert-manager` CRDs that conflict with the platform's older version. Lock down `customresourcedefinitions` to platform; offer vCluster for tenants who need their own.
20. **Tenant Helm charts installing ClusterRoleBindings.** A "harmless" Helm chart includes a ClusterRoleBinding granting `system:masters`. Block all ClusterRole/ClusterRoleBinding creation by tenants; whitelist platform charts only.
21. **NetworkPolicy without `policyTypes: [Ingress, Egress]`.** Default-deny only inbound; egress remains allow-all. Always specify both `policyTypes`.
22. **One NetworkPolicy that "denies" but selector doesn't match all pods.** `podSelector: {matchLabels: {tier: backend}}` denies only backend; frontend pods are allow-all. The deny rule's `podSelector: {}` is what selects-everything.
23. **No PDB on platform components in multi-tenant clusters.** During a node upgrade, all CoreDNS replicas drain simultaneously; cluster-wide DNS outage; every tenant goes red. PDB on every platform component.
24. **Tenants able to taint nodes.** A tenant adds a taint to a node "for their pods"; other tenants' pods get evicted. RBAC on Node objects is platform-only (the Node authorizer plus RBAC: only `system:masters` and CCM should write Nodes).
25. **Cluster-wide MutatingWebhook from a tenant.** A tenant installs Istio's webhook (somehow); now every Pod across the cluster gets a sidecar. ValidatingAdmissionPolicy / MutatingWebhookConfiguration are cluster-scoped — tenant-installed ones can affect all tenants. Lock down by RBAC; or run per-tenant in vCluster.
26. **vCluster sync of host CRDs the tenant relies on but the host doesn't have.** Tenant `kubectl get certificates` works inside vCluster; cert-manager isn't installed on host; nothing reconciles. Test the operator inside a vCluster before promising tenants it works.
27. **Forgetting `services.nodeports: 0` in quota.** Tenants create NodePort Services; bind to a host port; ahem, who told you 30000+ is the only range?; collisions; security implications. Always cap at 0.
28. **No `topologySpreadConstraints` enforcement.** A tenant's 10-replica Deployment lands all on one node; node fails; tenant outage. Kyverno mutation that injects sensible spread constraints when `replicas > 1`.
29. **GitOps engine running as cluster-admin.** ArgoCD's `argocd-application-controller` runs with full cluster-admin; a tenant who can edit their `Application` object effectively gets cluster-admin via ArgoCD. ArgoCD `AppProject` per tenant; restrict destinations to tenant namespaces only.
30. **Capsule Tenant with `ingressClasses.allowed: ["*"]`.** Tenants can use the platform's internal IngressClass and expose their workload through the wrong dataplane. Pin the allowed classes.

The pitfalls collectively explain why "soft multi-tenancy" is a design discipline, not a feature. Miss one item and the entire tenancy story collapses to "namespace as a security boundary," which it isn't.

---

## 34. TL;DR

**A tenant is not a feature; it's a definition.** Application team, environment, customer, fleet, hostile workload — pick exactly which one before reaching for any technology. The implementation is downstream of the model.

**The namespace is the logical scope, not the security boundary.** It gives you naming, RBAC scope, quota scope, default LimitRange application, NetworkPolicy scope, PSA enforcement. It does *not* give you kernel-level isolation. A privileged pod in any namespace owns the node.

**Soft multi-tenancy is a stack of five layers** — naming (namespace), authorization (RBAC + admission), capacity (ResourceQuota + LimitRange + PriorityClass), network (NetworkPolicy + ANP + egress gateways), workload (Pod Security Admission + Kyverno/Gatekeeper/VAP + RuntimeClass). Miss any layer and the rest don't matter.

**The platform team owns the policies; the tenant team owns the workload.** Generate the per-tenant bundle (namespace, RBAC, quota, LimitRange, default-deny NetworkPolicy, ReferenceGrant) from a template; apply via GitOps; enforce defaults via Kyverno/Gatekeeper/VAP so tenants can never weaken them.

**HNC and Capsule make namespace tenancy declarative.** HNC for hierarchical inheritance within a single tenant's many namespaces; Capsule for `Tenant`-as-a-CR with quota, RBAC, NetworkPolicy, image-registry allowlists baked in.

**vCluster is the middle option.** A virtual control plane in a host-cluster namespace; tenant is cluster-admin inside; syncer translates Pods/Services/PVCs/etc. to host. Wins when tenants need their own CRDs, their own cluster-scoped resources, their own admission rules — all without giving them real cluster-admin. Loses when tenants are hostile (still shared kernel) or when the workload depends on host CRDs the syncer doesn't translate.

**Hard multi-tenancy = separate clusters.** Always. For hostile tenants, regulated workloads, or true SLA independence. Cluster-per-tenant cost is justified by tenant value, compliance demands, or blast-radius requirements; the operational overhead is amortized by fleet tooling (chapter 26).

**Confidential / hostile patterns layer further:** dedicated cluster + dedicated nodes per tenant + sandbox runtime (gVisor/Kata, chapter 29) + confidential containers (SEV-SNP/TDX) for workloads where even the operator must not see plaintext.

**Network multi-tenancy = default-deny NetworkPolicy + ANP/BANP for cluster-level defaults + per-tenant egress gateways for source-IP attribution.** Storage multi-tenancy = per-tenant StorageClass (different IOPS, encryption keys, backup policies). Compute multi-tenancy = a mix of shared-tier nodes for trusted teams, dedicated nodes per tier for noisy-neighbor isolation, dedicated nodes per tenant for hostile workloads.

**Noisy neighbor is real and partially solvable.** Page-cache contention, CPU-throttling tail latency, NUMA migration, network/disk bandwidth contention, conntrack/PID/inode exhaustion. Each has a cgroup or kernel tunable, but the only complete fix for latency-sensitive workloads is dedicated nodes.

**Cost attribution and audit are not nice-to-haves.** Per-tenant labels propagated by Kyverno; OpenCost/Kubecost for compute/storage/network breakdown; audit-log filters per namespace; per-tenant Grafana dashboards. Without these, multi-tenancy collapses politically — the loudest tenant gets all the resources, the others leave.

**Onboarding and offboarding are scripted from one Git repo.** A tenant request is a PR; merge → GitOps applies the bundle in ~5 minutes; offboarding is a kubectl delete plus cleanup of cloud-tagged external resources. Stuck finalizers and retained PVs are the recurring offboarding pitfalls.

**The 30 pitfalls in §33 are the chapter's checklist.** Every multi-tenant platform team will hit at least half of them within the first year. Internalize them once; the chapter has done its job.

The big mental shift this chapter pushes: **stop thinking of multi-tenancy as a single configuration; think of it as a *contract* between platform and tenants, enforced by a stack of policies the tenants cannot edit, audited continuously, and chosen at the right point on the spectrum from "we share everything" to "we share nothing."** Namespaces, Capsule, vCluster, separate clusters are points on that line; the right answer is "two or three of them, simultaneously, for different tenant classes." The contract is the durable artifact; the technologies that implement it are interchangeable.
