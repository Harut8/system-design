# Container Runtimes: CRI, OCI, runc, containerd, and CRI-O

What "running a container" actually means at the layer Kubernetes calls. This chapter peels apart the three-layer separation between the **OCI runtime** (the thing that calls `clone3` and `pivot_root`), the **container runtime / CRI implementation** (containerd, CRI-O — the thing that manages images, snapshots, and a shim per container), and the **orchestrator** (kubelet). It is the most under-taught layer of the Kubernetes stack: most engineers learn `kubectl run` and never see the three gRPC + spec boundaries underneath, until something jams and they need `crictl`, `ctr`, and `runc` to dig out. This chapter is the vocabulary for every later chapter that says "the runtime starts a container".

If chapter 00 explained *namespaces and cgroups as kernel primitives*, this chapter explains *how Kubernetes wraps those primitives in three concentric layers of standards and daemons*, why each layer exists, and what the contract between them looks like wire-format-by-wire-format.

---

## Table of Contents

1. [The Three-Layer Separation: Why It Exists](#1-the-three-layer-separation-why-it-exists)
2. [The OCI Runtime Specification](#2-the-oci-runtime-specification)
3. [runc Internals: From config.json to a Running Process](#3-runc-internals-from-configjson-to-a-running-process)
4. [Alternative OCI Runtimes: crun, youki, Kata, gVisor](#4-alternative-oci-runtimes-crun-youki-kata-gvisor)
5. [The OCI Image Specification (Brief)](#5-the-oci-image-specification-brief)
6. [containerd Architecture](#6-containerd-architecture)
7. [CRI-O Architecture](#7-cri-o-architecture)
8. [The CRI gRPC Contract](#8-the-cri-grpc-contract)
9. [The Pause Container](#9-the-pause-container)
10. [Lifecycle Walkthrough: A Pod From Kubelet's Perspective](#10-lifecycle-walkthrough-a-pod-from-kubelets-perspective)
11. [Image Garbage Collection and Disk Pressure](#11-image-garbage-collection-and-disk-pressure)
12. [Performance and Debugging: ctr, crictl, runc, nsenter](#12-performance-and-debugging-ctr-crictl-runc-nsenter)
13. [Pitfalls and Common Failures](#13-pitfalls-and-common-failures)
14. [TL;DR](#14-tldr)

---

## 1. The Three-Layer Separation: Why It Exists

A common misreading of Kubernetes is that kubelet talks to "Docker" (or used to, anyway), and Docker runs containers. The real picture is three layers, each with its own specification or interface, each replaceable independently. Understanding the boundaries is the prerequisite for everything that follows.

### 1.1 The Stack, Top to Bottom

```
┌────────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR                                                   │
│  kubelet (one per node)                                         │
│    - Watches apiserver for Pods bound to this node              │
│    - Drives pod lifecycle via the CRI gRPC API                  │
│    - Owns volume mount, probe execution, status reporting       │
└──────────────────────────────┬─────────────────────────────────┘
                               │
                               │  CRI (Container Runtime Interface)
                               │  gRPC over Unix socket
                               │  /var/run/containerd/containerd.sock
                               │  or /var/run/crio/crio.sock
                               ▼
┌────────────────────────────────────────────────────────────────┐
│  CONTAINER RUNTIME / CRI IMPLEMENTATION                         │
│  containerd  |  CRI-O                                           │
│    - Implements RuntimeService + ImageService gRPC              │
│    - Manages images: pull, unpack, store (content + snapshot)   │
│    - Spawns and owns a shim process per pod sandbox / container │
│    - Translates CRI calls into OCI runtime invocations          │
└──────────────────────────────┬─────────────────────────────────┘
                               │
                               │  OCI Runtime Spec
                               │  config.json + rootfs directory
                               │  command-line: runc create / start / kill / delete
                               ▼
┌────────────────────────────────────────────────────────────────┐
│  OCI RUNTIME (low-level)                                        │
│  runc  |  crun  |  youki  |  kata-runtime  |  runsc (gVisor)    │
│    - Reads config.json                                          │
│    - Creates namespaces (CLONE_NEW*)                            │
│    - Sets up cgroups, capabilities, seccomp, AppArmor/SELinux   │
│    - Pivots root, drops privileges, execs the entrypoint        │
└──────────────────────────────┬─────────────────────────────────┘
                               │
                               │  syscalls
                               ▼
┌────────────────────────────────────────────────────────────────┐
│  LINUX KERNEL                                                   │
│    namespaces · cgroups v2 · capabilities · seccomp-bpf         │
│    overlayfs · veth · netfilter · LSM (AppArmor / SELinux)      │
│    (covered in ch 00)                                            │
└────────────────────────────────────────────────────────────────┘
```

There are **three contracts** here, each formally specified:

| Boundary | Spec | Wire format | Who owns it |
|---|---|---|---|
| kubelet ↔ container runtime | [Kubernetes CRI](https://github.com/kubernetes/cri-api) | gRPC (protobuf) over UDS | Kubernetes |
| container runtime ↔ OCI runtime | [OCI Runtime Spec](https://github.com/opencontainers/runtime-spec) | `config.json` on disk + CLI | Open Container Initiative |
| OCI runtime ↔ kernel | Linux ABI (syscalls) | n/a | Kernel |

### 1.2 Why Three Layers Instead of One

A reasonable first reaction: "isn't this overengineered? Why not let kubelet call `clone3` directly?" Each split exists for a reason that became visible only under pressure.

**The orchestrator / runtime split (kubelet ↔ CRI).** Before CRI (Kubernetes 1.5, late 2016), kubelet had hard-coded code paths for Docker and rkt. Adding a runtime meant patching kubelet. CRI turned that into a stable gRPC interface so containerd, CRI-O, and Kata could plug in without core changes. It also let "Docker" (dockershim) eventually be removed in 1.24 without ripping out kubelet (more in §13).

**The runtime / OCI split (CRI implementation ↔ runc).** Image management and container execution are different problems. Image management is "talk to registries, deduplicate layers, manage snapshots, garbage-collect content" — a stateful daemon problem with a lot of policy. Container execution is "given config.json + rootfs, set up namespaces and exec" — a stateless one-shot operation. Splitting them means:
- The image-management daemon (containerd) can crash and be restarted without killing running containers (because the shim — not containerd — is the parent of the workload).
- The execution layer is interchangeable: same containerd, swap runc for runsc (gVisor) or kata-runtime for stronger isolation, via Kubernetes' **RuntimeClass**.
- Reuse: containerd is used by Docker Engine, Kubernetes (via CRI plugin), and standalone CI systems. runc is used by containerd, CRI-O, Podman, Docker, and others. Each layer has more than one consumer, which is what justifies the boundary economically.

**The OCI / kernel split.** This is obvious in retrospect: namespaces and cgroups are kernel primitives; the kernel ABI doesn't change for containers. But the OCI runtime spec normalizes them into a *declarative* `config.json` so that the same JSON, given to runc or crun or youki, produces the same container. Without the spec, every runtime would invent its own bag of flags.

### 1.3 What Each Layer Owns

The clean way to memorize the split:

| Concern | Owned by | Why |
|---|---|---|
| Where the pod runs (scheduling) | kube-scheduler | Cluster-wide concern |
| Pod-to-pod networking | CNI plugin (Calico, Cilium, …) | Cluster-wide concern; CRI just gives the pod sandbox a network namespace and asks CNI to wire it |
| Pod volume mount | kubelet volume manager → CSI driver | Cluster-wide concern; CRI receives already-mounted host paths |
| Image pull, layer storage | container runtime (containerd/CRI-O) | Per-node concern |
| Setting up namespaces, cgroups | OCI runtime (runc/…) | Per-container concern |
| Sending the entrypoint a SIGTERM | shim → OCI runtime | Per-container concern |
| Reaping the entrypoint process | shim (it's the parent — PID 1 of the container is the entrypoint, but the shim is its waiter) | Per-container concern |

When something breaks, this table tells you which component to grep. A pod stuck in `ContainerCreating`? Probably CNI (chapter 15) or CSI (chapter 19), not the runtime. A pod stuck in `CrashLoopBackOff` with no logs? OCI runtime layer — `runc create` failed and you need to look at containerd's logs and the shim's stderr.

---

## 2. The OCI Runtime Specification

The OCI Runtime Spec (`runtime-spec`) defines two things: the on-disk **configuration** that fully describes a container (`config.json`), and the **lifecycle** of operations a runtime must support. That's it. No registry interaction, no image format (that's the image spec, §5), no networking (that's CNI). It is deliberately minimal so that any OCI-compliant runtime — runc, crun, youki, runsc, kata-runtime — can consume the same input.

### 2.1 The Bundle

An OCI **bundle** is a filesystem directory containing exactly two things:

```
my-bundle/
├── config.json        # the runtime configuration
└── rootfs/            # the root filesystem the container will see as /
    ├── bin/
    ├── etc/
    ├── lib/
    ├── usr/
    └── ...
```

`rootfs` is what `pivot_root` will move into place as the new `/`. It contains a complete filesystem image — typically built by unpacking the layers of an OCI image (§5). `config.json` is a single JSON document describing everything else.

### 2.2 The config.json Schema

The full schema has ~150 fields. The important ones, with a real example:

```json
{
  "ociVersion": "1.0.2",
  "process": {
    "terminal": false,
    "user": { "uid": 0, "gid": 0, "additionalGids": [10, 100] },
    "args": ["/usr/bin/nginx", "-g", "daemon off;"],
    "env": [
      "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
      "HOSTNAME=nginx-pod",
      "NGINX_PORT=80"
    ],
    "cwd": "/",
    "capabilities": {
      "bounding":   ["CAP_NET_BIND_SERVICE", "CAP_CHOWN", "CAP_SETUID", "CAP_SETGID"],
      "effective":  ["CAP_NET_BIND_SERVICE", "CAP_CHOWN", "CAP_SETUID", "CAP_SETGID"],
      "permitted":  ["CAP_NET_BIND_SERVICE", "CAP_CHOWN", "CAP_SETUID", "CAP_SETGID"],
      "ambient":    [],
      "inheritable": []
    },
    "rlimits": [
      { "type": "RLIMIT_NOFILE", "hard": 1024, "soft": 1024 }
    ],
    "noNewPrivileges": true
  },
  "root": {
    "path": "rootfs",
    "readonly": false
  },
  "hostname": "nginx-pod",
  "mounts": [
    { "destination": "/proc",     "type": "proc",     "source": "proc" },
    { "destination": "/dev",      "type": "tmpfs",    "source": "tmpfs",
      "options": ["nosuid", "strictatime", "mode=755", "size=65536k"] },
    { "destination": "/dev/pts",  "type": "devpts",   "source": "devpts",
      "options": ["nosuid", "noexec", "newinstance", "ptmxmode=0666", "mode=0620", "gid=5"] },
    { "destination": "/sys",      "type": "sysfs",    "source": "sysfs",
      "options": ["nosuid", "noexec", "nodev", "ro"] },
    { "destination": "/sys/fs/cgroup", "type": "cgroup", "source": "cgroup",
      "options": ["nosuid", "noexec", "nodev", "relatime", "ro"] },
    { "destination": "/etc/hosts", "type": "bind", "source": "/var/lib/kubelet/pods/.../etc-hosts",
      "options": ["rbind", "rprivate"] },
    { "destination": "/var/run/secrets/kubernetes.io/serviceaccount",
      "type": "bind", "source": "/var/lib/kubelet/pods/.../volumes/kubernetes.io~projected/sa-token",
      "options": ["rbind", "rprivate", "ro"] }
  ],
  "linux": {
    "namespaces": [
      { "type": "pid" },
      { "type": "network", "path": "/var/run/netns/cni-abc123" },
      { "type": "ipc",     "path": "/var/run/ipcns/cni-abc123" },
      { "type": "uts",     "path": "/var/run/utsns/cni-abc123" },
      { "type": "mount" },
      { "type": "cgroup" }
    ],
    "uidMappings": [
      { "containerID": 0, "hostID": 100000, "size": 65536 }
    ],
    "gidMappings": [
      { "containerID": 0, "hostID": 100000, "size": 65536 }
    ],
    "resources": {
      "memory": { "limit": 536870912, "swap": 536870912 },
      "cpu":    { "shares": 1024, "quota": 100000, "period": 100000, "cpus": "0-3" },
      "pids":   { "limit": 1024 },
      "blockIO": { "weight": 500 },
      "hugepageLimits": [],
      "devices": [
        { "allow": false, "access": "rwm" },
        { "allow": true, "type": "c", "major": 1, "minor": 3, "access": "rwm" },
        { "allow": true, "type": "c", "major": 1, "minor": 5, "access": "rwm" }
      ]
    },
    "cgroupsPath": "kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod<uid>.slice/cri-containerd-<id>.scope",
    "seccomp": {
      "defaultAction": "SCMP_ACT_ERRNO",
      "architectures": ["SCMP_ARCH_X86_64", "SCMP_ARCH_X86", "SCMP_ARCH_X32"],
      "syscalls": [
        {
          "names": ["accept", "accept4", "access", "bind", "brk", "close",
                    "connect", "epoll_create1", "epoll_ctl", "epoll_wait",
                    "execve", "exit", "exit_group", "fstat", "futex",
                    "getpid", "mmap", "mprotect", "munmap", "open",
                    "openat", "read", "rt_sigaction", "rt_sigprocmask",
                    "stat", "write", "writev"],
          "action": "SCMP_ACT_ALLOW"
        }
      ]
    },
    "maskedPaths": [
      "/proc/acpi", "/proc/asound", "/proc/kcore", "/proc/keys",
      "/proc/latency_stats", "/proc/timer_list", "/proc/timer_stats",
      "/proc/sched_debug", "/sys/firmware"
    ],
    "readonlyPaths": [
      "/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"
    ],
    "rootfsPropagation": "private",
    "apparmorProfile": "cri-containerd.apparmor.d",
    "selinuxLabel": "system_u:system_r:container_t:s0:c123,c456"
  }
}
```

Most fields map one-to-one onto Linux kernel features (chapter 00). A subset that's worth singling out:

- `process.user.uid/gid` → the UID/GID the entrypoint runs as inside the container. With `linux.uidMappings`, that's a *container UID* that gets mapped to a *host UID*. UID 0 inside a user namespace is *not* root on the host.
- `process.capabilities.{bounding,effective,permitted,ambient,inheritable}` → the five Linux capability sets. `bounding` is the ceiling; `effective` is what's actually granted to the entrypoint. Kubernetes' `SecurityContext.capabilities.drop=["ALL"]` shrinks all of these.
- `process.noNewPrivileges` → sets the `PR_SET_NO_NEW_PRIVS` prctl, which disables setuid binaries from gaining privileges. Equivalent to `allowPrivilegeEscalation: false` in a Kubernetes SecurityContext.
- `linux.namespaces` → which namespaces to create (no `path`) or join (with `path`). A pod's "sibling" containers all join the same network/IPC/UTS namespace by pointing at the same `/var/run/netns/cni-XXX` paths.
- `linux.resources` → directly translated into cgroup-v2 controllers. `cpu.shares` → `cpu.weight`, `cpu.quota`+`period` → `cpu.max`, `memory.limit` → `memory.max`, etc.
- `linux.seccomp` → compiled into a BPF program at runtime startup. The schema is straight from `seccomp.h` with verbose names.
- `linux.maskedPaths` → bind-mounts `/dev/null` over each path so the container sees an empty file. `readonlyPaths` remounts as `ro`. Both protect against reading host kernel state.

### 2.3 The Runtime Lifecycle State Machine

The spec defines a minimal state machine that every OCI runtime must implement:

```
                  create
   (no state) ────────────►  creating
                                │
                                │ namespaces, cgroups, rootfs set up;
                                │ entrypoint NOT yet exec'd
                                ▼
                            created
                                │
                                │ start
                                ▼
                            running
                                │
                                │ entrypoint runs to completion,
                                │ or kill signal delivered, or runtime
                                │ observes process exit
                                ▼
                            stopped
                                │
                                │ delete
                                ▼
                           (no state)
```

Operations the spec requires:

| Operation | Effect | Key detail |
|---|---|---|
| `create <id>` | Move from no-state → created. Set up namespaces, cgroups, mount rootfs, but **do not exec** the entrypoint. | Returns once the container exists; entrypoint is parked, waiting. This split lets the orchestrator inspect or modify the container before it runs. |
| `start <id>` | Move from created → running. Signal the parked entrypoint to exec. | Returns immediately; container runs in background. |
| `state <id>` | Report current state (id, status, pid, bundle). | JSON to stdout. |
| `kill <id> <signal>` | Send signal to the container's PID 1. | Default `SIGTERM`. Used for graceful shutdown. |
| `delete <id>` | Move from stopped → no-state. Free cgroup, remove state files. | Will fail if container is still running, unless `--force`. |
| `exec` (optional but ubiquitous) | Run a new process inside an existing container's namespaces/cgroups. | Foundation of `kubectl exec`. |

The create/start split deserves a moment. Why two operations? Because the orchestrator needs a window — between "all kernel resources allocated" and "entrypoint running" — to do things that require the container to *exist* but not *execute*: attach to its stdio, configure additional networking, apply post-create hooks, copy files into the rootfs. With a single `run` operation, you could only hook before-create or after-start, never *between*. The split is how Kubernetes can, for example, run init containers in a deterministic order: each init container is created, started, waited-on-to-exit, deleted, before the next is created.

### 2.4 Runtime Hooks

The spec also defines lifecycle **hooks** — arbitrary commands the runtime runs at specific points:

```json
"hooks": {
  "createRuntime":   [{"path": "/usr/local/bin/cni-bridge-add", "args": [...], "timeout": 5}],
  "createContainer": [...],
  "startContainer":  [...],
  "poststart":       [{"path": "/usr/local/bin/audit-log"}],
  "poststop":        [{"path": "/usr/local/bin/cleanup"}]
}
```

In practice Kubernetes does not lean on these hooks for primary lifecycle (it has its own preStop/postStart, see chapter 11). The `createRuntime` hook *was* historically used to invoke CNI from runc itself, but the modern model is that the **container runtime** (containerd/CRI-O) calls CNI before invoking runc; runc only sees a network namespace path it should join.

---

## 3. runc Internals: From config.json to a Running Process

`runc` is the reference OCI runtime, written in Go, originally extracted from Docker's `libcontainer` in 2015. It is what every other piece of this chapter ultimately invokes. Understanding what happens inside `runc create` is the key to understanding why some bugs only show up "between the shim and the kernel".

### 3.1 The Source Tree (where to look)

The runc repo (`github.com/opencontainers/runc`) is small enough to read in a weekend. The interesting files:

```
runc/
├── create.go                     # CLI: runc create
├── start.go                      # CLI: runc start
├── run.go                        # CLI: runc run (create + start)
├── init.go                       # CLI: runc init (the "child" process — see below)
├── libcontainer/
│   ├── container_linux.go        # the Container type, state transitions
│   ├── process_linux.go          # process start, the parent-side of the dance
│   ├── init_linux.go             # the child-side: nsenter, pivot_root, exec
│   ├── standard_init_linux.go    # the standard init path (non-setns)
│   ├── setns_init_linux.go       # the setns init path (for exec)
│   ├── factory_linux.go          # creates Container objects
│   ├── nsenter/                  # C code that runs BEFORE Go runtime starts
│   │   ├── nsenter.go            #   (constructor: __attribute__((constructor)))
│   │   ├── nsexec.c              #   the heart of the "double clone" trick
│   │   └── namespace.h
│   ├── cgroups/                  # cgroup v1 and v2 backends
│   ├── seccomp/                  # seccomp filter compilation
│   └── apparmor/, selinux/, keys/, capabilities/
```

The directory you must internalize is `libcontainer/nsenter/`. It contains *C code* that runs before runc's Go `main()` ever executes, because changing namespaces in Go is unsound (the Go runtime starts multiple goroutines and OS threads early; you cannot reliably `setns` from a multithreaded process — most namespace operations require a single-threaded process). The C constructor runs from `__attribute__((constructor))`, does the namespace transitions, and only then lets Go take over.

### 3.2 The Two-Process (Actually Three-Process) Dance

When you run `runc create <id> --bundle /path/to/bundle`, the following happens:

```
┌─────────────────────────────────────────────────────────────────────┐
│ Process A: runc (the CLI process you invoked)                       │
│   - parses config.json                                              │
│   - validates bundle                                                │
│   - sets up the cgroup (writes to /sys/fs/cgroup/...)               │
│   - opens an init socket pair (bootstrap pipe + sync pipe)          │
│   - forks Process B with clone3(CLONE_PARENT | ...)                 │
│   - waits to read the container PID from the bootstrap pipe         │
└─────────────────────────────────────────────────────────────────────┘
              │
              │ clone3()
              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Process B: nsexec stage-1 (C code, before Go runtime)              │
│   - re-execs "runc init" inside fresh namespaces                    │
│   - actually: forks itself in three stages (parent / child / init)  │
│     to handle the kernel quirk that some setns calls require        │
│     a fresh PID namespace via a fork, not setns                     │
│   - drops mapping caps, applies user namespace mappings             │
│   - sends container PID back to Process A via bootstrap pipe        │
└─────────────────────────────────────────────────────────────────────┘
              │
              │ exec "/proc/self/exe init"
              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Process C: runc init (Go code, inside the new namespaces)           │
│   - reads the rest of config.json from sync pipe                    │
│   - mounts /proc, /sys, /dev, the bind mounts                       │
│   - pivot_root into rootfs                                          │
│   - applies capabilities                                            │
│   - applies seccomp filter                                          │
│   - applies AppArmor / SELinux label                                │
│   - applies rlimits                                                 │
│   - applies oom_score_adj                                           │
│   - signals "ready" on sync pipe                                    │
│   - blocks on a final "start" signal                                │
│     (this is what runc create returns to; container is CREATED)     │
│   - on "start": exec()s the entrypoint (CONTAINER NOW RUNNING)      │
└─────────────────────────────────────────────────────────────────────┘
```

There are *three* processes because of a kernel rule: to enter a new **PID namespace**, you must `fork()` — `setns(CLONE_NEWPID)` only changes the namespace your *children* will be in, not your own PID namespace. So the C code forks once to apply most namespaces, then forks again so that the grandchild ends up as PID 1 in the new PID namespace. The grandchild is the one that ultimately exec()s the entrypoint and becomes PID 1 inside the container. (Read `libcontainer/nsenter/nsexec.c` for the gory details; the stage names there are STAGE_PARENT, STAGE_CHILD, STAGE_INIT.)

### 3.3 The Bootstrap Pipe and the Sync Pipe

Two unix pipes connect runc and runc-init:

| Pipe | Direction | What it carries |
|---|---|---|
| **Bootstrap pipe** (fd 3) | child → parent | The container PID, after PID namespace creation. The parent needs this to write cgroup membership. |
| **Sync pipe** (fd 4) | bidirectional | Synchronization messages: `procReady`, `procHooks`, `procResume`, `procError`. The orchestration between create-time setup and start-time exec. |

The sync pipe is what makes the create/start split possible: after `runc init` finishes setup, it sends `procReady` and blocks on a read. `runc create` returns to its caller. Later, `runc start` finds the parked init by container ID, writes `procResume` to the sync pipe, and `runc init` proceeds to `execve(entrypoint)`.

### 3.4 cgroup Setup

cgroup creation happens in the **parent** (Process A) — the child cannot create its own cgroup because it doesn't have the privileges to write to `/sys/fs/cgroup` after entering namespaces. The parent:

1. Creates the cgroup directory based on `linux.cgroupsPath`. With cgroup-v2 and systemd-cgroup integration, this becomes a transient systemd scope like `kubepods-burstable-pod<uid>.slice/cri-containerd-<id>.scope`.
2. Writes the resource limits: `cpu.max`, `memory.max`, `pids.max`, etc.
3. Once the child PID is known (via the bootstrap pipe), writes that PID into `cgroup.procs`. The process is now in the cgroup; resource accounting and enforcement begin.

cgroup membership is "sticky": once a PID is in a cgroup, all its descendants are too, until something explicitly moves them.

### 3.5 The Seccomp Filter Installation

`runc init` compiles the seccomp JSON into a BPF program and installs it via `prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program)`. The compilation uses `libseccomp` to translate syscall names like `"execve"` into the numeric syscall number for the current architecture (different per x86_64 vs arm64). Once installed, the BPF filter applies to all syscalls from this process and its descendants — including the soon-to-be-exec'd entrypoint. There is no way to remove a seccomp filter; you can only add stricter ones.

If you ever wondered why `kubectl exec` into a container respects the original seccomp profile: it's because the exec'd shell inherits the same seccomp filter via cgroup namespace and process descent. (Technically the exec is done by joining namespaces, and the seccomp filter is on the process tree, so the new process inherits.)

### 3.6 pivot_root

The kernel call `pivot_root(new_root, put_old)` swaps the mount-namespace's root: `new_root` becomes `/`, and the old root is mounted at `put_old`. After that, runc unmounts and detaches the old root. The result: the container sees only its rootfs.

Why `pivot_root` and not `chroot`? `chroot` is easily escaped (any process with `CAP_SYS_CHROOT` can chroot back out by holding an open fd to the old root). `pivot_root` is harder to escape because it actually swaps the mount namespace's root — the old root is gone from the namespace once unmounted.

### 3.7 AppArmor, SELinux, and rlimits

Three more security primitives applied by `runc init` before exec:

- **AppArmor profile** (`process.apparmorProfile` in config.json) is loaded via the `aa_change_onexec()` libapparmor call. The profile name (e.g., `cri-containerd.apparmor.d`) must already be loaded into the kernel via `apparmor_parser`; runc only requests a transition to it. If the profile is not loaded, runc fails the container start with an explicit error. The transition is *deferred to the exec*: the runc init process itself is not under the profile; only after `execve()` does the kernel apply it. This is why a broken AppArmor profile manifests as "container starts then immediately exits with permission denied", not "container fails to create".

- **SELinux label** (`linux.selinuxLabel`) is applied via `setexeccon()`. Same deferred-to-exec model. On RHEL/CentOS/Fedora with SELinux enabled, kubelet assigns a per-container MCS label (Multi-Category Security: two random categories like `s0:c123,c456`) so that two containers cannot read each other's files even if they share a bind mount, because their labels differ.

- **rlimits** (`process.rlimits`) are set via `setrlimit()` calls in the init process before exec. The most important is `RLIMIT_NOFILE` (open file descriptor limit). Container defaults are usually much lower than host defaults (often 1024 vs the host's 1048576), which trips up servers expecting to handle many connections. Pod-level overrides go through containerd's runtime options.

The full order of operations in `runc init` (Process C from §3.2), once it has the full spec from the sync pipe, is:

```
1.  Setup tmpfs at /tmp, /run for the new mount namespace
2.  Mount /proc, /sys, /dev, devpts, cgroup (per config.mounts)
3.  Process bind mounts (kubelet's volumes, /etc/hosts, /etc/resolv.conf, etc.)
4.  Set up read-only paths (remount specific paths as ro)
5.  Set up masked paths (bind-mount /dev/null over them)
6.  pivot_root into rootfs
7.  unmount the old root, lazy-detach
8.  Apply sysctls (linux.sysctl)
9.  Apply capabilities (capset)
10. Apply rlimits (setrlimit)
11. Apply no_new_privs (prctl(PR_SET_NO_NEW_PRIVS))
12. Apply SELinux/AppArmor labels (deferred to exec)
13. Apply seccomp filter (prctl(PR_SET_SECCOMP) — cannot be removed)
14. Set umask, working directory, user (setuid/setgid)
15. Close all file descriptors except 0, 1, 2
16. Signal procReady on sync pipe; block on read for procResume
17. (on procResume) execve(entrypoint, argv, envp)
```

Notice that seccomp is *last* before the exec wait. This is deliberate: every step before seccomp needs to make syscalls that the seccomp profile would forbid (setresuid, prctl, mount, pivot_root, etc.). Once seccomp is installed, the process can only make syscalls the profile allows — typically a much smaller set centered on what the workload actually needs. The runc init process self-restricts on its way to becoming the workload.

### 3.8 What runc Doesn't Do

For completeness, here's what runc explicitly leaves to the layer above it:

- **Image management**: runc takes an already-unpacked rootfs. It does not pull, unpack, or know about images.
- **Networking**: runc creates a network namespace (if requested by config.json with no `path`), or joins one (if a `path` is given). It does not set up veth, IP addresses, or routes. That's CNI's job, and the container runtime (containerd) calls CNI before invoking runc.
- **Volume mounting**: runc does the bind mounts described in `config.json`. It does not provision or attach storage. That's CSI's job (chapter 19); the kubelet has already mounted the volume into a host path before the runtime is told about the container.
- **Restart policy**: runc does not restart anything. If the entrypoint dies, runc's job is over. The shim notifies containerd, which notifies kubelet via the CRI ContainerStatus, and kubelet decides whether to recreate the container based on the pod's `restartPolicy`.

This is the recurring theme: every layer aggressively minimizes its scope.

---

## 4. Alternative OCI Runtimes: crun, youki, Kata, gVisor

The OCI runtime spec is a contract. Anyone can implement a runtime that consumes `config.json` and produces a container. Kubernetes selects between runtimes per-pod via the **RuntimeClass** resource:

```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
handler: runsc
---
apiVersion: v1
kind: Pod
metadata:
  name: untrusted-workload
spec:
  runtimeClassName: gvisor   # use the gvisor RuntimeClass
  containers:
    - name: app
      image: untrusted-app:v1
```

The container runtime (containerd, CRI-O) translates `runtimeClassName` into the actual runtime binary or shim to invoke. Chapter 29 covers sandboxing in depth; here we only need a tour to know what each runtime *is*.

### 4.1 crun: A Faster runc Reimplementation

`crun` is a C implementation of the OCI runtime spec, from Red Hat. The same `config.json` works with both runc and crun.

- **Why it exists**: Go's startup overhead (runtime initialization, goroutine scheduling) is non-trivial for short-lived containers. crun's `create` and `start` are measurably faster — usually 2x for the create operation, due to no Go runtime startup.
- **Memory**: crun's RSS at idle is a few MB vs runc's tens of MB. At thousand-pod scale, that adds up.
- **Cgroup-v2**: crun was first to fully support cgroup-v2 features (memory.swap.max, memory.peak, the unified hierarchy). runc has caught up.
- **Where used**: default in Podman, RHEL, Fedora CoreOS. Optional in containerd via `runtime_type = "io.containerd.runc.v2"` with `binary_name = "crun"`.

### 4.2 youki: A Rust Reimplementation

`youki` is a Rust implementation of the OCI runtime spec. Similar story to crun (faster startup, lower memory than runc) but with Rust's memory-safety guarantees over runc's Go and crun's C. As of 2024 it is feature-complete enough for production but not yet a default in any major distribution. Notable because it is the cleanest modern read-through of the runtime spec — the source tree mirrors the spec section-by-section and is excellent for learning.

### 4.3 Kata Containers: Lightweight VMs

Kata fundamentally changes the threat model. Instead of using Linux namespaces for isolation, each pod runs inside its own **lightweight VM**, and Kata exposes an OCI runtime interface that hides the VM from kubelet.

```
        kubelet ──CRI──► containerd ──shim──► kata-runtime
                                                 │
                                                 │ spawns/manages VM
                                                 ▼
                                       ┌──────────────────────┐
                                       │  VM (QEMU / Cloud    │
                                       │       Hypervisor /    │
                                       │       Firecracker)    │
                                       │                       │
                                       │  ┌────────────────┐  │
                                       │  │ Guest Kernel    │  │
                                       │  │ (~5 MB stripped)│  │
                                       │  └────────────────┘  │
                                       │  ┌────────────────┐  │
                                       │  │ kata-agent      │  │
                                       │  │ (gRPC over     │  │
                                       │  │  vsock to host) │  │
                                       │  └────────┬───────┘  │
                                       │           │           │
                                       │           ▼           │
                                       │  ┌────────────────┐  │
                                       │  │ Containers      │  │
                                       │  │ (real runc inside│  │
                                       │  │  the VM)        │  │
                                       │  └────────────────┘  │
                                       └──────────────────────┘
```

The Kata shim presents the OCI runtime CLI to containerd. Internally it boots a VM, drops the rootfs in via virtio-fs (or virtio-blk), and talks to a kata-agent inside via vsock to actually start the container (the agent calls runc inside the guest). Hardware virtualization (Intel VT-x / AMD-V) provides isolation — a kernel exploit inside the container has to break out of a VM, not just a namespace.

- **Hypervisor choices**: QEMU (full-featured, slower boot ~1s), Cloud Hypervisor (Rust, ~200ms boot), Firecracker (AWS's microVM, ~125ms boot, very small surface).
- **Cost**: ~50 MB extra RAM per pod (guest kernel + agent), ~100-200ms extra cold-start.
- **When to use**: untrusted multi-tenant workloads, code execution services (CI runners, ML inference of user code), regulated environments. Covered in chapter 29.

### 4.4 gVisor (runsc): Userspace Kernel via Sentry

gVisor takes a different tack: instead of running a separate kernel in a VM, it implements a **userspace re-implementation of the Linux syscall interface**. Container processes still run on the host, but every syscall is intercepted and serviced by gVisor's "Sentry" — a userspace process written in Go.

```
        Container process makes syscall (e.g., open("/etc/passwd"))
              │
              │  ptrace or KVM trap
              ▼
        ┌────────────────────────────────┐
        │ Sentry (userspace kernel, Go) │
        │  - Implements ~250 syscalls    │
        │  - Re-issues a much smaller    │
        │    set of host syscalls        │
        │  - Filesystem ops sent to      │
        │    "Gofer" via 9P over vsock   │
        └─────────────┬──────────────────┘
                      │
                      ▼
                  Gofer (filesystem proxy)
                      │
                      ▼
                  Host kernel (small attack surface)
```

The container *thinks* it's talking to a Linux kernel, but it's talking to Sentry. Sentry only needs to make a handful of host syscalls — typically `read`, `write`, `mmap`, `epoll_wait`, plus the seccomp-filtered ones for I/O. This dramatically reduces the host kernel's attack surface from ~400 syscalls to maybe 50.

- **Tradeoff**: many syscalls are slower (extra context switch through Sentry); some are unsupported. Performance-sensitive workloads (databases, syscall-heavy apps) suffer.
- **Where used**: Google App Engine, Google Cloud Run, ad-hoc Kubernetes via the `gvisor` RuntimeClass.
- **Operational model**: presented as `runsc`, a drop-in `runc` replacement at the OCI runtime layer.

### 4.5 Quick Comparison

| Runtime | Isolation primitive | Cold start | Per-pod RAM overhead | Use case |
|---|---|---|---|---|
| **runc** | Linux namespaces + cgroups + seccomp | ~50ms | ~0 | Default, trusted workloads |
| **crun** | Same as runc | ~25ms | ~0 | Same as runc, faster |
| **youki** | Same as runc | ~30ms | ~0 | Same as runc, Rust |
| **runsc** (gVisor) | Userspace syscall interception | ~150ms | ~15 MB (Sentry+Gofer) | Untrusted code, reduce kernel CVE blast radius |
| **kata** (Cloud Hypervisor) | Hardware virtualization (VM) | ~250ms | ~50 MB (guest kernel) | Hard multi-tenancy, regulated workloads |
| **kata** (Firecracker) | Hardware virtualization (VM) | ~150ms | ~30 MB | Function-as-a-service, faster cold start |

All five are reached the same way from Kubernetes: declare a RuntimeClass, set `runtimeClassName` on the Pod, and the container runtime (containerd/CRI-O) routes the create call to the right runtime binary. Everything above the OCI boundary (CRI, kubelet, scheduler) doesn't know or care.

---

## 5. The OCI Image Specification (Brief)

Chapter 02 covers OCI images in full depth (manifests, indexes, signing, registries, lazy pulling). Here we need just enough to understand how the container runtime turns a registry URL into a `rootfs/` directory.

### 5.1 The Image Anatomy

An OCI image has three kinds of objects, all stored in the registry as **blobs** addressed by SHA-256 digest:

```
                  ┌──────────────────────────────────────┐
                  │ Image Index (optional, multi-arch)   │
                  │  manifests:                          │
                  │   - linux/amd64 → sha256:abc...       │
                  │   - linux/arm64 → sha256:def...       │
                  └────────────┬─────────────────────────┘
                               │
                               ▼
                  ┌──────────────────────────────────────┐
                  │ Image Manifest                       │
                  │  config:  sha256:111... (the JSON)   │
                  │  layers:                             │
                  │   - sha256:aaa... (tar.gz, 50 MB)    │  ← base image
                  │   - sha256:bbb... (tar.gz, 12 MB)    │  ← dependencies
                  │   - sha256:ccc... (tar.gz, 200 KB)   │  ← app binary
                  └─────────────────────────────────────-┘
                               │
                               ▼
                  ┌──────────────────────────────────────┐
                  │ Image Config (JSON)                  │
                  │  architecture: amd64                 │
                  │  os: linux                           │
                  │  config:                             │
                  │    Entrypoint: ["/usr/bin/nginx"]    │
                  │    Cmd: ["-g", "daemon off;"]        │
                  │    Env: [...]                        │
                  │    ExposedPorts: {"80/tcp": {}}      │
                  │  rootfs:                             │
                  │    type: layers                      │
                  │    diff_ids:                         │
                  │      - sha256:aaa-uncompressed...    │
                  │      - sha256:bbb-uncompressed...    │
                  │      - sha256:ccc-uncompressed...    │
                  │  history: [...]                      │
                  └──────────────────────────────────────┘
```

Each layer is a `tar.gz` archive containing the *changes* from the previous layer (added/modified files; deleted files use special whiteout markers, e.g. `.wh.filename`). When unpacked in order, they reconstruct the full filesystem.

### 5.2 From Image to rootfs: The Snapshotter

The container runtime's **snapshotter** (containerd term) or **storage driver** (Docker/Podman term) is responsible for unpacking image layers into a stacked filesystem that becomes the container's rootfs. The standard implementation uses **overlayfs**:

```
Image layers (unpacked, read-only):
  /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/1/fs/
  /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/2/fs/
  /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/3/fs/

Container's writable layer (overlayfs upperdir):
  /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/47/fs/

Mounted as the container's rootfs via:
  mount -t overlay overlay \
      -o lowerdir=/snapshots/3/fs:/snapshots/2/fs:/snapshots/1/fs,\
         upperdir=/snapshots/47/fs,\
         workdir=/snapshots/47/work \
      /run/containerd/io.containerd.runtime.v2.task/k8s.io/<id>/rootfs
```

The overlayfs `lowerdir` is a colon-separated stack of read-only image layers (topmost first). The `upperdir` is the container's writable layer; any modifications go there via copy-on-write. The `workdir` is overlayfs scratch space.

Key properties for Kubernetes:
- **Layers are shared across containers**: 100 pods all running `nginx:1.27` share the same underlying lowerdir files; only the writable upperdir is per-container. This is why image GC (§11) is non-trivial — you can't delete a layer until no container references it.
- **Inode pressure**: every file in every layer consumes an inode on the host filesystem. Containers with millions of files (Java apps, Node.js with deep `node_modules`) can exhaust inodes long before disk space. This is a real production failure.
- **Snapshotter alternatives**: `native` (just copies files, no overlay; slow), `btrfs` (uses btrfs subvolumes for snapshots), `zfs`, `stargz` and `soci` (lazy-pull — start the container before all layers are downloaded; fetch on file access). Covered in chapter 02.

### 5.3 Image Pull Authentication

Pulling a private image requires authentication. The flow:

1. Container runtime issues HTTP GET to the registry: `GET /v2/myorg/myimage/manifests/v1`.
2. Registry returns `401 Unauthorized` with a `WWW-Authenticate: Bearer realm="..."` header.
3. Container runtime exchanges credentials (basic auth, OAuth, or cloud-IAM tokens) at the realm URL for a short-lived bearer token.
4. Container runtime retries the manifest request with `Authorization: Bearer <token>`.
5. On success, fetches blobs (the layer tarballs).

Where credentials come from depends on the runtime and the cluster setup. In Kubernetes:
- **`imagePullSecrets`** on the Pod or its ServiceAccount: the kubelet reads the Secret and passes credentials to the runtime via `PullImage`'s `AuthConfig`.
- **Node-level credentials**: containerd's `registry.configs.<host>.auth` in `/etc/containerd/config.toml`, useful for shared registries.
- **Credential helpers**: `ecr-credential-provider`, `gcp-credential-provider` etc., kubelet-side plugins that fetch IAM-based tokens at pull time. This avoids storing long-lived registry credentials anywhere.

A common pitfall (§13): registry credentials live with kubelet/containerd, *not* with the workload. Putting `imagePullSecrets` on a Pod doesn't give the pod access to the registry; it tells kubelet how to pull the image on the pod's behalf.

---

## 6. containerd Architecture

containerd started life as Docker's runtime engine, was donated to the CNCF in 2017, and is now the default container runtime in essentially every Kubernetes distribution shipped after 2022. It is a long-running daemon that exposes a gRPC API for image and container management.

### 6.1 The Architecture Overview

```
                ┌─────────────────────────────────────────┐
                │ Clients: kubelet (via CRI plugin),       │
                │          ctr CLI, nerdctl, BuildKit, ... │
                └────────────────┬────────────────────────┘
                                 │ gRPC over Unix domain socket
                                 │ /run/containerd/containerd.sock
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│ containerd daemon                                                 │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐│
│  │ Services (gRPC API surface — split into namespaces)         ││
│  │  - ImagesService    (image metadata)                         ││
│  │  - ContentService   (content-addressable blob store)         ││
│  │  - SnapshotService  (layered rootfs builder)                 ││
│  │  - ContainersService (container metadata: spec, snapshot ref)││
│  │  - TasksService     (running instances: start, kill, exec)   ││
│  │  - EventsService    (pub/sub for state changes)              ││
│  │  - DiffService      (compute and apply tar diffs)            ││
│  │  - LeasesService    (prevent GC of in-flight objects)        ││
│  │  - NamespacesService (multi-tenant containerd namespaces)    ││
│  └──────────────────────────────────────────────────────────────┘│
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐│
│  │ Plugins (loaded at startup based on config.toml)             ││
│  │  - io.containerd.runtime.v2.task          (shim launcher)    ││
│  │  - io.containerd.snapshotter.v1.overlayfs (default)          ││
│  │  - io.containerd.snapshotter.v1.native                       ││
│  │  - io.containerd.snapshotter.v1.btrfs                        ││
│  │  - io.containerd.grpc.v1.cri              (CRI plugin!)      ││
│  │  - io.containerd.metadata.v1.bolt         (metadata DB)       ││
│  │  - io.containerd.content.v1.content        (CAS store)        ││
│  └──────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────┘
                                 │
                                 │ exec containerd-shim-runc-v2
                                 ▼
        ┌──────────────────────────────────────────────────────┐
        │ Shim process (one per pod sandbox + one per container)│
        │  - Owns the workload (parent of runc)                 │
        │  - Survives containerd restarts                       │
        │  - Holds stdio FDs, manages reattachment              │
        │  - Calls runc create / start / kill / delete          │
        └──────────────────────────┬───────────────────────────┘
                                   │ exec
                                   ▼
                       ┌──────────────────────┐
                       │ runc (or crun, ...)  │
                       │ briefly runs, exits  │
                       │ leaving entrypoint as│
                       │ child of shim        │
                       └──────────────────────┘
```

### 6.2 The CRI Plugin

containerd implements the Kubernetes CRI as an internal **plugin** (`io.containerd.grpc.v1.cri`), not as a separate process. The plugin registers a second gRPC listener on the same socket but with a different service prefix. When kubelet connects to `/run/containerd/containerd.sock`, it speaks CRI; when `ctr` connects, it speaks the native containerd API.

This matters because:
- The CRI plugin is *built into* containerd and ships in the same binary; you don't run a separate "cri-containerd" daemon (that was the pre-1.2 architecture and is dead).
- Disabling the CRI plugin in `config.toml` (e.g., `disabled_plugins = ["io.containerd.grpc.v1.cri"]`) gives you a containerd that won't work with Kubernetes — a common misconfiguration when copy-pasting config.
- The CRI plugin owns the **pod sandbox** abstraction (§9). Native containerd has only "containers"; the CRI plugin layers pod-sandbox-ness on top.

### 6.3 The Content Store

The content store is a **content-addressable storage (CAS)** layer for blobs — image manifests, image configs, layer tarballs, anything fetched from a registry. Everything is keyed by its SHA-256 digest.

```
/var/lib/containerd/io.containerd.content.v1.content/
├── blobs/
│   └── sha256/
│       ├── 1a2b3c4d... (manifest, 1.5 KB)
│       ├── aabbccdd... (image config, 3 KB)
│       ├── eeeeffff... (layer tarball, 50 MB)
│       └── ...
└── ingest/   (in-progress downloads)
    └── <random-id>/  (resumable; restarts after crash)
```

The CAS gives automatic deduplication: 100 images that share a base layer share *exactly one* blob on disk. The cost is a manifest-driven indirection — to find a layer, you read the image manifest, find the layer digest, then look up that digest in the content store.

Operations on the content store happen via the **ContentService** gRPC API: `Read`, `Write`, `Status`, `Update`, `Delete`, `Abort`. Writes are streaming and atomic — partial writes go to `ingest/` and only get linked into `blobs/` when complete and the digest verifies. This is what makes image pulls resumable after a crash.

### 6.4 The Snapshotter

The snapshotter is the layer that takes "unpacked image layers" and produces "a mounted rootfs for a container". It is **plugged-in** — you choose one in `/etc/containerd/config.toml`:

```toml
[plugins."io.containerd.grpc.v1.cri".containerd]
  snapshotter = "overlayfs"
```

Each snapshotter offers the same gRPC interface (`SnapshotService` with `Prepare`, `View`, `Commit`, `Remove`, `Stat`, `Update`), but implements them differently:

| Snapshotter | Tech | Tradeoffs |
|---|---|---|
| `overlayfs` | overlayfs | Default. Fast, mature, but inode-heavy on host filesystem |
| `native` | plain directories + cp -r | Works anywhere, very slow, copies every file per container |
| `btrfs` | btrfs subvolumes | Fast snapshots if your host fs is btrfs; not portable |
| `zfs` | ZFS datasets | Like btrfs but ZFS; rare in production K8s |
| `devmapper` | LVM thin pool | Pre-overlayfs choice; persists in EL7-era deployments |
| `stargz` | overlayfs + remote tar | Lazy pull: container starts before all layers downloaded |
| `soci` | overlayfs + SOCI index | AWS-built lazy pull; production-grade |

Snapshotter performance is one of those non-obvious cluster bottlenecks (§13). Pulling and unpacking a multi-GB image on every new node is expensive; lazy pull (stargz/soci) can cut cold-start by 60-80% for large images.

### 6.5 Metadata Store: bbolt

containerd persists object metadata (which images exist, which containers reference which snapshots, what the spec for a given container is) in **bbolt** (an embedded key-value store; the same one etcd v3 uses). The DB lives at:

```
/var/lib/containerd/io.containerd.metadata.v1.bolt/meta.db
```

A single file. Read with `bolt-cli`:

```
$ bolt buckets /var/lib/containerd/io.containerd.metadata.v1.bolt/meta.db
v1
$ bolt keys /var/lib/containerd/io.containerd.metadata.v1.bolt/meta.db v1/k8s.io/images
docker.io/library/nginx:1.27
registry.k8s.io/pause:3.9
...
```

This DB also stores leases (anti-GC reservations) and labels. Deleting it nukes all containerd metadata; the actual content blobs and snapshots stay on disk but are no longer referenced.

### 6.6 The shim v2 Architecture

This is the part of containerd that surprises people. Each running container — actually, each pod sandbox plus each container in it — has its own **shim** process. The shim is `containerd-shim-runc-v2`, a small Go binary that:

1. Is spawned by containerd when a task (running container) is created.
2. Forks/execs runc to set up the container.
3. **Becomes the parent of the entrypoint process** (because runc exec'd into the entrypoint and exited; the shim was runc's parent, so it inherits).
4. Holds the container's stdio (stdin/stdout/stderr) as open file descriptors, exposing them via FIFOs in `/run/containerd/.../io/`.
5. Watches for the container process to exit, captures the exit code, and reports it back to containerd via a UDS in `/run/containerd/.../shim.sock`.
6. Outlives containerd: if containerd crashes or is restarted (e.g., for an upgrade), the shim keeps running, the container keeps running, and on restart containerd reconnects to the shim's socket.

#### Why shims exist

Without a shim, the workload's parent would be containerd. If containerd crashes, the workload's PPID becomes init (1) — actually fine for the workload, but containerd loses its file descriptors for stdin/stdout/stderr, loses the ability to wait() on the process, and has no clean way to recover state. Adding a tiny intermediary that *only* holds the workload's lifecycle means containerd can crash and recover without disturbing anything.

#### Shim layout per pod

For a pod with two containers (excluding pause):

```
Pod sandbox: shim process A (containerd-shim-runc-v2)
   └── pause container (PID 1 of pod's PID namespace, if shared)
       └── (just sleeps, holds namespaces)

Container 1: shim process B
   └── nginx process (parent = shim B)

Container 2: shim process C
   └── sidecar process (parent = shim C)
```

That's *three* shim processes for one pod with two app containers. Each shim is ~5-15 MB RSS, so a node with 100 pods of 2 containers has ~300 shims using ~3 GB. This is *real overhead* and it shows up in node sizing.

#### Why "v2"

Shim v1 was containerd 1.0's per-runtime monolithic shim. Shim v2 (since containerd 1.2) is a **pluggable interface**: any runtime can implement its own shim (e.g., `containerd-shim-kata-v2`, `containerd-shim-runsc-v1` for gVisor) by satisfying a small gRPC protocol. This is what makes RuntimeClass work — containerd looks at the runtime handler and execs the appropriate shim binary. The shim then handles its own runtime invocation (or VM management, in Kata's case).

The shim v2 protocol (defined in `containerd/runtime/v2/task/shim.proto`):
- `Create`, `Start`, `Delete`, `Kill`
- `Exec`, `ResizePty`
- `State`, `Pids`, `Stats`
- `Wait`, `Pause`, `Resume`, `Checkpoint`, `Update`, `Connect`, `Shutdown`

### 6.7 Directory Layout: /var/lib/containerd and /run/containerd

A cheat sheet for "where does containerd put things":

```
/var/lib/containerd/                                # persistent state
├── io.containerd.content.v1.content/               # CAS blob store
│   └── blobs/sha256/<digest>                       #   - layer tarballs, manifests
├── io.containerd.metadata.v1.bolt/meta.db          # bolt metadata DB
├── io.containerd.snapshotter.v1.overlayfs/         # snapshotter state
│   ├── metadata.db                                 #   - snapshotter's own metadata
│   └── snapshots/<id>/fs/                          #   - unpacked layer contents
└── tmpmounts/                                      # transient mounts during unpack

/run/containerd/                                    # ephemeral (tmpfs)
├── containerd.sock                                 # main gRPC socket (kubelet/ctr)
├── containerd.sock.ttrpc                           # ttrpc variant
├── io.containerd.runtime.v2.task/                  # per-task working directories
│   └── k8s.io/                                     #   - one namespace per CRI use
│       └── <container-id>/
│           ├── config.json                         #   - the OCI runtime spec
│           ├── init.pid                            #   - PID of container's init
│           ├── log.json                            #   - shim log
│           ├── address                             #   - shim socket address
│           └── rootfs/                             #   - the mounted overlayfs
└── debug.sock                                      # debugging endpoint
```

When a container is gone, its directory under `io.containerd.runtime.v2.task/k8s.io/` is deleted. The snapshot under `io.containerd.snapshotter.v1.overlayfs/snapshots/` is deleted only when no other container references the same lower stack.

---

## 7. CRI-O Architecture

CRI-O is the other major CRI implementation. Where containerd is a general-purpose container runtime that *also* implements CRI via a plugin, CRI-O is *purpose-built* for Kubernetes. It implements only the CRI; there is no general gRPC API, no `ctr`-equivalent for general use, no design accommodation for non-Kubernetes consumers.

### 7.1 The Pitch

CRI-O's argument: kubelet wants CRI, and only CRI. Building a runtime that supports only CRI lets you:
- Strip out everything else (no Docker compat, no BuildKit integration, no nerdctl).
- Track CRI versions tightly: each CRI-O minor version maps to a Kubernetes minor version (CRI-O 1.30 ↔ K8s 1.30).
- Smaller surface area = smaller attack surface, fewer features that aren't used.

### 7.2 Architecture

```
              kubelet
                │
                │ CRI gRPC over /var/run/crio/crio.sock
                ▼
         ┌──────────────────────────────────┐
         │ CRI-O daemon                      │
         │  - Implements RuntimeService      │
         │  - Implements ImageService        │
         │  - Uses containers/storage for    │
         │    image layer management         │
         │  - Uses containers/image for      │
         │    registry/pull logic            │
         │  - Spawns conmon per container    │
         └──────────────┬───────────────────┘
                        │ exec conmon
                        ▼
              ┌─────────────────────┐
              │ conmon (C, tiny)    │  ← analog to containerd's shim
              │  - holds stdio      │
              │  - waits on runc    │
              │  - reports exit     │
              └──────────┬──────────┘
                         │ exec
                         ▼
                     runc / crun
```

`conmon` plays the same role as containerd's shim: be the parent of the workload, survive CRI-O restarts, hold stdio, report exit. It's written in C (~3K LOC vs the shim's Go), tiny, and very stable.

CRI-O reuses libraries from the **containers/** organization (Red Hat / Podman's stack):
- `containers/storage` — the layered storage library (analog to containerd's snapshotter).
- `containers/image` — the image fetch and unpack library.
- `containers/common` — shared policy and config parsing.

The same libraries underpin Podman and Buildah, which is why those three tools share image storage at `/var/lib/containers/storage/` and can see each other's images.

### 7.3 Where CRI-O Lives in Production

- **OpenShift** — Red Hat's distribution exclusively uses CRI-O. CRI-O is the upstream of what ships in RHCOS.
- **Fedora CoreOS / RHEL CoreOS** — defaults to CRI-O.
- **kubeadm-built clusters** — increasingly default to containerd, but CRI-O is well-supported.

For most workloads, the choice between containerd and CRI-O is invisible. Performance is comparable; feature parity is high. CRI-O is simpler if you only ever run Kubernetes; containerd is more flexible if you also want a general container runtime on the same node (BuildKit jobs, ad-hoc `ctr run`, nerdctl).

---

## 8. The CRI gRPC Contract

The Container Runtime Interface is the gRPC API that kubelet uses to talk to the container runtime. It is **the** interface that defines what kubelet expects from a runtime and what a runtime is expected to provide.

### 8.1 The Two Services

Defined in `kubernetes/staging/src/k8s.io/cri-api/pkg/apis/runtime/v1/api.proto`:

```protobuf
service RuntimeService {
    // Sandbox lifecycle (the "pod" half)
    rpc RunPodSandbox(RunPodSandboxRequest) returns (RunPodSandboxResponse) {}
    rpc StopPodSandbox(StopPodSandboxRequest) returns (StopPodSandboxResponse) {}
    rpc RemovePodSandbox(RemovePodSandboxRequest) returns (RemovePodSandboxResponse) {}
    rpc PodSandboxStatus(PodSandboxStatusRequest) returns (PodSandboxStatusResponse) {}
    rpc ListPodSandbox(ListPodSandboxRequest) returns (ListPodSandboxResponse) {}

    // Container lifecycle (within a sandbox)
    rpc CreateContainer(CreateContainerRequest) returns (CreateContainerResponse) {}
    rpc StartContainer(StartContainerRequest) returns (StartContainerResponse) {}
    rpc StopContainer(StopContainerRequest) returns (StopContainerResponse) {}
    rpc RemoveContainer(RemoveContainerRequest) returns (RemoveContainerResponse) {}
    rpc ListContainers(ListContainersRequest) returns (ListContainersResponse) {}
    rpc ContainerStatus(ContainerStatusRequest) returns (ContainerStatusResponse) {}
    rpc UpdateContainerResources(UpdateContainerResourcesRequest) returns (UpdateContainerResourcesResponse) {}
    rpc ReopenContainerLog(ReopenContainerLogRequest) returns (ReopenContainerLogResponse) {}

    // Streaming (exec/attach/port-forward)
    rpc ExecSync(ExecSyncRequest) returns (ExecSyncResponse) {}      // blocking, for liveness/readiness probes
    rpc Exec(ExecRequest) returns (ExecResponse) {}                  // streaming, for kubectl exec
    rpc Attach(AttachRequest) returns (AttachResponse) {}
    rpc PortForward(PortForwardRequest) returns (PortForwardResponse) {}

    // Stats and metadata
    rpc ContainerStats(ContainerStatsRequest) returns (ContainerStatsResponse) {}
    rpc ListContainerStats(ListContainerStatsRequest) returns (ListContainerStatsResponse) {}
    rpc PodSandboxStats(PodSandboxStatsRequest) returns (PodSandboxStatsResponse) {}
    rpc ListPodSandboxStats(ListPodSandboxStatsRequest) returns (ListPodSandboxStatsResponse) {}
    rpc UpdateRuntimeConfig(UpdateRuntimeConfigRequest) returns (UpdateRuntimeConfigResponse) {}
    rpc Status(StatusRequest) returns (StatusResponse) {}            // runtime self-status

    // Checkpoint/restore (CRIU)
    rpc CheckpointContainer(CheckpointContainerRequest) returns (CheckpointContainerResponse) {}
    rpc GetContainerEvents(GetEventsRequest) returns (stream ContainerEventResponse) {}
}

service ImageService {
    rpc ListImages(ListImagesRequest) returns (ListImagesResponse) {}
    rpc ImageStatus(ImageStatusRequest) returns (ImageStatusResponse) {}
    rpc PullImage(PullImageRequest) returns (PullImageResponse) {}
    rpc RemoveImage(RemoveImageRequest) returns (RemoveImageResponse) {}
    rpc ImageFsInfo(ImageFsInfoRequest) returns (ImageFsInfoResponse) {}
}
```

Two services, ~30 RPCs total. That is the entirety of what kubelet asks of a container runtime.

### 8.2 The Pod Sandbox Abstraction

The key concept in CRI is the **PodSandbox**: an abstract holder for a pod's shared namespaces and shared lifecycle. In every implementation today, the pod sandbox is realized as a **pause container** (§9): a near-empty container that exists only to *hold* the pod's network, IPC, and UTS namespaces.

`RunPodSandbox` takes a `PodSandboxConfig`:

```protobuf
message PodSandboxConfig {
    PodSandboxMetadata metadata = 1;   // name, uid, namespace, attempt
    string hostname = 2;
    string log_directory = 3;          // where the runtime should put container logs
    DNSConfig dns_config = 4;          // /etc/resolv.conf content
    repeated PortMapping port_mappings = 5;
    map<string, string> labels = 6;
    map<string, string> annotations = 7;
    LinuxPodSandboxConfig linux = 8;   // cgroup parent, security context, sysctls
    WindowsPodSandboxConfig windows = 9;
}
```

`CreateContainer` takes a `ContainerConfig`, scoped to a sandbox:

```protobuf
message CreateContainerRequest {
    string pod_sandbox_id = 1;          // which sandbox to join
    ContainerConfig config = 2;
    PodSandboxConfig sandbox_config = 3;  // also passed for context
}

message ContainerConfig {
    ContainerMetadata metadata = 1;
    ImageSpec image = 2;
    repeated string command = 3;
    repeated string args = 4;
    string working_dir = 5;
    repeated KeyValue envs = 6;
    repeated Mount mounts = 7;
    repeated Device devices = 8;
    map<string, string> labels = 9;
    map<string, string> annotations = 10;
    string log_path = 11;
    bool stdin = 12;
    bool stdin_once = 13;
    bool tty = 14;
    LinuxContainerConfig linux = 15;   // resources, security context, capabilities
    WindowsContainerConfig windows = 16;
}
```

The runtime's job in `CreateContainer` is to:
1. Look up the sandbox to find its namespaces.
2. Construct a `config.json` that joins those namespaces (via `linux.namespaces[i].path`).
3. Set up the rootfs from the image, the mounts, the cgroup limits.
4. Call the OCI runtime's `create` (not `start`).

`StartContainer` then calls the OCI runtime's `start`, releasing the parked init to exec the entrypoint.

### 8.3 Streaming RPCs and the Streaming Server

`Exec`, `Attach`, `PortForward` are special. They don't return streamed gRPC data; they return a **URL** to a separate "streaming server" that the runtime hosts on a different port. kubelet then proxies that URL to the apiserver, which proxies it to `kubectl`. The actual data flow uses HTTP/2 streams over SPDY/WebSocket.

This indirection lets the streaming server run independently of the main CRI socket; it can be on a different process, different port, even different machine. In practice both containerd and CRI-O run the streaming server in the same process.

`ExecSync` is different: it is fully synchronous and returns the entire stdout/stderr as a `bytes` field in the response. It's what kubelet uses for `exec`-style readiness/liveness probes — the command runs, output is buffered, exit code returned. There's a configurable timeout but no streaming, which makes it unsuitable for long-running probe commands.

### 8.4 ContainerEvents (newer)

The `GetContainerEvents` RPC (added in CRI v1.25+) is a streaming subscription to container state changes:

```protobuf
message ContainerEventResponse {
    string container_id = 1;
    ContainerEventType container_event_type = 2;  // CREATED, STARTED, STOPPED, DELETED
    int64 created_at = 3;
    PodSandboxStatus pod_sandbox_status = 4;
    repeated ContainerStatus containers_statuses = 5;
}
```

Before this, kubelet's PLEG (Pod Lifecycle Event Generator; chapter 10) discovered state changes by *polling* `ListPodSandbox` + `ListContainers` every second. The streaming event API replaces polling with push, dramatically reducing PLEG overhead on dense nodes. The kubelet feature gate is `EventedPLEG`.

---

## 9. The Pause Container

The pod sandbox abstraction in CRI is realized as a pause container. Understanding the pause container is understanding why a pod is more than just "a group of containers".

### 9.1 What It Does

A Pod's containers share Linux namespaces — at minimum the network namespace (so they share an IP and can `localhost` each other), the IPC namespace (so they can use POSIX SHM/sysv IPC), and the UTS namespace (so they share a hostname). They optionally share the PID namespace (with `shareProcessNamespace: true`, so they can see each other's processes).

The question is: *which process owns those namespaces?*

A Linux namespace exists as long as at least one process is a member of it. If you create a network namespace, run a container in it, and the container exits, the namespace is destroyed — its IP address, routes, iptables rules, all gone. To keep the namespace alive across multiple containers (which start, exit, get restarted), some process has to perpetually live in it.

That's the pause container. It is a tiny binary whose only job is to:
1. Set up the namespaces (or, in modern setups, join the namespaces created by the CRI implementation).
2. Reap any orphaned zombie processes (it's PID 1 if the pod uses a shared PID namespace).
3. **Block forever** until signaled to exit.

When kubelet says "kill this pod", the runtime sends SIGTERM to the pause container; it exits; all namespaces it held are torn down; the pod is gone.

### 9.2 What's Inside It

The reference pause image is `registry.k8s.io/pause`. The source is in `kubernetes/build/pause/`. As of recent versions it's a Go program with a fallback to a tiny C program — the entire thing is a few hundred bytes:

```c
/* Excerpted, lightly cleaned, from kubernetes/build/pause/linux/pause.c */
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static void sigdown(int signo) {
    psignal(signo, "Shutting down, got signal");
    exit(0);
}

static void sigreap(int signo) {
    while (waitpid(-1, NULL, WNOHANG) > 0) ;
}

int main() {
    if (getpid() != 1) {
        fprintf(stderr, "Warning: pause should be the first process\n");
    }
    if (sigaction(SIGINT,  &(struct sigaction){.sa_handler = sigdown}, NULL) < 0) return 1;
    if (sigaction(SIGTERM, &(struct sigaction){.sa_handler = sigdown}, NULL) < 0) return 2;
    if (sigaction(SIGCHLD, &(struct sigaction){
                            .sa_handler = sigreap,
                            .sa_flags = SA_NOCLDSTOP}, NULL) < 0) return 3;
    for (;;) pause();   // sleep forever
    return 0;           // unreachable
}
```

It is `~700 KB` compressed in the image. Its purpose:
- `pause()` syscall: blocks forever, consumes no CPU.
- `SIGCHLD` handler reaps zombies: necessary because in a shared PID namespace where pause is PID 1, any orphaned child of any container becomes pause's responsibility to reap.
- `SIGTERM/SIGINT` handler: graceful exit, releasing all namespaces.

### 9.3 The Pod Topology

```
            ┌─────────────────────────────────────────────────────────┐
            │ Pod                                                       │
            │                                                            │
            │  ┌────────────────────────────────────────────────────┐  │
            │  │ Shared namespaces:                                  │  │
            │  │   - network (eth0=podIP, lo)                        │  │
            │  │   - ipc                                             │  │
            │  │   - uts (hostname)                                  │  │
            │  │   - pid (only if shareProcessNamespace: true)       │  │
            │  └────────────────────────────────────────────────────┘  │
            │                                                            │
            │  ┌──────────┐   ┌──────────┐   ┌──────────┐              │
            │  │  pause   │   │  app     │   │  sidecar │              │
            │  │ (PID 1)  │   │ (PID 6)  │   │ (PID 12) │              │
            │  │ owns     │   │ joins    │   │ joins    │              │
            │  │ ns       │   │ pause's  │   │ pause's  │              │
            │  │          │   │ ns       │   │ ns       │              │
            │  │ has own  │   │ has own  │   │ has own  │              │
            │  │ mount ns │   │ mount ns │   │ mount ns │              │
            │  │ rootfs:  │   │ rootfs:  │   │ rootfs:  │              │
            │  │ pause    │   │ nginx    │   │ envoy    │              │
            │  └──────────┘   └──────────┘   └──────────┘              │
            └─────────────────────────────────────────────────────────┘
```

The pause container's mount and cgroup namespaces are its own; only network, IPC, UTS (and optionally PID) are shared. That's why each container in a pod sees a different filesystem (its own image) but the same network interfaces and hostname.

### 9.4 Variations and Optimizations

In some configurations (containerd-CRI with the right config, or for static pods), the pause container isn't actually run as a process; instead, the network namespace is created via `ip netns add` (an open file descriptor in `/var/run/netns/<id>` keeps the namespace alive without a process). This saves a few MB of memory per pod at the cost of slightly more complex bookkeeping. Both containerd and CRI-O can do this; whether they do depends on configuration.

Even when pause is run, the image is so trivial that a single pause image is shared across every pod on the node — overlayfs lowerdir dedup means 100 pods cost ~700 KB of disk total for pause.

---

## 10. Lifecycle Walkthrough: A Pod From Kubelet's Perspective

Now we tie it all together: trace what happens when kubelet sees a Pod assigned to its node, from the moment the scheduler binds until the pod is `Running`.

### 10.1 The Setup

A pod with one init container and two regular containers:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: web
  namespace: prod
  uid: 7f3b...
spec:
  initContainers:
    - name: schema-init
      image: schema:v3
      command: ["/usr/local/bin/migrate"]
  containers:
    - name: nginx
      image: nginx:1.27
      ports: [{containerPort: 80}]
    - name: sidecar
      image: log-shipper:v2
  imagePullSecrets:
    - name: registry-creds
```

The scheduler has just written `spec.nodeName=node-2`.

### 10.2 The Trace

```
[T=0]    kubelet on node-2 sees Pod via watch
         podWorker enqueued

[T=10ms] podWorker starts syncPod()
         - Volume manager: nothing to mount (no PVCs)
         - Determines pod has no existing sandbox → create one

[T=15ms] kubelet calls CRI:
         RuntimeService.RunPodSandbox(PodSandboxConfig{
             metadata: {name: "web", namespace: "prod", uid: "7f3b..."},
             hostname: "web",
             log_directory: "/var/log/pods/prod_web_7f3b...",
             dns_config: {...},
             linux: {
                 cgroup_parent: "kubepods-burstable-pod7f3b....slice",
                 security_context: {namespace_options: {pid: CONTAINER}},
                 ...
             },
             annotations: {"kubernetes.io/config.source": "api", ...},
         })

[T=20ms] containerd CRI plugin:
         - Generates sandbox ID: abc123def456...
         - Calls CNI ADD via /opt/cni/bin/<plugin>
             → CNI allocates IP 10.244.2.17, creates veth, sets routes
             → CNI returns network namespace path /var/run/netns/cni-abc123
         - Pulls pause:3.9 image if not cached (it's almost always cached)
         - Creates config.json for pause:
             - namespaces: [{type: net, path: /var/run/netns/cni-abc123},
                            {type: ipc, path: /var/run/ipcns/cni-abc123},
                            {type: uts, path: /var/run/utsns/cni-abc123},
                            {type: pid}, {type: mount}, {type: cgroup}]
         - Spawns containerd-shim-runc-v2 for the sandbox
         - Shim calls: runc create <sandbox-id> --bundle /run/.../sandbox
         - Shim calls: runc start <sandbox-id>
         - pause is now running, holding namespaces

[T=180ms] kubelet calls CRI:
         RuntimeService.PodSandboxStatus(sandbox-id)
         → returns {state: SANDBOX_READY, network: {ip: "10.244.2.17"}}

[T=185ms] kubelet sees sandbox ready; proceeds to init container

[T=190ms] kubelet calls CRI:
         ImageService.PullImage(ImageSpec{image: "schema:v3"},
                                AuthConfig{from registry-creds Secret})
         → containerd pulls layers, unpacks via snapshotter, returns image ref

[T=2500ms] (assume 2.3s to pull)
         kubelet calls CRI:
         RuntimeService.CreateContainer(sandbox-id, ContainerConfig{
             metadata: {name: "schema-init"},
             image: {image: "schema:v3"},
             command: ["/usr/local/bin/migrate"],
             envs: [...],
             mounts: [
                 {host_path: ".../etc-hosts", container_path: "/etc/hosts"},
                 {host_path: ".../sa-token",  container_path: "/var/run/.../serviceaccount", readonly: true},
             ],
             log_path: "schema-init/0.log",
             linux: {
                 resources: {memory_limit_in_bytes: 536870912, cpu_quota: 100000, cpu_period: 100000},
                 security_context: {capabilities: {drop_capabilities: ["ALL"]}, ...},
             },
         }, sandbox_config)
         → containerd:
           - creates snapshot from schema:v3 layers (writable upperdir)
           - builds config.json joining sandbox's net/ipc/uts ns
           - spawns shim for this container
           - shim calls: runc create <init-id> --bundle /run/.../init
           → container is in "created" state, entrypoint not exec'd yet

[T=2520ms] kubelet calls CRI:
         RuntimeService.StartContainer(<init-id>)
         → shim calls: runc start <init-id>
         → runc init sends procResume on sync pipe
         → runc init does execve("/usr/local/bin/migrate")
         → migrate runs, exits with code 0

[T=4800ms] PLEG (or EventedPLEG) observes container exit
         kubelet calls CRI:
         RuntimeService.ContainerStatus(<init-id>)
         → returns {state: CONTAINER_EXITED, exit_code: 0, finished_at: ...}
         kubelet: init container succeeded, proceed to regular containers

[T=4810ms] kubelet calls CRI for nginx:
         ImageService.PullImage("nginx:1.27", ...)
         (parallel for sidecar: PullImage("log-shipper:v2", ...))

[T=8000ms] Both images pulled.
         CreateContainer(nginx) → StartContainer(nginx)
         CreateContainer(sidecar) → StartContainer(sidecar)
         (the two run in parallel; native sidecars in K8s 1.28+ have ordering)

[T=8200ms] Both containers running.
         PLEG observes via ContainerStats / events.

[T=8210ms] kubelet runs readiness probe (if defined):
         For HTTP probe: kubelet itself does HTTP GET against pod IP:port
         For exec probe: RuntimeService.ExecSync(container-id, ["cat", "/ready"], timeout=1s)
         → blocks, returns stdout/stderr/exitcode

[T=8500ms] Readiness probe succeeds.
         Status manager: PATCH pod.status = {phase: Running, conditions: [Ready=True], ...}
```

A few observations:

- **The shim is invisible to kubelet.** kubelet only talks to containerd; the shim is an internal implementation detail.
- **CNI happens between RunPodSandbox start and pause exec**, completely outside kubelet's awareness. kubelet just sees `RunPodSandbox` return with a pod IP.
- **Image pull is per-CreateContainer**, but `PullImage` is idempotent — if the image is cached, it returns instantly. kubelet calls it even on cache hits as a deduplication mechanism.
- **`UpdateContainerResources`** (not shown) is called when in-place pod resize (K8s 1.27+) updates cpu/memory limits; the runtime writes new values to the cgroup without restarting.

### 10.3 What Each Layer Logs

When debugging the trace above, it's worth knowing what *each* layer writes and where:

| Layer | Where logs go | What they look like |
|---|---|---|
| kubelet | `journalctl -u kubelet` | High-level events: "SyncLoop (ADD)", "Pulling image", "Created container", PLEG transitions |
| CRI plugin (containerd) | `journalctl -u containerd` | "RunPodSandbox", "PullImage", "CreateContainer" with full request/response at debug level |
| Shim | `/run/containerd/io.containerd.runtime.v2.task/k8s.io/<id>/log.json` (one file per task, JSON lines) | Per-task lifecycle, runc command lines, exec invocations |
| runc | stderr, propagated through shim into shim's log | Error messages from kernel calls; on success runc is silent |
| Container's entrypoint | `/var/log/pods/<ns>_<pod>_<uid>/<container>/<attempt>.log` (kubelet's path) | Symlinked-from `/var/log/containers/<pod>_<ns>_<container>-<id>.log` for log shippers |
| CNI plugin | Plugin-specific, often `/var/log/calico/cni/cni.log` or similar; also captured by containerd if plugin writes to stderr | IP allocation, veth creation, NetworkPolicy programming |

The chain is *not* fully unified: a single pod start touches all of these, and a failure can leave a useful message in any of them. Production runbooks should include greps against each.

### 10.4 Pod Deletion (briefly)

When a pod is deleted:

```
kubelet sees deletionTimestamp on pod
  │
  ▼
For each container in pod:
  RuntimeService.StopContainer(container-id, timeout=terminationGracePeriodSeconds)
   → shim sends SIGTERM to container's PID 1
   → waits up to timeout for graceful exit
   → if timeout: SIGKILL
   → shim collects exit code, exits itself
  RuntimeService.RemoveContainer(container-id)
   → removes shim's state directory, releases snapshot
  │
  ▼
RuntimeService.StopPodSandbox(sandbox-id)
  → SIGTERM to pause
  → CNI DEL: tear down veth, release IP
  → sandbox enters NOTREADY state
RuntimeService.RemovePodSandbox(sandbox-id)
  → fully cleanup
  │
  ▼
kubelet: PATCH pod (remove finalizer) → apiserver deletes from etcd
```

The `preStop` hook (chapter 11) runs *before* SIGTERM, inside the StopContainer flow.

---

## 11. Image Garbage Collection and Disk Pressure

A node's disk fills up over time as images accumulate. Each new image version, each test deploy, each ephemeral container with copy-on-write modifications, all leave behind blobs and snapshots. Without GC, the disk fills, kubelet evicts pods, the node becomes unhealthy.

### 11.1 What Gets GC'd

Two distinct stores need GC:

| Store | Contents | Reclaim by |
|---|---|---|
| Image layers (content store + snapshotter) | Layer tarballs, image configs, manifests, unpacked snapshots | Removing unused images |
| Container writable layers (snapshotter upperdir) | Modifications made by exited containers | Removing the container (RemoveContainer) |

Container writable layers are GC'd quickly: when a container is removed via `RemoveContainer`, its snapshot is reclaimed. Kubelet's **container GC** runs periodically to remove exited containers older than a threshold, even from terminated pods (so logs can be inspected for a while). Defaults: keep up to 1 dead container per pod, keep up to 240 dead containers per node, max age 0 (no time-based GC by default).

Image GC is trickier because layers are shared.

### 11.2 Kubelet's Image GC Loop

Kubelet's image GC is configured via flags or KubeletConfiguration:

```yaml
imageGCHighThresholdPercent: 85   # start GC when imagefs is 85% full
imageGCLowThresholdPercent:  80   # GC until imagefs is back to 80%
imageMinimumGCAge: 2m              # don't GC images less than 2 minutes old
imageMaximumGCAge: 0               # 0 = disabled; otherwise force-GC images older than this
```

The loop, every 5 minutes by default:

```
1. Query CRI: ImageService.ImageFsInfo() → bytes used, inodes used
2. If usage% < High threshold → done
3. List all images: ImageService.ListImages()
4. For each image, query containerd: which containers reference it?
   (Built from PodStatus snapshots — kubelet maintains a per-image lastUsed timestamp)
5. Sort unused images by lastUsed ascending
6. Until usage% < Low threshold OR all eligible images deleted:
     ImageService.RemoveImage(image)
     → containerd removes manifest, unreferenced layer blobs, snapshots
```

The key wrinkle is "which containers reference it". Kubelet remembers when an image was last used (last time it was a container's image, in a known PodStatus). Images currently used by any *running or recently-stopped* container are never GC'd.

### 11.3 Container Runtime's Own GC

Both containerd and CRI-O have their own internal GC for unreferenced blobs in the content store, snapshots that have no parent reference, and stale leases. This runs periodically (default 30s in containerd) and is driven by *lease references*, not by kubelet:

- Pulling an image creates a **lease** that prevents GC of the manifest and layers.
- Creating a container creates a lease on the snapshot.
- When the container is removed and the lease released, the next GC cycle reclaims orphans.

Kubelet's GC and containerd's GC are *cooperative*: kubelet removes images (releasing leases); containerd's GC then reclaims the underlying storage.

### 11.4 Eviction Interplay

If disk pressure exceeds kubelet's **eviction thresholds** (default `imagefs.available<15%` or `nodefs.available<10%`), kubelet starts evicting pods. The order:

1. **Image GC fires** first — try to reclaim unused images.
2. **Container GC fires** — remove exited containers.
3. **Pod eviction** if still under pressure: kubelet selects pods to evict, BestEffort first, then Burstable using more than their requests, then Guaranteed pods only as a last resort.

Eviction is harsher than GC: a running pod gets a `kubectl delete pod`-equivalent. The pod's containers are stopped, the pod is marked `Failed`, and the scheduler reschedules it (if it's part of a controller). On a node with many large images, *aggressive image GC* often staves off eviction; misconfigured `imageGCHighThresholdPercent: 99` is a classic pager-trigger.

---

## 12. Performance and Debugging: ctr, crictl, runc, nsenter

When the runtime is the suspect, you need to drop below kubectl. The three tools you must know cold are `crictl` (CRI debugging), `ctr` (native containerd), and `runc` (the OCI layer). Plus `nsenter` for kernel-level inspection.

### 12.1 crictl

`crictl` is kubelet's debugging tool. It speaks CRI directly to whatever runtime kubelet is configured to use (containerd or CRI-O), via the same socket. It is **the** tool for "what does kubelet see?" since it bypasses kubelet entirely and asks the runtime the same questions.

Configuration: `/etc/crictl.yaml`
```yaml
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint:   unix:///run/containerd/containerd.sock
timeout: 10
debug: false
```

Daily commands:

```
# List pod sandboxes (CRI's notion of "pods")
$ crictl pods
POD ID         CREATED       STATE   NAME              NAMESPACE   ATTEMPT  RUNTIME
abc123def456   2 hours ago   Ready   web-deploy-xyz    prod        0        runc
98a1b2c3d4e5   1 hour ago    Ready   coredns-789       kube-system 0        runc

# List containers (across all pods)
$ crictl ps
CONTAINER      IMAGE              CREATED       STATE    NAME         POD          NAMESPACE
1a2b3c4d       nginx:1.27         2 hours ago   Running  nginx        web-deploy   prod
5e6f7a8b       log-shipper:v2     2 hours ago   Running  sidecar      web-deploy   prod
9c0d1e2f       coredns:1.11       1 hour ago    Running  coredns      coredns-789  kube-system

# Inspect a container (full ContainerStatus including OCI spec annotations)
$ crictl inspect 1a2b3c4d
{
  "status": {
    "id": "1a2b3c4d...",
    "metadata": {"name": "nginx", "attempt": 0},
    "state": "CONTAINER_RUNNING",
    "createdAt": "...",
    "startedAt": "...",
    "image": {"image": "docker.io/library/nginx:1.27"},
    "imageRef": "sha256:abcdef...",
    "reason": "",
    "exitCode": 0,
    "mounts": [...],
    "logPath": "/var/log/pods/prod_web-deploy_.../nginx/0.log"
  },
  "info": {
    "pid": 14523,
    "sandboxID": "abc123def456...",
    "runtimeType": "io.containerd.runc.v2",
    "runtimeSpec": { /* full OCI config.json! */ }
  }
}

# Tail container logs (just reads the logPath file with kubectl-logs format)
$ crictl logs -f --tail 100 1a2b3c4d

# Exec into a container (analogous to kubectl exec)
$ crictl exec -it 1a2b3c4d /bin/sh

# Pull an image manually
$ crictl pull nginx:1.27

# List cached images
$ crictl images
IMAGE                                TAG     IMAGE ID       SIZE
docker.io/library/nginx              1.27    sha256:abcde   192MB
registry.k8s.io/pause                3.9     sha256:fghij   744kB
...

# Force runtime to remove an image (will fail if in use)
$ crictl rmi nginx:1.26

# Stats (live per-container CPU/memory)
$ crictl stats
CONTAINER     CPU%   MEM      DISK     INODES
1a2b3c4d      0.3%   42.1MB   12.5MB   201
5e6f7a8b      0.0%   8.7MB    1.2MB    47

# Runtime self-status (is the daemon healthy?)
$ crictl info
{
  "status": {
    "conditions": [
      {"type": "RuntimeReady", "status": true, "reason": "", "message": ""},
      {"type": "NetworkReady", "status": true, "reason": "", "message": ""}
    ]
  },
  "config": {
    "containerd": {...},
    "registry": {...}
  }
}
```

When a pod is stuck `ContainerCreating`, `crictl pods` will show the sandbox state. If `NotReady`, look at `crictl pods -v` or check `journalctl -u containerd` for errors. If sandbox is `Ready` but no containers exist, image pull is the most common culprit — `crictl images` to check, `crictl pull <image>` to retry manually.

### 12.2 ctr

`ctr` is containerd's native CLI, intended for containerd developers. It speaks the *full* containerd gRPC, not CRI. It's powerful but does not see what kubelet sees by default — kubelet uses the `k8s.io` containerd namespace, while `ctr` defaults to `default`. You must pass `-n k8s.io` to operate in the same namespace as kubelet:

```
$ ctr -n k8s.io images list
REF                                  TYPE    DIGEST     SIZE     PLATFORMS    LABELS
docker.io/library/nginx:1.27          ...     sha256:..  ...      linux/amd64  -
registry.k8s.io/pause:3.9             ...     sha256:..  ...      linux/amd64  -

$ ctr -n k8s.io containers list
CONTAINER     IMAGE                          RUNTIME
1a2b3c4d...   docker.io/library/nginx:1.27   io.containerd.runc.v2

$ ctr -n k8s.io tasks list
TASK          PID      STATUS
1a2b3c4d...   14523    RUNNING

# Get the OCI config.json for a container
$ ctr -n k8s.io containers info 1a2b3c4d... | jq .Spec
{
  "ociVersion": "1.0.2-dev",
  "process": {...},
  "root": {"path": "rootfs"},
  ...
}

# Run a one-shot container (ad-hoc, outside kubelet)
$ ctr -n default run --rm -t docker.io/library/alpine:latest alpine sh
/ # uname -r
6.6.32
```

`ctr` is useful for: debugging containerd itself (when CRI says "everything's fine" but pods aren't starting), pre-pulling images into the kubelet namespace before a node is added to a cluster, low-level inspection of container metadata.

### 12.3 runc

`runc` is the bottom layer. It lists containers it has direct knowledge of — these are *all* containers using runc as the OCI runtime, including ones launched by containerd via shim. Listing must specify the runc state directory:

```
$ runc --root /run/containerd/runc/k8s.io list
ID                                                                   PID      STATUS     BUNDLE  ...
1a2b3c4d5e6f7a8b9c0d1e2f3g4h5i6j7k8l9m0n1o2p3q4r5s6t7u8v9w0x1y2z   14523    running    /run/containerd/io.containerd.runtime.v2.task/k8s.io/1a2b3c4d.../
...

$ runc --root /run/containerd/runc/k8s.io state 1a2b3c4d...
{
  "ociVersion": "1.0.2-dev",
  "id": "1a2b3c4d...",
  "pid": 14523,
  "status": "running",
  "bundle": "/run/containerd/io.containerd.runtime.v2.task/k8s.io/1a2b3c4d...",
  "rootfs": "/run/containerd/io.containerd.runtime.v2.task/k8s.io/1a2b3c4d.../rootfs",
  "created": "2025-01-15T10:23:45.678901234Z",
  "owner": ""
}

# Send a signal directly (skipping all higher layers)
$ runc --root /run/containerd/runc/k8s.io kill 1a2b3c4d... SIGTERM

# Inspect cgroup membership and resource usage
$ runc --root /run/containerd/runc/k8s.io ps 1a2b3c4d...
UID     PID    PPID   C    STIME   TTY     TIME      CMD
0       14523  14521  0    10:23   ?       00:00:01  nginx: master process nginx -g daemon off;
101     14598  14523  0    10:23   ?       00:00:00  nginx: worker process
```

`runc` is the tool when you need to verify that the OCI runtime itself sees the container, when the higher layers report inconsistent state, or to study what runc would do for a given config.json (use `runc spec` to generate a default).

### 12.4 nsenter

To enter a running container's namespaces from outside (without going through `kubectl exec`/`crictl exec`, which sometimes hang), use `nsenter` with the container's main PID:

```
# Find the container's main PID
$ crictl inspect 1a2b3c4d... | jq .info.pid
14523

# Enter all its namespaces and run a command
$ sudo nsenter -t 14523 -n -u -i -p -m -- /bin/sh
# Now inside the container's namespaces:
/ # hostname
web
/ # ip addr show eth0
2: eth0@if16: <BROADCAST,MULTICAST,UP,LOWER_UP> ...
    inet 10.244.2.17/24 scope global eth0
/ # cat /proc/1/cmdline
/usr/bin/nginx-g daemon off;
```

Flag map: `-n` net, `-u` uts, `-i` ipc, `-p` pid, `-m` mount, `-U` user, `-C` cgroup, `-T` time. `-t <pid>` is the target process.

This is invaluable when the container has no shell installed (distroless images), or when its shell is broken, or when the runtime itself is wedged. You access the namespaces directly from the host's `/proc/<pid>/ns/` symlinks.

### 12.5 Diagnosing Stuck Containers

A grab-bag of real failures and how to diagnose:

**Symptom: pod stuck in `ContainerCreating` for minutes**
- `crictl pods <pod-uid>` → sandbox state.
- If sandbox `NotReady`: `journalctl -u containerd -n 100 --no-pager` for CNI errors, image pull failures.
- If sandbox `Ready` but no containers: image pull. `crictl pull <image>` to force a fresh attempt with verbose output.

**Symptom: zombie shim — pod gone, but `containerd-shim-runc-v2` still running**
- `ps auxf | grep shim` → list of shims.
- `crictl ps -a | grep <id>` → does CRI know about it?
- If not: the shim has lost its connection back to containerd. `kill <shim-pid>` is safe; it just kills its dead container.
- Root cause is usually a containerd crash that left an inconsistent state, or an OOM-killed shim that couldn't notify containerd before dying.

**Symptom: `kubectl exec` hangs forever**
- `crictl exec` likely also hangs.
- Streaming server is wedged. Look at containerd's logs for "streaming server" errors.
- Check shim health: `cat /run/containerd/io.containerd.runtime.v2.task/k8s.io/<id>/log.json`.
- Restart containerd if necessary (workloads survive, thanks to the shim).

**Symptom: high IOPS on `/var/lib/containerd/`**
- snapshotter activity. Run `ctr -n k8s.io snapshots list` to see counts; if thousands, kubelet may not be GC'ing.
- Check `imageGCHighThresholdPercent` and image disk usage with `crictl info` and `df -h /var/lib/containerd`.

**Symptom: containers can't start, `failed to mount overlayfs` errors**
- Usually inode exhaustion on `/var/lib/containerd`'s filesystem.
- `df -i /var/lib/containerd` to confirm.
- Aggressive image GC, or move containerd to a larger filesystem.

---

## 13. Pitfalls and Common Failures

A non-exhaustive but real-world list. Most of these have caused production incidents.

### 13.1 dockershim Is Dead

Kubernetes 1.20 deprecated dockershim, the in-tree shim that let kubelet talk to Docker. Kubernetes 1.24 removed it. As of 1.24+, **Docker Engine is not a supported container runtime for Kubernetes** — you must use containerd, CRI-O, or another CRI implementation.

What broke (and what didn't):
- Existing images continue to work — Docker images *are* OCI images.
- `docker build`-built images work via any CRI runtime.
- Anything that called the Docker daemon socket (`/var/run/docker.sock`) from within pods (Docker-in-Docker for CI) broke; the socket isn't there anymore. Replacements: Kaniko, BuildKit-rootless, Buildah, or just mount the containerd socket if you must (security implications).
- Mirantis maintains `cri-dockerd` as a third-party CRI shim around Docker, for those who really cannot migrate.

Many monitoring tools (cAdvisor, log shippers) had hard-coded Docker assumptions. Most are now CRI-aware; check yours.

### 13.2 Direct runc Is Not Container Management

A frequent misunderstanding: "I can run `runc create` myself, why do I need containerd?" Because runc only does the OCI runtime spec — namespaces, cgroups, exec. It doesn't:
- Pull images. You must hand it a bundle (rootfs + config.json) you assembled yourself.
- Manage state across restarts. Your management of `runc` is your problem.
- Talk to CNI. You'd need to invoke CNI binaries directly with the right environment.
- Provide a gRPC API. Anything that wants to drive runc programmatically needs a process tree of its own.

containerd (or CRI-O, or Docker, or Podman) is the *minimum* needed to actually orchestrate containers in production. runc alone is a primitive.

### 13.3 Shim v1 vs Shim v2 (Mostly Historic, but Surfaces in Old Configs)

If you find configs referencing `io.containerd.runtime.v1.linux` or `io.containerd.runc.v1`, they're for the old shim v1 architecture (pre-containerd 1.2). The modern runtime type is `io.containerd.runc.v2`. Symptoms of leftover v1 config: containerd refuses to start a container with an obscure plugin-not-found error. Fix by updating `/etc/containerd/config.toml`:

```toml
[plugins."io.containerd.grpc.v1.cri".containerd]
  default_runtime_name = "runc"

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc]
  runtime_type = "io.containerd.runc.v2"
  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]
    SystemdCgroup = true   # match kubelet's --cgroup-driver
```

### 13.4 SystemdCgroup Mismatch

If kubelet uses `--cgroup-driver=systemd` but containerd's runc options have `SystemdCgroup = false` (or vice versa), pods will start with cgroup paths in places kubelet doesn't expect. Symptoms: `kubectl top pod` shows nothing, eviction misfires, CPU pinning is wrong. Both must match. Modern defaults are systemd everywhere; older installs might be cgroupfs.

### 13.5 Snapshotter Inode Exhaustion

overlayfs creates a lot of inodes on the underlying filesystem. A node with hundreds of pods, each from a Java image with tens of thousands of `.class` files, easily blows through default ext4 inode counts. The disk shows 40% free but `df -i` shows 100% used. Containers cannot start: "no space left on device" even though there is space.

Mitigations:
- Format `/var/lib/containerd` with more inodes (`mkfs.ext4 -i 4096 -N <count>`), or use xfs which dynamically allocates inodes.
- Use slimmer base images (distroless, alpine).
- Move containerd to its own filesystem so inode pressure doesn't kill the host.
- Use a snapshotter that's less inode-heavy (stargz/SOCI lazy pull skips files until accessed).

### 13.6 Architecture Mismatch

Pulling a `linux/amd64` image on an arm64 node either fails or, worse, *succeeds because the image manifest list defaults to the requested platform* and then the runtime can't exec the binary because of "exec format error". On Apple Silicon laptops with Docker Desktop, this is benign because qemu-static is transparently used; on production arm64 nodes, you must build multi-arch images.

Building multi-arch:
- `docker buildx build --platform linux/amd64,linux/arm64 -t myimage:tag --push .`
- The pushed manifest is an **image index** with one manifest per platform; the runtime picks the right one for the node.

Check what platforms an image supports: `crane manifest <image> | jq .manifests[].platform`.

### 13.7 Registry Credentials in Runtime vs Kubelet

Pulling images requires credentials. There are several places to put them, and they are not equivalent.

| Mechanism | Lives in | Used when |
|---|---|---|
| `imagePullSecrets` on Pod or ServiceAccount | Kubernetes Secret | kubelet reads, passes to runtime via `PullImage(AuthConfig)` |
| containerd `registry.configs.<host>.auth` in config.toml | containerd config file on node | Always, for any pull of that registry |
| Kubelet credential providers (`credentialprovider.kubelet.k8s.io`) | kubelet plugin binary on node | kubelet invokes plugin per pull, plugin returns short-lived token |
| Static config file at `/var/lib/kubelet/config.json` | Kubelet on node (legacy) | Fallback if other mechanisms don't apply |

Common confusions:
- Setting `imagePullSecrets` does not give the container access to the registry at runtime — only kubelet uses it, and only at pull time.
- Setting credentials in containerd's `config.toml` works for all pulls but bypasses Kubernetes' per-namespace secret model — single shared credential.
- Credential providers (ECR, GCR, Azure) are the production answer for cloud registries: kubelet fetches a fresh IAM-derived token per pull, no long-lived credentials anywhere.

### 13.8 Forgetting About the Pod Sandbox in Cleanup

Manually deleting all containers via `crictl rm` does *not* delete the pod sandbox. The sandbox holds the pod's IP, network namespace, and shows up in `crictl pods` as `Ready` with no containers. Kubelet will not naturally recover this state if it didn't initiate the cleanup. To fully clean up: `crictl stopp <sandbox> && crictl rmp <sandbox>`. (Mnemonic: `stopp`/`rmp` are pod-sandbox variants; `stop`/`rm` are container variants.)

### 13.9 Custom OCI Runtimes Need the Right Config

If you set `runtimeClassName: gvisor` on a pod and get `RunPodSandbox: runtime "runsc" not found`, containerd doesn't know about the runtime. You must register it in `/etc/containerd/config.toml`:

```toml
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc.options]
    TypeUrl = "io.containerd.runsc.v1.options"
    ConfigPath = "/etc/containerd/runsc.toml"
```

And install `containerd-shim-runsc-v1` in `$PATH`. The RuntimeClass `handler` field must match the runtime name. Then `kubectl get runtimeclass` confirms; pods can use it.

### 13.10 The "Image Pull Backoff" That Isn't an Image Problem

A pod stuck in `ImagePullBackOff` can be many things:
- Network: DNS resolution of the registry hostname fails. Check `crictl pods` events.
- Auth: 401/403 from the registry. Check kubelet logs for the actual response.
- Rate limiting: Docker Hub anonymously throttles to 100 pulls per 6 hours per IP. A NAT'd cluster can hit this fast. Symptoms: random pulls fail with `TOOMANYREQUESTS`. Fix: configure an authenticated pull secret or use a mirror.
- TLS: registry cert not trusted. Self-signed registries need `registry.configs.<host>.tls.insecure_skip_verify_cert = true` or proper CA bundle.
- Disk full on the node: pull fails with "no space left" but kubelet reports it as ImagePullBackOff.

The remedy is always `crictl pull <image>` to get the actual error rather than the kubelet-laundered version.

---

### 13.11 conmon vs Shim Differences in Practice

Operationally, CRI-O's conmon and containerd's shim-v2 do the same thing, but the implementations diverge in two ways that surface in debugging:

- **conmon writes a single log per container at `/var/log/pods/...`** directly; the containerd shim writes via a fifo that the CRI plugin then forwards. If logs are missing, the failure point differs: with conmon, check the log path's permissions and free space; with containerd, check the CRI plugin's log-writing code path (and that the log directory exists with the right mode).
- **Per-pod resource use**: a containerd-shim-runc-v2 process is ~10-15 MB RSS per task (sandbox + each container); a conmon is ~1-3 MB RSS. On a node with 100 pods × 3 containers, that's ~4 GB vs ~1 GB just for the shim layer. CRI-O fans use this as a sales point; for most clusters it doesn't matter, but on memory-constrained edge nodes (chapter 33) it can.

Either way, the **mental model is the same**: a tiny process per workload, owning the workload's stdio and lifecycle, capable of surviving the daemon above it.

### 13.12 Don't Confuse Container Runtime Logs and Workload Logs

A pod's "logs" as seen by `kubectl logs` are the *workload's* stdout/stderr, captured by the shim/conmon and written to `/var/log/pods/.../<container>/<attempt>.log`. The container runtime itself (containerd, CRI-O) does not write into that file; its own logs go to journald. When you `kubectl logs` and see nothing, the question is: did the workload write anything? Often the workload crashed before writing, in which case its logs are empty and you need the runtime's logs (journalctl) to see *why* runc rejected the container.

Common case: a container with the wrong image architecture exits with "exec format error" from runc. `kubectl logs` is empty; `kubectl describe pod` shows the exit code 1; the diagnostic message is in `journalctl -u containerd` or in the shim log under `/run/containerd/io.containerd.runtime.v2.task/.../log.json`.

### 13.13 Log Rotation Is the Runtime's Job (Sort Of)

Container logs grow unbounded by default. Kubelet has built-in log rotation since 1.21 (`containerLogMaxSize`, `containerLogMaxFiles` in KubeletConfiguration, default 10 MB / 5 files), but it operates at the file-rename level: it renames `0.log` to `0.log.1` and asks the runtime to reopen the log file via `ReopenContainerLog` so new writes go to a fresh `0.log`. If a runtime doesn't implement `ReopenContainerLog` correctly, log rotation breaks and disks fill silently. Both containerd and CRI-O implement it; custom CRI implementations sometimes don't.

For high-volume logs, you typically want a log shipper DaemonSet (Fluent Bit, Vector, Promtail) reading from `/var/log/pods/`, not relying on `kubectl logs`. The DaemonSet must handle rotation itself (most do via inotify).

---

## 14. TL;DR

Kubernetes does not run containers; it tells a **container runtime** to. The three layers below kubelet — the **CRI** gRPC contract (RuntimeService + ImageService), the **container runtime** (containerd or CRI-O, with its content store, snapshotter, and per-container shim), and the **OCI runtime** (runc/crun/youki for normal pods, runsc or kata-runtime for sandboxed pods) — are each a separate replaceable specification, and their boundaries are the boundaries of the most confusing class of Kubernetes failures. **Pods become real** when kubelet calls `RunPodSandbox` (which spawns a pause container holding the pod's net/ipc/uts namespaces and gets the network wired up via CNI), then `CreateContainer` + `StartContainer` for each container in turn, which the runtime translates into an OCI bundle (config.json + rootfs) handed to runc; runc performs the namespace-creation dance with its C `nsexec` and Go `init` halves, applies cgroups, capabilities, seccomp, AppArmor, then `pivot_root`s and `execve`s the entrypoint. **dockershim is dead** as of 1.24 — containerd and CRI-O are the supported runtimes. **RuntimeClass** is how you select gVisor or Kata for stronger isolation. **crictl** is the tool to use when kubectl isn't enough; **ctr** for native containerd; **runc** for the bottom of the stack; **nsenter** for getting into a container's namespaces directly. Most production runtime pain is one of: image GC misconfigured, snapshotter inode exhaustion, registry auth in the wrong place, or a wedged shim from a stale containerd restart. Once you can name every box in the diagram at the top of §1, every chapter that says "the runtime starts a container" stops being a black box and starts being a known set of gRPC calls translating into a known set of syscalls.
