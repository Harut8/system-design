# Pod Internals

The Pod is the smallest deployable unit in Kubernetes, and almost every staff engineer underestimates it. From the outside it looks like a thin wrapper around a container; from the inside it is a *namespace-bundle scheduling unit* governed by a five-state phase machine, a per-container state machine, three probe state machines, two lifecycle hooks, a four-stage termination sequence, and an entire taxonomy of init/native-sidecar/regular/ephemeral container types — each with different lifecycle, restart, and resource semantics. This chapter is the long-form reference for everything that happens between the apiserver accepting a Pod object and that Pod's last container exiting.

The chapter is positioned between [ch 10 (kubelet)](10-kubelet-internals.md), which describes the agent that *creates* and *manages* Pods, and [ch 12 (workload controllers)](12-workload-controllers.md), which describes the controllers that *manufacture* Pods at scale. Underneath sits [ch 00 (Linux primitives)](00-linux-primitives-for-containers.md) — namespaces, cgroups, and capabilities are the substrate every Pod field eventually compiles down to. Above sits [ch 14 (services)](14-services-and-kube-proxy.md), which is how a Ready Pod becomes reachable, and [ch 21 (resources/QoS)](21-resource-management-and-qos.md), which is where requests/limits become CFS quotas and eviction thresholds.

If you only remember one sentence from this chapter: **a Pod is a group of containers sharing a network namespace anchored by a pause container, co-scheduled on one node, with one IP, lifecycle-managed atomically, and with a state machine you must internalize before you can debug anything that runs on Kubernetes.**

---

## Table of Contents

1. [What a Pod Actually Is](#1-what-a-pod-actually-is)
2. [The Pause Container: The Namespace Anchor](#2-the-pause-container-the-namespace-anchor)
3. [Which Namespaces Are Shared (And Which Are Not)](#3-which-namespaces-are-shared-and-which-are-not)
4. [The Pod Spec: Top-Level Walkthrough](#4-the-pod-spec-top-level-walkthrough)
5. [The Container Spec: Field-by-Field](#5-the-container-spec-field-by-field)
6. [Init Containers](#6-init-containers)
7. [Native Sidecars (KEP-753, GA in 1.29)](#7-native-sidecars-kep-753-ga-in-129)
8. [Ephemeral Containers (Debug Containers)](#8-ephemeral-containers-debug-containers)
9. [The Pod Lifecycle Phase Machine](#9-the-pod-lifecycle-phase-machine)
10. [The Container State Machine](#10-the-container-state-machine)
11. [CrashLoopBackOff: The Kubelet's Local Backoff](#11-crashloopbackoff-the-kubelets-local-backoff)
12. [Probes: Startup, Readiness, Liveness](#12-probes-startup-readiness-liveness)
13. [Lifecycle Hooks: postStart and preStop](#13-lifecycle-hooks-poststart-and-prestop)
14. [The Termination Sequence](#14-the-termination-sequence)
15. [podIP, podIPs, and the Pod Networking Model](#15-podip-podips-and-the-pod-networking-model)
16. [DNS and the Cluster DNS Contract](#16-dns-and-the-cluster-dns-contract)
17. [Security Context: Pod vs Container](#17-security-context-pod-vs-container)
18. [Volumes Visible at the Pod Level](#18-volumes-visible-at-the-pod-level)
19. [Resources, QoS, and In-Place Updates](#19-resources-qos-and-in-place-updates)
20. [Restart Policy and Job Semantics](#20-restart-policy-and-job-semantics)
21. [`status.containerStatuses`: The Runtime Truth](#21-statuscontainerstatuses-the-runtime-truth)
22. [Sidecar Patterns: Pre-1.28 vs Post-1.28](#22-sidecar-patterns-pre-128-vs-post-128)
23. [RuntimeClass: Picking Your Sandbox](#23-runtimeclass-picking-your-sandbox)
24. [Common Pod Failure Causes](#24-common-pod-failure-causes)
25. [Pitfalls](#25-pitfalls)
26. [TL;DR](#26-tldr)

---

## 1. What a Pod Actually Is

A Pod is not a process. A Pod is not a container. A Pod is a **group of containers** that the orchestrator treats as one unit for scheduling, networking, lifecycle, and (often) failure. The Linux kernel has no Pod abstraction; the entire concept lives in the kubelet and is realized by joining several containers to a shared set of namespaces. From a kernel perspective there is no difference between "a Pod with five containers" and "five containers that happen to share namespaces" — Kubernetes just guarantees that the five are colocated, co-scheduled, and torn down together.

### 1.1 The five guarantees a Pod gives you

1. **Co-scheduling.** Every container in the Pod runs on the same node. If the node cannot fit all of them, none of them schedule.
2. **Shared network identity.** Every container sees the same `lo`, the same eth0, the same routes, and binds against the same single `podIP`. Containers in a Pod talk to each other via `localhost`. Two containers in a Pod cannot both bind port 80 — that would be EADDRINUSE inside one namespace.
3. **Shared IPC + UTS.** SystemV semaphores, POSIX message queues, and the hostname are shared.
4. **Shared storage on demand.** Any volume declared at `spec.volumes` may be mounted into any subset of containers in the Pod.
5. **Atomic lifecycle.** When a Pod is created, its containers start in a defined sequence (init → app + sidecars). When it is deleted, every container receives a SIGTERM at the same logical moment, runs its preStop hook, and is killed together. The Pod object lives until the last byte of every volume is unmounted.

### 1.2 What Pods deliberately do **not** guarantee

- **They are not a security boundary.** Containers in the same Pod can usually see each other's filesystems via `/proc/PID/root`, can attach to each other with `ptrace` if `CAP_SYS_PTRACE` is held, and share a kernel.
- **They are not a process supervisor.** If your container's PID 1 dies, the *container* exits — there is no "process inside a Pod restart"; the kubelet either restarts the whole container or doesn't, per `restartPolicy`.
- **They are not stable.** A Pod's identity is `metadata.uid` plus `podIP`. A restart of the Pod is a *new Pod*, with a new UID, almost always a new IP, and no in-memory state. This is why every higher-level abstraction (Deployment, StatefulSet, Service, DNS) exists.

### 1.3 The diagram you must memorize

```
                      ┌──────────────────────────────────────────────┐
                      │                  POD                         │
                      │     metadata.uid = abc123                    │
                      │     status.podIP = 10.244.7.42               │
                      │                                              │
   ┌──────────────────┴──────────────────────────────────────────┐   │
   │                                                              │   │
   │   ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────┐  │   │
   │   │ container│    │ container│    │ container│    │pause │  │   │
   │   │  "app"   │    │ "sidecar"│    │ "metrics"│    │ (infra│  │   │
   │   │  PID ns A│    │  PID ns B│    │  PID ns C│    │  ctr)│  │   │
   │   │  MNT ns A│    │  MNT ns B│    │  MNT ns C│    │      │  │   │
   │   └────┬─────┘    └────┬─────┘    └────┬─────┘    └───┬──┘  │   │
   │        │  joins        │  joins        │  joins       │      │   │
   │        │  net+ipc+uts  │  net+ipc+uts  │  net+ipc+uts │      │   │
   │        └───────┬───────┴───────┬───────┴──────────────┘      │   │
   │                ▼                                              │   │
   │   ┌──────────────────────────────────────────────────────┐   │   │
   │   │   shared NET namespace  (one veth, one IP, one lo)   │   │   │
   │   │   shared IPC namespace  (SysV/POSIX IPC)             │   │   │
   │   │   shared UTS namespace  (one hostname/domainname)    │   │   │
   │   │   shared CGROUP namespace (single /proc/self/cgroup) │   │   │
   │   │   shared TIME namespace  (CLOCK_MONOTONIC offsets)   │   │   │
   │   └──────────────────────────────────────────────────────┘   │   │
   │                                                              │   │
   │   ┌──────────────────────────────────────────────────────┐   │   │
   │   │   shared VOLUMES   (emptyDir, configMap, secret, PV) │   │   │
   │   │   each container declares which mountPaths it wants  │   │   │
   │   └──────────────────────────────────────────────────────┘   │   │
   └──────────────────────────────────────────────────────────────┘   │
                                                                      │
   ┌──────────────────────────────────────────────────────────────┐   │
   │   Optional per-pod cgroup (pod-level memory/cpu limits)     │   │
   │   /sys/fs/cgroup/.../kubepods.slice/kubepods-pod<uid>.slice │   │
   └──────────────────────────────────────────────────────────────┘   │
                                                                      │
                                              co-scheduled on one node│
                      ────────────────────────────────────────────────┘
```

Every container in the Pod has its **own** PID and MNT namespace (by default — `shareProcessNamespace: true` is the override). Every container in the Pod **shares** NET, IPC, UTS, CGROUP, and TIME namespaces. The shared bundle is owned by an invisible container called *pause*, which is the namespace anchor described in §2.

### 1.4 The Pod object in the API

In `staging/src/k8s.io/api/core/v1/types.go`, a Pod is:

```go
type Pod struct {
    metav1.TypeMeta   `json:",inline"`
    metav1.ObjectMeta `json:"metadata,omitempty"`
    Spec              PodSpec   `json:"spec,omitempty"`
    Status            PodStatus `json:"status,omitempty"`
}
```

The Spec is what the user declares; the Status is what the kubelet (and a few controllers) writes back. Spec is *almost* immutable after creation — only a small allowlist of fields can be mutated (`spec.activeDeadlineSeconds`, `spec.tolerations` (additions only), `spec.terminationGracePeriodSeconds` (down to 1 only), image of existing containers, and — since 1.27 — `spec.containers[*].resources` via the resize subresource). Everything else is set once. This immutability is the whole reason workload controllers (Deployment, StatefulSet) exist: to delete-and-recreate Pods on spec change.

### 1.5 What "atomic" means

"Atomic" here is a soft term. Scheduling is atomic in the sense that the scheduler binds the Pod (sets `spec.nodeName`) in a single PATCH; once bound, the kubelet pulls images and starts containers serially. If the kubelet *fails partway through* startup, the Pod is left in a partial state, but the apiserver still considers it scheduled. The Pod will keep retrying until it either runs or is deleted. There is no transaction that says "all containers running or none". This is one reason init containers (§6) exist: to express ordering that the runtime alone cannot.

---

## 2. The Pause Container: The Namespace Anchor

Every Pod has a hidden container you didn't ask for, named in `crictl ps -a` as `k8s_POD_<podname>_<ns>_<uid>_0`, image `registry.k8s.io/pause:3.10` (as of this writing). It is the **pause container**, sometimes called the **infra container** or **sandbox container**. It exists for one reason: *some process must own the shared namespaces so the kubelet has something to `setns()` other containers into*.

### 2.1 What pause actually does

Pause is statically linked, about 250 KB on disk, written originally in C (the upstream source is in `kubernetes/build/pause/`, also in Go for some distributions). It does exactly three things:

1. Sets up signal handlers for SIGINT, SIGTERM (so it exits cleanly when the kubelet asks).
2. Installs a SIGCHLD reaper that calls `wait()` to reap zombies. (This is important when `shareProcessNamespace: true` is set — pause becomes PID 1 of the shared PID namespace, and orphaned children would otherwise become uninterruptible zombies.)
3. Calls `pause(2)` in a loop. The kernel parks the process in `TASK_INTERRUPTIBLE` state until a signal arrives. It uses zero CPU.

That's it. The entire source is small enough to read in five minutes:

```c
/* simplified pause.c */
static void sigdown(int signo) { exit(0); }
static void sigreap(int signo) { while (waitpid(-1, NULL, WNOHANG) > 0); }

int main() {
    signal(SIGINT,  sigdown);
    signal(SIGTERM, sigdown);
    signal(SIGCHLD, sigreap);
    for (;;) pause();
}
```

### 2.2 Why pause exists at all

A Linux namespace is reference-counted by the processes that hold it. If the only container in the Pod crashes, its namespaces would be torn down — and the kubelet would have no way to re-join a restarted container into the same network/IPC/UTS world (same IP, same hostname, same shared memory segments). By creating pause *first* and `setns`-ing every other container into pause's namespaces, the lifetime of the namespaces decouples from the lifetime of any single workload container. The pause container's job is to be alive whenever the Pod is alive.

This decoupling is what makes container restart inside a Pod cheap and meaningful: nginx can crash and be restarted twenty times, and every time it joins the exact same network namespace with the same `podIP`. If pause itself died, the kubelet treats it as a *sandbox loss* — the Pod's PodSandbox status flips to `NOTREADY`, the kubelet tears down all remaining containers and creates a fresh sandbox (new pause, new podIP).

### 2.3 The CRI's view: PodSandbox = pause

In the CRI gRPC ([ch 01](01-container-runtimes-cri-oci.md)), the equivalent of "create the pause container" is `RunPodSandbox`:

```protobuf
rpc RunPodSandbox(RunPodSandboxRequest) returns (RunPodSandboxResponse) {}

message RunPodSandboxRequest {
    PodSandboxConfig config = 1;
    string runtime_handler  = 2;  // selects RuntimeClass (gvisor/kata/runc)
}
```

The CRI implementation (containerd, CRI-O) is responsible for actually creating pause, plumbing the CNI ADD to assign the podIP, and returning a `pod_sandbox_id`. From that point on, every per-container CRI call (`CreateContainer`, `StartContainer`) takes the sandbox ID, and the runtime ensures the new container joins the sandbox's namespaces. The kubelet never has to know what the implementation looks like — it just speaks CRI.

### 2.4 Pause is the sandbox lifecycle anchor

The kubelet's PodSyncResult treats pause loss as a special event: see `kubernetes/pkg/kubelet/kuberuntime/kuberuntime_manager.go`, function `podSandboxChanged`. If the sandbox is gone or in a bad state, the result is "kill all containers and start over from RunPodSandbox". This is why a Pod that loses its pause container always gets a new IP — the CNI ADD runs again on a fresh sandbox.

---

## 3. Which Namespaces Are Shared (And Which Are Not)

This is the single most-asked Pod question. The honest answer: it depends on which namespace, and on `shareProcessNamespace`/`hostPID`/`hostNetwork`/`hostIPC`. The table below is the canonical mapping for a Pod with default settings (no host* flags, no `shareProcessNamespace`).

| Namespace | Shared by default? | How to share | How to break out |
|---|---|---|---|
| **NET** (`CLONE_NEWNET`) | **Yes** — always | (always) | `spec.hostNetwork: true` joins host net ns instead |
| **IPC** (`CLONE_NEWIPC`) | **Yes** — always | (always) | `spec.hostIPC: true` joins host IPC ns |
| **UTS** (`CLONE_NEWUTS`) | **Yes** — always | (always) | (no per-Pod knob — Pods always own a fresh UTS ns; only host* can break out) |
| **CGROUP** (`CLONE_NEWCGROUP`) | **Yes** | (always since cgroup v2 default) | (no opt-out) |
| **TIME** (`CLONE_NEWTIME`) | **Yes** | (effectively shared — Pods do not customize time ns per-container) | (no Pod-level knob) |
| **PID** (`CLONE_NEWPID`) | **No** — each container has its own | `spec.shareProcessNamespace: true` | `spec.hostPID: true` joins host PID ns |
| **MNT** (`CLONE_NEWNS`) | **No** — each container has its own rootfs | Only via declared `volumes` + `volumeMounts` (filesystem sharing, not namespace sharing) | (no flag — mount-ns is fundamental to having a per-container rootfs) |
| **USER** (`CLONE_NEWUSER`) | **No** — usually disabled entirely | UserNamespacesSupport feature gate + `spec.hostUsers: false` (alpha→beta in recent releases) | (default is to *not* use user ns at all) |

### 3.1 Network namespace — the most important one

Every container in the Pod sees:

- A single `lo` interface, shared.
- A single `eth0` interface (the pod end of a veth pair set up by the CNI), with the podIP.
- The same routes, the same iptables rules, the same conntrack table, the same kernel parameters (`net.ipv4.tcp_*`, etc.).
- The same `/etc/resolv.conf` (rendered by the kubelet from `spec.dnsPolicy` and `spec.dnsConfig`).

Two containers in the same Pod cannot both bind 0.0.0.0:8080. They can talk to each other over `127.0.0.1:<port>`, which is the canonical sidecar pattern (envoy listens on 15000, app listens on 8080, app calls `localhost:15000`).

### 3.2 IPC namespace

SystemV semaphores, message queues, shared memory segments (`shmget`), POSIX message queues. Shared across the Pod. Two containers in a Pod can `shmget()` the same key and see each other's shared memory. This is used by some legacy databases that run as multiple processes sharing a buffer pool.

### 3.3 UTS namespace

One hostname per Pod. By default it is set to `metadata.name` (with truncation/sanitization). `spec.hostname` overrides; `spec.subdomain` makes the Pod's FQDN resolvable via headless Service ([ch 18](18-dns-and-coredns.md)). Every container sees the same `gethostname(2)`.

### 3.4 PID namespace — the one that surprises people

**Default**: each container has its own PID namespace. Each container's main process is PID 1. Container A *cannot* see container B's processes in `ps`. They cannot `kill -9` each other.

This is often counterintuitive — people expect `kubectl exec sidecar -- ps aux` to show the app process. It does not. To make it show, set:

```yaml
spec:
  shareProcessNamespace: true
  containers:
  - name: app
    image: nginx
  - name: debug
    image: busybox
    command: ["sleep", "infinity"]
```

With `shareProcessNamespace: true`, all containers in the Pod share a single PID namespace. Pause becomes PID 1 (which is *why* the pause container needs the SIGCHLD reaper — orphans get reparented to PID 1). The app process is now PID 12, the sidecar is PID 22, etc., and `ps aux` from any container shows all of them.

The cost: each container can now signal and trace every other container. This is fine for trusted sidecars (logging, Envoy, debugging) but is a defense-in-depth weakening for adversarial-by-design workloads.

### 3.5 Mount namespace — the one that *also* surprises people

**Default**: each container has its own mount namespace, and thus its own rootfs derived from its own image layer stack. Sharing files between containers in a Pod requires **declaring a volume and mounting it in both** — there is no "they're in the same Pod, of course they share /". They don't.

This is by design: the mount namespace is the only thing that gives a container its private view of the filesystem. Sharing the mount namespace would mean container A could see container B's `/etc`, `/usr`, etc., and they would conflict.

The standard pattern: use an `emptyDir` volume:

```yaml
spec:
  volumes:
  - name: shared-data
    emptyDir: {}
  containers:
  - name: producer
    image: producer:1
    volumeMounts:
    - name: shared-data
      mountPath: /out
  - name: consumer
    image: consumer:1
    volumeMounts:
    - name: shared-data
      mountPath: /in
```

Now `/out` in the producer and `/in` in the consumer point at the same underlying directory on the node. Mount namespaces are still separate; the *contents* of one mountpoint are shared because the kubelet bind-mounted the same source into both.

### 3.6 User namespace — barely shipped yet

User namespaces let you map UIDs inside the container to different UIDs outside (typically: a root-inside-container that is an unprivileged UID outside). This is the strongest container-escape mitigation Linux provides and the foundation of "rootless" containers. As of Kubernetes 1.30 it is gated by `UserNamespacesSupport` (beta) and applies per-Pod via `spec.hostUsers: false`. When enabled, the kubelet asks the runtime to set up a user namespace for the Pod and remaps file ownership accordingly. Most production clusters do not yet use this — too many CSI drivers, CNI plugins, and image patterns assume host UID 0 inside the container.

### 3.7 Time namespace

Linux 5.6+ supports `CLONE_NEWTIME`, which gives a namespace its own offset for `CLOCK_MONOTONIC` and `CLOCK_BOOTTIME`. Used primarily for live migration of containers (CRIU) — Pods generally don't manipulate it directly.

### 3.8 cgroup namespace

When cgroup ns is in use, `/proc/self/cgroup` and `/sys/fs/cgroup` show a view rooted at the container's cgroup, not the host's. This lets a container's procfs/cgroupfs look like a top-level cgroup root, which prevents container-aware tools from leaking information about the node's full cgroup tree. Shared across the Pod.

### 3.9 host* fields: the namespace overrides

| Field | Effect |
|---|---|
| `spec.hostNetwork: true` | Pod's containers join the **host** network namespace. The Pod has the node's IP. Useful for system DaemonSets that need to see all NIC traffic (kube-proxy, CNI, monitoring). Dangerous — the Pod can bind any host port and read all traffic. |
| `spec.hostPID: true` | Pod's containers join the host PID namespace. Every container sees every process on the node. Used by Falco-style runtime security. |
| `spec.hostIPC: true` | Pod's containers join the host IPC namespace. Rarely useful. |
| `spec.hostUsers: false` | Opts the Pod into a *new* user namespace (alpha/beta), instead of the host's. Counter-intuitive name. |

When any host* flag is on, the corresponding Pod namespace is *not* created; containers in the Pod simply don't enter that namespace at all (they inherit the host's). The pause container is still created, but it's mostly a placeholder.

---

## 4. The Pod Spec: Top-Level Walkthrough

The Pod's `spec` field is large — about 60 fields in the v1 API. This section is a field-by-field walkthrough of the ones that matter at staff level. We will not cover deprecated fields (e.g., `spec.serviceAccount`, which has been an alias for `spec.serviceAccountName` for years).

The authoritative source is `staging/src/k8s.io/api/core/v1/types.go`, struct `PodSpec`. Below is a heavily annotated subset:

```go
type PodSpec struct {
    Volumes                       []Volume                  // see §18
    InitContainers                []Container               // see §6
    Containers                    []Container               // see §5
    EphemeralContainers           []EphemeralContainer      // see §8 (read via subresource)
    RestartPolicy                 RestartPolicy             // Always | OnFailure | Never; see §20
    TerminationGracePeriodSeconds *int64                    // see §14
    ActiveDeadlineSeconds         *int64                    // §4.2
    DNSPolicy                     DNSPolicy                 // see §16
    NodeSelector                  map[string]string         // §4.3
    ServiceAccountName            string                    // §4.4
    AutomountServiceAccountToken  *bool                     // §4.4
    NodeName                      string                    // set by scheduler
    HostNetwork                   bool                      // §3.9
    HostPID                       bool                      // §3.9
    HostIPC                       bool                      // §3.9
    HostUsers                     *bool                     // §3.9 (alpha/beta)
    ShareProcessNamespace         *bool                     // §3.4
    SecurityContext               *PodSecurityContext       // see §17
    ImagePullSecrets              []LocalObjectReference    // §4.5
    Hostname                      string                    // §3.3
    Subdomain                     string                    // §3.3
    Affinity                      *Affinity                 // §4.6
    SchedulerName                 string                    // §4.7
    Tolerations                   []Toleration              // §4.8
    HostAliases                   []HostAlias               // §4.9
    PriorityClassName             string                    // §4.10
    Priority                      *int32                    // computed from PriorityClassName
    DNSConfig                     *PodDNSConfig             // see §16
    ReadinessGates                []PodReadinessGate        // §4.11
    RuntimeClassName              *string                   // see §23
    EnableServiceLinks            *bool                     // §4.12
    PreemptionPolicy              *PreemptionPolicy         // §4.10
    Overhead                      ResourceList              // §4.13 (RuntimeClass overhead)
    TopologySpreadConstraints     []TopologySpreadConstraint// §4.14
    SetHostnameAsFQDN             *bool                     // §3.3
    OS                            *PodOS                    // §4.15 (windows/linux)
    SchedulingGates               []PodSchedulingGate       // §4.16
    ResourceClaims                []PodResourceClaim        // §4.17 (DRA)
    Resources                     *ResourceRequirements     // §4.18 (pod-level, 1.32+)
}
```

We walk these in groups.

### 4.1 The container lists

- **`spec.containers`** — the main containers. Must be non-empty. Cannot be edited after creation except via the image field (and via resize for resources). All containers in this list start in parallel after init containers complete.
- **`spec.initContainers`** — run to completion, in order, before any main container starts. See §6. Native sidecars (§7) are smuggled in here with `restartPolicy: Always`.
- **`spec.ephemeralContainers`** — added later via a subresource, never on Pod create. See §8.

### 4.2 `activeDeadlineSeconds`

A hard wall-clock cap on the Pod's lifetime. Counted from when the Pod first transitions to `Running`. When exceeded, the kubelet kills all containers and sets `status.phase = Failed`, `status.reason = DeadlineExceeded`. Mostly used by Job pods to prevent runaway batch jobs; rare outside Jobs. Editable on a running Pod (one of the few mutable spec fields).

### 4.3 `nodeSelector`

The simplest scheduler hint: a map of node labels the chosen node must have. ANDed. Hard requirement, no soft variant. Set:

```yaml
spec:
  nodeSelector:
    node.kubernetes.io/instance-type: m6i.4xlarge
    topology.kubernetes.io/zone: us-east-1a
```

Subsumed by `spec.affinity.nodeAffinity` (more expressive, supports OR, soft preferences). `nodeSelector` is still useful for its terseness. The scheduler does the matching ([ch 09](09-kube-scheduler-internals.md)).

### 4.4 ServiceAccount fields

```yaml
spec:
  serviceAccountName: my-app                    # default: "default"
  automountServiceAccountToken: false           # default: true
```

- `serviceAccountName` selects which ServiceAccount the Pod runs as. If not set, defaults to `default` in the namespace. The ServiceAccount governs the Pod's apiserver identity.
- `automountServiceAccountToken: false` opts out of the *projected* SA token volume that would otherwise be mounted at `/var/run/secrets/kubernetes.io/serviceaccount/`. This is a defense-in-depth flag: a Pod that never talks to the apiserver should not have a token. Modern projected SA tokens are short-lived (1h default), audience-bound, and rotated; legacy `Secret`-stored tokens (pre-1.24) were long-lived. See [ch 07](07-authentication-authorization.md).

### 4.5 `imagePullSecrets`

A list of Secret names in the same namespace, each containing a Docker-style `.dockerconfigjson`. The kubelet uses these for the CRI's `PullImage` auth. The Secret is referenced by name only — the kubelet reads it at pull time. Note that an `imagePullSecret` is *only* used for image pulls; it does not get mounted in the Pod.

### 4.6 `affinity`

Three sub-blocks: `nodeAffinity`, `podAffinity`, `podAntiAffinity`. Each has `requiredDuringSchedulingIgnoredDuringExecution` (hard) and `preferredDuringSchedulingIgnoredDuringExecution` (soft, weighted). Example:

```yaml
spec:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchExpressions:
          - key: topology.kubernetes.io/zone
            operator: In
            values: ["us-east-1a", "us-east-1b"]
    podAntiAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          labelSelector:
            matchLabels:
              app: my-app
          topologyKey: kubernetes.io/hostname
```

This says: place me in zone a or b (hard), and try to avoid other Pods with label `app=my-app` on the same host (soft). The scheduler ([ch 09](09-kube-scheduler-internals.md)) evaluates these via the `InterPodAffinity` and `NodeAffinity` plugins.

### 4.7 `schedulerName`

The name of the scheduler that should handle this Pod. Default: `default-scheduler`. You set it to your own value if you run a custom scheduler (gang scheduler, Volcano, Yunikorn). Schedulers ignore Pods whose `schedulerName` doesn't match theirs.

### 4.8 `tolerations`

The Pod's permission slips for **taints** on nodes. A node with `key=disk,value=ssd,effect=NoSchedule` will reject any Pod that doesn't tolerate it. Tolerations:

```yaml
spec:
  tolerations:
  - key: dedicated
    operator: Equal
    value: gpu
    effect: NoSchedule
  - key: node.kubernetes.io/not-ready
    operator: Exists
    effect: NoExecute
    tolerationSeconds: 300       # be evicted after 5 min, not immediately
```

`NoExecute` taints evict already-running Pods that don't tolerate them; `tolerationSeconds` grants a grace period. The control plane uses this for node failure: when a Node goes NotReady, it gets tainted with `node.kubernetes.io/not-ready:NoExecute`, and Pods that don't tolerate (or do, but with a low `tolerationSeconds`) are evicted by the NodeLifecycle controller.

### 4.9 `hostAliases`

```yaml
spec:
  hostAliases:
  - ip: 10.1.2.3
    hostnames: ["legacy-api.local", "lapi"]
```

The kubelet writes these into `/etc/hosts` on every container in the Pod. *This is the only way to get persistent `/etc/hosts` entries in a Pod* — writing to `/etc/hosts` directly works at runtime, but it's a tmpfs and gets reset on container restart (because the kubelet re-renders it).

### 4.10 `priorityClassName` and `preemptionPolicy`

```yaml
spec:
  priorityClassName: high-priority
  preemptionPolicy: PreemptLowerPriority      # or "Never"
```

The PriorityClass admission plugin resolves `priorityClassName` to a `priority` integer. The scheduler may **preempt** lower-priority pending or running Pods to fit this one. `preemptionPolicy: Never` lets you give the Pod a high score in scheduling without it kicking anyone out. PriorityClasses themselves are cluster-scoped objects ([ch 09](09-kube-scheduler-internals.md)).

### 4.11 `readinessGates`

```yaml
spec:
  readinessGates:
  - conditionType: "example.com/feature-1"
```

The Pod is not considered Ready (for Service endpoint inclusion) until **every** condition in `readinessGates` has `status: True` in `status.conditions`. The Pod controller doesn't write these conditions; an external actor (a controller, an admission webhook with a status sub-controller, etc.) does. Useful for "wait until cloud LB has actually started forwarding to me before declaring ready" — the cloud-controller writes the gate.

### 4.12 `enableServiceLinks`

Default true. When true, the kubelet injects env vars for every Service in the namespace (`<SERVICE_NAME>_SERVICE_HOST`, etc., the Docker-link style). At scale (hundreds of Services), this blows up your env block and slows container start. Set to `false` for most modern Pods (DNS makes the env vars redundant).

### 4.13 `overhead`

Filled in by the PodOverhead admission plugin based on `runtimeClassName` ([ch 23 / 29](23-crds-operators-and-controller-runtime.md)). Represents the resource cost of the runtime itself (e.g., gVisor's Sentry, Kata's hypervisor and guest kernel). Counted by the scheduler when computing node fit; counted by eviction.

### 4.14 `topologySpreadConstraints`

```yaml
spec:
  topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: ScheduleAnyway     # or DoNotSchedule
    labelSelector:
      matchLabels:
        app: my-app
```

"Across all Pods matching `app=my-app`, do not let the number of Pods in any one zone exceed (min + maxSkew)." A first-class scheduler primitive in the framework; far more useful than podAntiAffinity for HA spreading.

### 4.15 `os`

```yaml
spec:
  os:
    name: linux       # or "windows"
```

Lets the scheduler and webhook policies know whether this is a Linux or Windows Pod. Doesn't *force* anything by itself, but the kubelet/CRI rejects mismatches.

### 4.16 `schedulingGates`

```yaml
spec:
  schedulingGates:
  - name: example.com/queued-by-batch-controller
```

A Pod with any gate is *not eligible for scheduling*. It stays in `Pending` with phase `SchedulingGated`. Some controller removes the gate (PATCH that strips the entry) when it decides this Pod should now schedule. Used by queueing/quota systems (Kueue), to hold Pods until quota is available. New in 1.27 (beta) / 1.30 (GA).

### 4.17 `resourceClaims` (DRA — Dynamic Resource Allocation)

Beta/GA-progress in recent releases. Lets Pods request resources via ResourceClaim objects (e.g., a GPU with a specific feature). The DRA scheduler plugin drives binding. Forward-ref [ch 21](21-resource-management-and-qos.md).

### 4.18 `spec.resources` (pod-level resources, 1.32+ alpha)

Until 1.32, only containers had `resources`. Pod-level `spec.resources` lets you request a total for the Pod that the scheduler treats as the floor, with containers free to use any share. Mostly relevant for batch / ML — discussed in [ch 21](21-resource-management-and-qos.md).

---

## 5. The Container Spec: Field-by-Field

Each entry of `spec.containers` (and `spec.initContainers`) is a `Container`. From `types.go`:

```go
type Container struct {
    Name                     string
    Image                    string
    Command                  []string         // overrides ENTRYPOINT
    Args                     []string         // overrides CMD
    WorkingDir               string
    Ports                    []ContainerPort
    EnvFrom                  []EnvFromSource
    Env                      []EnvVar
    Resources                ResourceRequirements
    ResizePolicy             []ContainerResizePolicy // 1.27+
    RestartPolicy            *ContainerRestartPolicy // sidecar-mode, 1.28+
    VolumeMounts             []VolumeMount
    VolumeDevices            []VolumeDevice
    LivenessProbe            *Probe
    ReadinessProbe           *Probe
    StartupProbe             *Probe
    Lifecycle                *Lifecycle
    TerminationMessagePath   string
    TerminationMessagePolicy TerminationMessagePolicy
    ImagePullPolicy          PullPolicy
    SecurityContext          *SecurityContext
    Stdin                    bool
    StdinOnce                bool
    TTY                      bool
}
```

### 5.1 `name`, `image`, `imagePullPolicy`

```yaml
- name: app
  image: registry.example.com/team/app@sha256:abc123...
  imagePullPolicy: IfNotPresent       # or Always, or Never
```

- `name` must be a DNS label, unique within the Pod. It's how `kubectl logs <pod> -c <name>` works.
- `image` is the OCI reference. Best practice: pin by digest (`@sha256:...`), not tag, so the image cannot mutate beneath you.
- `imagePullPolicy`:
  - `Always` — always consult the registry. Pulls the image manifest every time (using the cached layers if their digests match).
  - `IfNotPresent` — only pull if the runtime doesn't already have the image locally. *Default if image tag is anything but `:latest`*.
  - `Never` — never pull. The image must already be on the node. Used in development and air-gapped clusters.
  - **Default when tag is `:latest` or no tag**: `Always`. This is one of the few cases where image-tag syntax changes Pod semantics.

### 5.2 `command` and `args` vs Dockerfile ENTRYPOINT/CMD

Confusing-but-canonical precedence table:

| Dockerfile has | Pod spec has | What runs |
|---|---|---|
| `ENTRYPOINT ["foo"]`, `CMD ["bar"]` | `command: []`, `args: []` (both absent) | `foo bar` |
| `ENTRYPOINT ["foo"]`, `CMD ["bar"]` | `command: ["baz"]`, `args: []` | `baz` |
| `ENTRYPOINT ["foo"]`, `CMD ["bar"]` | `command: []`, `args: ["qux"]` | `foo qux` |
| `ENTRYPOINT ["foo"]`, `CMD ["bar"]` | `command: ["baz"]`, `args: ["qux"]` | `baz qux` |

Mnemonic: Pod's `command` overrides Dockerfile's ENTRYPOINT; Pod's `args` overrides Dockerfile's CMD. Variable expansion: `args` strings of the form `$(VAR)` are substituted from `env` if present. **Note** the `$(VAR)` form, not `${VAR}` — the latter is interpreted only by the shell, and only if you launched a shell.

### 5.3 `ports`

```yaml
ports:
- name: http
  containerPort: 8080
  protocol: TCP
- name: metrics
  containerPort: 9090
  hostPort: 9090     # AVOID UNLESS NEEDED
```

`ports` is **informational** for plain ClusterIP usage — the kubelet does *not* enforce that the container actually listens on these ports. The fields matter when:

- `hostPort` is set: kubelet asks the runtime to publish the container's port on the node's IP via iptables DNAT. This makes the Pod consume a host port, limiting you to one Pod per node, and bypasses the Service abstraction. Use only for DaemonSets that genuinely need it (e.g., kube-proxy itself).
- `name` is referenced by a Service's `targetPort` or by a Probe's `port` field by name.

### 5.4 `env` and `envFrom`

```yaml
env:
- name: POD_IP
  valueFrom:
    fieldRef:
      fieldPath: status.podIP
- name: NODE_NAME
  valueFrom:
    fieldRef:
      fieldPath: spec.nodeName
- name: CPU_LIMIT
  valueFrom:
    resourceFieldRef:
      containerName: app
      resource: limits.cpu
      divisor: "1"
- name: DB_PASS
  valueFrom:
    secretKeyRef:
      name: db-creds
      key: password
- name: FEATURE_FLAG
  valueFrom:
    configMapKeyRef:
      name: features
      key: experimental_x
envFrom:
- configMapRef:
    name: app-config
- secretRef:
    name: app-secrets
    optional: true
```

- `valueFrom.fieldRef` — Downward API, exposes Pod metadata. Limited set: `metadata.name`, `metadata.namespace`, `metadata.uid`, `metadata.labels['k']`, `metadata.annotations['k']`, `spec.nodeName`, `spec.serviceAccountName`, `status.hostIP`, `status.podIP`, `status.podIPs`.
- `valueFrom.resourceFieldRef` — Downward API for resources. `requests.cpu`, `limits.cpu`, `requests.memory`, `limits.memory`, `requests.ephemeral-storage`, `limits.ephemeral-storage`. The `divisor` divides the value before exposing (e.g., `divisor: "1Mi"` returns megabytes).
- `valueFrom.configMapKeyRef` / `secretKeyRef` — pulls a single key from a ConfigMap/Secret. The kubelet reads these at container start; **changes to the source after start are not propagated to env vars** (unlike file mounts of ConfigMap/Secret, which can be updated).
- `envFrom` — splats every key/value of a ConfigMap or Secret into env. Each top-level key becomes an env var. Mostly used for "give me my whole config block as env".

### 5.5 `resources`

```yaml
resources:
  requests:
    cpu: "500m"
    memory: "256Mi"
    ephemeral-storage: "1Gi"
  limits:
    cpu: "1"
    memory: "512Mi"
```

`requests` is what the scheduler uses to find a fitting node and what cgroups treat as a soft floor. `limits` is what cgroups enforce as a hard ceiling. The interaction with QoS is in §19. The cgroup mappings are in [ch 21](21-resource-management-and-qos.md). Resources can also be expressed for `hugepages-2Mi`, `hugepages-1Gi`, and any extended resource (e.g., `nvidia.com/gpu`).

### 5.6 `volumeMounts` and `volumeDevices`

```yaml
volumeMounts:
- name: data
  mountPath: /var/lib/data
  subPath: instance-1
  readOnly: false
  mountPropagation: HostToContainer
volumeDevices:
- name: raw-disk
  devicePath: /dev/xvdf
```

- `name` references an entry in `spec.volumes`.
- `mountPath` is the path *inside the container*.
- `subPath` mounts only a subdirectory of the volume. Combined with `subPathExpr`, you can use env-var expansion to give each container its own slice of a shared volume.
- `mountPropagation` controls whether mount events propagate between the container and the host (None / HostToContainer / Bidirectional). Bidirectional requires `privileged: true` and is used by CSI node plugins.
- `volumeDevices` is for *raw block* volumes — the volume appears as a `/dev/X` device, not a filesystem.

### 5.7 `livenessProbe`, `readinessProbe`, `startupProbe`

Three independent probes. Each is a `Probe`:

```yaml
livenessProbe:
  httpGet:
    path: /healthz
    port: 8080
    scheme: HTTPS
    httpHeaders:
    - name: X-Probe-Source
      value: kubelet
  initialDelaySeconds: 10
  periodSeconds: 5
  timeoutSeconds: 2
  successThreshold: 1
  failureThreshold: 3
  terminationGracePeriodSeconds: 10
```

The four handler types are `httpGet`, `tcpSocket`, `exec`, and `grpc`. Semantics are covered in §12.

### 5.8 `lifecycle.postStart` and `lifecycle.preStop`

```yaml
lifecycle:
  postStart:
    exec:
      command: ["/bin/sh", "-c", "echo started > /tmp/up"]
  preStop:
    httpGet:
      path: /shutdown
      port: 8080
```

Semantics: §13. Note that probes' `httpGet` accepts the **named** port, but `lifecycle` handlers do not (must be numeric or named only for `livenessProbe`/`readinessProbe`/`startupProbe`).

### 5.9 `terminationMessagePath` and `terminationMessagePolicy`

`terminationMessagePath` defaults to `/dev/termination-log` — when the container exits, the kubelet reads this file and surfaces its contents in `status.containerStatuses[*].lastState.terminated.message`. Useful for human-readable exit hints. `terminationMessagePolicy: FallbackToLogsOnError` makes the kubelet use the last few KB of the container's stdout if `/dev/termination-log` is empty and the container exited non-zero.

### 5.10 `securityContext` (container-level)

Container-level `securityContext` overrides Pod-level for that container. Fields covered in §17.

### 5.11 `resizePolicy` and per-container `restartPolicy`

- `resizePolicy: [{resourceName: cpu, restartPolicy: NotRequired}, {resourceName: memory, restartPolicy: RestartContainer}]` — controls whether an in-place resource resize requires restarting the container. CPU resize is usually `NotRequired` (cgroup writes are live); memory shrinks often require a restart on some runtimes. See §19.5.
- Per-container `restartPolicy: Always` is the **native sidecar** marker, valid only in `initContainers`. See §7.

---

## 6. Init Containers

Init containers run before regular containers, **in order**, and **must each complete successfully** before the next starts. They are a Pod-level ordering primitive — the only way to express "do X before Y, in the same Pod, with the same volumes available" without writing a wrapper script.

### 6.1 Lifecycle

```
   Pod created
      │
      ▼
   Pod phase = Pending
   Pod condition Initialized = False
      │
   ┌──── for each initContainer in order ────┐
   │                                          │
   │  pull image                              │
   │  CreateContainer + StartContainer       │
   │  container runs to completion           │
   │  exit code 0?                            │
   │     yes → next init container           │
   │     no  → consult restartPolicy:        │
   │            Always   → restart           │
   │            OnFailure→ restart           │
   │            Never    → Pod phase=Failed  │
   └─────────────────────────────────────────┘
      │
   All init containers exited 0
      │
      ▼
   Pod condition Initialized = True
   Start main containers (in parallel)
   Pod phase = Running once at least one main container is Running
```

The restart policy for init container failure follows `spec.restartPolicy` of the *Pod* (Always behaves like OnFailure for init containers — restart on failure). On `Never`, an init container that exits non-zero causes the entire Pod to transition to `Failed`.

### 6.2 Resource requests interaction

The "effective request" of a Pod is computed by the scheduler as the **max** of:

- The maximum of any single init container's request (init containers run one at a time, so the peak is whichever one needs the most).
- The sum of all regular containers' requests + the sum of all native sidecar containers' requests (regular + native sidecars run concurrently).

In Go (`pkg/api/v1/resource/helpers.go`, function `PodRequests`):

```go
effective := sum(regular_containers.Requests) + sum(native_sidecars.Requests)
for each init in initContainers without restartPolicy=Always:
    effective = max(effective, init.Requests)
return effective
```

This means: a single init container with `requests.memory: 4Gi` reserves 4Gi at scheduling time, even if no main container needs that much. Once init completes, that 4Gi is "given back" to the node (still subject to limits). This is rarely a problem at scale but is one of the most-misunderstood scheduler facts.

### 6.3 Common init container patterns

**a. Wait for dependency:**

```yaml
initContainers:
- name: wait-for-db
  image: busybox:1.36
  command:
  - sh
  - -c
  - |
    until nc -z postgres 5432; do
      echo "waiting for postgres";
      sleep 2;
    done
```

**b. Schema migration:**

```yaml
initContainers:
- name: migrate
  image: my-app:1.2
  command: ["/app", "migrate", "up"]
  envFrom:
  - secretRef:
      name: db-creds
```

If the migration fails (exit 1), the Pod will keep restarting the init container, blocking the main app from starting. This is usually the *right* failure mode: a Pod that ran with a non-migrated DB would corrupt data.

**c. Fetch secrets from an external store:**

```yaml
initContainers:
- name: vault-fetch
  image: vault-init:1
  volumeMounts:
  - name: secrets
    mountPath: /vault-out
volumes:
- name: secrets
  emptyDir:
    medium: Memory       # tmpfs, doesn't touch disk
```

(Vault Agent Injector does this via mutating webhook injection.)

**d. Set sysctls or kernel params:**

```yaml
initContainers:
- name: sysctl-tuner
  image: busybox:1.36
  securityContext:
    privileged: true
  command:
  - sh
  - -c
  - |
    sysctl -w net.core.somaxconn=65535
    sysctl -w vm.swappiness=0
```

This requires `privileged: true` because the sysctls being set are *unsafe* (not in the kubelet's allowlist). Using an init container scoped privilege grant rather than running the whole app privileged is the canonical pattern.

### 6.4 Init container limits

- Cannot have `readinessProbe` (they're either running or done — readiness is meaningless).
- Cannot have `lifecycle.preStop` (they don't have a graceful shutdown phase — they just exit).
- Cannot be patched in place (init containers are immutable like any container spec field).

---

## 7. Native Sidecars (KEP-753, GA in 1.29)

For years, the "sidecar" pattern (a helper container running alongside the main app — Envoy, Vault Agent, Fluent Bit, etc.) was implemented as a *regular* container in `spec.containers`. This had three serious problems:

1. **No startup ordering.** Main and sidecar started in parallel. The app would race against the sidecar — connections to localhost:15000 would fail until Envoy was ready.
2. **No termination ordering.** When the Pod was deleted, all containers got SIGTERM at the same time. Envoy would exit *before* the app finished draining, dropping the app's last in-flight requests.
3. **No Job semantics.** A Job pod with `restartPolicy: Never` would never complete if the sidecar didn't exit on its own — the sidecar would just keep running while the Job container had already exited 0.

KEP-753 (sidecar containers as a first-class concept) fixed all three with a clever encoding: a sidecar is just an **init container with `restartPolicy: Always`**.

### 7.1 The encoding

```yaml
spec:
  initContainers:
  - name: envoy
    image: envoy:1.30
    restartPolicy: Always       # <-- THIS is what makes it a native sidecar
    ports:
    - containerPort: 15000
    startupProbe:
      httpGet:
        path: /ready
        port: 15000
  - name: db-migrate
    image: my-app:1.2
    command: ["/app", "migrate"]
    # no restartPolicy → regular init container
  containers:
  - name: app
    image: my-app:1.2
    ports:
    - containerPort: 8080
```

Native sidecars live in `initContainers` but with `restartPolicy: Always`. They:

- Start in initContainer order, like other init containers.
- Are considered "started" when their startup/readiness probe passes (so the next init container or the main containers can start).
- **Keep running** alongside main containers (unlike regular init containers, which run to completion).
- **Restart if they crash** during the Pod's running phase (unlike regular init containers, which would fail the whole Pod).
- **Terminate AFTER** all main containers exit, during Pod termination. This is the critical missing piece.

### 7.2 Lifecycle timeline diagram

```
                              POD LIFECYCLE WITH NATIVE SIDECAR
   ──────────────────────────────────────────────────────────────────────────────
   
   t=0      Pod scheduled, kubelet starts
            
   t=1      pause container running
   
   t=2      ┌──────────────────────────────────────────────────────────┐
            │  envoy (native sidecar — initContainer w/ Always)        │
            │  starting up, startup probe pending                       │
            └──────────────────────────────────────────────────────────┘
   
   t=5      envoy startup probe PASSES → "started"
            
            ┌──────────────────────────────────────────────────────────┐
            │  db-migrate (regular init container)                      │
            │  running…                                                 │
            └──────────────────────────────────────────────────────────┘
   
   t=15     db-migrate exits 0
   
            ┌──────────────────────────────────────────────────────────┐
            │  app (main container)                                     │
            │  starting…                                                │
            └──────────────────────────────────────────────────────────┘
   
   t=18     app readiness probe passes → Pod is Ready, in Endpoints
            
            ───────────── steady state: envoy + app running ───────────
            
   t=300    user runs `kubectl delete pod`
            deletionTimestamp set, Pod removed from Endpoints
            
   t=300.1  preStop hooks run (in parallel) on every main container
            
   t=300.5  SIGTERM to main container "app"
            ↓
   t=305    app drains, exits 0
            ↓ (only NOW the sidecar gets SIGTERM)
   t=305.1  SIGTERM to "envoy"
            ↓
   t=306    envoy drains, exits 0
            ↓
            pause stopped, sandbox torn down, podIP released
   ──────────────────────────────────────────────────────────────────────────────
```

The key invariant: **native sidecars do NOT receive SIGTERM until all main containers have exited.** This is what makes the pattern useful for Envoy (it must outlive the app to drain), Vault Agent (must serve credentials until app stops needing them), and Fluent Bit (must flush logs after the app has stopped writing).

### 7.3 Why this is a big deal

Pre-1.28, Istio's sidecar pattern required:

- A custom `preStop` on the *app* container with `sleep 5` to wait for Endpoints removal.
- A custom `lifecycle.preStop` on Envoy with `pilot-agent wait` to make Envoy drain only after the app had stopped.
- A custom shell wrapper to ensure the app didn't start until Envoy's xDS was ready.

All of this becomes unnecessary with native sidecars. The same is true for Vault Agent (which had a complex "wait until app dies before exiting" trick using shared filesystem signals), and for log shippers that lost the last seconds of logs on Pod death.

### 7.4 Restart semantics during steady state

If the native sidecar **crashes** while main containers are running, it restarts independently (subject to the kubelet's per-container backoff — §11). The main containers are unaffected. This is exactly the behavior you want — Envoy crashing should not kill the app, just restart Envoy.

If a **main container** crashes, the Pod's `restartPolicy` applies (Always → restart, OnFailure → restart on non-zero, Never → terminate). The sidecar is *not* affected by a main container crash; it keeps running.

### 7.5 Interaction with `restartPolicy: Never` and Jobs

This is the killer feature for batch workloads. Pre-1.28, a Job with a sidecar (e.g., a log shipper) would never complete:

```yaml
# DOES NOT WORK pre-1.28
spec:
  restartPolicy: Never
  containers:
  - name: worker
    image: batch-job:1
  - name: log-shipper
    image: fluent-bit:3        # keeps running forever
```

`worker` exits 0, `log-shipper` keeps running, so the Pod stays in `Running` phase — Job is never satisfied. Workaround was a custom shutdown signal between containers (Kubernetes-native API didn't help).

With native sidecars:

```yaml
spec:
  restartPolicy: Never
  initContainers:
  - name: log-shipper
    image: fluent-bit:3
    restartPolicy: Always
  containers:
  - name: worker
    image: batch-job:1
```

When `worker` exits 0, the kubelet sends SIGTERM to `log-shipper`, waits for it to exit, and the Pod transitions to `Succeeded`. The Job completes cleanly.

### 7.6 What native sidecars cannot do

- They cannot have a Pod-level dependency relationship beyond "start before main containers" — they cannot say "start after some other sidecar".
- Their `restartPolicy: Always` is a per-container field; it cannot be overridden at the Pod level.
- They still count toward the Pod's effective resource requests (sum, not max).

---

## 8. Ephemeral Containers (Debug Containers)

A Pod's containers list is immutable after creation. But operators sometimes need to add a container *to a running Pod* for debugging — to attach strace, to run tcpdump in the same network namespace, to inspect the filesystem of a crashed app. Ephemeral containers solve this.

### 8.1 What they are

An ephemeral container is a container added to a running Pod via the `pods/ephemeralcontainers` subresource. They are listed in `spec.ephemeralContainers` (read-only via normal GET) and treated like regular containers for namespace sharing — but with restrictions:

- **No `ports`** (the Pod's network is already bound; you can't expose new ports).
- **No `livenessProbe` / `readinessProbe` / `startupProbe`** (they're transient).
- **No `resources`** (they share the Pod's resource budget without contributing to scheduling).
- **No `lifecycle`** (no postStart/preStop).
- **Cannot be removed** once added — only Pod deletion removes them.

### 8.2 The `kubectl debug` UX

The typical entry point is `kubectl debug`:

```
kubectl debug my-pod -it --image=busybox --target=app -- /bin/sh
```

This adds an ephemeral container with `targetContainerName: app`, which means the new container shares **app's PID namespace** (in addition to the Pod's shared network, IPC, UTS). Once attached, you can:

- `ps aux` and see app's processes (they share PID ns).
- Look at `/proc/<app_pid>/root/etc/passwd` (you have access to app's mount namespace via procfs).
- `strace -p <app_pid>` — if your security context permits SYS_PTRACE.
- `curl localhost:8080` — you share the network namespace.

### 8.3 Why a subresource

The reason ephemeral containers use the special `pods/ephemeralcontainers` subresource is to keep them out of the normal `update` codepath. Normal `update` of `pod.spec` is forbidden (only a small allowlist mutates). Ephemeral containers needed a separate write path to bypass that. RBAC for debug is therefore `update pods/ephemeralcontainers`, granted separately from generic Pod update.

### 8.4 What they look like in the API

```go
type EphemeralContainer struct {
    EphemeralContainerCommon `json:",inline"`
    TargetContainerName      string  // PID-namespace target
}

type EphemeralContainerCommon struct {
    // Same fields as Container, MINUS ports/probes/resources/lifecycle
    Name             string
    Image            string
    Command          []string
    Args             []string
    Env              []EnvVar
    VolumeMounts     []VolumeMount
    SecurityContext  *SecurityContext
    // ... etc
}
```

### 8.5 The image trick

`kubectl debug` defaults to whatever image `--image` says. A common pattern is to maintain a "debug image" with strace, tcpdump, dig, curl, ngrep, lsof, gdb, etc., baked in:

```
kubectl debug my-pod --image=registry.example.com/debug:1 --target=app
```

This is much better than baking a debug shell into your production image (smaller production attack surface, faster pulls).

### 8.6 Limitations

- Ephemeral containers don't restart on crash — if your debug shell dies, you must add another.
- They count against the pod's CPU/memory `limits` if the Pod has them, since they live in the same cgroup.
- They cannot be created on Pods that don't yet exist; only on running (or even failed) Pods.

---

## 9. The Pod Lifecycle Phase Machine

Every Pod has a `status.phase`, which is one of five values. The phase is *derived* from container states by the kubelet's status manager — it is a coarse summary, not the source of truth.

```
   ┌─────────────┐
   │   Pending   │   Pod accepted, not yet running all containers
   └──────┬──────┘   (waiting for scheduling, image pull, init containers)
          │
          │ at least one container in Running, OR all containers in Waiting,
          │ but at least one with started status
          ▼
   ┌─────────────┐
   │   Running   │   At least one container is running or starting
   └──┬──────┬───┘
      │      │
      │      │ all containers terminated
      │      │
      │      ▼
      │  ┌─────────────────────────────────┐
      │  │  if restartPolicy != Always     │
      │  │  AND every container exited 0  │
      │  │  → Succeeded                    │
      │  └─────────────────────────────────┘
      │
      ▼
   ┌─────────────┐         ┌─────────────┐
   │  Succeeded  │         │   Failed    │   at least one container exited
   │ (terminal)  │         │ (terminal)  │   non-zero (with restartPolicy != Always)
   └─────────────┘         └─────────────┘
   
   ┌─────────────┐
   │   Unknown   │   kubelet cannot be reached / status lost
   │  (legacy)   │   (in modern releases, Node lifecycle controller marks pods
   └─────────────┘    NotReady but rarely sets phase=Unknown)
```

### 9.1 Phase derivation rules (kubelet)

The kubelet's `pkg/kubelet/status/status_manager.go` computes phase roughly:

```
if any container is in Waiting with reason in {CreateContainerConfigError, ImagePullBackOff, ErrImagePull, ...}:
    phase = Pending
elif any container is Running:
    phase = Running
elif all containers are Terminated:
    if all exit codes == 0:
        if restartPolicy == Always:
            phase = Running    # they'll be restarted
        else:
            phase = Succeeded
    else:
        if restartPolicy == Always or (restartPolicy == OnFailure and at least one failed):
            phase = Running    # they'll be restarted
        else:
            phase = Failed
else:
    phase = Pending
```

In other words, `Running` doesn't mean "everything's fine" — it means "at least one container is supposed to be alive or about to be alive." A Pod in CrashLoopBackOff has phase `Running` because the kubelet plans to restart the container. Read **conditions**, not **phase**, to know if a Pod is healthy.

### 9.2 Conditions

`status.conditions` is a list of named typed conditions:

| Condition Type | Meaning |
|---|---|
| `PodScheduled` | Pod has been bound to a node (`spec.nodeName` set). |
| `Initialized` | All init containers have completed successfully. |
| `ContainersReady` | All non-init containers are in `Ready` state (passing readinessProbe, or no probe defined). |
| `Ready` | The Pod is ready to serve. = `ContainersReady` AND all `readinessGates` are True. |
| `PodReadyToStartContainers` (1.29+) | The pod's network and runtime are set up; container creation can begin. Replaces the older `PodHasNetwork`. |
| `DisruptionTarget` | Set by the apiserver when a disruption (eviction, preemption) targets this Pod. |

Endpoints controllers look at `Ready=True`; the scheduler looks at `PodScheduled`; the kubelet itself writes `Initialized` and `ContainersReady`.

### 9.3 Why phase is the wrong thing to alert on

- `Running` includes Pods in CrashLoopBackOff.
- `Pending` includes Pods waiting on image pull (transient) and Pods that can never schedule (permanent — needs intervention).
- `Succeeded` for a Deployment Pod would be very strange (they're never supposed to complete), but it can happen if `restartPolicy` got misconfigured.

Production monitoring should alert on:

- `Pending` for > 5 min (scheduler problem).
- `containerStatuses[].restartCount > N` in a window (CrashLoop).
- `Ready=False` for > 30s (degraded serving).
- `phase=Failed` (terminal).

---

## 10. The Container State Machine

While the Pod has 5 phases, each container has 3 states (per `status.containerStatuses[].state`):

```
                  ┌─────────────┐
                  │   Waiting   │
                  │             │
                  │  reasons:   │
                  │   - ContainerCreating (kubelet asked CRI, creating)
                  │   - PodInitializing   (init containers still running)
                  │   - PullBackOff       (failing to pull image)
                  │   - ErrImagePull      (one-off pull failure)
                  │   - CreateContainerConfigError
                  │           (missing CM/Secret referenced in env/volume)
                  │   - CreateContainerError
                  │           (runtime rejected create — e.g., bad image)
                  │   - CrashLoopBackOff  (restarting after crash)
                  └──────┬──────┘
                         │ CRI: StartContainer succeeds
                         ▼
                  ┌─────────────┐
                  │   Running   │
                  │             │
                  │  startedAt  │
                  └──────┬──────┘
                         │ process exits OR kubelet kills it
                         ▼
                  ┌─────────────┐
                  │ Terminated  │
                  │             │
                  │  fields:    │
                  │   exitCode  │
                  │   signal    │
                  │   reason    │   (Completed | Error | OOMKilled |
                  │              │     ContainerCannotRun | DeadlineExceeded |
                  │              │     Evicted | Unknown)
                  │   message   │   (from /dev/termination-log)
                  │   startedAt │
                  │   finishedAt│
                  │   containerID
                  └──────┬──────┘
                         │ restartPolicy applies:
                         │   Always         → back to Waiting
                         │   OnFailure if exitCode != 0 → back to Waiting
                         │   Never          → stay Terminated
                         ▼
                     (back to Waiting, or stay Terminated)
```

The container state is stored in both `state` (current) and `lastState` (previous). `lastState` is *gold* for debugging: it tells you the exit code and reason of the most recent crash, even if the container is now Running again.

### 10.1 Reading container states

```
$ kubectl get pod my-app -o jsonpath='{.status.containerStatuses[*].state}'
$ kubectl get pod my-app -o jsonpath='{.status.containerStatuses[*].lastState}'
$ kubectl describe pod my-app
  ...
  Containers:
    app:
      State:          Running
        Started:      Mon, 18 May 2026 12:00:00 +0000
      Last State:     Terminated
        Reason:       OOMKilled
        Exit Code:    137
        Started:      Mon, 18 May 2026 11:59:30 +0000
        Finished:     Mon, 18 May 2026 12:00:00 +0000
      Ready:          True
      Restart Count:  3
```

A `Reason: OOMKilled` (exit code 137 = 128 + 9 = killed by SIGKILL from the OOM killer) is the most common diagnostic signal. The fix is almost always to raise the memory limit — see [ch 21](21-resource-management-and-qos.md).

### 10.2 Other useful fields

- `started: true` — for Pods with `startupProbe`, this flips to true only when the startup probe succeeds. The liveness/readiness probes do not run until then. After the startup probe passes, `started` stays true for the lifetime of the container, and is reset to false on container restart.
- `ready: true` — passes readiness probe (or no probe), and the kubelet judges this container "ready to receive traffic". The `ContainersReady` Pod condition is True iff all containers have `ready=true`.
- `restartCount: N` — number of times the kubelet has restarted this container. *Resets only on Pod recreation, not on Pod restart.* High and growing = CrashLoop.

---

## 11. CrashLoopBackOff: The Kubelet's Local Backoff

`CrashLoopBackOff` is a `waiting.reason` value. It means the container has exited at least once (or been killed) and the kubelet is delaying the next restart with an exponential backoff. This backoff is **kubelet-local**, **per-container**, and **not controlled by any higher-level controller**.

### 11.1 The backoff schedule

From `pkg/kubelet/kuberuntime/kuberuntime_manager.go`, the backoff doubles each restart, with a cap:

```
restart 1:    10 seconds
restart 2:    20 seconds
restart 3:    40 seconds
restart 4:    80 seconds
restart 5:   160 seconds
restart 6:   300 seconds   (the cap; some releases 300s, some 5m exact)
restart 7:   300 seconds
restart N:   300 seconds
```

The backoff timer is **reset** if the container manages to stay running for the threshold duration (default: the backoff is reset after the container has been running successfully for the cap duration). So a flaky container that crashes once an hour will never get into the high-delay territory — only one that crashes within seconds of starting.

### 11.2 Where it lives

`backoffEntry` is in `pkg/kubelet/util/flowcontrol` or similar (depending on K8s version). Each `<pod_uid>_<container_name>` key has its own backoff state in the kubelet's memory. **The state is lost when the kubelet restarts** — if you `systemctl restart kubelet`, the backoff resets. (This is sometimes used as a hack: "I just restarted kubelet and now my Pod started; was the kubelet broken?" No, the backoff was just paused.)

### 11.3 What CrashLoopBackOff actually means

`CrashLoopBackOff` itself is not an error — it's the kubelet *waiting*. The actual error is in `lastState.terminated`. Don't fix `CrashLoopBackOff`; fix the underlying exit. The error categorization:

| `lastState.terminated.reason` | What to look at |
|---|---|
| `OOMKilled` (exit 137) | Raise memory limit, or fix the leak. |
| `Error` (exit 1, 2, …, any non-zero) | Look at `kubectl logs --previous`. |
| `ContainerCannotRun` | The OCI runtime refused to start — usually bad command, missing executable. |
| `DeadlineExceeded` | `activeDeadlineSeconds` hit. |
| `Completed` (exit 0) + restart | Job-shaped workload with `restartPolicy: Always` (you usually want OnFailure/Never). |

### 11.4 No, you can't tune the backoff (much)

Kubelet has a flag `--container-runtime-pod-sandbox-attempt-restart` etc., but the per-container exponential backoff is hard-coded for most installations. KEP-3782 has proposed making it tunable; not yet GA at the time of writing.

---

## 12. Probes: Startup, Readiness, Liveness

Three probes, three roles, three state machines.

### 12.1 The big picture

```
   ┌──────────────────────────────────────────────────────────────────┐
   │                                                                  │
   │  Container starts                                                │
   │      │                                                           │
   │      ▼                                                           │
   │  ┌─────────────────┐                                            │
   │  │  startupProbe   │  if defined: runs until success (or fail   │
   │  │  (1.16+, GA     │  threshold). UNTIL success, liveness &     │
   │  │   1.20)         │  readiness do NOT run.                     │
   │  └────────┬────────┘                                            │
   │           │ success                                              │
   │           ▼                                                      │
   │  ┌────────────────────────────────────────────────────────────┐ │
   │  │       (startup never runs again for this container)         │ │
   │  │                                                              │ │
   │  │  ┌────────────────┐         ┌────────────────┐             │ │
   │  │  │  readinessProbe│         │  livenessProbe │             │ │
   │  │  │                │         │                │             │ │
   │  │  │  fail → remove │         │  fail → kubelet│             │ │
   │  │  │  from Endpoints│         │  kills the     │             │ │
   │  │  │  (does NOT     │         │  container     │             │ │
   │  │  │   restart)     │         │  (restart per  │             │ │
   │  │  │                │         │  policy)       │             │ │
   │  │  └────────────────┘         └────────────────┘             │ │
   │  └────────────────────────────────────────────────────────────┘ │
   └──────────────────────────────────────────────────────────────────┘
```

### 12.2 Handler types

All four take the same tuning fields (`initialDelaySeconds`, `periodSeconds`, `timeoutSeconds`, `successThreshold`, `failureThreshold`). The handler differs:

**`httpGet`:**

```yaml
httpGet:
  path: /healthz
  port: 8080
  scheme: HTTP            # or HTTPS
  host: ""                 # default: pod IP
  httpHeaders:
  - name: X-Probe
    value: kubelet
```

Success = 2xx or 3xx response within `timeoutSeconds`. Redirects (3xx) are NOT followed. TLS cert verification is **disabled** for HTTPS probes — they accept self-signed certs.

**`tcpSocket`:**

```yaml
tcpSocket:
  port: 5432
```

Success = TCP three-way handshake completes within `timeoutSeconds`. Useful for databases that don't expose HTTP.

**`exec`:**

```yaml
exec:
  command: ["/bin/sh", "-c", "test -f /tmp/ready && echo healthy"]
```

Success = command exits 0 within `timeoutSeconds`. Expensive — forks a process every period.

**`grpc` (stable in 1.27):**

```yaml
grpc:
  port: 9000
  service: my.proto.HealthCheck  # optional
```

Uses the gRPC Health Checking Protocol. Kubelet sends `grpc.health.v1.Health/Check`, expects `status: SERVING`. Replaces the old `grpc-health-probe` exec hack.

### 12.3 The probe state machine

For each probe:

```
        ┌──────────────────────────┐
        │   waiting for first run  │
        │   (initialDelaySeconds)  │
        └────────────┬─────────────┘
                     │
                     ▼
        ┌──────────────────────────┐
        │   running probe every    │
        │   periodSeconds          │
        └────────────┬─────────────┘
                     │ probe completes
                     ▼
              ┌─────────────┐
              │  successful │  → reset failure counter
              │  ?          │
              └──────┬──────┘
                     │ no
                     ▼
              ┌─────────────┐
              │ failure ctr │
              │ ++          │
              └──────┬──────┘
                     │
                     ▼
        ┌──────────────────────────┐
        │ failureCtr ≥             │
        │ failureThreshold ?       │
        └────────┬─────────────────┘
                 │ yes
                 ▼
        ┌──────────────────────────┐
        │ liveness  → kill container│
        │ readiness → mark NotReady │
        │ startup   → kill container│
        └──────────────────────────┘
```

`successThreshold` matters only for readiness — to mark a container "Ready" after recovery, it must succeed N times in a row. For liveness/startup, only the failure side matters (you don't "fail" by being healthy briefly).

### 12.4 Tuning rules of thumb

| Probe | Goal | Bias |
|---|---|---|
| `startup` | Tolerate slow boots (JVM, large model load). | Long `failureThreshold`, generous `periodSeconds`. Once passing, it stops running. |
| `readiness` | Reflect the container's *current* ability to serve. | Short period (1–2s), low timeout (1s), moderate failure threshold (3). Don't depend on downstream services unless you really want to be removed when they're down. |
| `liveness` | Detect *deadlock*, not slowness. | Conservative — long initial delay, long period, long timeout, high failure threshold. The cost of a false-positive liveness fail is a restart. |

### 12.5 The common-mistake matrix

| Mistake | Symptom |
|---|---|
| Liveness too aggressive (low timeout, low threshold) | Pods restart under load when GC pauses or downstream is slow. Restart loop under traffic spike. |
| Liveness equals readiness | Slow downstream = pod restart = cold start = even slower downstream. Cascading failure. |
| Readiness depends on a database the Pod is also trying to connect to | Chicken-and-egg if the database is in the same Pod, or in a Pod that depends on this one. Pod becomes never-Ready. |
| No `startupProbe` for slow-starting apps | Liveness fires before the app is up, kills the container, repeat forever — infinite CrashLoopBackOff. |
| `exec` probe forking a heavy script every second | CPU consumed by probe; throttling kicks in; probe itself starts timing out. |
| Probing localhost when app binds to 0.0.0.0 but inside an init namespace race | Race condition between Envoy's iptables setup and probe — Istio's istio-init container reroutes everything through Envoy, including probe traffic. (Native sidecars + `holdApplicationUntilProxyStarts` fixed this.) |

### 12.6 `terminationGracePeriodSeconds` on the probe itself

Since 1.22, individual probes can carry a `terminationGracePeriodSeconds`, which **overrides** the Pod-level value *for the liveness-kill path only*. Use case: liveness failure should kill fast (don't wait the default 30s for graceful shutdown if the container is wedged), without affecting normal shutdown grace.

---

## 13. Lifecycle Hooks: postStart and preStop

`spec.containers[*].lifecycle` has two optional hooks:

```yaml
lifecycle:
  postStart:
    exec:
      command: ["/usr/local/bin/register.sh"]
  preStop:
    httpGet:
      path: /drain
      port: 8080
```

Handler types are the same as probes: `exec`, `httpGet`, `tcpSocket` (and `sleep` since 1.29 — a no-op handler that just sleeps for N seconds, which removes the need to bake `sleep` into the container's image).

### 13.1 `postStart` — runs concurrently with the container's command

A common misconception is that `postStart` runs *before* the container's main process. It does not. The kubelet:

1. Sends `StartContainer` to the CRI.
2. The runtime fork+execs the container's command.
3. **At the same time**, the kubelet invokes the postStart handler (against the running container, via `exec` or HTTP into it).
4. The container is not marked Ready (its `started: false` and `ready: false`) **until postStart completes**.

If postStart fails (`exec` exits non-zero, HTTP returns non-2xx), the container is killed and the Pod's `restartPolicy` applies. Use cases: warm caches, register the Pod in some external service, signal "I'm here" to siblings.

`postStart` runs at most once per container start.

### 13.2 `preStop` — synchronous before SIGTERM

When the kubelet decides to stop a container (Pod deletion, eviction, preemption, drain), it:

1. Sends the `preStop` handler. It is **synchronous** — kubelet waits for it to return before sending SIGTERM.
2. Sends SIGTERM to PID 1.
3. Starts the `terminationGracePeriodSeconds` countdown.
4. If the container hasn't exited at the deadline, sends SIGKILL.

The catch: **`preStop` time is included in `terminationGracePeriodSeconds`.** If your `preStop` is `sleep 25` and your `terminationGracePeriodSeconds` is 30, you have only 5 seconds left after preStop for SIGTERM to take effect. If your `preStop` is `sleep 60` and `terminationGracePeriodSeconds` is 30, the kubelet **kills the preStop hook at 30s** and SIGKILLs the container. Always set `terminationGracePeriodSeconds` ≥ (preStop duration + expected SIGTERM-to-exit time + safety margin).

### 13.3 The `sleep` handler (1.29+)

```yaml
preStop:
  sleep:
    seconds: 5
```

Before 1.29, you had to do `exec: ["/bin/sleep", "5"]`, which required `sleep` to exist in the container image — a non-trivial assumption for distroless images. The native sleep handler removes that dependency.

### 13.4 Use cases

**a. Graceful HTTP shutdown:**

```yaml
preStop:
  httpGet:
    path: /shutdown
    port: 8080
```

App's `/shutdown` endpoint stops accepting new connections, drains in-flight requests, returns 200, then exits when SIGTERM arrives.

**b. Endpoint propagation race fix:**

```yaml
preStop:
  sleep:
    seconds: 5
```

Pod's endpoint is removed from EndpointSlice at the moment deletionTimestamp is set, but kube-proxies and load balancers take time to propagate the change. The 5-second sleep keeps the container alive long enough for the propagation to complete *before* SIGTERM is sent. (See §14.5.)

**c. External de-registration:**

```yaml
preStop:
  exec:
    command:
    - sh
    - -c
    - |
      curl -X POST http://consul:8500/v1/agent/service/deregister/$HOSTNAME
      sleep 5
```

### 13.5 `postStart` is NOT a substitute for an init container

If you need to do something *before* the container's main process starts (download a model, set up secrets), use an init container. `postStart` runs concurrent with the main process and cannot block it — only fail it. The mental model: init container = "before"; postStart = "alongside, just after kick-off".

---

## 14. The Termination Sequence

The single most-important sequence to memorize when running production Kubernetes. Errors in graceful shutdown cause dropped requests, leaked connections, and split-brain in stateful apps.

### 14.1 The canonical sequence

```
   T=0      User: `kubectl delete pod my-app`
            apiserver: set metadata.deletionTimestamp = now
                       set status condition DisruptionTarget=True
   T=0+ε    apiserver publishes update via watch
   
   T=0+δ1   ┌──────────────────────────────────────────────────────────┐
            │  endpoints/endpointslice-controller sees Pod has         │
            │  deletionTimestamp; removes Pod from EndpointSlice       │
            │  (Pod is now not in the list kube-proxies sync from)     │
            └──────────────────────────────────────────────────────────┘
   
   T=0+δ2   ┌──────────────────────────────────────────────────────────┐
            │  kube-proxies on every node (eventually) update          │
            │  iptables/IPVS rules to drop the deleted Pod's IP        │
            │  from Service VIP NAT targets. THIS TAKES TIME —         │
            │  watch propagation + reconcile interval                  │
            └──────────────────────────────────────────────────────────┘
   
   T=0+δ3   kubelet on Pod's node sees deletionTimestamp via watch
            
            For each container in the Pod (in PARALLEL, ALL main
            containers; native sidecars wait for main to finish):
              │
              ▼
            ┌────────────────────────────────────────┐
   T=0+δ3   │ preStop handler runs                   │
            │   (synchronous — kubelet waits)        │
            │   timeout = terminationGracePeriodSec  │
            └────────────────────────────────────────┘
              │
              ▼
            ┌────────────────────────────────────────┐
   T=δ3+H   │ SIGTERM sent to container's PID 1      │
            │   (H = preStop duration)               │
            └────────────────────────────────────────┘
              │
              │  (terminationGracePeriodSeconds - H) remaining
              │
              ▼
            ┌────────────────────────────────────────┐
   T=GP     │ if container still running:            │
            │   SIGKILL sent to PID 1                │
            │   (GP = terminationGracePeriodSeconds  │
            │    measured from deletion start)       │
            └────────────────────────────────────────┘
   
   T=GP+ε   All main containers exited.
            Native sidecars now receive SIGTERM
            (same sequence: preStop, SIGTERM, wait, SIGKILL).
   
   T=GP+ε2  All containers exited.
            kubelet: tear down pause container; release CNI resources
            (CNI DEL); unmount volumes (CSI NodeUnpublish, NodeUnstage,
            ControllerUnpublish if last attachment).
   
   T=final  kubelet: PATCH pod.status finalizers list, removing kubelet's
            implicit "I owe you cleanup" intent. When no finalizers
            remain, the apiserver deletes the Pod object from etcd.
```

### 14.2 The default `terminationGracePeriodSeconds`

Default is **30 seconds**. Set it explicitly. For databases, set higher (60–600s) — they need time to flush WAL, checkpoint, drain replication. For stateless HTTP, 30s is usually plenty.

```yaml
spec:
  terminationGracePeriodSeconds: 60
```

**Editable on a running Pod** — but only *downwards*. You can shrink to 1 to force a faster delete; you cannot extend.

### 14.3 The signal: which PID receives SIGTERM?

PID 1 inside the container's PID namespace. Whatever process the container's command launched. If your container runs:

```
ENTRYPOINT ["/bin/sh", "-c", "exec /usr/bin/myapp"]
```

then `sh` execs `myapp`, replacing itself, so PID 1 is `myapp` and signals work. But:

```
ENTRYPOINT ["/bin/sh", "-c", "/usr/bin/myapp"]
```

means `sh` forks `myapp`. Now PID 1 is `sh`, which does not forward SIGTERM to its children by default — `myapp` keeps running until the SIGKILL at the deadline. This is **the #1 graceful-shutdown bug** in container images.

Fixes:

1. Use `exec` in your shell wrapper (best).
2. Use a real init (tini, dumb-init, s6-overlay) as PID 1, which forwards signals.
3. Use `command: ["/usr/bin/myapp"]` directly, bypassing shell.

### 14.4 The shutdown sequence inside the container

A well-behaved app handles SIGTERM by:

1. Setting a "shutting down" flag.
2. Closing the listening socket (no new connections).
3. Letting in-flight requests complete with a timeout.
4. Closing external connections (DB pool, message queues) gracefully.
5. Exiting 0.

If steps 1–5 take less than `terminationGracePeriodSeconds - preStopDuration`, the Pod terminates cleanly. Otherwise, SIGKILL truncates it.

### 14.5 The Endpoints removal race (the most-quoted Kubernetes bug)

The Pod is removed from EndpointSlice the moment `deletionTimestamp` is set. **But**:

- Watch events take ms-to-seconds to propagate to every kube-proxy.
- Each kube-proxy syncs iptables/IPVS on a scheduled interval (default ~30s, but immediate on event).
- Cloud load balancers (especially external) have their own propagation delay — sometimes 30+ seconds.

If your app receives SIGTERM and closes the listening socket *immediately*, but some Service VIP is still DNAT-ing to your podIP, the next packet arriving gets RST/connection-refused. To clients, this looks like a partial outage during deploys.

**The fix**: insert a `preStop` sleep that gives the system time to propagate the Endpoints removal *before* the container shuts down:

```yaml
spec:
  terminationGracePeriodSeconds: 45      # 5s sleep + 40s for actual shutdown
  containers:
  - name: app
    lifecycle:
      preStop:
        sleep:
          seconds: 5
```

This 5-second sleep is one of the most-discussed "production K8s tricks" and is now baked into every popular Helm chart (NGINX, Envoy, etc.). On large clusters with slow LBs, bump to 10–30 seconds.

### 14.6 Finalizers and the Pod object's lifetime

Even after every container has exited, the Pod object stays in etcd until:

1. All volumes are unmounted (the kubelet writes its volume cleanup as a finalizer in some configurations, though most volume cleanup is non-finalizer-based).
2. Any user-added finalizers are cleared by their respective controllers.

The Pod's `metadata.deletionGracePeriodSeconds` shows the grace period; you may see Pods stuck in `Terminating` for a long time waiting on a finalizer or a volume that the CSI driver can't detach (most often: cloud volume stuck attached to a dead node).

`kubectl delete pod X --force --grace-period=0` removes the Pod object from etcd immediately, **without waiting for the kubelet to confirm container cleanup**. If the node is alive and responsive, this leaves a running container with no API representation — a ghost container holding the volume, the IP, and the resources. Use only when the node is genuinely dead and you've accepted the risk.

---

## 15. podIP, podIPs, and the Pod Networking Model

A Pod has exactly one routable IP (or a small set, with dual-stack). It is allocated by the CNI ([ch 15](15-cni-and-pod-networking.md)) at sandbox-creation time and cannot change during the Pod's lifetime.

### 15.1 The fields

```yaml
status:
  hostIP: 192.168.1.42       # the node's address
  podIP: 10.244.7.42
  podIPs:
  - ip: 10.244.7.42
  - ip: fd00::1234           # dual-stack v6
```

- `status.podIP` — the primary IP (first entry in `podIPs`). Always present once the CNI has assigned.
- `status.podIPs` — list of one IP per family the Pod has. For single-stack clusters, length 1.
- `status.hostIP` — the node's IP, equivalent to `spec.nodeName`'s primary InternalIP.

### 15.2 When podIP is assigned

```
                kubelet has Pod with spec.nodeName=this_node
                       │
                       ▼
                CRI: RunPodSandbox(podSandboxConfig)
                       │
                       ▼
                runtime (containerd/CRI-O): create pause container,
                set up its network namespace (empty so far)
                       │
                       ▼
                runtime invokes CNI ADD (via CNI plugin chain)
                       │
                       ▼
                CNI plugin: IPAM assigns 10.244.7.42;
                creates veth pair (host end + pod end);
                moves pod end into pause's netns;
                sets up routes inside; sets up routes on host bridge.
                       │
                       ▼
                CNI returns the assigned IP to runtime
                       │
                       ▼
                runtime returns sandbox_id to kubelet
                       │
                       ▼
                kubelet PATCHes pod.status with podIP
```

The condition `PodReadyToStartContainers` (formerly `PodHasNetwork`) flips True at this point.

### 15.3 Why the podIP is immutable for the Pod's life

The podIP is owned by the pause container's netns. As long as pause is alive, the netns is alive, and the IP keeps. Restarting a non-pause container reuses the netns and the IP. Only a *Pod restart* (which means a new sandbox, a new pause, a new CNI ADD) gives a new podIP. From the Pod object's perspective: a "restart" is impossible — the Pod object you see is the same Pod with the same IP until its eventual deletion.

### 15.4 The Kubernetes networking model

The model has three rules:

1. **Every Pod has a routable IP** in the cluster pod CIDR.
2. **Every Pod can talk to every other Pod** without NAT.
3. **The IP a Pod sees itself as** (via `ifconfig`, `ip addr`) **is the same one other Pods/services see it as.**

This is unlike Docker's default bridge networking (which uses NAT). It's the foundation that makes Services, NetworkPolicies, and east-west routing tractable. How it's implemented (overlay vs underlay vs eBPF) is the CNI plugin's choice — [ch 15](15-cni-and-pod-networking.md) and [ch 16](16-cilium-and-ebpf-deep-dive.md).

### 15.5 Dual-stack

When the cluster is dual-stack (Linux 5.x+, K8s 1.21+), `podIPs` has both an IPv4 and IPv6 address. Each Pod's Service has either a v4 ClusterIP, a v6 ClusterIP, or both (depending on Service's `ipFamilyPolicy`). Apps must `listen` on both to receive both kinds of traffic — usually `[::]` plus dual-bind on Linux works.

---

## 16. DNS and the Cluster DNS Contract

Every Pod has a `/etc/resolv.conf` rendered by the kubelet from `spec.dnsPolicy` and `spec.dnsConfig`. The DNS contract makes Service names resolvable cluster-wide.

### 16.1 `dnsPolicy`

```yaml
spec:
  dnsPolicy: ClusterFirst    # the default
```

| Value | What's in resolv.conf |
|---|---|
| `ClusterFirst` (default) | Cluster DNS (CoreDNS Service IP) first, then node fallback for non-cluster names. |
| `ClusterFirstWithHostNet` | Same as ClusterFirst but explicitly works with `hostNetwork: true` (the implicit "host net = no cluster DNS" is overridden). |
| `Default` | Inherit the node's `/etc/resolv.conf`. The Pod does *not* see cluster DNS. |
| `None` | Empty resolv.conf, populated only by `spec.dnsConfig`. |

### 16.2 The default resolv.conf

For a Pod in namespace `prod` with `dnsPolicy: ClusterFirst`:

```
nameserver 10.96.0.10                          # CoreDNS ClusterIP
search prod.svc.cluster.local svc.cluster.local cluster.local example.com
options ndots:5
```

`ndots:5` means: any name with fewer than 5 dots is tried in each search domain *before* being tried as-is. This is what makes `kubectl curl http://service-name` resolve to `service-name.prod.svc.cluster.local`. It also means that any external lookup (`example.com`) makes 4 extra NXDOMAIN queries (cluster.local, svc.cluster.local, prod.svc.cluster.local, example.com.cluster.local — depending on order). On busy CoreDNS, this is a measurable load. [Ch 18](18-dns-and-coredns.md) explores NodeLocalDNS and DNS tuning.

### 16.3 `dnsConfig`

```yaml
spec:
  dnsPolicy: ClusterFirst    # or "None"
  dnsConfig:
    nameservers:
    - 1.1.1.1
    searches:
    - my.tenant.local
    options:
    - name: ndots
      value: "2"
    - name: edns0
```

Appended (or replaces if dnsPolicy=None) the auto-rendered config. Common use: lower `ndots` for Pods that mostly resolve external names (avoiding the search-domain explosion); add a custom search domain for multi-tenant setups.

---

## 17. Security Context: Pod vs Container

`securityContext` controls the **Linux security primitives** applied to the Pod and its containers: user ID, group ID, capabilities, seccomp profile, AppArmor profile, SELinux label, sysctls, file system read-only, etc.

### 17.1 Two levels

- **Pod-level** (`spec.securityContext`, type `PodSecurityContext`): applies to all containers in the Pod, plus has some Pod-only fields (`fsGroup`, `sysctls`, `supplementalGroups`).
- **Container-level** (`spec.containers[*].securityContext`, type `SecurityContext`): overrides Pod-level for that container.

Pod-only fields (cannot be set at container level): `fsGroup`, `fsGroupChangePolicy`, `supplementalGroups`, `sysctls`.

### 17.2 The major fields

```yaml
spec:
  securityContext:
    runAsUser: 1000
    runAsGroup: 3000
    runAsNonRoot: true
    fsGroup: 2000
    fsGroupChangePolicy: OnRootMismatch      # avoid recursive chown on large volumes
    supplementalGroups: [4000, 5000]
    seLinuxOptions:
      level: "s0:c123,c456"
    seccompProfile:
      type: RuntimeDefault
    sysctls:
    - name: net.ipv4.ip_local_port_range
      value: "1024 65535"
  containers:
  - name: app
    securityContext:
      readOnlyRootFilesystem: true
      allowPrivilegeEscalation: false
      privileged: false
      capabilities:
        drop: ["ALL"]
        add: ["NET_BIND_SERVICE"]
      procMount: Default
      seccompProfile:
        type: Localhost
        localhostProfile: profiles/app.json
```

### 17.3 Field-by-field

- `runAsUser` / `runAsGroup` — primary uid/gid. Default: whatever the image's USER directive says (often 0/root).
- `runAsNonRoot: true` — admission-time refusal if the resolved UID is 0. Cheap defense against misconfigured images.
- `fsGroup` — supplemental group GID applied to all volumes mounted in the Pod. The kubelet runs `chown -R :<fsGroup>` on the volume root and `chmod g+rwx` (with `setgid` bit). **On large persistent volumes, this `chown -R` can be very slow** — `fsGroupChangePolicy: OnRootMismatch` only does it if the root's gid is wrong (skip on subsequent restarts).
- `fsGroupChangePolicy`: `Always` (default) or `OnRootMismatch`.
- `supplementalGroups` — additional group memberships, beyond the image's `/etc/group`. Useful for joining a Pod to a host group that owns a hostPath.
- `seLinuxOptions` — SELinux user/role/type/level. On systems with enforcing SELinux, the runtime applies this label. Mandatory for many CSI drivers.
- `seccompProfile`:
  - `RuntimeDefault` — runtime's default profile (containerd/CRI-O ship a sensible default).
  - `Localhost` + `localhostProfile: path/to.json` — a custom profile loaded from the kubelet's `seccomp-profile-root` directory.
  - `Unconfined` — disabled.
- `sysctls` — Pod-private sysctls. The kubelet writes them in the Pod's namespaces. Safe sysctls are unrestricted; unsafe sysctls (`net.*` mostly) require kubelet flag `--allowed-unsafe-sysctls`.
- `capabilities` — drop default caps, add specific ones. Best practice: `drop: ["ALL"]` and add only what you need. `NET_BIND_SERVICE` lets a non-root user bind to ports <1024 (so you can `runAsNonRoot: true` and still listen on 80).
- `readOnlyRootFilesystem: true` — the container's rootfs is read-only. Writes go to mounted volumes only. Massive hardening — denies in-place tampering.
- `allowPrivilegeEscalation: false` — sets `no_new_privs` flag on the container's processes. Prevents `setuid` from elevating privileges. Should be `false` by default; some legacy software needs it `true`.
- `privileged: true` — disables almost all security. The container can do everything the host can. Required for CSI node plugins, network plugins, some monitoring agents. Avoid for application Pods.
- `procMount` — controls how `/proc` is mounted. `Default` masks sensitive paths; `Unmasked` exposes them (only for sandbox runtimes that re-enforce themselves).

### 17.4 The Pod Security Standards

[Ch 28](28-runtime-security-and-policy.md) covers Pod Security Admission, which enforces three standard profiles (Privileged, Baseline, Restricted) on Pods. Setting `securityContext` correctly is what gets you into the Restricted tier — `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, capabilities dropped, seccomp RuntimeDefault, no privileged, no hostNetwork/hostPID/hostIPC, no hostPath volumes.

---

## 18. Volumes Visible at the Pod Level

[Ch 19](19-storage-csi-pv-pvc.md) covers CSI deeply. This section is about the volumes you'd declare *inline* in a Pod, the ones that don't require an external storage driver.

### 18.1 The Pod-local volumes

- **`emptyDir`** — a directory backed by the node's filesystem (default) or `tmpfs` (`emptyDir.medium: Memory`). Created when the Pod is scheduled, destroyed when the Pod is deleted. Survives container restart, not Pod restart. `sizeLimit` is optional and is *not* enforced by quota — eviction signals act on it.

```yaml
volumes:
- name: scratch
  emptyDir:
    medium: Memory     # tmpfs, ram-backed
    sizeLimit: 1Gi
```

- **`configMap`** — projects a ConfigMap as a directory of files. Updates to the source ConfigMap are propagated to the mounted directory *via atomic symlink swap* (the kubelet writes the new value into a hidden directory, then re-points a symlink). Periodic, not instantaneous — the sync period is `kubelet --configmap-and-secret-change-detection-period` (default 60s). For env-var-based references, no propagation: the values are baked in at container start.

```yaml
volumes:
- name: app-config
  configMap:
    name: app-config
    items:
    - key: settings.yaml
      path: settings.yaml
    defaultMode: 0644
```

- **`secret`** — same shape as `configMap`, but for Secrets. Backed by tmpfs (memory) on the node, so secrets do not hit disk (unless swap is enabled, which it usually isn't in K8s nodes).

- **`downwardAPI`** — projects Pod metadata as files (the file-mount equivalent of `valueFrom.fieldRef` env vars).

```yaml
volumes:
- name: podinfo
  downwardAPI:
    items:
    - path: labels
      fieldRef:
        fieldPath: metadata.labels
    - path: cpu_limit
      resourceFieldRef:
        containerName: app
        resource: limits.cpu
```

- **`projected`** — combines configMap, secret, downwardAPI, and serviceAccountToken sources into a single volume tree. This is the volume type used by the projected ServiceAccount token feature:

```yaml
volumes:
- name: token
  projected:
    sources:
    - serviceAccountToken:
        audience: vault
        expirationSeconds: 600
        path: token
    - configMap:
        name: trust-bundle
        items:
        - key: ca.crt
          path: ca.crt
```

The serviceAccountToken source produces a **bound** token (bound to the Pod's UID, audience-scoped, time-limited) that the kubelet auto-rotates. This is the *modern* way to authenticate Pods to external services like Vault, AWS (IRSA), GCP.

- **`hostPath`** — mounts a host directory into the Pod. Almost always wrong; massive security risk. Allowed for system DaemonSets that need it (e.g., a node-exporter wants `/proc`).

- **`emptyDir.medium: HugePages-2Mi`** etc. — backed by huge pages (requires kernel + cgroup support).

- Persistent volumes (`persistentVolumeClaim`) and CSI ephemeral volumes — [ch 19](19-storage-csi-pv-pvc.md).

### 18.2 Mount semantics — `mountPath` is `mkdir -p`

If `mountPath` doesn't exist in the container's filesystem, the kubelet creates it before mount. If it does exist, **whatever was in the image at that path is hidden** — the volume mount shadows it. This is sometimes surprising: mounting an empty `configMap` over `/etc/nginx` makes the image's nginx config invisible.

### 18.3 SubPath

```yaml
volumeMounts:
- name: shared
  mountPath: /etc/app/conf.d
  subPath: instance-1/conf.d
```

`subPath` makes the mount come from a sub-directory of the volume. A common use: one PVC with multiple Pods each mounting their own `subPath` directory.

**Gotcha**: with `subPath`, ConfigMap and Secret updates do NOT propagate (atomic symlink swap doesn't work through subPath). Use `subPathExpr` with downward API only if you understand this.

---

## 19. Resources, QoS, and In-Place Updates

[Ch 21](21-resource-management-and-qos.md) is the depth chapter; here we cover what the Pod object exposes.

### 19.1 QoS class derivation

The kubelet writes `status.qosClass` based on the Pod's resources:

```
For each container in the Pod (init + main + native sidecar):
  has_req_cpu    = container has resources.requests.cpu set
  has_req_mem    = container has resources.requests.memory set
  has_lim_cpu    = container has resources.limits.cpu set
  has_lim_mem    = container has resources.limits.memory set

If for EVERY container: has_lim_cpu && has_lim_mem && requests==limits for both:
    qosClass = Guaranteed
Elif at least one container has any requests OR limits set:
    qosClass = Burstable
Else (no requests, no limits anywhere):
    qosClass = BestEffort
```

QoS determines:

- **OOM killer priority**: BestEffort first, then Burstable (by overage from request), Guaranteed last.
- **Eviction order** under node pressure: BestEffort first, Burstable next (sorted by overage), Guaranteed last.
- **CPU manager static policy** (when enabled): only Guaranteed pods with integer CPU requests get exclusive cores.

### 19.2 Requests vs limits

```yaml
resources:
  requests:
    cpu: "500m"        # 0.5 cores; what the scheduler reserves
    memory: "256Mi"    # what the scheduler reserves
  limits:
    cpu: "1"           # 1 core max (CFS quota); throttles, doesn't kill
    memory: "512Mi"    # OOM-kills the container if exceeded
```

- `requests` → scheduler arithmetic + cgroup soft constraints + QoS derivation.
- `limits` → hard cgroup caps. For CPU: CFS quotas (throttling). For memory: OOM-kill on exceed.

### 19.3 What "1 CPU" means

A "CPU" is one **logical core** (HyperThreaded thread, in most clouds). `500m` = 500 milli-CPU = half a logical core. CFS quotas are time-based: with `limits.cpu: 1`, the cgroup gets 100ms of CPU time per 100ms of wall time. On a 32-core machine, the same cgroup with `limits.cpu: 4` gets 400ms per 100ms, but the wall clock is only 100ms, so the container can use 4 cores' worth of parallelism.

### 19.4 Memory limits and OOMKilled

When a container's RSS exceeds `limits.memory`, the kernel OOM killer fires on **that cgroup's** processes. Exit code 137 (= 128 + SIGKILL=9). `lastState.terminated.reason: OOMKilled`. The Pod's `restartPolicy` then decides whether to restart.

If the *node* runs out of memory (sum of cgroups > node memory), the eviction manager kicks in and kills Pods in QoS order — *before* the kernel OOM killer would. This is why setting requests/limits matters: it makes eviction deterministic.

### 19.5 In-place resource updates (1.27+ beta, GA in 1.32)

```yaml
spec:
  containers:
  - name: app
    resources:
      requests:
        cpu: "500m"
        memory: "256Mi"
      limits:
        cpu: "1"
        memory: "512Mi"
    resizePolicy:
    - resourceName: cpu
      restartPolicy: NotRequired       # change live, no restart
    - resourceName: memory
      restartPolicy: RestartContainer  # restart the container on memory change
```

Submit a PATCH to the `pods/resize` subresource changing `containers[*].resources`. The kubelet:

1. Reads the new spec.
2. Decides for each changed resource whether a restart is needed (per `resizePolicy`).
3. For CPU: writes new `cpu.max` to the container's cgroup live. No restart.
4. For memory shrink: usually needs RestartContainer (you can't shrink below current RSS without risking OOM).
5. Updates `status.containerStatuses[].allocatedResources` and `status.containerStatuses[].resources` (the latter is what's actually applied to the cgroup right now).
6. Sets `status.resize: InProgress | Infeasible | Deferred | ""`.

The scheduler sees the new requests for future scheduling decisions (e.g., if other Pods come and go). VPA and Karpenter integrate with the resize subresource so that vertical scaling no longer requires Pod recreation.

### 19.6 `status.resize` and `status.containerStatuses[].allocatedResources`

- `status.containerStatuses[].resources` — what the cgroup currently has.
- `status.containerStatuses[].allocatedResources` — what the kubelet has committed to apply (may lag while pending).
- `status.resize` — overall resize state.

---

## 20. Restart Policy and Job Semantics

`spec.restartPolicy` is one of:

| Value | Meaning |
|---|---|
| `Always` | Restart every container on any exit (success or failure). Default. |
| `OnFailure` | Restart only on non-zero exit. Don't restart on exit 0. |
| `Never` | Never restart. |

### 20.1 Where each is allowed

- `Always` — Deployments, StatefulSets, DaemonSets, plain Pods. **Required** for the deployment-shaped workloads (they're long-running services).
- `OnFailure` — Jobs, CronJobs. Allows the Job to handle transient failures by restart, but treats exit 0 as final.
- `Never` — Jobs, CronJobs. The Job controller will create a new Pod (with new UID, new IP) instead of restarting. Useful when failures should be diagnosable without container restart loops.

### 20.2 Uniform across containers

`restartPolicy` applies to **every container in the Pod uniformly**. You cannot have one container with `Always` and another with `Never`. The only exception is native sidecars (§7), whose per-container `restartPolicy: Always` modifies behavior within the init phase and afterwards.

### 20.3 Job batch semantics

A Job's Pod is `Succeeded` (terminal, Job marks completion) iff:
- `restartPolicy` is OnFailure or Never (not Always), **and**
- All containers exited 0.

This is why workload controllers like Deployment use Pods with `restartPolicy: Always`: such Pods never enter `Succeeded`, so the Deployment can keep them running forever and rely on the *Deployment's* logic (not the Pod's) for replacement.

### 20.4 Quick example: a Job

```yaml
apiVersion: batch/v1
kind: Job
spec:
  template:
    spec:
      restartPolicy: OnFailure
      containers:
      - name: migrate
        image: my-app:1
        command: ["/app", "migrate"]
  backoffLimit: 4         # how many retries before Job is Failed
  activeDeadlineSeconds: 600
```

If `migrate` exits 1, the kubelet restarts the container in place (counted by `restartCount`). When `restartCount` (per the Job controller's accounting) hits `backoffLimit + 1`, the Job is marked `Failed` and stops creating new Pod attempts.

---

## 21. `status.containerStatuses`: The Runtime Truth

This is where you read what's *actually* happening, not what's declared.

```yaml
status:
  containerStatuses:
  - name: app
    image: registry.example.com/app@sha256:abcdef...
    imageID: docker-pullable://registry.example.com/app@sha256:abcdef...
    containerID: containerd://1a2b3c4d5e6f...
    ready: true
    started: true
    restartCount: 3
    state:
      running:
        startedAt: "2026-05-23T11:59:00Z"
    lastState:
      terminated:
        exitCode: 137
        signal: 9
        reason: OOMKilled
        message: ""
        startedAt: "2026-05-23T11:58:00Z"
        finishedAt: "2026-05-23T11:58:30Z"
        containerID: containerd://abc123...
    allocatedResources:
      cpu: "500m"
      memory: "256Mi"
    resources:
      requests:
        cpu: "500m"
        memory: "256Mi"
      limits:
        cpu: "1"
        memory: "512Mi"
    volumeMounts:
    - name: data
      mountPath: /var/lib/data
      readOnly: false
    user:
      linux:
        uid: 1000
        gid: 3000
        supplementalGroups: [4000]
```

### 21.1 What each field tells you

- **`name`** — must match the Pod spec's container name.
- **`image`** + **`imageID`** — what's running. `image` is the tag-style ref; `imageID` is the canonical digest, telling you *exactly* which build is on disk. If `image` is `:latest` but `imageID` differs across Pods, you have a drift problem.
- **`containerID`** — the runtime's identifier (`containerd://` or `cri-o://` prefix). Use with `crictl inspect <id>` for low-level state.
- **`ready`** — passing readiness probe (or no probe + Running). Read by the Endpoints controller.
- **`started`** — startupProbe has passed (or no startupProbe + Running). When false, liveness/readiness are not yet running.
- **`restartCount`** — kubelet-observed restarts. Survives kubelet restart (written into etcd).
- **`state`** — current state (one of `waiting`, `running`, `terminated`).
- **`lastState`** — previous state. **The most useful diagnostic field.** Tells you what made the container restart.
- **`allocatedResources`** — what the kubelet has committed (resize machinery, see §19.5).
- **`resources`** — what the cgroup actually has right now.
- **`volumeMounts`** — the projection at runtime (post-subPath resolution).
- **`user`** (linux) — the resolved uid/gid + supplemental groups the runtime applied. Useful when `runAsUser` was implied by the image.

### 21.2 `status.initContainerStatuses` and `status.ephemeralContainerStatuses`

Same shape, separate lists. For init containers, `restartCount` is meaningful only while initialization is ongoing.

---

## 22. Sidecar Patterns: Pre-1.28 vs Post-1.28

Putting §7 in context with the legacy world.

### 22.1 The legacy pattern (still seen widely)

```yaml
apiVersion: v1
kind: Pod
spec:
  terminationGracePeriodSeconds: 60
  containers:
  - name: app
    image: my-app:1
    ports:
    - containerPort: 8080
    lifecycle:
      preStop:
        exec:
          command:
          - sh
          - -c
          - |
            # Wait for endpoints to propagate
            sleep 5
            # Tell app to drain
            curl -X POST localhost:8080/shutdown
            # Wait for sidecar (Envoy) to drain on its own timer
            sleep 20
  - name: envoy
    image: envoy:1.30
    ports:
    - containerPort: 15000
    lifecycle:
      preStop:
        exec:
          command:
          - sh
          - -c
          - |
            # Block until /quitquitquit is callable
            # Then wait until no upstream connections
            until [ $(curl -s localhost:15000/stats | grep -c upstream_cx_active=0) -gt 0 ]; do
              sleep 1
            done
            curl -X POST localhost:15000/quitquitquit
```

Problems:

- App races against Envoy at startup (no ordering).
- Both get SIGTERM at the same time; the orchestration of "envoy stays alive until app drains" is done in shell, brittle.
- A Job with this Envoy sidecar never completes (Envoy keeps running).

### 22.2 The native sidecar pattern (1.28+, GA 1.29)

```yaml
apiVersion: v1
kind: Pod
spec:
  terminationGracePeriodSeconds: 60
  initContainers:
  - name: envoy
    image: envoy:1.30
    restartPolicy: Always         # <-- native sidecar
    ports:
    - containerPort: 15000
    startupProbe:
      httpGet:
        path: /ready
        port: 15000
      failureThreshold: 30
      periodSeconds: 1
    lifecycle:
      preStop:
        exec:
          command: ["curl", "-X", "POST", "localhost:15000/quitquitquit"]
  containers:
  - name: app
    image: my-app:1
    ports:
    - containerPort: 8080
    lifecycle:
      preStop:
        sleep:
          seconds: 5             # endpoints propagation
```

The kubelet enforces:
- Envoy's startup probe must pass before app starts.
- On Pod delete, app's preStop runs, app gets SIGTERM. Envoy keeps running.
- Only after app exits does Envoy receive its preStop + SIGTERM.

You can throw away the shell scripts. The orchestration is in the API.

### 22.3 Why both still exist

Native sidecars require kubelet 1.28+. Many clusters are still on older versions, or use cloud-managed K8s with delayed rollouts. Many service meshes (Istio, Linkerd) have both code paths and pick based on cluster version. Operators writing for the broadest compatibility still ship the legacy pattern; greenfield deployments should use native sidecars.

---

## 23. RuntimeClass: Picking Your Sandbox

`spec.runtimeClassName` selects which OCI runtime handles the Pod:

```yaml
spec:
  runtimeClassName: gvisor       # or "kata", "runc" (default), custom
```

RuntimeClass is a cluster-scoped object:

```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
handler: runsc       # CRI handler name (containerd config)
overhead:
  podFixed:
    cpu: "250m"
    memory: "120Mi"
scheduling:
  nodeSelector:
    runtime: gvisor
```

The `handler` field maps to a containerd or CRI-O runtime configuration (e.g., `runsc` for gVisor). `overhead` is added to the Pod's effective requests by the PodOverhead admission plugin (and consumed by the scheduler). `scheduling` adds node selection requirements (only nodes that support this runtime).

[Ch 29](29-pod-sandboxing.md) covers gVisor, Kata, and Confidential Containers in depth — for now, know that `runtimeClassName` is the dial.

---

## 24. Common Pod Failure Causes

The fast lookup table.

| Symptom | First place to look |
|---|---|
| **`ImagePullBackOff`** | `kubectl describe pod` — events. Causes: typo in image name, wrong registry, missing `imagePullSecret`, image arch mismatch (arm64 image on amd64 node), registry rate-limit. |
| **`ErrImagePull`** | Same as above, one-shot (will retry into `ImagePullBackOff` if it keeps failing). |
| **`CreateContainerConfigError`** | A ConfigMap or Secret referenced in `env`/`envFrom`/`volumes` doesn't exist or doesn't have the requested key. |
| **`CreateContainerError`** | The runtime refused to create the container. Often: invalid `securityContext`, no such user/group, bad sysctl, RuntimeClass not available on this node. |
| **`CrashLoopBackOff`** | App crashes within seconds. Look at `lastState.terminated.exitCode` and `kubectl logs --previous`. |
| **`OOMKilled`** (in `lastState`) | Memory limit too low, or memory leak. Raise `limits.memory` or profile. |
| **`RunContainerError`** | Runtime started the container but it died immediately. Often: missing binary, wrong entrypoint, exec format error (wrong arch). |
| **`Pending` for > 1 min** | Scheduler can't find a node. `kubectl describe pod` — events from `default-scheduler` like "no nodes available that match all of the predicates". |
| **`Init:Error`** / **`Init:CrashLoopBackOff`** | An init container is failing. Same logs/exitCode investigation, just with `-c <initContainerName>`. |
| **`PodInitializing`** | Init containers are running normally; just wait. |
| **`Terminating` for > 1 min** | `preStop` is hanging, `terminationGracePeriodSeconds` not yet elapsed, or a finalizer is stuck. `kubectl get pod -o yaml | grep -A 5 finalizers`. |
| **`ContainerStatusUnknown`** | Kubelet lost the runtime state. Usually a kubelet or containerd crash. Check `kubectl describe node` and `journalctl -u kubelet`. |
| **Pod scheduled but no IP** | CNI is failing. Look at `kubectl describe pod` for CNI ADD errors; check CNI plugin DaemonSet logs. |
| **Pod has IP but no DNS** | CoreDNS down or `dnsPolicy` misconfigured. `kubectl exec pod -- cat /etc/resolv.conf` and `nslookup kubernetes.default`. |
| **Liveness causes restart loop** | Probe is too aggressive or tests something downstream. Reduce sensitivity or change to `tcpSocket`. |
| **Readiness never ready** | App doesn't bind on the configured port, or probe path is wrong, or app waits on a service that's not up. |

### 24.1 The diagnostic shell sequence

```
kubectl get pods                              # is it Running? Ready? CrashLoop?
kubectl describe pod <name>                   # events + last 16 events
kubectl get pod <name> -o yaml | less         # full status
kubectl logs <name> -c <ctr>                  # current logs
kubectl logs <name> -c <ctr> --previous       # logs from previous container instance (KEY for crashloops)
kubectl debug <name> --image=busybox -it      # ephemeral container if you need to poke around
crictl ps --pod $(kubectl get pod <name> -o jsonpath='{.metadata.uid}')   # runtime-level view (on the node)
journalctl -u kubelet | grep <pod_name>       # kubelet-side errors
```

---

## 25. Pitfalls

The list of "I just learned this the hard way" facts.

1. **Container startup order in `spec.containers` is not guaranteed.** Containers in `spec.containers` start in parallel. If you need ordering, use init containers or native sidecars.

2. **Writing to `/etc/hosts` doesn't persist.** It's a tmpfs file the kubelet re-renders on each container restart. Use `spec.hostAliases` for permanent entries.

3. **`restartPolicy: Always` on a Job is rejected by validation.** A Job pod must use OnFailure or Never. The admission plugin refuses Always for Job-owned pods.

4. **`preStop` longer than `terminationGracePeriodSeconds` = SIGKILL mid-hook.** Always: `terminationGracePeriodSeconds ≥ preStop_duration + expected_shutdown_time`.

5. **Readiness probe depending on a downstream service** that depends on this Pod creates a chicken-and-egg deadlock. Readiness should reflect the Pod's *own* health, not the world's.

6. **Two containers sharing an `emptyDir` and expecting "one writes, other tails" to be atomic.** POSIX file semantics only — `tail -f` will see partial writes if writes aren't aligned. Use a pipe (mkfifo) or a real IPC mechanism.

7. **`hostPort` consumes a node port.** Exactly one Pod with `hostPort: 80` can schedule per node. If two Pods both want `hostPort: 80`, the second stays `Pending`.

8. **`image: foo:latest` + `imagePullPolicy: IfNotPresent` = cached forever.** Once any version of `foo:latest` is on the node, it never re-pulls. Either use specific tags + digests, or `imagePullPolicy: Always`.

9. **`hostPID: true` exposes every process on the node** to anything in the Pod. `ps aux` from inside the container shows other Pods' processes, the kubelet, kube-proxy, etc. Same for `/proc/<host_pid>/`. Use very sparingly.

10. **PID 1 in a shell wrapper does not forward signals.** `sh -c "/usr/bin/myapp"` makes `sh` PID 1, and `sh` doesn't forward SIGTERM. Use `exec /usr/bin/myapp` or a real init like `tini`.

11. **`shareProcessNamespace: true` + `kubectl logs` shows only the targeted container.** Logs are still per-container (the kubelet tails each container's stdout separately). Process namespace sharing affects ps, not log multiplexing.

12. **`fsGroup` recursively chowns the volume on every Pod startup.** With `fsGroupChangePolicy: Always` (default) and a large PVC, this can take minutes. Set `OnRootMismatch` for big volumes.

13. **`configMap` updates don't reach `env` vars.** Only file-mounted ConfigMaps get the rolling update; env-injected values are baked in at container start.

14. **`configMap` updates take up to 60s to reach mounted files** (the kubelet's sync period). Apps that need instant config reload need a SIGHUP from a sidecar that watches the source ConfigMap directly via the API.

15. **`emptyDir` survives container restart but not Pod restart.** A Pod's whole `emptyDir` is wiped when the Pod is deleted — even if you `kubectl delete pod`-and-recreate with the same name (it's a new Pod with a new UID).

16. **`shareProcessNamespace` reveals other containers' command-line and environment.** A `ps -ef` from the sidecar shows the app's full command line, including any secrets passed as args. Use env or files, not args, for sensitive values.

17. **Native sidecars count toward `requests` (sum), not `max`.** A native sidecar with `requests.memory: 1Gi` permanently reduces schedulable capacity by 1Gi, even though it's in `initContainers`.

18. **`terminationGracePeriodSeconds: 0` is forbidden** (validation rejects it). The minimum legal value is 1. Setting it via `kubectl delete --grace-period=0 --force` bypasses kubelet cleanup entirely and is the only way to get exact-zero behavior; usually unsafe.

19. **Probes against `localhost` may not work inside an Istio sidecar mesh** unless `holdApplicationUntilProxyStarts: true` is set — the istio-init container's iptables rules redirect outgoing traffic to Envoy, and probes go through this redirect. Native sidecars resolve this by ordering.

20. **`spec.serviceAccountName` cannot be changed on a running Pod.** The kubelet sets up the SA token volume at startup; mutating the SA would invalidate the mounted token. Change requires Pod recreate.

21. **`automountServiceAccountToken: false` doesn't strip an already-mounted token if you forgot it.** It must be set at creation time. Existing Pods don't lose their tokens.

22. **`spec.activeDeadlineSeconds` counts from Running, not from creation.** A Pod stuck in `Pending` for 10 minutes does *not* count against its deadline.

23. **`PreStop` sleep using `exec: ["/bin/sleep", "5"]` fails in distroless images** (no `sleep` binary). Use `sleep: { seconds: 5 }` on 1.29+, or include `sleep` in your image.

24. **Two Pods with the same `hostname`/`subdomain` create ambiguous DNS records.** Headless Service DNS expects unique `hostname` per Pod. Two replicas of a StatefulSet correctly differ in their auto-assigned hostnames; manual override breaks this.

25. **`livenessProbe` failure during `terminationGracePeriodSeconds` causes early SIGKILL.** Once the Pod is terminating, the kubelet still runs probes briefly. A liveness failure during shutdown can short-circuit grace. Set `livenessProbe.terminationGracePeriodSeconds` to override.

26. **`runAsNonRoot: true` doesn't block a `USER 0` image** — it fails at runtime, not admission. The container goes into `CreateContainerError` rather than refusing to schedule. Combine with PSA Restricted profile to catch at admission time.

---

## 26. TL;DR

A **Pod** is one or more containers sharing namespaces, anchored by a tiny **pause** container that owns the network/IPC/UTS namespaces so the workload containers can come and go without losing the podIP. By default, NET + IPC + UTS + CGROUP + TIME are shared; PID and MNT are NOT (each container has its own); USER is not used unless the cluster opts in. `shareProcessNamespace: true` collapses PID; `hostNetwork/hostPID/hostIPC` break out to the host's namespace instead of having a Pod-owned one.

**Init containers** run to completion in order, then **native sidecars** (initContainers with `restartPolicy: Always`, KEP-753, GA in 1.29) start alongside main containers and shut down only after them. **Ephemeral containers** can be added at runtime via the `ephemeralcontainers` subresource for debugging, sharing the targeted container's PID namespace.

The Pod phase machine is `Pending → Running → Succeeded | Failed`, derived from per-container states (`Waiting → Running → Terminated`). Phase alone is misleading — alert on conditions (`Ready`, `ContainersReady`) and on `restartCount` + `lastState.terminated`. **CrashLoopBackOff** is a *kubelet-local* exponential backoff (10s, 20s, 40s, 80s, 160s, 300s cap), not a controller-driven state.

Probes: **startupProbe** gates the others until success (single-shot, never repeats once passing); **readinessProbe** controls Endpoints inclusion (no restart on failure); **livenessProbe** restarts the container on failure. Mis-tuned liveness causes cascading restart loops; readiness should reflect the Pod's own health.

**Lifecycle hooks**: `postStart` runs concurrently with the container's entrypoint, blocks Ready until it finishes; `preStop` runs synchronously before SIGTERM and counts against `terminationGracePeriodSeconds`. The termination sequence is: deletionTimestamp → Endpoints removal → preStop → SIGTERM → grace countdown → SIGKILL. The Endpoints propagation race is solved by a `preStop` sleep (5–10s) before the app starts draining.

**podIP** is allocated by the CNI on RunPodSandbox and never changes for the Pod's life; **dnsPolicy: ClusterFirst** with `ndots: 5` is the default and is also the source of many DNS performance problems. **SecurityContext** has Pod- and container-level layers; the Restricted PSA profile is `runAsNonRoot`, `allowPrivilegeEscalation: false`, all capabilities dropped, seccomp RuntimeDefault.

**QoS** (Guaranteed / Burstable / BestEffort) drives OOM-kill order and eviction order, derived from whether *every* container has `requests == limits` for cpu + memory. **In-place resize** (GA in 1.32) lets you change cpu/memory on a running Pod via the `resize` subresource without recreation.

**`restartPolicy: Always`** is the only legal value for Deployment/StatefulSet/DaemonSet pods; **OnFailure / Never** are required for Jobs. Restart policy is uniform across containers (except for the native-sidecar exception).

`status.containerStatuses[]` is the runtime truth — read `lastState.terminated.reason` and `.exitCode` first when anything goes wrong. The diagnostic sequence is `kubectl describe pod` → `kubectl logs --previous` → `crictl` on the node → kubelet journal.

The Pod is small but full of subtlety. Internalize the namespace table (§3), the termination sequence (§14), the three probe roles (§12), the QoS derivation (§19), and the native-sidecar lifecycle (§7) — these five pieces are 80% of every Pod-related production fire.

Forward references: [ch 10 (kubelet)](10-kubelet-internals.md) for who runs all this, [ch 12 (workload controllers)](12-workload-controllers.md) for what creates Pods, [ch 14 (services)](14-services-and-kube-proxy.md) for how Ready Pods get traffic, [ch 15/16 (networking)](15-cni-and-pod-networking.md) for the podIP plumbing, [ch 19 (storage)](19-storage-csi-pv-pvc.md) for volumes beyond the inline types, [ch 21 (resources/QoS)](21-resource-management-and-qos.md) for the cgroup details, [ch 29 (sandboxing)](29-pod-sandboxing.md) for RuntimeClass-driven isolation. Everything we wrote here is built on [ch 00 (Linux primitives)](00-linux-primitives-for-containers.md) and [ch 01 (CRI/OCI)](01-container-runtimes-cri-oci.md) — re-read those if any of this chapter felt like magic.
