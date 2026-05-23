# Garbage Collection and Object Lifecycle: A Staff-Level Deep Dive

A staff-engineer reference for the part of Kubernetes that nobody thinks about until something is stuck `Terminating` for six hours, or a cloud LoadBalancer that should have been deleted last week is still billing $0.025/hr. Garbage collection in Kubernetes is *not* a memory subsystem; it is a distributed, eventually-consistent ownership graph implemented by a controller that watches the entire cluster and a two-phase delete protocol layered on top of every REST verb. This chapter walks the protocol, the controller, the finalizer pattern, the five built-in finalizers, the three cascade policies, the TTL controllers, and the long catalogue of ways operator authors get this wrong.

This chapter sits on top of chapter 05 (apiserver delete handler — the place finalizers and deletionTimestamp are enforced), chapter 08 (controller pattern — the reconciler that adds and removes finalizers), and chapter 23 (CRDs and operators — where `metadata.ownerReferences` is the *only* way the GC controller knows your CR owns that ConfigMap). If chapter 05 taught you what `DELETE /api/v1/pods/x` does on the apiserver side, and chapter 08 taught you what a reconciler does when it sees `DeletionTimestamp != nil`, this chapter ties them together: the protocol that lets the apiserver and an arbitrary set of controllers agree, asynchronously, on when an object is truly gone.

The model is deceptively simple and almost universally misunderstood. There is no transactional cascade in etcd. There is no foreign-key constraint. There is no synchronous "delete owner deletes children" path. There is a single boolean (`metadata.deletionTimestamp`), a list of strings (`metadata.finalizers`), a list of references (`metadata.ownerReferences`), three propagation policies on the wire, and a controller in `pkg/controller/garbagecollector` that builds a graph in RAM and chases pointers. Everything else — chained deletions, cascade behaviour, "stuck Terminating", the operator-finalizer pattern, namespace deletion, PV protection — is emergent from those four primitives.

---

## Table of Contents

1. [The Object Lifecycle in One Picture](#1-the-object-lifecycle-in-one-picture)
2. [The Two-Phase Delete Model](#2-the-two-phase-delete-model)
3. [`metadata.ownerReferences`](#3-metadataownerreferences)
4. [`controller=true` and Single-Owner Semantics](#4-controllertrue-and-single-owner-semantics)
5. [`blockOwnerDeletion=true`](#5-blockownerdeletiontrue)
6. [The Garbage Collector Controller](#6-the-garbage-collector-controller)
7. [Cascade Policies: Background, Foreground, Orphan](#7-cascade-policies-background-foreground-orphan)
8. [The `kubectl --cascade` Flag and the Propagation Header](#8-the-kubectl---cascade-flag-and-the-propagation-header)
9. [Finalizers: The Wire Protocol](#9-finalizers-the-wire-protocol)
10. [The Canonical Finalizer Reconciler Loop](#10-the-canonical-finalizer-reconciler-loop)
11. [Why Finalizers Live On The Object](#11-why-finalizers-live-on-the-object)
12. [Built-In Finalizers](#12-built-in-finalizers)
13. [External Finalizers in Operators](#13-external-finalizers-in-operators)
14. [The "Stuck Deletion" Problem and Manual Recovery](#14-the-stuck-deletion-problem-and-manual-recovery)
15. [The Ownership Graph in Practice](#15-the-ownership-graph-in-practice)
16. [OwnerRef Invariants and Adoption](#16-ownerref-invariants-and-adoption)
17. [The Owner-Namespace Rule](#17-the-owner-namespace-rule)
18. [CRD `ownerReferences`: The Cleanup-Without-Finalizer Pattern](#18-crd-ownerreferences-the-cleanup-without-finalizer-pattern)
19. [TTLAfterFinished Controller](#19-ttlafterfinished-controller)
20. [The Pod GC Controller](#20-the-pod-gc-controller)
21. [Event TTL and Lease GC](#21-event-ttl-and-lease-gc)
22. [The "Node Deleted" Cascade](#22-the-node-deleted-cascade)
23. [Foreground Deletion: The Full Dance](#23-foreground-deletion-the-full-dance)
24. [Foreground vs Background: Performance](#24-foreground-vs-background-performance)
25. [Orphan: When To Use It](#25-orphan-when-to-use-it)
26. [Ownership Cycles](#26-ownership-cycles)
27. [Cascading Delete Bugs in Operators](#27-cascading-delete-bugs-in-operators)
28. [The `deletecollection` Verb](#28-the-deletecollection-verb)
29. [Server-Side Apply and Finalizers](#29-server-side-apply-and-finalizers)
30. [Audit and Forensics](#30-audit-and-forensics)
31. [Common Operator Finalizer Patterns](#31-common-operator-finalizer-patterns)
32. [Diagnosing Stuck-Terminating Objects](#32-diagnosing-stuck-terminating-objects)
33. [Best Practices for Operator Authors](#33-best-practices-for-operator-authors)
34. [GC Controller Observability](#34-gc-controller-observability)
35. [Pitfalls: The Long List](#35-pitfalls-the-long-list)
36. [TL;DR](#36-tldr)

---

## 1. The Object Lifecycle in One Picture

Before we get into protocol details, fix the mental model. Every Kubernetes object has exactly three phases. Two of them are visible to clients. One is invisible.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Lifecycle of any Kubernetes object                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   POST /pods                                                                │
│      │                                                                      │
│      ▼                                                                      │
│   ┌──────────┐                                                              │
│   │ CREATED  │   metadata.deletionTimestamp == nil                          │
│   │          │   metadata.finalizers may be set                             │
│   │          │   normal reconciliation                                      │
│   └────┬─────┘                                                              │
│        │                                                                    │
│        │ DELETE /pods/x   (propagationPolicy=Background|Foreground|Orphan)  │
│        ▼                                                                    │
│   ┌──────────────┐                                                          │
│   │ TERMINATING  │   metadata.deletionTimestamp != nil                      │
│   │              │   metadata.finalizers != [] (one or more left)           │
│   │              │   reconciler does external cleanup                       │
│   │              │   reconciler removes its finalizer when done             │
│   └──────┬───────┘                                                          │
│          │                                                                  │
│          │ last finalizer removed                                           │
│          ▼                                                                  │
│   ┌──────────────┐                                                          │
│   │  GONE        │   etcd row physically deleted by apiserver               │
│   │              │   subsequent GET → 404                                   │
│   │              │   audit event 'delete' emitted                           │
│   └──────────────┘                                                          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

The middle state — `TERMINATING` — is where ninety percent of the difficulty lives. It is a perfectly visible, GET-able, list-able, watch-able state in which the object is "about to be deleted" but is not yet. Clients can still read it, controllers can still observe it, the apiserver still accepts (some) updates to it, but no creates are allowed under the same name (we will see why below).

The key fact: **the apiserver itself never decides to remove an object from etcd while finalizers is non-empty.** That single rule is the entire foundation of the cleanup protocol.

---

## 2. The Two-Phase Delete Model

Kubernetes does not implement DELETE as a single etcd transaction. It implements DELETE as a *protocol*. The protocol has two phases, separated by an arbitrary amount of wall-clock time controlled by the controllers responsible for the object.

### 2.1 Phase 1: Mark for deletion

When a client sends `DELETE /api/v1/namespaces/default/pods/x`, the apiserver's delete handler in `staging/src/k8s.io/apiserver/pkg/registry/generic/registry/store.go` (the `Delete` method on `Store`) executes the following pseudocode:

```
func (e *Store) Delete(ctx, name, options, validation, deleteValidation) {
    // 1. Load the current object
    existing := storage.Get(ctx, key)

    // 2. Run prepareForDelete strategy hook (e.g., set deletionGracePeriodSeconds)
    e.DeleteStrategy.CheckGracefulDelete(ctx, existing, options)

    // 3. Decide: is this a "graceful" delete (object has finalizers OR
    //    needs grace period like Pods), or a hard delete?
    if shouldUpdateFinalizers(existing, options) ||
       hasNonEmptyFinalizers(existing) ||
       gracefulDeleteNeeded(existing, options) {

        // ---- PHASE 1 ----
        // Don't delete the row. Update it with:
        //   metadata.deletionTimestamp = now
        //   metadata.deletionGracePeriodSeconds = options.GracePeriodSeconds
        //   metadata.finalizers may be MODIFIED (foreground/orphan adds one)
        return e.updateForGracefulDeletionAndFinalizers(...)
    }

    // ---- PHASE 2 (only reached when finalizers is empty AND no grace period) ----
    storage.Delete(ctx, key)
}
```

The decision tree:

```
   apiserver receives DELETE
            │
            ▼
   ┌──────────────────┐
   │ Is finalizers    │   YES ──► UPDATE with deletionTimestamp=now;
   │ non-empty?       │           return object to client (200, not 204)
   └────────┬─────────┘
            │ NO
            ▼
   ┌──────────────────┐
   │ Does propagation │   FG  ──► add foregroundDeletion finalizer;
   │ policy add a     │           UPDATE with deletionTimestamp; return
   │ finalizer?       │   Orph──► add orphan finalizer; same as above
   └────────┬─────────┘
            │ Bg / nil
            ▼
   ┌──────────────────┐
   │ Object has a     │   YES ──► UPDATE with deletionTimestamp;
   │ grace period?    │           return; kubelet/controller deletes later
   │ (e.g. Pod)       │
   └────────┬─────────┘
            │ NO
            ▼
   ┌──────────────────┐
   │ Hard delete:     │
   │ etcd DELETE      │
   │ row gone now     │
   └──────────────────┘
```

### 2.2 Phase 2: actual etcd removal

The apiserver removes the etcd row when, and only when, a subsequent UPDATE (typically a finalizer-removing patch from a controller) results in `metadata.finalizers == []` *and* the deletionTimestamp is already set. The relevant code path is in the same `Store` file: `updateForGracefulDeletionAndFinalizers` checks the post-update state in a transaction; if it observes an empty finalizers list and a non-nil deletionTimestamp, it issues the etcd `Delete` rather than a `Put`.

This is the critical atomicity guarantee: **finalizer-removal + etcd-delete happen in the same RV transaction**, in the same handler. There is no window where a controller can observe "finalizers=[] but still in etcd" and act on it. Either you see the object with finalizers, or you don't see it at all.

### 2.3 Why two phases exist

Phase 1 exists for one reason: **cleanup beyond etcd**. Kubernetes objects are pointers to real-world state. A Pod points to a kernel cgroup, namespaces, network endpoints, mounted volumes. A PersistentVolume points to an EBS volume, an Azure Disk, an NFS export. A Service of type LoadBalancer points to a cloud LB worth real money. An ExternalSecret points to a Vault path. A CRD `KafkaTopic` points to a topic in an actual Kafka cluster.

If DELETE atomically removed the etcd row, the controller responsible for that external state would never see the deletion and would never clean up. The external resource would orphan. The two-phase model is Kubernetes' answer: the etcd row stays alive as a *grave marker* — visible, GET-able, with `deletionTimestamp` set — until every interested controller has had a chance to clean its corresponding external state and signal completion by removing its finalizer.

### 2.4 A trace

```
T=0.000  client: kubectl delete kafkatopic orders
T=0.002  apiserver: DELETE handler invoked
T=0.003  apiserver: load object, sees finalizers=["kafka.strimzi.io/topic-cleanup"]
T=0.004  apiserver: UPDATE object: deletionTimestamp=2026-05-23T10:00:00Z
T=0.004  apiserver: respond 200 OK with the updated object
T=0.005  client: prints "kafkatopic.kafka.strimzi.io/orders deleted"  (LIE)
T=0.500  strimzi-topic-operator: watch event MODIFIED; sees deletionTimestamp
T=0.501  operator: connect to Kafka, issue DeleteTopic("orders")
T=1.200  Kafka: topic deleted, broker ACK
T=1.210  operator: PATCH kafkatopic remove finalizer
T=1.211  apiserver: UPDATE handler sees finalizers=[], deletionTimestamp != nil
T=1.211  apiserver: etcd DELETE row
T=1.212  apiserver: emit audit event 'delete'
```

Note T=0.005: kubectl says "deleted" *before the object is gone*. This is the source of endless confusion. The CLI is reporting that the DELETE call returned successfully, not that the object is gone. To wait for actual deletion, use `kubectl wait --for=delete kafkatopic/orders`.

---

## 3. `metadata.ownerReferences`

The ownership graph is encoded in a per-object metadata field: `metadata.ownerReferences`, an array. Each entry points to *another* object that is conceptually the parent of this one. The GC controller (section 6) reads these refs and uses them to cascade deletions.

The full schema, from `staging/src/k8s.io/apimachinery/pkg/apis/meta/v1/types.go`:

```go
type OwnerReference struct {
    APIVersion          string `json:"apiVersion"`
    Kind                string `json:"kind"`
    Name                string `json:"name"`
    UID                 types.UID `json:"uid"`
    Controller          *bool  `json:"controller,omitempty"`
    BlockOwnerDeletion  *bool  `json:"blockOwnerDeletion,omitempty"`
}
```

A real example: a ReplicaSet owned by a Deployment.

```yaml
apiVersion: apps/v1
kind: ReplicaSet
metadata:
  name: webapp-7d8c5b9f8c
  namespace: default
  uid: 7c2e1b4a-5d6f-4a3b-8e9c-1234567890ab
  ownerReferences:
  - apiVersion: apps/v1
    kind: Deployment
    name: webapp
    uid: 1a2b3c4d-5e6f-7890-abcd-ef0123456789
    controller: true
    blockOwnerDeletion: true
spec:
  ...
```

The Pod under that ReplicaSet:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: webapp-7d8c5b9f8c-x9k2q
  namespace: default
  ownerReferences:
  - apiVersion: apps/v1
    kind: ReplicaSet
    name: webapp-7d8c5b9f8c
    uid: 7c2e1b4a-5d6f-4a3b-8e9c-1234567890ab
    controller: true
    blockOwnerDeletion: true
```

The graph (Deployment → ReplicaSet → Pod) is reconstructed entirely from these refs.

### 3.1 Why UID, not name

The most common newbie mistake on owner refs is to assume `name` is what matters. It is not. The GC controller only believes a ref is valid if **`UID` matches** the live object's UID at the time of lookup. Names get reused. UIDs do not.

Consider:

```
T=0   create Pod "frontend"               → UID = aaaa-1111
T=10  create CM owned by Pod "frontend"   → ownerRef.UID = aaaa-1111
T=20  delete Pod "frontend"
T=30  create Pod "frontend" (same name)   → UID = bbbb-2222
```

If owner refs were name-based, the new Pod would automatically "adopt" the orphaned ConfigMap from the old Pod — almost certainly wrong (the new Pod might be a different workload entirely with the same label). With UID-based refs, the ConfigMap's `ownerRef.UID = aaaa-1111` no longer matches *any* live Pod, so the GC controller will (a) detect the orphan, (b) delete the ConfigMap if no other owners exist, or (c) remove the stale ownerRef.

This is one of the most important invariants in Kubernetes' metadata model: **identity = UID, not name**. Anything that conflates them (a controller that re-attaches by name, an admission webhook that rewrites refs, a tool that copies objects across clusters preserving names but not UIDs) is broken in subtle ways.

### 3.2 The full set of fields

| Field | Required? | Meaning |
|-------|-----------|---------|
| apiVersion | yes | `apps/v1`, `v1`, `kafka.strimzi.io/v1beta2` etc. |
| kind | yes | The Kind of the owner (`Deployment`, `Pod`, `KafkaCluster`). |
| name | yes | Owner's metadata.name. Used for display / lookup. |
| uid | yes | Owner's metadata.uid. The *real* identifier. |
| controller | optional, default false | At most one ref per object may have `controller=true`. |
| blockOwnerDeletion | optional, default false | Foreground deletion will wait for this dependent. |

There is no relationship field for *what kind* of ownership. The semantics of "owns" are implicit and uniform: deleting the owner cascades (subject to policy) to the dependent. Any further semantics (e.g., "ConfigMap is the spec, Deployment is the workload") are encoded in your reconciler logic, not in the ref.

---

## 4. `controller=true` and Single-Owner Semantics

A given object may have multiple `ownerReferences`. They can all point to different parents. At most one of them is allowed to have `controller=true`. This special ref designates *the* controlling owner — the one whose reconciler is authoritative for the dependent.

The apiserver enforces the at-most-one rule. From `staging/src/k8s.io/apimachinery/pkg/apis/meta/v1/validation/validation.go`:

```go
func ValidateOwnerReferences(ownerReferences []OwnerReference, fldPath *field.Path) field.ErrorList {
    var allErrs field.ErrorList
    firstControllerName := ""
    for i, ref := range ownerReferences {
        if ref.Controller != nil && *ref.Controller {
            if firstControllerName != "" {
                allErrs = append(allErrs, field.Invalid(fldPath.Index(i),
                    ref, "Only one reference can have Controller=true"))
            }
            firstControllerName = ref.Name
        }
    }
    return allErrs
}
```

Why does this matter? Two reasons.

**Reason 1: adoption.** When a controller (e.g., a ReplicaSet's reconciler) scans for Pods matching its label selector, it must decide whether to adopt orphans. The rule, encoded in `controllerRefManager` (`pkg/controller/controller_ref_manager.go`), is: only adopt a pod that has *no* controller ref. If the pod has `controller=true` pointing at someone else, you must not steal it. This prevents two controllers from constantly stealing each other's pods.

**Reason 2: cleanup ambiguity.** A Pod with multiple owner refs — perhaps `controller=true` on ReplicaSet plus `controller=false` on some custom `Workspace` CRD — has a single source of authoritative truth (the RS) for *what it should look like*. Diagnostic tools (`kubectl describe`) print the controller as "the" parent. The GC controller treats it like any other ref for deletion purposes.

A pod with one controller ref:

```yaml
ownerReferences:
- apiVersion: apps/v1
  kind: ReplicaSet
  name: webapp-7d8c5b9f8c
  uid: 7c2e1b4a-5d6f-4a3b-8e9c-1234567890ab
  controller: true            # ← the authoritative owner
  blockOwnerDeletion: true
- apiVersion: example.com/v1
  kind: Workspace
  name: alice-dev
  uid: dddd-4444
  controller: false           # ← informational; just for GC cascade
  blockOwnerDeletion: false
```

If the Workspace is deleted (Background), the Pod is *not* immediately deleted — the GC controller checks all of its owners; if any are still alive, it stays. Only when *all* owner refs point at gone objects does the GC controller remove the dependent.

---

## 5. `blockOwnerDeletion=true`

This flag is the bridge between owner refs and the foreground deletion protocol. Set it on a ref to declare: "the owner cannot be physically deleted from etcd until this dependent has been deleted."

The apiserver enforces this through an admission plugin: `plugin/pkg/admission/gc/gc_admission.go`. When a client tries to set `blockOwnerDeletion=true` on an ownerRef, the plugin checks that the user has `update` permission on the owner's `finalizers` subresource. The reason: setting `blockOwnerDeletion=true` is functionally equivalent to adding a finalizer to the owner — it prevents the owner from being deleted until you say so. Random users should not be able to do this.

The RBAC for this:

```yaml
# Allow service account to set blockOwnerDeletion=true pointing at Deployments
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: deployment-finalizers-updater
rules:
- apiGroups: ["apps"]
  resources: ["deployments/finalizers"]
  verbs: ["update"]
```

Without this RBAC, an attempt to create an object with `blockOwnerDeletion=true` referencing a Deployment is rejected with:

```
admission webhook error: cannot set blockOwnerDeletion in this case because
cannot find RESTMapping for APIVersion apps/v1 Kind Deployment: user is not
allowed to update finalizers of the owner identified by the ownerReference
```

The interaction with foreground deletion (section 23) is: when an owner is being deleted in foreground mode, the GC controller waits for all dependents whose ownerRef has `blockOwnerDeletion=true` to be removed before allowing the owner's `foregroundDeletion` finalizer to be removed. Refs with `blockOwnerDeletion=false` (or unset) do not delay the owner's deletion.

---

## 6. The Garbage Collector Controller

The actor that makes the entire system work is a single controller running in kube-controller-manager: `pkg/controller/garbagecollector/garbagecollector.go`. It is, structurally, one of the most ambitious controllers in the Kubernetes codebase, because it must watch *every type* in the cluster — built-ins and CRDs alike.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                       Garbage Collector Architecture                    │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   ┌──────────────────────┐                                               │
│   │   RESTMapper         │   periodically polls /apis discovery doc      │
│   │   (Discovery)        │   notices new CRDs, missing resources         │
│   └──────────┬───────────┘                                               │
│              │                                                           │
│              ▼                                                           │
│   ┌──────────────────────┐                                               │
│   │  GraphBuilder        │   one informer per resource type              │
│   │  - monitors map      │   watches ADD/UPDATE/DELETE                   │
│   │  - graphChanges queue│                                               │
│   └──────────┬───────────┘                                               │
│              │                                                           │
│              ▼                                                           │
│   ┌──────────────────────────────────────────────────────────────────┐   │
│   │   In-memory ownership graph                                      │   │
│   │                                                                  │   │
│   │      node {uid} ── owners ─►  [node, node, ...]                  │   │
│   │      node {uid} ── dependents ─► [node, node, ...]               │   │
│   │      node {uid} ── virtual?, beingDeleted?, deletingDependents?  │   │
│   └──────────┬───────────────────────────────────────────────────────┘   │
│              │                                                           │
│       ┌──────┴───────┐                                                   │
│       ▼              ▼                                                   │
│   ┌──────────┐  ┌──────────────┐                                         │
│   │ attempt  │  │ attempt to   │                                         │
│   │ to delete│  │ orphan       │                                         │
│   │ workers  │  │ workers      │                                         │
│   └──────────┘  └──────────────┘                                         │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 6.1 Discovery and per-resource informers

On startup, GC calls Discovery to enumerate every API resource the apiserver claims to serve. For each resource that supports `list` and `watch` and has a `metadata` field (i.e., almost all of them), GC starts a metadata-only informer using `metadata.k8s.io/v1` (which returns only `ObjectMeta`, not specs/status — keeps memory bounded). This is `monitorFor()` in `garbagecollector.go`.

When a new CRD is created, the discovery loop notices, and GC starts another informer. When a CRD is deleted, the corresponding informer is stopped. This dynamic resource fanning is one of the few places in Kubernetes core that does runtime informer management.

The cost is real: at scale, GC opens hundreds of watches against the apiserver. On clusters with thousands of CRDs, this is a non-trivial source of apiserver load (chapter 35 covers scaling implications).

### 6.2 The graph

The in-memory data structure (`pkg/controller/garbagecollector/graph.go`):

```go
type node struct {
    identity         objectReference  // {namespace, name, UID, GVK}
    dependents       map[*node]struct{}
    owners           []metav1.OwnerReference

    virtual          bool   // we received a ref pointing at this object
                            // before we ever observed the object itself
    beingDeleted     bool   // deletionTimestamp is set
    deletingDependents bool // foregroundDeletion finalizer is set
}
```

Every cluster object becomes a node, keyed by UID. Owner refs become edges. When the GC controller wakes up to process a deletion (or finds an orphan), it walks the graph by UID, not name.

### 6.3 The work queues

GC maintains two work queues:

- `attemptToDelete`: nodes that should be considered for deletion (e.g., parent gone)
- `attemptToOrphan`: nodes whose owner is being orphan-deleted, so their owner ref must be stripped

A worker pool drains each queue. For an `attemptToDelete` work item:

```
1. Look up the node by UID in the graph
2. If the node has live owners (UID matches a live object), skip — not orphaned
3. If the node has stale owner refs (UID points to deleted object), remove
   those refs via PATCH
4. If after step 3 there are no owner refs left, issue DELETE with the
   same propagation policy as the parent
5. If DELETE returns 404 (already gone), update the graph
```

For an `attemptToOrphan` item:

```
1. Find dependents of the deleted owner
2. For each dependent, PATCH to remove the ownerRef pointing at the deleted owner
3. Once all dependents are stripped, signal completion
```

### 6.4 Code paths worth reading

- `pkg/controller/garbagecollector/garbagecollector.go` — the controller's `Run`, `processGraphChanges`, `processAttemptToDeleteWorker`.
- `pkg/controller/garbagecollector/graph_builder.go` — informer fanning, event translation to graph mutations.
- `pkg/controller/garbagecollector/graph.go` — the graph data structure.
- `pkg/controller/garbagecollector/operations.go` — the patch/delete operations issued to the apiserver.

---

## 7. Cascade Policies: Background, Foreground, Orphan

When a client deletes an object that has dependents (children with owner refs pointing at it), Kubernetes must decide what happens to the dependents. There are three possibilities, expressed as the `DeletionPropagation` enum (`staging/src/k8s.io/apimachinery/pkg/apis/meta/v1/types.go`):

```go
type DeletionPropagation string

const (
    DeletePropagationOrphan      DeletionPropagation = "Orphan"
    DeletePropagationBackground  DeletionPropagation = "Background"
    DeletePropagationForeground  DeletionPropagation = "Foreground"
)
```

### 7.1 Background (default for new clients)

```
┌──────────────────────────────────────────────────────────────────────────┐
│                     Background propagation                              │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   client: DELETE deployment/webapp  (propagationPolicy=Background)       │
│      │                                                                   │
│      ▼                                                                   │
│   apiserver: DELETE deployment row from etcd  (immediately!)             │
│              (no finalizers to wait for)                                 │
│      │                                                                   │
│      └──► returns 200 to client                                          │
│                                                                          │
│   GC controller: sees DELETE event for deployment                        │
│      │                                                                   │
│      └──► finds dependents (ReplicaSets) in graph                        │
│             └──► enqueue each to attemptToDelete                         │
│                    └──► DELETE each ReplicaSet (also Background)         │
│                           └──► GC sees those deletes                     │
│                                  └──► DELETE each Pod (Background)       │
│                                                                          │
│   Total time: O(depth × GC latency), but client sees instant response.   │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

The parent disappears from etcd *immediately*. There is no waiting. The GC controller does the rest asynchronously. This is what almost everything in production uses — it's the default for `kubectl delete` since Kubernetes 1.20.

Trade-off: there is a window during which the parent is gone but children still exist. A client that lists `Pods --selector=app=webapp` right after deleting `deployment/webapp` will *still see Pods* for a few seconds. They are orphans from the apiserver's perspective until GC cleans them up. This is rarely a problem but is occasionally surprising.

### 7.2 Foreground

```
┌──────────────────────────────────────────────────────────────────────────┐
│                     Foreground propagation                              │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   client: DELETE deployment/webapp  (propagationPolicy=Foreground)       │
│      │                                                                   │
│      ▼                                                                   │
│   apiserver: UPDATE deployment {                                         │
│                deletionTimestamp = now                                   │
│                finalizers += "foregroundDeletion"                        │
│              }                                                           │
│      │                                                                   │
│      └──► returns 200 to client (deployment still visible!)              │
│                                                                          │
│   GC controller: sees UPDATE event with deletionTimestamp +              │
│                  foregroundDeletion finalizer                            │
│      │                                                                   │
│      └──► find dependents with blockOwnerDeletion=true                   │
│             └──► DELETE each (also Foreground)                           │
│                    └──► recursively waits for them                       │
│                                                                          │
│   When last dependent is gone:                                           │
│      GC controller: PATCH deployment to remove foregroundDeletion        │
│         apiserver: finalizers=[], deletionTimestamp != nil               │
│         apiserver: DELETE etcd row                                       │
│                                                                          │
│   Total time: O(deepest blockOwnerDeletion chain × multiple syncs)       │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

The parent stays visible until all `blockOwnerDeletion=true` dependents are gone. This is the "correct" cascade from a user's perspective — `kubectl wait --for=delete deployment/webapp` actually waits for everything — but it is slow, especially at depth.

### 7.3 Orphan

```
┌──────────────────────────────────────────────────────────────────────────┐
│                     Orphan propagation                                  │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   client: DELETE deployment/webapp  (propagationPolicy=Orphan)           │
│      │                                                                   │
│      ▼                                                                   │
│   apiserver: UPDATE deployment {                                         │
│                deletionTimestamp = now                                   │
│                finalizers += "orphan"                                    │
│              }                                                           │
│      │                                                                   │
│      └──► returns 200 to client                                          │
│                                                                          │
│   GC controller: sees UPDATE with orphan finalizer                       │
│      │                                                                   │
│      └──► find dependents                                                │
│             └──► PATCH each to remove ownerRef pointing at deployment    │
│                    (dependents survive as standalone objects!)           │
│                                                                          │
│   When all dependents stripped:                                          │
│      GC controller: PATCH deployment to remove orphan finalizer          │
│         apiserver: DELETE etcd row for deployment                        │
│                                                                          │
│   Result: deployment is gone, RS + Pods are orphans that will be GC'd    │
│   only if they have no other owners. Often they keep running forever.    │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

This is the rare case. Almost no one wants it. The use cases are listed in section 25.

### 7.4 The comparison table

| Policy | Parent gone when? | Dependents handled by? | Dependents gone when? | Use case |
|--------|-------------------|------------------------|------------------------|----------|
| Background | Immediately | GC controller async | Eventually | Default; fast |
| Foreground | After all dependents | GC controller serially | Before parent | Strict cleanup |
| Orphan | After dependents detached | GC strips ownerRef | Never (kept alive) | Controller swap |

---

## 8. The `kubectl --cascade` Flag and the Propagation Header

The cascade policy is passed on the wire as either a query parameter or an option in the DELETE request body:

```
DELETE /api/v1/namespaces/default/deployments/webapp HTTP/1.1
Content-Type: application/json

{
  "kind": "DeleteOptions",
  "apiVersion": "v1",
  "propagationPolicy": "Background"
}
```

kubectl exposes this as `--cascade`:

```bash
# Background (default since 1.20)
kubectl delete deployment webapp
kubectl delete deployment webapp --cascade=background

# Foreground — wait for ReplicaSets and Pods to be gone before kubectl returns
kubectl delete deployment webapp --cascade=foreground

# Orphan — leave the ReplicaSet and Pods running
kubectl delete deployment webapp --cascade=orphan
```

### 8.1 The history of the default

Before 1.20, the kubectl default was effectively `--cascade=true` which was *Background* (not Foreground!). The names "cascade=true|false" mapped onto "Background|Orphan", which was confusing because neither was Foreground. In 1.20, kubectl was updated to take string values: `background`, `foreground`, `orphan`. The legacy boolean form was deprecated but still accepted (true → background, false → orphan).

This historical baggage means: anyone reading old shell scripts that say `kubectl delete --cascade=false` is asking for Orphan. This is almost never what the script author intended. Audit your CI/CD scripts.

### 8.2 The API-level default

When a DELETE request omits `propagationPolicy` entirely, the apiserver chooses a default *per resource type* via the `defaultPropagationPolicy` returned by the resource's storage strategy. Most types default to `Background`. A few don't:

- Custom resources from CRDs: default Background.
- Built-in workload resources (Deployment, StatefulSet, etc.): default Background.
- Namespace: a special case — namespace deletion is its own controller (NamespaceLifecycle) and the cascade is implicit (every object in the namespace is deleted regardless of refs).

### 8.3 Direct curl

```bash
# Foreground via raw API
kubectl proxy --port=8080 &
curl -X DELETE \
  -H 'Content-Type: application/json' \
  -d '{"kind":"DeleteOptions","apiVersion":"v1","propagationPolicy":"Foreground"}' \
  http://localhost:8080/apis/apps/v1/namespaces/default/deployments/webapp
```

The response is the partial object (with deletionTimestamp set) plus a 200 status code.

---

## 9. Finalizers: The Wire Protocol

A finalizer is a *string*. That is the whole protocol. The field is `metadata.finalizers: []string`. The semantics are entirely encoded in a single apiserver rule and an unwritten convention between controllers.

### 9.1 The apiserver rule

> **A row will not be deleted from etcd while `metadata.finalizers` is non-empty.**

That's it. The apiserver does not interpret the strings. It does not validate them (within reason). It does not know which controller "owns" which finalizer. It is, from the apiserver's perspective, a list of opaque tokens that block deletion until externally cleared.

### 9.2 The convention

By convention, each finalizer string is namespaced by the controller that adds it. The format is `<domain>/<name>`:

```
foregroundDeletion                          (apiserver-added, special)
orphan                                      (apiserver-added, special, legacy)
kubernetes.io/pv-protection                 (PV protection controller)
kubernetes.io/pvc-protection                (PVC protection controller)
service.kubernetes.io/load-balancer-cleanup (Service controller)
batch.tutorial.kubebuilder.io/finalizer     (a kubebuilder example)
aws.k8s.aws/finalizer                       (AWS Controllers for K8s)
finalizer.cert-manager.io                   (cert-manager)
velero.io/external-resources-finalizer      (Velero)
```

Each controller is responsible for removing its own finalizer when it has finished its cleanup work. Other controllers must not remove someone else's finalizer (with a few well-documented exceptions, like an admin manually clearing them).

### 9.3 A real object with finalizers

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: data-pv-1
  finalizers:
  - kubernetes.io/pv-protection
  - external-storage.local/csi-cleanup
spec:
  capacity:
    storage: 100Gi
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Delete
  csi:
    driver: ebs.csi.aws.com
    volumeHandle: vol-0abcdef123456
```

If you `kubectl delete pv data-pv-1`:

1. apiserver sets `deletionTimestamp`.
2. PV-protection controller checks: is any PVC bound? If no, removes `kubernetes.io/pv-protection`.
3. CSI driver controller: calls cloud API to delete EBS volume `vol-0abcdef...`, then removes `external-storage.local/csi-cleanup`.
4. apiserver observes `finalizers=[]`, deletes etcd row.

If step 3's API call fails (cloud account billing problem, IAM revoked), the PV stays in `Terminating` until the operator either fixes the underlying issue or manually clears the finalizer. The latter is dangerous: the EBS volume is now orphaned and will bill forever.

### 9.4 The `finalizers` subresource

Updating `metadata.finalizers` directly requires `update` on the resource *and* `update` on the `<resource>/finalizers` subresource (the same one as `blockOwnerDeletion`). This is enforced by `plugin/pkg/admission/gc/gc_admission.go`. Why: adding a finalizer to someone else's object is a denial-of-service vector ("I'll add `evil.com/never-finished` to your Pod and never remove it").

Operator service accounts need explicit RBAC:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: myapp-operator
rules:
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get", "list", "watch", "create", "update", "delete"]
- apiGroups: [""]
  resources: ["configmaps/finalizers"]
  verbs: ["update"]
```

---

## 10. The Canonical Finalizer Reconciler Loop

Every finalizer-aware controller follows the same structure. This is the pattern taught in kubebuilder, operator-sdk, and the controller-runtime docs.

```go
const myFinalizer = "myoperator.example.com/finalizer"

func (r *MyAppReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    var obj examplev1.MyApp
    if err := r.Get(ctx, req.NamespacedName, &obj); err != nil {
        return ctrl.Result{}, client.IgnoreNotFound(err)
    }

    // --- Deletion path ---
    if !obj.DeletionTimestamp.IsZero() {
        // Object is being deleted.
        if controllerutil.ContainsFinalizer(&obj, myFinalizer) {
            // Do our cleanup.
            if err := r.cleanupExternalResources(ctx, &obj); err != nil {
                // Cleanup failed; requeue. But beware: see section 33
                // about the orphan-vs-stuck-object trade-off.
                return ctrl.Result{}, err
            }
            // Cleanup succeeded; remove our finalizer.
            controllerutil.RemoveFinalizer(&obj, myFinalizer)
            if err := r.Update(ctx, &obj); err != nil {
                return ctrl.Result{}, err
            }
        }
        // Either our finalizer is gone or wasn't there. Done.
        return ctrl.Result{}, nil
    }

    // --- Normal path ---
    // Ensure our finalizer is present so we get a chance to clean up later.
    if !controllerutil.ContainsFinalizer(&obj, myFinalizer) {
        controllerutil.AddFinalizer(&obj, myFinalizer)
        if err := r.Update(ctx, &obj); err != nil {
            return ctrl.Result{}, err
        }
        // Requeue so we proceed with normal reconciliation after the update.
        return ctrl.Result{Requeue: true}, nil
    }

    // ... main reconciliation: create child resources, update status, etc.
    return r.reconcileNormal(ctx, &obj)
}
```

### 10.1 The invariants

- Finalizer is added on the *first* successful reconciliation of an object that does not yet have it.
- Finalizer is removed *only* when DeletionTimestamp is set *and* external cleanup has succeeded.
- The DeletionTimestamp branch must return *before* normal reconciliation runs; you do not want to keep creating child resources for an object that is being deleted.
- Removing the finalizer is the *last* step. If you remove it before cleanup completes, you lose the only signal that cleanup hasn't happened.

### 10.2 An example with retries

```go
func (r *MyAppReconciler) cleanupExternalResources(ctx context.Context, obj *examplev1.MyApp) error {
    log := log.FromContext(ctx)

    // Idempotent: if the LB is already gone, returns nil.
    if obj.Status.LoadBalancerARN != "" {
        if err := r.elb.DeleteLoadBalancer(ctx, obj.Status.LoadBalancerARN); err != nil {
            if isNotFoundErr(err) {
                log.Info("LB already deleted, continuing")
            } else {
                return fmt.Errorf("delete LB: %w", err)
            }
        }
    }

    // Idempotent: if the DNS record doesn't exist, returns nil.
    if err := r.dns.DeleteRecord(ctx, obj.Spec.Hostname); err != nil && !isNotFoundErr(err) {
        return fmt.Errorf("delete DNS: %w", err)
    }

    return nil
}
```

Note the idempotence. The reconciler may be called many times during the deletion window: every requeue, every informer update, every controller restart. Each call must either succeed or fail in a way that's safe to retry. *Idempotent cleanup is non-negotiable.*

### 10.3 The race-free pattern

There is a subtle race in the naive form: if you add the finalizer and immediately start creating child resources, then crash before the apiserver persists the finalizer, on restart the child resources exist but the parent doesn't have a finalizer. If the parent is then deleted, your reconciler never gets called with DeletionTimestamp set (because the parent has no finalizers, the apiserver removes it immediately) and the children leak.

The fix: **add the finalizer in its own update, return Requeue, then on the next reconcile do the actual creation work.** That is what the code in section 10 does. The two-step ensures that by the time the reconciler creates child external resources, the finalizer is durably persisted.

This is closely analogous to write-ahead logging in databases (chapter 05 in `databases/`): commit the intention before doing the work, so recovery can find it.

---

## 11. Why Finalizers Live On The Object

A reasonable design question: why are finalizers stored *on* the object they protect, rather than as separate "PendingCleanup" resources?

The answer is **atomicity**. The apiserver, when processing an UPDATE that touches `metadata.finalizers`, evaluates the post-update state inside the same etcd transaction:

```
TXN(rev_n -> rev_n+1):
    READ object at rev_n
    APPLY patch
    IF result.finalizers == [] AND result.deletionTimestamp != nil:
        DELETE etcd row
    ELSE:
        PUT etcd row at rev_n+1
```

If finalizers were stored separately (e.g., in a `Cleanup` resource), this single-transaction guarantee would not exist. A controller would have to:

1. Delete its `Cleanup` resource.
2. Hope the apiserver notices.
3. The apiserver would then need a separate process to check whether the owner can be deleted.

That introduces a race: between (1) and (3), another controller could *re-add* a Cleanup. Or worse: the apiserver could observe an empty cleanups list, decide to delete, but lose the signal that something is in progress.

Putting the finalizer list inside the object's metadata makes "remove finalizer + delete row" a single mutation, observable in one watch event. Watchers that see `finalizers=[X]` and then see the object gone in the next event can be certain that X was the last finalizer.

This atomicity is exposed to clients as a guarantee: the apiserver never emits a `MODIFIED` event for an object with `finalizers=[]` and `deletionTimestamp != nil`. The next event after the last finalizer is removed is always `DELETED`.

---

## 12. Built-In Finalizers

Kubernetes ships a small set of finalizers added by core controllers. Knowing them by name will save you hours of debugging.

### 12.1 `foregroundDeletion`

Added by: the apiserver itself when DELETE is received with `propagationPolicy=Foreground`.

Removed by: the GC controller, after all dependents with `blockOwnerDeletion=true` are gone.

Where in code: `pkg/registry/core/pod/strategy.go` (and friends) — the strategy hooks into the delete handler. The constant is `metav1.FinalizerDeleteDependents = "foregroundDeletion"`.

Diagnostic: any object stuck Terminating with only `foregroundDeletion` in its finalizers list is waiting on the GC controller. Either dependents are stuck Terminating themselves (recursive) or GC is misbehaving.

### 12.2 `orphan`

Added by: the apiserver when DELETE is received with `propagationPolicy=Orphan`.

Removed by: the GC controller, after all dependents have had their ownerRefs stripped.

Where in code: `metav1.FinalizerOrphanDependents = "orphan"`. The handling logic is in the GC controller's `attemptToOrphanWorker`.

This is the only finalizer whose presence indicates "I am being orphan-deleted, please strip my dependents' refs to me." It is essentially a special marker for the GC controller.

### 12.3 `kubernetes.io/pv-protection`

Added by: the PV protection controller (`pkg/controller/volume/pvprotection/`).

When: every PV created.

Removed when: the PV is not bound to any PVC.

Purpose: prevent admin from accidentally deleting a PV while a Pod is using its data. Without it, `kubectl delete pv data-pv-1` would immediately remove the PV from etcd; the CSI driver would, on next sync, see the PV is gone and call the cloud API to delete the underlying volume. The Pod using it gets I/O errors.

With pv-protection, the PV stays in Terminating until the bound PVC is gone (which itself can only go away after the Pod is gone).

```yaml
# Trying to delete a bound PV
$ kubectl delete pv data-pv-1
persistentvolume "data-pv-1" deleted

$ kubectl get pv data-pv-1
NAME        STATUS        ...
data-pv-1   Terminating   ...

$ kubectl get pv data-pv-1 -o jsonpath='{.metadata.finalizers}'
["kubernetes.io/pv-protection"]
```

### 12.4 `kubernetes.io/pvc-protection`

Added by: PVC protection controller (`pkg/controller/volume/pvcprotection/`).

Removed when: no Pod references this PVC in `spec.volumes[*].persistentVolumeClaim`.

Purpose: prevent deletion of a PVC while Pods still depend on it. Same reasoning as PV protection, one layer higher.

The race this prevents is severe: a user `kubectl delete pvc data` on a PVC bound to a running Pod. Without protection, the PVC is gone, the PV's reclaim policy fires, the cloud volume is deleted under the Pod's feet. With protection, the deletion is queued until the Pod is gone.

### 12.5 `service.kubernetes.io/load-balancer-cleanup`

Added by: the service controller in cloud-controller-manager (or kube-controller-manager with `--cloud-provider`), on Service objects of type LoadBalancer.

Removed when: the cloud LB has been deleted via the cloud API.

Purpose: prevent the Service from being removed from etcd before the cloud LB has been physically deleted. Without it, a `kubectl delete svc` immediately removes the Service object, the cloud controller loses the signal, and the LB orphans (billing forever).

This is the single most important finalizer in cloud production: forgetting it would, at the scale of public clouds, result in hundreds of millions of dollars in orphan LBs.

### 12.6 `kubernetes.io/cluster-claim-protection`

Legacy. Used in older versions of cluster-api / cluster-claim resources. Modern equivalents in Cluster API use operator-specific finalizers.

### 12.7 Others

- Namespace deletion has its own internal "finalizers" array on `Namespace.Spec.Finalizers` (note: spec, not metadata!). This is a legacy quirk; see section 21 of chapter 25 (multi-tenancy) for namespace deletion mechanics.
- `kubernetes.io/finalizer.localStorageProtection` — used for local-storage volumes in some distributions.

---

## 13. External Finalizers in Operators

Beyond the built-ins, every operator that manages external resources adds its own finalizer. A small survey:

### 13.1 AWS Controllers for Kubernetes (ACK)

```yaml
apiVersion: ec2.services.k8s.aws/v1alpha1
kind: VPC
metadata:
  name: my-vpc
  finalizers:
  - finalizers.ec2.services.k8s.aws/VPC
spec:
  cidrBlocks: ["10.0.0.0/16"]
```

The controller's cleanup runs the actual `ec2:DeleteVpc` API call before removing this finalizer. If the call fails (dependent subnets still exist, security group references, etc.), the finalizer stays — making this the user's problem to resolve in the cloud account, not Kubernetes' problem.

### 13.2 Velero

```yaml
apiVersion: velero.io/v1
kind: Backup
metadata:
  name: nightly-2026-05-23
  finalizers:
  - velero.io/external-resources-finalizer
spec: ...
```

Velero's finalizer ensures it deletes the backup objects in object storage (S3, GCS, Azure Blob) before letting the Kubernetes Backup CR disappear.

### 13.3 cert-manager

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: example-tls
  finalizers:
  - finalizer.cert-manager.io
```

The finalizer here is more about ensuring the controller can revoke the certificate (via Let's Encrypt ACME) before the CR vanishes — though revocation is best-effort and the finalizer is removed even if revocation fails (cert-manager values cluster hygiene over cert-revocation guarantees here, debatably).

### 13.4 Crossplane

Crossplane is the king of finalizers. Every managed resource (`Bucket`, `Database`, `IAMRole`, etc.) has a finalizer that ensures the cloud resource is deleted before the Kubernetes object is removed. Crossplane also has *composition* finalizers on `Composite` resources that wait for their composed children.

A real Crossplane object:

```yaml
apiVersion: s3.aws.crossplane.io/v1beta1
kind: Bucket
metadata:
  name: my-app-data
  finalizers:
  - finalizer.managedresource.crossplane.io
spec:
  forProvider:
    locationConstraint: us-east-1
```

### 13.5 Strimzi (Kafka)

```yaml
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaTopic
metadata:
  name: orders
  finalizers:
  - strimzi.io/topic-operator
```

Strimzi's topic operator deletes the topic from the actual Kafka brokers before removing this finalizer.

---

## 14. The "Stuck Deletion" Problem and Manual Recovery

The single most common operational incident around GC is "X is stuck Terminating, what do I do?". Almost always, the cause is a finalizer that nobody is removing.

### 14.1 The triage tree

```
┌──────────────────────────────────────────────────────────────────────────┐
│                  Stuck-Terminating Diagnosis Tree                       │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   Object stuck Terminating                                              │
│        │                                                                 │
│        ▼                                                                 │
│   kubectl get <obj> -o yaml | grep -A5 finalizers                       │
│        │                                                                 │
│        ▼                                                                 │
│   ┌────────────────────────────────────────────┐                        │
│   │ Is finalizers empty?                       │                        │
│   └────────────────────────────────────────────┘                        │
│        │ NO                                         │ YES                │
│        ▼                                            ▼                    │
│   For each finalizer name:                    GC controller stuck?       │
│        │                                      Or namespace stuck?        │
│        ▼                                                                 │
│   ┌────────────────────────────────────────────┐                        │
│   │ Is it 'foregroundDeletion'?                │                        │
│   └────────────────────────────────────────────┘                        │
│        │ YES                                        │ NO                 │
│        ▼                                            ▼                    │
│   GC controller waiting on dependents          External finalizer:      │
│   kubectl get <dependents> --show-labels       which controller owns    │
│        │                                       this finalizer name?     │
│        ▼                                            │                    │
│   Recurse on each stuck dependent              ┌───┴────────┐           │
│                                                Installed?    Uninstalled?│
│                                                │             │           │
│                                                ▼             ▼           │
│                                            Read its       Manual override:│
│                                            logs, look     kubectl patch  │
│                                            for errors     finalizers:[]  │
│                                                          (SEE WARNING)   │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 14.2 The manual escape hatch

```bash
# DANGEROUS — only as a last resort
kubectl patch kafkatopic orders --type=merge \
  -p '{"metadata":{"finalizers":[]}}'
```

This is the equivalent of `rm -rf` for the object: it tells the apiserver "skip cleanup, just delete." The danger:

- The Kafka topic in the actual broker is *not* deleted. It will continue to exist, consuming disk, until manually purged.
- Cloud LBs orphaned this way show up months later on the bill.
- PVs cleared this way leave EBS volumes in the cloud.

**The correct order of escalation:**

1. Identify the finalizer's owner controller.
2. Check its logs (`kubectl logs -n kube-system <controller-pod>`). Look for the object's name.
3. Common causes: missing RBAC (controller can't patch its own status), cloud API auth failure, dependent resource still exists.
4. Fix the underlying cause if possible.
5. If the controller is uninstalled forever, run the manual external cleanup *first* (delete the cloud LB by hand, drop the Kafka topic by hand, etc.).
6. *Then* patch the finalizers.

### 14.3 Namespace stuck Terminating

A special case: namespaces. The namespace controller is responsible for deleting all objects in the namespace before letting the namespace itself be deleted. If any object in the namespace has a finalizer that won't clear, the namespace stays in Terminating forever.

To debug:

```bash
# Find what's still in the namespace
kubectl api-resources --verbs=list --namespaced -o name \
  | xargs -n 1 kubectl get --show-kind --ignore-not-found -n stuck-ns

# Look at the namespace status for hints
kubectl get namespace stuck-ns -o yaml
```

The status will look like:

```yaml
status:
  phase: Terminating
  conditions:
  - type: NamespaceDeletionContentFailure
    status: "True"
    reason: ContentDeletionFailed
    message: 'Failed to delete all resource types, 1 remaining:
      unexpected items still remain in namespace: kafkatopics.kafka.strimzi.io'
```

Now you know: a KafkaTopic CR is stuck. Run the triage tree on it.

---

## 15. The Ownership Graph in Practice

Let's walk through a realistic cluster's ownership graph.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                 Ownership graph for a typical workload                  │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   Deployment "webapp"  (no owner)                                        │
│        │                                                                 │
│        │ owns (controller=true, blockOwnerDeletion=true)                 │
│        ▼                                                                 │
│   ReplicaSet "webapp-7d8c5b9f8c"                                         │
│        │                                                                 │
│        │ owns (controller=true, blockOwnerDeletion=true)                 │
│        ▼                                                                 │
│   Pod "webapp-7d8c5b9f8c-x9k2q"                                          │
│        │  (Pods are leaf nodes; no children with refs to Pod)            │
│                                                                          │
│   Service "webapp"  (no owner; standalone)                               │
│                                                                          │
│   PVC "webapp-data"  (no owner from Pod; lifecycle decoupled)            │
│        │                                                                 │
│        │ binds to                                                        │
│        ▼                                                                 │
│   PV "data-pv-1"  (no owner; cluster-scoped lifecycle)                   │
│                                                                          │
│   StatefulSet "kafka"  (no owner)                                        │
│        │                                                                 │
│        │ owns (controller=true)                                          │
│        ▼                                                                 │
│   Pod "kafka-0", "kafka-1", "kafka-2"                                    │
│        │                                                                 │
│        │  StatefulSet does NOT own its PVCs by default!                  │
│        │  (volumeClaimTemplate creates PVCs but no ownerRef)             │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 15.1 Why Pods don't own PVCs

This is intentional. Pods are transient; PVCs hold persistent data. If the Pod owned the PVC, deleting the Pod would cascade-delete the PVC (and, by extension, the PV and underlying data). That is the *opposite* of what users want from persistent storage.

Instead, PVCs are independent. They have no owner refs by default. Lifecycle is managed by users (the StatefulSet creates them but does not own them) and by reclaim policies on the PV.

A consequence: deleting a StatefulSet via `kubectl delete sts kafka` does not delete its PVCs. The StatefulSet, Pods, and Services owned by it go away. The PVCs remain. If you want to also delete the PVCs, you must do it manually, or use `--retain-pvcs=false` (a relatively recent feature, see `spec.persistentVolumeClaimRetentionPolicy`).

### 15.2 Services are unowned

Services usually have no owner refs. They are created by users (or by operators) and live until explicitly deleted. The exception: a few operators (e.g., the Headless Service for a StatefulSet, or per-Pod Services from cluster-api) do set owner refs.

### 15.3 ConfigMaps and Secrets

Default-created ConfigMaps and Secrets are unowned. Operator-created ones almost always have owner refs pointing at the operator's CR — this is the key pattern for "delete CR → all child config gone" (section 18).

### 15.4 A real `kubectl get` showing the graph

```bash
$ kubectl get pod webapp-7d8c5b9f8c-x9k2q -o yaml | yq .metadata.ownerReferences
- apiVersion: apps/v1
  blockOwnerDeletion: true
  controller: true
  kind: ReplicaSet
  name: webapp-7d8c5b9f8c
  uid: 7c2e1b4a-5d6f-4a3b-8e9c-1234567890ab

$ kubectl get rs webapp-7d8c5b9f8c -o yaml | yq .metadata.ownerReferences
- apiVersion: apps/v1
  blockOwnerDeletion: true
  controller: true
  kind: Deployment
  name: webapp
  uid: 1a2b3c4d-5e6f-7890-abcd-ef0123456789
```

---

## 16. OwnerRef Invariants and Adoption

The GC controller enforces a set of invariants on owner refs whenever it walks the graph.

### 16.1 UID must match a live object

When GC processes a node, it consults its in-memory graph. If an owner ref points at a UID that does not exist in the graph (because the owner has been deleted), the ref is **stale**. The GC controller resolves stale refs in two steps:

1. Remove the stale ref from the dependent's metadata (PATCH).
2. If the dependent now has zero owner refs (after stale removal), and was previously owned by something that has been deleted, enqueue it for deletion.

```
Before:
  Pod "p1" ownerRefs = [
    {kind: ReplicaSet, name: rs1, uid: AAAA (live)},
    {kind: ReplicaSet, name: rs2-old, uid: BBBB (gone)}
  ]

GC observes BBBB is not in graph (or has tombstone).
GC PATCHes p1 to remove the BBBB ref:
  Pod "p1" ownerRefs = [
    {kind: ReplicaSet, name: rs1, uid: AAAA}
  ]

p1 still has a live owner, so p1 survives.
```

### 16.2 Adoption

The dual to "remove stale ref" is "adopt orphan". A ReplicaSet's reconciler, on every sync, looks for Pods matching its label selector:

- If a Pod matches *and* has no controller ref → adopt: PATCH to add ownerRef with controller=true pointing at the ReplicaSet.
- If a Pod matches *and* has a controller ref pointing at *this* ReplicaSet → leave alone.
- If a Pod matches *and* has a controller ref pointing at *someone else* → do not adopt; that's their pod.
- If a Pod has a ref pointing at this RS but doesn't match the selector → release: PATCH to remove the ref.

Adoption is the mechanism by which `kubectl delete rs --cascade=orphan` followed by `kubectl scale deployment` can re-attach surviving Pods to a new RS. It's also the basis for some live-migration patterns.

### 16.3 Adoption requires controller consent

There's a subtle security issue: if random users could write arbitrary `ownerReferences` to your Pod, an attacker could "claim" your Pod by writing a ref to their own object. To prevent this, the apiserver requires write access to the *target* resource's `finalizers` subresource when *setting* `blockOwnerDeletion=true` (section 5).

For `controller=true` specifically, there's no apiserver-side enforcement — adoption is implemented as a *consent dance* in the controller code:

- Built-in controllers (RS, StatefulSet, Job, etc.) implement `ControllerRefManager` which only adopts pods whose `metadata.ownerReferences` does not already have a controller ref.
- The acquired ref includes the controller-ref's UID, which the controller verifies before treating the pod as "mine".

If someone manages to inject a controller ref pointing at your Deployment onto a Pod they own, your Deployment's controller will see the Pod as a child *only if labels match*. If labels don't match, it'll try to release it (remove the ref). If labels do match, you'll start treating it as your child — at which point the attacker has tricked you, but the consequence is limited (you'll either ignore it or rotate it on the next reconcile).

### 16.4 The CRD case

For CRDs, there is no built-in adoption logic. Operators that want adoption (rare) must implement it themselves. Most operators *don't* support adoption: if you remove an ownerRef manually, the operator will re-add it on next reconcile (because it created the child and tracks it by name/namespace).

---

## 17. The Owner-Namespace Rule

This rule trips up almost everyone the first time they hit it.

> **A namespaced object cannot have an owner ref pointing at an object in a different namespace.**
>
> **A namespaced object can have an owner ref pointing at a cluster-scoped object.**
>
> **A cluster-scoped object cannot have an owner ref pointing at a namespaced object.**

The apiserver enforces this in `plugin/pkg/admission/gc/gc_admission.go`. The check is during admission of any UPDATE that touches `metadata.ownerReferences`.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                 Owner-namespace permitted combinations                  │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   Dependent in ns A         Owner in ns A     ✓ allowed                  │
│   Dependent in ns A         Owner in ns B     ✗ REJECTED                 │
│   Dependent in ns A         Owner cluster-sc  ✓ allowed                  │
│   Dependent cluster-sc      Owner cluster-sc  ✓ allowed                  │
│   Dependent cluster-sc      Owner in any ns   ✗ REJECTED (silently       │
│                                                  ignored — GC won't      │
│                                                  resolve it)             │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

The reason for the rules:

- Cross-namespace ownership would mean the GC controller has to look up an owner in a different namespace from the dependent. It can do this, but the semantics get weird: deleting namespace A would have to wait for namespace B to allow it. Worse, RBAC for cross-namespace ownership is hard to reason about.
- A cluster-scoped object owned by a namespaced one would mean deleting that namespace deletes a cluster-scoped resource — surprising, hard to audit.

In practice, operators that want to model "this CR owns objects across many namespaces" must use a cluster-scoped owner. The pattern is: define the CRD as cluster-scoped, then namespaced child resources can reference it.

### 17.1 The silent-failure case

The third rule — "cluster-scoped dependent with namespaced owner" — is *silently ignored* by the GC controller. The apiserver does not actively reject it (depending on version), but the GC controller refuses to follow such a ref because it has no way to scope the lookup. The result is that you can construct such a ref and watch nothing happen — your "ownership" is invisible to GC.

This is a frequent bug in operators that try to model "this Pod owns my Cluster CR". The right direction is the opposite: the cluster-scoped Cluster CR owns the namespaced Pod.

---

## 18. CRD `ownerReferences`: The Cleanup-Without-Finalizer Pattern

For operators, the most powerful application of owner refs is to delegate cleanup to the GC controller — avoiding the need for a finalizer entirely.

### 18.1 The pattern

```go
func (r *MyAppReconciler) reconcileConfigMap(ctx context.Context, app *examplev1.MyApp) error {
    cm := &corev1.ConfigMap{
        ObjectMeta: metav1.ObjectMeta{
            Name:      app.Name + "-config",
            Namespace: app.Namespace,
        },
        Data: map[string]string{
            "config.yaml": render(app.Spec),
        },
    }
    // Set the CR as the owner of the ConfigMap.
    if err := controllerutil.SetControllerReference(app, cm, r.Scheme); err != nil {
        return err
    }
    return r.Patch(ctx, cm, client.Apply, client.FieldOwner("myapp-operator"))
}
```

`SetControllerReference` is the standard helper from controller-runtime:

```go
// Sets ownerReference of object to refer to owner, with controller=true
// and blockOwnerDeletion=true (default).
func SetControllerReference(owner, controlled metav1.Object, scheme *runtime.Scheme) error {
    // ... checks namespaces match, checks no existing controller ref, etc.
    ref := metav1.OwnerReference{
        APIVersion:         gvk.GroupVersion().String(),
        Kind:               gvk.Kind,
        Name:               owner.GetName(),
        UID:                owner.GetUID(),
        Controller:         pointer.Bool(true),
        BlockOwnerDeletion: pointer.Bool(true),
    }
    upsertOwnerRef(ref, controlled.GetOwnerReferences())
    return nil
}
```

Now: when the MyApp CR is deleted, the GC controller sees the dependent ConfigMap, observes its owner is gone, and deletes the ConfigMap. The operator does not need a finalizer for the ConfigMap. The cleanup is *free*.

### 18.2 When this is sufficient

- You only create Kubernetes-native child resources (ConfigMaps, Secrets, Deployments, Services, Pods, PVCs).
- You don't care about ordered cleanup.
- You don't have external resources (cloud APIs, third-party services) to clean up.

In this case, **no finalizer is needed**. The operator can be a pure level-triggered reconciler with no deletion path at all (other than ignoring objects with DeletionTimestamp set).

### 18.3 When you still need a finalizer

- External resource cleanup (cloud LB, S3 bucket, Kafka topic, Vault secret, DNS record).
- Pre-deletion validation (refuse to delete if running production traffic).
- State drain (move workload off, then allow deletion).
- Snapshot before delete.
- Ordered teardown of multi-resource graphs that can't be expressed by `blockOwnerDeletion`.

### 18.4 Mixing both

It's common to use both: owner refs for child Kubernetes objects (cheap, automatic) and a finalizer for external cleanup (specific, controlled). The reconciler structure is unchanged:

```go
if !obj.DeletionTimestamp.IsZero() {
    if containsFinalizer(obj, myFinalizer) {
        // External cleanup (S3, cloud LB, etc.)
        if err := r.cleanupExternal(ctx, obj); err != nil {
            return ctrl.Result{}, err
        }
        // Note: we do NOT need to delete ConfigMaps/Secrets/Deployments
        // here. The GC controller will do that for us, after we remove
        // our finalizer.
        removeFinalizer(obj, myFinalizer)
        return ctrl.Result{}, r.Update(ctx, obj)
    }
    return ctrl.Result{}, nil
}
```

---

## 19. TTLAfterFinished Controller

A small but useful built-in controller: `pkg/controller/ttlafterfinished/`. It auto-deletes Jobs (and, in some plans, other resources) after they reach a terminal state.

### 19.1 Setting the TTL

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: nightly-report
spec:
  ttlSecondsAfterFinished: 3600   # delete 1 hour after completion
  template:
    spec:
      containers:
      - name: report
        image: report:1.0
      restartPolicy: Never
```

### 19.2 How it works

The TTLAfterFinished controller watches Jobs. On every sync, it checks: is `status.completionTime` (or the equivalent for Failed) set? If yes, is `now - completionTime > ttlSecondsAfterFinished`? If yes, issue DELETE.

This is a separate controller from the Job controller itself. It runs only if the controller manager is started with `--controllers=*,ttl-after-finished` (it's on by default).

### 19.3 Why this exists

Without TTL, completed Jobs pile up. They sit in etcd forever. They consume memory in informers. They clutter `kubectl get jobs` output. At scale (CI pipelines that run thousands of Jobs per day) this becomes a real problem.

Before TTL existed (pre-1.12), the canonical solution was a CronJob whose job was to delete old Jobs. That worked but was operationally annoying. TTL pushes it into the apiserver/controller plane.

### 19.4 Caveat

TTL deletes the Job in Background mode. The Pods owned by the Job are also deleted (cascade). Their logs become inaccessible. If you want to keep logs, ship them to a log aggregator before the TTL fires.

---

## 20. The Pod GC Controller

A different controller — `pkg/controller/podgc/` — exists to clean up *Pods* that have reached terminal states (Succeeded/Failed) and are not owned by anyone who'd clean them up.

### 20.1 What it does

- Periodically (every 20 seconds) lists all Pods in the cluster.
- For Pods in phase Succeeded or Failed:
  - If the pod has been in this phase longer than some threshold, *and* the total number of terminated pods exceeds `--terminated-pod-gc-threshold` (default 12500), delete the oldest ones.

The threshold-based behaviour means PodGC only kicks in at scale: small clusters with <12500 terminated pods see no PodGC activity. Large clusters (or clusters with high Job throughput) rely on it to prevent etcd from filling with old terminal pods.

### 20.2 The orphaned pod case

PodGC also handles pods whose Node is gone. When a Node is deleted, the kubelet for that node is gone, so it cannot transition its pods to Failed. PodGC notices a pod whose `spec.nodeName` does not refer to any live Node and force-deletes it. Without this, pods on deleted nodes would stay in their last-observed phase forever.

This is the mechanism behind the "ghost pod" cleanup in chapter 22 (node deletion cascade).

### 20.3 Interaction with TTLAfterFinished

PodGC and TTLAfterFinished can both delete a completed Job's Pods, depending on which runs first. The end result is the same. No coordination needed; each operates on its own criteria.

---

## 21. Event TTL and Lease GC

Two more types of objects have built-in TTL.

### 21.1 Events

Every cluster generates Events at high volume — every Pod scheduling, every Pull, every Restart, every NodeReady transition. Without TTL, Events would crush etcd.

The mechanism is an etcd lease attached to each Event. The lease has a TTL of (by default) 1 hour (`--event-ttl` flag on kube-apiserver). When the lease expires, etcd auto-deletes the row. The apiserver does not poll for old events; etcd does the work.

```
$ kubectl get events --sort-by='.lastTimestamp'
LAST SEEN   TYPE    REASON      OBJECT          MESSAGE
3m          Normal  Pulled      pod/webapp-x9k2q  Successfully pulled image
1m          Normal  Started     pod/webapp-x9k2q  Started container webapp
30s         Normal  Killing     pod/webapp-x9k2q  Stopping container webapp
```

Events older than 1 hour are gone. `kubectl describe pod` only shows recent events.

The flag `--event-ttl` can be raised on the apiserver for forensics, but the cost is etcd size.

### 21.2 Leases

`coordination.k8s.io/Lease` objects are used for leader election among controller replicas. Each replica writes its identity into a Lease and renews it every few seconds. If a replica dies, the renewal stops and other replicas can take over.

Leases are NOT TTL'd by etcd. The Lease object itself is a normal etcd row. However, the *application-level* TTL is encoded in `spec.holderIdentity` and `spec.renewTime`: the holder is considered to have lost the lease if `now - renewTime > leaseDurationSeconds`. Other controllers can then claim the lease via a compare-and-swap on `resourceVersion`.

Leases stay in etcd forever unless explicitly deleted. They are small (<1KB) so this is fine in practice.

---

## 22. The "Node Deleted" Cascade

Deleting a Node is one of the more complex cascade events because Nodes are the anchor for many other types.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    Cascade triggered by node deletion                   │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   kubectl delete node worker-7                                           │
│       │                                                                  │
│       ▼                                                                  │
│   apiserver: removes Node row from etcd (no finalizers by default)       │
│       │                                                                  │
│       ├─► PodGC notices: pods with spec.nodeName=worker-7 have no node   │
│       │     └─► force-delete those Pods                                  │
│       │                                                                  │
│       ├─► Endpoints controller: refresh Endpoints for Services           │
│       │     └─► remove pod IPs from worker-7 from Service backends       │
│       │                                                                  │
│       ├─► EndpointSlice controller: update EndpointSlices accordingly    │
│       │                                                                  │
│       ├─► CSI VolumeAttachment controller:                               │
│       │     for each VA bound to worker-7, mark detach pending           │
│       │     (in cloud: detach volume)                                    │
│       │                                                                  │
│       ├─► Node lease ('Lease' in kube-node-lease ns): explicit GC        │
│       │                                                                  │
│       └─► CSR/Bootstrap tokens for this node: revoked                    │
│                                                                          │
│   Note: the kubelet on worker-7 may still be running. It will see        │
│   "node not found" on its next watch, refuse to function, but cannot     │
│   forcibly cancel the local pods (they keep running until reschedule    │
│   on new node, at which point the local pods are stopped).              │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 22.1 The split-brain risk

If a node is partitioned (network down, not actually crashed) and the admin deletes it from the cluster believing it's gone, but the node's kubelet is still running, you have a split-brain. The kubelet keeps running pods locally. Those pods may have stable IPs (depending on CNI) that are also assigned to new pods running on other nodes (because the cluster believes the old IPs are free). This is a classic source of "two production instances writing to the same database" incidents.

Mitigation:

- Use `kubectl drain` before delete — cordons the node, waits for graceful eviction.
- Use cluster-api or cloud-controller-manager which validate node liveness before deletion.
- For CSI workloads, use `VolumeAttachment` finalizers (some drivers add them) so that volumes aren't double-attached.

### 22.2 Node finalizers

Cluster API and some Kubernetes distributions (OpenShift, GKE Autopilot) add finalizers to Node objects to ensure proper cleanup:

```yaml
apiVersion: v1
kind: Node
metadata:
  name: worker-7
  finalizers:
  - machine.cluster.x-k8s.io/node-drain
```

When such a Node is deleted, the controller drains it (evict all Pods) before allowing the actual Node deletion. This is the "proper" cluster-api node deletion flow.

---

## 23. Foreground Deletion: The Full Dance

Foreground deletion is the most intricate of the three policies. Walk through the full sequence for a Deployment with one ReplicaSet and three Pods.

```
Initial state:
  Deployment "webapp"  (no finalizers)
    ▼ blockOwnerDeletion=true
  RS "webapp-7d8c5b9f8c"  (no finalizers)
    ▼ blockOwnerDeletion=true
  Pods x9k2q, p3lqz, m8r1f  (no finalizers)
```

**Step 1: client issues DELETE on Deployment with propagationPolicy=Foreground**

```
T=0  apiserver: receives DELETE
T=0  apiserver: existing.deletionTimestamp is nil; existing.finalizers is []
T=0  apiserver: adds 'foregroundDeletion' to Deployment.metadata.finalizers
T=0  apiserver: sets Deployment.metadata.deletionTimestamp = now
T=0  apiserver: PUT row (NOT delete)
T=0  apiserver: returns 200 to client with updated object
```

State now:
```
  Deployment "webapp"  finalizers=[foregroundDeletion]  deletionTimestamp=set
    ▼
  RS  (unchanged)
    ▼
  Pods x9k2q, p3lqz, m8r1f
```

**Step 2: GC controller observes the Deployment is being deleted (foreground)**

```
T=1  GC: sees UPDATE event with foregroundDeletion + deletionTimestamp
T=1  GC: marks deployment node 'deletingDependents=true'
T=1  GC: enumerate dependents: 1 RS (blockOwnerDeletion=true)
T=1  GC: enqueue attemptToDelete for the RS with propagation=Foreground
```

**Step 3: GC sends DELETE on RS with Foreground**

```
T=2  GC: DELETE /apis/apps/v1/.../replicasets/webapp-7d8c5b9f8c?propagation=Foreground
T=2  apiserver: adds 'foregroundDeletion' finalizer to RS, sets DT
T=2  RS now in same state as Deployment
```

**Step 4: GC recursively descends to Pods**

```
T=3  GC: sees RS update; enumerate dependents: 3 Pods
T=3  GC: enqueue DELETE for each Pod, also Foreground (technically not
         required to recurse Foreground all the way — see note below)
T=4  apiserver: receives DELETE for each Pod; standard graceful-delete kicks
                in; adds deletionTimestamp; Pod has grace period 30s
T=4  kubelet: receives MODIFIED; SIGTERM containers
T=34 kubelet: containers gone; PATCH Pod to remove kubelet's role
T=34 apiserver: pod has no finalizers; DELETE row
T=34 GC: sees Pod gone
```

(Note: Pods don't have `foregroundDeletion` finalizer added because they have no dependents to wait on; the GC controller is smart enough to skip the foreground propagation for leaf nodes. Actually, the controller may still set it for correctness; see source.)

**Step 5: GC observes all RS dependents (Pods) are gone**

```
T=35 GC: graph shows RS has 0 dependents
T=35 GC: PATCH RS to remove 'foregroundDeletion' finalizer
T=35 apiserver: RS.finalizers=[], RS.deletionTimestamp != nil → DELETE row
T=35 GC: sees RS gone
```

**Step 6: GC observes Deployment has no remaining dependents**

```
T=36 GC: graph shows Deployment has 0 dependents
T=36 GC: PATCH Deployment to remove 'foregroundDeletion' finalizer
T=36 apiserver: DELETE row for Deployment
T=36 apiserver: emit audit event for delete
T=36 client (if waiting): sees object gone
```

Total elapsed time: ~36 seconds, dominated by the Pod graceful termination (30s default).

### 23.1 The visible state during the dance

```
$ kubectl get deployment webapp -o yaml
metadata:
  deletionTimestamp: "2026-05-23T10:00:00Z"
  finalizers:
  - foregroundDeletion
status:
  conditions:
  - type: Progressing
    status: "True"
    reason: NewReplicaSetCreated
  ...
```

If you `kubectl describe deployment webapp` mid-dance, you'll see the deployment "alive" with deletion timestamp. The status updates are typically frozen — the Deployment controller stops reconciling normally once DeletionTimestamp is set.

### 23.2 The "stuck foreground" failure mode

If even one Pod fails to clean up (e.g., a finalizer on the Pod, a stuck CSI detach), the entire foreground chain stalls. The Deployment stays Terminating forever (or until manual intervention).

This is why Background is the default. Foreground is correct but fragile.

---

## 24. Foreground vs Background: Performance

A comparison of empirical behaviour at scale.

### 24.1 Time complexity

| Policy | Wall-clock until client sees parent gone | Wall-clock until all dependents gone |
|--------|------------------------------------------|--------------------------------------|
| Background | O(1) — single apiserver round trip | O(depth × GC sync interval × resource count) |
| Foreground | O(depth × pod-graceful-shutdown × N dependents) | Same as parent's time |
| Orphan | O(N dependents × patch latency) | Never |

For a typical Deployment with 100 Pods:
- Background: client returns in ~10 ms. All Pods gone in ~30s (graceful shutdown).
- Foreground: client returns in ~35s (everything in sequence).
- Orphan: client returns in ~1s (100 patches in parallel). Pods continue running.

### 24.2 Apiserver load

Foreground generates more apiserver writes:

- Adding the foregroundDeletion finalizer (1 write).
- Recursively adding it to dependents (N writes).
- Removing it from each in reverse order (N+1 writes).

Total: ~3N writes vs Background's ~N writes. At cluster scale where you're frequently deleting large Deployments (CI runners, batch jobs), this difference matters.

### 24.3 What production uses

- `kubectl delete` defaults to Background since 1.20. Almost all human deletions are Background.
- Operators that create child resources use owner refs and rely on the parent's deletion policy to cascade. Most operators do not specify a custom propagation policy on their DELETE requests, so children inherit Background (the GC controller default).
- A few specific use cases use Foreground: testing tools that want to assert "everything is gone" before continuing (`kubectl wait --for=delete` works better with Foreground).

---

## 25. Orphan: When To Use It

Orphan is rarely used. The legitimate cases:

### 25.1 Controller swap / migration

```
Old controller v1 owns ReplicaSet, owns Pods
Admin wants to upgrade to v2 with breaking semantic changes
Strategy:
  1. kubectl delete deployment my-app --cascade=orphan
     → Deployment gone, RS and Pods continue running (orphaned)
  2. kubectl apply -f new-deployment.yaml (with same labels)
  3. New Deployment's reconciler discovers orphaned RS, decides whether
     to adopt or replace
```

This is the "zero-downtime controller swap" pattern. The Pods stay alive during the transition.

### 25.2 Re-parenting

You want to move a set of Pods from one StatefulSet to another StatefulSet of a different type. Orphan delete the source StatefulSet, then have the destination adopt (via matching labels).

### 25.3 Debugging

Sometimes you want to delete a Deployment but keep its Pods running for forensics (`kubectl exec` into them, dump core, capture logs). `--cascade=orphan` gives you that.

### 25.4 Why it's risky

The orphaned dependents have no controller. Nothing watches them. If a Pod crashes, nothing restarts it. If the node fails, nothing reschedules it. Orphans are stateless lame ducks: they keep doing whatever they were doing until something kills them. You must manually adopt them into a new owner or manually delete them.

### 25.5 Operator awareness

Almost no operator handles "my CR's child has had its owner ref stripped" gracefully. If you orphan-delete an operator's CR, you'll have a pile of leftover Deployments / Services / ConfigMaps the operator no longer tracks. Cleanup is on you.

---

## 26. Ownership Cycles

The apiserver does *not* detect ownership cycles. It is on you to avoid them.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          Ownership cycle                                │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   ┌─────────────┐                                                        │
│   │   A         │◄────────────────┐                                      │
│   │             │                 │                                      │
│   │ ownerRef:B  │                 │                                      │
│   └─────┬───────┘                 │                                      │
│         │                         │                                      │
│         │ owns                    │ owns                                 │
│         ▼                         │                                      │
│   ┌─────────────┐                 │                                      │
│   │   B         │                 │                                      │
│   │             │                 │                                      │
│   │ ownerRef:A  │─────────────────┘                                      │
│   └─────────────┘                                                        │
│                                                                          │
│   Delete A:                                                              │
│     - apiserver waits for B to be gone (foreground) or                   │
│       just deletes A (background)                                        │
│     - GC controller wants to delete B because owner A is gone            │
│     - But B's deletion also tries to cascade to A                        │
│     - A is gone but the graph shows A still as B's owner                 │
│     - Eventually both are stale; GC removes both                         │
│                                                                          │
│   But if both have finalizers waiting on the other → deadlock.           │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 26.1 Why this happens

Almost never intentional. Common ways to introduce a cycle:

- Operator A creates a CR of type B which has an ownerRef to A. Later, A is modified to be owned by B (e.g., A's status field is mirrored as an annotation on B and someone mistakenly sets it as an ownerRef).
- Two operators both think they own the same shared resource and both add controller refs pointing at each other (this also violates the at-most-one-controller rule, so it would be rejected).
- Test fixtures that copy objects around without rewriting refs.

### 26.2 The pathological behaviour

GC controller will eventually GC both, but only after both have lost their finalizers. If both have finalizers pointing at each other in deletion logic, deadlock. Manual intervention required.

### 26.3 Avoiding cycles

- Never let a child object's controller decide to add an ownerRef pointing *back* at the parent. Owner refs flow one way.
- If you need bi-directional pointers, use annotations on one side and ownerRefs on the other.

---

## 27. Cascading Delete Bugs in Operators

Real, observed bugs.

### 27.1 Missing ownerRef → orphaned resources forever

```go
// BUG: forgot SetControllerReference
cm := &corev1.ConfigMap{...}
r.Create(ctx, cm)
// When CR is deleted, ConfigMap stays.
```

The CR is deleted, the ConfigMap stays. Over time, the namespace accumulates dozens of leftover ConfigMaps from deleted CRs. Recovery: write a one-shot cleanup script that lists ConfigMaps without owners.

### 27.2 Wrong UID → adoption fails silently

```go
// BUG: hand-rolled ownerRef without UID
cm.OwnerReferences = []metav1.OwnerReference{{
    APIVersion: "example.com/v1",
    Kind:       "MyApp",
    Name:       app.Name,
    // UID missing!
}}
```

The apiserver accepts this (UID is technically validated but a blank UID isn't an "invalid" UID per se on PATCH, depending on version). The GC controller sees `UID=""` and cannot match any live object. The ref is silently ignored. The ConfigMap is orphan.

Always use `SetControllerReference` or `SetOwnerReference` from controller-runtime, which fills in the UID for you.

### 27.3 Cross-namespace ownerRef

```go
// BUG: child is in 'workloads' namespace, parent in 'system'
cm.Namespace = "workloads"
cm.OwnerReferences = []metav1.OwnerReference{{
    Kind: "MyApp",
    Name: "system-app",
    Namespace_implicit_: "system",  // not encoded! refs have no namespace
    UID:  "...",
}}
```

The apiserver enforces the same-namespace rule (section 17). The CREATE is rejected with `cross-namespace owner references are disallowed`. Common when copy-pasting objects across namespaces.

### 27.4 Two controllers fighting for ownership

```
Controller A creates Pod P with controller=A
Controller B's label selector matches P; B wants to adopt
B sees P already has a controller; doesn't adopt — correct
But: a buggy version of B PATCHes P's ownerRefs to include B as controller
The apiserver rejects (only one controller=true allowed)
B's reconciler errors out, requeues, retries forever
```

This is a "fighting controllers" issue. The fix is in the controllers' label selectors (no overlap) or in their adoption logic (respect existing controller refs).

### 27.5 Stale ownerRef after manual rename

If you `kubectl get` an object, edit its metadata to rename it (in some out-of-band way), other objects' refs become stale. Adoption logic must use UID, not name, but if anything compares by name (legacy code), bugs surface.

---

## 28. The `deletecollection` Verb

Kubernetes supports a bulk-delete verb on collections:

```
DELETE /api/v1/namespaces/default/pods
```

This deletes *all* pods in `default`. Combined with label selectors:

```bash
kubectl delete pods -l app=webapp,environment=staging
```

issues a single API call that maps to the `deletecollection` verb:

```
DELETE /api/v1/namespaces/default/pods?labelSelector=app=webapp,environment=staging
```

### 28.1 The semantics

- The apiserver lists all matching objects and calls DELETE on each, atomically iterating with internal pagination.
- Each individual DELETE follows the same protocol as a single DELETE — finalizers, cascade, etc.
- The propagation policy applies to each one.

### 28.2 The danger

```bash
# Intended: delete pods with that label
kubectl delete pods -l app=webapp

# Typo: deletes EVERYTHING
kubectl delete pods --all
kubectl delete pods           # in some kubectl versions, defaults to --all? check
```

A misplaced flag or a malformed selector can delete thousands of objects in milliseconds. There's no "are you sure?" prompt. RBAC is the only safety; `deletecollection` is a separate verb in RBAC rules:

```yaml
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  # Missing: "deletecollection" — this user cannot bulk-delete
```

### 28.3 Best practice

For production write-access RBAC, explicitly grant `deletecollection` only where needed (it's not granted by `delete`). For developer/admin roles, accept the risk but encourage `kubectl delete --dry-run=server` first.

### 28.4 Performance

`deletecollection` is much faster than a loop of single DELETEs at the network layer (one round trip) but each underlying DELETE is still processed individually by the apiserver. The savings are mostly client-side.

---

## 29. Server-Side Apply and Finalizers

Server-Side Apply (SSA, introduced in 1.16, stable in 1.22) changes how field ownership works. It does *not*, however, manage `metadata.finalizers` in any special way.

### 29.1 The interaction

```go
// SSA does NOT set finalizers via Apply
obj := &examplev1.MyApp{
    ObjectMeta: metav1.ObjectMeta{
        Name: "x",
    },
    Spec: ...,
}
client.Apply(ctx, obj, client.FieldOwner("my-controller"))
// Even if obj.Finalizers is set on the local struct, SSA does not
// apply finalizers as a managed field by default.
```

In practice, SSA implementations skip `metadata.finalizers` because:

1. Finalizers are typically owned by *the controller adding it*, not by the user/manifest.
2. Treating finalizers as managed fields would mean removing them whenever Apply doesn't include them, which would break the finalizer protocol.

Instead, finalizers must be added/removed via explicit PATCH or UPDATE:

```go
// Correct way to add a finalizer with controller-runtime
if controllerutil.AddFinalizer(&obj, myFinalizer) {
    if err := r.Update(ctx, &obj); err != nil {
        return err
    }
}

// Or via JSON Merge Patch
patch := []byte(`{"metadata":{"finalizers":["my.example.com/finalizer"]}}`)
client.Patch(ctx, obj, client.RawPatch(types.MergePatchType, patch))
```

### 29.2 The managed-fields trap

If an old version of an operator owned `metadata.finalizers` as a managed field via SSA, the field-manager metadata persists in `metadata.managedFields`. A subsequent SSA from a different manager will "fight" with that ownership. The fix: explicitly clear the managed-fields claim:

```bash
kubectl patch myapp x --type=json \
  -p='[{"op":"remove","path":"/metadata/managedFields/0"}]'
```

…but you generally don't need this if you never owned finalizers via SSA in the first place. Use Update/Patch for finalizers; reserve SSA for spec.

---

## 30. Audit and Forensics

Every DELETE generates an audit event at the apiserver. The audit policy controls what's logged.

```yaml
# audit-policy.yaml
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
- level: RequestResponse
  verbs: ["delete", "deletecollection"]
  resources:
  - group: ""
    resources: ["pods", "services", "configmaps", "secrets", "persistentvolumes"]
  - group: "apps"
    resources: ["deployments", "statefulsets"]
- level: Metadata
  verbs: ["update", "patch"]
  resources:
  - group: ""
    resources: ["pods/finalizers"]
  - group: "apps"
    resources: ["deployments/finalizers"]
```

A real audit event for a DELETE:

```json
{
  "kind": "Event",
  "apiVersion": "audit.k8s.io/v1",
  "level": "RequestResponse",
  "auditID": "e3a8b612-...-...",
  "stage": "ResponseComplete",
  "requestURI": "/apis/apps/v1/namespaces/default/deployments/webapp?propagationPolicy=Background",
  "verb": "delete",
  "user": {
    "username": "alice@example.com",
    "groups": ["system:authenticated"]
  },
  "sourceIPs": ["10.0.1.42"],
  "objectRef": {
    "resource": "deployments",
    "namespace": "default",
    "name": "webapp",
    "apiGroup": "apps",
    "apiVersion": "v1"
  },
  "responseStatus": { "code": 200 },
  "requestReceivedTimestamp": "2026-05-23T10:00:00.001Z",
  "stageTimestamp": "2026-05-23T10:00:00.012Z",
  "annotations": {
    "authorization.k8s.io/decision": "allow"
  }
}
```

### 30.1 Detecting unauthorized finalizer removal

A particularly important audit pattern: catching manual finalizer clearing. The pattern is a PATCH on `<resource>/finalizers` that sets the array to empty.

Query in your SIEM / log aggregator:

```
verb = "patch"
AND requestURI ~ "/finalizers"
AND user.username NOT IN ("system:serviceaccount:kube-system:*")
```

This finds humans (or non-system service accounts) clearing finalizers, which is almost always a manual override that bypasses cleanup. Audit alerts should fire here.

### 30.2 Tracking cascade

When a single user DELETE cascades to many child deletions, the audit log shows:

1. The user's DELETE (with user.username).
2. N service-account DELETEs (with user.username = "system:serviceaccount:kube-system:generic-garbage-collector").

To reconstruct a cascade, correlate timestamps. Look at the GC's deletions within the time window after the user's delete.

---

## 31. Common Operator Finalizer Patterns

A taxonomy of what real operators do with finalizers.

### 31.1 External resource cleanup

The most common pattern. The operator manages a resource that lives outside Kubernetes (cloud LB, S3 bucket, Vault secret, Kafka topic). On CR deletion, the finalizer ensures the external resource is deleted *before* the CR is removed from etcd, preserving the only record of what to clean up.

```go
func (r *Reconciler) cleanup(ctx context.Context, app *v1.MyApp) error {
    if app.Status.S3BucketName != "" {
        if err := r.s3.DeleteBucket(ctx, app.Status.S3BucketName); err != nil {
            if !isNotFound(err) {
                return err
            }
        }
    }
    return nil
}
```

### 31.2 State drain before deletion

Database operators (Postgres, Cassandra, etcd) use this. Before deleting a Pod that hosts the primary replica, the operator triggers a failover to a secondary, then removes its finalizer once the failover is confirmed.

```go
if !pod.DeletionTimestamp.IsZero() && pod.Annotations["role"] == "primary" {
    if !r.isFailoverComplete(ctx, pod) {
        if err := r.triggerFailover(ctx, pod); err != nil {
            return err
        }
        // requeue, wait for failover
        return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
    }
    // failover done; safe to remove finalizer
    removeFinalizer(pod, primaryDrainFinalizer)
    return ctrl.Result{}, r.Update(ctx, pod)
}
```

### 31.3 Snapshot before delete

Backup operators (Velero, Stash) use finalizers to ensure a final backup is taken before resources disappear.

```go
if !backup.DeletionTimestamp.IsZero() {
    if backup.Status.Phase != "Completed" {
        // Trigger backup, wait
        return ctrl.Result{RequeueAfter: 30 * time.Second}, r.runBackup(ctx, backup)
    }
    // Backup done; remove finalizer
    removeFinalizer(backup, snapshotFinalizer)
    return ctrl.Result{}, r.Update(ctx, backup)
}
```

### 31.4 Multi-step state-machine deletion

When cleanup involves multiple steps that must happen in order, the state is encoded in conditions:

```yaml
status:
  conditions:
  - type: DrainStarted
    status: "True"
  - type: TrafficDrained
    status: "True"
  - type: DataMigrated
    status: "False"
  - type: ExternalResourceDeleted
    status: "False"
```

The reconciler picks up where it left off: on each call, find the first `False` condition that's actionable and progress it. Only when all are `True` does it remove the finalizer.

This is the most flexible pattern but also the most code. It's typical of capability-level-4/5 operators (chapter 23).

---

## 32. Diagnosing Stuck-Terminating Objects

A field guide.

### 32.1 The five-minute triage

```bash
# 1. Confirm it's stuck — has it been Terminating long?
kubectl get <obj> -o yaml | grep deletionTimestamp

# 2. Look at finalizers
kubectl get <obj> -o yaml | grep -A 10 finalizers

# 3. Identify each finalizer's owner controller
#    Conventional naming: <domain>/<short-name>
#    Look for matching pods in kube-system or operators namespace
kubectl get pods --all-namespaces | grep <domain>

# 4. Check that controller's logs for errors mentioning the object
kubectl logs -n <ns> <controller-pod> | grep <obj-name>

# 5. Check RBAC: does the controller's SA have permission?
kubectl auth can-i patch <resource>/finalizers \
  --as system:serviceaccount:<ns>:<sa>
```

### 32.2 The deep-dive

For really hairy cases:

```bash
# Look at the cluster's GC controller logs
kubectl logs -n kube-system kube-controller-manager-master \
  | grep -i "garbage\|gc " | tail -50

# Look at deletion events for the object
kubectl get events --all-namespaces \
  --field-selector involvedObject.name=<obj-name> \
  --sort-by='.lastTimestamp'

# Check etcd directly (admin only)
ETCDCTL_API=3 etcdctl get /registry/<resource>/<namespace>/<name>
```

The etcd lookup is the ground truth: if etcd has the row and the apiserver returns it, the row exists. If etcd has the row but the apiserver returns 404, you have an apiserver caching bug (rare). If etcd doesn't have the row but kubectl can still see it, that's stale cache (call kubectl with a fresh kubeconfig).

### 32.3 Common stuck-Terminating signatures

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `foregroundDeletion` is the only finalizer | Dependent stuck | Recurse on dependents |
| `kubernetes.io/pv-protection` | PVC still bound | Delete PVC first |
| `kubernetes.io/pvc-protection` | Pod still mounted | Delete or evict pod first |
| `service.kubernetes.io/load-balancer-cleanup` | Cloud LB delete failed | Check cloud-controller-manager logs |
| Custom operator finalizer + operator running | Cleanup error | Check operator logs |
| Custom operator finalizer + operator gone | Operator uninstalled | Manual override (see warning) |

### 32.4 The namespace-stuck case

```bash
# Find which resource type is blocking namespace deletion
kubectl get namespace stuck-ns -o yaml | grep -A 5 conditions

# Look for the offending CR
kubectl api-resources --verbs=list --namespaced -o name \
  | xargs -n 1 kubectl get -n stuck-ns --ignore-not-found

# For each remaining resource, run the standard finalizer triage
```

If a CRD is uninstalled but its CRs remain, those CRs become inaccessible via kubectl (the apiserver returns "the server could not find the requested resource"). You can still reach them through the API server's raw discovery, but cleanup requires re-installing the CRD temporarily.

---

## 33. Best Practices for Operator Authors

Distilled wisdom.

1. **Always use UID, not name**, for ownership semantics. Helper: `SetControllerReference` from controller-runtime.
2. **Always implement DeletionTimestamp handling.** Even if you think you don't need a finalizer, write the no-op branch:
   ```go
   if !obj.DeletionTimestamp.IsZero() {
       return ctrl.Result{}, nil
   }
   ```
   This prevents accidentally creating child resources for a CR that's being deleted.
3. **Always make cleanup idempotent.** It will be retried. Returning "not found" on the second call must be safe.
4. **Have a maximum-retry policy.** If external cleanup fails for an hour, decide: stay stuck (correct for safety-critical resources like databases) or remove the finalizer anyway and log loudly (correct for ephemeral resources like cache layers).
5. **Add a unique finalizer name.** Format: `<domain>/<purpose>`. Examples:
   - `myapp.example.com/cleanup`
   - `myapp.example.com/drain-traffic`
   - `myapp.example.com/snapshot-required`

   Don't use generic names like `finalizer` or `cleanup` — they will collide.
6. **Don't add finalizers in admission webhooks.** Add them in the reconciler. Admission webhooks are stateless; if they fail-open, you miss the chance to add the finalizer. Reconcilers are level-triggered and will retry forever.
7. **Use owner refs for Kubernetes-native children.** Don't use finalizers to delete child ConfigMaps; let GC do it.
8. **Use finalizers for external cleanup only.** Not for Kubernetes-native cleanup.
9. **Don't block on slow external operations indefinitely.** Add a deadline:
   ```go
   if time.Since(obj.DeletionTimestamp.Time) > 1*time.Hour {
       log.Error("cleanup timed out; removing finalizer to prevent stuck object")
       metrics.OrphanedExternalResources.Inc()
       removeFinalizer(obj, myFinalizer)
       return ctrl.Result{}, r.Update(ctx, obj)
   }
   ```
   Better an orphaned cloud LB (visible in cloud cost dashboards) than a stuck Kubernetes object (invisible until someone notices).
10. **Document your finalizer**, its name, what it does, and how to manually clear it. Operator users will need this exact information at 3 AM.
11. **Test the deletion path.** A common gap: integration tests cover create/update but not delete. Add a test:
    ```go
    func TestCleanupOnDelete(t *testing.T) {
        cr := createMyApp(...)
        // Wait for cleanup mark
        deleteMyApp(cr)
        // Assert: external resources also deleted
        // Assert: finalizer eventually removed
        // Assert: object is gone
    }
    ```
12. **RBAC for finalizers subresource.** Make sure your operator's ClusterRole includes `update` on `<resource>/finalizers`.

---

## 34. GC Controller Observability

Metrics exposed by the GC controller (in `pkg/controller/garbagecollector/metrics.go`):

```
garbage_collector_attempt_to_delete_queue_latency_seconds{...}
    Histogram of how long items spend in the attemptToDelete queue.
    High values → GC behind.

garbage_collector_dirty_processing_latency_seconds{...}
    Histogram of how long graph-change processing takes.
    High values → graph is large or churning fast.

garbage_collector_event_processing_latency_seconds{...}
    How long it takes to process each event.

garbage_collector_graph_changes_pending{...}
    Gauge of pending events in the graph-change queue.

garbage_collector_force_deleted_pods_total{}
    Counter of pods force-deleted by PodGC.
```

### 34.1 Useful alerts

```yaml
- alert: GCControllerBehind
  expr: |
    histogram_quantile(0.99,
      rate(garbage_collector_attempt_to_delete_queue_latency_seconds_bucket[5m])
    ) > 60
  for: 10m
  annotations:
    summary: GC controller is more than 60s behind; objects piling up

- alert: GCGraphTooLarge
  expr: garbage_collector_graph_changes_pending > 10000
  for: 5m
  annotations:
    summary: GC graph has >10k pending changes; investigate churn source

- alert: StuckTerminatingObjects
  expr: |
    sum by (resource, namespace) (
      kube_object_age_seconds{phase="Terminating"} > 600
    ) > 0
  for: 15m
  annotations:
    summary: Objects stuck Terminating for >10 minutes
```

### 34.2 Cluster-level GC tuning

The GC controller has flags on kube-controller-manager:

- `--concurrent-gc-syncs=20` (default): number of worker goroutines processing the `attemptToDelete` queue.
- `--terminated-pod-gc-threshold=12500`: PodGC kicks in above this number.

On large clusters (>5000 nodes, lots of CRDs, high Job throughput), bumping `--concurrent-gc-syncs` to 50–100 helps drain backlog. The cost: more apiserver QPS from GC.

---

## 35. Pitfalls: The Long List

A catalogue.

1. **Finalizer not removed on error path.** Reconciler returns early on error; never reaches `removeFinalizer`. Object stays Terminating. Always wrap cleanup in idempotent retries.
2. **Cleanup not idempotent.** Second call deletes something different (e.g., a Secret with a fixed name that's already gone but the call deletes a re-created Secret).
3. **Finalizer added without DeletionTimestamp handler.** You added the gate but never coded the cleanup; objects are permanently stuck.
4. **SSA + finalizer interaction.** Old SSA managed-fields for finalizers cause fights; clear managed-fields claims manually.
5. **UID in ownerRef wrong.** Silent orphan; the GC controller skips. Always use `SetControllerReference`.
6. **GC controller off or rate-limited.** Disabled by `--controllers=*,-garbagecollector` (don't do this) or rate-limited via low `--kube-api-qps`; objects pile up.
7. **Ownership cycle.** A owns B owns A. GC can't make progress without manual help.
8. **`cascade=orphan` in CI scripts.** Leaks Pods/RS/etc. that never get cleaned. Audit your scripts.
9. **`kubectl delete --grace-period=0 --force`.** Skips finalizers — DANGEROUS. The apiserver removes the row immediately; controllers never see the deletion. Use only when you're certain there's nothing to clean up.
10. **Namespace deletion stuck on one finalizer.** Triage the offender; fix it before patching finalizers.
11. **PV stuck Released because Reclaim policy mismatch.** Reclaim=Retain doesn't auto-cleanup; PV needs manual disposal.
12. **Service Type=LoadBalancer with cloud provider down.** The load-balancer-cleanup finalizer stays; Service stuck Terminating. Wait for cloud provider recovery, or manually remove cloud LB and patch finalizer.
13. **Uninstalled operator leaving finalizers.** Re-install the operator long enough to drain finalizers, then uninstall again. Or accept manual cleanup of orphaned external resources, then patch finalizers.
14. **Finalizer name collision between two controllers.** Both removing each other's finalizer or fighting for ownership. Use uniquely-namespaced finalizer strings.
15. **Bulk `deletecollection` without label safety.** A typo deletes the world. Always test with `--dry-run=server` first; reserve `deletecollection` RBAC for tightly-scoped roles.
16. **CRD deleted while CRs remain.** CRs become inaccessible via kubectl. Re-install the CRD, drain finalizers, re-uninstall.
17. **Manual removal of finalizers leaving cloud LBs orphaned.** Catastrophic cost impact at scale. Always do external cleanup first, then patch finalizers.
18. **Too many tiny CRs.** GC controller falls behind; informer memory grows; apiserver QPS spikes. Coalesce CRs into fewer larger ones.
19. **Foreground delete of a parent with millions of dependents.** Linear time in dependents. Use Background or pre-scale-down first.
20. **OwnerRef across non-matching namespace.** Apiserver rejects; CREATE fails. Use cluster-scoped owner.
21. **`controller=true` on two refs.** Apiserver rejects. Choose one.
22. **Managed-fields finalizer interaction.** Old SSA owners of finalizers cause stuck patches. Clear `managedFields` entries selectively.
23. **Pod with finalizer from a deleted operator.** Pod is stuck Terminating. Kubelet stops the containers but the Pod object doesn't go away. Manual finalizer patch needed.
24. **Helm uninstall leaving CRs behind.** Helm doesn't track CR instances of a CRD it installed; uninstall removes the CRD but the CRs survive (now inaccessible). Use `helm uninstall --wait` plus explicit CR cleanup before uninstall.
25. **GitOps controller (ArgoCD/Flux) repeatedly recreating an object you're trying to delete.** The CR has `prune=false` or the controller's source-of-truth still includes it. Remove from source first.
26. **`kubectl delete -f` with files that reference nonexistent resources.** Returns partial success; some objects deleted, others errored. Always check return code and logs.
27. **Cron-job-spawned Job objects piling up.** Set `spec.successfulJobsHistoryLimit` and `spec.failedJobsHistoryLimit` (or use `ttlSecondsAfterFinished`).
28. **Failure to set `terminationGracePeriodSeconds`** appropriately. The Pod-delete protocol gives the kubelet this many seconds to gracefully stop containers; too short causes data loss; too long causes deletion-blocking.
29. **Pod with `preStop` hook that loops forever.** Pod stuck in Terminating until grace period expires, then SIGKILL. Long grace periods amplify the effect.
30. **CRD conversion webhook down during cascade.** GC tries to list dependents but cannot serialize them. Cascade stalls. Restore the webhook.

---

## 36. TL;DR

Garbage collection in Kubernetes is a two-phase delete protocol layered over etcd. Phase 1 sets `metadata.deletionTimestamp`; phase 2 (etcd row removal) happens only when `metadata.finalizers == []`. The atomicity of finalizer removal + etcd delete is the entire foundation of the cleanup model.

Three cascade policies decide what happens to dependents:
- **Background** (default): parent gone immediately, dependents cleaned asynchronously by the GC controller.
- **Foreground**: parent stays visible (with `foregroundDeletion` finalizer) until all `blockOwnerDeletion=true` dependents are gone.
- **Orphan**: dependents have their ownerRefs stripped; parent gone, dependents survive without a controller.

Owner refs (`metadata.ownerReferences`) encode the graph. The GC controller in `pkg/controller/garbagecollector/` watches every resource type, builds a UID-keyed in-memory graph, and chases pointers to issue cascade deletes. `controller=true` designates the authoritative reconciler; `blockOwnerDeletion=true` participates in the foreground protocol.

Finalizers are opaque strings in `metadata.finalizers`. While non-empty, the apiserver refuses to delete the etcd row. The convention is one finalizer per controller, named `<domain>/<purpose>`. The canonical reconciler pattern: on first sight, add finalizer; on DeletionTimestamp != nil, do cleanup, remove finalizer, return.

Built-in finalizers include `foregroundDeletion`, `orphan`, `kubernetes.io/pv-protection`, `kubernetes.io/pvc-protection`, `service.kubernetes.io/load-balancer-cleanup`. External finalizers come from operators: ACK, Crossplane, Velero, cert-manager, Strimzi. The "stuck Terminating" failure mode is almost always a finalizer whose owning controller is failing or absent.

The most useful design pattern for operators is to set owner refs on Kubernetes-native children (let GC do the work) and reserve finalizers for *external* resource cleanup. Always use UID, never name. Always handle DeletionTimestamp. Always make cleanup idempotent. Always have a deadline so an orphaned cloud LB is preferred over a stuck Kubernetes object.

The kubectl flag is `--cascade=background|foreground|orphan`. The wire-level header is `propagationPolicy` in the DELETE body. Default is Background.

TTLAfterFinished auto-deletes Jobs after `spec.ttlSecondsAfterFinished`. PodGC sweeps terminal pods over `--terminated-pod-gc-threshold`. Event TTL is 1 hour via etcd lease. Node deletion cascades through PodGC, Endpoints, EndpointSlices, VolumeAttachments, and node leases.

`deletecollection` is the bulk verb: powerful, dangerous, often missing from default RBAC. SSA does not manage finalizers; use PATCH/UPDATE. Audit every DELETE and every PATCH on `<resource>/finalizers`.

The whole system is eventually consistent, with the apiserver as the source of truth and the GC controller as the active reconciler. Understanding it is a prerequisite to writing operators that don't leak cloud resources, namespaces that delete cleanly, and clusters that survive a graceful shutdown.

Next: chapter 37 covers the cloud-provider integration that the load-balancer-cleanup finalizer points at. Chapter 38 is the capstone — building this whole thing from scratch.
