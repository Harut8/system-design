# Kubelet Internals: The Node Agent

The kubelet is the single most complex process on a Kubernetes worker node. It is the only consumer on the node of "what pods should run here?" and the only producer of "what pods *are* running here?". Everything else on the node — the container runtime, CNI plugins, CSI node plugins, device plugins — is something the kubelet drives. Lose the kubelet and the node freezes in time: existing containers keep running (the kernel doesn't care), but no new pods land, no probes fire, no statuses update, no evictions happen, and the apiserver will eventually mark the node `NotReady`.

This chapter dissects the kubelet from the inside. We start with the architecture (§1), walk the central `syncLoop` event loop (§3), and then peel off each subsystem in turn: pod workers, PLEG, the full pod sync, CRI call sequencing, probes, status manager, volume manager, CNI integration, device/CPU/memory/topology managers, the eviction manager, OOM kill semantics, image GC, log management, the kubelet's own API surface, graceful node shutdown, static pods, bootstrap and cert rotation. We finish with the failure-mode pitfalls that bite at 3 am.

Pre-reqs: chapter 00 (Linux primitives — kubelet sits on top of namespaces, cgroups, capabilities), chapter 01 (CRI/OCI — the kubelet's southbound gRPC interface). Forward-references: chapter 11 (Pod internals — the *object* the kubelet manages), chapter 15 (CNI — pod networking detail), chapter 19 (CSI — volume detail), chapter 21 (resources & QoS — the policies the kubelet enforces).

---

## Table of Contents

1.  [The Kubelet's Role and Architecture](#1-the-kubelets-role-and-architecture)
2.  [Sources of Truth for Pods](#2-sources-of-truth-for-pods)
3.  [The syncLoop: Central Event Loop](#3-the-syncloop-central-event-loop)
4.  [Pod Workers: Per-Pod Serialization](#4-pod-workers-per-pod-serialization)
5.  [PLEG: The Pod Lifecycle Event Generator](#5-pleg-the-pod-lifecycle-event-generator)
6.  [The Full Pod Sync: computePodActions and SyncPod](#6-the-full-pod-sync-computepodactions-and-syncpod)
7.  [CRI Call Order During Pod Startup](#7-cri-call-order-during-pod-startup)
8.  [Probes: Startup, Readiness, Liveness](#8-probes-startup-readiness-liveness)
9.  [Status Manager](#9-status-manager)
10. [Volume Manager](#10-volume-manager)
11. [CNI Integration](#11-cni-integration)
12. [Device Manager](#12-device-manager)
13. [CPU Manager](#13-cpu-manager)
14. [Memory Manager](#14-memory-manager)
15. [Topology Manager](#15-topology-manager)
16. [Eviction Manager](#16-eviction-manager)
17. [OOM Kill Behavior](#17-oom-kill-behavior)
18. [OOM Score Adjustment](#18-oom-score-adjustment)
19. [Image Garbage Collection](#19-image-garbage-collection)
20. [Container Log Management](#20-container-log-management)
21. [Authentication to the apiserver](#21-authentication-to-the-apiserver)
22. [The /metrics, /metrics/cadvisor, /metrics/resource Endpoints](#22-the-metrics-metricscadvisor-metricsresource-endpoints)
23. [Kubelet API Endpoints](#23-kubelet-api-endpoints)
24. [Graceful Node Shutdown](#24-graceful-node-shutdown)
25. [Static Pods and the Mirror Pod Pattern](#25-static-pods-and-the-mirror-pod-pattern)
26. [Bootstrap, Certs, Kubeconfig](#26-bootstrap-certs-kubeconfig)
27. [Pitfalls](#27-pitfalls)
28. [TL;DR](#28-tldr)

---

## 1. The Kubelet's Role and Architecture

The kubelet is the only on-node component that knows about Pods. Everything below the kubelet (runtimes, CNI plugins, CSI drivers, device plugins) operates on lower-level concepts (containers, namespaces, volumes, devices) and is unaware of `Pod` as an API object. Everything above the kubelet (apiserver, controllers, scheduler) operates on `Pod` objects in etcd and is unaware of how containers actually run. The kubelet is the translation layer.

```
                          (control plane — chapter 05)
                          ┌────────────────────────────┐
                          │  kube-apiserver             │
                          │  (watch /pods, /nodes,      │
                          │   /configmaps, /secrets;    │
                          │   patch /pods/status,       │
                          │   /nodes/status)            │
                          └─────────────┬──────────────┘
                                        │ HTTPS · client cert
                                        │ watch / list / patch
                                        ▼
       ┌────────────────────────────────────────────────────────────────────┐
       │  KUBELET   (pkg/kubelet/, ~150k LoC)                                │
       │                                                                    │
       │   ┌──────────────────────────────────────────────────────────────┐ │
       │   │  CONFIG SOURCES (multiplexed PodConfig)                       │ │
       │   │    • apiserver (informer) — normal pods                       │ │
       │   │    • file (--pod-manifest-path) — static pods                 │ │
       │   │    • http (--manifest-url) — legacy                           │ │
       │   └──────────────────┬───────────────────────────────────────────┘ │
       │                      │ podUpdates channel                          │
       │                      ▼                                              │
       │   ┌──────────────────────────────────────────────────────────────┐ │
       │   │  syncLoop  (the heartbeat — one goroutine, select loop)       │ │
       │   │    inputs:                                                    │ │
       │   │      podUpdates · pleg.Events · syncCh · housekeepingCh       │ │
       │   │      liveness/readiness/startupManager.Updates                │ │
       │   └─────┬─────────────────────┬──────────────────────┬──────────┘ │
       │         │                     │                      │              │
       │         ▼                     ▼                      ▼              │
       │   ┌──────────┐         ┌────────────┐         ┌─────────────┐    │
       │   │   pod    │         │    PLEG    │         │   probe     │    │
       │   │ workers  │         │  (relist   │         │   manager   │    │
       │   │ (1 per   │         │   every 1s │         │  (per-      │    │
       │   │  pod)    │         │   / evented│         │   container │    │
       │   │          │         │   stream)  │         │   tickers)  │    │
       │   └────┬─────┘         └─────┬──────┘         └─────────────┘    │
       │        │                     │                                     │
       │        ▼                     ▼                                     │
       │   ┌────────────┐      ┌───────────────┐   ┌────────────┐  ┌─────┐│
       │   │  status    │      │   volume      │   │  device    │  │ cpu ││
       │   │  manager   │      │   manager     │   │  manager   │  │ mgr ││
       │   │  (batched  │      │   (DSW vs ASW │   │ (plugins   │  │     ││
       │   │   PATCH    │      │    reconciler)│   │  + Allocate│  │ mem ││
       │   │   /status) │      │               │   │   RPC)     │  │ mgr ││
       │   └────────────┘      └───────────────┘   └────────────┘  └─────┘│
       │                                                                    │
       │   ┌────────────┐   ┌─────────────┐   ┌──────────────┐   ┌──────┐ │
       │   │ topology   │   │  eviction   │   │  image GC    │   │ log  │ │
       │   │ manager    │   │  manager    │   │  container GC│   │ mgmt │ │
       │   │ (hint mux) │   │             │   │              │   │      │ │
       │   └────────────┘   └─────────────┘   └──────────────┘   └──────┘ │
       │                                                                    │
       │   ┌───────────────────────────────────────────────────────────┐  │
       │   │  Kubelet HTTPS server   :10250                             │  │
       │   │   /pods · /healthz · /metrics · /run · /exec · /attach     │  │
       │   │   /portForward · /logs · /containerLogs · /stats           │  │
       │   │   /metrics/cadvisor · /metrics/resource · /metrics/probes  │  │
       │   └───────────────────────────────────────────────────────────┘  │
       └──────────────────────────────┬─────────────────────────────────────┘
                                      │
              ┌───────────────────────┼─────────────────────────────────┐
              │ CRI gRPC              │ CNI exec                        │ CSI gRPC
              │ (unix socket)         │ (binary in /opt/cni/bin)        │ (unix socket)
              ▼                       ▼                                 ▼
       ┌──────────────┐       ┌────────────────┐               ┌────────────────┐
       │  container   │       │  CNI plugin    │               │  CSI node      │
       │  runtime     │       │  (Calico,      │               │  plugin        │
       │  (containerd │       │   Cilium,      │               │  (NodeStage,   │
       │   / CRI-O)   │       │   Flannel)     │               │   NodePublish) │
       │  ── ch 01    │       │  ── ch 15      │               │  ── ch 19      │
       └──────┬───────┘       └────────────────┘               └────────────────┘
              │ OCI runtime (runc / kata / gvisor)
              ▼
       ┌─────────────────────────────────────────────────────────────────┐
       │  LINUX KERNEL  (ch 00)                                          │
       │   namespaces · cgroups v2 · netfilter · overlayfs · seccomp     │
       └─────────────────────────────────────────────────────────────────┘
```

A few facts about that diagram that the rest of this chapter unpacks:

- **Single process.** Despite the apparent fan-out of managers, the kubelet is *one* process with *many* goroutines. The managers communicate via channels and shared caches, not gRPC. The only network surfaces the kubelet exposes are the HTTPS API (`:10250`), read-only port (`:10255`, deprecated), and the metrics/healthz ports.
- **The kubelet is the only writer of `pod.status`.** No controller may modify a pod's status; that field belongs to the kubelet that owns the pod. The apiserver enforces this via the Node authorizer (chapter 07).
- **The kubelet is the only writer of its own `Node.status`.** Heartbeats happen through `node.status.conditions` and `Lease` objects in `kube-node-lease` (default since 1.13 — a Lease is ~50 bytes vs ~10 KB for a Node status patch, which matters at 5000 nodes).
- **The southbound is plug-and-play.** The kubelet doesn't know what container runtime it's talking to; it just speaks CRI to whatever Unix socket `--container-runtime-endpoint` points at. Same for CNI (chain of binaries) and CSI (Unix socket per driver registered via the kubelet plugin registration server).

The kubelet is the only Kubernetes component that *cannot* be horizontally scaled. There is exactly one kubelet per node, and that kubelet owns *all* of that node's local state.

### 1.1 Source layout (where to read the code)

The kubelet lives under `pkg/kubelet/` in the `kubernetes/kubernetes` repo. The map is worth memorizing because every subsystem we discuss has a directory:

```
pkg/kubelet/
├── kubelet.go                   # struct Kubelet + the syncLoop
├── kubelet_pods.go              # pod helpers (volume mounts, env, etc.)
├── pod_workers.go               # per-pod goroutine dispatcher
├── pleg/                        # generic + evented PLEG
├── prober/                      # liveness, readiness, startup probes
├── status/                      # status manager
├── volumemanager/               # volume manager (desired/actual state)
├── images/                      # image manager + image GC
├── container/                   # runtime abstraction (cri-api wrapper)
├── kuberuntime/                 # the CRI-backed runtime implementation
├── cm/                          # container manager — the big one
│   ├── cpumanager/              # CPU manager (none/static)
│   ├── memorymanager/           # Memory manager
│   ├── topologymanager/         # Topology manager
│   ├── devicemanager/           # Device manager + plugin registration
│   ├── container_manager_linux.go
│   └── ...
├── eviction/                    # eviction manager
├── nodestatus/                  # node status setters (one per condition)
├── server/                      # the HTTPS server
├── certificate/                 # bootstrap + rotation
├── config/                      # PodConfig (multiplexes the 3 sources)
└── ...
```

Tip: when you read the source and feel lost, start in `kubelet.go` at `func (kl *Kubelet) syncLoop(...)` and `func (kl *Kubelet) syncPod(...)`. Every other file is reachable from those two functions in two or three hops.

---

## 2. Sources of Truth for Pods

The kubelet does *not* have a single source of truth for what pods should run on the node. It has up to three, multiplexed by a small component called `PodConfig` (`pkg/kubelet/config/config.go`).

```
                                 ┌──────────────────────────┐
   apiserver ── informer ───────►│                          │
   (normal pods, scheduled to    │                          │
    spec.nodeName == this node)  │       PodConfig          │
                                 │                          │
   file watcher  ────────────────►│  • dedups by pod UID    │ ──► podUpdates chan
   --pod-manifest-path           │  • emits ADD/UPDATE/    │     PodUpdate{Op,Pods}
   /etc/kubernetes/manifests/    │    REMOVE/DELETE        │
                                 │  • tags source on each  │
   http poller  ────────────────►│    incoming pod         │
   --manifest-url (legacy)       │                          │
                                 └──────────────────────────┘
```

Each source produces `PodUpdate` events of type `ADD`, `UPDATE`, `REMOVE`, `DELETE`, `SET`, `RECONCILE`. `PodConfig` merges them into one stream tagged with `kubetypes.ConfigSource`. Downstream, the `syncLoop` doesn't care which source a pod came from — *except* for one detail discussed in §25: static pods (from `file` or `http`) get a *mirror Pod* created on the apiserver so the rest of the cluster can see them.

### 2.1 Source 1: apiserver (the normal one)

The kubelet runs a `cache.Reflector` (chapter 08) on `core/v1/Pod` with a field selector pinning `spec.nodeName=<this-node>`. This is the *only* watch the kubelet uses for pod assignments. When the scheduler patches a pod's `spec.nodeName` to this node, that PATCH propagates to the apiserver's watch cache, then to the kubelet's informer, then into `PodConfig`, then to `syncLoop`. Steady-state lag is single-digit milliseconds.

There is a subtle correctness rule: a pod isn't "for this node" until `spec.nodeName` is set *and* the kubelet's watch has caught the event. If the kubelet was restarted and is still catching up the initial LIST, scheduler-assigned pods sit in apiserver waiting. (This is one reason kubelet startup latency matters at scale — a 30-second initial LIST means 30 seconds of scheduling pile-up on that node.)

### 2.2 Source 2: file (`--pod-manifest-path`)

This is the static-pod mechanism. Point the kubelet at a directory (default on kubeadm clusters: `/etc/kubernetes/manifests/`), drop YAML files in it, and each YAML becomes a pod whose lifecycle is owned locally by this kubelet. The control plane on a kubeadm cluster runs this way: kube-apiserver, kube-controller-manager, kube-scheduler, etcd are all static pods on the control-plane nodes.

```
$ ls /etc/kubernetes/manifests/
etcd.yaml  kube-apiserver.yaml  kube-controller-manager.yaml  kube-scheduler.yaml
```

The kubelet uses fsnotify (`inotify` on Linux) to watch this directory plus a periodic re-read at `--file-check-frequency` (default 20s) as a safety net. A change to any YAML produces a PodUpdate.

A static pod's UID is deterministic — derived from the node name and the file path — so the same YAML on the same node always produces the same pod identity.

### 2.3 Source 3: http (`--manifest-url`)

Same as `file` but fetched periodically from an HTTP endpoint. Originally used by early bare-metal deployments to centralize the manifests. Effectively legacy; nobody should be using this in 2026.

### 2.4 The mirror-pod pattern

A static pod, by definition, has no representation in etcd. That's a problem for the rest of the cluster: `kubectl get pods -n kube-system` would not show kube-apiserver. To fix this, when the kubelet starts a static pod, it creates a *mirror Pod* on the apiserver:

```
   static pod (lives in kubelet's local cache, source=file)
       │ kubelet creates a mirror on apiserver, with:
       │   • metadata.annotations[kubernetes.io/config.mirror] = <hash>
       │   • metadata.annotations[kubernetes.io/config.source] = "file"
       │   • metadata.annotations[kubernetes.io/config.seen]   = <RFC3339>
       │   • ownerReferences = [{kind: Node, name: this-node}]
       ▼
   mirror Pod (lives in etcd, identical spec)
```

Key properties of mirror pods:

- They are **read-only proxies**. `kubectl describe pod kube-apiserver-master-1` shows you the same spec/status as the local static pod.
- You **cannot `kubectl delete`** the mirror pod to delete the static pod. The delete will appear to succeed; seconds later the kubelet recreates the mirror because the local source still exists. To actually delete a static pod you must remove the file.
- If you edit the mirror with `kubectl edit`, the change is rejected (apiserver admission blocks edits to mirror-pod-managed fields).
- The mirror is garbage-collected when the file is removed (`ownerReferences[0].kind=Node`, plus the kubelet itself deletes the mirror when its source disappears).
- The kubelet hash annotation lets the kubelet detect "the apiserver lost the mirror, recreate it" and "the YAML on disk changed, replace the mirror".

This pattern is one of the cleanest examples of "the kubelet is bidirectional" in the codebase — it consumes the apiserver as a source, but also produces objects there.

---

## 3. The syncLoop: Central Event Loop

The `syncLoop` is one goroutine in the kubelet that owns the *decision* of when to (re-)sync a pod. It does almost no work directly; instead it dispatches to pod workers. But every event that should trigger a pod-level reconcile passes through this loop. If you understand `syncLoop`, you understand kubelet timing.

```go
// pkg/kubelet/kubelet.go (simplified, names match source)
func (kl *Kubelet) syncLoopIteration(
    configCh    <-chan kubetypes.PodUpdate,        // 1. config (apiserver/file/http)
    handler     SyncHandler,                       //    sink for sync calls
    syncCh      <-chan time.Time,                  // 2. periodic full sync (every 1s)
    housekeepingCh <-chan time.Time,               // 3. periodic cleanup (every 2s)
    plegCh      <-chan *pleg.PodLifecycleEvent,    // 4. lifecycle events from runtime
) bool {
    select {
    case u, open := <-configCh:
        // Pod added / updated / removed by a config source.
        switch u.Op {
        case kubetypes.ADD:    handler.HandlePodAdditions(u.Pods)
        case kubetypes.UPDATE: handler.HandlePodUpdates(u.Pods)
        case kubetypes.REMOVE: handler.HandlePodRemoves(u.Pods)
        case kubetypes.RECONCILE: handler.HandlePodReconcile(u.Pods)
        case kubetypes.DELETE: handler.HandlePodUpdates(u.Pods)  // mark for graceful delete
        case kubetypes.SET:    // initial set on startup
        }
    case e := <-plegCh:
        // A container changed state (running/exited/created/died).
        if e.Type == pleg.ContainerDied {
            // GC the dead container, free resources
            kl.cleanUpContainersInPod(e.ID, ...)
        }
        if pod, ok := kl.podManager.GetPodByUID(e.ID); ok {
            handler.HandlePodSyncs([]*v1.Pod{pod})
        }
    case <-syncCh:
        // Periodic full sync of every known pod. Safety net.
        podsToSync := kl.getPodsToSync()
        if len(podsToSync) == 0 { break }
        handler.HandlePodSyncs(podsToSync)
    case update := <-kl.livenessManager.Updates():
        if update.Result == proberesults.Failure {
            handleProbeSync(kl, update, handler, "liveness", "unhealthy")
        }
    case update := <-kl.readinessManager.Updates():
        ready := update.Result == proberesults.Success
        kl.statusManager.SetContainerReadiness(update.PodUID, update.ContainerID, ready)
        handleProbeSync(kl, update, handler, "readiness", boolToReady(ready))
    case update := <-kl.startupManager.Updates():
        started := update.Result == proberesults.Success
        kl.statusManager.SetContainerStartup(update.PodUID, update.ContainerID, started)
        handleProbeSync(kl, update, handler, "startup", boolToStarted(started))
    case <-housekeepingCh:
        // Cleanup orphaned pods, orphan volumes, orphan cgroups.
        if err := handler.HandlePodCleanups(ctx); err != nil { /* retry */ }
    }
    return true
}
```

That `select` is the kubelet's heartbeat. Every channel buys you a different *trigger* for the same fundamental action: "rerun pod sync on the affected pod(s)".

```
                       ┌──────────────────────────────────────────┐
                       │           syncLoop  (1 goroutine)         │
                       │             select { … }                  │
                       └─────────────────────┬─────────────────────┘
                                             │ HandlePod{Additions,Updates,Removes,Syncs}
                                             ▼
                       ┌──────────────────────────────────────────┐
                       │   PodWorkers.UpdatePod(pod, syncType)      │
                       │   - one goroutine per pod UID              │
                       │   - serializes operations on a single pod  │
                       └─────────────────────┬─────────────────────┘
                                             │ chosen path:
                       ┌─────────────────────┼─────────────────────┐
                       ▼                     ▼                     ▼
              ┌────────────────┐    ┌────────────────┐    ┌────────────────┐
              │  syncPod       │    │  syncTerminating│    │ syncTerminated │
              │  (create/run/  │    │  Pod (drain     │    │ Pod (clean up  │
              │   reconcile)   │    │   containers)   │    │  volumes etc.) │
              └────────────────┘    └────────────────┘    └────────────────┘
```

### 3.1 Channel-by-channel breakdown

| Channel | Source | Period | What it triggers |
|---|---|---|---|
| `configCh` | `PodConfig` (apiserver + file + http multiplex) | event-driven | Pod added / updated / removed; the only source of *spec* changes |
| `plegCh` | PLEG (generic relist or evented) | ~1s (generic) / push (evented) | Container started/died/changed; *status* changes from the runtime |
| `syncCh` | `time.Tick` | 1s (`--sync-frequency`) | Periodic full reconcile. Catches anything the event channels missed |
| `housekeepingCh` | `time.Tick` | 2s | Garbage-collect orphaned pods (deleted from apiserver but still in local state), orphan volumes, orphan cgroups |
| `livenessManager.Updates` | per-container probe goroutines | per-probe period | A liveness probe transition; failing one restarts the container |
| `readinessManager.Updates` | per-container probe goroutines | per-probe period | A readiness probe transition; toggles the `ContainersReady` condition and the Service endpoint |
| `startupManager.Updates` | per-container probe goroutines | per-probe period | A startup probe transition; gates the other probes |

The keyword to notice is *trigger*. None of these channels carries the work; they all hand off to pod workers (§4), which run the actual sync. This separation is what lets the kubelet do thousands of pod operations per second without blocking the event loop.

### 3.2 What "sync" actually does

When `HandlePodSyncs([]*v1.Pod{pod})` fires, the chosen pod worker eventually calls `kubelet.SyncPod(ctx, updateType, pod, mirrorPod, podStatus)`. That function is *the* big decision point — see §6. It is idempotent and level-triggered: calling it twice in a row on the same state should produce the same outcome on the second call.

### 3.3 A trace through one syncLoop iteration

Walk the kubelet through a single PLEG-driven sync, so the channels and managers land in your head:

```
T+0.000s   PLEG relist tick fires.
           CRI: ListPodSandbox() returns 47 sandboxes.
           CRI: ListContainers() returns 132 containers.
           diff vs cached state: container "abc123" (pod-X, "main")
             was Running, now Exited (exit code 1).
           emit PodLifecycleEvent{ID: pod-X.UID, Type: ContainerDied,
                                  Data: "abc123"}

T+0.001s   syncLoop blocks in select; the PLEG event wakes it.
           branch: case e := <-plegCh:
             - e.Type == ContainerDied
               → kl.cleanUpContainersInPod(pod-X.UID, "abc123")
                 (removes finished container records older than threshold)
             - kl.podManager.GetPodByUID(pod-X.UID) → pod
             - HandlePodSyncs([]*v1.Pod{pod})

T+0.002s   HandlePodSyncs computes podWork{
             pod:          pod-X,
             updateType:   SyncPodSync,
             mirrorPod:    nil,
           }
           UpdatePod(podWork): pod-X's worker channel already has one update
             queued — coalesce. The pending update is dropped; podWork takes
             its place. Worker is currently busy with the previous sync.

T+0.040s   Worker finishes previous sync. Loops, reads new podWork.
           managePodLoop: pod's state is "running" → run syncPod(...).

T+0.041s   syncPod:
             podStatus := kl.podCache.Get(pod-X.UID)
             podStatus.containerStatuses = [
               {name: "init1", state: Exited(0)},
               {name: "main",  state: Exited(1), exitCode: 1, restartCount: 2},
             ]
             pod.spec.restartPolicy = Always

T+0.042s   computePodActions(pod, podStatus) returns:
             {
               SandboxID: "sandbox-xyz",     // sandbox still running
               CreateSandbox: false,
               KillPod: false,
               NextInitContainerToStart: nil,
               ContainersToStart: [0],       // index of "main" in spec
               ContainersToKill: {},
             }

T+0.043s   Step 4: PullImage("nginx:1.27") — already cached, fast no-op.

T+0.045s   Step 7: CreateContainer + StartContainer for "main":
             CRI: CreateContainer(sandbox=sandbox-xyz, config=...)
                  → ContainerID "def456"
             CRI: StartContainer(def456)
                  → ok

T+0.140s   syncPod returns. Worker marks pod-X clean and waits on channel.

T+0.141s   probeManager (separate goroutine) sees that "main" has changed
           container ID. It restarts the per-container probe worker, which
           begins ticking again with the new container ID.

T+0.180s   Status manager: notice pod's container status changed
             (restartCount 2 → 3, containerID changed). Mark pod as dirty
             in podStatusChannel.

T+0.~10s   Status manager's batch ticker fires.
           PATCH /api/v1/namespaces/.../pods/pod-X/status
             new container status, lastState.terminated{exitCode: 1}
           apiserver records it.
T+~10.05s  Watch fan-out: any controller watching this pod (e.g., the
           Deployment controller via ReplicaSet) sees the restart count
           bump.
```

A few takeaways from this trace:

- The kubelet does ~30–40 ms of CRI work to restart a container, but the status update is delayed up to 10 s by the status manager batch interval. That delay is invisible during steady state but matters during cascading restarts.
- The PLEG event for `ContainerDied` is the *only* signal the kubelet has that the container needs restarting. If PLEG misses an event (rare, but possible during runtime hangs), the periodic `syncCh` tick is the safety net.
- Image was cached — pull was no-op. On a cold image, T+0.045s through `StartContainer` could be many seconds depending on registry latency.

### 3.4 The PodCache: bridging PLEG events and SyncPod inputs

You may notice `syncPod` reads `podStatus` from `kl.podCache.Get()`, *not* from PLEG directly. The `PodCache` (`pkg/kubelet/container/cache.go`) is a small shim:

```
                ┌───────────────────────────────────────────────────────┐
                │  PodCache                                              │
                │                                                       │
                │  per-pod entry:                                       │
                │    timestamp  time.Time                               │
                │    status     *PodStatus                              │
                │    err        error                                   │
                │                                                       │
                │  PLEG writes here every relist with fresh status.     │
                │  syncPod (in pod worker) reads here.                  │
                │  Get() blocks until cache timestamp >= request time.  │
                │                                                       │
                │  Why block? syncPod was triggered by a PLEG event at  │
                │  time T. It needs a status snapshot >= T to be sure   │
                │  it's seeing the event's effects. A stale cache could │
                │  make it act on pre-event state.                      │
                └───────────────────────────────────────────────────────┘
```

This handshake is invisible in the source until you read the cache; understanding it explains why a slow PLEG poisons the whole pod-sync pipeline. Even if syncLoop's other channels keep firing, the pod workers block on `PodCache.Get()` waiting for fresh PLEG data.

### 3.5 Channel sizing and back-pressure

What happens if `syncLoop`'s consumers fall behind? Look at the channel buffer sizes:

- `configCh`: small buffer (~50). Back-pressures `PodConfig`'s informer; the informer slows down processing apiserver events.
- `plegCh`: large buffer (~1000). PLEG never blocks; the events queue up.
- `livenessManager.Updates`, `readinessManager.Updates`, `startupManager.Updates`: per-channel small buffers. Probe workers will drop updates if the channel is full (and probes are level-triggered, so a missed update gets retried on the next probe period).

If `syncLoop` is fully blocked (e.g., a long-running `HandlePodCleanups` call), pod-worker dispatches stop and the node freezes for a brief window. This is rare but does happen during pathological housekeeping runs on nodes with many orphaned cgroups.

---

## 4. Pod Workers: Per-Pod Serialization

The kubelet handles many pods concurrently but serializes operations on a *single* pod. Two reasons:

1. A pod is one object with many side-effects (volumes, network namespace, container processes). Concurrent sync attempts could double-mount, double-start, or fight over the network namespace.
2. The CRI itself is not transactional. `RunPodSandbox` followed by `CreateContainer` is a sequence; you can't have two threads concurrently driving it.

`pkg/kubelet/pod_workers.go` solves this with a per-pod goroutine and a per-pod state machine:

```
                       ┌─────────────────────────────────────────────────────┐
                       │  PodWorkers  (map[UID] → podWorkerState)             │
                       │                                                      │
                       │   UpdatePod(podWork) — the dispatch entry point      │
                       │     if no worker for pod yet → start goroutine       │
                       │     else → push update onto pod's input channel      │
                       │                                                      │
                       └──────────────────┬──────────────────────────────────┘
                                          │ one goroutine per pod UID
                       ┌──────────────────▼──────────────────────────────────┐
                       │  managePodLoop(podUpdates <-chan podWork)            │
                       │                                                      │
                       │   for w := range podUpdates {                        │
                       │     switch state {                                   │
                       │     case running:                                    │
                       │        syncPod(ctx, w.options...)                    │
                       │     case terminating:                                │
                       │        syncTerminatingPod(ctx, ...)                  │
                       │     case terminated:                                 │
                       │        syncTerminatedPod(ctx, ...)                   │
                       │     }                                                │
                       │   }                                                  │
                       └─────────────────────────────────────────────────────┘
```

Important properties:

- **Coalescing.** If five updates arrive while the worker is busy with one, only the latest pending update is kept on the channel (`workQueue` collapses them). You don't replay every intermediate state.
- **Three-phase termination.** A pod that's being deleted goes through `terminating` (preStop hooks + SIGTERM + grace period + SIGKILL) and then `terminated` (volume teardown, cgroup teardown, network teardown) phases. The worker can't go from running directly to gone.
- **Force-kill path.** When a pod is force-deleted (grace period 0), the worker fast-paths through `syncTerminatingPod` with immediate SIGKILL semantics.

### 4.1 SyncPodType

`SyncPodType` is the verb the worker is asked to perform on a pod. Five values:

| Value | Meaning |
|---|---|
| `SyncPodCreate` | First time we're syncing this pod (worker just spawned) |
| `SyncPodUpdate` | Spec changed (e.g., image bump on a static pod, or in-place resize) |
| `SyncPodSync` | Periodic sync — no spec change, just reconcile actual vs desired |
| `SyncPodKill` | Pod is being deleted; drain it |

The worker picks the right phase routine based on a combination of the current state and the requested SyncPodType. In practice, `Create` and `Update` and `Sync` all end in `syncPod`; `Kill` goes through `syncTerminatingPod` → `syncTerminatedPod`.

### 4.2 The pod state machine

```
                ┌─────────────┐
                │   absent    │
                └──────┬──────┘
              UpdatePod(Create)
                       │
                       ▼
                ┌─────────────┐   periodic / event
                │   running   │◄─────────────┐
                │             │              │
                │ syncPod()   │              │
                └──────┬──────┘──────────────┘
            UpdatePod(Kill) │   spec.deletionTimestamp set
                            │   OR
                            │   apiserver REMOVE
                            ▼
                ┌──────────────────┐
                │   terminating    │
                │                  │  preStop hook
                │ syncTerminating  │  SIGTERM → wait → SIGKILL
                │     Pod()        │  containers exit
                └────────┬─────────┘
                         │ all containers gone
                         ▼
                ┌──────────────────┐
                │   terminated     │
                │                  │  unmount volumes
                │ syncTerminated   │  tear down sandbox network (CNI DEL)
                │     Pod()        │  remove cgroup
                └────────┬─────────┘
                         │ all cleanup done
                         ▼
                  ┌─────────────┐
                  │  finished   │  worker exits, removed from map
                  └─────────────┘
```

The worker is the place where you can trust that "things happen in order" for a pod. Everything above it (syncLoop, PLEG, status manager) is best understood as *poking* the worker, not directly mutating pod state.

---

## 5. PLEG: The Pod Lifecycle Event Generator

The kubelet cannot trust the apiserver to tell it what its containers are doing — the apiserver only knows what the kubelet *previously reported*. The truth lives in the runtime. PLEG is the kubelet's bridge to that truth.

### 5.1 Generic PLEG (the original)

`pkg/kubelet/pleg/generic.go`. One goroutine. Every `relistPeriod` (default `1s`), do this:

```
                ┌──────────────────────────────────────────────────────┐
                │  generic PLEG relist tick (every 1s)                  │
                │                                                       │
                │   1. CRI: ListPodSandbox() + ListContainers()         │
                │   2. Build new state: map[podUID] → []containerState  │
                │   3. Diff vs previous state                           │
                │   4. For each diff, emit PodLifecycleEvent:           │
                │        ContainerStarted | ContainerDied               │
                │        ContainerRemoved | PodSync                     │
                │   5. Cache the new state                              │
                │   6. updateRelistTime() — heartbeat                   │
                └──────────────────────────────────────────────────────┘
                                       │
                                       ▼
                            plegCh — consumed by syncLoop
```

The relist heartbeat is also the input to PLEG health monitoring. The kubelet exposes a function `Healthy()` that returns `false` if the last relist completed more than `relistThreshold` ago (default 3 minutes). When that happens, you see the famous error in node events:

```
  Warning  ContainerGCFailed  kubelet  PLEG is not healthy: pleg was last seen active 5m12.345s ago; threshold is 3m0s
```

The `Node.status.conditions[type=Ready]` flips to `False` with reason `KubeletNotReady` and message `PLEG is not healthy`. The node becomes unschedulable. Why? Because if PLEG can't list containers, the kubelet has no idea what's running, can't report status, can't make sync decisions. Better to take the node out of service than to make wrong decisions.

**What causes "PLEG is not healthy"?** Almost always a slow or hung container runtime. Typical culprits:

- Container runtime (containerd) deadlocked on an internal lock (look in containerd's own logs)
- Disk I/O so saturated that runtime CRI calls take >60s each
- A single very large `ListContainers` response (thousands of dead containers not GC'd) timing out
- Image pull blocking with a stuck CNI mount somewhere

This is the #1 reason real-world nodes go `NotReady` under load.

### 5.2 The PLEG state per pod

PLEG maintains a per-pod cache (`pleg/pod_lifecycle_event_cache.go`) keyed by pod UID. Each entry holds the most recent observed state:

```
podRecord {
    pod    *kubecontainer.Pod         // current snapshot
    old    *kubecontainer.Pod         // previous snapshot
    next   *kubecontainer.Pod         // next snapshot (mid-update)
}
```

The diff produces events. Container states form a tiny state machine:

```
            (no record)
                │
                │  ListContainers shows container in Created/Init state
                ▼
            ┌──────────┐
            │  Created │
            └─────┬────┘
                  │  Status.State.Running
                  ▼
            ┌──────────┐
            │  Running │◄──┐ self-loop: still running, no event
            └─────┬────┘   │
                  │        │
                  │ Status.State.Exited
                  ▼        │
            ┌──────────┐   │
            │  Exited  │   │  (restartPolicy=Always & ExitCode!=0
            │  /Dead   │───┘   → runtime creates a new container)
            └─────┬────┘
                  │ container deleted by runtime / image GC
                  ▼
            (removed)
```

The events PLEG emits (`type Type string`):

- `ContainerStarted` — transition (none|exited) → running
- `ContainerDied` — transition running → exited
- `ContainerRemoved` — container removed entirely from runtime
- `ContainerChanged` — state changed in a way the diff sees but doesn't fit above
- `PodSync` — catch-all forced sync (e.g., on initial pod registration)

### 5.3 Evented PLEG (1.27+ beta, 1.29 GA)

The 1s relist period is a scaling problem. At 250 pods per node × 3 containers per pod = 750 containers; `ListContainers` returns all of them every second whether anything changed or not. CPU usage of the kubelet is dominated by this loop on busy nodes.

Evented PLEG (`pkg/kubelet/pleg/evented.go`) replaces the poll with a CRI streaming RPC:

```protobuf
// k8s.io/cri-api/pkg/apis/runtime/v1/api.proto
service RuntimeService {
  // Returns a stream of container events.
  rpc GetContainerEvents(GetEventsRequest) returns (stream ContainerEventResponse) {}
  // …
}

message ContainerEventResponse {
  string container_id = 1;
  ContainerEventType container_event_type = 2;
  int64 created_at = 3;
  PodSandboxStatus pod_sandbox_status = 4;
  repeated ContainerStatus containers_statuses = 5;
}
```

The runtime (containerd 1.7+, CRI-O 1.27+) pushes container state changes (create/start/exit/delete) over this stream. The kubelet processes them as they arrive, with a slow safety-net relist every 5 minutes to catch drift.

The win at scale is dramatic: kubelet CPU drops by 30–60% on busy nodes, and the "PLEG is not healthy" alert essentially disappears because relists are no longer in the hot path. Fallback: if the runtime doesn't support `GetContainerEvents`, the kubelet automatically falls back to the generic relist loop.

Enable with the `EventedPLEG` feature gate (`--feature-gates=EventedPLEG=true`). Verify with:

```
$ kubectl get --raw /api/v1/nodes/<node>/proxy/metrics | grep kubelet_pleg
kubelet_pleg_relist_duration_seconds_count   …
kubelet_pleg_events_count_total{type="EventedPLEG"} …
```

### 5.4 Why generic PLEG is fundamentally limited

The poll loop has three failure modes evented fixes:

1. **Latency.** A container that crashes 100 ms after the last relist won't be observed for ~900 ms. That's 900 ms where readiness/liveness logic sees an obsolete state.
2. **CPU cost scales with #containers, not #events.** Idle nodes pay the same.
3. **Single big RPC.** One slow `ListContainers` blocks everything. With evented streaming, slow processing on one container's status doesn't block others.

For new clusters in 2026, enable evented PLEG and never look back.

### 5.5 The evented PLEG diagram

```
                        Generic PLEG (default)                     Evented PLEG (1.27+)
                        ──────────────────────                     ─────────────────────

   ┌────────────┐                              ┌────────────┐
   │ kubelet    │ relist tick every 1s         │ kubelet    │ subscribe once,
   │ (1 GR)     │ ──────────────────────►      │ (1 GR)     │ receive forever
   └─────┬──────┘                              └─────┬──────┘
         │                                            │
         │ ListPodSandbox()                           │ GetContainerEvents() (server stream)
         │ ListContainers()                           │ ◄─── event ─── (push)
         ▼                                            │ ◄─── event ─── (push)
   ┌─────────────┐                              ┌────▼──────┐
   │  runtime    │ scan ALL containers          │  runtime   │ push only on
   │             │ build response               │            │ state transition
   └─────────────┘                              └────────────┘

   per-tick cost:                               per-event cost:
     O(N containers)                              O(1)
   per-second cost:                             per-second cost:
     O(N) regardless of churn                     O(events_per_second)

   latency to observe a transition:             latency:
     up to 1s (next relist)                       ~few ms (push)
```

The runtime-side change to support evented PLEG is small (containerd implements it via its existing event bus). The kubelet still falls back to generic PLEG if the streaming RPC returns `Unimplemented`. There is no correctness change from operator's perspective; it's a pure efficiency upgrade.

### 5.6 Useful PLEG metrics

```
kubelet_pleg_relist_duration_seconds              histogram of relist duration
kubelet_pleg_relist_interval_seconds              histogram of time between relists (~1s expected)
kubelet_pleg_last_seen_seconds                    unix time of the last successful relist
kubelet_pleg_events_count_total{type=...}         counter of emitted events by type
```

`kubelet_pleg_relist_interval_seconds_bucket{le="3"}` should be effectively 100% in a healthy node. If you see it drop below 99%, look at runtime latency.

`kubelet_pleg_last_seen_seconds` is the metric that drives the "PLEG is not healthy" condition. Alert when `(time() - kubelet_pleg_last_seen_seconds) > 60`.

---

## 6. The Full Pod Sync: computePodActions and SyncPod

`SyncPod` is the function that, given a pod's *desired* spec and *observed* CRI state, decides what to do. It is the kubelet's biggest decision point. Reading `pkg/kubelet/kuberuntime/kuberuntime_manager.go` — specifically `computePodActions()` and `SyncPod()` — is worth a quiet afternoon.

The logic, distilled:

```
                    ┌──────────────────────────────────────────────────┐
                    │  SyncPod(pod, podStatus, ...)                     │
                    │                                                  │
                    │  ┌────────────────────────────────────────────┐  │
                    │  │ Step 1: computePodActions(pod, podStatus)  │  │
                    │  │                                            │  │
                    │  │   inputs:                                  │  │
                    │  │     pod.Spec.Containers / InitContainers   │  │
                    │  │     pod.Spec.RestartPolicy                 │  │
                    │  │     podStatus (from PLEG cache)            │  │
                    │  │                                            │  │
                    │  │   outputs (podActions struct):             │  │
                    │  │     KillPod          bool                  │  │
                    │  │     CreateSandbox    bool                  │  │
                    │  │     SandboxID        string                │  │
                    │  │     Attempt          uint32                │  │
                    │  │     NextInitContainerToStart *Container    │  │
                    │  │     ContainersToStart  []int               │  │
                    │  │     ContainersToKill   map[ID]ContainerToKill│
                    │  │     EphemeralContainers []int              │  │
                    │  └────────────────┬───────────────────────────┘  │
                    │                   ▼                              │
                    │  Step 2: Kill containers in ContainersToKill    │
                    │  Step 3: Create sandbox if requested            │
                    │  Step 4: Pull images (parallel within pod)      │
                    │  Step 5: Start NextInitContainer (one at a time)│
                    │  Step 6: Start native sidecars (1.28+)          │
                    │  Step 7: Start app containers in ContainersToStart│
                    └──────────────────────────────────────────────────┘
```

### 6.1 The decision tree (simplified)

```
Is the pod's sandbox running and matches pod.Spec.HostNetwork etc.?
  │
  ├── NO  → KillPod = true; CreateSandbox = true; Attempt++; restart everything
  │
  └── YES, sandbox OK
      │
      Are all init containers terminated successfully?
      │
      ├── NO, one is still running   → wait, do nothing
      │
      ├── NO, one failed             → restartPolicy decides:
      │      Always       → restart it
      │      OnFailure    → restart it
      │      Never        → mark pod Failed, kill remaining
      │
      ├── NO, none started yet       → start init[0]
      │
      ├── PARTIAL                    → start next init in order
      │
      └── YES, all init done OK
            │
            For each native sidecar (restartPolicy=Always init container, 1.28+):
              if not running → start; runs in parallel with app containers
            │
            For each app container in spec.Containers (in order):
              ├── currently Running, spec unchanged
              │       └─ leave it
              ├── currently Running, spec changed (image/env/etc.)
              │       └─ ContainersToKill[i] (will recreate)
              ├── currently Exited
              │       └─ restartPolicy decides:
              │           Always    → ContainersToStart += i
              │           OnFailure → ContainersToStart += i if ExitCode!=0
              │           Never     → leave it, pod will go Succeeded/Failed
              └── never created
                      └─ ContainersToStart += i
```

### 6.2 Init containers and ordering

Init containers run **sequentially**, in `spec.initContainers` order. The next one cannot start until the previous one terminates successfully. This is a strict serial chain.

Native sidecars (introduced as `restartPolicy: Always` on an init container, stable in 1.29) break this: they are conceptually init containers (they appear in the `initContainers` array) but they don't have to terminate before the next one starts, and they keep running through the pod's app-container phase.

```
init-1 (regular)           start ──exit──┐
                                          │
init-2 (sidecar, RP=Always) start ────────┴───── runs through entire pod lifetime
                                          │
init-3 (regular)                          start ──exit──┐
                                                         │
app-1                                                    start ──┐
app-2                                                            start ──┐
                                                                          ...
                                                          (containers in spec.containers order)
```

Termination order is reverse: app containers stopped first (with `preStop` and grace period), then sidecars stopped in reverse init order. This is enforced by the kubelet during `syncTerminatingPod`.

### 6.3 Idempotency

`SyncPod` is called many times per pod lifetime — on every PLEG event affecting the pod, every probe transition, every spec change, every periodic resync. It must do nothing if nothing changed. The way `computePodActions` is structured (compute everything by comparing observed-vs-desired, then act on the diff) makes that property fall out naturally.

The hidden trap: state that lives outside the runtime — host directories, sysctls, kernel parameters — is *not* part of the diff. A `syncPod` that thinks it has nothing to do may have a half-mounted volume from a crashed prior attempt. The volume manager (§10) handles that with its own reconciliation loop.

### 6.4 Worked example: a pod with 1 init, 1 sidecar, 2 app containers

Consider:

```yaml
apiVersion: v1
kind: Pod
metadata: {name: web, namespace: default}
spec:
  restartPolicy: Always
  initContainers:
    - name: migrate                       # regular init: runs to completion
      image: app:1.0
      command: ["./migrate.sh"]
    - name: proxy                          # native sidecar (restartPolicy: Always)
      image: envoy:1.30
      restartPolicy: Always
  containers:
    - name: app                            # app container
      image: app:1.0
    - name: metrics                        # app container (could be a sidecar conceptually)
      image: prom-exporter:1.0
```

Startup sequence (timeline):

```
T=0    syncLoop sees pod ADD. PodWorker spawned.
T=10   syncPod: computePodActions:
         no sandbox yet → CreateSandbox=true, Attempt=1
T=15   CRI: RunPodSandbox returns sandbox id
T=20   CRI: PullImage(app:1.0)         } parallel
       CRI: PullImage(envoy:1.30)       } parallel
       CRI: PullImage(prom-exporter:1.0)} parallel
       (kubelet limits concurrency per --max-parallel-image-pulls)
T=80   all images cached.
T=82   CRI: CreateContainer + StartContainer "migrate" (init[0])
T=85   syncPod returns; nothing more to do until migrate exits.

T=120  PLEG observes migrate Exited(0). syncLoop queues SyncPodSync.
T=130  syncPod: computePodActions:
         all regular init containers done up to migrate? yes.
         next init: "proxy" (sidecar).
         start sidecars first, before app containers.
       CRI: CreateContainer + StartContainer "proxy"
T=140  proxy reports Running via PLEG.

T=145  syncPod (next pass): all init+sidecars started? yes.
       ContainersToStart = [0 (app), 1 (metrics)].
       CRI: CreateContainer + StartContainer "app"
       CRI: CreateContainer + StartContainer "metrics"

T=150  All containers running. probeManager starts probe workers for each.
T=160  Readiness gates resolve, status manager PATCHes pod.status:
         conditions: PodScheduled=True, Initialized=True,
                     ContainersReady=True, Ready=True
```

Now imagine "app" crashes 5 minutes in:

```
T=300  PLEG: ContainerDied(app, exit=1).
T=300  syncLoop: HandlePodSyncs.
T=300.05  syncPod: computePodActions:
            restartPolicy=Always, ExitCode=1
            ContainersToStart = [0 (app)]
          CRI: CreateContainer + StartContainer "app"
T=300.1   New container id; probeManager swaps workers.
T=300.2   restartCount in pod.status: 0 → 1 (batched within 10s).
          "proxy" and "metrics" untouched.
```

If `app` had restartPolicy `Never` and exited with code 0, computePodActions would mark it `done`, and once all *regular* (non-sidecar) containers in `spec.containers` complete, the pod transitions to `Succeeded`. The sidecar `proxy` would be terminated by the kubelet when the pod is wound down.

### 6.5 The "ContainersToKill" reasons

When the kubelet decides to kill a container that's currently running, it records *why* on each entry. The reasons land in pod events and are worth recognizing:

| Reason | When triggered |
|---|---|
| `Container is dead` | The runtime reports it dead but it's still in our state |
| `Container failed liveness probe` | livenessManager said so |
| `Container failed startup probe` | startupManager said so |
| `Container spec hash changed` | Image, env, command, or another spec field changed |
| `Container failed to start` | A previous `StartContainer` errored; tearing down for retry |
| `Pod is terminating` | spec.deletionTimestamp set; cleanup in progress |

The `Container spec hash changed` reason is how in-place container restarts (e.g., bumping `image` on a static pod's YAML) get triggered. The kubelet computes a hash of the container's relevant spec fields and stores it as an annotation on the running container; when the hash differs from the spec's hash, kill-and-recreate.

---

## 7. CRI Call Order During Pod Startup

The kubelet's communication with the runtime is over CRI gRPC (`k8s.io/cri-api`). For a fresh pod, the sequence is:

```
                 ┌─────────────────────────────────────────────────────────┐
                 │  Pod startup CRI sequence  (kuberuntime/kuberuntime_manager) │
                 └─────────────────────────────────────────────────────────┘

   kubelet                                      container runtime (containerd)
   ───────                                      ──────────────────────────────
       │
       │  1. RunPodSandbox(PodSandboxConfig)
       │ ─────────────────────────────────────► creates "pause" container
       │                                        - creates net/uts/ipc namespaces
       │                                        - kubelet calls CNI ADD before
       │                                          (or runtime calls CNI itself,
       │                                           depending on shim model)
       │  ◄───── PodSandboxID + IP ───────────
       │
       │  2. PullImage(image) — in parallel for multi-container pods
       │ ─────────────────────────────────────►
       │ ─────────────────────────────────────► registry auth (imagePullSecrets)
       │                                        layer downloads + unpack
       │  ◄───── ImageRef ────────────────────
       │  ◄───── ImageRef ────────────────────
       │
       │  3. CreateContainer(PodSandboxID, ContainerConfig)
       │ ─────────────────────────────────────► OCI bundle prepared
       │                                        cgroup + namespace settings
       │                                        in config.json
       │  ◄───── ContainerID ──────────────────
       │
       │  4. StartContainer(ContainerID)
       │ ─────────────────────────────────────► runc create → exec entrypoint
       │  ◄───── ok ───────────────────────────
       │
       │  (repeat 3-4 per init container, in order)
       │  (then repeat 3-4 per native sidecar)
       │  (then repeat 3-4 per app container, in spec order)
       │
```

Notes:

- **Sandbox first.** The pause container's only job is to hold the pod's namespaces. App containers `join` those namespaces; if the pause container dies, the namespaces collapse. (This is why a stuck pause container takes the whole pod down.)
- **Image pull is parallelized within the pod**, but serialized across pods by default (`--serialize-image-pulls=true`). On modern multi-core nodes you want `false` plus `--max-parallel-image-pulls=N`.
- **Init containers create and start one at a time** because the next one cannot run until the previous exited successfully.
- **App containers create concurrently** but order in the spec matters for sidecar conventions (e.g., the proxy sidecar typically comes first).

### 7.1 Shutdown order

```
   kubelet                                      container runtime
   ───────                                      ─────────────────
       │
       │  app containers (reverse spec order):
       │   for each:
       │     ExecSync(preStop)   — if hook configured
       │     StopContainer(ID, timeoutSeconds = grace)
       │ ────────────────────────────────────►  SIGTERM
       │                                        wait up to grace
       │                                        SIGKILL if not exited
       │  ◄───── ok ───────────────────────────
       │
       │  native sidecars (reverse init order):
       │     same as above
       │
       │  StopPodSandbox(PodSandboxID)
       │ ────────────────────────────────────►  stop pause container
       │                                        kubelet calls CNI DEL
       │                                        (or runtime does)
       │
       │  RemovePodSandbox(PodSandboxID)
       │ ────────────────────────────────────►  delete bundle + cgroup
       │
       │  (volume manager unmounts; cgroup teardown; status PATCH "Succeeded"/"Failed")
```

`terminationGracePeriodSeconds` (default 30s) on the pod is the upper bound on how long the kubelet waits for an app container to exit gracefully. If the app's process ignores SIGTERM, the kubelet eventually sends SIGKILL — but the time spent waiting is real and shows up as slow rolling restarts and slow node drains.

---

## 8. Probes: Startup, Readiness, Liveness

Probes are per-container health checks. The kubelet runs them on a schedule and reacts to the result.

```
                        ┌────────────────────────────────────────────────┐
                        │  probeManager   (pkg/kubelet/prober/manager.go)│
                        │                                                │
                        │   for each running container × probe type:     │
                        │     a worker goroutine ticks at probe.PeriodSeconds │
                        │       runs the probe (httpGet | tcpSocket |    │
                        │                       exec | grpc)             │
                        │       posts result to startup/readiness/       │
                        │       liveness manager                         │
                        └──────────┬─────────────────────────────────────┘
                                   │
                  ┌────────────────┼────────────────┐
                  ▼                ▼                ▼
            startupManager   readinessManager   livenessManager
                  │                │                │
                  │ Updates() chan │ Updates() chan │ Updates() chan
                  ▼                ▼                ▼
                                syncLoop
```

### 8.1 The three probe types and their effects

| Probe | Effect of FAILURE | Effect of SUCCESS | Effect of "not running yet" |
|---|---|---|---|
| **Startup** | After `failureThreshold` consecutive failures, **kill+restart** the container (per restartPolicy). | Marks container "started". Disables itself. **Enables** liveness + readiness. | Liveness + readiness probes are *not run* yet; container is treated as "still starting". |
| **Readiness** | Container removed from Service endpoints; pod `Ready` condition false. **No restart.** | Container included in Service endpoints; pod `Ready` true if all containers ready. | (Until startup probe passes, readiness is treated as `Success` for the purpose of "still booting" — but `ContainersReady` stays false.) |
| **Liveness** | After `failureThreshold` consecutive failures, **kill+restart** the container. | Container considered healthy. | Not run until startup probe passes. |

**The startup probe is the bug-fix probe.** Before startup probes existed (added in 1.16), the only way to give a slow-booting app room to start was to set a long `initialDelaySeconds` on its liveness probe — which then permanently delayed liveness detection for the rest of the container's life. Startup probes separate "is it booting?" from "is it healthy?".

### 8.2 Probe handler types

```yaml
livenessProbe:
  # 1) HTTP GET
  httpGet:
    path: /healthz
    port: 8080
    scheme: HTTP        # or HTTPS
    httpHeaders:
      - name: X-Probe
        value: kubelet
  # 2) TCP socket — succeeds if connect() succeeds
  # tcpSocket:
  #   port: 5432
  # 3) Exec — runs a command inside the container, exit code 0 = pass
  # exec:
  #   command: ["/bin/sh", "-c", "pg_isready -U postgres"]
  # 4) gRPC (1.27+, stable in 1.27) — uses grpc.health.v1.Health
  # grpc:
  #   port: 9090
  #   service: ""
  initialDelaySeconds: 5
  periodSeconds:       10
  timeoutSeconds:      1
  successThreshold:    1      # liveness must be 1
  failureThreshold:    3
```

A few subtleties most operators get wrong:

- `exec` probes **fork a process inside the container's namespaces**. On busy nodes, the fork+exec cost adds up; on a node with 250 pods each running an `exec` probe every 5 seconds, that's 50 forks/second of additional work.
- `httpGet` probes are issued **by the kubelet** to the *pod's* IP, *bypassing* Services. They don't traverse iptables/IPVS. Failing a probe is unrelated to whether kube-proxy works.
- `grpc` probes (since 1.27) require the container to implement the `grpc.health.v1.Health` service. They're a real win for gRPC apps because previously you'd run a separate sidecar like grpc-health-probe.
- **Probes against `localhost` from inside the container** are a misconception — the kubelet probes from the **kubelet's** netns, not the container's. The IP it uses is `pod.status.podIP`. (Exec probes are the exception — they run inside the container.)

### 8.3 Common probe misconfigurations and their failure mode

| Misconfig | Failure mode |
|---|---|
| `livenessProbe` checks deep DB connectivity | DB hiccup restarts every pod simultaneously → outage amplified, not contained |
| `livenessProbe` with `timeoutSeconds: 1` against an HTTP server that GCs for 1.2s occasionally | Random restarts under load |
| No `readinessProbe`, only `livenessProbe` | Traffic hits not-yet-ready pods during rollout |
| `livenessProbe` and `readinessProbe` are the same | Slow-but-healthy pod gets restarted instead of just being removed from endpoints |
| `initialDelaySeconds: 300` to "give it time to start" | Pod takes 300s to detect a real liveness failure for the rest of its lifetime; should use `startupProbe` instead |
| Probe path requires auth | Always fails, container restart-looped |
| Probe against `0.0.0.0` instead of the actual port | Listens but probe times out (kubelet probes podIP, not localhost) |

The general rule:

> **Liveness = "is the process so broken that restart will help?".** Readiness = "should I receive traffic right now?". Startup = "am I done initializing?". They should answer different questions.

### 8.4 The probe state machine

```
                       ┌─────────────────────┐
                       │  container started   │
                       └──────────┬──────────┘
                                  │
                       ┌──────────▼──────────┐
                       │ startup probe        │   yes ─► startup probe disabled
                       │ configured?          │          enable liveness + readiness
                       └──────────┬──────────┘          probes immediately
                                  │ yes
                                  ▼
                       ┌────────────────────────────────────────────────────┐
                       │  startup probe ticks at periodSeconds                │
                       │    ├── success — flip "started" flag in startupManager│
                       │    │            stop running startup probe           │
                       │    │            now liveness + readiness probes start│
                       │    │            no liveness restarts before this    │
                       │    └── failureThreshold consecutive failures        │
                       │              ↓                                      │
                       │            kill container (per restartPolicy)       │
                       └────────────────────────────────────────────────────┘
                                  │
                                  ▼ (started=true)
                       ┌────────────────────────────────────────────────────┐
                       │  liveness probe ticks                                │
                       │    ├── success — no action (or recovery from prior)  │
                       │    └── failureThreshold consecutive failures        │
                       │              ↓                                      │
                       │            kill container (per restartPolicy)       │
                       └────────────────────────────────────────────────────┘
                       
                       ┌────────────────────────────────────────────────────┐
                       │  readiness probe ticks                               │
                       │    ├── success — set containerStatus.ready = true   │
                       │    │            include in Service endpoints         │
                       │    └── failure — set containerStatus.ready = false  │
                       │              ↓ no kill, just deregister              │
                       │            EndpointSlice controller removes pod     │
                       │            from Service endpoints                    │
                       └────────────────────────────────────────────────────┘
```

The two probes that *restart* (startup, liveness) and the one that *deregisters* (readiness) are the entire contract.

### 8.5 Failure threshold and timing math

Time to detect liveness failure:

```
detection_time = (failureThreshold - 1) * periodSeconds + (1 * periodSeconds_with_timeout)
               ≈ failureThreshold * periodSeconds   (worst-case)
```

With defaults (`periodSeconds=10, failureThreshold=3`), it takes ~30 seconds to restart a stuck container. For latency-critical workloads with fast failover, set `periodSeconds=5, failureThreshold=2` → 10s detection. But beware: flakier networks may produce false positives at those tighter thresholds.

Time to be considered Ready:

```
ready_time = startup_threshold_passes + readiness_threshold_passes
           = startupProbe.successThreshold * startupProbe.periodSeconds
             + readinessProbe.successThreshold * readinessProbe.periodSeconds
```

A `readinessProbe.successThreshold` greater than 1 is one of the few cases where >1 makes sense (require the probe to be consistently green before exposing). For `livenessProbe.successThreshold`, the kubelet *requires* it to be 1 — recovery is always immediate.

---

## 9. Status Manager

The status manager (`pkg/kubelet/status/status_manager.go`) is the kubelet's single point of contact with the apiserver for *writing* pod status. Every other subsystem (PLEG, probes, eviction, volume manager) calls *into* the status manager; the status manager batches and PATCHes.

```
                                  ┌────────────────────────────────────────┐
                                  │   statusManager                         │
                                  │                                        │
   probeManager   ───SetReady()──►│   podStatusChannel (buffered)         │
   PLEG           ───SyncStatus──►│         │                              │
   volumeManager  ───SetVolume───►│         │                              │
   evictionMgr    ───MarkEvict───►│         ▼                              │
                                  │   syncBatch goroutine (every 10s)      │
                                  │     for each dirty UID:                │
                                  │       compare cached status vs apiserver│
                                  │       PATCH /api/v1/namespaces/.../   │
                                  │         pods/<name>/status            │
                                  └────────────────────────────────────────┘
                                              │
                                              ▼
                                        kube-apiserver
                                        (Node authorizer:
                                         this kubelet may only
                                         patch its own pods' status)
```

### 9.1 What pod.status contains

```yaml
status:
  phase: Running            # Pending | Running | Succeeded | Failed | Unknown
  conditions:
    - type: PodScheduled    # set by scheduler, not kubelet
      status: "True"
    - type: Initialized     # all init containers done
      status: "True"
    - type: ContainersReady # all app containers ready
      status: "True"
    - type: Ready           # = ContainersReady AND all readinessGates true
      status: "True"
  hostIP: 10.0.1.42
  hostIPs: [{ip: 10.0.1.42}, {ip: fd00::42}]
  podIP:  10.244.3.17
  podIPs: [{ip: 10.244.3.17}, {ip: fd00::3:17}]
  qosClass: Burstable
  startTime: "2026-05-23T14:00:00Z"
  containerStatuses:
    - name: nginx
      ready: true
      restartCount: 0
      image: nginx:1.27
      imageID: docker.io/library/nginx@sha256:abc…
      containerID: containerd://0xdeadbeef…
      state:
        running: {startedAt: "2026-05-23T14:00:01Z"}
      lastState: {}
  initContainerStatuses: [...]
```

### 9.2 Who writes what

This is the rule: **only the kubelet writes `pod.status`**. The Node authorizer + NodeRestriction admission (chapter 07) enforces this. Specifically, a kubelet for `node-X` may only write to pods whose `spec.nodeName=node-X`.

Inside the kubelet:

- `phase` is computed from container states (`kubelet_pods.go: getPhase()`).
- `conditions[Initialized/ContainersReady]` are computed from container statuses.
- `conditions[Ready]` is `ContainersReady AND all spec.readinessGates true`. Readiness gates allow external controllers (like an Istio sidecar's startup gate) to influence pod readiness.
- `conditions[PodScheduled]` is set by the scheduler — but the kubelet may patch it if it must.
- `podIP/podIPs` is set after CNI ADD returns.

### 9.3 Why batching matters

A noisy pod (one with frequent probe transitions or restart loops) could generate dozens of status changes per second. Without batching, that's dozens of PATCHes to the apiserver. With 5000 nodes × 100 pods/node, the apiserver write load would crush etcd.

`statusManager` therefore:
- Coalesces multiple updates per UID — keeps only the *latest* version
- Syncs at a fixed cadence (default ~10s) plus event-triggered flushes for high-priority transitions
- Skips no-op updates (cached status equals apiserver status)

If you ever see `kubectl get pod` show stale information, the kubelet has it locally — it just hasn't pushed yet.

---

## 10. Volume Manager

Volumes are the most state-heavy piece of pod startup. The volume manager (`pkg/kubelet/volumemanager/`) follows the classic "Desired State of World (DSW) vs Actual State of World (ASW) + reconciler" pattern, mirroring the rest of the K8s control plane.

```
                ┌─────────────────────────────────────────────────────────────┐
                │  volumeManager                                              │
                │                                                             │
                │   ┌─────────────────┐         ┌─────────────────────┐      │
                │   │  DSW populator   │         │  ASW                │      │
                │   │                  │         │                     │      │
                │   │ scans pods,      │         │ what is actually    │      │
                │   │ expands their    │         │ attached & mounted  │      │
                │   │ spec.volumes →   │         │ on this node        │      │
                │   │ DSW              │         │                     │      │
                │   └────────┬─────────┘         └─────────┬───────────┘      │
                │            │                              │                  │
                │            ▼                              ▼                  │
                │           ┌────────────────────────────────────────┐        │
                │           │  reconciler (loop, every 100ms)         │        │
                │           │                                         │        │
                │           │   for each volume in DSW not in ASW:    │        │
                │           │     AttachVolume (CSI ControllerPublish)│        │
                │           │     MountVolume:                        │        │
                │           │       NodeStageVolume   (CSI)           │        │
                │           │       NodePublishVolume (CSI)           │        │
                │           │     update ASW                          │        │
                │           │                                         │        │
                │           │   for each volume in ASW not in DSW:    │        │
                │           │     UnmountVolume                       │        │
                │           │       NodeUnpublishVolume               │        │
                │           │       NodeUnstageVolume                 │        │
                │           │     DetachVolume (ControllerUnpublish)  │        │
                │           └────────────────────────────────────────┘        │
                └─────────────────────────────────────────────────────────────┘
```

### 10.1 The CSI flow (forward ref to ch 19)

For an in-tree CSI volume, the steps the kubelet drives are:

```
1. ControllerPublishVolume     — out-of-process controller plugin (or external-attacher)
                                  attaches the volume to the *node* (e.g., AWS attaches EBS volume)
2. NodeStageVolume             — on the node, prepare a global mount
                                  /var/lib/kubelet/plugins/kubernetes.io/csi/<driver>/<volId>/globalmount
3. NodePublishVolume           — bind-mount the global mount into the pod's volume dir
                                  /var/lib/kubelet/pods/<podUID>/volumes/kubernetes.io~csi/<volName>/mount
4. (containers see this path mounted at their spec.containers[].volumeMounts[].mountPath)
```

Teardown is the reverse: `NodeUnpublishVolume` → `NodeUnstageVolume` → `ControllerUnpublishVolume`.

Two operations the volume manager does *separately* from the reconciler:

- **`VerifyControllerAttachedVolume`** — confirms the external attacher has actually attached the volume before the kubelet tries to mount.
- **`actualStateOfWorld` rebuild on startup** — when the kubelet restarts, it scans `/var/lib/kubelet/pods/*/volumes/*` and queries CSI drivers to rebuild ASW. This is why a kubelet restart doesn't unmount everything.

### 10.2 The volume manager's failure modes

| Symptom | Cause |
|---|---|
| Pod stuck in `ContainerCreating` for minutes | CSI controller plugin can't attach (cloud quota, IAM, attach limit per node) |
| Pod stuck terminating | CSI node plugin pod itself is gone; kubelet can't call `NodeUnpublishVolume`. Workaround: force-delete with `--grace-period=0` *after* manually unmounting |
| `MountVolume.MountDevice failed` | `NodeStageVolume` failed; usually file system check or wrong fsType |
| Two pods stuck on RWO volume during rollout | `ReadWriteOncePod` would help — `ReadWriteOnce` historically allowed mount across pods on the same node |

The "CSI node plugin down" failure is a tricky bootstrap problem: the CSI node plugin itself usually runs as a DaemonSet. If it can't start (e.g., its image isn't pulled yet), the kubelet can't mount any volumes — including the volumes the CSI plugin pod might need. Production CSI plugins are designed to need only hostPath/emptyDir.

---

## 11. CNI Integration

The kubelet doesn't have a built-in network stack. Instead, the CNI spec defines a contract: the kubelet (or its CRI shim) executes a plugin binary from `/opt/cni/bin/` with a JSON config from `/etc/cni/net.d/`. The plugin attaches the sandbox to the network.

```
                     ┌──────────────────────────────────────────────────┐
                     │  Pod startup: network setup                       │
                     └──────────────────────────────────────────────────┘

  kubelet (or CRI shim, depending on implementation):
    1. Create pod sandbox (pause container) with net namespace
    2. Read /etc/cni/net.d/*.conflist (first lexicographic match)
    3. For each plugin in the chain:
         exec plugin binary
         stdin:
           {
             "cniVersion": "1.0.0",
             "name": "calico",
             "type": "calico",
             "ipam": {...},
             "containerID": "abcdef…",
             "netns": "/proc/12345/ns/net",
             "ifName": "eth0",
             ...
           }
         stdout (success):
           {
             "ips": [{"address": "10.244.3.17/24", "gateway": "10.244.3.1"}],
             "routes": [...],
             "dns": {...}
           }
    4. Capture podIP from the result; pass to status manager
    5. Container runtime starts pause container; app containers join its netns
```

Whether *the kubelet* or *the runtime* runs CNI depends on the runtime. With containerd's default shim, the runtime itself runs CNI (using libcni embedded in containerd) — the kubelet just supplies the netconf path. With CRI-O, similar. Either way, the kubelet is the source of truth for *when* CNI should run.

Teardown is symmetric: CNI `DEL` is called when the sandbox is stopped, before `RemovePodSandbox`.

See chapter 15 for the full CNI deep-dive (overlay vs underlay, IPAM, dual-stack, NetworkPolicy enforcement, the Cilium socket-LB datapath).

---

## 12. Device Manager

GPUs, RDMA NICs, FPGAs, SR-IOV VFs, Intel QAT, NVMe namespaces — none of these are first-class in K8s. They're exposed through the **device plugin framework**: a gRPC contract between the kubelet and a per-device-type plugin (typically running as a DaemonSet).

### 12.1 Registration

```
                   ┌─────────────────────────────────────────────────┐
                   │  Kubelet device manager                          │
                   │  (pkg/kubelet/cm/devicemanager)                  │
                   │                                                  │
                   │   kubelet plugin registration server:            │
                   │     /var/lib/kubelet/plugins_registry/           │
                   │       <plugin>.sock                              │
                   │                                                  │
                   │   on registration:                               │
                   │     plugin tells kubelet:                         │
                   │       - name (e.g., "nvidia.com/gpu")            │
                   │       - endpoint (its gRPC socket)               │
                   │       - API version                              │
                   │     kubelet starts a ListAndWatch stream         │
                   └──────────────────────────┬──────────────────────┘
                                              │
                                              ▼
                                  ┌────────────────────────┐
                                  │  device plugin pod       │
                                  │  (e.g., NVIDIA daemonset)│
                                  │                          │
                                  │  ListAndWatch():         │
                                  │    streams [Device{      │
                                  │      ID: "GPU-0",        │
                                  │      Health: Healthy     │
                                  │    }, ...]               │
                                  │                          │
                                  │  Allocate(devIDs):       │
                                  │    returns env vars,     │
                                  │    device mounts,        │
                                  │    annotations needed by │
                                  │    the runtime           │
                                  └────────────────────────┘
```

### 12.2 What appears on the Node

The kubelet updates `Node.status.capacity` and `Node.status.allocatable`:

```yaml
capacity:
  cpu: "32"
  memory: "256Gi"
  nvidia.com/gpu: "8"          # <- device plugin advertised these
  intel.com/qat: "16"
allocatable:
  cpu: "31"
  memory: "248Gi"
  nvidia.com/gpu: "8"
  intel.com/qat: "16"
```

The scheduler then sees `nvidia.com/gpu: 8` and can schedule pods that request `nvidia.com/gpu: 1` to this node.

### 12.3 The Allocate RPC

When a pod requesting `nvidia.com/gpu: 2` lands on the node and is about to start, the kubelet calls `Allocate({ContainerRequests: [{DevicesIDs: ["GPU-0","GPU-3"]}]})`. The plugin responds with everything the container needs to use those devices:

```protobuf
message AllocateResponse {
  repeated ContainerAllocateResponse container_responses = 1;
}

message ContainerAllocateResponse {
  map<string,string> envs        = 1;  // e.g., NVIDIA_VISIBLE_DEVICES=GPU-0,GPU-3
  repeated DeviceSpec devices    = 2;  // /dev paths to bind-mount with permissions
  repeated Mount mounts          = 3;  // additional host paths (driver libs)
  map<string,string> annotations = 4;  // hints to the runtime (e.g., nvidia runtime hook)
}
```

The kubelet merges these into the CRI `CreateContainer` call. The runtime sets up the device cgroup and bind-mounts at container start. The application inside the container then sees `/dev/nvidia0` and `/dev/nvidia3`.

State file: `/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint`. Survives kubelet restart: which devices are allocated to which container.

### 12.4 ListAndWatch and health

`ListAndWatch` is a server-streaming RPC. The plugin pushes a new full list whenever a device's state changes (e.g., a GPU goes unhealthy). The kubelet:

- Updates `allocatable` (subtracts unhealthy devices)
- If a container has an unhealthy device assigned, the container is not automatically restarted (the kubelet logs the event; recovery is up to a workload controller)

---

## 13. CPU Manager

By default, every container's `cpu.weight` is set proportional to its CPU requests, and `cpu.max` is set to its CPU limit (CFS quota). All containers share the same set of CPUs. For latency-sensitive workloads, this isn't enough — CFS scheduling decisions can cause tail-latency spikes when a Guaranteed pod's threads share CPUs with bursting BestEffort pods.

The CPU manager (`pkg/kubelet/cm/cpumanager`) lets you **pin** Guaranteed-class pods to dedicated CPUs.

### 13.1 Policies

`--cpu-manager-policy=` (default `none`):

| Policy | Behavior |
|---|---|
| `none` | All containers in the shared CPU pool (default cgroup `cpuset` = all CPUs). |
| `static` | For Guaranteed-QoS pods with integer CPU requests, allocate exclusive CPUs via cgroup `cpuset.cpus`. Other containers (Burstable, BestEffort, Guaranteed with fractional CPU) stay in the shared pool, which shrinks as exclusive CPUs are taken. |

Static policy in action:

```
   Node has 16 CPUs.
   ────────────────────────────────────────────────────────
   Shared pool start: {0,1,2,...,15}

   Pod A (Guaranteed, cpu req=2, limit=2)
     → kubelet picks {0,1} (NUMA-aware selection, see §15)
     → A's cgroup: cpuset.cpus = "0,1"
     → shared pool now {2,3,...,15}

   Pod B (Burstable, cpu req=1, limit=4)
     → stays in shared pool
     → B's cgroup: cpuset.cpus = "2,3,...,15"  (the entire shared pool)

   Pod C (Guaranteed, cpu req=4, limit=4)
     → kubelet picks {2,3,4,5} (avoid 0,1; prefer same NUMA as A if possible)
     → C's cgroup: cpuset.cpus = "2,3,4,5"
     → shared pool now {6,7,...,15}
     → B's cpuset shrinks to "6,7,...,15"  (shared pool updated on every change)

   Pod D (Guaranteed, cpu req=0.5, limit=0.5)
     → fractional request — stays in shared pool
```

### 13.2 The state file

`/var/lib/kubelet/cpu_manager_state` — a JSON file that records which container has which CPUs. Survives kubelet restart:

```json
{
  "policyName": "static",
  "defaultCpuSet": "6-15",
  "entries": {
    "<podUID-A>": {"main": "0-1"},
    "<podUID-C>": {"main": "2-5"}
  },
  "checksum": 123456789
}
```

On kubelet restart, the state is reloaded, the policy is re-applied to running containers, and the shared pool is recomputed.

### 13.3 What you give up

- Pods with `cpu.request != cpu.limit` (so not Guaranteed) cannot get exclusive CPUs.
- Pods with fractional CPU requests cannot get exclusive CPUs.
- Once allocated, CPU IDs do not move — until the pod terminates. If you have many small Guaranteed pods, the shared pool fragments.
- The Linux scheduler may still schedule kernel threads (`ksoftirqd`, `migration`) on "exclusive" CPUs. For true isolation, combine with `isolcpus=` and `nohz_full=` boot parameters and the `IRQBalance` discipline.

### 13.4 Config

```yaml
# /var/lib/kubelet/config.yaml
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cpuManagerPolicy: static
cpuManagerReconcilePeriod: 10s
reservedSystemCPUs: "0,1"     # not allocatable to any pod; for kubelet + kernel
kubeReserved:
  cpu: 500m
  memory: 1Gi
systemReserved:
  cpu: 500m
  memory: 1Gi
```

`reservedSystemCPUs` is the production lever: dedicate specific cores to the system (and pin kubelet + container runtime there), and let pods use everything else.

---

## 14. Memory Manager

The memory manager (`pkg/kubelet/cm/memorymanager`, 1.21+ GA in 1.32) is the memory analog of the CPU manager. Its only real job is **NUMA-aware memory allocation** for Guaranteed pods, so that the memory a pod uses comes from the same NUMA node as its pinned CPUs.

### 14.1 Policies

`--memory-manager-policy=` (default `None`):

| Policy | Behavior |
|---|---|
| `None` | The kernel decides where memory lives. With CPU pinning, this is *not enough* — the kernel may allocate memory from a remote NUMA node, costing 20–80 ns per access. |
| `Static` | For Guaranteed pods, the manager pre-reserves NUMA-aligned memory and tells the kernel via cgroup `cpuset.mems` to use that NUMA node. |

### 14.2 Without and with the memory manager

```
   Node: 2 NUMA nodes, 64GB each.
   ────────────────────────────────────────────────────────

   Without memory manager:
     Pod (Guaranteed, cpu pinned to {0,1} on NUMA0, memory=4Gi)
       cpuset.cpus = "0,1"       — CPU pinned
       cpuset.mems = "0,1"       — kernel free to use either node
     → kernel allocates 50% of pages from NUMA1
     → cross-NUMA accesses; tail latency spike

   With memory manager (Static):
     Pod (Guaranteed, cpu pinned to {0,1} on NUMA0, memory=4Gi)
       cpuset.cpus = "0,1"
       cpuset.mems = "0"         — kernel must allocate from NUMA0
     → all pages local; consistent latency

   The memory manager keeps a NUMA-node accounting ledger:
     NUMA0: 60Gi available, 4Gi reserved for pod-X
     NUMA1: 64Gi available
   When a new pod wants 16Gi on NUMA0, manager checks the ledger.
```

### 14.3 State file

`/var/lib/kubelet/memory_manager_state` — like the CPU manager's, persists assignments across restart.

### 14.4 Hugepages

Hugepages (`hugepages-2Mi`, `hugepages-1Gi` resources) also go through the memory manager when Static is enabled. The manager tracks per-NUMA hugepage availability and aligns allocations with CPUs.

---

## 15. Topology Manager

CPU manager and memory manager each make their own allocation decision. Without coordination, you can end up with CPUs on NUMA0 + memory on NUMA1 + GPU on NUMA1 — a fragmented placement that defeats the point. The topology manager (`pkg/kubelet/cm/topologymanager`) is the **arbiter** that gets all hint providers to agree on a NUMA node before any allocation happens.

### 15.1 Hint providers

Three currently:

- **CPU Manager** — for each request, returns which NUMA nodes can satisfy `N` exclusive CPUs.
- **Memory Manager** — returns which NUMA nodes have enough memory.
- **Device Manager** — returns which NUMA nodes the requested devices (GPUs) are on.

Each hint is a bitmask of NUMA nodes plus a `Preferred` flag.

### 15.2 Policies

`--topology-manager-policy=` (default `none`):

| Policy | Behavior |
|---|---|
| `none` | No coordination. Each manager decides on its own. |
| `best-effort` | Pick the NUMA mask that's the AND of all preferred hints; if empty, accept the pod anyway. |
| `restricted` | Pick the AND; if empty *or* not preferred, **reject** the pod with `TopologyAffinityError`. Failed pods go to `Failed`/`Terminated`. |
| `single-numa-node` | Like restricted but require the resulting mask to fit on a single NUMA node. |

`--topology-manager-scope=`:

| Scope | Behavior |
|---|---|
| `container` (default) | Hints evaluated per container. Different containers in the same pod can land on different NUMA nodes. |
| `pod` | Hints evaluated for the whole pod's combined request. All containers land on the same NUMA mask. |

### 15.3 Hint flow

```
                  Pod requests: cpu=4, memory=8Gi, nvidia.com/gpu=1
                  ──────────────────────────────────────────────────

                  Topology Manager: "give me hints"
                       │ broadcasts to all hint providers
                       ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  CPU manager hints  (4 CPUs needed)                                  │
   │    NUMA mask 0b01 (NUMA0):  4 free, preferred=true                   │
   │    NUMA mask 0b10 (NUMA1):  4 free, preferred=true                   │
   │    NUMA mask 0b11 (both):   4 free across, preferred=false           │
   │                                                                      │
   │  Memory manager hints  (8Gi needed)                                  │
   │    NUMA mask 0b01: 16Gi free, preferred=true                         │
   │    NUMA mask 0b10:  2Gi free, preferred=false                        │
   │    NUMA mask 0b11: 18Gi free, preferred=false                        │
   │                                                                      │
   │  Device manager hints  (1 GPU needed, GPU lives on NUMA1)           │
   │    NUMA mask 0b10: 1 GPU free, preferred=true                        │
   │    NUMA mask 0b11: 1 GPU free, preferred=false                       │
   └─────────────────────────────────────────────────────────────────────┘
                       │
                       ▼
   Topology manager merges:
     Candidate masks = intersection of all providers' "available" masks.
       0b01 ∩ 0b01 ∩ 0b10 = 0b00   → no
       0b01 ∩ 0b01 ∩ 0b11 = 0b01   → CPU & MEM yes, DEV "yes via 0b11"  → 0b01 is feasible IF device manager allows 0b11
       0b10 ∩ 0b10 ∩ 0b10 = 0b10   → all preferred
       0b11 ∩ 0b11 ∩ 0b11 = 0b11   → feasible, all non-preferred
     Pick best: 0b10 — single NUMA, all hint providers preferred it.

   single-numa-node policy: 0b10 ✓ (single bit set)
   best-effort:             0b10 ✓
   restricted:              0b10 ✓ (preferred)

   Now Topology Manager tells each provider "use mask 0b10":
     CPU manager: pick 4 CPUs on NUMA1
     Memory manager: allocate 8Gi from NUMA1
     Device manager: pick the GPU on NUMA1
```

If memory had been 2Gi only on NUMA1 and the policy was `single-numa-node`, the only candidate would be the cross-NUMA mask `0b11`, the policy would reject, and the pod would be marked `Failed` with reason `TopologyAffinityError`. The scheduler then has to retry on another node.

### 15.4 Worth it?

For HPC, latency-sensitive trading, ML training with high inter-GPU bandwidth, telco DPDK workloads — yes, this is mandatory. For general microservices — leave it at `none`. The configuration cost of getting topology right (and the operational cost of pods being rejected as Failed) is real.

### 15.5 Container vs pod scope: a worked example

Two-container pod, one needs 2 CPUs + 1 GPU, one needs 2 CPUs (no GPU). Node: 2 NUMA nodes, the only GPU is on NUMA1.

**Scope = container:**
- Container A (2 CPUs + GPU): preferred NUMA mask 0b10 (NUMA1) — that's where the GPU is.
- Container B (2 CPUs):       preferred NUMA mask 0b01 OR 0b10 — no GPU, either is fine.
- Result: A pinned to NUMA1 CPUs, B pinned to NUMA0 CPUs. Pod spans NUMA, but each container's *own* work is local.

**Scope = pod:**
- Combined request: 4 CPUs + 1 GPU.
- The GPU forces NUMA1.
- Both containers' CPUs come from NUMA1.
- If NUMA1 doesn't have 4 free exclusive CPUs, the policy rejects.

Pod scope is much stricter; container scope is the default and almost always what you want unless the containers share a lot of state (e.g., the same shared memory segment).

### 15.6 Observability

The topology manager emits the `topology_manager_admission_*` metrics:

```
topology_manager_admission_requests_total           total hint evaluations
topology_manager_admission_errors_total{type=...}   rejections by reason
topology_manager_admission_duration_ms              histogram of decision time
```

When the policy rejects a pod, you'll see a Pod event:
```
Warning  TopologyAffinityError  kubelet  Resources cannot be allocated with Topology locality
```
followed by the pod being marked `Failed`. The scheduler controller (or a higher-level operator) is responsible for retrying.

---

## 16. Eviction Manager

When a node runs out of a critical resource (memory, disk, inodes, PIDs), waiting for the kernel to OOM-kill processes is too late: a memory-only OOM picks a victim by `oom_score_adj`, which may not be what you want, and it doesn't free disk or PIDs at all. The eviction manager (`pkg/kubelet/eviction`) is the kubelet's **proactive** layer: detect pressure, pick a pod, evict it cleanly.

### 16.1 Signals

`--eviction-hard=` and `--eviction-soft=` accept threshold expressions on these signals:

| Signal | Source | Default hard threshold (typical) |
|---|---|---|
| `memory.available` | `/proc/meminfo` (MemAvailable) minus reserved | `< 100Mi` |
| `nodefs.available` | filesystem usage where `/var/lib/kubelet` lives | `< 10%` |
| `nodefs.inodesFree` | inode pressure on the same | `< 5%` |
| `imagefs.available` | filesystem where the runtime stores images (often same as nodefs) | `< 15%` |
| `imagefs.inodesFree` | imagefs inodes | `< 5%` |
| `pid.available` | `/proc/sys/kernel/pid_max` minus current pids | `< 10%` |
| `allocatableMemory.available` | memory available minus what is requested by pods | (computed for soft) |

### 16.2 Hard vs soft thresholds

```
   Hard threshold:
     - When signal crosses threshold, evict immediately
     - Selected pod gets a *zero* grace period — straight to SIGKILL
     - Default: hard MUST be tighter than soft
     - Tunable: --eviction-pressure-transition-period (default 5m)

   Soft threshold:
     - When signal crosses, *and stays crossed* for --eviction-soft-grace-period,
       evict
     - Selected pod gets up to --eviction-max-pod-grace-period for preStop+SIGTERM
     - Allows graceful drain when the node is just "getting tight"
```

Config:

```yaml
# kubelet config
evictionHard:
  memory.available:   "100Mi"
  nodefs.available:   "10%"
  nodefs.inodesFree:  "5%"
  imagefs.available:  "15%"
  pid.available:      "10%"
evictionSoft:
  memory.available:   "200Mi"
  nodefs.available:   "15%"
evictionSoftGracePeriod:
  memory.available:   "1m"
  nodefs.available:   "1m"
evictionMaxPodGracePeriod: 60
```

### 16.3 The eviction decision tree

```
                ┌──────────────────────────────────────────────────────────┐
                │  eviction manager loop  (every 10s)                       │
                │                                                          │
                │   1. Collect signals (cAdvisor + summary)                 │
                │   2. Compute pressure conditions for each threshold       │
                │   3. If any hard threshold crossed:                       │
                │        → evict NOW                                        │
                │      else if any soft threshold crossed AND duration ≥    │
                │      grace:                                               │
                │        → evict GRACEFULLY                                 │
                │      else:                                                │
                │        → set/clear node conditions (MemoryPressure,       │
                │          DiskPressure, PIDPressure)                       │
                └──────────────────────────────────────────────────────────┘

   Selecting a pod to evict:
     1. Filter: only "evictable" pods (no static pods, no critical SystemNodeCritical)
     2. Sort by:
          a. QoS class:   BestEffort     (evict first)
                          Burstable      (next, sorted by overuse below)
                          Guaranteed     (last resort)
          b. Within Burstable: sort by *memory usage above request*
                               (the more over, the higher priority to evict)
             For disk pressure: sort by *local ephemeral storage above request*
          c. Tiebreak: priority (lower priority evicted first), then pod age
     3. Evict the top pod:
          for memory pressure → kill all containers, set status to Failed
                                 with reason "Evicted"
          for disk pressure   → ditto, plus before that, ImageGC + ContainerGC
     4. Re-check signal; if still pressured, evict next
```

### 16.4 Node conditions and taints

When pressure persists, the kubelet sets `Node.status.conditions`:

- `MemoryPressure: True` — also adds `node.kubernetes.io/memory-pressure:NoSchedule` taint, blocking new BestEffort pods.
- `DiskPressure: True` — adds `node.kubernetes.io/disk-pressure:NoSchedule`.
- `PIDPressure: True` — adds `node.kubernetes.io/pid-pressure:NoSchedule`.

The scheduler reacts to these taints and stops sending new pods to a pressured node.

### 16.5 Worked example: a node under memory pressure

```
   Node: 32Gi total, kubeReserved+systemReserved = 2Gi.
   evictionHard: memory.available<500Mi
   evictionSoft: memory.available<1Gi   (gracePeriod 30s, maxPodGrace 60s)

   Pods on node:
     P1 (Guaranteed, request=limit=8Gi, using 7Gi)
     P2 (Burstable,  request=2Gi limit=8Gi, using 6Gi)
     P3 (Burstable,  request=1Gi limit=4Gi, using 3.5Gi)
     P4 (Burstable,  request=512Mi limit=2Gi, using 1.8Gi)
     P5 (BestEffort, no requests, using 800Mi)
     P6 (BestEffort, no requests, using 500Mi)

   Total used: 19.6Gi. Plus 2Gi reserved. Plus 0.5Gi kernel caches.
   memory.available = 32 - 22.1 ≈ 9.9Gi. All quiet.

   T+0:    A traffic spike pushes P2 to 11Gi (within its 8Gi limit? NO — P2
           was set with limit=8Gi). Actually let's say P2 spikes to 8Gi
           (its limit). Now total: 22.6Gi. avail: ~9Gi. Still fine.

   T+30:   P3 leaks. Climbs to 4Gi (its limit). total: 23.1Gi. avail: ~8Gi.

   T+60:   Kernel caches grow because workloads do disk IO. avail: 900Mi.
           Soft threshold (1Gi) crossed.
           eviction loop: signal crossed but only just; grace timer starts.

   T+90:   Still under 1Gi. 30s grace satisfied.
           Pick eviction target:
             BestEffort group first.
             P5: 800Mi usage.
             P6: 500Mi usage.
             → P5 (higher usage) evicted first.
           PreStop hooks run, SIGTERM, up to 60s grace.

   T+92:   P5's containers gone. ~800Mi freed.
           avail: 1.7Gi. Recovered above soft threshold.

   T+120:  Another spike. P3 keeps growing. avail: 400Mi.
           Hard threshold (500Mi) crossed.
           No grace. Immediate eviction.
           Pick: BestEffort first → P6 (only remaining BestEffort) → SIGKILL.

   T+121:  ~500Mi freed. avail: ~900Mi. Crossed back over hard, below soft.
           Burstable taint added: node.kubernetes.io/memory-pressure:NoSchedule.

   T+150:  Spike persists. avail drops to 400Mi again. Hard re-triggered.
           No BestEffort pods left. Pick Burstable:
             P2 usage above request: 8 - 2 = 6Gi (rank score 6Gi)
             P3 usage above request: 4 - 1 = 3Gi (rank score 3Gi)
             P4 usage above request: 1.8 - 0.5 = 1.3Gi (rank score 1.3Gi)
           → P2 evicted (highest overage). SIGKILL.

   T+151:  ~8Gi freed. avail: ~8.5Gi. Pressure cleared.
           memory-pressure condition stays True until
           --eviction-pressure-transition-period (5m default) elapses.
```

Notes from the trace:

- BestEffort goes first regardless of how much memory they use, because they have no request — they're explicitly "I'm cheap, kill me first".
- Among Burstable, the metric is *usage above request*, not absolute usage. P2 was Burstable but using 6Gi over its 2Gi request, so it ranks highest.
- Guaranteed pod P1 was never touched. To evict a Guaranteed pod, eviction has to exhaust all Burstable options too.
- After eviction, the node carries the `MemoryPressure` taint for a 5-minute window even after recovery, blocking new pods that wouldn't tolerate it. This prevents a flapping scheduling pattern.

### 16.6 Common eviction mistakes

| Mistake | Consequence |
|---|---|
| Setting hard thresholds with no monitoring | Pods evicted randomly under burst; no idea why |
| Setting soft thresholds without configuring grace period | Soft thresholds ignored |
| Eviction-hard memory < real memory headroom needed by system | Kubelet evicts under any spike |
| No `kubeReserved`/`systemReserved` | Eviction signals look "fine" until kernel OOMs the kubelet itself |
| All pods BestEffort | Every memory spike evicts random workloads |

The full picture of resources and QoS is chapter 21; here, just internalize that **the eviction manager is the kubelet's hand on the lever, and the only way to keep the node alive when limits are wrong**.

---

### 16.7 The cgroup hierarchy the kubelet manages

The kubelet creates and maintains a cgroup hierarchy under `--cgroup-root` (default `/`). On a systemd-cgroup-driver node (the modern default), the layout under `/sys/fs/cgroup/` is:

```
/sys/fs/cgroup/
├── kubepods.slice/                         # all pods live here
│   ├── kubepods-besteffort.slice/          # cgroup for QoS BestEffort tier
│   │   ├── kubepods-besteffort-pod<UID>.slice/
│   │   │   ├── cri-containerd-<id>.scope/  # one container
│   │   │   ├── cri-containerd-<id>.scope/
│   │   │   └── ...
│   │   └── ...
│   ├── kubepods-burstable.slice/           # cgroup for QoS Burstable tier
│   │   └── kubepods-burstable-pod<UID>.slice/
│   │       └── ...
│   └── kubepods-pod<UID>.slice/            # Guaranteed pods live at top level
│       └── cri-containerd-<id>.scope/
│
├── system.slice/                           # kubelet, runtime, system daemons
│   ├── kubelet.service/
│   ├── containerd.service/
│   └── ...
└── user.slice/                              # per-user (irrelevant for the kubelet)
```

The kubelet:

- Creates `kubepods.slice` with limits derived from `(node capacity) - (kubeReserved) - (systemReserved) - (evictionHard.memory)`. This is the "allocatable" envelope.
- Creates `kubepods-besteffort.slice` and `kubepods-burstable.slice` as middle tiers so kernel OOM and reclaim happen *within* a QoS tier first.
- Creates a per-pod slice for each pod, with limits computed from the pod's resource requests/limits.
- The container runtime (containerd, CRI-O) creates the per-container scopes inside the pod's slice.

This three-level nesting (qos → pod → container) is why an OOM in a Burstable pod doesn't preempt Guaranteed pods: the kernel's cgroup-OOM stays within the deepest cgroup that's over its limit.

`--cgroup-driver=systemd` vs `--cgroup-driver=cgroupfs`: in cgroupfs mode the kubelet manipulates `/sys/fs/cgroup` directly; in systemd mode it asks systemd to create slices via D-Bus. The modes must match between the kubelet *and* the container runtime, or the runtime tries to put containers in cgroups the kubelet didn't create and things get bizarrely broken.

---

## 17. OOM Kill Behavior

Even with the eviction manager running, the kernel may OOM-kill before the kubelet has a chance. Two distinct events to keep separated in your head:

```
   Kubelet eviction (proactive)              Kernel OOM kill (reactive)
   ─────────────────────────────             ────────────────────────────
   Trigger: signal threshold                 Trigger: cgroup memory.max
            crossed (memory.available)                hit, or global OOM
                                                      condition
   Frequency: every 10s                     Frequency: immediate
   Victim:    chosen by QoS + over-request  Victim: highest oom_score in
              ranking                                eligible set
   Granularity: pod                          Granularity: process
   Cleanup:   full pod lifecycle (preStop,   Cleanup: none; the kernel kills
              status update, volumes)                 the process and that's it
   Visibility: pod status                    Visibility: dmesg + container
              "Reason: Evicted"                        ExitCode 137 in pod status
```

When a container's memory cgroup hits `memory.max`:

1. The kernel runs the **OOM killer**, restricted to processes in that cgroup.
2. It selects the process with the highest `oom_score`. (`oom_score` is computed from RSS + `oom_score_adj`.)
3. It sends SIGKILL.
4. The runtime sees the process die, marks the container Exited with ExitCode 137 (= 128 + SIGKILL signal 9).
5. PLEG sees the death and emits `ContainerDied`.
6. The kubelet's syncLoop wakes, runs `SyncPod`, and (per restartPolicy) restarts the container.

You can see this in pod status:

```yaml
containerStatuses:
- name: app
  restartCount: 3
  lastState:
    terminated:
      exitCode: 137
      reason: OOMKilled
      startedAt: ...
      finishedAt: ...
```

The OOM kill is *cgroup-local*. A misbehaving container that hits its own limit only kills itself; it doesn't affect the rest of the pod (unless the pod's `shareProcessNamespace: true` or the pause container itself is killed, which would tear down the whole pod).

A **global OOM** (when the *entire node* runs out of memory, not just one cgroup) is more dangerous: the kernel scans every process and kills the worst offender. If the kubelet itself is killed, the node goes `NotReady` until systemd restarts it.

---

## 18. OOM Score Adjustment

To influence which process the kernel picks under OOM, Linux exposes `/proc/<pid>/oom_score_adj` (range `-1000` to `+1000`). The kubelet writes a value per container based on QoS, so that the kernel preferentially kills less-important pods first.

### 18.1 The formula

```
QoS class      oom_score_adj
─────────      ─────────────
Guaranteed     -997
Burstable      1000 - (1000 × memory_request_bytes / node_memory_capacity_bytes)
                clipped to [2, 999]
BestEffort     1000
```

(Plus a few specials: pause container at `-998`, kubelet itself at `-999`, and system-cluster-critical pods at `-997`.)

### 18.2 The table for a 16Gi node

```
Container                                    Effective oom_score_adj
─────────────────────────────────────────    ────────────────────────
kubelet                                                  -999
pause container                                          -998
Guaranteed pod (req=limit, no class override)            -997
SystemNodeCritical pod                                  -997
Burstable pod, request 8Gi  on 16Gi node                  500    (1000 - 500)
Burstable pod, request 1Gi  on 16Gi node                  938    (1000 - 62)
Burstable pod, request 100Mi on 16Gi node                 994    (1000 - 6)
BestEffort pod                                           1000
```

So under global OOM pressure: BestEffort dies first, then Burstable in order of "uses more memory than its share would suggest", and Guaranteed dies last (and only if everything else is gone). Pause containers and the kubelet are essentially un-killable.

### 18.3 Reading it on the node

```
$ ps -ef | grep nginx
root     12345  ... nginx: master process ...
$ cat /proc/12345/oom_score_adj
938
$ cat /proc/12345/oom_score
850          # actual score (RSS-weighted + adjusted)
```

This is one of those subsystems most operators never look at — until the kernel kills the wrong thing in a node-OOM, and then everyone wishes they understood it before the postmortem.

---

## 19. Image Garbage Collection

Container images take disk. The kubelet runs `imageGC` (`pkg/kubelet/images/image_gc_manager.go`) to keep image-storage utilization under a threshold.

### 19.1 Thresholds

```yaml
# kubelet config
imageGCHighThresholdPercent: 85   # GC kicks in when imagefs > 85% full
imageGCLowThresholdPercent: 80    # GC stops when imagefs < 80% full
imageMinimumGCAge: 2m             # don't delete images younger than this
imageMaximumGCAge: 0s             # 1.30+: max age regardless of usage (0 = off)
```

### 19.2 The loop

```
                Image GC tick (every 5 minutes)
                ───────────────────────────────

   1. Query CRI: ImageFsInfo → usage%, inodes%
   2. If usage% < highThreshold → done.
   3. List images via CRI: ListImages
   4. For each image, look at "last used time":
        - If currently referenced by a container → unevictable, skip
        - If pulled less than imageMinimumGCAge ago → skip
        - Else: candidate. Use "last used" timestamp.
   5. Sort candidates by last used (oldest first)
   6. Delete oldest until usage% < lowThreshold
        - CRI: RemoveImage(imageRef)
```

Notes:

- The kubelet tracks "last used" for an image (when it was last referenced by any container). When a container exits, its image's last-used time is updated to now. So an image with a recently-exited container won't be GC'd immediately.
- An image that was pulled by the kubelet (e.g., via an `ImagePullSecret`) but *never* used by any container can still be GC'd once `imageMinimumGCAge` has elapsed.
- **Pinned images** (an image marked `pinned: true` via the CRI) are never GC'd. The runtime can pin the pause image so it survives GC and is always available.

### 19.3 Container GC

Distinct from image GC. The kubelet also removes *dead containers* (exited container records) at `--container-gc-threshold-*` settings — by default, keeping up to 1 dead instance per container per pod, up to 5 dead pods, up to 240 total dead containers. Dead containers occupy inodes and slow `ListContainers`.

---

## 20. Container Log Management

The CRI spec defines a log file format and path. The kubelet itself does not write container logs — the runtime does. But the kubelet *configures* the runtime (and reads the logs back when you `kubectl logs`).

### 20.1 The on-disk layout

```
/var/log/pods/<namespace>_<pod-name>_<pod-uid>/
└── <container-name>/
    ├── 0.log         # current rotation
    ├── 0.log.20260523-140000-1.gz
    └── 0.log.20260523-130000-2.gz

Each line is JSON per CRI spec:
{"log":"hello world\n","stream":"stdout","time":"2026-05-23T14:00:00.123Z"}
```

### 20.2 Rotation

Rotation is performed **by the container runtime**, not the kubelet. But the kubelet *tells* the runtime when to rotate via the CRI:

```yaml
# kubelet config
containerLogMaxSize:  "10Mi"      # rotate file when it reaches 10 MiB
containerLogMaxFiles: 5           # keep 5 files; older deleted
```

containerd reads these from the kubelet's CRI config request and applies them. CRI-O the same.

### 20.3 The legacy `/var/log/containers/` symlinks

For backward compat with logging agents (fluent-bit, fluentd) that predate the `/var/log/pods/` layout, the kubelet (well, the runtime) maintains symlinks:

```
/var/log/containers/<pod>_<namespace>_<container>-<container-id>.log
   → /var/log/pods/<ns>_<pod>_<uid>/<container>/0.log
```

A log-shipper DaemonSet can mount `/var/log/containers/` and discover logs by parsing the symlink names.

### 20.4 `kubectl logs`

When you `kubectl logs <pod>`, the apiserver proxies the request to the *kubelet*, which serves the contents of `0.log` (and its rotated siblings) over its `/containerLogs` endpoint (§23). The kubelet doesn't ship logs anywhere itself — that's the job of an external log pipeline.

---

## 21. Authentication to the apiserver

The kubelet is a client of the apiserver. Like any client, it authenticates with a credential, and what it can do is bounded by authorization. Forward-ref chapter 07 for full detail.

### 21.1 The credential

Two paths:

- **Bootstrap TLS** (kubeadm and most managed clusters): the kubelet starts with a *bootstrap token* (`--bootstrap-kubeconfig`), uses it to authenticate to the apiserver, submits a CertificateSigningRequest, gets back a client cert signed by the cluster CA with user `system:node:<node-name>` in group `system:nodes`.
- **Static client cert** (rare, manual setup): a long-lived client cert with the same identity, written into `--kubeconfig`.

Either way, the kubelet ends up with `/var/lib/kubelet/pki/kubelet-client-current.pem`, a symlink to the current cert/key bundle.

### 21.2 The authorizer: Node + NodeRestriction

The apiserver runs two relevant authorization modes for kubelet requests:

- **Node authorizer**: a special authorizer that grants a kubelet identified as `system:node:<name>` read access only to Secrets, ConfigMaps, PVs, and Pods *that are referenced by pods scheduled to its node*. (Implemented via a graph of object references in the apiserver.)
- **NodeRestriction admission**: limits *writes*. A kubelet may only:
  - Modify its own Node object (status, conditions, taints — only specific ones)
  - Modify status of pods bound to its node
  - Create mirror pods for static pods on its node
  - Not touch pods on other nodes, not create regular pods, not modify random objects

This is what stops a compromised kubelet from harvesting all cluster secrets or impersonating other nodes. Without NodeRestriction, a node compromise was effectively a cluster compromise.

### 21.3 Configuration

```yaml
# kubelet authentication+authorization to *clients of the kubelet's own API*
# (separate from the kubelet's identity as a client of the apiserver)
authentication:
  x509:
    clientCAFile: /etc/kubernetes/pki/ca.crt
  webhook:
    enabled: true
    cacheTTL: 2m
  anonymous:
    enabled: false                  # NEVER set true
authorization:
  mode: Webhook
  webhook:
    cacheAuthorizedTTL:   5m
    cacheUnauthorizedTTL: 30s
```

Anonymous-auth was historically a major footgun: enabling it lets anyone with network access to `:10250` exec into containers. Always `enabled: false`.

---

## 22. The /metrics, /metrics/cadvisor, /metrics/resource Endpoints

The kubelet exposes several Prometheus-formatted metrics endpoints. Each one serves a different audience.

```
                   ┌──────────────────────────────────────────────────────┐
                   │  Kubelet HTTPS server (:10250)                        │
                   │                                                       │
                   │   /metrics                — kubelet itself             │
                   │     • syncLoop iteration durations                    │
                   │     • PLEG relist latency                              │
                   │     • probe results                                    │
                   │     • pod_worker durations                            │
                   │     • volume_manager actions                          │
                   │     • Go runtime + process metrics                     │
                   │                                                       │
                   │   /metrics/cadvisor       — container resource usage  │
                   │     • container_cpu_usage_seconds_total                │
                   │     • container_memory_usage_bytes                     │
                   │     • container_fs_reads_bytes_total                   │
                   │     • container_network_receive_bytes_total            │
                   │     (one series per container, scraped by Prometheus)  │
                   │                                                       │
                   │   /metrics/resource       — slim aggregate of above   │
                   │     • node_cpu_usage_seconds_total                     │
                   │     • container_memory_working_set_bytes               │
                   │     (intended for metrics-server, ~5% of cadvisor size)│
                   │                                                       │
                   │   /metrics/probes         — probe results             │
                   │     • prober_probe_total{type,result}                  │
                   │                                                       │
                   │   /stats/summary          — JSON, metrics-server input │
                   │     (per-pod + node summary in one document)           │
                   └──────────────────────────────────────────────────────┘
```

### 22.1 cAdvisor lives inside the kubelet

cAdvisor (Container Advisor) used to be a separate process; since 1.7+ it is *compiled into* the kubelet. It walks the cgroup tree (`/sys/fs/cgroup/...`) and the runtime's container metadata to derive per-container resource usage. On cgroups v2 it reads `memory.current`, `memory.stat`, `cpu.stat`, `io.stat`, etc.

cAdvisor is the underlying data source for both `/metrics/cadvisor` (Prometheus format) and `/stats/summary` (JSON aggregate). The latter is what `metrics-server` scrapes; the former is what your full Prometheus scrapes for fine-grained data.

### 22.2 Scrape strategy at scale

`/metrics/cadvisor` produces ~50 series per container. On a node with 250 pods × 3 containers, that's 37,500 series per scrape per node. A 5000-node cluster: 187M series. Most of this is noise — only a fraction of the metrics are ever queried.

The recommendation in production is: scrape `/metrics/cadvisor` from a smaller relabeling-aggressive Prometheus, drop labels that explode cardinality (`pod`, `container`, `image` are unavoidable; `boot_id`, `host`, anything per-pid is gratuitous), and keep retention low.

### 22.3 The Node heartbeat and lease

Separate from pod-status reporting, the kubelet has to *announce its own liveness* to the cluster. This is the **node heartbeat**, and how it works changed significantly over Kubernetes history.

Pre-1.13:
- Kubelet PATCHed `Node.status` every 10 seconds. A full status patch is ~10 KB. At 5000 nodes × 0.1 Hz = 500 writes/s × 10 KB = 5 MB/s into etcd just for heartbeats.

1.13+:
- Two heartbeats:
  - **Node status update**: still happens, but only every `--node-status-update-frequency` (default 10s) *or* when status materially changes (e.g., a condition flips). On steady state this PATCH is rare.
  - **Lease**: a separate `coordination.k8s.io/v1/Lease` object in the `kube-node-lease` namespace, one per node. Tiny (~50 bytes). Updated at `--node-status-update-frequency` (10s default). Renewed via `Lease.spec.renewTime`.
- The node-lifecycle controller (in kube-controller-manager) watches the Lease, not the Node, to decide when to mark a Node `NotReady`.

The default ratio is: full Node status update every `nodeStatusUpdateFrequency × nodeStatusReportFrequency = 10s × 5 = 50s`, plus event-triggered. Lease renew every 10s.

If the controller hasn't seen a lease renewal in `--node-monitor-grace-period` (default 40s), the Node is marked `NotReady`. After `--pod-eviction-timeout` (default 5m) without recovery, pods on that node are evicted (set `deletionTimestamp`; they re-schedule elsewhere).

### 22.4 cAdvisor data sources

cAdvisor (now `internal/cAdvisor`) collects metrics from many sources:

```
   ┌─────────────────────────────────────────────────────────────┐
   │  cAdvisor inside the kubelet                                  │
   │                                                              │
   │  Per-container data sources (per cgroup):                    │
   │    /sys/fs/cgroup/<...>/cpu.stat       → cpu.usage_usec      │
   │    /sys/fs/cgroup/<...>/memory.current → RSS+cache           │
   │    /sys/fs/cgroup/<...>/memory.stat    → working set, etc.   │
   │    /sys/fs/cgroup/<...>/io.stat        → bytes/iops per dev  │
   │    /sys/fs/cgroup/<...>/pids.current   → process count       │
   │                                                              │
   │  Per-container network (via runtime or netns):                │
   │    /proc/<pause-pid>/net/dev → ifInBytes, ifOutBytes         │
   │                                                              │
   │  Container filesystem (overlayfs):                           │
   │    statfs on the rootfs upperdir                             │
   │                                                              │
   │  Container metadata (from CRI):                              │
   │    image, container name, pod name, namespace, labels        │
   │                                                              │
   │  Polling interval: --housekeeping-interval (default 10s)      │
   └─────────────────────────────────────────────────────────────┘
```

The "working set" memory cAdvisor reports is `memory.usage_in_bytes - inactive_file` on v1 and `memory.current - inactive_file` on v2 — an estimate of "memory the application is actually using" excluding reclaimable cache. This is the value the eviction manager uses for ranking decisions, and the value HPA's memory-based scaling reads from metrics-server.

### 22.5 What metrics-server reads

metrics-server is a small aggregated API server (chapter 24) that serves the `metrics.k8s.io/v1beta1` API. Internally it does:

```
   metrics-server pod (replica per cluster, or several behind HA)
     │
     │ every 15s (configurable):
     │   for each Node:
     │     HTTPS GET https://<kubelet-IP>:10250/metrics/resource
     │     ↑ kubelet authn/authz via apiserver
     │
     │ aggregate per-pod + per-node
     ▼
   serve at /apis/metrics.k8s.io/v1beta1/nodes  and  /pods
     ↑ HPA, VPA, `kubectl top` read here
```

The reason `/metrics/resource` exists (as a sibling to `/metrics/cadvisor`) is that metrics-server only needs a tiny subset of cadvisor — CPU usage and memory working-set — and dropping the rest cuts the scrape volume by ~20×.

---

## 23. Kubelet API Endpoints

The kubelet exposes a small but powerful HTTP API on `:10250`:

| Path | Verb | Purpose |
|---|---|---|
| `/pods` | GET | List pods on this node (the kubelet's local view) |
| `/healthz` | GET | Liveness for the kubelet process |
| `/metrics`, `/metrics/cadvisor`, etc. | GET | See §22 |
| `/run/{ns}/{pod}/{container}` | POST | Run a command inside a container |
| `/exec/{ns}/{pod}/{container}` | POST | `kubectl exec` — interactive command |
| `/attach/{ns}/{pod}/{container}` | POST | Attach to running container's stdio |
| `/portForward/{ns}/{pod}` | POST | `kubectl port-forward` — proxy TCP |
| `/logs/` | GET | Read host log files (under `/var/log`) |
| `/containerLogs/{ns}/{pod}/{container}` | GET | `kubectl logs` — read container logs |
| `/stats/summary` | GET | metrics-server input |

The `/run`, `/exec`, `/attach`, `/portForward` endpoints are how `kubectl exec` actually works:

```
  $ kubectl exec -it nginx -- bash
       │
       │ POST /api/v1/namespaces/default/pods/nginx/exec?command=bash&...
       ▼
  apiserver
       │ proxies to kubelet on the node hosting the pod
       ▼
  kubelet POST /exec/default/nginx/nginx?command=bash
       │ SPDY/WebSockets upgrade for stdio multiplexing
       │ kubelet calls CRI: ExecSync or Exec (streaming)
       ▼
  container runtime
       │ runc exec into the container's namespaces
       ▼
  bash running inside container
```

Why these need protection: anyone with `:10250` access who can authenticate is one `/exec` away from root inside any container. The mandatory hardening is:

```yaml
authentication:
  anonymous:
    enabled: false               # MUST
authorization:
  mode: Webhook                  # apiserver decides on each request
readOnlyPort: 0                   # disable :10255 (the legacy unauthenticated port)
```

Plus close port 10250 to anything other than apiserver IPs at the network layer.

---

## 24. Graceful Node Shutdown

Before this feature (introduced 1.20, GA 1.21), a node power-off mid-workload was a forcible kill: SIGKILL to every container, no preStop hooks, no `terminationGracePeriodSeconds` honored. Stateful workloads hated this.

The graceful shutdown feature has the kubelet hook into systemd's *inhibitor lock* mechanism:

```
                ┌─────────────────────────────────────────────────────┐
                │  graceful shutdown sequence                          │
                │                                                     │
                │  1. systemd receives shutdown signal                 │
                │  2. systemd asks: "anyone holding inhibitor locks?" │
                │  3. kubelet (taking lock at startup) says YES       │
                │  4. systemd waits up to inhibitor timeout            │
                │  5. kubelet observes shutdown event via D-Bus       │
                │  6. kubelet starts draining pods:                   │
                │       a. Mark node NotReady (stop new pods)         │
                │       b. Iterate pods in priority order:            │
                │            non-critical pods first                  │
                │            (within: by PodPriority lower → higher)  │
                │            then critical pods                       │
                │       c. For each pod: send termination, wait       │
                │       d. Up to shutdownGracePeriod total            │
                │  7. kubelet releases inhibitor lock                  │
                │  8. systemd proceeds with shutdown                   │
                └─────────────────────────────────────────────────────┘
```

### 24.1 Config

```yaml
# kubelet config
shutdownGracePeriod: 60s             # total budget
shutdownGracePeriodCriticalPods: 20s # reserved for critical pods at end
# OR (1.24+) per-priority budgets:
shutdownGracePeriodByPodPriority:
  - priority: 0
    shutdownGracePeriodSeconds: 30
  - priority: 1000
    shutdownGracePeriodSeconds: 20
  - priority: 10000          # system-cluster-critical
    shutdownGracePeriodSeconds: 10
```

### 24.2 Pitfalls

- Requires **systemd with D-Bus**, won't work on Alpine/musl or in containers running the kubelet (kind, k3s with cgroupv1 hacks).
- Each pod's actual grace period is `min(shutdownGracePeriod_for_my_priority, pod.spec.terminationGracePeriodSeconds)`. A pod requesting `terminationGracePeriodSeconds: 600` will still only get the configured node-level budget.
- Spot-instance / preemption deletion: cloud-provider-specific. AWS Spot has its own 2-minute warning that kubelet doesn't natively integrate with (the node-termination-handler DaemonSet bridges this).

---

## 25. Static Pods and the Mirror Pod Pattern

We introduced static pods in §2; this section drills into how they work end-to-end.

### 25.1 The lifecycle

```
   You drop /etc/kubernetes/manifests/foo.yaml on the node
       │
       ▼
   kubelet's file source (inotify) reads it
       │ kubetypes.ADD event
       ▼
   PodConfig multiplexer
       │ podUpdates channel
       ▼
   syncLoop → HandlePodAdditions → pod worker → SyncPod
       │ container runs locally
       ▼
   kubelet creates a mirror Pod on apiserver:
     metadata:
       name: foo-<node-name>            # the kubelet suffixes the node name
       namespace: kube-system
       annotations:
         kubernetes.io/config.source: file
         kubernetes.io/config.mirror: <sha256 of file>
         kubernetes.io/config.hash:   <pod UID>
         kubernetes.io/config.seen:   <RFC3339>
       ownerReferences:
         - apiVersion: v1
           kind: Node
           name: <node-name>
           uid: <node UID>
           controller: true
```

### 25.2 What's special about mirror pods

1. **You can't kubectl delete them.** Delete succeeds but kubelet recreates within ~20s. Only way to remove: remove the file.
2. **They are not scheduled.** `spec.nodeName` is set from creation; the scheduler ignores them.
3. **Their status is reported by the kubelet** like any other pod (status manager doesn't care that the pod is mirrored).
4. **They're garbage-collected** when the file is gone (kubelet sends DELETE) and on node deletion (Node ownerRef → cascade).
5. **They have a constraint on resource requests vs limits**: usually static pods on the control plane are Guaranteed-QoS (so they survive eviction).

### 25.3 Use cases

- **kubeadm control plane**: kube-apiserver, etcd, kube-controller-manager, kube-scheduler. The chicken-and-egg problem is "how do you run the apiserver when there's no apiserver to schedule it?" — static pods solve this. The kubelet on a control-plane node starts kube-apiserver locally; once kube-apiserver is up, it talks to itself for the mirror creation.
- **Local-only system daemons** that absolutely must run regardless of cluster state.

### 25.4 The atomic-write footgun

If you `vi /etc/kubernetes/manifests/etcd.yaml` and save, depending on your editor's strategy the kubelet may briefly see a truncated file, parse-fail, and (mis)read it as "delete the static pod, recreate from new content". For 5–10 seconds, etcd is gone. Production playbooks use atomic replace:

```bash
# Wrong:
vi /etc/kubernetes/manifests/etcd.yaml

# Right:
cp /etc/kubernetes/manifests/etcd.yaml /tmp/etcd.yaml.new
vi /tmp/etcd.yaml.new
mv /tmp/etcd.yaml.new /etc/kubernetes/manifests/etcd.yaml   # atomic rename
```

`mv` on the same filesystem is an atomic `rename(2)`, so the kubelet never sees a half-file.

---

## 26. Bootstrap, Certs, Kubeconfig

How does a freshly-provisioned node go from "no credentials" to "running pods"? The kubeadm bootstrap flow is the canonical example.

### 26.1 The bootstrap dance

```
   T+0   Node provisioned. /etc/kubernetes/kubelet.conf doesn't exist.
         kubeadm join provides:
           --token <bootstrap-token>
           --discovery-token-ca-cert-hash sha256:...
           --apiserver-advertise-address <ip>:6443
         These are placed in /etc/kubernetes/bootstrap-kubelet.conf

   T+1   kubelet starts.
         --kubeconfig=/etc/kubernetes/kubelet.conf doesn't exist →
         fallback to --bootstrap-kubeconfig=/etc/kubernetes/bootstrap-kubelet.conf

   T+2   Authenticate to apiserver using bootstrap token.
         Identity: system:bootstrappers:kubeadm:default-node-token

   T+3   kubelet generates a new private key:
           /var/lib/kubelet/pki/kubelet-client.key
         creates a CertificateSigningRequest (CSR):
           CN: system:node:<node-name>
           O:  system:nodes
         POSTs CSR to apiserver.

   T+4   kube-controller-manager's csrapproving controller sees the CSR,
         verifies the signer (only bootstrap-token identities may request
         system:node certs), and approves it.
         The csrsigning controller signs the CSR with the cluster CA.

   T+5   kubelet polls the CSR until status.certificate is filled in.
         Writes /var/lib/kubelet/pki/kubelet-client-current.pem
         Generates /etc/kubernetes/kubelet.conf using this cert.

   T+6   kubelet rewinds, uses kubelet.conf (real client cert) from now on.
         Bootstrap token is no longer used.

   T+7   kubelet sends a second CSR for its *serving* cert (the cert it
         presents on :10250). Same approval flow.

   T+8   kubelet registers the Node object with apiserver, starts
         heartbeating, accepts pod assignments.
```

### 26.2 Cert rotation

Certs expire. The kubelet supports automatic renewal:

```yaml
# kubelet config
serverTLSBootstrap: true            # use CSR for serving cert (rotates)
rotateCertificates: true             # rotate client cert when within 1/3 of expiry
```

When a cert is within 1/3 of its lifetime remaining, the kubelet generates a fresh keypair and submits a new CSR. After approval, it atomically swaps the cert files (the `-current.pem` symlink retargets).

### 26.3 The /var/lib/kubelet/pki layout

```
/var/lib/kubelet/pki/
├── kubelet-client-current.pem    -> kubelet-client-<timestamp>.pem
├── kubelet-client-2026-05-23-14-00-00.pem   # cert + key bundle
├── kubelet-client-2026-08-23-14-00-00.pem
├── kubelet.crt                                # serving cert (if not rotated)
├── kubelet.key
```

### 26.4 Silent rotation failure

One of the kubelet's most painful failure modes: the client cert rotation **fails silently** if the CSR controller is broken or the apiserver clock skew is too high. Months later, the existing cert expires, and the kubelet goes offline. The node was perfectly healthy until the day everything broke at once.

Detection: monitor `kubelet_certificate_manager_client_expiration_seconds` (Prometheus metric exposed at `/metrics`). If it's decreasing past `kube-controller-manager`'s default cert lifetime, you have a problem.

---

## 27. Pitfalls

The kubelet's surface is large; this section is the consolidated "things that go wrong in production".

### 27.1 PLEG not healthy under load
Generic PLEG relists every 1s. At 250 pods × 3 containers = 750 containers, that's a 750-row `ListContainers` every second. Under load (high I/O, slow runtime), it stalls. → enable evented PLEG (1.27+) on busy nodes.

### 27.2 Eviction thresholds without monitoring
`evictionHard: memory.available<100Mi` looks fine until you forget that `kubeReserved + systemReserved + 100Mi` is what's actually unavailable to pods. Set thresholds *and* monitor eviction events; alerts on `kubelet_evictions_total` are mandatory.

### 27.3 CPU manager static with too-few integer pods
Static policy works best when most of your traffic is in Guaranteed-class pods with integer CPU requests. With many tiny Burstable pods, the shared pool gets fragmented and you lose more performance than you gain. Run benchmarks before enabling.

### 27.4 Long terminationGracePeriodSeconds on misbehaving apps
A pod with `terminationGracePeriodSeconds: 600` whose process ignores SIGTERM blocks node drains for 10 minutes. Node-shutdown grace caps this, but Deployment rollouts don't. → enforce a sensible max via OPA/Kyverno (`<= 120s` for most workloads).

### 27.5 ImagePullPolicy: Always undermines image GC
Setting `imagePullPolicy: Always` re-pulls the image on every pod startup, which keeps it "recently used" and immune to GC. Combine that with churn and you OOM on `nodefs.available`. Use `IfNotPresent` and pin by digest.

### 27.6 Static pods written non-atomically
See §25.4. `vi` of `/etc/kubernetes/manifests/etcd.yaml` can briefly delete the control plane. Always `mv` from a temp file on the same filesystem.

### 27.7 kubelet client cert rotation failing silently
See §26.4. Monitor `kubelet_certificate_manager_client_expiration_seconds`. The day this metric crosses 0 is the day everything stops working.

### 27.8 Volume manager stuck on dead CSI driver
If the CSI node plugin DaemonSet is down (image pull failure, OOM, crashloop), no pod with a CSI volume can mount. The pod sits in `ContainerCreating` forever. Worse: a pod with a CSI volume that's *terminating* may sit in `Terminating` forever because `NodeUnpublishVolume` fails. Force-delete only after manually unmounting.

### 27.9 Probe misconfig restarting healthy containers
The classic: `livenessProbe.timeoutSeconds: 1` against an app that has 1.5s GC pauses. Restart loops. Always start with a generous `timeoutSeconds`; tighten only after you've seen real probe latency in `/metrics/probes`.

### 27.10 Multi-container pod, one container terminating
`restartPolicy: Always` on the pod (default) means a container that exits is restarted. `restartPolicy: OnFailure` restarts only on non-zero exit. `restartPolicy: Never` lets the container stay terminated. With sidecars (1.28+), the semantics are subtler: if a regular container exits and `restartPolicy=Never`, the pod is `Succeeded`/`Failed` once *all* containers are gone — but sidecars keep running and must be terminated separately by the kubelet. A bug here causes pods stuck Terminating.

### 27.11 Kubelet runs out of inodes before bytes
`imagefs.inodesFree < 5%` triggers eviction even when disk has plenty of space. Common in CI environments that build many small images. Monitor both.

### 27.12 PID exhaustion
`pid.available < 10%`. Each container's process tree counts. A Java app with 1000 threads per process × 100 pods is 100k PIDs. Default `pid_max=4194304` is usually fine; lower limits or per-cgroup `pids.max` is the failure mode.

### 27.13 Node graceful shutdown without systemd inhibitor
On distros without proper systemd, the kubelet's shutdown manager logs warnings and does nothing. Pods get SIGKILLed during reboot. Test it before relying on it.

### 27.14 Mirror pod resurrection
Operator runs `kubectl delete pod kube-apiserver-master-1`. Pod disappears for 5 seconds. Kubelet's mirror-pod loop recreates. Operator panics. → mirror pods are not deletable by the apiserver; the operator must understand static pods.

### 27.15 Topology manager rejecting pods on busy nodes
`single-numa-node` policy rejects a Guaranteed pod when the only NUMA node with enough CPU doesn't have enough memory. The pod goes `Failed`. The scheduler retries elsewhere — but if every node has the same fragmentation, the pod is permanently `Pending`. → bias the scheduler away with affinity/anti-affinity, or use `best-effort`.

### 27.16 Status updates lagging
`statusManager` batches every ~10s. `kubectl get pod` showing stale data is normal during fast churn. If a controller depends on instant accuracy, it must watch directly, not poll via Get.

### 27.17 Two kubelets registering the same node
If a node is cloned (cloud VM template, replicated state), two kubelets may both report as the same `Node.metadata.name`. Last writer wins on heartbeat; pods flap. NodeRestriction limits the damage (each kubelet only writes its own status), but Pod statuses pingpong between the two. → always ensure unique node names.

### 27.18 `--hostname-override` mismatch
If `kubelet --hostname-override=foo` doesn't match the node name in DNS, `kubectl logs`/`exec` may fail because apiserver can't reach the node by name. Set hostname consistently.

### 27.19 cgroups v1 versus v2 confusion
On cgroups v1, `cpu`, `memory`, `cpuset`, `pids` are separate hierarchies. On v2 they're unified. The kubelet supports both, but a node mixing them (some controllers v1, some v2) confuses everything. Pick one; v2 is required for newer features (memory.high, memory swap).

### 27.20 `--max-pods` too high
Default 110. On a beefy node with 256 GB RAM, increasing to 500 sounds reasonable — until you discover PLEG can't keep up, `iptables-restore` for 500 pods takes 30s, and the CNI's IP pool runs out. Scale `--max-pods` proportional to the bottleneck (usually PLEG or IPAM), not RAM.

---

## 28. TL;DR

The kubelet is the only node-local Pod-aware process. It has three input sources (apiserver, file, http) multiplexed by `PodConfig`, one central `syncLoop` `select` that demuxes config changes / PLEG events / probe results / periodic ticks, and a per-pod goroutine in `PodWorkers` that serializes operations.

The hard subsystems:

- **PLEG**: polls (or, with evented PLEG, streams) container state from the runtime. Stalls here cause "PLEG is not healthy" and `NotReady` nodes.
- **`SyncPod` + `computePodActions`**: the level-triggered decision function that turns "current state from PLEG" + "desired state from spec" into a list of CRI calls.
- **Pod startup CRI order**: `RunPodSandbox → PullImage(s) → CreateContainer + StartContainer` per init (serial), per sidecar, per app (in spec order). Teardown is reverse.
- **Probes**: startup gates the others; readiness toggles endpoint inclusion; liveness restarts on failure.
- **Status manager**: only writer of `pod.status`; batches PATCHes to the apiserver.
- **Volume manager**: DSW/ASW with a reconciler driving CSI `Attach → NodeStage → NodePublish`.
- **Device manager**: gRPC plugin framework. `ListAndWatch` advertises devices; `Allocate` returns env+mounts at container creation.
- **CPU manager**: static policy pins integer-CPU Guaranteed pods to exclusive cgroup `cpuset`s.
- **Memory manager**: NUMA-aware memory allocation via `cpuset.mems`.
- **Topology manager**: arbiter that asks CPU + memory + device managers for hints and picks a coherent NUMA placement.
- **Eviction manager**: proactive resource-pressure response. Hard thresholds → immediate SIGKILL; soft thresholds → graceful within `evictionMaxPodGracePeriod`. QoS-then-overuse ranking.
- **OOM**: kubelet writes `oom_score_adj` per QoS class (Guaranteed -997, BestEffort 1000) so the kernel picks the right victim under cgroup OOM.
- **Image GC**: high/low thresholds on imagefs. Container GC trims dead containers.
- **Log management**: runtime writes JSON-lines to `/var/log/pods/...`; rotation per kubelet config; `/var/log/containers/` symlinks for legacy shippers.
- **Kubelet's apiserver client**: x509 cert with identity `system:node:<name>`. Node authorizer + NodeRestriction limit blast radius.
- **Kubelet's own API**: `:10250` serves `/pods`, `/exec`, `/logs`, `/metrics`, `/stats/summary`. Always `anonymous-auth=false`, `authorization=Webhook`.
- **Graceful shutdown**: D-Bus inhibitor lock with systemd; drains pods in priority order.
- **Static pods**: file source + mirror pods on apiserver. The kubeadm control plane runs this way.
- **Bootstrap + cert rotation**: bootstrap token → CSR → cluster-CA-signed client cert; rotates automatically near expiry. Silent rotation failure is a top-tier outage.

If you remember one sentence: **the kubelet is a fan-in / fan-out: many event sources fan in to one decision loop, which fans out to many subsystem managers, all serialized per pod by a pod worker.** Every weird behavior you see — slow pod start, stuck terminate, restart loops, surprise evictions — is one of those managers misbehaving or being misconfigured.

Next: chapter 11 covers **what** the kubelet manages (the Pod object's own internals — init containers, native sidecars, ephemeral containers, lifecycle hooks, readiness gates, the pause container) and chapter 21 covers the resources & QoS policies the kubelet enforces in much deeper detail.
