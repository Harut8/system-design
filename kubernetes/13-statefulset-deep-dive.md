# StatefulSet Deep Dive

The StatefulSet is the Kubernetes controller most engineers underestimate, then overestimate, then finally — usually after a 3 a.m. data incident — develop a healthy fear of. Where a Deployment treats Pods as a herd of interchangeable cattle, a StatefulSet treats them as a numbered cohort whose identity, storage, and ordering matter. `pod-0` is not `pod-1`. The volume attached to `pod-0` is not the volume attached to `pod-1`. The DNS name `pod-0.svc.ns.svc.cluster.local` will resolve to the same logical workload tomorrow, next week, and after the next cluster upgrade — even if the Pod's IP, node, and container ID all change. That stability is the entire reason this controller exists.

This chapter is the long-form reference for everything the StatefulSet controller does, what its four guarantees actually mean at the etcd-and-DNS level, and how production stateful systems (Postgres, etcd, Kafka, Cassandra) build on top of it. It sits between [ch 12 (other workload controllers)](12-workload-controllers.md), which describes the controllers that *don't* have stable identity (Deployment, DaemonSet, Job), and [ch 19 (CSI / PV / PVC)](19-storage-csi-pv-pvc.md), which is the storage substrate every non-trivial StatefulSet depends on. It is tightly coupled to [ch 18 (DNS / CoreDNS)](18-dns-and-coredns.md): the headless Service that powers StatefulSet identity is a DNS contract, and the contract is meaningful only if you understand how CoreDNS resolves headless A records to individual Pod IPs.

If you only remember one sentence from this chapter: **a StatefulSet is a numbered set of Pods bound 1:1 to numbered PVCs and addressable by ordinal DNS names, where create/delete/update order is deterministic and storage outlives the Pod — and every other property follows from those three invariants (identity, storage, ordering).**

---

## Table of Contents

1.  [Why StatefulSets Exist](#1-why-statefulsets-exist)
2.  [The Four Guarantees](#2-the-four-guarantees)
3.  [Pod Identity: Names, Hostnames, FQDNs](#3-pod-identity-names-hostnames-fqdns)
4.  [Headless Services: The DNS Contract](#4-headless-services-the-dns-contract)
5.  [`volumeClaimTemplates`: Per-Pod Storage Materialization](#5-volumeclaimtemplates-per-pod-storage-materialization)
6.  [PVC Retention Policy (1.27+ GA)](#6-pvc-retention-policy-127-ga)
7.  [Pod Management Policy: `OrderedReady` vs `Parallel`](#7-pod-management-policy-orderedready-vs-parallel)
8.  [The Reconcile Algorithm](#8-the-reconcile-algorithm)
9.  [Rolling Updates: Descending Ordinal Order](#9-rolling-updates-descending-ordinal-order)
10. [Partitioned Rollout: Canary for Stateful Workloads](#10-partitioned-rollout-canary-for-stateful-workloads)
11. [`maxUnavailable` for StatefulSets](#11-maxunavailable-for-statefulsets)
12. [Scale-Down: Highest Ordinal First](#12-scale-down-highest-ordinal-first)
13. [Stable DNS Records Across Scaling Events](#13-stable-dns-records-across-scaling-events)
14. [The Bootstrap Problem: Predictable DNS as Cluster Topology](#14-the-bootstrap-problem-predictable-dns-as-cluster-topology)
15. ["Wait for Pod-N Ready" Semantics](#15-wait-for-pod-n-ready-semantics)
16. [Init Containers and the Join Pattern](#16-init-containers-and-the-join-pattern)
17. [Pod Identity vs Node Placement](#17-pod-identity-vs-node-placement)
18. [Topology Constraints: One-per-Zone Patterns](#18-topology-constraints-one-per-zone-patterns)
19. [Headless vs ClusterIP: Use Both](#19-headless-vs-clusterip-use-both)
20. [PVC Expansion in a StatefulSet](#20-pvc-expansion-in-a-statefulset)
21. [Backups: Snapshots, App-Level, and the Snapshot-of-a-Running-DB Problem](#21-backups-snapshots-app-level-and-the-snapshot-of-a-running-db-problem)
22. [Operators That Wrap StatefulSets](#22-operators-that-wrap-statefulsets)
23. [Diagnosing "Pod Pending: PVC Unbound"](#23-diagnosing-pod-pending-pvc-unbound)
24. [Disaster Recovery: Losing Pod-0](#24-disaster-recovery-losing-pod-0)
25. [Migration Patterns for Stateful Data](#25-migration-patterns-for-stateful-data)
26. [`whenDeleted=Delete`: The Auto-Cleanup Option](#26-whendeletedelete-the-auto-cleanup-option)
27. [The "Ordinal 0 Is Leader" Myth](#27-the-ordinal-0-is-leader-myth)
28. [`minReadySeconds` and Rollout Pacing](#28-minreadyseconds-and-rollout-pacing)
29. [Observability: Metrics, Conditions, Alerts](#29-observability-metrics-conditions-alerts)
30. [Pitfalls](#30-pitfalls)
31. [TL;DR](#31-tldr)

---

## 1. Why StatefulSets Exist

Take any introductory Kubernetes course and you will be told that Pods are ephemeral and disposable. That is true — for stateless workloads. A `Deployment` rolling out an HTTP server treats every Pod as a replaceable instance: which Pod takes the next request is decided by kube-proxy load balancing; which Pod gets evicted during a node drain is decided by the controller's surge math; which Pod has the highest hostname suffix changes every time the ReplicaSet hash rotates. The contract is *replication, not identity*. Every Pod is the same Pod.

Stateful workloads break that contract. A Postgres primary is not the same as a Postgres standby. Etcd peer `etcd-0` advertises a `peerURL` of `https://etcd-0.etcd.default.svc.cluster.local:2380`, and the other members of the Raft cluster expect that exact URL to resolve to the right voter. Kafka broker `broker-3` owns partitions whose log segments live on a specific disk; if that broker comes back as `broker-7`, the cluster reassigns leadership and rebalances terabytes of data. Cassandra node tokens are pinned to instances. The notion that any replica can replace any other replica is wrong; the application has *per-instance* state and identity.

There are exactly two things stateful applications need from the orchestrator that stateless ones do not:

1.  **Stable network identity.** Every replica needs a name that survives Pod restarts, Pod re-scheduling, image upgrades, and ReplicaSet-like rotations. The replica must be able to advertise that name and have it resolve to itself, and only itself, every time.
2.  **Stable storage.** Every replica needs a persistent volume that survives Pod restarts and is *re-mounted to the same logical replica* every time. Pod `db-0` always gets disk `data-db-0`, even if the underlying PV is moved between nodes by the storage layer. If a fresh Pod comes up as `db-0`, it must inherit `db-0`'s disk — not get a blank one.

```
WHAT A DEPLOYMENT GIVES YOU                        WHAT A STATEFULSET GIVES YOU
============================                       ==============================

Deployment "web"  ───►  ReplicaSet  ───►  Pod ┐    StatefulSet "db"      Pod
                                          web-7c-x4│    replicas=3            ─────►  db-0
                                          web-7c-yk│                   ─────►  db-1
                                          web-7c-az│                   ─────►  db-2

Pod names: random hash                              Pod names: ordinal-indexed,
IPs:       ephemeral, recycled                                 deterministic
DNS:       only the Service VIP resolves            IPs:       still ephemeral
Storage:   emptyDir, or one shared PVC              DNS:       db-0.db.ns.svc.cluster.local
Identity:  none beyond labels                                  resolves to db-0's IP
Order:     parallel everything                      Storage:   PVC data-db-0 ↔ Pod db-0 forever
                                                    Identity:  ordinal + DNS
                                                    Order:     create 0,1,2 in sequence
                                                               delete 2,1,0 in reverse
```

Could you build identity + stable storage on top of a Deployment? Technically yes — you could write a controller that names Pods deterministically and reassigns volumes. That controller would be a StatefulSet. There is no reason to reinvent it, and several reasons not to: the upstream implementation is battle-tested across ten years of operator-driven workloads (Postgres, Cassandra, Kafka, etcd, Redis, MongoDB, Elasticsearch). What the StatefulSet does *not* do is interesting and important — it does not give you leader election, failover, schema migration, version-aware rolling upgrades, or any application-specific orchestration. Those live in operators (§22). The StatefulSet gives you the substrate; the operator gives you the application.

### 1.1 The Pod-0-Is-Not-Pod-1 Axiom

The single intuition that distinguishes a stateful workload is this: the *name* of the Pod matters to the application running inside it. The Postgres process inside Pod `pg-0` reads `pg-0.pg.default.svc.cluster.local` from its own environment, advertises that as its `primary_conninfo`, and configures replication slot names from the ordinal. The application is not stateless because *its identity is a function of its name*, and its name is a function of its ordinal, and its ordinal is fixed.

This is why every workload controller in Kubernetes splits into one of two camps:

- **Identity-free controllers** (Deployment, DaemonSet, Job, ReplicaSet) produce Pods with random suffixes. The set of Pods is what matters; individual names are irrelevant.
- **Identity-bearing controllers** (StatefulSet, and a handful of operators that wrap it) produce Pods with deterministic names and 1:1 storage bindings. Each named instance is a first-class object with its own persistent state.

The rest of this chapter is a careful walk through how Kubernetes makes the second camp tractable.

### 1.2 What problems the StatefulSet does *not* solve

It is worth listing what you do *not* get from a raw StatefulSet, because the gap is exactly what operators (§22) fill:

1.  **No leader election.** The StatefulSet creates Pods in a deterministic order, but it does not promote any of them to "leader". An application that needs primary/replica semantics must implement its own election (Raft, ZooKeeper, etcd-based locks, etc.).
2.  **No automated failover.** If `pg-0` is the primary and its underlying node fails, the StatefulSet will eventually recreate `pg-0` on a new node — but it will *not* fail traffic over to `pg-1` in the meantime. That is operator territory.
3.  **No schema or data migrations.** A rolling update of the StatefulSet template runs new container images; it does not run `ALTER TABLE` or `pg_upgrade`.
4.  **No backup orchestration.** Snapshots, point-in-time recovery, WAL archiving — none of this is built in.
5.  **No quorum awareness during rollouts.** The default rollout replaces one Pod at a time, in descending ordinal order. It does not pause if doing so would lose quorum; it just keeps going.

These gaps are *not bugs*. The StatefulSet is a primitive. Putting application logic into it would make it useless for every other application. The right abstraction is "raw StatefulSet for substrate, operator on top for app-specific orchestration", and that is the layering production K8s shops converge on.

---

## 2. The Four Guarantees

The StatefulSet API contract is a list of exactly four guarantees. Internalize these and the rest of the controller's behavior is derivable.

### 2.1 Guarantee 1: Stable Hostname

Every Pod managed by a StatefulSet named `db` has a deterministic name of the form `db-<ordinal>`, where ordinal is an integer in `[0, replicas)`. The Pod's `metadata.name` is exactly that string. The Pod's `spec.hostname` is set to the same string. The hostname inside the container — what `hostname(1)` prints — is also that string, because the kubelet propagates `spec.hostname` to the UTS namespace.

```
$ kubectl get pods -l app=db
NAME    READY   STATUS    RESTARTS   AGE
db-0    1/1     Running   0          12d
db-1    1/1     Running   0          12d
db-2    1/1     Running   0          12d

$ kubectl exec db-1 -- hostname
db-1
```

Compare to a Deployment, where the Pod name is `web-<replicaset-hash>-<random-suffix>` (e.g., `web-7c5d8f9b6-x4qzm`), and the hostname is the same random string. The ReplicaSet hash changes every rollout; the random suffix changes every Pod creation. There is no stable identifier the Pod can put in its own configuration.

### 2.2 Guarantee 2: Stable Network Identity

Each Pod in a StatefulSet is reachable at a deterministic DNS name:

```
<pod-name>.<headless-service-name>.<namespace>.svc.cluster.local
```

For the StatefulSet `db` in namespace `default` with a headless Service `db`:

```
db-0.db.default.svc.cluster.local  →  10.244.1.17
db-1.db.default.svc.cluster.local  →  10.244.2.42
db-2.db.default.svc.cluster.local  →  10.244.3.88
```

The IPs change every Pod restart. The DNS names do not. Any other Pod in the cluster (and any process inside the StatefulSet itself) can dial `db-0.db` (using the standard `ndots:5` search path) and reach `db-0`, whichever node it happens to be running on. This is the contract every stateful application is built on: the cluster topology is encoded *in DNS*, not in IPs.

The headless Service is the linchpin that makes this work; we cover it in §4.

### 2.3 Guarantee 3: Stable Storage

For every entry in `spec.volumeClaimTemplates`, the controller materializes one PVC *per Pod ordinal*. The PVC name is deterministic:

```
<template-name>-<statefulset-name>-<ordinal>
```

So a template named `data` in StatefulSet `db` produces PVCs:

```
data-db-0
data-db-1
data-db-2
```

The PVC is mounted into the Pod via a `volumeMount` that references the template name. Crucially: **the PVC is not deleted when the Pod is deleted**. By default it survives Pod restarts, node failures, rolling updates, and even StatefulSet deletion (the default retention policy is `Retain`, see §6). If `db-0` is rescheduled to a different node, the kubelet on the new node will mount `data-db-0` — the *same* underlying PV that the old `db-0` was using. The Pod inherits its predecessor's data.

```
Pod db-0  ←─────── PVC data-db-0  ←─────── PV pvc-abc123  ←──── EBS vol-0a1b2c
                   (immutable binding,      (one volume,         (the actual disk)
                    survives Pod restart)    one PVC, one PV)

Pod db-1  ←─────── PVC data-db-1  ←─────── PV pvc-def456  ←──── EBS vol-1d2e3f
Pod db-2  ←─────── PVC data-db-2  ←─────── PV pvc-789xyz  ←──── EBS vol-9a8b7c
```

### 2.4 Guarantee 4: Ordered Lifecycle

Under the default `podManagementPolicy: OrderedReady`, the controller does not create Pod `N+1` until Pod `N` has reached the `Ready` condition. Conversely, it does not delete Pod `N-1` until Pod `N` has been fully deleted (i.e., disappeared from the apiserver).

```
CREATE TIMELINE (replicas: 0 → 3, OrderedReady)
================================================

T=0   create db-0  (PVC data-db-0 provisioned, Pod scheduled)
T=4   db-0 Pending → ContainerCreating → Running
T=12  db-0 Ready ✓
T=13  create db-1  (PVC data-db-1 provisioned, ...)
T=23  db-1 Ready ✓
T=24  create db-2
T=35  db-2 Ready ✓

Total time: 35s — sequential, bounded by slowest Pod's readiness probe

DELETE TIMELINE (replicas: 3 → 0)
==================================

T=0   delete db-2  (Pod gets SIGTERM, preStop hook runs, terminationGracePeriod)
T=10  db-2 fully gone from apiserver
T=11  delete db-1
T=21  db-1 fully gone
T=22  delete db-0
T=32  db-0 fully gone

Note: PVCs data-db-{0,1,2} STILL EXIST (Retain is default retention policy)
```

The contrast with `Parallel` (§7) is stark: with `Parallel`, all Pods are created concurrently. With `OrderedReady`, they march in lockstep — slow, but deterministic, and that determinism is what makes peer-discovery bootstrap scripts (§14) tractable.

### 2.5 What the guarantees do *not* cover

- **They do not pin a Pod to a specific node.** Pod `db-0` may run on `node-a` today, `node-b` tomorrow. The PV follows it via CSI attach/detach (assuming the storage class supports cross-zone or cross-node attach; see §17).
- **They do not guarantee atomicity across replicas.** During a rolling update, the cluster is briefly running mixed image versions. Applications must tolerate this; the StatefulSet does not provide cluster-wide transactions.
- **They do not guarantee data consistency.** If your application's storage layer is asynchronously replicated, a Pod restart can resurrect with stale data relative to its peers. The StatefulSet does not solve that — the application or operator does.

---

## 3. Pod Identity: Names, Hostnames, FQDNs

The full identity contract of a StatefulSet Pod has four layers, each derived deterministically from the StatefulSet name and the ordinal:

```
LAYER                      VALUE                              DERIVED FROM
==========================================================================================
ordinal index              0, 1, 2, …, replicas-1             StatefulSet spec.replicas
metadata.name              <sts>-<ordinal>                    sts name + ordinal
spec.hostname              <sts>-<ordinal>                    same as metadata.name
spec.subdomain             <headless-service-name>            sts spec.serviceName
FQDN (cluster DNS)         <hostname>.<subdomain>.<ns>.svc.cluster.local
                           = <sts>-<ordinal>.<svc>.<ns>.svc.cluster.local
```

For a StatefulSet:

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
  namespace: prod
spec:
  serviceName: db          # MUST be a headless Service
  replicas: 3
  selector:
    matchLabels: { app: db }
  template:
    metadata:
      labels: { app: db }
    spec:
      containers:
      - name: postgres
        image: postgres:16
```

The three Pods are:

```
db-0:  hostname=db-0,  subdomain=db,  FQDN=db-0.db.prod.svc.cluster.local
db-1:  hostname=db-1,  subdomain=db,  FQDN=db-1.db.prod.svc.cluster.local
db-2:  hostname=db-2,  subdomain=db,  FQDN=db-2.db.prod.svc.cluster.local
```

### 3.1 Where the controller sets these fields

In the source tree, the assignment lives in `pkg/controller/statefulset/stateful_set_utils.go`. The function `getPodName(set *apps.StatefulSet, ordinal int) string` is the single source of truth for the naming scheme:

```go
// (simplified, illustrative)
func getPodName(set *apps.StatefulSet, ordinal int) string {
    return fmt.Sprintf("%s-%d", set.Name, ordinal)
}

func newStatefulSetPod(set *apps.StatefulSet, ordinal int) *v1.Pod {
    pod, _ := controller.GetPodFromTemplate(&set.Spec.Template, set, ...)
    pod.Name = getPodName(set, ordinal)
    pod.Namespace = set.Namespace
    initIdentity(set, pod)
    updateStorage(set, pod)
    return pod
}

func initIdentity(set *apps.StatefulSet, pod *v1.Pod) {
    pod.Spec.Hostname = pod.Name
    pod.Spec.Subdomain = set.Spec.ServiceName
    pod.Labels[apps.StatefulSetPodNameLabel] = pod.Name
    pod.Annotations[apps.PodIndexLabel] = strconv.Itoa(getOrdinal(pod))
}
```

The Pod is labeled with `statefulset.kubernetes.io/pod-name=<sts>-<ordinal>` and (in newer versions) annotated with `apps.kubernetes.io/pod-index=<ordinal>`. The labels are how the controller finds its Pods on every reconcile; the index annotation/label is how downstream tools (kube-state-metrics, custom controllers) can discover ordinals without parsing names.

### 3.2 Hostname propagation to the container

The kubelet, when constructing the PodSandbox via CRI, passes `pod.Spec.Hostname` as the sandbox hostname. Inside the container, `gethostname(2)` returns `db-0`. That value is reflected in:

- `hostname(1)`'s output
- `/etc/hostname` in the container's mount namespace
- `$HOSTNAME` environment variable (set by the shell, not by Kubernetes)
- Many application self-identification paths (`pg_basebackup --slot=$HOSTNAME`, etcd's `--name=$HOSTNAME`, etc.)

This is the bridge between Kubernetes identity and the application: the application reads its own hostname and uses that ordinal-bearing string to construct its peer URLs, replication slot names, partition assignments, and so on.

### 3.3 Why ordinals are integers, contiguous, and bounded by `replicas`

A StatefulSet's ordinals are always `[0, replicas)`. When you scale from 3 → 5, you get `db-3, db-4` next, in that order. When you scale 5 → 3, you remove `db-4, db-3`, in that order (highest first; §12). You never get a "gap" — `db-0, db-2, db-3` is not a possible state. There is no "ordinal 1 is missing, please fill it in" state.

This invariant is what makes the bootstrap problem (§14) tractable. Any process inside the cluster can compute the full set of peers from just `(statefulset name, replicas, headless service, namespace)`:

```python
peers = [f"{sts}-{i}.{svc}.{ns}.svc.cluster.local" for i in range(replicas)]
```

That four-line list is the entire cluster topology, derivable without an API call.

### 3.4 Start-from-non-zero ordinals (1.27+ alpha → 1.31+ beta)

KEP-3335 adds `spec.ordinals.start` to allow ordinals to begin at a value other than 0. This is useful for *migrating* a workload from one StatefulSet to another: you create a new StatefulSet with `ordinals.start: 3` and scale the old one down. Each Pod in the new STS has a unique name that doesn't collide with the old one's `db-0/db-1/db-2`, and the headless Service can dispatch traffic across both.

```yaml
spec:
  ordinals:
    start: 3
  replicas: 3
# Pods: db-3, db-4, db-5
```

This is opt-in and changes the *naming* but not the *contiguity*: ordinals are still `[start, start+replicas)`.

---

## 4. Headless Services: The DNS Contract

A regular `ClusterIP` Service gives you one stable virtual IP (the ClusterIP), and clients connect to that VIP; kube-proxy DNATs the packet to a random backend Pod. The destination of any given connection is unpredictable. That is fine for stateless workloads and useless for stateful ones, because you cannot "talk to `db-1` specifically" — you can only "talk to *some* `db` Pod".

A **headless Service** is what you get when you set `clusterIP: None`. CoreDNS treats this specially:

- It does *not* publish an `A` record for the Service name pointing at a VIP (because there is no VIP).
- Instead, it publishes one `A` record per ready Pod in the Service's selector, *and* one `A` record per Pod under the per-Pod subdomain.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: db
  namespace: prod
spec:
  clusterIP: None           # headless
  selector:
    app: db
  ports:
  - name: postgres
    port: 5432
    targetPort: 5432
```

With three ready Pods `db-0` (IP 10.244.1.17), `db-1` (10.244.2.42), `db-2` (10.244.3.88), CoreDNS publishes:

```
;; Round-robin list of all ready Pods (the "service" itself)
db.prod.svc.cluster.local.        IN  A  10.244.1.17
db.prod.svc.cluster.local.        IN  A  10.244.2.42
db.prod.svc.cluster.local.        IN  A  10.244.3.88

;; Per-Pod records (identity)
db-0.db.prod.svc.cluster.local.   IN  A  10.244.1.17
db-1.db.prod.svc.cluster.local.   IN  A  10.244.2.42
db-2.db.prod.svc.cluster.local.   IN  A  10.244.3.88

;; SRV records for service discovery
_postgres._tcp.db.prod.svc.cluster.local.  IN  SRV  10 33 5432 db-0.db.prod.svc.cluster.local.
_postgres._tcp.db.prod.svc.cluster.local.  IN  SRV  10 33 5432 db-1.db.prod.svc.cluster.local.
_postgres._tcp.db.prod.svc.cluster.local.  IN  SRV  10 33 5432 db-2.db.prod.svc.cluster.local.
```

The behavior is driven by the EndpointSlice controller: every ready Pod becomes an endpoint in the EndpointSlice for the headless Service, and CoreDNS reads the EndpointSlice (via the Kubernetes API) to construct A/SRV records.

### 4.1 Why headless, not ClusterIP

There are two reasons to use a headless Service for a StatefulSet, and you need both:

1.  **Per-Pod A records.** Only a headless Service generates `<pod>.<svc>` A records. A ClusterIP Service publishes only `<svc>` → VIP. Without the per-Pod records, `db-0.db.prod.svc.cluster.local` does not resolve, and the entire stable-identity guarantee evaporates.

2.  **Direct Pod connections, no load balancing.** Stateful protocols (Raft, replication streams, consensus messages) need to connect to *a specific peer*, not to "any backend". A headless Service hands the application the raw Pod IPs and lets the application choose.

```
QUERY: db-0.db.prod.svc.cluster.local
=========================================

CoreDNS plugin chain:
   kubernetes plugin → consult API/EndpointSlice
       sees: headless Service db.prod, Pod db-0 has IP 10.244.1.17
       returns: A 10.244.1.17

QUERY: db.prod.svc.cluster.local  (the "service" name itself)
=============================================================

CoreDNS plugin chain:
   kubernetes plugin → consult API/EndpointSlice
       sees: headless Service db.prod with 3 ready endpoints
       returns: A 10.244.1.17, A 10.244.2.42, A 10.244.3.88
                (randomized order)
```

The headless-with-listing behavior is also how peer discovery works without an external service registry. When `db-0` boots and wants to find its peers, it queries `db.prod.svc.cluster.local`, gets a list of all ready Pod IPs, and connects to each.

### 4.2 The `spec.serviceName` field

The StatefulSet's `spec.serviceName` must reference the *name* of a headless Service in the same namespace. The controller does not create the Service for you; you create it as a separate manifest. The controller does, however, refuse to create Pods if `spec.serviceName` is empty.

There is no enforcement at admission that the referenced Service is actually headless. If you point `spec.serviceName` at a regular ClusterIP Service, the StatefulSet will happily create Pods with `spec.subdomain=<svc>` — but CoreDNS will not generate the per-Pod A records, and `db-0.db.ns.svc.cluster.local` will return NXDOMAIN. The application will fail in mysterious ways. **Always make the Service headless.**

### 4.3 What "ready" means for DNS

A Pod is included in the headless Service's EndpointSlice — and therefore its A record is published — only when:

1.  All probes pass: startup, then readiness.
2.  The Pod's `status.conditions[ContainersReady]` is True.
3.  *Additionally*, the Pod is included in the headless Service's EndpointSlice even when not ready, if the Service has `publishNotReadyAddresses: true`.

That last clause matters: stateful bootstrap scripts (§14, §16) often need to discover peers *before* the peers are ready. If `db-0` and `db-1` and `db-2` all start at once and each waits for the others to be ready before announcing itself, the cluster deadlocks. Setting `publishNotReadyAddresses: true` on the headless Service breaks the deadlock by publishing A records for not-yet-ready Pods:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: db
spec:
  clusterIP: None
  publishNotReadyAddresses: true       # critical for bootstrap
  selector: { app: db }
  ports: [{ port: 5432 }]
```

With `OrderedReady` pod management, this is usually unnecessary (Pods are created one at a time, so peers are always ready before the next Pod looks them up). With `Parallel`, or with applications that do post-bootstrap peer rediscovery, it's important.

### 4.4 `clusterIP: None` is one of three values

For completeness, `clusterIP` has three legal states:

| `clusterIP`     | Meaning                                                                                  |
|-----------------|------------------------------------------------------------------------------------------|
| `<unset>`       | Allocate a VIP from the Service CIDR. Standard ClusterIP Service.                        |
| `None`          | No VIP. Per-Pod DNS A records. **Headless.** This is what StatefulSets need.             |
| `<an IP>`       | Use this specific VIP. Same as unset, but pinned. Rare; used for "well-known" Services.  |

The wrong choice here is the most common StatefulSet misconfiguration; we revisit it in pitfalls (§30).

---

## 5. `volumeClaimTemplates`: Per-Pod Storage Materialization

`spec.volumeClaimTemplates` is the field that distinguishes a StatefulSet from every other Pod-producing controller. It is a list of PVC templates; the controller materializes *one PVC per Pod ordinal* from each template.

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
spec:
  serviceName: db
  replicas: 3
  selector:
    matchLabels: { app: db }
  template:
    metadata:
      labels: { app: db }
    spec:
      containers:
      - name: postgres
        image: postgres:16
        volumeMounts:
        - name: data            # references the template name
          mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
  - metadata:
      name: data                # template name; PVC name = data-db-<ordinal>
    spec:
      accessModes: [ ReadWriteOnce ]
      storageClassName: gp3
      resources:
        requests:
          storage: 100Gi
```

For each ordinal `i ∈ [0, replicas)`, the controller creates a PVC named `data-db-i` with the spec above (modulo ordinal-specific labels). The PVC is owned by the StatefulSet via an `ownerReference` (with `blockOwnerDeletion: true` so the GC respects the order).

```
Pod db-0 ──spec.volumes[data].persistentVolumeClaim.claimName=data-db-0──► PVC data-db-0
Pod db-1 ──spec.volumes[data].persistentVolumeClaim.claimName=data-db-1──► PVC data-db-1
Pod db-2 ──spec.volumes[data].persistentVolumeClaim.claimName=data-db-2──► PVC data-db-2

(The controller injects the claimName at Pod-template materialization time)
```

### 5.1 The PVC name formula

For a StatefulSet `<sts>` and a `volumeClaimTemplate` named `<tmpl>`, the PVC for ordinal `i` is named:

```
<tmpl>-<sts>-<i>
```

This is in `pkg/controller/statefulset/stateful_set_utils.go`:

```go
func getPersistentVolumeClaimName(set *apps.StatefulSet, claim *v1.PersistentVolumeClaim, ordinal int) string {
    return fmt.Sprintf("%s-%s-%d", claim.Name, set.Name, ordinal)
}
```

If you have two templates `data` and `wal` in the same StatefulSet `db`, the controller creates *two* PVCs per Pod:

```
data-db-0, wal-db-0   (mounted into db-0)
data-db-1, wal-db-1   (mounted into db-1)
data-db-2, wal-db-2   (mounted into db-2)
```

Each PVC is independent; they can use different StorageClasses, sizes, and access modes.

### 5.2 PVC immutability

`spec.volumeClaimTemplates` is **immutable after StatefulSet creation**. You cannot change the storage size, the storage class, the access mode, or the template name without recreating the StatefulSet. This is enforced by the apiserver's `pkg/registry/apps/statefulset/strategy.go` ValidateUpdate:

```go
// Disallow changes to volumeClaimTemplates, serviceName, selector, podManagementPolicy
allErrs = append(allErrs, apivalidation.ValidateImmutableField(
    newStatefulSet.Spec.VolumeClaimTemplates,
    oldStatefulSet.Spec.VolumeClaimTemplates,
    field.NewPath("spec", "volumeClaimTemplates"))...)
```

The exception is *PVC size expansion* (§20), which is done by editing the PVC directly, not the template. The template's size is the *initial* size for new ordinals; existing PVCs keep their (possibly expanded) size.

This immutability is why you should treat the StatefulSet spec as a contract: get it right the first time, or plan for a recreate. There is a workaround for adding a new template (orphan-delete the STS and recreate; the existing PVCs are reused), but it's surgery, not a feature.

### 5.3 The crucial property: PVCs survive Pod deletion

The default behavior — and arguably the entire point of StatefulSet — is that PVCs *are not deleted* when their Pods are deleted. If you `kubectl delete pod db-0`, the controller will:

1.  Notice that ordinal 0 is missing.
2.  Recreate Pod `db-0` from the template.
3.  Bind it to the existing PVC `data-db-0`, which still exists.
4.  When the new Pod is scheduled, the kubelet mounts the same underlying PV.
5.  Inside the new Pod, the application reads the data that the old Pod wrote.

This is what makes "Pod restart" non-destructive for stateful workloads. The Pod is ephemeral; the PVC is permanent. The PV is bound 1:1 to the PVC (which is bound 1:1 to the ordinal), so the data follows the *ordinal*, not the *Pod*.

The only way to lose the data on a Pod restart is to also delete the PVC — which is a separate, explicit action, controlled by the retention policy (§6).

### 5.4 The full chain: Pod → PVC → PV → backing volume

```
┌─────────────────────────────────────────────────────────────────────┐
│   PHYSICAL                  KUBERNETES                  WORKLOAD     │
│                                                                      │
│   EBS volume                ─────► PV pvc-abc123                    │
│   (vol-0a1b2c…)                    ├─ capacity: 100Gi               │
│                                    ├─ accessMode: RWO                │
│                                    ├─ reclaimPolicy: Delete          │
│                                    └─ claimRef: data-db-0            │
│                                              │                       │
│                                              ▼                       │
│                                    PVC data-db-0                    │
│                                    ├─ namespace: prod                │
│                                    ├─ storageClassName: gp3          │
│                                    ├─ ownerRef: db StatefulSet       │
│                                    │  (blockOwnerDeletion=true if    │
│                                    │   whenDeleted=Delete)           │
│                                    └─ volumeName: pvc-abc123         │
│                                              │                       │
│                                              ▼                       │
│                                    Pod db-0                          │
│                                    spec.volumes:                     │
│                                    - name: data                      │
│                                      persistentVolumeClaim:          │
│                                        claimName: data-db-0          │
│                                                                      │
│                                    containers[0].volumeMounts:       │
│                                    - name: data                      │
│                                      mountPath: /var/lib/postgres…   │
└─────────────────────────────────────────────────────────────────────┘
```

Each link in the chain has its own lifecycle. The PV's reclaimPolicy controls what happens to the backing volume when the PVC is deleted. The PVC retention policy on the StatefulSet (§6) controls what happens to the PVC when the Pod is deleted. The StatefulSet's lifecycle controls what happens to the Pods.

---

## 6. PVC Retention Policy (1.27+ GA)

`spec.persistentVolumeClaimRetentionPolicy` is the explicit declaration of what should happen to PVCs when the parent StatefulSet is deleted, or when the StatefulSet is scaled down. It has two fields, each with two options:

```yaml
spec:
  persistentVolumeClaimRetentionPolicy:
    whenDeleted: Retain | Delete
    whenScaled: Retain | Delete
```

This feature went through `StatefulSetAutoDeletePVC` feature gate (alpha 1.23, beta 1.27, GA 1.32). Before GA, the only behavior was `whenDeleted=Retain, whenScaled=Retain` — PVCs were never automatically deleted. With the feature gate enabled (or in GA versions), you can opt into automatic GC.

### 6.1 The four combinations

```
                  whenScaled=Retain                whenScaled=Delete
              ┌─────────────────────────────┬─────────────────────────────┐
              │                             │                             │
whenDeleted   │ DEFAULT, SAFEST.            │ Scale down deletes the      │
=Retain       │ Nothing is ever auto-       │ excess PVC; deleting the    │
              │ deleted. Useful for         │ STS leaves all current      │
              │ databases, message queues,  │ PVCs alone.                 │
              │ anything with irreplaceable │ Useful for elastic clustered│
              │ state.                      │ caches you can re-shard.   │
              │                             │                             │
              ├─────────────────────────────┼─────────────────────────────┤
              │                             │                             │
whenDeleted   │ Scale-down keeps PVCs       │ Most aggressive: PVCs are   │
=Delete       │ around (so scaling back     │ GC'd on both scale-down     │
              │ up reuses data), but        │ and STS deletion. Use only  │
              │ deleting the STS deletes    │ if your data is 100%        │
              │ everything.                 │ reproducible from scratch.  │
              │                             │                             │
              └─────────────────────────────┴─────────────────────────────┘
```

### 6.2 The default: `Retain / Retain`

If you omit `persistentVolumeClaimRetentionPolicy` entirely, both fields default to `Retain`. PVCs are never automatically deleted by the StatefulSet controller. Even if you `kubectl delete statefulset db`, the PVCs `data-db-0`, `data-db-1`, `data-db-2` remain — and so do the underlying PVs (subject to the PV's own `reclaimPolicy`).

This is the safest default for stateful data. The downside: if you don't clean up by hand, you get orphan PVCs and (if the PV has `reclaimPolicy: Delete`) eventually orphan cloud volumes that you keep paying for. Auditing for orphan PVCs is part of cluster hygiene.

### 6.3 Per-PVC owner references

The mechanism behind the retention policy is the `ownerReferences` on each PVC. The controller maintains them dynamically:

```yaml
# When whenScaled=Delete and the PVC is for an ordinal >= current replicas:
metadata:
  name: data-db-3
  ownerReferences:
  - apiVersion: apps/v1
    kind: StatefulSet
    name: db
    uid: abc123…
    controller: false               # not the controller, just a back-reference
    blockOwnerDeletion: true        # block STS deletion until PVC is gone
```

The garbage collector (ch 36) cascades: when the StatefulSet is deleted, any PVC with an ownerReference back to it is eligible for deletion. The retention policy decides whether the controller maintains those ownerReferences.

The decision logic lives in `pkg/controller/statefulset/stateful_set_utils.go` (`isClaimOwnerUpToDate`, `claimOwnerMatchesSetAndPod`). For each PVC, the controller checks:

```
if whenDeleted == Delete:
    PVC.ownerReferences must include the StatefulSet (block-on-delete)
else:
    PVC.ownerReferences must NOT include the StatefulSet

if whenScaled == Delete AND ordinal >= replicas:
    PVC.ownerReferences must include the StatefulSet
else (whenScaled == Retain OR ordinal < replicas):
    PVC.ownerReferences must NOT include the StatefulSet (for scale-related ownership)
```

Mismatches trigger a PATCH on the PVC's ownerReferences. The next reconcile re-evaluates, and the GC controller handles deletion if a PVC's ownerReferences list points to a now-gone StatefulSet.

### 6.4 When to choose each combination

| Combination                     | When to use                                                                |
|---------------------------------|-----------------------------------------------------------------------------|
| `Retain / Retain` (default)     | Production stateful databases, any data you cannot recreate. Always.        |
| `Retain / Delete`               | Sharded systems where scaling down means "this shard is gone forever".      |
| `Delete / Retain`               | Development clusters, test fixtures: scale-down for cost savings, full clean on delete. |
| `Delete / Delete`               | Stateless-but-stateful: caches with no persistence guarantee (e.g., Redis as cache). |

In production, the consensus is: keep the default unless you have an operator that knows what it's doing. Operators (§22) often manage ownership themselves and override these settings.

---

## 7. Pod Management Policy: `OrderedReady` vs `Parallel`

`spec.podManagementPolicy` controls the ordering of Pod creation, deletion, and scaling. Two values are legal:

- **`OrderedReady`** (default): Create Pod `N+1` only after Pod `N` is `Ready`. Delete Pod `N-1` only after Pod `N` is fully gone (deleted from the apiserver, not just terminating).
- **`Parallel`**: Create and delete Pods concurrently, with no ordering constraint.

The choice is **immutable after StatefulSet creation**, like `volumeClaimTemplates` and `serviceName`. Choose it carefully up front.

### 7.1 `OrderedReady`: the default

```
Scale 0 → 3 with OrderedReady:

  T=0   Controller observes desired=3, actual=0
        Creates db-0 (PVC data-db-0 + Pod db-0)
  T=2   db-0 Pending → ContainerCreating
  T=8   db-0 Running but not Ready (readinessProbe still failing)
  T=15  db-0 Ready ← gate passes
  T=15  Controller creates db-1
  T=23  db-1 Ready
  T=23  Controller creates db-2
  T=31  db-2 Ready

  Total: 31s, single in-flight Pod creation at any moment
```

```
Scale 3 → 0 with OrderedReady:

  T=0   Controller observes desired=0, actual=3
        Deletes db-2 (Pod gets SIGTERM)
  T=10  db-2 deletion completes (PVC retained or deleted per policy)
  T=10  Controller deletes db-1
  T=20  db-1 gone
  T=20  Controller deletes db-0
  T=30  db-0 gone

  Total: 30s, reverse ordinal order
```

Why OrderedReady is the default: it gives the application a deterministic environment to bootstrap into. When `db-1` starts and queries DNS for `db-0.db`, the answer is guaranteed to be a *ready* Pod, not a Pending one. Many cluster-bootstrap scripts (etcd in particular) assume earlier ordinals are already operational.

The cost is throughput. Scaling from 0 to 100 with OrderedReady takes 100 × (probe-passing-time) seconds. For a slow-starting database, that can be tens of minutes.

### 7.2 `Parallel`: when bootstrap is symmetric

`Parallel` mode creates all desired Pods concurrently, in no particular order. The controller still names them deterministically (`db-0, db-1, db-2`), still creates the matching PVCs, still maintains the headless DNS records — but it does *not* gate `db-1` on `db-0` being ready.

```
Scale 0 → 3 with Parallel:

  T=0   Controller observes desired=3, actual=0
        Creates db-0, db-1, db-2 in parallel
  T=2   All three Pending
  T=8   All three Running, none Ready
  T=15  All three Ready (roughly simultaneously)

  Total: 15s (same as OrderedReady would take for one Pod)
```

Parallel is the right choice when the application has *symmetric* peer discovery: every replica boots, finds its peers via DNS, and joins the cluster regardless of arrival order. Cassandra is the canonical example:

- All nodes start, contact the configured seed nodes (which can include themselves), and join the ring.
- The order of arrival affects token assignment slightly but does not deadlock the cluster.
- Faster scale-up is operationally valuable for capacity events.

Compare to etcd, which uses `--initial-cluster` with the full set of peer URLs at bootstrap. The first node to bring up the cluster needs its peer URLs to resolve; the second and third nodes need to find each other. With `OrderedReady`, the sequencing is implicit. With `Parallel`, you need `publishNotReadyAddresses: true` on the headless Service so that A records exist even before peers are ready.

### 7.3 The decision matrix

```
                            Bootstrap dependency
                            on prior ordinals
                            ─────────────────────
                            Yes                   No
                  ┌─────────────────────┬─────────────────────┐
  Fast scale-up   │                     │                     │
  required?       │   Risky:            │   Parallel ✓        │
  Yes             │   You probably want │   (Cassandra,       │
                  │   to fix the        │    sharded systems  │
                  │   application,      │    with own seeds)  │
                  │   not the policy    │                     │
                  ├─────────────────────┼─────────────────────┤
  No              │   OrderedReady ✓    │   Either, but       │
                  │   (etcd, Postgres-  │   OrderedReady is   │
                  │    replica chains,  │   more predictable  │
                  │    Kafka KRaft)     │                     │
                  └─────────────────────┴─────────────────────┘
```

### 7.4 What `Parallel` does *not* do

`Parallel` only affects creation and deletion timing. It does **not** affect rolling-update ordering: even with `Parallel` podManagementPolicy, the update strategy (§9) still processes Pods in descending ordinal order by default. The two policies are orthogonal:

- `podManagementPolicy` = how Pods are *created and deleted* during scale operations.
- `updateStrategy` = how Pods are *replaced* during a template change.

If you want concurrent updates *as well*, you set `spec.updateStrategy.rollingUpdate.maxUnavailable` (§11).

---

## 8. The Reconcile Algorithm

The StatefulSet controller's reconcile logic is in `pkg/controller/statefulset/stateful_set_control.go`, specifically the `UpdateStatefulSet` method. The high-level algorithm is straightforward; the subtleties are in the edge cases.

```
ON EACH RECONCILE (triggered by Pod, PVC, or StatefulSet event):

1.  Get the StatefulSet (sts)
2.  List all Pods with selector matching sts.Spec.Selector
3.  Sort Pods by ordinal (extracted from name)
4.  Build a sparse array of size sts.Spec.Replicas
5.  Slot each existing Pod into the array by ordinal
6.  Reconcile slot by slot:

    for i in 0..replicas:
        slot = pods[i]
        if slot is missing:
            create Pod for ordinal i (which also creates the PVCs if needed)
            if OrderedReady: return (wait for ready)
        elif slot is Pending or Failed or Terminating:
            if OrderedReady: return (wait for it to stabilize or get cleaned up)
        elif slot is Running but not Ready:
            if OrderedReady: return (wait for ready)
        elif slot's template hash doesn't match desired:
            (rolling update: handled by update strategy, §9)

    for i in replicas..max(observed ordinal):
        slot = pods[i]
        if slot exists:
            delete Pod (which may or may not delete PVC per retention policy)
            if OrderedReady: return (wait for deletion to complete)

7.  Reconcile PVCs:
    - For each ordinal in [0, replicas): ensure PVC exists with correct ownerRefs
    - For each ordinal beyond replicas: update PVC ownerRefs per whenScaled policy

8.  Update sts.Status (replicas, readyReplicas, currentReplicas, updatedReplicas, etc.)
```

### 8.1 The algorithm in pseudo-code

```python
def reconcile(sts):
    pods = list_pods(selector=sts.spec.selector)
    pods_by_ordinal = {ordinal_of(p): p for p in pods}

    desired = sts.spec.replicas
    observed_max = max(pods_by_ordinal.keys(), default=-1)

    # --- create missing ordinals in [0, desired) ---
    for i in range(desired):
        if i not in pods_by_ordinal:
            create_pvcs_for_ordinal(sts, i)
            create_pod_for_ordinal(sts, i)
            if sts.spec.podManagementPolicy == "OrderedReady":
                return   # wait for ready, requeue on next event
        elif not is_ready(pods_by_ordinal[i]):
            if sts.spec.podManagementPolicy == "OrderedReady":
                return   # wait

    # --- delete extra ordinals in [desired, observed_max] ---
    for i in range(observed_max, desired - 1, -1):  # descending!
        if i in pods_by_ordinal:
            delete_pod(pods_by_ordinal[i])
            if sts.spec.podManagementPolicy == "OrderedReady":
                return   # wait for deletion to complete

    # --- reconcile PVCs ---
    for i in range(observed_max + 1):
        for vct in sts.spec.volumeClaimTemplates:
            pvc_name = f"{vct.metadata.name}-{sts.name}-{i}"
            reconcile_pvc_owner_refs(pvc_name, sts, ordinal=i)

    # --- rolling update (if template changed) ---
    rolling_update(sts, pods_by_ordinal)

    # --- status ---
    update_status(sts, pods_by_ordinal)
```

The actual code is more careful about: detecting Pods that are terminating (don't recreate the ordinal yet, wait for cleanup), handling `metadata.uid` mismatches (a Pod with the right name but the wrong UID is a leftover from a previous incarnation), and reconciling owner references on PVCs in lockstep.

### 8.2 Why descending order for deletion

When scaling down from 3 → 2, the controller deletes Pod `db-2`, not `db-0`. The reasoning:

- `db-0`, `db-1` are the "lower" ordinals; their PVCs are most likely to be active data shards.
- `db-2` was the *most recently created* (in OrderedReady; in Parallel it doesn't matter as much), so its loss is the least disruptive in expectation.
- By convention, ordinal 0 is often the primary (see §27); preserving it across scale-down is desirable.

The same convention applies to rolling updates (§9): descend from highest to lowest, leaving ordinal 0 for last.

### 8.3 The role of the workqueue

The StatefulSet controller, like every Kubernetes controller, uses a workqueue (ch 08). When any of these events fires, the relevant StatefulSet key is enqueued:

- A Pod owned by an STS changes state (Add/Update/Delete handlers on the Pod informer).
- A PVC owned by an STS changes (Add/Update/Delete on the PVC informer).
- The StatefulSet itself is updated.

The workqueue is rate-limited; the controller processes one key at a time per worker. A single reconcile loop can return early (e.g., "waiting for ready") and the next event will re-enqueue. This makes the controller level-triggered: it computes what state it wants and converges toward it, regardless of which event triggered the wakeup.

### 8.4 The Pod controller revision history

To support rolling updates and rollback, the StatefulSet controller maintains a list of `ControllerRevision` objects (the same mechanism DaemonSets use). Each revision is a snapshot of `spec.template` plus its name (e.g., `db-6b8d7c5f4`).

The controller stores up to `spec.revisionHistoryLimit` revisions (default 10). When a Pod is created or recreated, it's labeled with the *current revision*:

```
labels:
  controller-revision-hash: 6b8d7c5f4
```

The reconcile loop uses this label to distinguish "Pod is up-to-date" from "Pod is on old revision and needs replacing". Rollback is done by editing the StatefulSet's spec to match a previous revision.

---

## 9. Rolling Updates: Descending Ordinal Order

`spec.updateStrategy` controls how the StatefulSet handles a template change. Two strategies are legal:

- **`RollingUpdate`** (default): The controller automatically replaces Pods one at a time, in descending ordinal order.
- **`OnDelete`**: The controller does nothing; you delete Pods manually, and the controller recreates them from the new template. Useful for applications where you want explicit, human-driven rollouts.

### 9.1 `RollingUpdate`: the automatic case

Suppose you have a 3-replica StatefulSet running `postgres:15` and you change the image to `postgres:16`. With `RollingUpdate`:

```
T=0   You: kubectl set image sts/db postgres=postgres:16
T=0   Controller observes template change; new revision created (e.g., db-7a2b...)
T=0   db-0, db-1, db-2 are still on revision db-6b8d... (the old one)

T=1   Controller picks the HIGHEST ordinal not on the new revision: db-2
      Delete db-2 (preStop hook → SIGTERM → terminationGracePeriod)
T=10  db-2 gone
T=10  Controller creates new db-2 with image postgres:16 (same PVC data-db-2)
T=15  db-2 Pending → Running, but NOT yet Ready (PG starts up, checks data, etc.)
T=45  db-2 Ready ✓

T=46  Controller picks next: db-1
      Delete, recreate, wait
T=90  db-1 Ready ✓

T=91  Controller picks db-0 (LAST)
      Delete, recreate, wait
T=135 db-0 Ready ✓

T=135 All replicas updated. Status: updatedReplicas=3, currentRevision=db-7a2b
```

The descending order matters for two reasons:

1.  **Ordinal 0 is often the leader/primary.** By convention (and by many operators' choice), `db-0` is the primary; the higher ordinals are replicas/standbys. Updating standbys first keeps the primary alive longest. If `db-0`'s update fails, the rest of the cluster is still serving traffic.
2.  **The update is downstream of the data flow.** In a chained-replication setup (`db-0` → `db-1` → `db-2`), replicas at the end of the chain can be replaced without affecting upstream nodes.

### 9.2 The replacement is *not in-place*

A common misconception: "the Pod's image is updated; the container restarts." That is what `Deployment` does (sort of — it creates a new ReplicaSet and a new Pod with a new name). What StatefulSet does is more direct: it *deletes* the Pod entirely, then *creates a new Pod with the same name and same PVC* from the new template.

```
Before update:                          After update:
  Pod db-0 (uid=u1, podIP=10.244.1.17,    Pod db-0 (uid=u2, podIP=10.244.1.99,
            image=pg:15)                            image=pg:16)
  PVC data-db-0  ←──── bound              PVC data-db-0  ←──── still bound, same PV
  PV pvc-abc123                           PV pvc-abc123  ←──── same data on disk
```

- The Pod's UID changes (it's a new object).
- The Pod's IP changes (it's rescheduled, a new podIP is allocated).
- The PVC is unchanged. The data on disk is unchanged.
- The DNS name `db-0.db.ns.svc.cluster.local` resolves to the new IP within seconds.

The application must tolerate the IP change. If it's caching peer IPs (instead of re-resolving DNS), that's a bug. If it's using long-lived TCP connections, those connections break and need to reconnect.

### 9.3 `OnDelete`: manual rollouts

```yaml
spec:
  updateStrategy:
    type: OnDelete
```

With `OnDelete`, the controller stores the new template but does not touch existing Pods. To roll out, you `kubectl delete pod db-2`; the controller recreates `db-2` from the new template; you wait for ready; you `kubectl delete pod db-1`; and so on.

`OnDelete` is the right choice when:

- The application requires a custom verification step between Pods (e.g., run a schema migration after each upgrade).
- The operator wants full control over the order (e.g., update non-leader first, then trigger a leader failover, then update the old leader).
- Some upgrades are not safe to do automatically (major version bumps with on-disk format changes).

Operators (§22) typically use `OnDelete` and orchestrate the rollout themselves, calling `kubectl delete pod` (or the equivalent client-go DELETE) in their own controller logic.

### 9.4 Update failure and `currentRevision` vs `updateRevision`

The StatefulSet's status tracks two revisions:

```yaml
status:
  currentRevision: db-6b8d7c5f4    # revision of the "stable" Pods (lower ordinals)
  updateRevision: db-7a2bc9d31     # revision being rolled out (higher ordinals)
  currentReplicas: 1               # Pods at currentRevision (db-0 only)
  updatedReplicas: 2               # Pods at updateRevision (db-1, db-2)
  replicas: 3
  readyReplicas: 3
```

When the rollout completes, `currentRevision = updateRevision`. If a Pod fails its readiness probe under the new revision, the controller stops there; no further Pods are updated. The mixed-version state can persist indefinitely until you fix the issue.

To roll back: edit the template to match `currentRevision`, or use `kubectl rollout undo statefulset/db`. The controller will replace the high-ordinal Pods (running the new revision) with Pods running the old one — same descending order.

---

## 10. Partitioned Rollout: Canary for Stateful Workloads

`spec.updateStrategy.rollingUpdate.partition` is a powerful, often-overlooked feature: it tells the controller to update only Pods with ordinal **≥ partition**. Everything below the partition is left untouched.

```yaml
spec:
  updateStrategy:
    type: RollingUpdate
    rollingUpdate:
      partition: 5      # only update ordinals 5, 6, 7, ...
```

For a 10-replica StatefulSet with `partition: 5`, an image change causes `db-9, db-8, db-7, db-6, db-5` to be updated (in descending order); `db-4` through `db-0` stay on the old revision.

### 10.1 The canary pattern

```
INITIAL STATE: 10 replicas, all on revision v1, partition: 10 (effectively no rollout)

  db-0 ... db-9 [all v1]

STEP 1: Update template to v2. Set partition: 9 (only db-9 will be updated).

  db-0 ... db-8 [v1]    db-9 [v2 ← canary]

STEP 2: Observe db-9. Does it serve traffic correctly? Are metrics healthy?

STEP 3: If happy, reduce partition: 7 (db-7, db-8, db-9 will be on v2).

  db-0 ... db-6 [v1]    db-7, db-8, db-9 [v2]

STEP 4: Continue reducing partition gradually.

  partition: 5  →  db-5..db-9 on v2
  partition: 3  →  db-3..db-9 on v2
  partition: 0  →  all replicas on v2 (full rollout)
```

Setting `partition: 0` (the default) means the rollout proceeds through every ordinal. Setting `partition: <replicas>` (or higher) means no rollout happens.

### 10.2 Why this matters for stateful workloads

For a stateless Deployment, canary deployments are done with two ReplicaSets (one running v1, one running v2) and a Service that load-balances across both. The canary fraction is the count of v2 Pods divided by total Pods.

For a stateful workload, you cannot do this naively — each replica has its own state, and the application is sensitive to which ordinal is on which version. You also cannot "redirect 5% of traffic to v2" because traffic is going to specific replicas by name (DNS), not load-balanced.

Partitioned rollout is the StatefulSet equivalent: you canary by *ordinal*. One specific Pod (highest ordinal) runs the new version, and the rest stay on the old one. Operators (§22) use this heavily for cautious database upgrades — promote the canary replica to handle a fraction of read traffic, observe, then continue.

### 10.3 The math: how many Pods are on which revision

```
total replicas = N
partition       = P

Pods on updateRevision: ordinals in [P, N) → count = N - P
Pods on currentRevision: ordinals in [0, P) → count = P
```

When `currentReplicas == 0` (all Pods updated), the controller sets `currentRevision = updateRevision`.

### 10.4 Combined with `maxUnavailable`

Partitioned rollouts are usually serial (one Pod at a time). Combined with `maxUnavailable: 2`, you can update *multiple* of the eligible Pods in parallel:

```yaml
spec:
  updateStrategy:
    type: RollingUpdate
    rollingUpdate:
      partition: 5
      maxUnavailable: 2
```

This updates ordinals 9, 8, 7, 6, 5 in batches of 2 — `(9, 8)` then `(7, 6)` then `(5)`. Below ordinal 5, nothing happens. This is mostly relevant for large StatefulSets (50+ replicas) where serial rollout is too slow.

---

## 11. `maxUnavailable` for StatefulSets

`spec.updateStrategy.rollingUpdate.maxUnavailable` was added to StatefulSets in 1.24 (alpha behind `MaxUnavailableStatefulSet` feature gate; beta in 1.27; expected GA later). It allows multiple Pods to be updated in parallel during a rolling update.

```yaml
spec:
  updateStrategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 2    # up to 2 Pods can be Unavailable at once during update
```

The default is `1`: one Pod at a time, classic serial rollout.

### 11.1 What "unavailable" means here

A Pod is *available* if:

- It exists.
- It has the latest revision.
- It is `Ready`.
- It has been Ready for at least `minReadySeconds` (§28).

Any Pod that does not meet all of these is unavailable. The controller ensures that the number of unavailable Pods is at most `maxUnavailable`. So with `maxUnavailable: 2`:

```
Start: db-0..db-9 all on v1, all Ready.

Round 1: Pick db-9 and db-8 (highest two). Delete both. Recreate from v2.
         Now 2 Pods are unavailable; budget exhausted.
         Wait until both db-9 and db-8 are Ready + minReadySeconds.

Round 2: Pick db-7, db-6. Delete + recreate.
         Wait.

Round 3: db-5, db-4. ...
```

### 11.2 The quorum trap

`maxUnavailable: 2` on a 3-replica quorum-based system (etcd, Raft-based DBs) is a disaster:

```
3 replicas, maxUnavailable: 2
==============================
Update starts: delete db-2 and db-1 in parallel.
Now only db-0 is alive. Quorum (2/3) is LOST.
etcd cluster fails writes. Consul outage. Postgres-with-Patroni outage.

Recovery: wait for db-1 and db-2 to come back, restart quorum.
But: during the gap, every write request to the cluster has failed.
```

For quorum-based systems with 3 replicas, `maxUnavailable: 1` is the *only* safe value. For 5-replica systems, `maxUnavailable: 2` is safe (5/2 + 1 = 3 still alive ≥ quorum). The general rule:

```
maxUnavailable ≤ floor(N / 2)   for an N-replica quorum-based system
```

For non-quorum systems (Cassandra with RF=3, write quorum LOCAL_QUORUM = 2): `maxUnavailable: 1` is still safest, because losing 2/3 replicas at once means write requests fail even though the cluster is technically operational.

### 11.3 Use cases for `maxUnavailable > 1`

- Read-mostly clusters with many replicas (e.g., 50 Cassandra nodes, RF=3): updating 5 at a time is fine.
- Sharded systems where each replica is independent: e.g., a sharded Redis Cluster where each shard has its own master/replicas.
- Large Elasticsearch clusters with replicas distributed across shards.

For small clusters and any consensus protocol, leave `maxUnavailable: 1`.

---

## 12. Scale-Down: Highest Ordinal First

When you reduce `spec.replicas`, the controller deletes Pods starting from the highest ordinal. For a scale from 5 → 2:

```
Before: db-0, db-1, db-2, db-3, db-4   (5 Pods)
Action: replicas: 2

Sequence (OrderedReady):
  T=0   Delete db-4. Pod terminates (preStop, SIGTERM, grace period).
  T=10  db-4 fully gone.
  T=10  Delete db-3.
  T=20  db-3 gone.
  T=20  Delete db-2.
  T=30  db-2 gone.

After: db-0, db-1   (2 Pods)
```

### 12.1 What happens to the PVCs

This depends on `persistentVolumeClaimRetentionPolicy.whenScaled`:

- `whenScaled: Retain` (default): PVCs `data-db-2, data-db-3, data-db-4` survive. The underlying PVs and storage are still there.
- `whenScaled: Delete`: PVCs `data-db-2, data-db-3, data-db-4` are deleted. The PV's `reclaimPolicy` (Delete vs Retain) decides what happens to the backing volume.

The asymmetry — Pods deleted in reverse order, PVCs persist or not — is the source of the most subtle StatefulSet bugs. A pattern:

```
1. Scale from 5 to 2. Pods db-4, db-3, db-2 are deleted. PVCs survive (retain).
2. Later, scale from 2 to 5. Pods db-2, db-3, db-4 are recreated.
3. Each new Pod is bound to its OLD PVC (data-db-2, data-db-3, data-db-4) with the old data.
4. The application sees a "fresh" Pod with stale state from days ago.
```

If your application uses sharding, the new `db-2` may have data for a shard that's been redistributed elsewhere in the cluster. The application must detect and reconcile this — or, more commonly, the operator (§22) deletes the PVCs before scaling back up.

### 12.2 The PDB interaction

A scale-down deletes Pods, which counts against any matching PodDisruptionBudget. If you have:

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: db-pdb
spec:
  minAvailable: 3
  selector:
    matchLabels: { app: db }
```

…and you scale from 5 → 2, the PDB *will not block* the scale-down. PDBs only protect against *involuntary* disruption (node drain, evictions). Scale-down via the StatefulSet controller is a *voluntary* disruption from the user, and the eviction API is not in the path.

This is a subtle source of outages: an operator scales a quorum-based system down by accident, the PDB doesn't help, and the cluster loses quorum mid-scale.

### 12.3 Scale-down with running connections

When a Pod is deleted, the kubelet sends SIGTERM to PID 1 of each container, waits up to `terminationGracePeriodSeconds`, then SIGKILL. During the grace period, the application is supposed to:

- Stop accepting new connections.
- Drain existing connections (or politely close them).
- Flush state to disk.
- Exit cleanly.

For a database, this means: refuse new transactions, finish or abort in-flight ones, checkpoint the WAL, fsync, exit. A `preStop` hook is often used to trigger graceful shutdown:

```yaml
containers:
- name: postgres
  lifecycle:
    preStop:
      exec:
        command: ["/usr/local/bin/pg_ctl", "-D", "/var/lib/postgresql/data", "stop", "-m", "smart"]
```

`pg_ctl stop -m smart` waits for connections to close before shutting down. The grace period must be long enough; default 30s is often too short for a busy database. Set `terminationGracePeriodSeconds: 300` (or longer) for slow-shutdown applications.

---

## 13. Stable DNS Records Across Scaling Events

A subtle but powerful guarantee: DNS records for in-use ordinals are stable across scaling. If your cluster has ordinals 0-4, scales down to 0-1, then scales back up to 0-4:

```
T=0:   db-0, db-1, db-2, db-3, db-4 all exist; DNS records all published.
T=10:  Scale to 2. Pods db-2, db-3, db-4 deleted. PVCs retained.
       DNS records for db-0.db.ns and db-1.db.ns continue to resolve.
       DNS records for db-2.db.ns, db-3.db.ns, db-4.db.ns are GONE.
       (CoreDNS removes them when the Pods leave the EndpointSlice.)

T=100: Scale to 5. Pods db-2, db-3, db-4 recreated, bound to retained PVCs.
       Each new Pod has a new IP, but the DNS name is the same as before.
       db-2.db.ns now resolves to the new db-2's new IP.
```

To the application: the DNS name `db-2.db.ns.svc.cluster.local` is *a stable identity for ordinal 2*, regardless of whether ordinal 2 is currently running, was deleted yesterday, was rescheduled to a new node, or is freshly recreated.

### 13.1 During Pod restart, DNS briefly returns NXDOMAIN

There is a window — from "Pod deleted" to "new Pod Ready" — during which the DNS name has no A record. Clients querying during this window get NXDOMAIN.

```
T=0    db-2 is Ready. DNS: db-2.db.ns → 10.244.1.17
T=1    kubectl delete pod db-2
T=1    EndpointSlice updates: db-2 removed from endpoints (terminating)
T=3    CoreDNS no longer returns A record for db-2.db.ns
T=4    Controller creates new db-2 (Pod Pending)
T=10   db-2 Running but not Ready
T=15   db-2 Ready. EndpointSlice updates: db-2 has endpoint 10.244.1.99
T=17   CoreDNS now returns A record for db-2.db.ns → 10.244.1.99

WINDOW OF NXDOMAIN: ~14 seconds (T=3 to T=17)
```

Applications must handle this. The pattern:

```python
def dial_peer(ordinal):
    fqdn = f"db-{ordinal}.db.prod.svc.cluster.local"
    for attempt in range(MAX_RETRIES):
        try:
            return socket.create_connection((fqdn, 5432), timeout=5)
        except (socket.gaierror, ConnectionRefusedError):
            time.sleep(backoff(attempt))
    raise PeerUnreachable(fqdn)
```

`gaierror` covers NXDOMAIN; `ConnectionRefusedError` covers "Pod is up but the application hasn't started listening yet". Most stateful applications get this right; the failure mode of "we cache resolved IPs and never re-resolve" is rare in clients that follow standard DNS patterns.

### 13.2 `publishNotReadyAddresses: true`

For bootstrap scenarios where the Pod needs to be addressable *before* it passes its readiness probe (e.g., it needs to talk to its own DNS name to advertise itself to peers), set `publishNotReadyAddresses: true` on the headless Service. The EndpointSlice will then include the Pod even when not ready, and CoreDNS will return an A record.

This is critical for:

- Etcd, which advertises its own peer URL during bootstrap.
- Cassandra, where seeds may include the bootstrapping node itself.
- Any cluster where peer discovery happens before readiness.

The trade-off: client traffic might be sent to a not-ready Pod. If you have a client-facing ClusterIP Service alongside the headless one (§19), this is fine — clients use the ClusterIP, peers use the headless. If you have only the headless Service serving both roles, clients may hit a not-ready Pod and fail.

---

## 14. The Bootstrap Problem: Predictable DNS as Cluster Topology

Almost every clustered stateful system has a bootstrap problem: how does node N find the other nodes when the cluster doesn't exist yet? The answer in Kubernetes is: **derive the peer list from the StatefulSet's predictable DNS**.

### 14.1 Etcd's bootstrap

Etcd starts with `--initial-cluster=<name1>=<peerURL1>,<name2>=<peerURL2>,...`, listing every member and its peer URL. The URLs must resolve for the cluster to converge.

A 3-node etcd cluster on StatefulSet `etcd` in namespace `default` with headless Service `etcd`:

```bash
# Inside each Pod (computed at startup from $HOSTNAME and the STS name/svc):
INITIAL_CLUSTER="etcd-0=http://etcd-0.etcd.default.svc.cluster.local:2380,\
etcd-1=http://etcd-1.etcd.default.svc.cluster.local:2380,\
etcd-2=http://etcd-2.etcd.default.svc.cluster.local:2380"

exec etcd \
  --name=$HOSTNAME \
  --listen-peer-urls=http://0.0.0.0:2380 \
  --listen-client-urls=http://0.0.0.0:2379 \
  --advertise-client-urls=http://$HOSTNAME.etcd.default.svc.cluster.local:2379 \
  --initial-advertise-peer-urls=http://$HOSTNAME.etcd.default.svc.cluster.local:2380 \
  --initial-cluster=$INITIAL_CLUSTER \
  --initial-cluster-state=new \
  --data-dir=/var/run/etcd/default.etcd
```

Three things make this work:

1.  The StatefulSet name (`etcd`) and headless Service name (`etcd`) are known *a priori*.
2.  The Pod's hostname is `etcd-<ordinal>`, deterministic.
3.  The headless Service's `publishNotReadyAddresses: true` allows peer resolution before readiness.

```
ETCD CLUSTER BOOTSTRAP TIMELINE (OrderedReady)
================================================

T=0    StatefulSet created with replicas: 3
T=0    Controller creates etcd-0 (PVC, Pod)
T=2    etcd-0 starts. Runs --initial-cluster pointing at etcd-0,1,2.
       Resolves etcd-0.etcd.ns: itself (via publishNotReadyAddresses)
       Resolves etcd-1.etcd.ns: NXDOMAIN (etcd-1 doesn't exist yet)
       Resolves etcd-2.etcd.ns: NXDOMAIN
       etcd binds peer port, tries to contact peers, fails initially.
       Election: etcd-0 votes for itself, waits.
T=20   etcd-0 still cannot form quorum (1/3 not enough). Logs "no leader".
       But: etcd-0 is bound and listening. Pod becomes Ready.
       (readinessProbe usually checks /health on client port; etcd reports
        "healthy" if it's responding, even without quorum, depending on probe)
T=20   Controller creates etcd-1.
T=22   etcd-1 starts. --initial-cluster lists all three.
       Resolves etcd-0.etcd.ns: 10.244.1.5 (etcd-0's IP, since etcd-0 is in EndpointSlice)
       Resolves etcd-1.etcd.ns: itself
       Resolves etcd-2.etcd.ns: NXDOMAIN
       etcd-1 contacts etcd-0; they form a 2-node quorum.
       LEADERSHIP: one of them (typically etcd-0) becomes leader.
T=40   etcd-1 Ready.
T=40   Controller creates etcd-2.
T=42   etcd-2 starts. Joins the existing 2-node cluster. Catches up via Raft.
T=55   etcd-2 Ready.
T=55   Quorum: 3/3. Cluster fully operational.
```

The critical detail: every node knows the full peer list at startup, because the topology is encoded in the StatefulSet name + ordinal + headless service. No external service registry. No coordination layer. Just DNS.

### 14.2 Kafka KRaft

Kafka in KRaft mode (no Zookeeper) uses similar topology. The controller quorum is a `controller.quorum.voters` list:

```properties
controller.quorum.voters=0@kafka-0.kafka.default.svc.cluster.local:9093,\
                       1@kafka-1.kafka.default.svc.cluster.local:9093,\
                       2@kafka-2.kafka.default.svc.cluster.local:9093
node.id=$(echo $HOSTNAME | rev | cut -d- -f1 | rev)
process.roles=controller,broker
```

The same predictability: ordinal → node.id → peer URL.

### 14.3 Cassandra seeds

Cassandra uses a "seeds" list: a small number of nodes (usually 2-3) that bootstrap nodes contact to learn the rest of the cluster. The list is computed at startup:

```bash
# Use the first three ordinals as seeds:
SEEDS=$(for i in 0 1 2; do
  echo -n "cassandra-$i.cassandra.default.svc.cluster.local"
  [ $i -lt 2 ] && echo -n ","
done)
echo "seeds: $SEEDS" >> /etc/cassandra/cassandra.yaml
```

Cassandra uses `Parallel` podManagementPolicy because seeds-based discovery handles arrival ordering itself.

### 14.4 The general pattern

Every stateful operator that wraps a StatefulSet follows the same pattern at bootstrap:

```python
# Pseudocode for the bootstrap script of any clustered stateful application
def bootstrap():
    sts = os.environ["STATEFULSET_NAME"]       # injected via downward API
    svc = os.environ["HEADLESS_SERVICE"]       # same as sts, usually
    ns  = os.environ["NAMESPACE"]
    n   = int(os.environ["REPLICAS"])
    me  = os.environ["HOSTNAME"]               # e.g., "db-1"
    ordinal = int(me.rsplit("-", 1)[1])        # 1

    peers = [f"{sts}-{i}.{svc}.{ns}.svc.cluster.local"
             for i in range(n) if i != ordinal]

    write_config(my_id=ordinal, my_address=f"{me}.{svc}.{ns}.svc.cluster.local",
                 peers=peers)
    start_app()
```

The StatefulSet's deterministic naming is the lynchpin. Take it away, and you need an external service registry (etcd-of-etcd, ZooKeeper, Consul) to do peer discovery. With it, the cluster topology is a fixed function of the manifest.

---

## 15. "Wait for Pod-N Ready" Semantics

Under `OrderedReady`, the controller does not create ordinal N+1 until ordinal N is `Ready`. What does "Ready" mean, precisely?

A Pod is Ready when:

- `status.conditions[ContainersReady] = True` (all containers' readiness probes have passed; init containers have all completed).
- `status.conditions[Ready] = True` (this is `ContainersReady` AND all `readinessGates` pass).

The readiness probe is configured per container:

```yaml
containers:
- name: postgres
  readinessProbe:
    exec:
      command: ["pg_isready", "-U", "postgres"]
    initialDelaySeconds: 10
    periodSeconds: 5
    timeoutSeconds: 3
    successThreshold: 1
    failureThreshold: 3
```

`pg_isready` checks that PostgreSQL is accepting connections. Once it succeeds, the Pod is Ready, the controller proceeds to the next ordinal.

### 15.1 Slow readiness = slow scale-up

If your readiness probe takes 60s to pass (database warming caches, replaying WAL, joining cluster), then scaling from 0 to 10 takes 10 × 60s = 10 minutes.

For applications where readiness is genuinely slow (Postgres after a restore, Cassandra after a large repair), this is acceptable. For applications where readiness *can* be fast but is artificially slow due to a bad probe, it's a footgun. Audit your probes.

### 15.2 Readiness vs liveness vs startup

- **Startup probe**: long timeout; for slow-starting containers. While it's pending, neither readiness nor liveness probes run.
- **Readiness probe**: "am I ready to serve traffic?" Failure removes the Pod from Service endpoints (and from the headless Service's EndpointSlice).
- **Liveness probe**: "am I deadlocked / unrecoverable?" Failure restarts the container.

For stateful applications, the distinction matters because:

- **Readiness should reflect cluster membership**, not just "process is running." `pg_isready` is good; "TCP connect to port 5432 succeeds" is bad (port may be open before the server is actually accepting queries).
- **Liveness must not be too aggressive.** A common failure mode: liveness probe checks "is this Pod the primary?" — and during a planned failover, the answer is briefly "no", so the container is restarted, breaking the failover. Liveness should detect deadlock only.
- **Startup probes are useful for slow boot.** A 10-minute initial sync should be a startup probe with `failureThreshold: 60, periodSeconds: 10`, not a readiness probe with a giant timeout.

### 15.3 Readiness gates

A `readinessGate` is a custom condition on a Pod that must be `True` for the Pod to be considered Ready. They're set externally by some controller (often an operator).

```yaml
spec:
  readinessGates:
  - conditionType: db.example.com/replication-caught-up
```

The Pod will not be Ready until *some* controller sets `status.conditions[db.example.com/replication-caught-up] = True`. This is how operators inject application-aware readiness on top of the basic probe.

For a Postgres operator, the gate might be "replication lag < 5 seconds, replica is caught up". The operator's controller patches the Pod's status to set the condition once it observes the lag.

This is a powerful mechanism: it lets the operator participate in the StatefulSet's `OrderedReady` semantics without the StatefulSet controller knowing about replication.

---

## 16. Init Containers and the Join Pattern

A common pattern for clustered stateful applications: use an init container to run the join logic, then start the main container.

```yaml
template:
  spec:
    initContainers:
    - name: etcd-join
      image: etcd:v3.5
      command:
      - /bin/sh
      - -c
      - |
        # Determine my ordinal
        ORDINAL=${HOSTNAME##*-}

        # If this is the first node (ordinal 0) and the data directory is empty,
        # bootstrap a new cluster.
        if [ "$ORDINAL" -eq "0" ] && [ ! -d /var/run/etcd/member ]; then
          echo "Bootstrapping new etcd cluster"
          # No special init needed; main container will use --initial-cluster-state=new
          exit 0
        fi

        # Otherwise, check if the data directory has a member ID. If yes,
        # this is a restart; main container handles it. If no, this is a
        # join: we need to add ourselves to the existing cluster via API.
        if [ -d /var/run/etcd/member ]; then
          echo "Existing data, will restart"
          exit 0
        fi

        # Use etcdctl from another node to add ourselves as a learner
        etcdctl --endpoints=etcd-0.etcd.default.svc.cluster.local:2379 \
                member add etcd-$ORDINAL \
                --peer-urls=http://etcd-$ORDINAL.etcd.default.svc.cluster.local:2380
      volumeMounts:
      - name: data
        mountPath: /var/run/etcd

    containers:
    - name: etcd
      image: etcd:v3.5
      command:
      - /usr/local/bin/etcd
      - --name=$(HOSTNAME)
      - --data-dir=/var/run/etcd
      # ... rest of args ...
      volumeMounts:
      - name: data
        mountPath: /var/run/etcd
      readinessProbe:
        httpGet:
          path: /health
          port: 2379
```

The init container's job:

- Detect whether this Pod is a fresh node, a re-join (data wiped), or a restart (data intact).
- Run the appropriate join logic (talk to existing nodes to add a member, register, etc.).
- Exit successfully so the main container can start.

The main container then runs the actual etcd process, with the data directory in the expected state. The persistent volume guarantees that data survives container restarts within the same Pod, and PVC retention guarantees data survives Pod restarts.

### 16.1 Bootstrap state detection

The trickiest part of the init container is *detecting* what state the cluster is in. The data directory is the source of truth:

- Empty data dir + ordinal 0 + no other Pods: bootstrap new cluster.
- Empty data dir + ordinal 0 + other Pods exist: catastrophic — this means ordinal 0 lost its data and is rejoining a cluster that thinks ordinal 0 is gone. Operator territory.
- Empty data dir + ordinal > 0: join existing cluster as new member.
- Non-empty data dir: restart, use existing member ID.

Operators encode this logic explicitly. Without an operator, you write init container shell scripts that try to be smart and inevitably get it wrong on the rare edge cases. This is why the operator pattern (§22) won: getting bootstrap right by hand is *hard*.

### 16.2 Why an init container and not the main container

You *could* put all the join logic in the main container's entrypoint. The reason to use an init container is separation of concerns:

- Init container fails → Pod is in `Init:Error`. Operator can debug; main container hasn't run yet.
- Main container has a clean role: "run etcd, period." No conditional bootstrap logic inside the runtime.
- Init containers run *sequentially*, so you can chain join logic with config templating, secret materialization, etc.

```yaml
initContainers:
- name: config-render
  image: busybox
  command: [sh, -c, "envsubst < /tmpl/etcd.conf > /shared/etcd.conf"]
- name: join
  image: etcd:v3.5
  command: [/scripts/join.sh]
```

The Pod won't progress to the main container until both init containers exit 0.

---

## 17. Pod Identity vs Node Placement

The StatefulSet's identity contract is about Pod name + PVC binding, *not* node placement. Pod `db-0` may run on `node-a` today, `node-b` tomorrow. The PVC follows it.

### 17.1 What can move a Pod between nodes

- Node failure (kubelet stops reporting; node is tainted unreachable; eventually Pod is force-deleted; controller recreates it on a different node).
- Voluntary eviction (`kubectl drain` to upgrade the node).
- Rescheduling due to a node-level taint that the Pod no longer tolerates.

In all cases, the PVC stays bound; the new Pod mounts the same PV.

### 17.2 The CSI attach/detach dance

When Pod `db-0` moves from `node-a` to `node-b`:

```
1. Old Pod db-0 on node-a is terminating.
2. kubelet on node-a unmounts the PV from the container.
3. kubelet on node-a calls CSI NodeUnstageVolume.
4. External attacher controller calls CSI ControllerUnpublishVolume:
   the cloud detaches the EBS volume from node-a's instance.
5. Pod db-0 deleted from apiserver.
6. Controller recreates db-0; scheduler binds it to node-b.
7. External attacher calls CSI ControllerPublishVolume:
   cloud attaches the EBS volume to node-b's instance.
8. kubelet on node-b sees Pod's volume not yet ready; calls CSI NodeStageVolume.
9. kubelet calls CSI NodePublishVolume to mount into the container.
10. Pod db-0 starts.
```

This is the three-phase volume lifecycle from ch 19. The relevant property here: the data on the volume is unchanged across the move. The application restarts with the same data, on a new node, with a new IP.

### 17.3 Zone constraints and `WaitForFirstConsumer`

The above works smoothly for EBS *within a single zone*. EBS volumes cannot be attached across zones in AWS (and equivalent restrictions apply in GCP/Azure for zonal disks). If the new node is in a different zone from the PV, attach fails.

The fix is the StorageClass's `volumeBindingMode`:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3-wffc
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer    # critical for zone-locked storage
parameters:
  type: gp3
```

With `WaitForFirstConsumer` (WFFC):

1.  PVC is created when the StatefulSet creates Pod `db-0`. PVC is **Pending**, PV is **not yet provisioned**.
2.  Scheduler schedules `db-0` to some node (say in `us-east-1a`), considering all other constraints.
3.  Once the node is chosen, the external provisioner creates a PV *in that zone*.
4.  Pod starts, mounts the PV.

With the default `Immediate` binding mode, the PV is provisioned immediately upon PVC creation, in whatever zone the provisioner chooses. If the scheduler later picks a different zone for the Pod, attach fails.

For any StatefulSet with zone-locked storage, **always use `WaitForFirstConsumer`**. The scheduler's `VolumeBinding` plugin handles the deferred binding via the PreFilter/PreBind extension points.

### 17.4 Pod-to-node pinning (rare, usually wrong)

You *can* pin a Pod to a specific node using `nodeName`, `nodeSelector`, or strict `nodeAffinity`. For a StatefulSet this is almost always wrong:

- If the node dies, the Pod can never be rescheduled.
- Upgrades that involve cordoning the node leave the Pod stuck.
- The PV might still exist, but the Pod can't run anywhere else.

The correct pattern: let the scheduler choose. Use anti-affinity to spread Pods, not nodeName to pin them.

The only legitimate use case for pinning: local storage (PV with `volumeMode: Filesystem` backed by a local SSD on a specific node). For this, the PV itself has node affinity, and the scheduler respects it.

---

## 18. Topology Constraints: One-per-Zone Patterns

For most production StatefulSets, you want replicas spread across failure domains. "3 replicas, one per AZ" is the canonical pattern.

### 18.1 Pod anti-affinity (hard)

```yaml
template:
  spec:
    affinity:
      podAntiAffinity:
        requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchLabels:
              app: db
          topologyKey: kubernetes.io/hostname    # one per node
```

`requiredDuringSchedulingIgnoredDuringExecution` is a hard requirement: a Pod cannot be scheduled on a node that already has a Pod matching the selector.

- `topologyKey: kubernetes.io/hostname` → one per node.
- `topologyKey: topology.kubernetes.io/zone` → one per zone.
- `topologyKey: topology.kubernetes.io/region` → one per region.

Hard anti-affinity is strict: if no zone has space, the Pod stays Pending. For 3 replicas in 3 AZs, this is exactly the desired behavior.

### 18.2 Pod anti-affinity (soft)

```yaml
podAntiAffinity:
  preferredDuringSchedulingIgnoredDuringExecution:
  - weight: 100
    podAffinityTerm:
      labelSelector:
        matchLabels: { app: db }
      topologyKey: topology.kubernetes.io/zone
```

`preferredDuringScheduling…` is a soft preference: the scheduler will try to satisfy it, but if it can't, the Pod schedules anyway (possibly co-located). Useful when you have more replicas than zones.

### 18.3 `topologySpreadConstraints` (recommended)

```yaml
template:
  spec:
    topologySpreadConstraints:
    - maxSkew: 1
      topologyKey: topology.kubernetes.io/zone
      whenUnsatisfiable: DoNotSchedule          # or ScheduleAnyway
      labelSelector:
        matchLabels: { app: db }
```

`maxSkew: 1` means: the difference between the most-loaded zone and least-loaded zone (in terms of matching Pods) cannot exceed 1.

For 6 replicas across 3 AZs:

```
maxSkew=1, DoNotSchedule
  → 2 in zone-a, 2 in zone-b, 2 in zone-c   (skew = 0, allowed)
  → 3 in zone-a, 2 in zone-b, 1 in zone-c   (skew = 2, NOT allowed)
  → 2 in zone-a, 2 in zone-b, 1 in zone-c, 1 Pending  (skew = 1 among scheduled, 6th waits)
```

For most StatefulSets, `topologySpreadConstraints` is better than `podAntiAffinity` because it handles the case where you have more replicas than zones gracefully.

### 18.4 Combining with the headless Service

The headless Service publishes A records for *all* ready Pods regardless of zone. Clients in `us-east-1a` calling `db.prod.svc.cluster.local` will get a list of all three Pods' IPs — including the ones in `us-east-1b` and `us-east-1c`. Cross-zone traffic is real (and billed).

For latency-sensitive clients, you can:

- Use Topology Aware Routing (`service.kubernetes.io/topology-mode: Auto`): kube-proxy prefers same-zone endpoints when scaling allows.
- Use a per-zone headless Service: filter the EndpointSlice by zone using a labelSelector on a Service. Each client uses the Service for *its own* zone.
- Use the application's own load-balancing logic (the application picks which peer to talk to, based on whatever criteria it likes).

For stateful applications, often the third option is most natural: the application reads the full peer list from headless DNS, then talks to the *correct* peer (e.g., the Postgres primary specifically, not a random replica).

### 18.5 NodeAffinity for dedicated pools

Sometimes you want to pin a StatefulSet to a dedicated node pool (e.g., nodes with NVMe SSDs):

```yaml
template:
  spec:
    nodeSelector:
      workload-class: database-nodes
    tolerations:
    - key: dedicated
      operator: Equal
      value: database
      effect: NoSchedule
```

Combined with anti-affinity and `WaitForFirstConsumer`, this gives you "3 replicas, one per AZ, each on a database-tagged node, with local NVMe storage". Useful for high-throughput databases that can't share nodes with general workloads.

---

## 19. Headless vs ClusterIP: Use Both

Many production StatefulSets have **two Services**:

1.  A **headless Service** for peer discovery — used by Pods *within* the StatefulSet to find each other.
2.  A **ClusterIP Service** for client traffic — used by *external* clients (other apps in the cluster) to connect.

```yaml
---
# Headless Service: for peer discovery, used by the StatefulSet itself
apiVersion: v1
kind: Service
metadata:
  name: db
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  selector:
    app: db
  ports:
  - name: postgres
    port: 5432
---
# ClusterIP Service: for client traffic
apiVersion: v1
kind: Service
metadata:
  name: db-client
spec:
  type: ClusterIP
  selector:
    app: db
    role: primary       # optional: only routes to the primary
  ports:
  - port: 5432
    targetPort: 5432
```

### 19.1 Why two services

- The headless Service publishes per-Pod A records; clients can connect to specific Pods (`db-0.db.prod.svc.cluster.local`). This is what peer discovery needs.
- The ClusterIP Service load-balances across all matching Pods. Clients dial `db-client.prod.svc.cluster.local` and get connected to *some* backend. This is what stateless clients want.
- The StatefulSet's `spec.serviceName` MUST point at the headless one. The ClusterIP Service is independent.

The labels on each Pod determine which Services include it. With a `role: primary` label set dynamically (by an operator, or via `kubectl label`), the ClusterIP Service routes only to the primary:

```bash
# Operator promotes db-1 to primary:
kubectl label pod db-0 role-
kubectl label pod db-1 role=primary
```

The ClusterIP Service's EndpointSlice updates within seconds; client traffic now goes to `db-1`. The headless Service is unaffected.

### 19.2 Read replicas pattern

Add a third Service:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: db-replicas
spec:
  type: ClusterIP
  selector:
    app: db
    role: replica
```

Pods `db-1, db-2` have `role: replica`; `db-0` has `role: primary`. Now clients can use `db-client` for writes, `db-replicas` for read-only queries.

This is a classic pattern in Postgres operators: the operator manages the labels, the Services route traffic, the StatefulSet handles identity and storage.

### 19.3 Zone topology

Zone information for PV provisioning comes from node labels (`topology.kubernetes.io/zone`), *not* from the Service. The Service is unaware of zones; it just publishes endpoints. The PV provisioner reads the scheduled Pod's node's zone label to decide which AZ to provision the PV in (with WFFC).

This is a common confusion point: "does my headless Service determine the zones?" No. The headless Service is just a DNS publisher. Zones are decided by:

1.  Node labels.
2.  Pod's nodeAffinity / topologySpreadConstraints / podAntiAffinity.
3.  StorageClass's `volumeBindingMode: WaitForFirstConsumer`.
4.  The scheduler's combined evaluation.

---

## 20. PVC Expansion in a StatefulSet

A common operational need: grow a database's disk without recreating it. The StatefulSet does *not* provide a direct API for this; it must be done at the PVC level.

### 20.1 Prerequisites

The StorageClass must allow expansion:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
provisioner: ebs.csi.aws.com
allowVolumeExpansion: true        # required
parameters:
  type: gp3
```

The CSI driver must support `ControllerExpandVolume` and `NodeExpandVolume` (most modern drivers do).

### 20.2 The expand procedure

```bash
# 1. Edit each PVC to increase its size.
kubectl edit pvc data-db-0
# Change: spec.resources.requests.storage: 100Gi → 500Gi

kubectl edit pvc data-db-1
# (same)
kubectl edit pvc data-db-2
# (same)

# 2. The CSI driver's external-resizer sidecar observes the PVC size increase.
#    It calls ControllerExpandVolume to resize the underlying EBS volume.
#    This is online: no Pod restart needed for the *volume* part.

# 3. For the filesystem to use the new size, NodeExpandVolume must run.
#    For ext4/xfs, this requires the volume to be mounted with the new size.
#    With CSI's "FsResizeOnRestart" pattern, the kubelet runs the resize when
#    the Pod restarts. Some CSI drivers support online filesystem resize, in
#    which case no restart is needed.

# 4. To force the filesystem expansion to take effect on rdrivers that need
#    a restart, delete the Pod (the StatefulSet recreates it):
kubectl delete pod db-0
# (PVC stays; new Pod mounts the resized PV; filesystem expands during mount)
```

### 20.3 You cannot shrink

PVC expansion is one-way. You cannot reduce a PVC's size via the API. To shrink, you must:

1.  Create a new, smaller PVC.
2.  Copy data from the old PV to the new one (application-level migration).
3.  Delete the old PV (which removes the original data).
4.  Update the StatefulSet's volumeClaimTemplate.

In practice, no one shrinks; you live with the extra capacity.

### 20.4 The template's size is not retroactively applied

If you have 3 Pods with `data-db-0` (100Gi), `data-db-1` (100Gi), `data-db-2` (100Gi), and you edit `volumeClaimTemplates[0].spec.resources.requests.storage: 500Gi` — the existing PVCs are *not* affected. The template is immutable in the API anyway, but even if it weren't, the change would only apply to *newly created* ordinals (`data-db-3, db-4, ...`).

To grow existing PVCs, edit them directly (as above). The template's size is just the default for new ordinals.

---

## 21. Backups: Snapshots, App-Level, and the Snapshot-of-a-Running-DB Problem

Backups for stateful workloads break into two paradigms:

- **CSI VolumeSnapshot**: ask the storage layer to take a point-in-time snapshot of the underlying volume.
- **Application-level**: ask the application to produce a consistent backup (pg_basebackup, mongodump, etcdctl snapshot, etc.).

### 21.1 CSI VolumeSnapshots

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: db-0-snapshot-20260523
  namespace: prod
spec:
  source:
    persistentVolumeClaimName: data-db-0
  volumeSnapshotClassName: csi-aws-vsc
```

This triggers the CSI snapshotter to create a snapshot of the PV backing `data-db-0`. The snapshot is an opaque object managed by the storage provider (EBS snapshot in AWS, persistent disk snapshot in GCP).

To restore: create a new PVC with `dataSource` pointing at the snapshot:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data-db-0-restored
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: gp3
  resources:
    requests:
      storage: 100Gi
  dataSource:
    name: db-0-snapshot-20260523
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
```

The new PVC is provisioned with the snapshot's contents.

### 21.2 The crash-consistent vs application-consistent problem

CSI snapshots are typically **crash-consistent**: they capture the state of the disk as if the machine had been instantly powered off. For most databases, this is *almost* good enough — the database will replay its WAL on next startup and reach a consistent state. But:

- If the database has uncommitted transactions, they're lost (expected behavior).
- If the database uses in-memory buffers that haven't been fsync'd, those writes are lost.
- For multi-volume databases (data on one PVC, WAL on another), a snapshot of each PVC at different times produces inconsistent state — replay can fail.

```
DANGER: SNAPSHOTTING A MULTI-VOLUME DATABASE
=============================================

T=0   Snapshot data-db-0 (the data PVC) → captures version V1 of data
T=1   ... transactions are committed, WAL records appended ...
T=2   Snapshot wal-db-0 (the WAL PVC)   → captures WAL up to V2 of data

Restored cluster: data at V1, WAL at V2.
On startup, the database tries to replay WAL records V1+1..V2 against data V1.
If WAL is incompatible (e.g., references pages that don't exist in V1), CRASH.
```

### 21.3 Application-consistent backups

For correctness, you want a snapshot of *all volumes* taken atomically — or, better, you want to coordinate with the application:

```bash
# Postgres:
psql -c "SELECT pg_start_backup('snapshot-20260523', false, false)"
# Take CSI snapshots of all PVCs for db-0
psql -c "SELECT pg_stop_backup(false, true)"

# Or use pg_basebackup for a complete logical backup:
pg_basebackup -D /backups/db-$(date +%F) -h db-1.db.prod.svc.cluster.local
```

`pg_basebackup` connects to a replica, asks the primary to put the WAL in a known-good state, copies all data files, and finishes with a consistency marker. The output is application-consistent regardless of underlying volume semantics.

Operators usually do this: the Postgres operator orchestrates `pg_basebackup` + WAL archiving, providing point-in-time recovery without relying on CSI snapshot semantics.

### 21.4 Velero

Velero (ch 32) is a cluster-level backup tool that integrates with CSI VolumeSnapshots and PV/PVC objects:

```yaml
apiVersion: velero.io/v1
kind: Backup
metadata:
  name: db-prod-backup-20260523
spec:
  includedNamespaces: [prod]
  labelSelector:
    matchLabels:
      app: db
  snapshotVolumes: true
  volumeSnapshotLocations: [aws-default]
```

Velero captures: every Kubernetes object in the namespace, plus snapshots of every PV. Restore recreates the objects and rehydrates PVs from snapshots.

For stateful workloads with operators, the operator usually has its own backup CRDs (e.g., `pgbackup`, `etcdsnapshot`). Velero is a fallback for whole-cluster DR.

### 21.5 The snapshot-of-a-running-DB rule

Never assume a CSI snapshot of a running database is application-consistent. Either:

1.  Use the application's backup tool (pg_basebackup, mongodump, mysqldump, etcdctl snapshot save, ...).
2.  Stop or quiesce the application before snapshotting.
3.  Use the operator's snapshot CRD, which knows how to coordinate.

The CSI snapshot is fine for the *substrate* (the storage volume); it's not fine for the *application state* unless the application has been told.

---

## 22. Operators That Wrap StatefulSets

Operators are the production answer to "I need application-aware orchestration on top of a StatefulSet." They are controllers (ch 23) that watch a custom resource (e.g., `PostgresCluster`) and reconcile it into a StatefulSet plus a bunch of supporting objects (Services, Secrets, ConfigMaps, PodMonitors, backup CronJobs).

```
USER LAYER                OPERATOR LAYER                  PRIMITIVE LAYER
==========                ==============                  ===============

  PostgresCluster   →   PostgresOperator   →   StatefulSet
  (custom resource)     (CRD + controller)     Services (headless + clusterip)
                                                Secrets (passwords, certs)
                                                ConfigMaps (postgresql.conf)
                                                PodMonitors (metrics)
                                                Backup CronJobs (pg_basebackup)
                                                PV / PVC (per-pod, per-template)
                                                NetworkPolicies (lockdown)
```

The operator's value is twofold:

1.  **Higher-level abstraction.** Users say "I want 3 replicas with sync replication, automatic failover, WAL archiving to S3", not "here are 47 YAML files."
2.  **Reconciliation of application state.** The operator watches Pod status, detects failures, runs failover, manages version upgrades, executes maintenance jobs — all the things the raw StatefulSet does not do.

### 22.1 Postgres operators

**Zalando's postgres-operator** uses the `postgresql` CRD. A user creates:

```yaml
apiVersion: acid.zalan.do/v1
kind: postgresql
metadata:
  name: acid-minimal
spec:
  teamId: acid
  numberOfInstances: 3
  postgresql:
    version: "16"
  volume:
    size: 100Gi
  databases:
    foo: foo_owner
  users:
    foo_owner: [superuser, createdb]
```

The operator reconciles this into a StatefulSet, Services, Secrets, etc. It uses **Patroni** (an open-source Postgres HA tool) running inside each Pod to handle leader election via Kubernetes API or DCS. Patroni manipulates Pod labels to set `role: master` / `role: replica`, and the Services route traffic accordingly.

**CrunchyData / Percona** operators take similar approaches with different ergonomics.

### 22.2 Etcd operator

Historically, the etcd-operator (CoreOS) was one of the first widely deployed operators. It managed etcd clusters by creating StatefulSets and orchestrating member-add/member-remove via etcd's API. The project has been deprecated; current best practice for self-hosted etcd is `kubeadm` for control-plane clusters or the **etcd-druid** operator for application etcd. The pattern remains the same: CRD → operator → StatefulSet + lifecycle automation.

### 22.3 Kafka operators

**Strimzi** is the de facto Kafka operator. Its CRDs include `Kafka`, `KafkaTopic`, `KafkaUser`, `KafkaConnect`, `KafkaMirrorMaker`. A `Kafka` CR generates:

- A StatefulSet per broker pool.
- A StatefulSet for the controller pool (in KRaft mode) or ZooKeeper pool (in legacy mode).
- Services (headless + bootstrap + per-broker external).
- ConfigMaps with `server.properties`.
- Secrets with TLS certificates and SASL credentials.

Strimzi handles rolling upgrades carefully: it bumps brokers one at a time, waits for under-replicated partitions to converge to zero before proceeding. This is the kind of quorum-aware orchestration that raw StatefulSets cannot do.

### 22.4 Cassandra operators

**k8ssandra** (DataStax's open-source operator, with **cass-operator** at its core) manages Cassandra clusters. Its CRDs include `CassandraDatacenter`, `Reaper`, `MedusaBackup`. The operator orchestrates:

- StatefulSet per rack/datacenter.
- Headless Service with `publishNotReadyAddresses: true` (Cassandra needs peer discovery before readiness).
- `Parallel` podManagementPolicy (Cassandra handles join order itself).
- Repair scheduling via Reaper.
- Backup orchestration via Medusa.

### 22.5 MongoDB operators

**Percona** and **MongoDB Atlas Operator** wrap MongoDB ReplicaSets (the MongoDB clustering primitive, not to be confused with the Kubernetes ReplicaSet). Each MongoDB node is a Pod in a StatefulSet; the operator handles election parameters, secondary indexing, oplog tail backup, sharding (which involves multiple StatefulSets coordinated).

### 22.6 What operators add beyond raw StatefulSet

Across every operator, the value-add is roughly the same:

| Capability                     | Raw StatefulSet | Operator |
|--------------------------------|-----------------|----------|
| Stable identity + storage      | ✓               | ✓ (delegates) |
| Ordered creation/deletion      | ✓               | ✓ (delegates) |
| Rolling updates                | ✓ (template change) | ✓ (custom orchestration) |
| **Leader election / failover**  | ✗               | ✓ |
| **Version-aware upgrades**     | ✗               | ✓ |
| **Backup / restore**           | ✗               | ✓ |
| **Configuration management**   | ✗               | ✓ |
| **User / database / schema**   | ✗               | ✓ |
| **Quorum-aware operations**    | ✗               | ✓ |
| **Metrics / alerting setup**   | ✗               | ✓ |

The operator handles "what the application needs"; the StatefulSet handles "what every clustered workload needs."

---

## 23. Diagnosing "Pod Pending: PVC Unbound"

The most common StatefulSet operational issue is a Pod stuck in `Pending`, with the underlying reason being a PVC that won't bind. The diagnostic path:

### 23.1 First, check the Pod's events

```bash
kubectl describe pod db-0
```

Look at the **Events** section. Common messages:

- `FailedScheduling: ... persistentvolumeclaim "data-db-0" not found`
  → Race condition; usually transient. PVC is being created.
- `FailedScheduling: ... 0/3 nodes are available: 3 node(s) had volume node affinity conflict`
  → PVC bound to a PV in a different zone than any candidate node. Usually `volumeBindingMode: Immediate` strikes.
- `FailedScheduling: ... persistentvolumeclaim "data-db-0" is being deleted`
  → Previous PVC's finalizer hasn't run; controller waiting.
- `FailedScheduling: ... 0/3 nodes are available: 3 node(s) didn't find available persistent volumes to bind`
  → No PV matches the PVC, and no provisioner is dynamically provisioning.

### 23.2 Then, check the PVC

```bash
kubectl describe pvc data-db-0
```

Pay attention to:

- **Status**: `Pending` vs `Bound` vs `Lost`.
- **StorageClass**: does it exist? `kubectl get sc`.
- **VolumeName**: empty if not yet bound.
- **Events**: provisioner errors, capacity errors.

Common PVC failure modes:

```
PVC PENDING + no StorageClass set:
  → No default StorageClass in the cluster, and PVC doesn't specify one.
  Fix: set spec.storageClassName explicitly, or mark a default SC.

PVC PENDING + StorageClass exists, but provisioner not running:
  → CSI driver controller plugin is down, or external-provisioner sidecar crashed.
  Check: kubectl get pods -n kube-system -l app=ebs-csi-controller (or your driver)

PVC PENDING + provisioner running but failing:
  → kubectl get events --namespace=<ns> shows CSI errors.
  Common: cloud account out of quota, IAM permissions missing.

PVC PENDING + bound to a PV in wrong zone:
  → Pod cannot schedule in zone of PV. Usually Immediate binding.
  Fix: switch to WaitForFirstConsumer, delete the PVC, let it re-bind.

PVC LOST:
  → PV that was bound to this PVC has been deleted out from under it.
  Disaster: data is gone unless you have snapshots. See §24.
```

### 23.3 Check the PV

```bash
kubectl get pv
kubectl describe pv <pv-name>
```

A PV in `Available` state with the right size and access mode should bind. A PV in `Released` state was previously bound but has been released and not yet reclaimed (depends on `reclaimPolicy`).

For `reclaimPolicy: Retain`, a released PV stays around with the data; you can manually rebind it. For `reclaimPolicy: Delete`, the PV is deleted shortly after the PVC is deleted.

### 23.4 Check the StorageClass

```bash
kubectl describe sc <name>
```

- **Provisioner**: should be a known CSI driver name.
- **AllowVolumeExpansion**: relevant for §20.
- **VolumeBindingMode**: `Immediate` vs `WaitForFirstConsumer`.
- **Parameters**: type-specific (EBS type, IOPS, etc.).

### 23.5 The CSI controller logs

```bash
kubectl logs -n kube-system <csi-controller-pod> -c csi-provisioner
kubectl logs -n kube-system <csi-controller-pod> -c <driver-name>
```

CSI errors are usually verbose. Common: cloud API rate limits, AZ-specific outages, IAM permissions, capacity exhausted.

### 23.6 The diagnostic decision tree

```
Pod Pending: PVC not bound
  │
  ├─ PVC missing? → Wait or check controller logs
  │
  ├─ PVC Pending → check StorageClass
  │     │
  │     ├─ SC missing → set spec.storageClassName, or set default SC
  │     │
  │     ├─ SC has no provisioner → driver issue
  │     │
  │     ├─ SC volumeBindingMode=Immediate, zone mismatch
  │     │   → switch to WaitForFirstConsumer; delete PVC; recreate
  │     │
  │     └─ Provisioner failing → check CSI logs, cloud quotas, IAM
  │
  ├─ PVC Bound, but Pod won't schedule
  │     │
  │     ├─ Volume in wrong zone → only fix is recreate (data loss unless retained)
  │     │
  │     └─ Node selector / taint / affinity mismatch
  │
  └─ PVC Lost → data loss; see §24 disaster recovery
```

---

## 24. Disaster Recovery: Losing Pod-0

"Pod-0 dies, its PVC is gone" is the disaster scenario every StatefulSet operator should have a runbook for. Causes:

- Accidental `kubectl delete pvc data-db-0` (with retention=Delete or with the cascade of PV reclaim=Delete).
- Storage backend failure (EBS volume deleted out-of-band).
- Operator misconfiguration that ran `whenDeleted: Delete` and someone deleted the STS.

### 24.1 What's gone, what's left

If the PVC and PV are both gone:

- Pod-0 cannot start because the StatefulSet controller will try to create a new Pod bound to PVC `data-db-0`, which it expects to exist.
- The controller *will* recreate the PVC (from the volumeClaimTemplate) and provision a new, empty PV.
- The new Pod-0 starts with an empty volume. The application sees a "fresh" Pod with no data.

For a database, this is data loss. For a cluster member, it's a member that's now out of sync.

### 24.2 Recovery from snapshot

The standard recovery path:

```bash
# 1. Pause the StatefulSet so it doesn't recreate Pod-0 with empty storage.
#    Set replicas: 0 via patch (or scale down to 0). The PVCs (if retained)
#    survive. If they don't survive, you need to manually recreate them.
kubectl scale sts db --replicas=0

# 2. Identify the most recent snapshot for data-db-0.
kubectl get volumesnapshot -l app=db,ordinal=0

# 3. Create a new PVC with name "data-db-0" from the snapshot.
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data-db-0
  namespace: prod
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: gp3
  resources:
    requests:
      storage: 100Gi
  dataSource:
    name: db-0-snapshot-20260523
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
EOF

# 4. Scale back up. Pod-0 will bind to the new PVC (which now has snapshot data).
kubectl scale sts db --replicas=3
```

The new Pod-0 starts with data from the snapshot. The application replays any necessary WAL or re-syncs from peers (depending on the application).

### 24.3 Recovery via peer resync

Some applications can resync from peers if a member's storage is gone:

- **Etcd**: remove the lost member via `etcdctl member remove`, then add a new member via `etcdctl member add`. The new etcd-0 starts with empty data, joins as a learner, and snapshots from the leader.
- **Cassandra**: replace the node using `cassandra.replace_address_first_boot`. The new node bootstraps from peers using its assigned token.
- **Postgres replicas (not primary)**: rebuild via `pg_basebackup` from another replica.

For a *primary* whose data is gone with no snapshot: the cluster has lost data permanent to the primary's role. The promotion of a replica becomes a hard requirement, and any writes since the last replica sync are lost.

### 24.4 The orphan pattern

For complex recoveries, you may want to delete the StatefulSet *without* deleting the Pods or PVCs (orphan the children):

```bash
kubectl delete sts db --cascade=orphan
```

The Pods and PVCs survive; the StatefulSet object is gone. You can now:

- Manipulate individual Pods directly (kill them, restart them, modify their specs).
- Create new PVCs by hand.
- Eventually recreate the StatefulSet with `--replicas=3` and the same name; the controller will adopt the existing Pods (if their labels match) and PVCs.

This is heavy surgery, used only in DR scenarios. Operators (§22) often automate this via their own logic.

### 24.5 The "everything is gone" case

If you've lost everything — all replicas, all PVCs, all PVs, no snapshots — you're restoring from off-site backup (S3 WAL archive, Velero, application-level backup). The procedure:

1.  Bring up the StatefulSet from scratch.
2.  Restore the backup to Pod-0 (e.g., `pg_basebackup` followed by WAL replay from S3).
3.  Once Pod-0 is healthy, the other Pods join via replication.

Plan for this. Practice it. Production stateful workloads without a tested DR plan are a ticking bomb.

---

## 25. Migration Patterns for Stateful Data

"Move the StatefulSet to a different node pool" or "across clusters" sounds simple. It is not. The wrong way: `kubectl scale` and hope the storage rebinds. The right way involves explicit data motion.

### 25.1 Why `kubectl scale` does not move data across zones

Suppose your StatefulSet is in `us-east-1a` (because the PVs are there), and you want to move it to `us-east-1b`:

- Editing `nodeAffinity` to prefer `us-east-1b` does nothing: the existing PVs are pinned to `us-east-1a` and cannot attach to `us-east-1b` nodes.
- Scaling down to 0 and back up to 3 keeps the same PVCs (retain default), which are still bound to PVs in `us-east-1a`.
- You'd need to delete the PVCs to allow new ones to provision in `us-east-1b` — but that loses data.

### 25.2 The snapshot-restore migration pattern

```bash
# 1. Take snapshots of all PVCs in the old location.
for i in 0 1 2; do
  cat <<EOF | kubectl apply -f -
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: db-${i}-migration-snapshot
spec:
  source:
    persistentVolumeClaimName: data-db-${i}
EOF
done

# 2. Wait for snapshots to be readyToUse.
kubectl wait volumesnapshot/db-0-migration-snapshot --for=condition=ReadyToUse --timeout=10m
# (and so on for 1, 2)

# 3. Copy the snapshots to the new location (e.g., cross-region snapshot copy via cloud API).
aws ec2 copy-snapshot --source-region us-east-1 --source-snapshot-id snap-abc \
  --destination-region us-west-2 --description "migration"

# 4. Create VolumeSnapshotContent objects in the destination that reference the copied snapshots.

# 5. Create a new StatefulSet in the destination with the same name and spec,
#    but with PVCs prepopulated from the copied snapshots.
#    Note: you must create the PVCs before the StatefulSet adopts them,
#    or use the "orphan adoption" pattern.

# 6. Scale down the source, scale up the destination, swap DNS.
```

The same pattern works for cross-cluster migrations: snapshot, copy, restore in target cluster, swap traffic.

### 25.3 Application-level replication

A safer, smoother migration:

1.  Add the destination Pods as replicas of the source's primary.
2.  Wait for replication to catch up (replication lag → 0).
3.  Promote the destination's primary.
4.  Cut over client traffic.
5.  Decommission the source.

This is how cross-region database migrations work in practice: the application's own replication is used, and the StatefulSet is just the substrate. The migration is not a "StatefulSet operation"; it's a *database operation* on Pods that happen to be in StatefulSets.

### 25.4 The "rename a StatefulSet" trap

You cannot rename a StatefulSet. The PVC names are derived from the StatefulSet name; changing the name means new PVCs are created (empty), the old ones are abandoned.

To "rename": create a new StatefulSet, migrate data into it, delete the old one. There is no shortcut.

---

## 26. `whenDeleted=Delete`: The Auto-Cleanup Option

We covered the four combinations in §6; here we focus on the `whenDeleted=Delete` half. This option (GA in 1.32) tells the controller: when the StatefulSet is deleted, also delete all the PVCs.

```yaml
spec:
  persistentVolumeClaimRetentionPolicy:
    whenDeleted: Delete
    whenScaled: Retain
```

When the StatefulSet `db` is deleted:

1.  Controller observes the deletion (via finalizer).
2.  Controller (well, the GC) propagates deletion to PVCs `data-db-0, data-db-1, data-db-2`.
3.  Each PVC's deletion triggers the PV's reclaim policy (Delete → cloud volume gone; Retain → PV released, volume still exists).
4.  StatefulSet finalizer is removed; STS object is purged from etcd.

### 26.1 Why anyone would want this

- **Test fixtures**: spin up a stateful test environment, run tests, tear it all down. No orphan PVs.
- **CI clusters**: ephemeral StatefulSets in CI pipelines that should clean up after themselves.
- **Development environments**: dev clusters with auto-cleanup avoid the "abandoned PV charges" problem.

For production, the default `Retain` is almost always the safe choice. If you accidentally `kubectl delete statefulset db` in a Retain environment, the data survives; you can recreate the STS and reattach. In a Delete environment, it's gone forever.

### 26.2 The Retain → Delete migration

If you have an existing StatefulSet with `Retain` (default) and want to switch to `Delete`, you patch:

```bash
kubectl patch sts db --type=merge -p '{"spec":{"persistentVolumeClaimRetentionPolicy":{"whenDeleted":"Delete","whenScaled":"Retain"}}}'
```

The controller reconciles owner references on PVCs: it adds an ownerRef pointing back to the StatefulSet with `blockOwnerDeletion: true`. Now, deleting the STS cascades to delete the PVCs.

### 26.3 The Delete → Retain migration

Reverse: if you currently have `Delete` and want to switch back to safer `Retain`:

```bash
kubectl patch sts db --type=merge -p '{"spec":{"persistentVolumeClaimRetentionPolicy":{"whenDeleted":"Retain"}}}'
```

Controller removes the ownerRef from PVCs. Now, deleting the STS leaves the PVCs in place.

### 26.4 Recommendation

For everything that holds data you care about: `Retain / Retain`. The cost of orphan PVCs is small (a quarterly audit reveals them); the cost of accidental data loss is enormous.

Use `Delete` only for genuinely ephemeral data, and gate it behind explicit operator policy (Kyverno: "deny any StatefulSet in prod namespace with whenDeleted=Delete").

---

## 27. The "Ordinal 0 Is Leader" Myth

It is a widespread folk belief that `pod-0` is "the leader" or "the primary" in a StatefulSet. This is half true at best.

### 27.1 The technical reality

The StatefulSet controller has no concept of leadership. Pod-0 is just the lowest ordinal. It is:

- The first Pod created (under OrderedReady).
- The last Pod deleted during scale-down.
- The last Pod replaced during rolling update.

That's it. There is no API field that says "this is the primary." There is no health check that promotes a Pod. The application decides who leads.

### 27.2 Why the myth persists

Many production deployments label `pod-0` as the primary because:

- It's the most stable: longest-lived, least likely to be replaced during rolling updates.
- Bootstrap scripts often assume ordinal 0 is special (e.g., "ordinal 0 initializes the cluster; others join").
- Operators frequently set `role: master` on `pod-0` by default until something promotes another Pod.

For etcd, the leader is whoever wins the Raft election; it can be any ordinal. For Postgres with Patroni, the leader is whoever holds the leader lock in the DCS; can be any ordinal. For Kafka KRaft, the controller leader is whoever wins the Raft election among controllers.

### 27.3 The asymmetry that *is* real

Even without explicit leadership, ordinal 0 is asymmetric in two ways:

1.  **Bootstrap order.** Many applications boot ordinal 0 first (under OrderedReady) and treat it as the "first member" — its presence is required for the cluster to come up. Higher ordinals join an existing cluster.
2.  **Rolling update order.** Updates proceed from highest ordinal to ordinal 0. Ordinal 0 is replaced last. If you can only afford to lose one Pod at a time during an upgrade, you want the leader to be the last one taken down — and ordinal 0 is the last replaced by default.

So even though there is no inherent leadership, the *convention* of "ordinal 0 is the most stable instance" is real, and operators exploit it.

### 27.4 What you should not do

Do not write application logic that *requires* ordinal 0 to be the primary. The Pod might be evicted, restarted, or replaced; if your code assumes "always read from db-0", it will fail when db-0 is briefly Pending.

Always use a Service (ClusterIP, with label-based selectors) for the primary endpoint. Let the operator label whichever Pod is currently primary, and let clients hit the Service. The StatefulSet handles identity; the Service handles routing.

---

## 28. `minReadySeconds` and Rollout Pacing

`spec.minReadySeconds` (added to StatefulSet in 1.25 GA) is the number of seconds a Pod must be Ready before it counts as "available" for the purpose of rolling updates.

```yaml
spec:
  minReadySeconds: 30
```

With this setting:

- During a rolling update, after the controller replaces a Pod and the new Pod becomes Ready, the controller waits 30 seconds before proceeding to the next Pod.
- This gives any traffic / cluster-membership effects time to stabilize.

### 28.1 Why pacing matters

A common failure mode: rolling update replaces Pod-2; it becomes Ready immediately (probe passes); controller deletes Pod-1; but Pod-2 was *not yet fully integrated* (cluster sync hadn't completed, replication slot hadn't caught up, ZooKeeper session hadn't stabilized). Now Pod-1 is gone and Pod-2 isn't really serving; cluster goes degraded.

`minReadySeconds: 30` says: wait 30 seconds after Pod-2 reports Ready before declaring it available and moving on. The pacing gives the cluster time to absorb the change.

The tradeoff: longer rollouts. With 3 replicas, the rollout adds 3 × 30s = 90s to the total time. With 50 replicas, it's 25 minutes. Tune to your application's stabilization time.

### 28.2 Same field as Deployment

This field is shared with the Deployment controller (where it's been around since 1.6). The semantics are identical. For both controllers, "available" requires `Ready` + `minReadySeconds` elapsed.

The `maxUnavailable` calculation uses "available", not "ready" — so during the `minReadySeconds` window after a Pod becomes Ready, it does not count toward the budget. This prevents the controller from blowing past `maxUnavailable` due to fast probe success.

---

## 29. Observability: Metrics, Conditions, Alerts

Operating a StatefulSet at scale requires telemetry beyond `kubectl get`.

### 29.1 `kube-state-metrics`

`kube-state-metrics` exports object-level metrics from the Kubernetes API. The relevant ones for StatefulSets:

```
kube_statefulset_replicas{statefulset="db", namespace="prod"}                3
kube_statefulset_status_replicas{statefulset="db", namespace="prod"}         3
kube_statefulset_status_replicas_ready{statefulset="db", namespace="prod"}   3
kube_statefulset_status_replicas_current{statefulset="db", namespace="prod"} 3
kube_statefulset_status_replicas_updated{statefulset="db", namespace="prod"} 3
kube_statefulset_status_observed_generation{statefulset="db", namespace="prod"}  17
kube_statefulset_metadata_generation{statefulset="db", namespace="prod"}     17
kube_statefulset_status_current_revision{statefulset="db", revision="db-6b8d..."} 1
kube_statefulset_status_update_revision{statefulset="db", revision="db-6b8d..."}  1
```

And per-PVC:

```
kube_persistentvolumeclaim_status_phase{persistentvolumeclaim="data-db-0", phase="Bound"} 1
kube_persistentvolumeclaim_resource_requests_storage_bytes{persistentvolumeclaim="data-db-0"} 1.07e11
```

### 29.2 Useful alerts

```yaml
# Pod missing in a critical StatefulSet
- alert: StatefulSetReplicasMismatch
  expr: |
    kube_statefulset_status_replicas_ready{namespace="prod"}
      != kube_statefulset_replicas{namespace="prod"}
  for: 5m
  annotations:
    summary: "StatefulSet {{ $labels.statefulset }} has fewer ready replicas than desired."

# StatefulSet rollout stuck
- alert: StatefulSetRolloutStuck
  expr: |
    kube_statefulset_status_observed_generation
      != kube_statefulset_metadata_generation
  for: 30m
  annotations:
    summary: "StatefulSet {{ $labels.statefulset }} rollout stuck (generation mismatch)."

# PVC not bound
- alert: PvcUnbound
  expr: |
    kube_persistentvolumeclaim_status_phase{phase="Pending"} == 1
  for: 10m
  annotations:
    summary: "PVC {{ $labels.persistentvolumeclaim }} has been Pending for 10+ minutes."

# PVC capacity at risk
- alert: PvcFillingUp
  expr: |
    (kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes) > 0.85
  for: 1h
  annotations:
    summary: "PVC {{ $labels.persistentvolumeclaim }} is over 85% full."
```

### 29.3 Per-Pod readiness gates as observability

Recall §15.3: readinessGates allow custom conditions on Pod readiness. They are also a great observability primitive — the condition reflects the operator's view of the application's health, beyond raw probe success.

```yaml
status:
  conditions:
  - type: Ready
    status: "True"
  - type: ContainersReady
    status: "True"
  - type: db.example.com/replication-caught-up
    status: "True"
    lastTransitionTime: 2026-05-23T14:32:01Z
    reason: ReplicationCaughtUp
    message: "Replication lag: 0.3s"
```

Querying these conditions across the StatefulSet gives a real-time view of cluster health beyond what kube-state-metrics exports by default.

### 29.4 Etcd / database-specific metrics

Beyond Kubernetes-level metrics, you need application-specific telemetry:

- **Postgres**: `pg_stat_replication`, `pg_stat_wal`, replication lag.
- **Etcd**: `etcd_server_has_leader`, `etcd_server_leader_changes_seen_total`, `etcd_disk_wal_fsync_duration_seconds`.
- **Kafka**: `kafka_server_replicamanager_underreplicatedpartitions`, `kafka_controller_kafkacontroller_offlinepartitionscount`.
- **Cassandra**: `cassandra_compactions_pending`, `cassandra_read_latency`, `cassandra_repair_status`.

These come from each application's metrics endpoint; Prometheus scrapes them via PodMonitors / ServiceMonitors (ch 30).

---

## 30. Pitfalls

A consolidated list of mistakes that production StatefulSets repeatedly hit. Most are subtle and easy to do by accident.

1.  **Assuming Deployment-like in-place updates.** A StatefulSet rolling update *deletes* the Pod and creates a new one. The Pod UID and IP change. Anything caching them breaks. Use DNS, not cached IPs.

2.  **Changing `volumeClaimTemplates` after creation.** Immutable. The apiserver rejects the change. To "change" templates: orphan-delete the STS, manually adjust PVCs or create new ones, recreate the STS.

3.  **Changing `spec.serviceName` after creation.** Immutable. If you want a new Service name, you must recreate the StatefulSet.

4.  **Using a non-headless Service for `serviceName`.** Pods will not get per-Pod DNS records. `db-0.db.ns.svc.cluster.local` will return NXDOMAIN. Bootstrap will fail in confusing ways.

5.  **Deleting a PVC while a Pod still uses it.** The PVC enters terminating state but won't actually delete until the Pod releases it (it has a finalizer). If you force-delete the PVC and the Pod, the underlying PV may still be attached to a (now-orphan) cloud volume.

6.  **`whenDeleted=Delete` in production.** One accidental `kubectl delete sts` and all data is gone. Use `Retain` for production, gate `Delete` behind admission policy.

7.  **Pod-N joining with stale data after PVC delete-and-recreate.** If you delete `data-db-1` and recreate it (perhaps from an old snapshot), the new Pod-1 has stale state. It joins the cluster claiming to be ordinal 1, but the other peers have moved on. Many applications detect and reject this, but some don't and corrupt themselves.

8.  **Using `nodeName` on a StatefulSet Pod.** Pins the Pod to a node permanently. Node failure = permanent unschedulable Pod. Use nodeSelector / nodeAffinity instead, and let the scheduler choose.

9.  **Excessive `maxUnavailable` on a quorum-based system.** Updating 2/3 etcd Pods simultaneously loses quorum. Keep `maxUnavailable: 1` for quorum systems.

10. **No PodDisruptionBudget on stateful workloads.** A node drain (during cluster upgrade) can evict multiple Pods at once. With a PDB (`minAvailable: 2` for a 3-replica STS), drains are paced.

11. **PVC bound to a PV in the wrong zone.** With `volumeBindingMode: Immediate`, the PV is provisioned eagerly in some zone; the Pod can only be scheduled in that zone. If the zone is full or down, the Pod is stuck. Use `WaitForFirstConsumer`.

12. **Probe configured for liveness when it should be readiness.** Liveness restarts the container; readiness removes it from endpoints. For "cluster sync in progress" or "warming caches", readiness is correct.

13. **No `terminationGracePeriodSeconds` tuning.** The default 30s is too short for many databases. A database that hasn't flushed WAL before SIGKILL will replay on next start (slow) or, in edge cases, be inconsistent.

14. **Ignoring the `currentRevision` / `updateRevision` split.** After a failed rollout, the STS sits with mixed revisions. `kubectl get sts -o yaml` shows it; metrics show it. If you don't notice, you have a half-upgraded cluster.

15. **Assuming Pods come back with the same IP.** They don't. Always re-resolve DNS. Never persist Pod IPs anywhere.

16. **Forgetting `publishNotReadyAddresses: true` for self-discovery.** Cluster bootstrap deadlocks when peers can't resolve each other before readiness.

17. **Mounting a single PVC into multiple Pods.** Possible only with RWX (ReadWriteMany) access mode. Most block storage is RWO. Trying to use one PVC across the StatefulSet (instead of per-Pod via templates) fails with attach errors.

18. **Confusing `spec.replicas` with high-availability.** A 3-replica StatefulSet on 1 node is *not* highly available. Use topology constraints (§18).

19. **No backup strategy.** Snapshots are not enough (crash-consistent ≠ application-consistent). Use the application's backup tool, scheduled regularly, with off-cluster storage.

20. **Forgetting that PVCs survive everything by default.** Deleting and recreating a StatefulSet does *not* wipe data. New Pods bind to existing PVCs and see old data. If you actually want a clean slate, delete the PVCs too.

21. **Running the StatefulSet under default RBAC for the operator.** Operators need permissions on Pods, PVCs, Services, sometimes the StatefulSet itself. Audit the role; over-permissive operator roles are a common privilege escalation path.

22. **Mixing different applications in one StatefulSet.** Don't. One StatefulSet = one application. Different applications have different scale, different update cadences, different probes.

23. **Using a default StorageClass that's slow.** A StatefulSet provisioned against a `standard` (HDD) storage class will be I/O-bound for any real database. Specify the StorageClass explicitly; benchmark with your workload.

24. **Trusting CSI snapshot atomicity across PVCs.** A snapshot of `data-db-0` and `wal-db-0` taken in two separate VolumeSnapshot objects is *not* atomic. Use application-level coordination, or single-PVC layouts.

25. **Using zone-locked storage with cross-zone scheduling.** EBS in AWS, persistent disks in GCP — these are zonal. Once a Pod's PV is provisioned in zone A, the Pod is stuck in zone A. To "move", you need a snapshot + restore in the target zone.

26. **Scaling down a StatefulSet without considering PDBs.** The StatefulSet controller does scale-down via direct Pod delete, bypassing the eviction API. PDBs don't help. If you're operating quorum systems, scale down carefully.

27. **Not setting `minReadySeconds` for slow-stabilizing applications.** Rolling updates may race ahead before clusters stabilize. `minReadySeconds: 30-60` is a cheap insurance.

28. **Using `nodeSelector` without considering scheduler resilience.** If your StatefulSet has `nodeSelector: workload=db`, and all `db` nodes are down or cordoned, Pods are stuck. Have a fallback or autoscale the pool.

29. **Forgetting that ordinals start at 0 by default and conflict with `start` offset.** When using `spec.ordinals.start` (1.27+ alpha), you must keep ordinal ranges unique across StatefulSets that share a headless Service. Conflicting ordinals = colliding DNS names.

30. **Putting application secrets in `volumeClaimTemplates`-mounted volumes.** Secrets belong in `secret` or `configMap` volumes, not in the persistent data volume. If you persist them, every snapshot/backup contains them; rotation becomes a nightmare.

31. **Ignoring `kubelet_volume_stats_*` metrics.** PVC fill rate is the silent killer. Set alerts at 85% to give time to expand or clean up before hitting 100%.

32. **Not testing DR.** Every stateful workload needs a documented, tested DR procedure. The first time you try to restore from snapshot during an outage is the worst time to discover that the snapshot is incompatible with the current version.

---

## 31. TL;DR

**A StatefulSet is the Kubernetes primitive for workloads where individual Pods are not interchangeable.** Where a Deployment treats Pods as a herd, a StatefulSet treats them as a numbered cohort. The four guarantees — stable hostname, stable network identity, stable per-Pod storage, ordered lifecycle — let stateful applications encode their topology in DNS, persist data across Pod restarts, and bootstrap clusters without an external service registry. Pod-0 is not Pod-1. The PVC `data-db-0` is bound to ordinal 0 forever, surviving Pod restarts, rolling updates, node failures, and StatefulSet deletion (under the default Retain retention policy).

The control flow is a tight reconcile loop: list Pods sorted by ordinal, create the first missing one, wait for Ready (under `OrderedReady`), then proceed; for deletion, work top-down in reverse ordinal order. Rolling updates replace Pods in descending order, leaving ordinal 0 for last — this is the basis of the "ordinal 0 is special" convention. Partitioned rollouts (`spec.updateStrategy.rollingUpdate.partition`) provide ordinal-level canarying for stateful workloads; `maxUnavailable` allows parallel update of a quorum-safe fraction.

The bootstrap problem — how do clustered members find each other before the cluster exists? — is solved by predictable DNS. Headless Services (`clusterIP: None`) publish per-Pod A records of the form `<sts>-<ordinal>.<svc>.<ns>.svc.cluster.local`. Every stateful application boots, computes its peer list from the StatefulSet name + ordinal + headless service, and joins. Etcd, Kafka KRaft, Postgres-with-Patroni, Cassandra, MongoDB — all of them rely on this contract.

What StatefulSets do *not* do: leader election, failover, schema migration, backup orchestration, quorum-aware rolling updates. That's where operators come in. The Zalando/CrunchyData/Percona Postgres operators, Strimzi Kafka operator, k8ssandra Cassandra operator, MongoDB operators — they all wrap a StatefulSet (often multiple) and add application-aware automation on top. The StatefulSet is the substrate; the operator is the application.

For production: always use `WaitForFirstConsumer` storage classes (to avoid zone-locked PV traps), set PodDisruptionBudgets (to survive node drains), use anti-affinity or topology-spread to put one Pod per zone, configure `terminationGracePeriodSeconds` generously, prefer `Retain/Retain` PVC retention unless you really know what you're doing, mount PVCs via `volumeClaimTemplates` (never share a PVC across Pods unless RWX), tune readiness probes to reflect "I have joined the cluster" not just "I am listening on port", and test your disaster recovery procedure before you need it.

The intuition to carry: **identity comes from ordinal, addressability comes from DNS, durability comes from PVC, order comes from policy, and everything application-specific lives in the operator.** Internalize that and the StatefulSet stops being mysterious and becomes a small, sharp tool that does exactly one thing — preserve identity and storage for numbered Pods — and lets the rest of the stateful complexity live in the right layer.
