# Storage in Kubernetes: CSI, PV/PVC, and the Three-Phase Volume Lifecycle

How persistent state actually lands on a Pod. This chapter is the storage counterpart of chapter 15 (CNI) and chapter 14 (kube-proxy): it covers the API objects users touch (PV, PVC, StorageClass, VolumeSnapshot), the in-cluster machinery that reconciles them (the external sidecars and the kubelet volume manager), and the out-of-tree gRPC protocol — CSI — that finally calls a cloud SDK or a kernel `mount(2)` to make a block device or filesystem appear inside a container.

The goal: by the end, you should be able to pick up a Pending PVC, trace why it isn't binding, identify whether the breakage is in the provisioner sidecar, the CSI driver, the kubelet volume manager, the cloud API, or scheduling topology, and fix it without grepping the source tree blindly. Storage is the place where Kubernetes most clearly stops being a self-contained abstraction and becomes a thin orchestrator over the actual SAN/cloud/local-disk reality underneath. The leaks are everywhere — single-zone disks under multi-zone schedulers, ReadWriteMany requests against block-only drivers, stuck VolumeAttachments after node failures, online-expand without filesystem resize, snapshots without quiescence — and you can only debug them by understanding which component holds which lock.

Storage is also unusual in one respect: it has the longest critical path of any pod-startup operation. Network setup is a CNI exec measured in tens of milliseconds; image pull is parallel with everything; but a CSI `ControllerPublishVolume` against EBS can take 20–60 seconds, and a stuck multipath detach can wedge a Pod's replacement for 6 minutes (the default `--node-monitor-grace-period`) plus the cloud API's own detach timeout. Most "Pod stuck in ContainerCreating for 8 minutes" tickets are storage tickets.

---

## Table of Contents

1. [The Storage Object Model](#1-the-storage-object-model)
2. [Static vs Dynamic Provisioning, and the Bind Cycle](#2-static-vs-dynamic-provisioning-and-the-bind-cycle)
3. [`WaitForFirstConsumer` vs `Immediate` Binding](#3-waitforfirstconsumer-vs-immediate-binding)
4. [CSI Architecture: Controller Plugin + Node Plugin](#4-csi-architecture-controller-plugin--node-plugin)
5. [The CSI gRPC Services](#5-the-csi-grpc-services)
6. [The Three-Phase Lifecycle (Provision → Attach → Stage/Mount)](#6-the-three-phase-lifecycle-provision--attach--stagemount)
7. [Pod Termination: Reverse Lifecycle](#7-pod-termination-reverse-lifecycle)
8. [The External Sidecars](#8-the-external-sidecars)
9. [StorageClass: Parameters, Reclaim, BindingMode, Expansion, Topology](#9-storageclass-parameters-reclaim-bindingmode-expansion-topology)
10. [Access Modes: RWO / ROX / RWX / RWOP](#10-access-modes-rwo--rox--rwx--rwop)
11. [Volume Snapshots](#11-volume-snapshots)
12. [Volume Expansion: Online vs Offline](#12-volume-expansion-online-vs-offline)
13. [Ephemeral Volumes](#13-ephemeral-volumes)
14. [Raw Block Volumes (`volumeMode: Block`)](#14-raw-block-volumes-volumemode-block)
15. [CSI Migration: In-Tree → Out-of-Tree](#15-csi-migration-in-tree--out-of-tree)
16. [The Driver Zoo: Cloud, Networked, Local](#16-the-driver-zoo-cloud-networked-local)
17. [Topology-Aware Provisioning](#17-topology-aware-provisioning)
18. [Mount Propagation](#18-mount-propagation)
19. [Performance: IOPS, Throughput, Latency Tax](#19-performance-iops-throughput-latency-tax)
20. [Backup Integration with Velero](#20-backup-integration-with-velero)
21. [`ReadWriteOncePod`: The Real Mutex](#21-readwriteoncepod-the-real-mutex)
22. [Pre-Provisioned PVs](#22-pre-provisioned-pvs)
23. [Reclaim Policy and Accidental Delete Recovery](#23-reclaim-policy-and-accidental-delete-recovery)
24. [The PVC Protection Finalizer](#24-the-pvc-protection-finalizer)
25. [Writing a CSI Driver](#25-writing-a-csi-driver)
26. [Debugging Storage](#26-debugging-storage)
27. [Observability and Metrics](#27-observability-and-metrics)
28. [Pitfalls](#28-pitfalls)
29. [TL;DR](#29-tldr)

---

## 1. The Storage Object Model

Kubernetes deliberately separates *what a workload wants* (a claim) from *what the cluster has* (a volume) from *how the cluster makes more* (a class), with three further objects (`VolumeAttachment`, `CSIDriver`, `CSINode`) that exist solely to coordinate the multiple controllers that drive a volume through its lifecycle.

### 1.1 The Object Graph

```
                       ┌─────────────────────────────────────────┐
                       │              USER NAMESPACE              │
                       │                                          │
                       │   ┌─────────────┐      ┌─────────────┐  │
                       │   │    Pod      │─────▶│ PersistentVol│ │
                       │   │             │volumes│   Claim     │  │
                       │   │ spec.volumes│      │  (PVC)       │  │
                       │   └─────────────┘      └──────┬───────┘  │
                       │                               │           │
                       └───────────────────────────────┼───────────┘
                                                       │ claimRef
                                                       ▼
                       ┌───────────────────────────────────────────┐
                       │            CLUSTER SCOPE                  │
                       │                                            │
                       │   ┌─────────────────┐  ┌────────────────┐ │
                       │   │ PersistentVolume│◀─│ StorageClass    │ │
                       │   │      (PV)        │  │  (provisioner   │ │
                       │   │ - capacity       │  │   template)     │ │
                       │   │ - accessModes    │  │ - parameters    │ │
                       │   │ - csi.driver     │  │ - reclaimPolicy │ │
                       │   │ - csi.volumeHandle│ │ - volumeBindingMode│
                       │   └─────────┬────────┘  └────────────────┘ │
                       │             │ nodeAffinity                  │
                       │             ▼                                │
                       │   ┌─────────────────┐  ┌────────────────┐ │
                       │   │ VolumeAttachment│  │ CSIDriver        │ │
                       │   │ (per node ×     │  │ (registration:   │ │
                       │   │  per volume)    │  │  attachRequired, │ │
                       │   │                  │  │  podInfoOnMount, │ │
                       │   │                  │  │  fsGroupPolicy)  │ │
                       │   └─────────────────┘  └────────────────┘ │
                       │                                            │
                       │   ┌─────────────────┐                      │
                       │   │   CSINode        │  per-node:           │
                       │   │  (per Node)      │  - driver list       │
                       │   │                  │  - nodeID per driver │
                       │   │                  │  - topology keys     │
                       │   └─────────────────┘                      │
                       └───────────────────────────────────────────┘
```

### 1.2 Resource Cheat Sheet

| Object | Scope | Owned by | Purpose |
|---|---|---|---|
| `PersistentVolume` (PV) | Cluster | provisioner or admin | A *handle* to a real storage object (an EBS volume, a Ceph image, an NFS export). Decoupled from the consuming workload. |
| `PersistentVolumeClaim` (PVC) | Namespace | user | A *request* for a PV of a certain class, size, accessMode. Pods reference this, not the PV. |
| `StorageClass` (SC) | Cluster | admin | A *template* describing which CSI driver provisions, with what parameters, with what binding/reclaim semantics. |
| `VolumeAttachment` (VA) | Cluster | external-attacher | A per-(PV, Node) record telling the CSI controller "attach this volume to that node." The kubelet won't mount until this is `attached: true`. |
| `CSIDriver` | Cluster | operator/install | Driver registration: capabilities (attach required? mount info?), filesystem policies, ephemeral support. |
| `CSINode` | Cluster (one per Node) | node-driver-registrar | Per-node enumeration of installed drivers, the node's driver-specific node-ID, and topology labels. |
| `VolumeSnapshot` | Namespace | user | A *request* for a point-in-time copy of a PVC. |
| `VolumeSnapshotContent` (VSC) | Cluster | external-snapshotter | The actual cloud-side or driver-side snapshot, paired with a VolumeSnapshot like PV↔PVC. |
| `VolumeSnapshotClass` | Cluster | admin | Template for snapshot creation (driver, deletionPolicy). |

The PV/PVC split is the single most important shape to internalize: **a Pod never names a PV**. It names a PVC, and the binder finds (or creates) a PV that matches. This is what enables the PVC to outlive the Pod, to survive Pod replacement during a rolling update, and to act as the unit of "data that I, the user, own."

### 1.3 A Minimum-Viable YAML Set

```yaml
# storageclass.yaml — admin-owned, cluster-scoped template
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
provisioner: ebs.csi.aws.com           # the CSI driver name
parameters:
  type: gp3
  iops: "3000"
  throughput: "125"
  encrypted: "true"
reclaimPolicy: Delete                  # or Retain
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
mountOptions:
  - noatime
allowedTopologies:
  - matchLabelExpressions:
      - key: topology.ebs.csi.aws.com/zone
        values: [us-east-1a, us-east-1b, us-east-1c]
---
# pvc.yaml — user-owned, namespaced claim
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data-pvc
  namespace: app
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: gp3
  resources:
    requests:
      storage: 50Gi
---
# pod.yaml — references the PVC by name, not the PV
apiVersion: v1
kind: Pod
metadata:
  name: app
  namespace: app
spec:
  containers:
    - name: app
      image: app:1.0
      volumeMounts:
        - name: data
          mountPath: /var/lib/app
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: data-pvc
---
# After binding, the PV is automatically created:
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pvc-9c2f5a3e-...    # auto-generated by the external-provisioner
  finalizers:
    - kubernetes.io/pv-protection
    - external-attacher/ebs-csi-aws-com
spec:
  capacity:
    storage: 50Gi
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Delete
  storageClassName: gp3
  claimRef:                            # bound to the PVC above
    namespace: app
    name: data-pvc
    uid: 8a1f...
  csi:
    driver: ebs.csi.aws.com
    volumeHandle: vol-0123456789abcdef0   # real cloud-side ID
    fsType: ext4
    volumeAttributes:
      storage.kubernetes.io/csiProvisionerIdentity: 1700000000-1234-ebs.csi.aws.com
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: topology.ebs.csi.aws.com/zone
              operator: In
              values: [us-east-1a]
```

The PV's `spec.nodeAffinity` is what later restricts the scheduler — a single-AZ EBS volume can only attach to nodes in that AZ, and the scheduler honors that affinity transparently via the `VolumeBinding` plugin (chapter 09).

---

## 2. Static vs Dynamic Provisioning, and the Bind Cycle

There are two ways a PV comes to exist.

```
STATIC PROVISIONING (rare in cloud, common on-prem)

  Admin creates real backing storage out-of-band
    (e.g., `aws ec2 create-volume`, `rbd create`, `nfs export`)
       │
       ▼
  Admin creates a PV with the right driver + volumeHandle, no claimRef
       │
       ▼
  User creates a PVC with matching size + accessMode + storageClassName ""
                                                       (or omitted)
       │
       ▼
  PV controller in kube-controller-manager:
    walks unbound PVCs, walks unbound PVs, matches by:
       storageClassName, accessModes, size (PV >= PVC request),
       selector (if PVC has matchLabels), nodeAffinity (if any)
    on match: writes pvc.spec.volumeName and pv.spec.claimRef
              atomic, optimistic-concurrency on resourceVersion

DYNAMIC PROVISIONING (the default in any modern cluster)

  User creates a PVC referencing storageClassName "gp3"
       │
       ▼
  external-provisioner sidecar (sitting next to the CSI controller plugin):
    watches PVCs; for each PVC matching its driver name + Pending state:
       calls CSI CreateVolume(name, capacity, parameters, accessibilityRequirements)
       on success: writes a new PV with claimRef set to the PVC
       sets pv.spec.csi.volumeHandle to the returned volume ID
  PV controller binds PVC ↔ PV (same logic as static)
```

### 2.1 The Bind Cycle in More Detail

The binder lives in `kube-controller-manager` (`pkg/controller/volume/persistentvolume`). It runs two reconcile loops — one over PVCs, one over PVs — that converge on the same `(PV.claimRef, PVC.volumeName)` invariant.

```
PVC controller loop:                  PV controller loop:
  for each PVC:                         for each PV:
    if Pending:                           if Available:
      if static-class:                       if has claimRef:
        find matching PV                       check PVC still wants it
        write pvc.volumeName +                 → write pv.status=Bound
            pv.claimRef                      else:
      if dynamic-class:                       leave Available; wait for PVC controller
        wait for provisioner (out-of-process)
    if Bound:                             if Bound:
      check pv.claimRef.uid == pvc.uid       check pvc.uid matches claimRef.uid
      if not: requeue with error             if stale claim: → Released
      set status.phase = Bound
```

The key safety property is the **UID check on `claimRef`**. A PV's `claimRef.uid` pins it to a specific PVC instance. If you delete and recreate a PVC with the same name, the new PVC has a new UID. The PV sees a mismatch and moves to `Released`. It will NOT auto-rebind to a PVC just because the name matches — this is exactly the protection that prevents accidental data exposure to a re-created namespace.

```
PV phases:

  Available  ── claim found ──▶ Bound  ── claim deleted ──▶ Released
                                                                  │
                                                                  ▼
                                                  reclaimPolicy = Delete?
                                                   ├── yes: external-provisioner deletes,
                                                   │        then PV is gone
                                                   └── no  (Retain): stays Released forever
                                                              until admin acts

  Available is the entry state for static or post-delete PVs that
  weren't tied to a claim. Dynamic PVs jump straight from "doesn't exist"
  to Bound (created with claimRef already set).
```

### 2.2 Bind Diagram

```
                            ┌──────────────┐
       user creates PVC ──▶ │ PVC: Pending │
                            └──────┬───────┘
                                   │
              ┌────────────────────┴──────────────────────┐
              │                                            │
   storageClassName=""                          storageClassName="gp3"
   (or no provisioner)                          (dynamic)
              │                                            │
              ▼                                            ▼
   PVC binder scans existing PVs                external-provisioner sidecar
   for compatible Available PV.                 in the driver Deployment
              │                                  watches PVCs of class gp3.
              │                                            │
              │  match?                                    │
              ├── no → stays Pending                       │
              │       (no provisioner, no static PV)       │
              │                                            ▼
              │                              calls CSI CreateVolume()
              │                              over UDS to controller plugin
              │                                            │
              │                                            ▼
              │                              cloud SDK / driver creates volume
              │                                            │
              │                                            ▼
              │                              sidecar writes a PV object
              │                              with claimRef pre-filled
              │                                            │
              ▼                                            ▼
              PV controller validates claimRef.uid == PVC.uid,
              sets PVC.spec.volumeName, marks both Bound.
```

---

## 3. `WaitForFirstConsumer` vs `Immediate` Binding

The `volumeBindingMode` on a StorageClass is one of the most consequential settings in a multi-AZ cloud cluster.

### 3.1 The Modes

```
volumeBindingMode: Immediate

  PVC created
     │
     ▼
  external-provisioner immediately calls CreateVolume
  with NO topology hint (it doesn't know which node the pod will use)
     │
     ▼
  cloud creates volume in some zone (typically the first listed,
  or whatever the cloud SDK picks)
     │
     ▼
  PV is created with nodeAffinity = "this zone only"
     │
     ▼
  Pod is scheduled later — must land in that zone, or stays Pending forever


volumeBindingMode: WaitForFirstConsumer  ★ (the right default)

  PVC created → stays Pending (no provisioning yet!)
     │
     ▼
  Pod referencing the PVC is created
     │
     ▼
  Scheduler picks a node (considering CPU, memory, taints, affinity,
                          topology spread, etc.)
     │
     ▼
  Scheduler annotates the PVC: volume.kubernetes.io/selected-node = <node>
     │
     ▼
  external-provisioner sees the annotation, calls CreateVolume
  with accessibilityRequirements = topology of the selected node
     │
     ▼
  cloud creates volume IN the right zone
     │
     ▼
  PVC binds, Pod proceeds to mount
```

### 3.2 Why `WaitForFirstConsumer` Is Almost Always Right

The classic failure mode for `Immediate`:

```
       Cluster spans us-east-1a, 1b, 1c
       PVC for "logs-pvc" created with Immediate binding
       Provisioner picks zone 1a (alphabetical)
       PV nodeAffinity = us-east-1a

       Later: deployment.replicas = 1, anti-affinity not set
       Scheduler decides node-2b is the best fit (lowest load)
       Filter plugin VolumeBinding REJECTS node-2b
            (PV requires 1a, node is in 1b)
       Scheduler tries every node in 1b, 1c — all rejected
       Only nodes in 1a are eligible
       If 1a is full or being drained → Pod stays Pending
       User: "Why is my pod pending? There's plenty of capacity!"
```

`WaitForFirstConsumer` inverts the problem: the scheduler picks the node first, considering all constraints, and only then does the volume get created in the right zone. This is also how the scheduler can co-locate two PVCs (same Pod, two volumes) in the same zone — the scheduler's `VolumeBinding` plugin handles "pre-binding" across both.

You want `Immediate` only when:
- The driver is zone-less (e.g., NFS, or a global filesystem)
- You're statically pre-provisioning and want to recycle PVs
- You have a topology-aware driver that genuinely doesn't pin a volume to one zone

For every cloud-block-storage class (EBS, GCE PD, Azure Disk), use `WaitForFirstConsumer`. Cloud-provider default StorageClasses set this since ~1.17.

---

## 4. CSI Architecture: Controller Plugin + Node Plugin

CSI (Container Storage Interface) is the boundary between Kubernetes and the storage system. Pre-CSI, every storage backend had its code compiled into `kube-controller-manager` and `kubelet` — the so-called *in-tree* plugins. CSI moved that out: each backend is a separate process (or pair of processes) speaking gRPC over a Unix domain socket. Kubernetes itself is now storage-agnostic.

### 4.1 The Two Plugins

A CSI driver in production almost always ships two distinct workloads:

```
                    Kubernetes cluster
   ┌────────────────────────────────────────────────────────────────────┐
   │                                                                     │
   │   ┌─────────────────────────────────────────────────────────┐     │
   │   │  CONTROLLER PLUGIN  (Deployment, replicas≥1, leader-elected)│   │
   │   │                                                          │     │
   │   │  Pod:                                                    │     │
   │   │    ┌──────────────────┐  ┌────────────────────────────┐  │     │
   │   │    │ csi-driver       │  │ external-provisioner       │  │     │
   │   │    │ (controller mode)│  │ (sidecar)                  │  │     │
   │   │    │                  │  │ ┌──────────┐               │  │     │
   │   │    │ implements:      │◀──gRPC│CreateVolume│           │  │     │
   │   │    │   Identity       │  │ └──────────┘               │  │     │
   │   │    │   Controller     │  └────────────────────────────┘  │     │
   │   │    │                  │  ┌────────────────────────────┐  │     │
   │   │    │                  │◀─│ external-attacher          │  │     │
   │   │    │                  │  └────────────────────────────┘  │     │
   │   │    │                  │  ┌────────────────────────────┐  │     │
   │   │    │                  │◀─│ external-resizer            │  │     │
   │   │    │                  │  └────────────────────────────┘  │     │
   │   │    │                  │  ┌────────────────────────────┐  │     │
   │   │    │                  │◀─│ external-snapshotter        │  │     │
   │   │    └────────┬─────────┘  └────────────────────────────┘  │     │
   │   │             │ unix socket: /csi/csi.sock                  │     │
   │   │             ▼                                              │     │
   │   │      ┌──────────────────┐                                  │     │
   │   │      │ cloud SDK / API  │  (e.g., AWS EBS CreateVolume)    │     │
   │   │      └──────────────────┘                                  │     │
   │   └─────────────────────────────────────────────────────────┘     │
   │                                                                     │
   │   ┌─────────────────────────────────────────────────────────┐     │
   │   │  NODE PLUGIN  (DaemonSet, one per node)                  │     │
   │   │                                                          │     │
   │   │   Pod:                                                   │     │
   │   │     ┌─────────────────┐  ┌──────────────────────────┐    │     │
   │   │     │ csi-driver       │  │ node-driver-registrar    │    │     │
   │   │     │ (node mode)      │  │ (sidecar)                │    │     │
   │   │     │                  │  │                          │    │     │
   │   │     │ implements:      │  │ talks to kubelet's       │    │     │
   │   │     │   Identity       │  │ plugin watcher socket    │    │     │
   │   │     │   Node           │  │ at /var/lib/kubelet/     │    │     │
   │   │     │                  │  │   plugins_registry/      │    │     │
   │   │     │ does:            │  │                          │    │     │
   │   │     │   NodeStageVolume│  │ registers this node's    │    │     │
   │   │     │   NodePublishVol │  │ driver with kubelet so   │    │     │
   │   │     │   NodeUnstageVol │  │ kubelet knows to call it │    │     │
   │   │     │   NodeUnpublishVol  │ for matching PVs         │    │     │
   │   │     │   NodeExpandVol  │  │                          │    │     │
   │   │     │                  │  │ also writes the CSINode  │    │     │
   │   │     │ mounts to:       │  │ object (driver + nodeID) │    │     │
   │   │     │   host /var/lib/ │  └──────────────────────────┘    │     │
   │   │     │   kubelet/pods   │                                  │     │
   │   │     │   (mountPropagation: Bidirectional)              │     │
   │   │     └─────────────────┘                                 │     │
   │   └─────────────────────────────────────────────────────────┘     │
   └────────────────────────────────────────────────────────────────────┘
```

### 4.2 Sockets and Filesystem Layout

The plumbing between the kubelet and the CSI driver is purely Unix-domain sockets on the host filesystem, mounted into the driver Pod via `hostPath`.

```
HOST FILESYSTEM ON A NODE
─────────────────────────
/var/lib/kubelet/
├── plugins_registry/
│   └── ebs.csi.aws.com-reg.sock          ← node-driver-registrar exposes this
│                                            kubelet's plugin watcher polls it
│
├── plugins/
│   └── ebs.csi.aws.com/
│       └── csi.sock                       ← the actual CSI Node-side socket
│                                            kubelet connects here for Node RPCs
│
└── pods/
    └── <pod-uid>/
        └── volumes/
            └── kubernetes.io~csi/
                └── <pv-name>/
                    ├── mount/             ← bind-mount into the container
                    └── vol_data.json      ← kubelet's bookkeeping
```

The split between `plugins_registry/` and `plugins/` is the **registration protocol**. The node-driver-registrar sidecar exposes a tiny Identity-only socket in `plugins_registry/`; kubelet's plugin watcher polls that socket, calls `GetInfo`, learns the driver name and the *real* socket path under `plugins/`, then writes the `CSINode` resource. Only after this registration round-trip will the kubelet route Node-RPCs (Stage/Publish) to the driver.

### 4.3 Why the Split (Controller vs Node)?

The controller-side operations (`CreateVolume`, `DeleteVolume`, `ControllerPublishVolume`, `CreateSnapshot`) need cloud credentials and run cluster-wide actions — they belong to one logical process, leader-elected, that nobody else duplicates. The node-side operations (`NodeStageVolume`, `NodePublishVolume`) need to manipulate the node's actual kernel mount table — they must run on the node, in a process with `CAP_SYS_ADMIN`, with `mountPropagation: Bidirectional` into the host's `/var/lib/kubelet/pods` tree so that bind-mounts it creates are visible to the kubelet (and through it, to the container).

The driver process itself is usually a single binary that flips behavior based on a `--mode=controller` or `--mode=node` flag. The sidecars (provisioner, attacher, ...) are separate processes from `kubernetes-csi/external-*` that translate Kubernetes API events into CSI gRPCs.

---

## 5. The CSI gRPC Services

CSI defines three gRPC services. Identity is mandatory in both controller and node modes; Controller is implemented in the controller plugin; Node is implemented in the node plugin.

### 5.1 The proto (excerpt from `container-storage-interface/spec/csi.proto`)

```protobuf
service Identity {
  rpc GetPluginInfo(GetPluginInfoRequest) returns (GetPluginInfoResponse) {}
  rpc GetPluginCapabilities(GetPluginCapabilitiesRequest)
      returns (GetPluginCapabilitiesResponse) {}
  rpc Probe(ProbeRequest) returns (ProbeResponse) {}
}

service Controller {
  rpc CreateVolume(CreateVolumeRequest) returns (CreateVolumeResponse) {}
  rpc DeleteVolume(DeleteVolumeRequest) returns (DeleteVolumeResponse) {}
  rpc ControllerPublishVolume(ControllerPublishVolumeRequest)
      returns (ControllerPublishVolumeResponse) {}
  rpc ControllerUnpublishVolume(ControllerUnpublishVolumeRequest)
      returns (ControllerUnpublishVolumeResponse) {}
  rpc ValidateVolumeCapabilities(ValidateVolumeCapabilitiesRequest)
      returns (ValidateVolumeCapabilitiesResponse) {}
  rpc ListVolumes(ListVolumesRequest) returns (ListVolumesResponse) {}
  rpc GetCapacity(GetCapacityRequest) returns (GetCapacityResponse) {}
  rpc ControllerGetCapabilities(ControllerGetCapabilitiesRequest)
      returns (ControllerGetCapabilitiesResponse) {}
  rpc CreateSnapshot(CreateSnapshotRequest) returns (CreateSnapshotResponse) {}
  rpc DeleteSnapshot(DeleteSnapshotRequest) returns (DeleteSnapshotResponse) {}
  rpc ListSnapshots(ListSnapshotsRequest) returns (ListSnapshotsResponse) {}
  rpc ControllerExpandVolume(ControllerExpandVolumeRequest)
      returns (ControllerExpandVolumeResponse) {}
  rpc ControllerGetVolume(ControllerGetVolumeRequest)
      returns (ControllerGetVolumeResponse) {}
  rpc ControllerModifyVolume(ControllerModifyVolumeRequest)
      returns (ControllerModifyVolumeResponse) {}
}

service Node {
  rpc NodeStageVolume(NodeStageVolumeRequest) returns (NodeStageVolumeResponse) {}
  rpc NodeUnstageVolume(NodeUnstageVolumeRequest) returns (NodeUnstageVolumeResponse) {}
  rpc NodePublishVolume(NodePublishVolumeRequest) returns (NodePublishVolumeResponse) {}
  rpc NodeUnpublishVolume(NodeUnpublishVolumeRequest)
      returns (NodeUnpublishVolumeResponse) {}
  rpc NodeGetVolumeStats(NodeGetVolumeStatsRequest)
      returns (NodeGetVolumeStatsResponse) {}
  rpc NodeExpandVolume(NodeExpandVolumeRequest) returns (NodeExpandVolumeResponse) {}
  rpc NodeGetCapabilities(NodeGetCapabilitiesRequest)
      returns (NodeGetCapabilitiesResponse) {}
  rpc NodeGetInfo(NodeGetInfoRequest) returns (NodeGetInfoResponse) {}
}
```

### 5.2 What Each RPC Does (the abridged reference)

| RPC | Side | What it actually does | Triggered by |
|---|---|---|---|
| `GetPluginInfo` | both | Returns driver name + vendor version. Driver name MUST match what's used in PVs/StorageClasses. | kubelet during registration |
| `GetPluginCapabilities` | both | Lists capabilities: `CONTROLLER_SERVICE` (driver has a controller), `VOLUME_ACCESSIBILITY_CONSTRAINTS` (driver is topology-aware). | external-provisioner once at startup |
| `Probe` | both | Lightweight health check. | sidecars periodically |
| `CreateVolume` | controller | Allocate a new volume of `capacity_range.required_bytes`, in `accessibility_requirements.preferred` topology. Idempotent on `name`. Returns `volume_id`. | external-provisioner on PVC create |
| `DeleteVolume` | controller | Delete the backing volume. | external-provisioner on PV delete (if reclaim=Delete) |
| `ControllerPublishVolume` | controller | "Attach" a volume to a node. For block storage: hot-plug into the VM's PCI bus. Returns a `publish_context` map (often the device path). | external-attacher on VolumeAttachment create |
| `ControllerUnpublishVolume` | controller | Detach from node. | external-attacher on VA delete |
| `CreateSnapshot` | controller | Create a snapshot of an existing volume. Idempotent on `name`. | external-snapshotter |
| `DeleteSnapshot` | controller | Delete a snapshot. | external-snapshotter |
| `ControllerExpandVolume` | controller | Grow the backing storage to a new size. | external-resizer |
| `NodeStageVolume` | node | Mount the device into a *staging path* — a global per-node mount that is shared across pods. For block: format if needed, then `mount(8)` to `/var/lib/kubelet/plugins/.../globalmount`. | kubelet volume manager |
| `NodePublishVolume` | node | Bind-mount the staged path into the per-pod path `/var/lib/kubelet/pods/<pod-uid>/volumes/kubernetes.io~csi/<pv>/mount`. | kubelet volume manager |
| `NodeUnpublishVolume` | node | Unmount the per-pod bind mount. | kubelet on pod delete |
| `NodeUnstageVolume` | node | Unmount the staging mount once no pod on this node uses it. | kubelet |
| `NodeExpandVolume` | node | Filesystem-level resize after the controller grew the block device (e.g., `resize2fs`, `xfs_growfs`). | kubelet (online) or VolumeAttachment controller (offline) |
| `NodeGetVolumeStats` | node | Returns capacity/used bytes/inodes. Source for `kubelet_volume_stats_*` metrics. | kubelet periodically |
| `NodeGetInfo` | node | Returns the driver-specific node ID (the string the cloud uses, e.g., the EC2 instance ID) and topology labels. | node-driver-registrar on startup |

A driver advertises which RPCs it implements via `GetPluginCapabilities` and `ControllerGetCapabilities` / `NodeGetCapabilities`. NFS-style drivers, for example, often don't implement `ControllerPublishVolume` (no attach step — every node can mount); they declare themselves attach-less by setting `attachRequired: false` on their `CSIDriver` object.

### 5.3 Idempotency Requirements

CSI RPCs are required by spec to be idempotent on their primary key:
- `CreateVolume` is keyed by `name` (the external-provisioner passes the PV name → if the driver gets called twice with the same name, the second call returns the same volume).
- `ControllerPublishVolume` is keyed by `(volume_id, node_id)`.
- `NodeStageVolume` is keyed by `staging_target_path`.

This is non-negotiable because the sidecars retry aggressively. A 30-second network blip between the provisioner and the cloud is normal; the provisioner will retry `CreateVolume` and would create duplicate cloud volumes if the driver weren't idempotent. Most cloud APIs solve this with a client-side request token: the driver hashes the CSI `name` parameter into the cloud API's idempotency token.

### 5.4 Error Codes and Their Semantics

CSI uses standard gRPC status codes with very specific meanings. The sidecars react differently to each one, so getting them right is essential.

| gRPC code | Meaning | Sidecar behavior |
|---|---|---|
| `OK` (0) | Success. | Mark operation complete. |
| `CANCELED` (1) | Operation was canceled. | Retry with backoff. |
| `UNKNOWN` (2) | Internal driver bug or unhandled error. | Retry with backoff; alert. |
| `INVALID_ARGUMENT` (3) | Caller's request is malformed. | DO NOT retry; surface as Event. |
| `DEADLINE_EXCEEDED` (4) | Operation timed out (cloud-side). | Retry with backoff; operation may have partially completed. |
| `NOT_FOUND` (5) | Volume doesn't exist. | For Delete: success; for others: surface as failure. |
| `ALREADY_EXISTS` (6) | A volume with that name exists with INCOMPATIBLE parameters. | Surface as failure; user must rename. |
| `PERMISSION_DENIED` (7) | Cloud credentials lack permission. | Retry briefly, then surface — usually IAM issue. |
| `RESOURCE_EXHAUSTED` (8) | Quota or rate limit. | Retry with long backoff. |
| `FAILED_PRECONDITION` (9) | Driver state doesn't permit (e.g., trying to delete attached volume). | Surface; controller will resolve precondition. |
| `ABORTED` (10) | Concurrency conflict; retry. | Retry. |
| `OUT_OF_RANGE` (11) | Capacity request outside supported range. | Surface as failure. |
| `UNIMPLEMENTED` (12) | RPC not implemented. | Sidecar should never call this; bug if it does. |
| `INTERNAL` (13) | Driver bug. | Retry with backoff; alert. |
| `UNAVAILABLE` (14) | Driver is starting up / cloud temporarily unavailable. | Retry quickly. |
| `DATA_LOSS` (15) | Unrecoverable. | Surface as critical alert. |
| `UNAUTHENTICATED` (16) | Auth failed. | Surface; usually credential rotation needed. |

A common driver bug: returning `INTERNAL` instead of `ALREADY_EXISTS` when a duplicate-name `CreateVolume` arrives. The provisioner then retries forever, and the user sees endless `ProvisioningFailed` events. Reading the gRPC status code in the error message is half the diagnostic.

### 5.5 The `volume_capability` Negotiation

Every Node-RPC and `ValidateVolumeCapabilities` carries a `VolumeCapability` describing what the caller wants. The driver returns whether it can serve it.

```protobuf
message VolumeCapability {
  message BlockVolume { /* empty */ }
  message MountVolume {
    string fs_type = 1;
    repeated string mount_flags = 2;
    string volume_mount_group = 3;        // fsGroup, since CSI 1.6
  }
  oneof access_type {
    BlockVolume block = 1;
    MountVolume mount = 2;
  }
  AccessMode access_mode = 3;             // SINGLE_NODE_WRITER, MULTI_NODE_READER_ONLY, etc.
}
```

The negotiation matters because it's how the driver knows whether to format a filesystem (`mount` mode) or just symlink a block device (`block` mode), and whether to enforce single-writer semantics. `ValidateVolumeCapabilities` is also the way the external-provisioner asks "can this volume serve RWX?" before binding — the answer informs PV creation.

---

## 6. The Three-Phase Lifecycle (Provision → Attach → Stage/Mount)

The single most important diagram in this chapter.

```
                       ★ THREE-PHASE VOLUME LIFECYCLE ★

  ┌────────────┐ ┌──────────────────────────────────────────────────────────────┐
  │  PHASE 1   │ │  PROVISION                                                   │
  │ "Provision"│ │  Happens once per volume. Cluster-wide. Cloud-API-bound.    │
  └────────────┘ │                                                              │
                 │  user → PVC created                                          │
                 │       ↓                                                       │
                 │  external-provisioner sees Pending PVC of its driver class  │
                 │       ↓                                                       │
                 │  gRPC: CSI.Controller.CreateVolume(name, size, topology)    │
                 │       ↓                                                       │
                 │  driver: cloud SDK CreateVolume → vol-0123...                │
                 │       ↓                                                       │
                 │  provisioner: write PV object, claimRef preset               │
                 │       ↓                                                       │
                 │  PV binder: PVC.spec.volumeName + PV.status=Bound            │
                 └──────────────────────────────────────────────────────────────┘
                              │
                              ▼  (later: a pod referencing this PVC is scheduled)
                 ┌──────────────────────────────────────────────────────────────┐
  ┌────────────┐ │  ATTACH                                                      │
  │  PHASE 2   │ │  Happens once per (volume, node). Cluster-wide. Cloud-API.  │
  │  "Attach"  │ │                                                              │
  └────────────┘ │  scheduler: binds pod to node-2                              │
                 │       ↓                                                       │
                 │  attach-detach controller in kube-controller-manager         │
                 │  (or kubelet, if --enable-controller-attach-detach=false)    │
                 │  creates a VolumeAttachment object:                          │
                 │      VA{ PV: pv-xxx, Node: node-2, attached: false }         │
                 │       ↓                                                       │
                 │  external-attacher sidecar (next to controller plugin)       │
                 │  sees the VA, calls:                                         │
                 │  gRPC: CSI.Controller.ControllerPublishVolume(vol, node)    │
                 │       ↓                                                       │
                 │  driver: cloud SDK AttachVolume(vol-0123, instance-i-abc)   │
                 │     hardware: hypervisor hot-plugs the disk to the VM       │
                 │     OS sees a new /dev/nvme1n1 (or /dev/xvdf, /dev/sdb)     │
                 │       ↓                                                       │
                 │  driver returns publish_context = { devicePath: /dev/nvme1n1}│
                 │       ↓                                                       │
                 │  attacher patches VA: attached=true, attachmentMetadata=…   │
                 └──────────────────────────────────────────────────────────────┘
                              │
                              ▼
                 ┌──────────────────────────────────────────────────────────────┐
  ┌────────────┐ │  STAGE + PUBLISH (Mount)                                     │
  │  PHASE 3   │ │  Happens for each pod consuming the volume. Per-node.       │
  │  "Mount"   │ │  Kernel-bound (no cloud API).                                │
  └────────────┘ │                                                              │
                 │  kubelet volume manager (chapter 10) sees:                  │
                 │    Pod assigned to me, uses PVC bound to PV pv-xxx          │
                 │    VolumeAttachment(pv-xxx, this-node).attached == true     │
                 │       ↓                                                       │
                 │  if first pod on this node to use this PV:                   │
                 │    gRPC: CSI.Node.NodeStageVolume(                           │
                 │       volume_id=vol-0123,                                    │
                 │       staging_target_path=/var/lib/kubelet/plugins/         │
                 │                            kubernetes.io/csi/pv/pv-xxx/      │
                 │                            globalmount,                       │
                 │       publish_context={devicePath:/dev/nvme1n1},             │
                 │       volume_capability={mount, ext4, [rw, noatime]})        │
                 │       ↓                                                       │
                 │  driver: mkfs.ext4 if blank disk (only first time!)         │
                 │  driver: mount /dev/nvme1n1 → staging path                  │
                 │       ↓                                                       │
                 │  gRPC: CSI.Node.NodePublishVolume(                           │
                 │       staging_target_path=…/globalmount,                    │
                 │       target_path=/var/lib/kubelet/pods/<podUID>/volumes/   │
                 │                    kubernetes.io~csi/pv-xxx/mount,           │
                 │       volume_capability=…)                                   │
                 │       ↓                                                       │
                 │  driver: bind-mount staging → per-pod target path           │
                 │       ↓                                                       │
                 │  kubelet then bind-mounts the per-pod target into the       │
                 │  container's mount namespace at the volumeMount.mountPath.  │
                 │       ↓                                                       │
                 │  Pod sees /var/lib/app as the volume.                       │
                 └──────────────────────────────────────────────────────────────┘
```

### 6.1 Why Stage AND Publish?

The two-step mount looks redundant. It isn't.

`NodeStageVolume` mounts the device exactly once per node — at a *global path* that is shared by every Pod on this node that uses the same volume. This matters for:
- **ReadOnlyMany / ReadWriteMany**: many Pods on the same node share one device, one filesystem mount, with bind mounts per Pod.
- **Filesystem formatting**: `mkfs` happens once, at stage. If you skipped staging and formatted in publish, two parallel pod starts would race to mkfs the same disk → corruption.
- **Filesystem options**: tunables like `noatime` are set on the global mount; bind mounts inherit them.
- **NodeExpandVolume**: filesystem resize happens against the staging mount, then the bind mounts pick it up live.

`NodePublishVolume` is the lightweight bind mount per Pod. It's safe to call many times in parallel for different Pods — they all bind-mount from the same staged source. It is also the only RPC the driver needs to implement if it has `NodeServiceCapability=STAGE_UNSTAGE_VOLUME` *not* set (rare; mostly for ephemeral inline drivers).

### 6.2 Concrete Filesystem Tree After Three Phases

```
Node node-2, /var/lib/kubelet/:

  plugins/
    kubernetes.io/csi/
      pv/pv-xxx/
        globalmount/             ◀── NodeStageVolume mounted /dev/nvme1n1 here
          lost+found/
          actualdata/

  pods/
    8a1f...../                   ◀── Pod 1 UID
      volumes/kubernetes.io~csi/pv-xxx/
        mount/                   ◀── bind-mount of globalmount/
          lost+found/
          actualdata/

    9b2e...../                   ◀── Pod 2 UID (RWX only)
      volumes/kubernetes.io~csi/pv-xxx/
        mount/                   ◀── another bind-mount of the same globalmount

Inside Pod 1's container, mount table:
  /var/lib/app  ←  bind-mount from .../8a1f.../volumes/.../mount/
```

The same physical disk shows up via three nested mounts. The kubelet creates the final bind into the container; that's a separate `mount(2)` it does itself (not a CSI call).

---

## 7. Pod Termination: Reverse Lifecycle

The teardown order is the strict reverse, except `DeleteVolume` only runs if the PVC is deleted with `reclaimPolicy: Delete`.

```
  Pod deleted (deletionTimestamp set, grace period running)
       │
       ▼
  kubelet stops containers in the Pod
       │
       ▼
  kubelet volume manager: unwire this pod from its volumes
  gRPC: CSI.Node.NodeUnpublishVolume(target_path=<per-pod path>)
       │
       ▼
  driver: umount the per-pod bind mount
       │
       ▼
  (other pods on this node may still use the same PV → stop here for them)
       │
       ▼
  When the LAST pod on this node releases the volume:
  gRPC: CSI.Node.NodeUnstageVolume(staging_target_path=<global mount>)
       │
       ▼
  driver: umount /dev/nvme1n1 from the staging path
       │
       ▼
  attach-detach controller notices: no more pods need this PV on this node
  deletes the VolumeAttachment (or sets deletionTimestamp)
       │
       ▼
  external-attacher: ControllerUnpublishVolume(vol, node)
       │
       ▼
  driver: cloud SDK DetachVolume → hypervisor unplugs disk → /dev/nvme1n1 gone
       │
       ▼
  attacher finishes deleting the VA object

  ────────────────────────────────────────────────────
  ── At this point the PV is back to bound-but-detached. ──
  ── If the user deletes the PVC AND reclaimPolicy=Delete: ──

  PVC deleted → PV moves to Released (claimRef.uid no longer matches)
       │
       ▼
  external-provisioner: PV in Released + reclaim=Delete + my driver?
  gRPC: CSI.Controller.DeleteVolume(volume_id=vol-0123)
       │
       ▼
  driver: cloud SDK DeleteVolume → actually destroys the EBS volume
       │
       ▼
  PV object is deleted
```

### 7.1 The Detach Trap

The detach side has the longest tail of any storage operation. Common causes of stuck detaches:

```
- Pod's process still has the file open after umount fails (busy)
- Kernel has dirty pages it's still flushing
- Multipath/iSCSI: stale path entries pin /dev/dm-*
- Cloud API rate-limited (AWS EBS detach is 6 IOps per second per region)
- Node has died: kubelet can't NodeUnstage, but attach-detach
  controller is conservative and won't force-detach without --node-monitor-grace-period elapsing
```

Force-detach for dead nodes is governed by the attach-detach controller flag `--disable-force-detach-on-timeout` (defaults to false, meaning it WILL force-detach after `--max-bound-attached-volumes` exceeds and `--reconciler-sync-loop-period` cycles report the node unreachable). In practice, a node failure can hold up a StatefulSet pod's rescheduling for 6 minutes (the default unreachable taint-based eviction) + however long the cloud detach takes.

---

## 8. The External Sidecars

The CSI driver does not directly talk to the Kubernetes API. The community-maintained sidecars in `kubernetes-csi/external-*` translate K8s API events into CSI gRPCs. This is deliberate: the driver author writes one gRPC service, ignorant of K8s versions, and gets all the K8s integration for free.

| Sidecar | Source repo | Watches | Calls CSI |
|---|---|---|---|
| `external-provisioner` | `kubernetes-csi/external-provisioner` | PVCs, StorageClasses | CreateVolume, DeleteVolume |
| `external-attacher` | `kubernetes-csi/external-attacher` | VolumeAttachments | ControllerPublishVolume, ControllerUnpublishVolume |
| `external-resizer` | `kubernetes-csi/external-resizer` | PVCs (resize requests) | ControllerExpandVolume |
| `external-snapshotter` | `kubernetes-csi/external-snapshotter` | VolumeSnapshot, VolumeSnapshotContent | CreateSnapshot, DeleteSnapshot |
| `node-driver-registrar` | `kubernetes-csi/node-driver-registrar` | (nothing) — registers driver with kubelet | (none; talks to kubelet plugin watcher) |
| `livenessprobe` | `kubernetes-csi/livenessprobe` | (nothing) — exposes HTTP endpoint | Identity.Probe |

### 8.1 Why They're Separate Containers

Single responsibility: each sidecar has one job, one set of CRDs to watch, one set of CSI calls to issue. They share the same `csi.sock` Unix socket inside the Pod (volume `emptyDir` mounted at `/csi` in both the driver and each sidecar). The driver doesn't import `client-go`. The sidecars don't import any storage SDK. That's the entire architectural insight.

### 8.2 Controller Plugin YAML (Schematic)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ebs-csi-controller
spec:
  replicas: 2
  selector: { matchLabels: { app: ebs-csi-controller } }
  template:
    metadata:
      labels: { app: ebs-csi-controller }
    spec:
      serviceAccountName: ebs-csi-controller-sa
      containers:
        - name: ebs-plugin
          image: public.ecr.aws/ebs-csi-driver/aws-ebs-csi-driver:v1.28.0
          args:
            - --endpoint=unix:///csi/csi.sock
            - --logtostderr
          volumeMounts:
            - name: socket-dir
              mountPath: /csi
          env:
            - name: AWS_REGION
              value: us-east-1
        - name: csi-provisioner
          image: registry.k8s.io/sig-storage/csi-provisioner:v4.0.0
          args:
            - --csi-address=/csi/csi.sock
            - --feature-gates=Topology=true
            - --extra-create-metadata=true
            - --leader-election=true
          volumeMounts:
            - name: socket-dir
              mountPath: /csi
        - name: csi-attacher
          image: registry.k8s.io/sig-storage/csi-attacher:v4.5.0
          args:
            - --csi-address=/csi/csi.sock
            - --leader-election=true
          volumeMounts:
            - name: socket-dir
              mountPath: /csi
        - name: csi-resizer
          image: registry.k8s.io/sig-storage/csi-resizer:v1.10.0
          args: [--csi-address=/csi/csi.sock, --leader-election=true]
          volumeMounts: [{ name: socket-dir, mountPath: /csi }]
        - name: csi-snapshotter
          image: registry.k8s.io/sig-storage/csi-snapshotter:v7.0.0
          args: [--csi-address=/csi/csi.sock, --leader-election=true]
          volumeMounts: [{ name: socket-dir, mountPath: /csi }]
      volumes:
        - name: socket-dir
          emptyDir: {}
```

### 8.3 Node Plugin YAML (Schematic)

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: ebs-csi-node
spec:
  selector: { matchLabels: { app: ebs-csi-node } }
  template:
    metadata: { labels: { app: ebs-csi-node } }
    spec:
      serviceAccountName: ebs-csi-node-sa
      hostNetwork: true
      priorityClassName: system-node-critical
      tolerations:
        - operator: Exists
      containers:
        - name: ebs-plugin
          image: public.ecr.aws/ebs-csi-driver/aws-ebs-csi-driver:v1.28.0
          args:
            - node
            - --endpoint=unix:///csi/csi.sock
          securityContext:
            privileged: true                  # needed for mount(2), iscsiadm
          volumeMounts:
            - name: kubelet-dir
              mountPath: /var/lib/kubelet
              mountPropagation: Bidirectional   # ★ critical
            - name: plugin-dir
              mountPath: /csi
            - name: device-dir
              mountPath: /dev
        - name: node-driver-registrar
          image: registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.10.0
          args:
            - --csi-address=/csi/csi.sock
            - --kubelet-registration-path=/var/lib/kubelet/plugins/ebs.csi.aws.com/csi.sock
          volumeMounts:
            - name: plugin-dir
              mountPath: /csi
            - name: registration-dir
              mountPath: /registration
      volumes:
        - name: kubelet-dir
          hostPath: { path: /var/lib/kubelet, type: Directory }
        - name: plugin-dir
          hostPath: { path: /var/lib/kubelet/plugins/ebs.csi.aws.com/, type: DirectoryOrCreate }
        - name: registration-dir
          hostPath: { path: /var/lib/kubelet/plugins_registry/, type: Directory }
        - name: device-dir
          hostPath: { path: /dev, type: Directory }
```

The `mountPropagation: Bidirectional` on `kubelet-dir` is the single most important field on the entire DaemonSet. Without it, mounts the driver makes in its own mount namespace would not propagate to the host's `/var/lib/kubelet/pods/<uid>/...` paths, and the kubelet wouldn't see them. See §18 for the full mount-propagation taxonomy.

---

## 9. StorageClass: Parameters, Reclaim, BindingMode, Expansion, Topology

The StorageClass is where most production tuning happens. It is also a *cluster-scoped* resource, so the admin owns it; users pick a class by name, but they cannot change its parameters.

### 9.1 Anatomy of a Production StorageClass

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3-encrypted
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
parameters:
  type: gp3                              # cloud-specific
  iops: "3000"
  throughput: "125"
  encrypted: "true"
  kmsKeyId: arn:aws:kms:us-east-1:111:key/abcd
  csi.storage.k8s.io/fstype: ext4         # cross-driver convention
reclaimPolicy: Delete                    # or Retain
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
mountOptions:
  - noatime
  - nodiratime
allowedTopologies:
  - matchLabelExpressions:
      - key: topology.ebs.csi.aws.com/zone
        values: [us-east-1a, us-east-1b]
```

| Field | What it controls | Failure mode if wrong |
|---|---|---|
| `provisioner` | CSI driver name (matches `CSIDriver.metadata.name`) | PVCs stuck Pending — no provisioner watching this class |
| `parameters` | Free-form key/value passed to `CSI.CreateVolume`. Driver-specific. | Cloud API error → PVC has `ProvisioningFailed` event |
| `reclaimPolicy` | `Delete` (default for dynamic) deletes the cloud volume on PVC delete; `Retain` leaves PV in `Released` for manual handling. | Accidental data deletion vs orphaned cloud volumes |
| `volumeBindingMode` | `Immediate` provisions at PVC-create; `WaitForFirstConsumer` waits for the scheduler. | Multi-zone scheduling deadlocks (§3) |
| `allowVolumeExpansion` | Whether `kubectl edit pvc` to grow the size is allowed. | Field is silently rejected if false |
| `mountOptions` | Flags passed through to `mount(8)` for filesystem volumes. | `noatime` is almost always correct |
| `allowedTopologies` | Constraint passed in `accessibility_requirements.requisite` to CSI. | Limits valid zones; useful for multi-region clusters |

### 9.2 Reclaim Policy

```
reclaimPolicy: Delete

  PVC delete → PV moves to Released → external-provisioner deletes cloud volume
                                    → PV object is deleted
  ★ Default for all dynamically provisioned PVs.
  ★ Set on the StorageClass; copied to the PV on creation; can be edited on PV.

reclaimPolicy: Retain

  PVC delete → PV moves to Released → stays there forever, claimRef preserved but stale
                                    → cloud volume is NOT deleted
                                    → admin must manually:
                                        1. Inspect the data on the volume
                                        2. Either delete the PV (cloud volume stays)
                                           and clean up the cloud volume manually,
                                        OR edit the PV: remove claimRef.uid →
                                           PV moves back to Available →
                                           a new PVC with matching constraints can bind to it
                                              (this is "manual recycling" — risky!)
```

The legacy `reclaimPolicy: Recycle` (which did a wipe of the volume on release) is gone. Don't use it; it was removed in 1.31.

---

## 10. Access Modes: RWO / ROX / RWX / RWOP

Access modes are how a PVC says "what kind of concurrent access do I need?" Drivers declare which modes they support via `ValidateVolumeCapabilities`.

| Mode | Abbrev | Meaning | Typical drivers |
|---|---|---|---|
| `ReadWriteOnce` | RWO | One **node** may mount RW. Multiple pods on that node may share. | EBS, GCE PD, Azure Disk, all block storage |
| `ReadOnlyMany` | ROX | Many nodes may mount RO. | Pre-populated snapshots, ConfigMap-like usage |
| `ReadWriteMany` | RWX | Many nodes may mount RW concurrently. | NFS, CephFS, Azure Files, EFS |
| `ReadWriteOncePod` | RWOP | Exactly ONE pod cluster-wide may mount RW. | Block drivers (1.27+ GA) |

### 10.1 The Trap: RWO Is per-Node, not per-Pod

```
PVC accessMode: ReadWriteOnce
Pod A on node-1 mounts it.    ★ allowed
Pod B on node-1 mounts it.    ★ also allowed! (same node)
Pod C on node-2 wants to mount it. ✗ blocked
```

If you needed "only one Pod can write at a time," and you used RWO, you can still get two Pods writing concurrently on the same node — they share the filesystem mount. This is the bug that motivated `ReadWriteOncePod`.

### 10.2 `ReadWriteOncePod` (RWOP)

GA in 1.29. Enforces *true* single-pod access. Implemented at the API server level: when a Pod is admitted with a PVC that's bound to an RWOP PV that's already in use by another Pod, the new Pod fails admission.

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: leader-state
spec:
  accessModes: [ReadWriteOncePod]
  storageClassName: gp3
  resources: { requests: { storage: 10Gi } }
```

Use this for leader-election state, single-writer queues, anything where two-writer semantics would corrupt data.

### 10.3 Mode Compatibility Matrix

```
Driver class           RWO  ROX  RWX  RWOP
─────────────────────  ───  ───  ───  ────
Cloud block (EBS/PD)    ✓    ✗    ✗    ✓
Cloud file (EFS/Files)  ✓    ✓    ✓    ✓ (rare)
NFS / CephFS            ✓    ✓    ✓    ✓
RBD (Ceph block)        ✓    ✗    ✗    ✓
Local PV / TopoLVM       ✓    ✗    ✗    ✓
HostPath                ✓    ✓    ✓    ✓
```

If you request RWX on a block driver, the PVC stays Pending with `failed to provision volume: access mode ReadWriteMany not supported`. Pick a file-based or networked driver.

---

## 11. Volume Snapshots

Snapshots are point-in-time copies of a PVC. The CSI snapshot mechanism, like PV/PVC, splits the user-facing claim from the underlying handle.

### 11.1 The Three Snapshot CRDs

```
                user namespace                        cluster scope
    ┌────────────────────────────────┐    ┌────────────────────────────────┐
    │   VolumeSnapshot (VS)           │───▶│  VolumeSnapshotContent (VSC)   │
    │   - namespaced                  │    │  - cluster-scoped               │
    │   - spec.source.persistentVolume│    │  - spec.driver                  │
    │              ClaimName: data-pvc│    │  - spec.source.snapshotHandle  │
    │   - spec.volumeSnapshotClass    │    │  - spec.deletionPolicy          │
    └────────────────────────────────┘    │  - spec.volumeSnapshotRef       │
                                          └────────────────────────────────┘

                                          ┌────────────────────────────────┐
                                          │  VolumeSnapshotClass            │
                                          │  - cluster-scoped                │
                                          │  - driver: ebs.csi.aws.com      │
                                          │  - deletionPolicy: Delete/Retain│
                                          └────────────────────────────────┘
```

The mapping is exact parallel to PV/PVC:
- VolumeSnapshot ≈ PVC (user-facing request)
- VolumeSnapshotContent ≈ PV (cluster-side handle)
- VolumeSnapshotClass ≈ StorageClass (template)

### 11.2 Create-Snapshot YAML

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotClass
metadata:
  name: ebs-snapshot-class
driver: ebs.csi.aws.com
deletionPolicy: Delete
parameters: {}
---
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: data-snap-2026-05-23
  namespace: app
spec:
  volumeSnapshotClassName: ebs-snapshot-class
  source:
    persistentVolumeClaimName: data-pvc
```

### 11.3 The Snapshot Lifecycle

```
user creates VolumeSnapshot
   │
   ▼
external-snapshotter sidecar (sits next to controller plugin)
   ▼
- Resolves PVC → PV → volume_id
- Calls CSI.Controller.CreateSnapshot(source_volume_id, name)
   ▼
driver: cloud SDK CreateSnapshot → snap-abcd...
   ▼
snapshotter writes a VolumeSnapshotContent object with snapshotHandle=snap-abcd
sets VolumeSnapshot.status.readyToUse=true once cloud reports the snapshot is done
```

Crucially, `CreateSnapshot` is *non-blocking* in CSI — the driver returns immediately with `ready_to_use=false` if the cloud snapshot is still completing in the background. The snapshotter then polls with `ListSnapshots` until it's ready.

### 11.4 Restore from Snapshot

To create a new PVC from a snapshot:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data-restored
  namespace: app
spec:
  storageClassName: gp3
  accessModes: [ReadWriteOnce]
  resources: { requests: { storage: 50Gi } }
  dataSource:
    name: data-snap-2026-05-23
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
```

The external-provisioner sees `dataSource` and calls `CreateVolume` with `volume_content_source.snapshot.snapshot_id=snap-abcd` — the driver knows to create the new volume as a clone of the snapshot.

### 11.5 Cross-Namespace Restore

`VolumeSnapshot.spec.source.persistentVolumeClaimName` only references PVCs in the same namespace. For cross-namespace restore (e.g., copy prod-db snapshot into staging), use `dataSourceRef` (1.24+) and `ReferenceGrant` from `gateway.networking.k8s.io`:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: from-prod-snap
  namespace: staging
spec:
  dataSourceRef:
    apiGroup: snapshot.storage.k8s.io
    kind: VolumeSnapshot
    name: nightly-snap
    namespace: prod                    # ★ different namespace
  storageClassName: gp3
  accessModes: [ReadWriteOnce]
  resources: { requests: { storage: 50Gi } }
---
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  name: allow-staging-restore
  namespace: prod
spec:
  from:
    - group: ""
      kind: PersistentVolumeClaim
      namespace: staging
  to:
    - group: snapshot.storage.k8s.io
      kind: VolumeSnapshot
```

### 11.6 Pre-Provisioned (Static) VolumeSnapshotContent

If you already have a cloud-side snapshot (say, from a manual `aws ec2 create-snapshot`) and want K8s to pick it up:

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotContent
metadata:
  name: pre-existing-snap
spec:
  deletionPolicy: Retain
  driver: ebs.csi.aws.com
  source:
    snapshotHandle: snap-0123456789abcdef0
  sourceVolumeMode: Filesystem
  volumeSnapshotRef:
    name: pre-existing-snap-bind
    namespace: app
---
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: pre-existing-snap-bind
  namespace: app
spec:
  source:
    volumeSnapshotContentName: pre-existing-snap
```

---

## 12. Volume Expansion: Online vs Offline

A PVC can be grown by editing `spec.resources.requests.storage`. The mechanics involve both the controller and node sides.

### 12.1 The Two-Step Expansion

```
user edits PVC: storage 50Gi → 100Gi
       │
       ▼
external-resizer sees the request (PVC.spec.resources != PVC.status.capacity)
calls CSI.Controller.ControllerExpandVolume(volume_id, capacity=100Gi)
       │
       ▼
driver: cloud SDK ModifyVolume(vol-0123, size=100GB)
hardware: cloud grows the block device
       │
       ▼
resizer patches PV.spec.capacity = 100Gi
sets a condition on PVC: FileSystemResizePending
       │
       ▼
       ★ STEP 2: filesystem resize
       │
       ▼
   IF online expansion (the default; driver reports `EXPAND_VOLUME` capability):
       kubelet on the node sees FileSystemResizePending
       calls CSI.Node.NodeExpandVolume(volume_path=<global mount>, capacity=100Gi)
       driver: resize2fs / xfs_growfs on the mounted filesystem
       resizer clears the FileSystemResizePending condition
       PVC.status.capacity is updated
       ★ Pod did NOT restart; mount is hot-expanded.
       │
   IF offline expansion (driver doesn't support online):
       resizer marks the PVC condition saying "restart the pod"
       Pod must be deleted and recreated to trigger NodeExpandVolume during NodeStage
```

### 12.2 The Three Capability Flags

A driver advertises expansion capability in `ControllerGetCapabilities`:
- `EXPAND_VOLUME` on the Controller service → driver can grow the underlying storage.
- `EXPAND_VOLUME` on the Node service → driver can grow the filesystem online.

Combinations:

| Controller `EXPAND_VOLUME` | Node `EXPAND_VOLUME` | Behavior |
|---|---|---|
| Yes | Yes | Full online expansion |
| Yes | No | Offline expansion (block grows, filesystem grows only on next mount = pod restart) |
| No | No | Driver doesn't support expansion; PVC edit is rejected |

### 12.3 YAML to Expand

```bash
kubectl patch pvc data-pvc -n app \
  --type=merge -p '{"spec":{"resources":{"requests":{"storage":"100Gi"}}}}'
```

Or just `kubectl edit pvc data-pvc` and update `spec.resources.requests.storage`.

The PVC's StorageClass must have `allowVolumeExpansion: true`; otherwise the patch is rejected with a validating-admission error.

### 12.4 Pitfall: Shrinkage

PVCs cannot shrink. The API validation rejects `spec.resources.requests.storage` lower than current. To "shrink," you snapshot, restore into a smaller PVC, swap.

---

## 13. Ephemeral Volumes

Two kinds, both pod-scoped:

### 13.1 Generic Ephemeral Volumes

A full PVC, but its lifecycle is tied to the Pod. Created inline in the Pod spec; the PV-binding/CSI machinery is otherwise identical.

```yaml
apiVersion: v1
kind: Pod
metadata: { name: scratch-job }
spec:
  containers:
    - name: worker
      image: worker:1.0
      volumeMounts:
        - name: scratch
          mountPath: /scratch
  volumes:
    - name: scratch
      ephemeral:
        volumeClaimTemplate:
          metadata:
            labels: { purpose: scratch }
          spec:
            accessModes: [ReadWriteOnce]
            storageClassName: gp3
            resources: { requests: { storage: 20Gi } }
```

What happens: a PVC named `<pod-name>-scratch` is auto-created with an ownerRef to the Pod. When the Pod is deleted, the GC controller deletes the PVC, which (if reclaim=Delete) deletes the PV and the cloud volume. This is the cloud-block-storage equivalent of an `emptyDir` with persistence guarantees during the pod's life.

### 13.2 CSI Inline Ephemeral Volumes

A more specialized form: the driver itself implements `NodePublishVolume` without any controller-side `CreateVolume`. Useful for drivers that synthesize content (secrets, configuration, ephemeral encryption keys) rather than allocating persistent storage. The canonical example is the **secrets-store-csi-driver**.

```yaml
volumes:
  - name: secrets-store
    csi:
      driver: secrets-store.csi.k8s.io
      readOnly: true
      volumeAttributes:
        secretProviderClass: my-secrets
```

The `CSIDriver` resource must have `volumeLifecycleModes: [Ephemeral]` for this to be allowed. The driver receives the entire `volumeAttributes` map at `NodePublishVolume` time and synthesizes the directory contents on the fly.

### 13.3 When Not to Use Ephemeral

Generic ephemeral is fine for scratch space. Do not use it for stateful databases — if the Pod is rescheduled, the PVC is deleted, and the data is gone. Use a StatefulSet with `volumeClaimTemplates` (chapter 13) for any data you want to outlive the pod.

---

## 14. Raw Block Volumes (`volumeMode: Block`)

By default, PVs are mounted as filesystems (`volumeMode: Filesystem`, the default). For workloads that want to manage their own storage layout (databases doing direct I/O, software-defined storage running atop K8s, anything that needs `O_DIRECT`-aligned access), Kubernetes can expose the raw block device.

### 14.1 YAML

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: raw-block-pvc }
spec:
  accessModes: [ReadWriteOnce]
  volumeMode: Block                  # ★
  storageClassName: gp3
  resources: { requests: { storage: 100Gi } }
---
apiVersion: v1
kind: Pod
metadata: { name: db }
spec:
  containers:
    - name: db
      image: db:1.0
      volumeDevices:                 # ← not volumeMounts!
        - name: data
          devicePath: /dev/xvda      # block device path inside container
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: raw-block-pvc
```

### 14.2 What's Different on the Driver Side

`NodeStageVolume` is called with `volume_capability.access_type = block` rather than `mount`. The driver does NOT format. The driver does NOT mount. It only ensures the device is visible at `staging_target_path` (often as a symlink to `/dev/nvme1n1`). `NodePublishVolume` bind-mounts that device node into the per-pod path. The container sees a real block device.

### 14.3 Use Cases

- Databases with their own block-level format (e.g., MongoDB WiredTiger with direct I/O, Cassandra).
- Software-defined storage stacks (Rook-Ceph OSDs, Longhorn engines).
- Multipath aggregation.
- Performance: skips the filesystem layer's metadata overhead.

---

## 15. CSI Migration: In-Tree → Out-of-Tree

Before CSI, every storage backend (`awsElasticBlockStore`, `gcePersistentDisk`, `azureDisk`, `cinder`, ...) had its code compiled into `kube-controller-manager` and `kubelet`. This created a versioning nightmare (a Ceph bug forced a K8s release) and locked storage vendors out of independent development cycles.

The CSI migration project (KEP-625) moved every in-tree plugin out-of-tree. As of 1.30, **all in-tree cloud volume plugins are removed**. The "migration" feature gates intercept legacy PV specs (like `awsElasticBlockStore`) and reroute them transparently to the corresponding CSI driver.

```
Pre-migration:                       Post-migration (the only path now):

PV spec:                             PV spec:
  awsElasticBlockStore:                csi:
    volumeID: vol-0123                   driver: ebs.csi.aws.com
    fsType: ext4                         volumeHandle: vol-0123
                                         fsType: ext4

Code in kube-controller-manager      External: aws-ebs-csi-driver Deployment
calls AWS SDK directly               + sidecars, communicates via csi.sock
```

### 15.1 The Migration Feature Gates (historical reference)

- `InTreePluginAWSUnregister` (GA → locked on in 1.31) — refuses to handle in-tree AWS specs.
- `CSIMigrationAWS` (GA, on by default) — translates in-tree specs into CSI calls for compatibility.
- Similar gates for GCE, Azure, OpenStack, vSphere.

The user-visible effect: nothing, as long as the CSI driver is installed. If you upgrade to 1.30 without installing the corresponding CSI driver first, every PV stops working.

### 15.2 The `csi-translation-lib`

`k8s.io/csi-translation-lib` is the shim. The PV controller, attach-detach controller, and kubelet volume manager all call into it when they see a legacy in-tree PV spec. It translates the in-tree fields to a synthetic CSI PV in memory, then proceeds normally. Old YAML still applies, new behavior under the hood.

---

## 16. The Driver Zoo: Cloud, Networked, Local

A non-exhaustive map of what's out there.

### 16.1 Cloud Block

| Driver | Backend | Access modes | Notes |
|---|---|---|---|
| `ebs.csi.aws.com` | AWS EBS | RWO, RWOP | gp3 is default; io2 for high IOPS; one volume = one AZ |
| `pd.csi.storage.gke.io` | GCE PD | RWO, RWOP | Regional PD (`pd-balanced` zonal, `regional-pd-balanced` cross-zone replicated) |
| `disk.csi.azure.com` | Azure Managed Disk | RWO, RWOP | Premium SSD v2 for high IOPS; LRS vs ZRS for zone-redundancy |

### 16.2 Cloud File / NAS

| Driver | Backend | Access modes |
|---|---|---|
| `efs.csi.aws.com` | AWS EFS (NFS) | RWO, ROX, RWX |
| `filestore.csi.storage.gke.io` | GCP Filestore (NFS) | RWO, ROX, RWX |
| `file.csi.azure.com` | Azure Files (SMB or NFS) | RWO, ROX, RWX |

### 16.3 Networked / On-Prem

| Driver | Backend |
|---|---|
| `nfs.csi.k8s.io` | Generic NFS server |
| `cephfs.csi.ceph.com` | CephFS (RWX file) |
| `rbd.csi.ceph.com` | Ceph RBD (block) |
| `iscsi.csi.k8s.io` | iSCSI targets |
| `portworx-csi` | Portworx (HCI overlay) |
| `driver.longhorn.io` | Longhorn (Rancher's HCI block) |
| `rook-ceph.rook.io/block` | Rook-managed Ceph |

### 16.4 Local

| Driver | Strategy |
|---|---|
| `kubernetes.io/local-volume` (in-tree, only one left) | Pre-provisioned static PVs on a node-local path. No dynamic provisioning. |
| `topolvm.cybozu.com` (TopoLVM) | Dynamic LVM-based local PVs. Driver carves out LVs from a host VG. |
| `openebs.io/local` | OpenEBS LocalPV variants (hostpath, device, lvm, zfs) |
| `directpv.min.io` | DirectPV: each disk = one PV, no LVM |

Local drivers always pin a PV to a single node via `nodeAffinity`; the scheduler must place the Pod on that node.

---

## 17. Topology-Aware Provisioning

The mechanics that connect scheduling decisions to volume placement.

### 17.1 The Topology Keys

Each driver advertises topology keys it cares about — usually the AZ label, sometimes rack/region/host. A driver implements `NodeGetInfo` which returns:

```
{
  "node_id": "i-0123abcd",
  "max_volumes_per_node": 26,
  "accessible_topology": {
    "topology.ebs.csi.aws.com/zone": "us-east-1a"
  }
}
```

The node-driver-registrar writes these labels into the `CSINode` resource and onto the Node itself (if not already present).

### 17.2 The Flow

```
1. Pod scheduling decision in progress.
2. Scheduler's VolumeBinding plugin (PreFilter):
     For each PVC in the Pod:
       If already bound, fetch PV's nodeAffinity → constrains node selection.
       If unbound + WaitForFirstConsumer:
         Note that we'll need to provision; defer to ReserveVolume hook.
3. Filter phase: nodes not matching all PVs' nodeAffinity are filtered out.
4. Scoring: balance volume-locality vs CPU/memory/etc.
5. Node selected (e.g., node-2 in us-east-1b).
6. VolumeBinding plugin (PreBind):
     For each unbound PVC, set the annotation
       volume.kubernetes.io/selected-node: node-2
7. external-provisioner sees the annotation, calls CreateVolume with
     accessibility_requirements:
       requisite: [{ topology.ebs.csi.aws.com/zone: us-east-1b }]
       preferred: [{ topology.ebs.csi.aws.com/zone: us-east-1b }]
8. Driver creates volume in us-east-1b.
9. PV is created with nodeAffinity us-east-1b.
10. Pod is bound to node-2.
11. Lifecycle continues (attach, mount).
```

### 17.3 `allowedTopologies`

A constraint on top of the driver's natural topology. Use this when you want to forbid certain AZs (e.g., one zone is being deprecated):

```yaml
allowedTopologies:
  - matchLabelExpressions:
      - key: topology.ebs.csi.aws.com/zone
        values: [us-east-1a, us-east-1b]   # exclude 1c
```

### 17.4 Multi-PVC Pods and Topology

A Pod with two PVCs creates a topology-coupling problem: both volumes must be in the same zone (otherwise neither can be attached to the same node). The scheduler's `VolumeBinding` plugin handles this by treating the set of unbound PVCs as a co-located group.

```
Pod spec.volumes: [pvc-A, pvc-B]
   ↓
Scheduler PreFilter:
   - Find a node where:
       * pvc-A can be (re)bound: either already bound + nodeAffinity matches,
         or unbound + the SC can provision in this node's zone
       * pvc-B can be (re)bound: same conditions
   - Reject nodes that can't satisfy BOTH simultaneously.
   ↓
Scheduler PreBind:
   - For each unbound PVC, annotate volume.kubernetes.io/selected-node
   - Provisioners create both volumes in the chosen zone.
```

The implementation lives in `pkg/scheduler/framework/plugins/volumebinding`. It is a Filter+Reserve plugin (it temporarily reserves nodes for the binding decision and then commits in PreBind).

### 17.5 `CSIStorageCapacity`: Capacity-Aware Scheduling

For local-storage drivers (TopoLVM, OpenEBS LocalPV), the scheduler needs to know which nodes have *available* capacity, not just "are topologically valid." Driver publishes `CSIStorageCapacity` objects:

```yaml
apiVersion: storage.k8s.io/v1
kind: CSIStorageCapacity
metadata:
  name: csi-capacity-node-2-fast
  namespace: kube-system
storageClassName: local-fast
capacity: 800Gi              # bytes available right now on this node
maximumVolumeSize: 400Gi
nodeTopology:
  matchLabels:
    kubernetes.io/hostname: node-2
```

When `CSIDriver.spec.storageCapacity: true`, the scheduler reads these and filters out nodes that don't have enough free space. This avoids the classic "pod scheduled to a node where local-storage is already full" failure.

---

## 18. Mount Propagation

`mountPropagation` controls how mounts inside a container relate to mounts on the host (and vice versa). It's set per-volume-mount, and it follows the Linux kernel's mount-propagation semantics (`shared`, `slave`, `private`).

### 18.1 The Three Modes

```
            HOST mount table          CONTAINER mount table
            ────────────────          ──────────────────────
None        host mounts are not       container mounts are not
            visible in container      visible in host
            (the default; private)    (the default; private)

HostToContainer
            New host mounts under     container mounts NOT
            the path become visible   visible in host
            inside the container

Bidirectional
            Container mounts under    Host mounts under the path
            the path are propagated   are visible to container
            to the host (and from
            the host to all other
            containers with shared
            propagation)
```

### 18.2 Why CSI Node Plugins Need `Bidirectional`

The CSI node driver runs in its own Pod, in its own mount namespace. When it calls `mount(2)` to bind-mount a staged volume to `/var/lib/kubelet/pods/<uid>/...`, that mount must be visible to:
- The kubelet, which later bind-mounts it into the container.
- Any other Pod's container if the volume is shared (RWX).

Bidirectional propagation makes the host's `/var/lib/kubelet` a shared mount point. The driver's mounts flow upward to the host, and from there into every other container that has a `HostToContainer` view of that subtree.

```yaml
volumeMounts:
  - name: kubelet-dir
    mountPath: /var/lib/kubelet
    mountPropagation: Bidirectional      # ★ MUST for CSI node drivers
```

### 18.3 Why You Should Never Set `Bidirectional` on a User Workload

Bidirectional gives the container the ability to mount things on the host. That's effectively container-escape primitives. PodSecurityAdmission's `restricted` profile blocks it. Only privileged system components (CSI drivers, monitoring agents that need to read other containers' filesystems) should set it.

### 18.4 The Default Trap

The default is `None`. This means if you have, say, a Filebeat DaemonSet that bind-mounts `/var/lib/docker/containers` and you start a new pod after Filebeat is running, Filebeat won't see the new pod's logs — because the new pod's container-log mount didn't propagate. Solution: set `mountPropagation: HostToContainer` on the Filebeat side so it picks up new mounts dynamically.

### 18.5 Linux Kernel Background

Underneath, Kubernetes is just exposing the kernel's mount-propagation flags:

```
MS_PRIVATE   ←  mountPropagation: None
MS_SLAVE     ←  mountPropagation: HostToContainer
MS_SHARED    ←  mountPropagation: Bidirectional
```

These are set on the mount namespace at mount-creation time via `mount --make-shared` etc. The kernel propagates mount/umount events between peer mounts in the same "peer group." A `MS_SHARED` peer group is bidirectional; `MS_SLAVE` is a slave to one upstream master, getting events from it but not pushing to it; `MS_PRIVATE` is isolated.

The reason `Bidirectional` requires `privileged: true` on the container is that creating a shared mount requires `CAP_SYS_ADMIN` to issue the `mount(2)` syscall with `MS_REC | MS_SHARED`. Restricted pods cannot do this.

For background, see `man 7 mount_namespaces` and the linked propagation documentation.

---

## 19. Performance: IOPS, Throughput, Latency Tax

The CSI layer adds two kinds of latency: setup latency (attach + mount) and runtime latency (the filesystem and block layer between the container and the actual storage).

### 19.1 Setup Latency Budgets (Empirical, AWS as example)

| Phase | Typical | Pathological |
|---|---|---|
| `CreateVolume` (EBS gp3, 100GB) | 1.5–3s | 15s when EBS API rate-limited |
| `ControllerPublishVolume` (attach) | 3–8s | 30s on busy hypervisor |
| Device appears in `/dev/nvme*` after attach | 2–6s | up to 30s if `udev` is slow |
| `NodeStageVolume` mkfs (ext4, 100GB) | 2–4s | 30s if disk is HDD |
| `NodeStageVolume` mount | <100ms | seconds on FUSE drivers |
| `NodePublishVolume` bind-mount | <50ms | (negligible) |
| **Total cold-start** | **~10–20s** | **60–120s** |

For a StatefulSet with 5 pods × `OrderedReady` policy, this means a ~1-minute startup tail per pod, 5 minutes total. Use `Parallel` pod management (chapter 13) if you can.

### 19.2 Runtime Performance: Driver Comparison

```
EBS gp3 baseline:           3,000 IOPS / 125 MB/s; max 16,000 IOPS / 1000 MB/s
EBS io2 Block Express:      up to 256,000 IOPS, sub-ms latency
GCE pd-balanced:            ~6,000 IOPS at 1TB; latency 1-3ms
GCE pd-extreme:             up to 120,000 IOPS, sub-ms
Azure Premium SSD v2:       up to 80,000 IOPS, sub-ms
Local NVMe (TopoLVM):       500,000+ IOPS, microsecond latency, NO replication
NFS over 10GbE:             10,000–30,000 IOPS shared, single-digit-ms latency
EFS (provisioned throughput): ~10s of MB/s per pod, 5-10ms latency
CephFS / RBD over RoCE:     50,000+ IOPS, 1-2ms latency, with replication
```

### 19.3 The CSI Latency Tax

Compared to a direct host mount, CSI adds:
- One gRPC roundtrip for each Stage / Publish / Unpublish / Unstage call (microseconds; trivial).
- A bind-mount layer (negligible).
- The `mountPropagation: Bidirectional` setup is free at runtime.

In other words: the runtime performance is the same as raw mounting. The cost is at *setup*, not at I/O time. So a database doing 100,000 IOPS sees no difference whether the disk was attached via in-tree code or via CSI; it sees a 10-second wait at pod creation.

### 19.4 The Filesystem Choice

| Filesystem | Strengths | Weaknesses |
|---|---|---|
| `ext4` | Most mature, fastest mkfs, good general performance | Limited online-resize semantics on shrink; max 16TB without `meta_bg` |
| `xfs` | Better for large files, faster growfs, better with high concurrency | Cannot shrink at all (online or offline) |
| `btrfs` | Snapshots, COW, checksums | Slower, harder to recover |
| `zfs` | Snapshots, checksums, compression | Out-of-tree; license issues |

Default for most cloud drivers: `ext4`. Override via `csi.storage.k8s.io/fstype: xfs` in StorageClass parameters.

---

## 20. Backup Integration with Velero

Velero (chapter 32 covers it in depth) is the standard backup tool. It hooks into CSI snapshots for the data plane.

### 20.1 Two Backup Strategies

**CSI snapshot-based**: Velero creates a VolumeSnapshot for each PVC in the backup, exports the snapshot to object storage as a reference (or the data via `DataMover`), restores by creating new PVCs with `dataSource: VolumeSnapshot`.

**File-system-based (restic / kopia)**: Velero spawns a sidecar that walks the volume's filesystem and uploads files to object storage. Slower, but driver-agnostic, and works across clusters/clouds.

### 20.2 Volume Backup YAML (Velero)

```yaml
apiVersion: velero.io/v1
kind: Backup
metadata:
  name: nightly-2026-05-23
  namespace: velero
spec:
  includedNamespaces: [app]
  storageLocation: default
  volumeSnapshotLocations: [aws-us-east-1]
  snapshotMoveData: true            # use DataMover to copy data to object storage
  defaultVolumesToFsBackup: false   # prefer CSI snapshot over restic
  ttl: 720h0m0s                     # 30-day retention
```

### 20.3 The Quiescence Problem

A snapshot taken without application-level quiescence is *crash-consistent* (equivalent to pulling the power plug), not *application-consistent* (equivalent to a clean shutdown). For Postgres, MySQL, etcd — most databases — crash-consistent snapshots replay the WAL on restore and usually come back. For applications that buffer in userspace without fsync semantics (some message queues, some caches), they don't.

Velero supports pre/post hooks (which run `exec` in a container) to quiesce:

```yaml
metadata:
  annotations:
    pre.hook.backup.velero.io/container: postgres
    pre.hook.backup.velero.io/command: '["/bin/bash","-c","psql -c CHECKPOINT"]'
    post.hook.backup.velero.io/container: postgres
    post.hook.backup.velero.io/command: '["/bin/bash","-c","true"]'
```

For databases, a CSI snapshot of an `xfs` filesystem after a `CHECKPOINT` is generally safe. For LVM-backed volumes (TopoLVM), the snapshot is filesystem-level COW and can be application-quiesced via the pre-hook.

---

## 21. `ReadWriteOncePod`: The Real Mutex

Recap from §10 with implementation detail.

### 21.1 Why It Exists

`ReadWriteOnce` enforces one *node*. Many workloads need one *pod*: leader election state files, single-writer queue heads, anything that uses file locks for mutual exclusion (the kernel's `flock` doesn't work across pods even on the same node if they're in different mount namespaces? — actually it does, but only via the underlying inode, which works fine — the issue is logical, not kernel: most code assumes RWO means "one writer").

### 21.2 Where It's Enforced

The kube-apiserver's PVC admission validates this. When a Pod is admitted, the apiserver scans its `spec.volumes`, finds RWOP PVCs, and checks the PV's `claimRef` and any in-use VolumeAttachments. If another Pod is already using the volume, the new Pod is rejected.

This was the major behavioral change in 1.27 (beta) → 1.29 (GA). Before RWOP, "single-writer" was an honor system.

### 21.3 Driver Requirements

The driver must advertise `RWOP` support via the `accessModes` in `ValidateVolumeCapabilities`. Most modern block drivers do. NFS-style drivers can also support it, but it's less useful there (you typically want RWX).

---

## 22. Pre-Provisioned PVs

The static-provisioning workflow.

### 22.1 When to Use It

- Importing an existing cloud volume into K8s (e.g., a manually-created EBS volume from a migration).
- Restoring from a backup taken outside CSI snapshots (e.g., a manual `aws ec2 create-snapshot` + new volume).
- Legacy on-prem storage where the admin allocates volumes out-of-band.
- "Adoption" workflows: importing a Released PV that was kept under `Retain`.

### 22.2 The YAML

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: imported-data-v1
  annotations:
    pv.kubernetes.io/provisioned-by: ebs.csi.aws.com   # tells the system this is CSI-managed
spec:
  capacity: { storage: 100Gi }
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""                                  # no SC
  csi:
    driver: ebs.csi.aws.com
    volumeHandle: vol-0123456789abcdef0                 # the real cloud volume
    fsType: ext4
    volumeAttributes:
      storage.kubernetes.io/csiProvisionerIdentity: imported
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: topology.ebs.csi.aws.com/zone
              operator: In
              values: [us-east-1a]                       # MUST match the cloud volume's AZ
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: imported-data-claim
  namespace: app
spec:
  storageClassName: ""                                   # empty: no dynamic provisioning
  volumeName: imported-data-v1                           # ★ direct pin
  accessModes: [ReadWriteOnce]
  resources: { requests: { storage: 100Gi } }
```

The `storageClassName: ""` (empty string, not unset!) prevents the binder from looking for a dynamic provisioner. The explicit `volumeName` short-circuits the matching loop.

---

## 23. Reclaim Policy and Accidental Delete Recovery

The most common storage incident: someone deletes a PVC, the PV goes `Released`, and (if reclaim was `Delete`) the cloud volume is gone seconds later.

### 23.1 The Defensive Setup

Set `reclaimPolicy: Retain` on every StorageClass for stateful workloads. The cost is orphaned cloud volumes when PVCs are legitimately deleted (you need a script to clean them up). The benefit is that "kubectl delete pvc" doesn't immediately become "kubectl delete data."

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata: { name: gp3-retain }
provisioner: ebs.csi.aws.com
reclaimPolicy: Retain
parameters: { type: gp3 }
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
```

### 23.2 Recovery After Accidental PVC Delete

If reclaim was `Delete`:

```
PVC deleted → PV → Released → external-provisioner sees Released
                            → calls DeleteVolume
                            → cloud volume is gone
                            → PV is deleted
```

Race window: seconds. Once `DeleteVolume` returns success, the data is unrecoverable (unless the cloud has its own snapshots or recycle bin: AWS Recycle Bin, GCP undelete, etc. — but these aren't default).

If reclaim was `Retain`:

```
PVC deleted → PV → Released → STAYS HERE forever
                            → cloud volume still exists
                            → admin can:
                              1. kubectl get pv  → finds the Released PV
                              2. kubectl edit pv pv-xxx → remove .spec.claimRef.uid
                                                       → PV moves to Available
                              3. Create a new PVC with same name OR
                                 with .spec.volumeName: pv-xxx
                              4. PV rebinds, data is back
```

### 23.3 The Even-Safer Pattern: PVC Finalizers

Add a custom finalizer to PVCs you really want to protect:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: critical-data
  finalizers:
    - mycompany.io/require-manual-delete
spec: ...
```

`kubectl delete pvc critical-data` will hang on `deletionTimestamp` set + finalizer present, until a human removes the finalizer. Pair with PSA/Kyverno to refuse PVC creates without this finalizer for namespaces marked `data-tier=prod`.

---

## 24. The PVC Protection Finalizer

There's a built-in mechanism to prevent PVC deletion *while a Pod is using it*.

```
At PVC creation, the apiserver adds:
  finalizers:
    - kubernetes.io/pvc-protection

kube-controller-manager's "pvc-protection-controller" watches PVCs.

When a PVC is deleted (deletionTimestamp set):
  Loop: is any Pod referencing this PVC?
    yes → keep the finalizer; PVC stays in Terminating state
    no  → remove the finalizer → PVC is GC'd by apiserver → cascade to PV
```

This is why `kubectl delete pvc` often hangs ("Terminating") until you also delete the consuming Pods. It's a feature, not a bug — it prevents the PV from being deleted while a Pod is still mounting it (which would leave the kubelet's mount manager confused).

The PV has a parallel `kubernetes.io/pv-protection` finalizer that prevents PV deletion while it's `Bound`.

```
PV protection states:
  PV Bound      → finalizer held       → cannot delete
  PV Released   → finalizer released   → can delete (if reclaim=Delete, controller does it)
```

---

## 25. Writing a CSI Driver

The mechanics of building one yourself. Useful both as understanding and for the case where you do need a custom driver (e.g., for in-house storage hardware).

### 25.1 Minimum Implementation

A "hello world" CSI driver must implement:
- All three Identity RPCs (`GetPluginInfo`, `GetPluginCapabilities`, `Probe`).
- Either `CreateVolume`+`DeleteVolume` (for dynamic provisioning) or none (static-only).
- Either `ControllerPublishVolume`+`ControllerUnpublishVolume` (for attachable) or set `attachRequired: false` on `CSIDriver`.
- On the node side: `NodePublishVolume`+`NodeUnpublishVolume` (minimum) and `NodeGetCapabilities`+`NodeGetInfo`.

### 25.2 The CSIDriver Resource

```yaml
apiVersion: storage.k8s.io/v1
kind: CSIDriver
metadata:
  name: ebs.csi.aws.com                # MUST match the driver's GetPluginInfo
spec:
  attachRequired: true                  # if false, skip VolumeAttachment + ControllerPublish
  podInfoOnMount: true                  # pass pod.namespace/pod.name/pod.uid to NodePublishVolume
  fsGroupPolicy: File                   # how to handle pod.spec.securityContext.fsGroup
  volumeLifecycleModes:                 # Persistent (PV-backed) and/or Ephemeral (inline CSI)
    - Persistent
  storageCapacity: true                 # driver reports CSIStorageCapacity objects
  requiresRepublish: false              # don't periodically re-NodePublish (set true for secrets driver)
  seLinuxMount: true                    # 1.27+: pass mount-time SELinux label, no relabel walk
```

### 25.3 Scaffolding

The Go ecosystem has `kubernetes-csi/csi-driver-host-path` as a reference implementation. It's a hostpath-backed driver — useful only for tests, but it's the canonical "minimum readable CSI driver."

```
github.com/kubernetes-csi/csi-driver-host-path/
├── cmd/hostpathplugin/main.go     ← entry point, picks controller vs node mode
├── pkg/hostpath/
│   ├── identityserver.go          ← Identity RPCs
│   ├── controllerserver.go        ← Controller RPCs
│   ├── nodeserver.go              ← Node RPCs
│   ├── hostpath.go                ← in-memory state, hostpath operations
│   └── snapshotter.go             ← snapshot logic
```

### 25.4 Testing: `csi-test`

The conformance test suite at `kubernetes-csi/csi-test` exercises every CSI RPC against your driver via a sanity client. It runs as a Go test binary:

```bash
csi-sanity --csi.endpoint=/tmp/csi.sock \
           --csi.testvolumesize=1073741824 \
           --csi.testvolumeparameters=/tmp/params.yaml
```

It validates idempotency, error codes, parameter handling, and the full lifecycle. A driver that passes `csi-sanity` is roughly conformant.

### 25.5 End-to-End Tests

The Kubernetes test/e2e/storage suite has a CSI driver test harness. By implementing the `TestDriver` interface in your test repo, you can run the full e2e storage suite against your driver in a real cluster, validating PV binding, dynamic provisioning, snapshots, expansion, etc.

### 25.6 A Walkthrough: Implementing `CreateVolume`

Concretely, here's what the controller-side `CreateVolume` handler looks like in a real driver. This is paraphrased from `kubernetes-csi/csi-driver-host-path` and the EBS driver:

```go
func (c *Controller) CreateVolume(ctx context.Context, req *csi.CreateVolumeRequest) (*csi.CreateVolumeResponse, error) {
    // 1. Validate the request.
    if req.GetName() == "" {
        return nil, status.Error(codes.InvalidArgument, "Name missing in request")
    }
    if req.GetVolumeCapabilities() == nil {
        return nil, status.Error(codes.InvalidArgument, "VolumeCapabilities missing")
    }

    // 2. Check capacity range.
    capacity := req.GetCapacityRange().GetRequiredBytes()
    if capacity < c.minVolumeSize || capacity > c.maxVolumeSize {
        return nil, status.Errorf(codes.OutOfRange, "Capacity %d out of range", capacity)
    }

    // 3. Idempotency: have we seen this name before?
    if existing := c.lookupByName(req.GetName()); existing != nil {
        // Check capability + capacity match. If they DON'T match → ALREADY_EXISTS.
        if !capabilitiesMatch(existing.caps, req.VolumeCapabilities) {
            return nil, status.Errorf(codes.AlreadyExists,
                "Volume %s exists with different capabilities", req.GetName())
        }
        if existing.capacity != capacity {
            return nil, status.Errorf(codes.AlreadyExists,
                "Volume %s exists with different size", req.GetName())
        }
        // Same name, same params → return the existing volume.
        return &csi.CreateVolumeResponse{Volume: existing.toCSI()}, nil
    }

    // 4. Resolve topology — which AZ should this volume be in?
    zone := ""
    for _, top := range req.GetAccessibilityRequirements().GetPreferred() {
        if z, ok := top.GetSegments()["topology.ebs.csi.aws.com/zone"]; ok {
            zone = z
            break
        }
    }
    if zone == "" {
        return nil, status.Error(codes.InvalidArgument, "No zone topology in request")
    }

    // 5. Call the cloud API.
    params := req.GetParameters()
    cloudVolID, err := c.cloud.CreateVolume(ctx, cloud.CreateVolumeInput{
        Name:          req.GetName(),         // used as the cloud-side idempotency token
        SizeBytes:     capacity,
        Zone:          zone,
        VolumeType:    params["type"],        // "gp3", "io2", ...
        IOPS:          parseInt(params["iops"]),
        Throughput:    parseInt(params["throughput"]),
        Encrypted:     params["encrypted"] == "true",
        KMSKeyID:      params["kmsKeyId"],
    })
    if err != nil {
        return nil, mapCloudError(err)        // translates AWS errors to CSI codes
    }

    // 6. Persist driver-side bookkeeping (in memory or a backing store).
    c.recordVolume(req.GetName(), cloudVolID, capacity, req.VolumeCapabilities, zone)

    // 7. Return the response.
    return &csi.CreateVolumeResponse{
        Volume: &csi.Volume{
            VolumeId:      cloudVolID,
            CapacityBytes: capacity,
            VolumeContext: map[string]string{
                "storage.kubernetes.io/csiProvisionerIdentity": c.identity,
            },
            AccessibleTopology: []*csi.Topology{
                {Segments: map[string]string{"topology.ebs.csi.aws.com/zone": zone}},
            },
            ContentSource: req.GetVolumeContentSource(), // pass through for clones/snaps
        },
    }, nil
}
```

The seven steps — validate, capacity-check, idempotency lookup, topology resolve, cloud call, record, respond — are the canonical structure for every Controller RPC. `ControllerPublishVolume` differs only in step 5 (cloud attach instead of create) and step 7 (returns `publish_context`).

### 25.7 The `NodePublishVolume` Pattern (Node Side)

```go
func (n *Node) NodePublishVolume(ctx context.Context, req *csi.NodePublishVolumeRequest) (*csi.NodePublishVolumeResponse, error) {
    if req.GetVolumeId() == "" || req.GetTargetPath() == "" || req.GetStagingTargetPath() == "" {
        return nil, status.Error(codes.InvalidArgument, "missing fields")
    }

    target := req.GetTargetPath()
    source := req.GetStagingTargetPath()

    // Idempotency: if target is already a mount of source, return success.
    if mounted, _ := n.isMountedFrom(target, source); mounted {
        return &csi.NodePublishVolumeResponse{}, nil
    }

    // Ensure the target directory exists. Note: this directory is auto-created
    // by kubelet at /var/lib/kubelet/pods/<uid>/volumes/kubernetes.io~csi/<pv>/mount,
    // but the driver re-asserts it for safety.
    if err := os.MkdirAll(target, 0750); err != nil {
        return nil, status.Errorf(codes.Internal, "mkdir target: %v", err)
    }

    // Bind-mount staging → target. The propagation of this mount is controlled
    // by how the driver Pod is configured (mountPropagation: Bidirectional).
    flags := []string{"bind"}
    if req.GetReadonly() {
        flags = append(flags, "ro")
    }
    flags = append(flags, req.GetVolumeCapability().GetMount().GetMountFlags()...)

    if err := n.mounter.Mount(source, target, "", flags); err != nil {
        return nil, status.Errorf(codes.Internal, "bind-mount failed: %v", err)
    }

    return &csi.NodePublishVolumeResponse{}, nil
}
```

The key invariant: `NodePublishVolume` never formats. The filesystem already exists (formatted by `NodeStageVolume`); this is just a bind-mount.

---

## 26. Debugging Storage

The four-place rule: when a PVC is stuck, the problem is in one of four places. Check them in order.

### 26.1 The Order of Inspection

```
1. The PVC itself
   kubectl describe pvc -n app data-pvc
   - Look at Events. Most stuck PVCs have ProvisioningFailed events with
     a clear cloud-API error.
   - Look at Status.Phase: Pending vs Bound.
   - Look at Annotations:
       volume.kubernetes.io/selected-node      ← scheduler picked a node
       volume.kubernetes.io/storage-provisioner ← which provisioner is responsible
       volume.beta.kubernetes.io/storage-provisioner

2. The provisioner sidecar logs
   kubectl logs -n kube-system deploy/ebs-csi-controller -c csi-provisioner --tail=200
   - Errors here = the provisioner sees the PVC but the CreateVolume CSI call failed.
   - Common: cloud quota exceeded, IAM permission missing, AZ doesn't have capacity.

3. The driver itself (controller side)
   kubectl logs -n kube-system deploy/ebs-csi-controller -c ebs-plugin --tail=200
   - This is where you see the actual cloud SDK error.
   - "InvalidParameterValue: Iops 16001 not supported for gp3" etc.

4. If PVC binds but Pod is stuck "ContainerCreating":
   kubectl describe pod ...   → look for FailedMount events
   kubectl get volumeattachment | grep <pv-name>
   - VA exists, attached: true   → mount-side problem; check node plugin
   - VA exists, attached: false  → attach-side problem; check attacher + driver
   - VA missing                  → attach-detach controller didn't enqueue; rare

5. If attach is stuck:
   kubectl logs -n kube-system deploy/ebs-csi-controller -c csi-attacher
   kubectl logs -n kube-system deploy/ebs-csi-controller -c ebs-plugin

6. If mount is stuck (node side):
   kubectl logs -n kube-system ds/ebs-csi-node -c ebs-plugin --tail=200
   - On the affected node:
     journalctl -u kubelet -f | grep -i volume
   - Look for: mount failed, mkfs failed, device not found.

7. The node itself, last resort:
   On the host:
     lsblk                          ← does the disk show up?
     mount | grep kubelet           ← is anything mounted?
     ls -la /var/lib/kubelet/plugins/.../ ← does the global mount exist?
     dmesg | tail                   ← kernel-level disk errors
```

### 26.2 The Most Common Failure Modes (and their tells)

| Symptom | Likely cause | Where to look |
|---|---|---|
| PVC Pending, no events | No provisioner running for this class | Check StorageClass.provisioner matches an installed driver |
| PVC Pending, `ProvisioningFailed` | Cloud API error | Provisioner logs, then driver logs |
| PVC Bound, Pod ContainerCreating, `FailedAttachVolume` | Detach from prior node didn't finish | Check VolumeAttachment objects, attacher logs |
| Pod ContainerCreating, `MountVolume.MountDevice failed` | Filesystem corruption or mkfs failure | Node-plugin logs, `dmesg`, `lsblk` |
| Pod stuck, no FailedMount events | kubelet volume manager wedged | `journalctl -u kubelet -f` |
| Detach stuck for 6+ minutes after node failure | node-monitor-grace-period + force-detach | Wait, or manually delete the VolumeAttachment |
| Resize doesn't take effect | Filesystem resize step is offline-only; need pod restart | Check NodeGetCapabilities for EXPAND_VOLUME |

### 26.3 The Diagnostic Tree

```
                  ┌────────────────────────────────────────┐
                  │   Pod stuck "ContainerCreating"        │
                  └────────────────┬───────────────────────┘
                                   │
                                   ▼
                  ┌────────────────────────────────────────┐
                  │  kubectl describe pod → Events?        │
                  └────────────────┬───────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
   "FailedScheduling"      "FailedAttachVolume"       "FailedMount"
        │                          │                          │
        ▼                          ▼                          ▼
  Scheduling problem         Attach problem            Mount problem
  (chapter 09).              See below.                See below.
  Likely PV nodeAffinity     Check VolumeAttachment    Check kubelet log
  doesn't match any node.    + attacher log.           + node-plugin log.

  ┌─────────────────────────────────────────────────────────┐
  │  Attach problem dive                                     │
  │                                                          │
  │  kubectl get va | grep <pv-name>                         │
  │      ↓                                                   │
  │  attached=true ──→ false alarm; mount must be the issue  │
  │  attached=false ─→ external-attacher log                 │
  │                       ↓                                  │
  │                    "PERMISSION_DENIED" ─→ IAM            │
  │                    "RESOURCE_EXHAUSTED" → quota          │
  │                    "ABORTED" ─────────→ retry pending    │
  │                    "INTERNAL" ──────→ driver bug         │
  │  no VA ───────────→ attach-detach controller wedged      │
  │                     (KCM log; rare)                       │
  └─────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────┐
  │  Mount problem dive                                      │
  │                                                          │
  │  ssh <node>                                              │
  │      ↓                                                   │
  │  lsblk → does the device exist?                          │
  │  → no  ──→ attach didn't actually expose disk;           │
  │            cloud detach pending; udev stuck              │
  │  → yes ──→ next step                                     │
  │                                                          │
  │  mount | grep <pv-name> → is staging mount up?           │
  │  → no  ──→ NodeStageVolume failed (mkfs, mount error)    │
  │  → yes ──→ NodePublishVolume failed (bind-mount)         │
  │                                                          │
  │  journalctl -u kubelet --since "10 min ago" | grep -i csi│
  │  kubectl logs ds/<driver>-node -c <plugin> -n kube-system│
  └─────────────────────────────────────────────────────────┘
```

### 26.4 Useful One-Liners

```bash
# Show all PVCs and their phases across the cluster
kubectl get pvc -A

# Find PVs whose claim still references a missing PVC
kubectl get pv -o json | jq '.items[] | select(.status.phase=="Released") | .metadata.name'

# Inspect a VolumeAttachment
kubectl get volumeattachment -o yaml | grep -A 20 <pv-name>

# Show the CSINode registration for a node
kubectl get csinode <node-name> -o yaml

# Driver capabilities (what RPCs does it implement)
kubectl get csidriver <driver> -o yaml

# Watch volume manager activity on a node
ssh node-2 'journalctl -u kubelet -f | grep -iE "volume|mount|csi"'

# Find Pods that reference a specific PVC
kubectl get pod -A -o json | jq '.items[] | select(.spec.volumes[]?
  .persistentVolumeClaim.claimName=="data-pvc") | .metadata.namespace + "/" + .metadata.name'

# List all volumes attached to a node
kubectl get volumeattachment -o json | jq --arg n node-2 \
  '.items[] | select(.spec.nodeName==$n) | .metadata.name + " → " + .spec.source.persistentVolumeName'

# Force-detach a VolumeAttachment (DANGEROUS — only if you know the node is gone)
kubectl patch volumeattachment <name> -p '{"metadata":{"finalizers":null}}' --type=merge
kubectl delete volumeattachment <name>

# Show CSI driver capabilities
kubectl get csidriver -o custom-columns=NAME:.metadata.name,\
ATTACH:.spec.attachRequired,EPHEMERAL:.spec.volumeLifecycleModes,\
FSGROUP:.spec.fsGroupPolicy
```

### 26.5 Anatomy of the Kubelet Volume Manager (Chapter 10 Cross-Ref)

The kubelet's volume manager (in `pkg/kubelet/volumemanager`) maintains two parallel data structures:

```
DesiredStateOfWorld          ActualStateOfWorld
─────────────────────        ──────────────────
"these pods on this           "these mounts exist
 node want these               in this kernel right
 volumes mounted"              now"

Built from:                   Built from:
- Pod watch                   - filesystem scan of
- PV/PVC informers              /var/lib/kubelet/pods
- VolumeAttachment status     - mount table reads
                              - CSI Node RPCs we issued

       ┌──────────────────────────────┐
       │     Reconciler loop          │
       │   (every 100ms)              │
       │                              │
       │  diff DSW vs ASW:            │
       │    in DSW, not in ASW → mount│
       │    in ASW, not in DSW → umnt │
       └──────────────────────────────┘
```

The reconciler is what makes the volume manager level-triggered: it doesn't react to events, it reconciles state. If a CSI gRPC fails, the next 100ms tick tries again. This is also why a wedged `NodeUnstageVolume` can spin in logs for hours — the kubelet keeps trying.

If the reconciler is stuck on one volume, it can block progress on others (single-threaded per-volume worker per the `actual_state_of_world` populator). The kubelet metric `volume_manager_total_volumes{state="uncertain"}` is the canary.

---

## 27. Observability and Metrics

The metrics surface for storage spans kubelet, CSI sidecars, and the driver.

### 27.1 Core Metrics

| Metric | Source | What it tells you |
|---|---|---|
| `csi_operations_seconds{driver_name, method_name, grpc_status_code}` | each sidecar | Histogram of CSI gRPC latencies. Tail latency on `CreateVolume` or `NodeStageVolume` is the canonical "storage is slow" signal. |
| `csi_operations_seconds_count{grpc_status_code!="OK"}` | each sidecar | Error rate per RPC per driver. Spikes correlate with cloud-API throttling. |
| `kubelet_volume_stats_capacity_bytes{namespace, persistentvolumeclaim}` | kubelet (from `NodeGetVolumeStats`) | Volume size. |
| `kubelet_volume_stats_used_bytes` | kubelet | Used space. Drives "PVC almost full" alerts. |
| `kubelet_volume_stats_inodes_used` | kubelet | Inode usage. Important for small-file workloads. |
| `storage_operation_duration_seconds{operation_name}` | kube-controller-manager | Attach/detach durations as observed by the attach-detach controller. |
| `attachdetach_controller_total_volumes{state, plugin_name}` | KCM | How many volumes are attached/desired-attached per driver. |
| `volume_manager_total_volumes{plugin_name, state}` | kubelet | How many volumes the node thinks it has in each state. |
| `volume_provisioner_succeeded_total` / `volume_provisioner_failed_total` | external-provisioner | Provisioning success rate. |

### 27.2 Alert Recipes

```yaml
- alert: CSIOperationLatencyHigh
  expr: |
    histogram_quantile(0.99,
      sum by (driver_name, method_name, le) (
        rate(csi_operations_seconds_bucket[5m])
      )
    ) > 30
  for: 10m
  annotations:
    summary: "p99 latency on {{$labels.driver_name}}/{{$labels.method_name}} > 30s"

- alert: VolumeNearlyFull
  expr: |
    (kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes) > 0.9
  for: 30m
  annotations:
    summary: "PVC {{$labels.namespace}}/{{$labels.persistentvolumeclaim}} >90% full"

- alert: PVCStuckPending
  expr: |
    kube_persistentvolumeclaim_status_phase{phase="Pending"} == 1
  for: 15m
  annotations:
    summary: "PVC stuck Pending for 15m"

- alert: AttachDetachErrors
  expr: |
    rate(storage_operation_duration_seconds_count{status="failure"}[5m]) > 0.1
  for: 5m
  annotations:
    summary: "Attach/detach failure rate > 0.1/s"

- alert: CSISidecarRestarting
  expr: |
    increase(kube_pod_container_status_restarts_total{namespace="kube-system",
            container=~"csi-.*"}[1h]) > 3
  annotations:
    summary: "CSI sidecar {{$labels.container}} restarting"
```

### 27.3 What to Look at First in an Incident

1. **`csi_operations_seconds` p99 across all drivers.** A regression here flags a driver problem.
2. **`storage_operation_duration_seconds` for attach.** Spikes here flag cloud-side throttling.
3. **`kubelet_volume_stats_used_bytes` percentiles.** Tail-full volumes cause cascading "Pod stuck" tickets.
4. **`volume_manager_total_volumes{state="uncertain"}`.** A growing "uncertain" count means the kubelet doesn't know if a volume is mounted or not — usually a stuck umount.

---

## 28. Pitfalls

A field guide to the storage problems you will encounter.

1. **Single-zone storage under multi-zone scheduling.** StorageClass with `volumeBindingMode: Immediate` and a block driver. Pod scheduled to AZ B, volume in AZ A → Pod Pending forever. *Fix:* `volumeBindingMode: WaitForFirstConsumer`.
2. **Deleting a PV before its PVC, while reclaim=Retain.** PV deleted; cloud volume orphaned; new PVCs cannot find it. *Fix:* always delete PVC first; let GC + reclaim handle the PV.
3. **PVC stuck "Released" after `Retain`.** The PV is held by claimRef.uid that no longer matches. *Fix:* `kubectl edit pv pv-xxx`, remove `.spec.claimRef.uid` (keep the name/namespace if you want it to rebind to a new PVC of the same name), and the PV will move to `Available`.
4. **`ReadWriteMany` on a block driver.** PVC Pending with `does not support access mode`. *Fix:* use an RWX-capable driver (NFS, CephFS, EFS).
5. **Ephemeral generic volumes used for persistent data.** Pod rescheduled → PVC deleted → data gone. *Fix:* use StatefulSet `volumeClaimTemplates`.
6. **Volume expansion without filesystem resize.** Cloud volume grows (`ControllerExpandVolume` ok), but the filesystem inside is still old size. *Fix:* ensure driver supports Node `EXPAND_VOLUME`; if not, restart the pod to trigger `NodeStageVolume` resize.
7. **Long mount times during pod startup (10+ minutes).** Usually: udev settle delay + filesystem repair + iSCSI rescan. *Fix:* check `dmesg`, check `udevadm settle`, consider switching from iSCSI to NVMe-oF, or pre-creating ext4 with `lazy_itable_init=0`.
8. **CSI controller pod evicted (no replicas).** No provisioner running → all PVCs Pending. *Fix:* `priorityClassName: system-cluster-critical`, PDB with `minAvailable: 1`, anti-affinity across nodes.
9. **CSI node pod missing on a node.** Pods scheduled to that node can't mount. *Fix:* DaemonSet tolerations should include `operator: Exists` to land on tainted nodes; `priorityClassName: system-node-critical`.
10. **`mountPropagation: None` default blocking host tools.** Filebeat / cAdvisor / Falco mounts `/var/lib/docker/containers` but later container starts don't propagate to them. *Fix:* `mountPropagation: HostToContainer` on the agent's mount.
11. **secrets-store-csi RBAC: ServiceAccount can't read the secret backend.** Driver fails NodePublishVolume → Pod hangs. *Fix:* explicit RBAC on the SP class's ServiceAccount, especially with Workload Identity / IRSA.
12. **One single StorageClass for the whole cluster.** Workloads with different SLAs (logs vs DB vs ephemeral) all on the same gp3 → DB throttled by log churn. *Fix:* multiple classes (`gp3-standard`, `io2-db`, `local-cache`), per-namespace defaults via Kyverno.
13. **Missing topology labels on a node.** Driver registered, but `CSINode.spec.drivers[].topologyKeys` empty. Provisioner can't constrain volume to a zone → may create in wrong AZ. *Fix:* check the node-driver-registrar logs; ensure the driver's `NodeGetInfo` returns `accessible_topology`.
14. **Detach stuck on stale multipath/conntrack entries.** `umount` succeeds, `multipath -ll` still shows the device → `ControllerUnpublish` times out. *Fix:* `multipath -f /dev/dm-x`; long-term, ensure `multipathd` is current and `find_multipaths yes` is set.
15. **Snapshot without quiescing the application.** Crash-consistent snapshot of a write-heavy DB → restore boots into recovery mode. *Fix:* Velero hooks; for Postgres: `pg_start_backup`/`pg_stop_backup` style, or VolumeSnapshot timed after `CHECKPOINT`.
16. **PVC delete that hangs forever.** A Pod is still using the PVC; the `pvc-protection` finalizer blocks deletion. *Fix:* delete the Pod first. If the Pod is stuck terminating, address that first.
17. **`reclaimPolicy: Delete` set on a StorageClass for an audit-required tenant.** A user `kubectl delete pvc` and seconds later a TB of PHI is gone. *Fix:* admission policy (Kyverno) forcing `reclaimPolicy: Retain` for namespaces labeled `tier=regulated`.
18. **Image-pull-secret-style typo in StorageClass parameters.** `iops: 3000` (string) vs `iops: "3000"` (string in YAML). Most CSI drivers expect strings. *Fix:* always quote.
19. **CSI driver upgrade breaks running pods.** New driver version changes the `staging_target_path` layout. Existing mounted volumes don't unmount cleanly. *Fix:* drain nodes before upgrading the DaemonSet; or test the upgrade in a non-prod cluster.
20. **Backing volume deleted out-of-band (someone clicked delete in the cloud console).** PV still says Bound, but every operation fails. *Fix:* monitor cloud audit logs; admission policy that requires a label or tag on cloud volumes for K8s management.
21. **`fsGroup` causes minute-long pod starts on large volumes.** Default `fsGroupChangePolicy: Always` walks every file. *Fix:* set `fsGroupChangePolicy: OnRootMismatch` on the Pod's `securityContext`. Or use `seLinuxMount` for SELinux-labeled clusters.
22. **NodeStageVolume idempotency assumption violated.** Driver doesn't handle "already staged" gracefully; retries fail. *Fix:* test with `csi-sanity`; the staging path is the idempotency key.
23. **PV with stale `claimRef.uid` adopted incorrectly.** Admin edits PV to clear the UID; a new PVC with the same name *but different intent* binds and gets the old data. *Fix:* clear the entire `claimRef`, not just the UID; verify the PVC's storage size and namespace.
24. **`kubectl delete --force --grace-period=0` on a Pod with a PVC.** Pod object is gone from etcd, but the kubelet on the (possibly partitioned) node is still mounting the volume, and the kubelet on a new node now wants to attach the same volume. Result: ABA race, possible data corruption with RWO drivers. *Fix:* never use `--force` for PVC-attached Pods unless you have confirmed the source node is truly dead and the volume is force-detached.
25. **No PDB on the CSI controller Deployment.** Cluster-autoscaler drains the node hosting the only controller replica; provisioning stalls until rescheduled. *Fix:* `minAvailable: 1` PDB, replicas≥2.
26. **CSI driver missing `fsGroupPolicy: File`.** Setting `pod.spec.securityContext.fsGroup: 1000` does nothing — files remain owned by root. *Fix:* set `fsGroupPolicy: File` on the CSIDriver (it tells the kubelet to do the chown walk). For large volumes, also set `fsGroupChangePolicy: OnRootMismatch` on the Pod to avoid the recursive chown every mount.
27. **Single PV class for the whole cluster + reclaim=Delete + accidental kubectl delete namespace.** The cascade deletes every PVC, which deletes every PV, which deletes every cloud volume. *Fix:* reclaim=Retain on prod classes; Kyverno policy blocking namespace deletion when it contains PVCs.
28. **Online expansion: PVC says 100Gi, Pod's `df -h` still shows 50Gi.** The block grew but `NodeExpandVolume` hasn't run. Common cause: kubelet feature gate `ExpandInUsePersistentVolumes` was off (pre-1.24); or driver doesn't advertise `NodeExpandVolume` capability. *Fix:* check the driver caps, restart the pod as a last resort.
29. **Hostpath / local PVs without nodeAffinity.** PV is created with no `nodeAffinity` — scheduler picks any node; mount fails because the path doesn't exist there. *Fix:* always set `nodeAffinity` on local PVs; or use TopoLVM which manages this for you.
30. **CSI driver YAML applied without RBAC.** Driver Pods start, but cannot list PVs or update VolumeAttachments. *Fix:* every driver Helm chart bundles the necessary ClusterRole/Binding; install via the chart, not from cherry-picked YAML.
31. **Inline `csi:` volumes (non-ephemeral) in a Pod.** These were deprecated and removed; only ephemeral CSI volumes are allowed inline now. Use a PVC for persistent volumes. *Fix:* convert to PVC + Pod reference.
32. **Forgetting `storageCapacity: true` on the CSIDriver.** The scheduler doesn't know whether a node's storage is full → can schedule Pods to nodes that can't satisfy. *Fix:* enable `storageCapacity: true` for local drivers especially.

---

## 29. TL;DR

```
WHAT YOU TYPE                                WHAT KUBERNETES DOES
─────────────                                ────────────────────

apiVersion: v1                               PVC stays Pending
kind: PersistentVolumeClaim
spec:
  storageClassName: gp3                      external-provisioner sees PVC, sees class,
  accessModes: [ReadWriteOnce]               sees Pending → calls CSI CreateVolume
  resources: {requests: {storage: 50Gi}}     → driver calls cloud SDK
                                             → PV is created with claimRef pre-set
                                             → PV binder marks both Bound

apiVersion: v1                               scheduler picks node-2 (considering volume
kind: Pod                                    nodeAffinity if Immediate; or annotates PVC
spec:                                        with selected-node if WaitForFirstConsumer)
  containers:
    - name: app                              attach-detach controller creates a
      volumeMounts: [{name: d, mountPath: …}]VolumeAttachment(pv-xxx, node-2)
  volumes:
    - name: d                                external-attacher calls
      persistentVolumeClaim: {claimName: …} ControllerPublishVolume → cloud attaches
                                             → device shows up in /dev/

                                             kubelet volume manager:
                                             - NodeStageVolume → mkfs (first time)
                                                                + mount staging path
                                             - NodePublishVolume → bind-mount per-pod
                                             - bind-mount into container's mount ns

                                             Pod sees /var/lib/app as the volume.
```

**The three-phase invariant.** Provision is cluster-wide and runs once per volume. Attach is per-(volume, node) and runs once per scheduling decision. Stage+Publish (mount) is per-(node, volume) for staging and per-(pod, volume) for publishing. Each phase has a distinct CSI RPC, a distinct sidecar or controller responsible, and a distinct failure mode. Storage debugging is almost entirely about pinning down which phase is stuck.

**The five rules to remember.**
1. `volumeBindingMode: WaitForFirstConsumer` is almost always correct in cloud.
2. `reclaimPolicy: Retain` on anything stateful you care about.
3. RWO is per *node*, RWOP is per *pod* — pick the one that matches your real concurrency need.
4. `mountPropagation: Bidirectional` only on CSI node DaemonSets; never on user workloads.
5. CSI is gRPC over a Unix socket: when in doubt, the bug is in one of the sidecars or in `journalctl -u kubelet`, not in the API server.

The storage layer is the most physically grounded part of Kubernetes — every YAML edit eventually becomes a `mount(2)` somewhere. Understand the three phases and the sidecars, and the rest of the chapter is just configuration.
