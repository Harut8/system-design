# Linux Primitives for Containers

How the Linux kernel actually implements containers. This document covers the eight namespace types, the cgroup v2 unified hierarchy, capabilities, seccomp-bpf, AppArmor and SELinux, OverlayFS, the pod networking dataplane (veth, bridge, VXLAN, netfilter), and just enough eBPF to make later chapters land. A Kubernetes Pod is a thin orchestration concept wrapped around these primitives — if you cannot read `/proc/$pid/ns/` or `/sys/fs/cgroup/` and explain what every file does, you cannot debug Kubernetes at the layer where things actually go wrong.

---

## Table of Contents

1. [The Big Picture: A Container Is Just a Process](#1-the-big-picture-a-container-is-just-a-process)
2. [Namespaces: The Eight Restricted Views](#2-namespaces-the-eight-restricted-views)
3. [Building a Container by Hand with `unshare` and `nsenter`](#3-building-a-container-by-hand-with-unshare-and-nsenter)
4. [cgroups v1 vs v2: Bounded Resources](#4-cgroups-v1-vs-v2-bounded-resources)
5. [How Kubernetes Lays Out cgroups](#5-how-kubernetes-lays-out-cgroups)
6. [Capabilities: Splitting Root into 41 Pieces](#6-capabilities-splitting-root-into-41-pieces)
7. [seccomp-bpf: Per-Syscall Filtering](#7-seccomp-bpf-per-syscall-filtering)
8. [LSMs: AppArmor and SELinux](#8-lsms-apparmor-and-selinux)
9. [Filesystem Isolation: OverlayFS](#9-filesystem-isolation-overlayfs)
10. [Networking Primitives: veth, Bridges, VXLAN, netfilter](#10-networking-primitives-veth-bridges-vxlan-netfilter)
11. [eBPF: Hook Points, Maps, Verifier, CO-RE](#11-ebpf-hook-points-maps-verifier-co-re)
12. [Putting It All Together: What `docker run` Actually Does](#12-putting-it-all-together-what-docker-run-actually-does)
13. [Pitfalls](#13-pitfalls)
14. [TL;DR](#14-tldr)

---

## 1. The Big Picture: A Container Is Just a Process

There is no `container` system call. There is no kernel object named `struct container`. A "container" is a colloquial name for a Linux process (or set of processes) that the kernel has been asked to lie to about three things and constrain in two more:

```
                ┌─────────────────────────────────────────────┐
                │   "Container" = ordinary task_struct        │
                │                                              │
                │   Restricted views (the lies):              │
                │     1. Namespaces  (pid, net, mnt, uts,     │
                │                     ipc, user, cgroup, time)│
                │     2. Mount tree   (chroot-equivalent via  │
                │                     pivot_root + mnt ns)    │
                │     3. /proc, /sys  (procfs/sysfs reflect   │
                │                     only this view)          │
                │                                              │
                │   Bounded resources (the constraints):      │
                │     4. cgroup v2    (cpu, memory, io, pids) │
                │     5. rlimit       (legacy per-process)    │
                │                                              │
                │   Reduced authority (defence in depth):     │
                │     6. Capabilities  (subset of root)        │
                │     7. seccomp-bpf   (syscall allowlist)    │
                │     8. LSM profile   (AppArmor/SELinux)     │
                │     9. no_new_privs  (suid bypass blocked)  │
                └─────────────────────────────────────────────┘

                          ┌───────────────┐
                          │  task_struct  │
                          │ (one per      │
                          │  thread)      │
                          └───────┬───────┘
                                  │
        ┌────────────┬────────────┼────────────┬────────────┐
        │            │            │            │            │
        ▼            ▼            ▼            ▼            ▼
  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐
  │ nsproxy │  │  cred   │  │  fs     │  │ files   │  │ cgroups │
  │  (8 ns) │  │ (caps,  │  │ (root,  │  │ (fd     │  │ (v2     │
  │         │  │  uid,   │  │  pwd,   │  │  table) │  │  node)  │
  │         │  │  seccomp│  │  umask) │  │         │  │         │
  └─────────┘  └─────────┘  └─────────┘  └─────────┘  └─────────┘
```

In `linux/include/linux/sched.h` the per-task pointers are literally:

```c
struct task_struct {
    /* ... */
    struct nsproxy        *nsproxy;     /* the 7 ns; user ns lives in cred */
    const struct cred     *cred;        /* uid/gid + capabilities + user_ns */
    struct seccomp         seccomp;     /* BPF filter chain */
    struct fs_struct      *fs;          /* root + cwd (mount-ns-relative) */
    struct files_struct   *files;       /* open fds */
    struct css_set        *cgroups;     /* cgroup membership */
    /* ... */
};
```

Two consequences fall out of this design that every Kubernetes engineer hits eventually:

1. **A container can be split across namespaces.** A process can be in your PID namespace but the host network namespace (this is exactly what `hostNetwork: true` Pods do). Or in a new mount namespace but sharing the host PID namespace (debug containers). The "container" abstraction is a *bundle of choices*, not a single switch.

2. **Containers are not VMs.** There is exactly one Linux kernel. Every "container" shares the same scheduler, the same page cache, the same network stack — they just see different slices of it. A kernel bug that escapes any container escapes onto the host. This is why ch 29 (gVisor, Kata, Confidential Containers) exists: when the kernel boundary is not strong enough, you wrap an actual VM.

The rest of this chapter walks each box in the diagram above, top to bottom.

---

## 2. Namespaces: The Eight Restricted Views

A namespace wraps a global kernel resource so that processes inside the namespace see a private instance of it. As of Linux 5.6 there are eight namespace types, identified in `linux/include/uapi/linux/sched.h` by `CLONE_NEW*` flags:

| Namespace | `CLONE_NEW*` | Introduced | What it isolates |
|---|---|---|---|
| mnt | `CLONE_NEWNS` | 2.4.19 (2002) | Mount points (the filesystem tree) |
| uts | `CLONE_NEWUTS` | 2.6.19 (2006) | hostname, NIS domain name (uname()) |
| ipc | `CLONE_NEWIPC` | 2.6.19 (2006) | SysV IPC, POSIX message queues |
| pid | `CLONE_NEWPID` | 2.6.24 (2008) | Process IDs (PID 1 inside is a child outside) |
| net | `CLONE_NEWNET` | 2.6.29 (2009) | Network devices, stacks, ports, routes, sockets |
| user | `CLONE_NEWUSER` | 3.8 (2013) | UIDs/GIDs and capabilities |
| cgroup | `CLONE_NEWCGROUP` | 4.6 (2016) | cgroup root visible in `/proc/$pid/cgroup` |
| time | `CLONE_NEWTIME` | 5.6 (2020) | CLOCK_MONOTONIC and CLOCK_BOOTTIME offsets |

Every namespace is a kernel object with a 64-bit inode number, accessible via `/proc/$pid/ns/*`:

```
$ ls -l /proc/self/ns/
lrwxrwxrwx 1 root root 0  cgroup -> 'cgroup:[4026531835]'
lrwxrwxrwx 1 root root 0  ipc    -> 'ipc:[4026531839]'
lrwxrwxrwx 1 root root 0  mnt    -> 'mnt:[4026531840]'
lrwxrwxrwx 1 root root 0  net    -> 'net:[4026531992]'
lrwxrwxrwx 1 root root 0  pid    -> 'pid:[4026531836]'
lrwxrwxrwx 1 root root 0  pid_for_children -> 'pid:[4026531836]'
lrwxrwxrwx 1 root root 0  time   -> 'time:[4026531834]'
lrwxrwxrwx 1 root root 0  time_for_children -> 'time:[4026531834]'
lrwxrwxrwx 1 root root 0  user   -> 'user:[4026531837]'
lrwxrwxrwx 1 root root 0  uts    -> 'uts:[4026531838]'
```

**Two processes are in the same namespace iff the magic links point at the same inode.** This is the canonical way to test namespace membership from userspace; `readlink /proc/$pid/ns/net | cmp /proc/$$/ns/net` answers "is this container sharing my network namespace?". The `_for_children` variants (pid_for_children, time_for_children) record the namespace that `clone()` will place the child in — distinct from the calling task's own namespace because pid and time can only be entered for a new child, never for the current task.

### The Three Syscalls That Touch Namespaces

```
clone(2) / clone3(2)   ─── Create child + (optionally) new namespaces
                            CLONE_NEWPID | CLONE_NEWNET | ...
                            Used by container runtimes and the kernel for fork().

unshare(2)             ─── Move CURRENT process into new namespaces
                            unshare(CLONE_NEWNS) → I now have a private mnt ns
                            (PID ns is special: only affects future children)

setns(2)               ─── Enter an EXISTING namespace via fd to /proc/$pid/ns/X
                            Used by `nsenter`, by CNI plugins, by debug containers.
```

A typical container runtime uses all three: `clone3()` with a flag bundle to spawn the container's init process, the child calls `unshare()` for namespaces that cannot be created at clone time (notably new mount propagation behavior), and tools like CNI plugins or `kubectl exec` use `setns()` to enter an existing container's namespaces.

The C signature of `clone3` is illuminating because it is the actual API container runtimes invoke:

```c
struct clone_args {
    __aligned_u64 flags;        /* CLONE_NEW* | CLONE_PIDFD | ... */
    __aligned_u64 pidfd;        /* out: PID-file descriptor */
    __aligned_u64 child_tid;    /* out: TID in child memory */
    __aligned_u64 parent_tid;   /* out: TID in parent memory */
    __aligned_u64 exit_signal;  /* signal sent to parent on exit (SIGCHLD) */
    __aligned_u64 stack;        /* child stack base */
    __aligned_u64 stack_size;
    __aligned_u64 tls;
    __aligned_u64 set_tid;      /* request specific PID (containers!) */
    __aligned_u64 set_tid_size;
    __aligned_u64 cgroup;       /* fd to target cgroup (CLONE_INTO_CGROUP) */
};

long pid = syscall(SYS_clone3, &args, sizeof args);
```

`set_tid` lets containerd or runc request that the container's init be PID 1 inside its new PID namespace — without it the kernel just assigns the next free PID, which works but feels accidental. `CLONE_INTO_CGROUP` (Linux 5.7+) lets the runtime atomically place the new task into a target cgroup at creation time, eliminating a race window where the process briefly belongs to the parent's cgroup before being migrated.

### 2.1 PID Namespace

A new PID namespace creates a private PID numbering. The first process in a new PID ns becomes **PID 1 inside**; the kernel synthesizes this — outside, the kernel still has a regular PID for it.

```
Host PID namespace:                  New PID namespace (created by clone CLONE_NEWPID):
┌──────────────────────────────┐    ┌──────────────────────────┐
│ PID 1   systemd              │    │ PID 1   container init   │
│ PID 9   sshd                 │    │ PID 2   bash             │
│ PID 12  bash                 │    │ PID 5   nginx            │
│ ...                          │    │ PID 6   nginx worker     │
│ PID 8421  container init     │◄──►│                          │
│ PID 8422  bash               │◄──►│                          │
│ PID 8425  nginx              │◄──►│                          │
│ PID 8426  nginx worker       │◄──►│                          │
└──────────────────────────────┘    └──────────────────────────┘
        (sees everyone)                      (sees only itself)
```

**Asymmetry**: the host PID namespace sees *all* PIDs (translated). Container processes see only their own subtree. PID namespaces nest. `/proc/$pid/status` field `NSpid:` shows all PIDs the process has across namespaces from outermost to innermost:

```
NSpid:  8425   3
        ^^^^   ^
         host  container PID
```

**Two non-obvious rules:**

1. **PID 1 is special inside a PID ns.** It must reap zombies (or they accumulate), and it receives only signals it has registered handlers for (so an unhandled SIGTERM is *ignored*, not fatal). This is why `bash` as a container's PID 1 is a footgun — see §13.
2. **Killing PID 1 inside terminates the whole PID namespace.** The kernel then SIGKILLs every other process in the ns. This is how container runtimes implement "stop the container": send SIGTERM to PID 1, wait for `terminationGracePeriodSeconds`, then send SIGKILL.

A PID namespace cannot be entered by an existing process (`setns()` only sets `pid_for_children`); the actual PID ns is fixed at fork time. This is why `kubectl exec` works: it `setns()`'s into the target's pid_for_children, then forks — the child is born into the container's PID namespace, but the kubelet's exec helper process itself is not.

### 2.2 Network Namespace

The most heavyweight namespace. A new network namespace gets:

- An empty interface list (only `lo` exists, and even `lo` is DOWN initially)
- Its own routing table, neighbor table (ARP), conntrack table
- Its own netfilter rules (iptables/nftables chains)
- Its own socket table (TCP/UDP/UNIX/etc.) — sockets are *bound* to a netns
- Its own `/proc/net/` and `/sys/class/net/`
- Its own port number space (two containers can both bind 80 without conflict)

```
$ unshare --net /bin/bash
# ip link
1: lo: <LOOPBACK> mtu 65536 qdisc noop state DOWN
# # nothing else! no eth0, no nothing.
# ip route        # empty
# iptables -L     # default-empty chains
```

To get connectivity into a fresh netns you must create devices and wire them up. The standard recipe (which is exactly what every CNI plugin does internally) uses **veth pairs**: a virtual ethernet cable with two endpoints, where any packet on one endpoint appears on the other. See §10 for the full walkthrough.

### 2.3 Mount Namespace

The mount namespace owns the *mount tree*: where filesystems are attached, with what flags. Crucially, it does **not** isolate the filesystems themselves — ext4 on `/dev/sda1` is the same ext4 regardless of who mounts it. The namespace just controls the visible mount tree.

A new mount namespace starts as a *copy* of its parent's mount tree (clone-on-write). Subsequent mounts in the new ns are private unless the mount has propagation type **shared** (`MS_SHARED`) — a Linux concept layered on top of namespaces that decides whether a mount event in one ns propagates to peer namespaces. Container runtimes default to `MS_PRIVATE` on the root, which is why `kubelet`'s extensive mount activity does not leak into Pod containers.

The interaction of mount namespaces with `pivot_root(2)` is what gives containers their isolated filesystem view:

```
Container runtime startup (simplified):
  1. unshare(CLONE_NEWNS)           # private mount tree
  2. mount("/", "/", MS_PRIVATE)    # detach from host propagation
  3. mount(image_rootfs, "/mnt/rootfs", ...)
  4. chdir("/mnt/rootfs")
  5. pivot_root(".", "old_root")    # swap roots: new is /, old is /old_root
  6. umount2("/old_root", MNT_DETACH)
  7. execve(container_entrypoint)
```

After step 7 the process has no way to reach the host filesystem from inside its mount namespace — there is simply no path. This is *not* a security boundary by itself (a malicious container with CAP_SYS_ADMIN can remount, escape) but combined with capability drops it is.

### 2.4 UTS Namespace

The lightest namespace: it isolates two strings, `nodename` (hostname) and `domainname`, returned by `uname(2)`/`gethostname(2)`. That's the entire feature.

```
$ unshare --uts /bin/bash
# hostname container-42
# hostname
container-42
# exit
$ hostname
my-laptop                # unchanged
```

This is why a Pod can set `spec.hostname: my-app` independently of the node it runs on.

### 2.5 IPC Namespace

Isolates SysV IPC (shared memory segments, semaphores, message queues — `ipcs(1)`) and POSIX message queues (`mq_open(3)`). Two processes in different IPC namespaces cannot see each other's `shmget()` segments, even if they could deduce the same key.

Mostly relevant for legacy enterprise software (Oracle, SAP, JVMs with `-XX:+UseLargePages` doing SHM tricks) that still uses SysV SHM. Modern Kubernetes Pods rarely care, except that all containers in a Pod share IPC namespace by default (so a sidecar can talk to the main container via shared memory).

### 2.6 User Namespace

The most powerful and most complicated namespace. A user namespace maps UIDs/GIDs between inside and outside, and it owns capabilities — a process can be UID 0 (root) inside a user namespace while being an unprivileged user outside.

```
Host (parent user ns)            New user ns
┌────────────────────────┐       ┌────────────────────────┐
│ UID 100000  alice      │◄─────►│ UID 0       (root)     │
│ UID 100001  alice+1    │◄─────►│ UID 1                  │
│ UID 100002  alice+2    │◄─────►│ UID 2                  │
│ ...                    │       │ ...                    │
│ UID 165535  alice+65k  │◄─────►│ UID 65535              │
└────────────────────────┘       └────────────────────────┘
                                          ▲
                       Capabilities inside this ns are full:
                       CAP_SYS_ADMIN, CAP_NET_RAW, ... ALL of them,
                       but they ONLY apply to resources OWNED BY this user ns.
                       Trying to chown a file owned by UID 100000 fails,
                       because UID 100000 outside is not owned by this user ns.
```

Mappings are written to `/proc/$pid/uid_map` and `/proc/$pid/gid_map`, format `inside-id outside-id length`:

```
$ cat /proc/self/uid_map
         0     100000      65536
# means: inside UID 0 .. 65535 maps to outside UID 100000 .. 165535
```

User namespaces are how **rootless containers** work (podman, BuildKit, increasingly Kubernetes via `runAsUser` + the user namespace alpha/beta feature). The container believes it is running as root, can do "root things" like binding to privileged ports inside its own netns, but on the host it is an unprivileged user — kernel exploits via container escape are much harder because the escaping process has no real privileges.

User namespaces also break a long-standing assumption: capabilities are no longer global. A capability is meaningful only *relative to the user namespace that owns the resource*. CAP_NET_ADMIN inside a child user ns lets you reconfigure interfaces in netns'es owned by that user ns; it does nothing to host interfaces.

Two production gotchas with user namespaces:
- **Filesystem UID mapping**: a file owned by UID 100000 on disk appears as UID 0 inside the container. Conversely, a file the container creates as "UID 0" lands on disk as UID 100000. Tooling that doesn't understand this (backup tools, host inspections) gets confused. The Linux 5.12 idmapped mounts feature (`mount --bind -o idmap=`) makes this much cleaner but isn't yet universally adopted.
- **Subuid/subgid allocation**: the host must have a delegated UID range per "container user". `/etc/subuid` and `/etc/subgid` list these. Kubernetes' UserNamespaces support (KEP-127) allocates these ranges per Pod automatically; without it, you do it by hand.

### 2.7 cgroup Namespace

This one only isolates *the view* of the cgroup hierarchy, not membership in it. Without a cgroup ns, a process in `/kubepods.slice/.../pod-abc/container-xyz` sees that full path when reading `/proc/$pid/cgroup`. With a cgroup ns, it sees `/` as the root of *its own* cgroup, hiding the parent path.

```
Without cgroup ns:                    With cgroup ns (and bind-mount of cgroupfs):
$ cat /proc/self/cgroup               $ cat /proc/self/cgroup
0::/kubepods.slice/                   0::/
   kubepods-burstable.slice/
   kubepods-burstable-podabc.slice/
   cri-containerd-xyz.scope
```

Why this matters: software that wants to manage its *own* cgroup (e.g., a JVM that reads its memory limit from `memory.max`, or systemd inside a container) gets confused by the full host path. The cgroup namespace lets the container see a relative view.

### 2.8 Time Namespace

The newest (Linux 5.6, 2020). Lets you change the offsets for `CLOCK_MONOTONIC` and `CLOCK_BOOTTIME` per namespace. You **cannot** change `CLOCK_REALTIME` (system time) — that remains global, because too much breaks if processes disagree about what time it is.

The intended use case is checkpoint-restore (CRIU) — when you restore a container after migration, you want `CLOCK_MONOTONIC` (which should be monotonic *forever* per the POSIX spec) to not jump backward. With a time namespace you can offset it to maintain the illusion of continuity.

In practice almost nothing uses this yet in production Kubernetes; it exists for CRIU and live migration scenarios.

---

## 3. Building a Container by Hand with `unshare` and `nsenter`

The fastest way to make namespaces feel concrete is to build a "container" with shell tools. No Docker, no runc, no Kubernetes — just kernel primitives.

### 3.1 The Minimum Viable Container

```
# Open one terminal — call it OUTSIDE.
# Note PID and namespaces:
outside$ echo $$
12345
outside$ ls -l /proc/self/ns/{pid,net,mnt,uts}
... pid:[4026531836]
... net:[4026531992]
... mnt:[4026531840]
... uts:[4026531838]

# Spawn a "container":
outside$ sudo unshare --fork --pid --net --mount --uts --ipc --cgroup \
                      --mount-proc /bin/bash

# We are now INSIDE.
inside# echo $$
1                               # PID 1 inside the new pid namespace!
inside# hostname container-demo
inside# hostname
container-demo
inside# ps -ef                  # only sees this bash + its children
UID  PID  PPID  C STIME TTY  TIME CMD
root   1     0  0 12:34 pts/0 0:00 /bin/bash
root   5     1  0 12:34 pts/0 0:00 ps -ef
inside# ip link                 # only lo, and it's DOWN
1: lo: <LOOPBACK> mtu 65536 qdisc noop state DOWN

# From OUTSIDE:
outside$ hostname               # unchanged
my-laptop
outside$ pidof bash             # the same bash, but as a host PID
12350
outside$ ls -l /proc/12350/ns/pid
... pid:[4026532455]            # DIFFERENT inode now!
```

Notes:
- `--mount-proc` is required because PID namespace alone does not isolate `/proc` — you have to re-mount procfs inside the new mount namespace so that `/proc/$pid/` reflects the new PID numbering. Without it, `ps` inside still shows host processes.
- `--fork` is required for PID namespace because the unshared process itself is not in the new PID ns (PID ns only applies to children). The forked child becomes PID 1 inside; `unshare` then `exec`s the requested command in that child.

### 3.2 Wiring Network with a veth Pair

The container above has *no* connectivity. To give it some:

```
# Still inside the container, with bash as PID 1.
# Note its host PID (from outside):
outside$ NSPID=12350

# Create a veth pair on the host:
outside$ sudo ip link add veth-host type veth peer name veth-cnt
outside$ sudo ip addr add 10.42.0.1/24 dev veth-host
outside$ sudo ip link set veth-host up

# Move one end into the container's net namespace:
outside$ sudo ip link set veth-cnt netns $NSPID

# Inside the container, configure it:
inside# ip link set lo up
inside# ip link set veth-cnt up
inside# ip addr add 10.42.0.2/24 dev veth-cnt
inside# ip route add default via 10.42.0.1

# Test:
inside# ping -c1 10.42.0.1
PING 10.42.0.1: 64 bytes from 10.42.0.1: time=0.05 ms

# For outbound internet, enable forwarding + masquerade on the host:
outside$ sudo sysctl -w net.ipv4.ip_forward=1
outside$ sudo iptables -t nat -A POSTROUTING -s 10.42.0.0/24 -o eth0 -j MASQUERADE
inside# ping -c1 8.8.8.8       # works
```

You just did exactly what every CNI plugin does. The only thing missing is bridge sharing (so multiple containers on this node can talk to each other) — see §10.

### 3.3 Entering an Existing "Container" with `nsenter`

```
outside$ sudo nsenter --target $NSPID --pid --net --mount --uts --ipc /bin/bash
inside-via-nsenter# ps -ef
UID  PID  PPID  C STIME TTY  TIME CMD
root   1     0  0 12:34 pts/0 0:00 /bin/bash
root   7     0  0 12:40 pts/1 0:00 /bin/bash    # us, but PID 7, no parent!
```

`nsenter` is `setns(2)` plus `execve(2)`. It's the foundation of `kubectl exec`, `docker exec`, `crictl exec`, and every container-debugging tool. The "no parent" oddity (`PPID 0` for the nsenter'd shell) happens because the parent process (sudo on the host) is in a different PID namespace and is therefore invisible to the new shell.

### 3.4 Adding cgroups

The container above is unbounded — it can spawn unlimited PIDs, eat all memory, hog all CPU. Bound it:

```
outside$ CG=/sys/fs/cgroup/demo
outside$ sudo mkdir $CG
outside$ echo "+cpu +memory +pids" | sudo tee /sys/fs/cgroup/cgroup.subtree_control
outside$ echo "100M"  | sudo tee $CG/memory.max
outside$ echo "100000 1000000" | sudo tee $CG/cpu.max     # 10% of one core
outside$ echo "50"    | sudo tee $CG/pids.max
outside$ echo $NSPID  | sudo tee $CG/cgroup.procs

# Now the container is bounded. Verify from inside:
inside# cat /sys/fs/cgroup/memory.max     # only works if cgroupfs is mounted inside
```

You now have a container that:
- has its own PID, net, mount, uts, ipc, cgroup namespaces
- can reach the host and the internet via a veth + NAT
- is bounded to 100 MB RAM, 10% of one CPU, 50 processes

This is roughly what `docker run --memory 100M --cpus 0.1 --pids-limit 50 alpine sh` does — minus capability drops, seccomp, AppArmor, and the OverlayFS rootfs, which we add later.

---

## 4. cgroups v1 vs v2: Bounded Resources

Control groups (cgroups) are the kernel mechanism for grouping processes and applying *resource controllers* to the group. There are two ABIs — v1 (originated ~2008) and v2 (the unified hierarchy, stable since 4.5, 2016). Most modern distros default to v2; Kubernetes 1.25+ requires v2 for many features (memory.high, PSI, swap accounting).

### 4.1 The v1 Tragedy: Multiple Hierarchies

cgroup v1 lets each controller live in its own hierarchy. You could put process P in cgroup `/a` for the memory controller and cgroup `/b` for the cpu controller. In theory this is flexible; in practice it created enough confusion (orphaned controllers, inconsistent grouping, no controller could safely depend on another) that v2 abandoned the model entirely.

```
cgroup v1 (multiple hierarchies, mounted separately):

/sys/fs/cgroup/
├── memory/        ← memory controller's tree
│   ├── kubepods/
│   └── system.slice/
├── cpu,cpuacct/   ← cpu controller's tree (DIFFERENT structure!)
│   ├── kubepods/
│   └── system.slice/
├── pids/
├── devices/
├── freezer/
├── blkio/
├── ...
```

cgroup v2 (single unified hierarchy):

```
/sys/fs/cgroup/                       ← root cgroup
├── cgroup.controllers               (which controllers are available here)
├── cgroup.subtree_control           (which controllers are ENABLED for children)
├── cgroup.procs                     (PIDs in this cgroup)
├── cpu.stat
├── memory.stat
├── kubepods.slice/
│   ├── cgroup.subtree_control       "+cpu +memory +pids +io"
│   ├── cgroup.procs
│   ├── kubepods-burstable.slice/
│   │   └── kubepods-burstable-pod123.slice/
│   │       ├── cri-containerd-abc.scope/        ← one cgroup per container
│   │       │   ├── cgroup.procs                 (the actual container PIDs)
│   │       │   ├── cpu.max
│   │       │   ├── memory.max
│   │       │   ├── memory.high
│   │       │   ├── pids.max
│   │       │   └── ...
│   │       └── cri-containerd-def.scope/
│   └── kubepods-besteffort.slice/
└── system.slice/                    ← systemd-managed services
```

The key rules of v2:
1. **One hierarchy.** A process is in exactly one cgroup. Period.
2. **Controllers are enabled top-down via `cgroup.subtree_control`.** A child cgroup can only use controllers its parent has enabled in `subtree_control`. This is the "no internal processes" rule — non-root cgroups with active controllers cannot themselves contain processes; they must delegate to leaves.
3. **Files are typed:**
    - `*.max` = hard limit (kill or throttle on hit)
    - `*.high` = soft throttling threshold (slowing target without OOM)
    - `*.low` = best-effort protection (don't reclaim from me unless others starve)
    - `*.min` = hard protection (never reclaim below this)
    - `*.weight` = relative share among siblings (cpu.weight = 1..10000, default 100)
    - `*.stat` = read-only counters
    - `*.events` = read-only event counters (e.g., oom_kill counter)
    - `*.pressure` = PSI (Pressure Stall Information) — % time stalled

### 4.2 Controllers You'll Touch

#### cpu

```
cpu.weight        100               # relative share among siblings (1-10000)
cpu.weight.nice   0                 # alternative units (nice value)
cpu.max           100000 100000     # quota period  (μs CPU per period)
                                    # "max 100000" = unlimited
cpu.stat          usage_usec ...
cpu.pressure      some avg10=0.00 ... full avg10=0.00 ...
```

The two relevant knobs Kubernetes turns:
- **Requests** become `cpu.weight` (proportional share when there is contention)
- **Limits** become `cpu.max` (CFS bandwidth — hard quota over a 100 ms period)

`cpu.max 50000 100000` means "50 ms CPU per 100 ms wall time" = 50% of one core. The 100 ms default period is short enough that 100 ms latency hiccups become routine when limits are set tightly — this is the throttling pathology that drives some shops to remove CPU limits entirely (see ch 21).

#### memory

```
memory.max        2147483648        # 2 GiB hard limit; exceed → OOM kill
memory.high       1879048192        # soft limit; over this triggers throttling
                                    # of allocations + aggressive reclaim
memory.low        524288000         # best-effort protection
memory.min        0                 # hard protection (never reclaim below)
memory.current    1234567890        # current usage
memory.swap.max   0                 # cap swap (0 = no swap allowed)
memory.events     low 0 high 0 max 0 oom 0 oom_kill 0
memory.stat       anon ... file ... kernel ... slab ... pgfault ... pgmajfault ...
memory.pressure   some avg10=0.00 avg60=0.00 avg300=0.00 ...
                  full avg10=0.00 avg60=0.00 avg300=0.00 ...
```

The crucial difference vs v1: cgroup v2 accounts both userspace and kernel memory (slab, network buffers, page tables) into `memory.current`. This is why migrating to v2 sometimes makes containers "use more memory" — they were always using it; v1 just didn't count it. Many Java/Go containers needed memory limit bumps after the migration.

#### io

```
io.max            8:0 rbps=1048576 wbps=1048576 riops=100 wiops=100
                  # device 8:0  read/write bytes-per-second and IOPS caps
io.weight         100               # blkio scheduler weight (BFQ)
io.stat           8:0 rbytes=... wbytes=... rios=... wios=...
io.pressure       some avg10=... full avg10=...
io.cost.qos       8:0 enable=1 ctrl=auto rpct=95.00 rlat=2000 wpct=95.00 wlat=2000
io.latency        8:0 target=50              # latency-based throttling (msec target)
```

The `io.latency` controller is the interesting one: instead of bandwidth caps, you specify a target latency, and the kernel throttles the *least-protected* cgroups when the device misses the target for protected cgroups. This is much closer to what database workloads actually want than bandwidth caps.

#### pids

```
pids.max          1024
pids.current      37
pids.events       max 0
```

A trivial but life-saving controller. Without `pids.max`, a fork bomb in one container can exhaust the global PID space and kill the entire node. Kubelet sets a default `--pod-max-pids=4096` per Pod.

#### hugetlb

```
hugetlb.2MB.max    1073741824       # 1 GiB worth of 2MB hugepages
hugetlb.2MB.current
hugetlb.2MB.events max 0
```

Used by databases (ch databases/00 §3) and HPC workloads.

#### misc / rdma / cpuset / freezer

`cpuset.cpus` and `cpuset.mems` pin a cgroup's processes to specific CPUs and NUMA nodes — exactly the mechanism Kubernetes' static CPU manager and topology manager (ch 10, ch 21) use to give Guaranteed pods exclusive cores.

### 4.3 Reading and Writing cgroup Files

Everything is a text file. To enable a controller for a child cgroup:

```
# Enable cpu+memory in /sys/fs/cgroup/foo/
sudo mkdir /sys/fs/cgroup/foo
echo "+cpu +memory +pids" | sudo tee /sys/fs/cgroup/cgroup.subtree_control
# Now /sys/fs/cgroup/foo/cpu.* and memory.* exist
sudo mkdir /sys/fs/cgroup/foo/child
echo $$ | sudo tee /sys/fs/cgroup/foo/child/cgroup.procs    # move current shell
```

Moving a PID into a cgroup writes the PID to `cgroup.procs`. To move a single thread, write to `cgroup.threads` (only legal in "threaded" cgroups). Reading the file lists current members. To delete a cgroup, `rmdir` it — but only after all processes have left and all child cgroups are removed.

### 4.4 Pressure Stall Information (PSI)

PSI files (`cpu.pressure`, `memory.pressure`, `io.pressure`) report how much wall time the cgroup spent stalled on that resource:

```
$ cat /sys/fs/cgroup/kubepods.slice/memory.pressure
some avg10=2.34 avg60=1.87 avg300=0.92 total=12345678
full avg10=0.45 avg60=0.34 avg300=0.12 total=2345678
```

- **some** = at least one task stalled
- **full** = all tasks stalled (the cgroup was effectively frozen on this resource)

This is the modern signal for "this workload is memory-pressured" or "this workload is IO-bound", much more useful than the legacy approach of polling `memory.current` against `memory.max`. Kubelet uses PSI for eviction decisions (ch 21).

---

## 5. How Kubernetes Lays Out cgroups

Kubelet drives the cgroup tree according to a configurable cgroup driver (`systemd` or `cgroupfs`; almost everyone uses `systemd` now). The result is a deterministic tree:

```
/sys/fs/cgroup/
├── kubepods.slice/                                              [root for K8s]
│   │
│   ├── kubepods.slice                                           Guaranteed pods
│   │   (yes, the same name — Guaranteed pods sit directly here)
│   │   limits: full node minus reservations
│   │
│   ├── kubepods-burstable.slice/                                Burstable QoS
│   │   ├── cgroup.subtree_control = "+cpu +memory +pids +io"
│   │   ├── memory.high = (best-effort: node memory * burstable share)
│   │   │
│   │   ├── kubepods-burstable-pod<UID>.slice/                   per-Pod cgroup
│   │   │   ├── cpu.weight   = sum(container.cpu.request) * 1024 / 1
│   │   │   ├── memory.max   = sum(container.memory.limit)  (if all set)
│   │   │   ├── pids.max     = configured pod-pids limit
│   │   │   │
│   │   │   ├── cri-containerd-<containerID>.scope/              per-container
│   │   │   │   ├── cgroup.procs   ← actual container PIDs live here
│   │   │   │   ├── cpu.weight     = container.cpu.request * 1024
│   │   │   │   ├── cpu.max        = container.cpu.limit (period 100ms)
│   │   │   │   ├── memory.max     = container.memory.limit
│   │   │   │   ├── memory.high    = container.memory.limit * 0.8 (heuristic)
│   │   │   │   ├── memory.swap.max= 0
│   │   │   │   └── ...
│   │   │   └── cri-containerd-<sidecarID>.scope/
│   │   │
│   │   └── kubepods-burstable-pod<otherUID>.slice/...
│   │
│   └── kubepods-besteffort.slice/                               BestEffort QoS
│       ├── cpu.weight = small (default 1, lowest)
│       ├── memory.max = max (unbounded; OOM-killed first)
│       └── ...
│
├── system.slice/                                                systemd services
│   ├── kubelet.service/
│   ├── containerd.service/
│   └── ...
│
└── user.slice/                                                  user sessions
```

The three QoS classes map to cgroups as follows:

| QoS | Conditions | cgroup placement | Behavior |
|---|---|---|---|
| **Guaranteed** | every container has CPU+memory request == limit | `kubepods.slice/kubepods-pod<UID>.slice/` (top level) | Highest priority, last to be evicted, can be pinned to exclusive cores |
| **Burstable** | at least one container has request, but not all request == limit | `kubepods-burstable.slice/kubepods-burstable-pod<UID>.slice/` | Medium priority |
| **BestEffort** | no requests or limits on any container | `kubepods-besteffort.slice/kubepods-besteffort-pod<UID>.slice/` | Lowest priority, first to be evicted |

When the node is under memory pressure, kubelet's eviction manager reads PSI and `memory.events` from these subtrees in priority order and SIGTERMs pods (gracefully) before the kernel OOM killer steps in. If the kernel beats kubelet to it, the OOM killer uses `oom_score_adj` (which kubelet sets per container based on QoS — BestEffort gets +1000, Burstable gets 1000-(1000*request/limit), Guaranteed gets -997) to pick a victim.

The cgroup path for a container is what shows up in `/proc/$pid/cgroup`:

```
$ cat /proc/12345/cgroup
0::/kubepods.slice/kubepods-burstable.slice/
   kubepods-burstable-pod3a1f...slice/
   cri-containerd-9b2e...scope
```

If you're debugging "why is this container being throttled", `cat /sys/fs/cgroup/<that path>/cpu.stat` shows the throttling counters:

```
nr_periods 1000
nr_throttled 47
throttled_usec 423000     # 423 ms of stalled time over the sample
```

A non-zero `nr_throttled` count means cpu.max was hit during the period. A high `throttled_usec` against a tight cpu.max is the smoking gun for the CPU-throttling pathology mentioned in ch 21.

---

## 6. Capabilities: Splitting Root into 41 Pieces

Historically, on Linux, you were either root (UID 0, can do anything) or not (can do almost nothing). Capabilities split "root" into 41 distinct privileges — `man capabilities(7)`. A process can have any subset.

The complete list (kernel 6.x; the number grows occasionally) includes:

```
CAP_AUDIT_CONTROL    CAP_AUDIT_READ        CAP_AUDIT_WRITE     CAP_BLOCK_SUSPEND
CAP_BPF              CAP_CHECKPOINT_RESTORE CAP_CHOWN          CAP_DAC_OVERRIDE
CAP_DAC_READ_SEARCH  CAP_FOWNER            CAP_FSETID          CAP_IPC_LOCK
CAP_IPC_OWNER        CAP_KILL              CAP_LEASE           CAP_LINUX_IMMUTABLE
CAP_MAC_ADMIN        CAP_MAC_OVERRIDE      CAP_MKNOD           CAP_NET_ADMIN
CAP_NET_BIND_SERVICE CAP_NET_BROADCAST     CAP_NET_RAW         CAP_PERFMON
CAP_SETFCAP          CAP_SETGID            CAP_SETPCAP         CAP_SETUID
CAP_SYS_ADMIN        CAP_SYS_BOOT          CAP_SYS_CHROOT      CAP_SYS_MODULE
CAP_SYS_NICE         CAP_SYS_PACCT         CAP_SYS_PTRACE      CAP_SYS_RAWIO
CAP_SYS_RESOURCE     CAP_SYS_TIME          CAP_SYS_TTY_CONFIG  CAP_SYSLOG
CAP_WAKE_ALARM
```

The ones to know:

| Capability | Lets you | Why it matters in K8s |
|---|---|---|
| **CAP_SYS_ADMIN** | Mount, swap, set hostname, do everything else | Essentially "root". If a container has this, the namespace boundary is mostly cosmetic. Drop ruthlessly. |
| **CAP_NET_ADMIN** | Configure interfaces, routes, iptables | CNI plugins need this; workloads almost never. |
| **CAP_NET_RAW** | Raw and packet sockets (ping, tcpdump) | Default-on for backward compat; lots of CVEs. Drop unless needed. |
| **CAP_NET_BIND_SERVICE** | Bind to ports < 1024 | Lets non-root containers expose port 80/443. |
| **CAP_SYS_PTRACE** | ptrace any process in the user ns | Lets one container debug another in the same Pod. Required for sidecar debug tools. |
| **CAP_SYS_MODULE** | Load kernel modules | NEVER grant this in any container; it's instant host takeover. |
| **CAP_SYS_BOOT** | Reboot the host | Same. |
| **CAP_DAC_OVERRIDE** | Bypass file permission checks | Lots of malware needs this; default-on for root in container. |
| **CAP_SETUID / CAP_SETGID** | setuid/setgid syscalls | Required to drop privileges (e.g., a process starting as root then becoming `www-data`). |
| **CAP_CHOWN** | chown to arbitrary UIDs | Lets you "give away" files to another user. |
| **CAP_BPF** (5.8+) | Load BPF programs | Network observability sidecars need this. |
| **CAP_PERFMON** (5.8+) | Access perf_event_open | Profilers (parca, pixie, pyroscope). |

### 6.1 The Five Capability Sets

Every process has five capability sets (and every file can have three more):

```
Process sets (in /proc/$pid/status):

CapInh   Inheritable    Carried across execve() to file with the same cap in its
                         inheritable set. Mostly historical.
CapPrm   Permitted      The maximum the process CAN have. Cannot exceed this.
CapEff   Effective      The caps the kernel actually consults during permission checks.
                         Process can move bits between Permitted and Effective freely.
CapBnd   Bounding       Upper bound for ALL sets in this process tree. Once dropped,
                         cannot be re-acquired (even via setuid root binary).
CapAmb   Ambient        (3.10+) Preserved across execve() of non-setuid programs.
                         Useful for handing capabilities to subprocesses.

Example:
$ grep ^Cap /proc/self/status
CapInh: 0000000000000000
CapPrm: 000001ffffffffff      # all 41 caps permitted (root)
CapEff: 000001ffffffffff      # all 41 caps effective
CapBnd: 000001ffffffffff      # all 41 caps allowed in tree
CapAmb: 0000000000000000
```

Each bit position in the 64-bit mask corresponds to a CAP_* number (see `/usr/include/linux/capability.h`). The `capsh` tool decodes them:

```
$ capsh --decode=000001ffffffffff
0x000001ffffffffff=cap_chown,cap_dac_override,cap_dac_read_search,cap_fowner,
cap_fsetid,cap_kill,cap_setgid,cap_setuid,cap_setpcap,cap_linux_immutable,
cap_net_bind_service,cap_net_broadcast,cap_net_admin,cap_net_raw,cap_ipc_lock,
cap_ipc_owner,cap_sys_module,cap_sys_rawio,cap_sys_chroot,cap_sys_ptrace,
cap_sys_pacct,cap_sys_admin,cap_sys_boot,cap_sys_nice,cap_sys_resource,
cap_sys_time,cap_sys_tty_config,cap_mknod,cap_lease,cap_audit_write,
cap_audit_control,cap_setfcap,cap_mac_override,cap_mac_admin,cap_syslog,
cap_wake_alarm,cap_block_suspend,cap_audit_read,cap_perfmon,cap_bpf,
cap_checkpoint_restore
```

### 6.2 File Capabilities

A binary can have capabilities baked into its xattr (`security.capability`):

```
$ getcap /usr/bin/ping
/usr/bin/ping cap_net_raw=ep
$ setcap cap_net_raw+ep /usr/bin/mybinary
```

The `e` (effective) and `p` (permitted) suffixes set those bits on exec, regardless of who invokes the binary. This replaces the historical "make ping setuid root" hack — `ping` only needs CAP_NET_RAW, so we grant exactly that.

In containers, file capabilities are usually ignored because containers run with no_new_privs (see §6.4), which prevents privilege gain via exec. But they can subtly bite when an image baker accidentally sets file caps on `/bin/sh` or similar — see ch 27.

### 6.3 The Default Capability Set in Containers

The Docker / OCI / Kubernetes "default" capability set (the bounding/permitted/effective set granted to a container running as root) is conventionally:

```
CAP_AUDIT_WRITE  CAP_CHOWN       CAP_DAC_OVERRIDE  CAP_FOWNER
CAP_FSETID       CAP_KILL        CAP_MKNOD         CAP_NET_BIND_SERVICE
CAP_NET_RAW      CAP_SETFCAP     CAP_SETGID        CAP_SETPCAP
CAP_SETUID       CAP_SYS_CHROOT
```

14 capabilities, down from the 41 a real root has. Everything else is dropped at container start. Notably absent: CAP_NET_ADMIN, CAP_SYS_ADMIN, CAP_SYS_PTRACE, CAP_SYS_MODULE, CAP_BPF.

In a Kubernetes Pod spec you control this via `securityContext.capabilities`:

```yaml
securityContext:
  capabilities:
    drop: ["ALL"]
    add: ["NET_BIND_SERVICE"]      # only this one, to bind :80
  runAsNonRoot: true
  runAsUser: 65534
  allowPrivilegeEscalation: false
```

The shop standard is **drop ALL, add only what the workload provably needs**. This is the difference between "container running as root with a reduced set of root powers" and "container running as nobody with one specific power". The former is still close to a host takeover when chained with a kernel exploit; the latter is much harder to escape from.

### 6.4 no_new_privs

`prctl(PR_SET_NO_NEW_PRIVS, 1)` tells the kernel: this process and its descendants can never gain new privileges via execve. It disables setuid/setgid, file capabilities, LSM exec-time transitions, and so on.

Containers set it by default (`securityContext.allowPrivilegeEscalation: false` forces it on; the default is currently true for backward compat with images that depend on setuid binaries). Without it, a container that contains a setuid-root binary (often unintentionally — many distro images ship them) can escalate by running that binary.

A useful test: inside a container, run `getpcaps` on its own PID. If the effective set is non-empty for a non-root UID, capabilities are being granted via file caps or ambient — investigate.

### 6.5 The capset Syscall and How Capabilities Are Set

The kernel ABI for capabilities is the `capset(2)` / `capget(2)` syscall pair. The data structure carries the inheritable, permitted, and effective sets for three capability "versions" (the v3 format is current):

```c
struct __user_cap_header_struct {
    __u32 version;          /* _LINUX_CAPABILITY_VERSION_3 */
    int   pid;              /* target PID, 0 = self */
};

struct __user_cap_data_struct {
    __u32 effective;
    __u32 permitted;
    __u32 inheritable;
} data[2];                  /* two structs: bits 0-31 and 32-63 */
```

A runtime that wants to set the bounding set must `prctl(PR_CAPBSET_DROP, cap)` for each capability it wants to remove from the bounding set — there is no atomic "set the bounding set to this mask" call. Runc loops over the 64 cap numbers and drops the ones not in the target set; this is a hot path during container startup (~100 syscalls just for capability setup).

The ambient set, added in 4.3, is more recent and avoids a long-standing wart: previously, to pass capabilities to a child via exec, you needed file capabilities on the binary. The ambient set propagates across exec of non-setuid binaries, with the rule that any cap in ambient must also be in permitted and inheritable. Setting an ambient cap uses `prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_RAISE, cap, 0, 0)`.

### 6.6 Why "Run as Non-Root" Matters Even With Capability Drops

A common misconception: "I dropped CAP_SYS_ADMIN, so my container is safe to run as root inside". This misses two attack surfaces:

1. **Filesystem permission bypass within the container**. Root inside the container can read every file the container mounts (including secrets, projected SA tokens, mounted ConfigMaps). A user-level compromise (RCE in the application) becomes a full container takeover trivially.
2. **CVE exposure**. New CVEs in syscall handlers, namespace code, or LSM bypasses periodically grant capability holders new powers. Running as UID 0 means you're always one CVE away from regaining what you dropped. Running as UID 65534 means even an unexpected capability re-grant requires a privilege escalation step.

The Pod Security Standard `restricted` profile requires `runAsNonRoot: true` for exactly this reason. Combined with `runAsUser: 65534` (or anything non-zero) and `allowPrivilegeEscalation: false`, you have a process that is unprivileged in *every* sense — UID-wise, capability-wise, and exec-time-escalation-wise.

---

## 7. seccomp-bpf: Per-Syscall Filtering

seccomp ("secure computing") lets you install a BPF program that runs on every syscall the process makes. The program inspects the syscall number and arguments and returns an action: ALLOW, ERRNO (return a specific error), KILL_PROCESS, TRAP, LOG, or NOTIFY (forward to a userspace supervisor).

```
Syscall flow with seccomp filter installed:

User-space code: write(fd, buf, len)
       │
       │ syscall instruction
       ▼
┌──────────────────────────────────────────┐
│ Kernel syscall entry                      │
│ ┌─────────────────────────────────────┐ │
│ │ seccomp_run_filters()                │ │
│ │   for each filter in chain:         │ │
│ │     run BPF on (nr, arch, args[6])  │ │
│ │   return WORST action                │ │
│ └─────────────────────────────────────┘ │
│        │                                  │
│        ▼                                  │
│   action == SECCOMP_RET_ALLOW?            │
│        │                                  │
│        ├─ yes ─► proceed to handler       │
│        ├─ ERRNO ─► return -ERRNO          │
│        ├─ KILL_PROCESS ─► SIGSYS, dies    │
│        ├─ TRAP ─► SIGSYS to handler       │
│        ├─ LOG ─► log + allow              │
│        └─ NOTIFY ─► userspace supervisor  │
└──────────────────────────────────────────┘
```

A seccomp BPF program is a classic-BPF program (the older, simpler BPF; not eBPF — though you can convert). The input is `struct seccomp_data`:

```c
struct seccomp_data {
    int nr;                  /* syscall number */
    __u32 arch;              /* AUDIT_ARCH_X86_64 etc */
    __u64 instruction_pointer;
    __u64 args[6];           /* syscall args */
};
```

A trivial filter that only allows `read`, `write`, `exit_group`, and `rt_sigreturn`:

```c
struct sock_filter filter[] = {
    /* validate architecture */
    BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),

    /* load syscall nr */
    BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),

    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_read,         3, 0),
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_write,        2, 0),
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_exit_group,   1, 0),
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_rt_sigreturn, 0, 1),
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
};
struct sock_fprog prog = { .len = ARRAY_SIZE(filter), .filter = filter };

prctl(PR_SET_NO_NEW_PRIVS, 1);             /* required before SET_SECCOMP */
prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog);
```

After the prctl, every syscall in this process is filtered. Filters chain (you can install many); the kernel applies all and takes the worst action.

### 7.1 RuntimeDefault Profile

Docker, containerd, and CRI-O ship a default seccomp profile that allows ~300 syscalls out of the ~360 Linux exposes on x86_64, and blocks the dangerous remainder. Kubernetes wires this through:

```yaml
securityContext:
  seccompProfile:
    type: RuntimeDefault          # use containerd/CRI-O default
    # or:
    type: Localhost
    localhostProfile: profiles/myapp.json    # a custom profile installed on the node
    # or:
    type: Unconfined              # no filter at all (don't)
```

Kubernetes 1.25+ defaults pods to `RuntimeDefault` if you set `--seccomp-default` on kubelet. The blocked syscalls are roughly: `add_key`, `bpf`, `clock_settime`, `create_module`, `delete_module`, `finit_module`, `get_kernel_syms`, `get_mempolicy`, `init_module`, `ioperm`, `iopl`, `kcmp`, `kexec_*`, `keyctl`, `lookup_dcookie`, `mbind`, `mount`, `move_pages`, `name_to_handle_at`, `nfsservctl`, `open_by_handle_at`, `perf_event_open`, `personality`, `pivot_root`, `process_vm_*`, `ptrace`, `query_module`, `quotactl`, `reboot`, `request_key`, `set_mempolicy`, `setns`, `settimeofday`, `stime`, `swapon`, `swapoff`, `sysfs`, `_sysctl`, `umount`, `umount2`, `unshare`, `uselib`, `userfaultfd`, `ustat`, `vm86`, `vm86old`.

Workloads that need any of these need a custom profile (or worse, Unconfined).

### 7.2 Building a Profile from Audit Logs

The honest way to build a tight seccomp profile is to run the workload in audit mode, then translate observed syscalls into an allow list.

```
# 1. Run pod with audit profile (allow everything but log all syscalls)
{
  "defaultAction": "SCMP_ACT_LOG",
  "architectures": ["SCMP_ARCH_X86_64"]
}

# 2. Watch the kernel audit log:
sudo ausearch -m SECCOMP --start recent

# 3. Extract syscall names:
sudo ausearch -m SECCOMP --start recent | \
  grep -oP 'syscall=\d+' | sort -u | \
  while read s; do
    nr=${s#syscall=}
    ausyscall x86_64 $nr
  done

# 4. Convert to a profile (denominate observed, deny rest):
jq -n --argjson syscalls '["read","write","close",...]' '{
  defaultAction: "SCMP_ACT_ERRNO",
  architectures: ["SCMP_ARCH_X86_64"],
  syscalls: [{ action: "SCMP_ACT_ALLOW", names: $syscalls }]
}' > myprofile.json
```

Tools like `containerd`'s built-in seccomp logger, `oci-seccomp-bpf-hook` (for CRI-O), and the Kubernetes Security Profiles Operator (KSPO) automate this loop.

### 7.3 The Performance Cost

Every syscall pays a small BPF interpretation cost. On x86_64 with the RuntimeDefault profile (~50 jump comparisons), measurements show:

- Bare syscall (gettid): ~30 ns
- With RuntimeDefault filter: ~50-80 ns
- With a complex 500-rule filter: 200+ ns

For most workloads this is invisible. For syscall-heavy workloads (proxies, low-latency RPC servers, databases doing millions of `epoll_wait` per second), it shows up. The mitigation is to use a JIT-compiled filter (kernel handles this automatically when `seccomp.filter_jit` is on) and keep filters short.

### 7.4 seccomp_unotify: Forwarding Decisions to Userspace

A more recent feature (5.0+, made truly usable by 5.9). The filter returns `SECCOMP_RET_USER_NOTIF`, which suspends the syscall and forwards a notification to a userspace supervisor via an fd. The supervisor inspects the call (including the actual memory of pointer arguments) and decides what to do — return an arbitrary errno, inject a result, or even perform the syscall itself in a different context and return the result to the target.

This is how rootless container runtimes implement "fake CAP_SYS_ADMIN": when the container tries to mount, the runtime intercepts the mount syscall via seccomp_unotify, performs the equivalent operation in the host (with its own privileges, scoped to the container's namespaces), and returns success. The container believes it has done a mount; really, an unprivileged supervisor mediated.

It's also the mechanism for some sandbox technologies (gVisor uses it sparingly, mostly relies on its own syscall interception). The cost is high — every intercepted syscall is now a context switch to the supervisor — so it's reserved for rare operations.

---

## 8. LSMs: AppArmor and SELinux

Linux Security Modules are a kernel framework for *Mandatory Access Control* — restrictions the kernel enforces on top of standard DAC (Discretionary Access Control, the user/group/mode permission bits). Two major LSMs are deployed in production: AppArmor (Ubuntu, SUSE) and SELinux (RHEL, CentOS, Fedora, Android).

```
Permission decision in kernel (every file op, socket op, signal, etc.):

  ┌───────────────────────────────┐
  │ user calls open("/etc/shadow")│
  └──────────────┬────────────────┘
                 │
                 ▼
  ┌───────────────────────────────────┐
  │ DAC check                          │
  │   uid/gid vs file mode             │
  │   pass? → continue                 │
  │   fail? → EPERM                    │
  └──────────────┬────────────────────┘
                 │ (passes DAC)
                 ▼
  ┌───────────────────────────────────┐
  │ Capability check                   │
  │   does subject have CAP_DAC_*?     │
  └──────────────┬────────────────────┘
                 │
                 ▼
  ┌───────────────────────────────────┐
  │ LSM hook: file_open                │
  │   AppArmor: path-based match       │
  │              against profile       │
  │   SELinux : type-enforcement check │
  │              against policy        │
  │   (both can deny; can also audit)  │
  └──────────────┬────────────────────┘
                 │
                 ▼
            allow / EPERM / EACCES
```

Crucial point: LSM hooks fire **after** DAC, so MAC can only further restrict, never grant. A file unreadable by DAC stays unreadable; a file readable by DAC may still be denied by MAC.

### 8.1 AppArmor — Path-Based Profiles

AppArmor profiles refer to filesystem objects by *path*. A profile is a text file in `/etc/apparmor.d/` enumerating which paths the subject may read/write/execute, which capabilities it may use, which network protocols, etc.

```
# /etc/apparmor.d/k8s-myapp
#include <tunables/global>

profile k8s-myapp flags=(attach_disconnected,mediate_deleted) {
  #include <abstractions/base>

  capability net_bind_service,

  # Allow reading the app and configs
  /opt/myapp/**       r,
  /etc/myapp/**       r,

  # Allow writing only to scratch
  /var/lib/myapp/**   rw,
  /tmp/**             rw,

  # Network
  network inet stream,
  network inet dgram,

  # Deny everything else implicitly
  deny /etc/shadow r,                  # explicit denies override
  deny /proc/sys/kernel/** w,
}
```

The profile is compiled into kernel-loadable form by `apparmor_parser`, then activated. Two modes:

- **enforce**: violations are denied + logged
- **complain**: violations are *allowed* but logged (use during profile development)

Kubernetes 1.30+ supports AppArmor directly in PodSecurityContext (before that, only via annotations):

```yaml
securityContext:
  appArmorProfile:
    type: Localhost
    localhostProfile: k8s-myapp     # the profile must already be loaded on every node
```

The profile must be on every node that might run the Pod. There is no built-in distribution mechanism — operators typically use a DaemonSet to lay down `/etc/apparmor.d/` files and reload.

AppArmor's "path-based" approach is its strength (profiles are readable) and its weakness — if an attacker can hardlink, symlink, or bind-mount a sensitive file to a permitted path, AppArmor may grant access. Containers mitigate this via mount namespaces and no_new_privs, but the model is fundamentally weaker than label-based MAC.

### 8.2 SELinux — Label-Based Type Enforcement

SELinux attaches a *label* (subject context) to every process and a *label* (object context) to every file, port, socket, IPC object, etc. Policy is a set of allow rules between label types. A label looks like `user:role:type:level`, e.g.:

```
$ ls -lZ /etc/shadow
-rw-------. 1 root root system_u:object_r:shadow_t:s0 1234 /etc/shadow

$ ps -eZ | grep sshd
system_u:system_r:sshd_t:s0  1234 ?  00:00:01 sshd
```

A policy rule would say:

```
allow sshd_t shadow_t : file { read getattr };
```

Meaning: a process labeled `sshd_t` may read or stat a file labeled `shadow_t`. Anything else is denied (the default-deny model — type enforcement). The type system has tens of thousands of rules in production policies (e.g., `targeted` policy on RHEL ships with ~5000 types and ~100,000 rules).

For containers, the relevant SELinux abstraction is **container_t** (or a derived type) for the container process, with files labeled `container_file_t`. Policy permits `container_t` to do "container things" (read its own files, talk to the container runtime via specific channels) and forbids cross-container or host access.

To prevent containers on the same host from interfering with each other, SELinux uses **Multi-Category Security (MCS)**: each container gets a unique pair of categories (e.g., `c123,c456`), attached to its label. Two containers with different MCS categories cannot read each other's files even if both are labeled `container_t`:

```
container A: system_u:system_r:container_t:s0:c123,c456
container B: system_u:system_r:container_t:s0:c789,c012
```

Files A creates are labeled with c123,c456; B is denied access because its label lacks those categories.

Kubernetes exposes this via:

```yaml
securityContext:
  seLinuxOptions:
    user:  system_u
    role:  system_r
    type:  container_t
    level: s0:c123,c456
```

In practice, the container runtime allocates MCS labels automatically (one unique pair per container), and you almost never set this manually. The exception is **volume sharing** between containers in the same Pod with `fsGroupChangePolicy: OnRootMismatch` and the CSI volume's `SELinuxMount` capability — the kubelet/CSI need to relabel the volume to the Pod's MCS label so the container can actually read it.

A `seLinuxOptions.level: s0` (no categories) means "any container can read this Pod's files" — effectively disabling MCS for this Pod. Use only when sharing volumes across unrelated Pods, and know what you're trading off.

### 8.3 Choosing AppArmor vs SELinux

You don't, usually — your distro chooses. RHEL/CentOS/Fedora/OpenShift run SELinux; Ubuntu and SUSE run AppArmor; Debian can do either. The OpenShift world is SELinux-native and assumes strict enforcement; the upstream Kubernetes world is more AppArmor-friendly because Ubuntu is more common.

Operationally:

| Aspect | AppArmor | SELinux |
|---|---|---|
| Mental model | "what paths can I access?" | "what types can my type touch?" |
| Profile authoring | Readable, hand-writable | Requires policy engineering |
| Container isolation | Per-profile rules | MCS automatic, type-enforcement layered |
| Common failure mode | Path bypass via mount tricks | Wrong label, denials in audit log |
| Diagnostics | `dmesg`, `aa-status`, `aa-logprof` | `ausearch -m AVC`, `audit2allow`, `sealert` |

---

## 9. Filesystem Isolation: OverlayFS

A container image is a stack of layers. When the runtime instantiates a container, it materializes those layers into a unified filesystem the container sees as `/`. The standard mechanism is **OverlayFS** — a union filesystem that stacks read-only lower layers under a read-write upper layer.

```
┌─────────────────────────────────────────────────────────────┐
│ MERGED (what the container sees as /)                        │
│  All files from all layers, with upper overriding lower      │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           │  overlay mount
                           │
   ┌───────────────────────┼────────────────────────────┐
   │                       │                            │
   ▼                       ▼                            ▼
┌────────────┐      ┌────────────┐              ┌────────────┐
│ UPPERDIR   │      │ WORKDIR    │              │ LOWERDIR(s)│
│ (writable) │      │ (kernel    │              │ stacked    │
│            │      │  scratch)  │              │ read-only  │
│ copy-up    │      │            │              │            │
│  + new     │      │            │              │ image      │
│  + whiteout│      │            │              │ layers     │
└────────────┘      └────────────┘              └─────┬──────┘
                                                       │
                                                       ▼
                                             ┌──────────────────┐
                                             │ Layer N (top)    │
                                             │ Layer N-1        │
                                             │ ...              │
                                             │ Layer 0 (base)   │
                                             └──────────────────┘
```

The kernel mount command literally is:

```
mount -t overlay overlay \
  -o lowerdir=/layers/base:/layers/runtime:/layers/app,\
upperdir=/containers/abc/diff,\
workdir=/containers/abc/work \
  /containers/abc/merged
```

`lowerdir` is colon-separated, **leftmost wins** in case of conflict (some implementations vary; standard kernel OverlayFS is leftmost-wins). `upperdir` and `workdir` must be on the same filesystem (the work directory holds temporary inodes the kernel uses during atomic operations like rename).

### 9.1 Copy-Up Semantics

When a process inside the container modifies a file that exists only in a lower (read-only) layer, OverlayFS copies the entire file to the upper layer first ("copy-up"), then applies the modification:

```
Read /etc/hosts (exists only in layer base):
  → kernel checks upper: not present
  → check workdir: not present
  → check lowerdir[0], [1], ... → found in base
  → open from base (read-only is fine)

Write to /etc/hosts:
  → kernel checks upper: not present
  → COPY-UP: cp /lower/base/etc/hosts → /upper/etc/hosts
              (full file, even if you're changing 1 byte)
              (preserves permissions, xattrs, mtime, etc.)
  → modify /upper/etc/hosts
  → subsequent reads/writes hit upper only
```

Copy-up has two practical costs:
1. **Storage**: a 1-byte change to a 1 GB file uses 1 GB of upper-dir storage.
2. **Time**: copy-up of a multi-GB file can stall the container's write for minutes.

This is why databases inside containers should *never* keep their data files on the container filesystem (overlayfs upper) — always mount a volume so writes go to the underlying filesystem directly, bypassing copy-up. Same for logs that get appended frequently.

### 9.2 Whiteouts and Opaque Directories

To represent "this file existed in a lower layer but is deleted in this layer", OverlayFS uses a **whiteout**: a character device with major 0, minor 0 in the upper directory.

```
$ ls -la /containers/abc/diff/etc/
total 0
crw-r--r-- 1 root root 0, 0 Jan  1 12:00 oldfile     ← whiteout for "oldfile"
```

When the kernel walks the merged view and finds a whiteout, it stops descending the lower stack for that name — the file is "gone". Similarly, an **opaque directory** (xattr `trusted.overlay.opaque=y`) tells the kernel "even though there are layers below, ignore them for this directory" — used when a layer wants to completely replace a directory's contents.

### 9.3 Image Layers Become OverlayFS Layers

The mapping from OCI image to OverlayFS is direct:

```
OCI image manifest:
  config (entrypoint, env, layers)
  layers: [
    sha256:aaa  (base: debian:slim ~50MB tarball)
    sha256:bbb  (apt-get install nginx, ~30MB diff)
    sha256:ccc  (COPY ./app /opt/app, ~5MB)
  ]

containerd snapshotter:
  /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/
  ├── 1/fs/    ← layer aaa extracted (whole base rootfs)
  ├── 2/fs/    ← layer bbb extracted (just the nginx-related files)
  └── 3/fs/    ← layer ccc extracted (just /opt/app)

When container starts:
  mkdir /var/lib/containerd/.../snapshots/active/N/fs       (upperdir)
  mkdir /var/lib/containerd/.../snapshots/active/N/work     (workdir)
  mount -t overlay overlay -o \
    lowerdir=/snapshots/3/fs:/snapshots/2/fs:/snapshots/1/fs,\
    upperdir=/snapshots/active/N/fs,\
    workdir=/snapshots/active/N/work \
    /run/containerd/io.containerd.runtime.v2.task/.../rootfs
```

`crictl inspect <container>` (or `docker inspect`) shows the actual mount under `GraphDriver.Data`.

### 9.4 Inode Exhaustion

OverlayFS has a peculiar inode pressure: each layer is its own inode set, and the merged view "consumes" inodes from the upper filesystem only on copy-up. But the lower filesystems' inodes are still occupied. On nodes with hundreds of containers and large images:

```
$ df -i /var/lib/containerd
Filesystem      Inodes  IUsed  IFree IUse%
/dev/nvme0n1p1  6553600 6300000 253600 96%
```

When `IFree` hits 0, **no new files can be created anywhere** on that filesystem, even with terabytes of free space. Kubelet's eviction signals include `imagefs.inodesFree` precisely for this case — when free inodes drop, kubelet starts garbage-collecting unused images before the node becomes unusable.

Mitigations:
- Use a filesystem with dynamic inode allocation (XFS, btrfs) instead of ext4.
- Aggressive image GC tuning (`--image-gc-high-threshold`, `--image-gc-low-threshold`).
- For massive image counts, use a snapshotter like `stargz` (lazy pull) that doesn't materialize files until accessed.

### 9.5 Alternatives

OverlayFS is the default, but containerd supports other snapshotters:

| Snapshotter | Mechanism | When to use |
|---|---|---|
| overlayfs | Linux OverlayFS | Default; works everywhere |
| native | Plain copy of files | When OverlayFS unavailable; very slow |
| btrfs | Btrfs subvolume snapshots | If host is btrfs; very fast clones |
| zfs | ZFS dataset snapshots | If host is zfs; very fast clones |
| stargz / soci | Lazy-loaded indexed tarballs | Multi-GB images, fast container start |
| devmapper | LVM thin pools | Legacy, mostly retired |

---

## 10. Networking Primitives: veth, Bridges, VXLAN, netfilter

The Kubernetes networking model says: every Pod has a unique IP, every Pod can reach every other Pod's IP without NAT, every node can reach every Pod's IP. The CNI plugin implements that promise. Under the hood, the kernel provides four primitives.

### 10.1 The Network Namespace, in Detail

A new netns is a sealed island. The kernel struct `net` (in `linux/include/net/net_namespace.h`) holds *per-namespace copies* of essentially every networking subsystem state:

- The interface list (`net_device` array)
- IPv4 + IPv6 routing tables (per-table FIBs)
- ARP / NDP neighbor tables
- The conntrack table (`nf_conntrack`)
- All netfilter chains and rules
- The socket hash tables (TCP, UDP, etc.)
- The IP fragmentation cache
- The XFRM (IPsec) state
- `/proc/net/`, `/sys/class/net/`, `/sys/net/`

A socket is created in the netns of the calling process and stays there for its lifetime. This is why you can have many containers each binding `0.0.0.0:80` — each is in its own netns with its own socket table.

### 10.2 veth: The Virtual Ethernet Cable

A **veth pair** is two virtual network devices created together. Whatever frame goes in one comes out the other. Put one end in a container's netns, the other in the host's netns, and you have a bidirectional link.

```
┌────────────────────────────┐         ┌──────────────────────┐
│ Host netns                  │         │ Container netns A    │
│                             │         │                      │
│   cni0 bridge               │         │   eth0 (= veth-a-c)  │
│   ├── veth-a-h ◄──────────────────────► (other end)         │
│   ├── veth-b-h ◄────────┐  │         │   ip 10.244.1.2/24   │
│   └── ...               │  │         │   default via 10.244.│
│                          │  │         │                      │
│   eth0 (real NIC) ──► VXLAN/BGP       │   1.1                │
└──────────────────────────┼─┘         └──────────────────────┘
                           │
                           │  ┌──────────────────────┐
                           └──► Container netns B    │
                              │   eth0 (= veth-b-c)  │
                              │   ip 10.244.1.3/24   │
                              └──────────────────────┘
```

Creating one:

```
ip link add veth-h type veth peer name veth-c
ip link set veth-c netns container-pid
ip -n container-pid addr add 10.244.1.2/24 dev veth-c
ip -n container-pid link set veth-c up
ip link set veth-h up
```

`ip -n container-pid` runs the command in that netns (it's a shortcut for `nsenter --net=/proc/.../ns/net ip ...`).

### 10.3 Linux Bridge — the L2 Switch

A bridge is a virtual L2 switch. You add interfaces (veth host ends, physical NICs, tun/tap, ...) and the bridge forwards frames among them based on a learned MAC table. The classic CNI plugin (`bridge`) does exactly:

```
ip link add cni0 type bridge
ip link set cni0 up
ip addr add 10.244.1.1/24 dev cni0           # bridge has an IP (gateway for pods)

# For each new pod:
ip link add vethXXX type veth peer name eth0 netns $POD_NS
ip link set vethXXX master cni0              # attach host end to bridge
ip link set vethXXX up
ip -n $POD_NS addr add 10.244.1.N/24 dev eth0
ip -n $POD_NS route add default via 10.244.1.1
```

All pods on this node now share an L2 segment via `cni0` and can talk directly. The bridge IP is their default gateway, so cross-node traffic goes via the host stack (which routes/encapsulates appropriately).

`docker0` is the same idea — a bridge created by the Docker daemon. The name `cni0` is just the conventional name when a CNI plugin creates it.

### 10.4 VXLAN Encapsulation

To reach pods on *other* nodes, we either need routing (BGP / native routing, e.g., Calico in BGP mode, Cilium native routing) or encapsulation. The default for most overlays (Flannel, default Calico, Weave) is **VXLAN**: each cross-node packet is wrapped in a UDP packet on port **8472** with a **VNI** (VXLAN Network Identifier) tag.

```
Original packet (pod-to-pod):
┌─────────────────────────────────────────────────────────────┐
│ src=10.244.1.2 dst=10.244.2.5 proto=TCP sport=... dport=80 │
│ payload                                                       │
└─────────────────────────────────────────────────────────────┘

VXLAN-encapsulated on the wire:
┌──────────────────────────────────────────────────────────────┐
│ Outer Eth:  src=node1-MAC  dst=node2-MAC                     │
│ Outer IP :  src=node1-IP   dst=node2-IP   proto=UDP          │
│ Outer UDP:  sport=random   dport=8472                        │
│ VXLAN   :   flags  VNI=1                                      │
│ Inner Eth:  src=pod1-MAC  dst=pod2-MAC                       │
│ Inner IP :  src=10.244.1.2 dst=10.244.2.5                    │
│ TCP      :  sport=... dport=80                               │
│ payload                                                       │
└──────────────────────────────────────────────────────────────┘
```

The kernel `vxlan` interface type implements this. CNI configures it:

```
ip link add flannel.1 type vxlan id 1 dstport 8472 dev eth0 nolearning
ip link set flannel.1 up

# FDB entries map (inner MAC → outer node IP):
bridge fdb append 00:00:00:00:00:00 dev flannel.1 dst <node2-public-IP> self
```

When a packet destined for a pod on node2 arrives at node1's routing layer, the route says "send via flannel.1", which encapsulates and unicasts to node2:8472. Node2's `flannel.1` decapsulates and re-routes the inner packet to the local pod.

**Overhead**: 50 bytes of headers (14 eth + 20 IP + 8 UDP + 8 VXLAN) per packet. On a 1500-MTU network this means the inner MTU must be ≤ 1450, or you get fragmentation. CNI plugins typically set the pod-facing interface MTU to 1450 to avoid this.

### 10.5 netfilter — Where Packets Get Filtered, NAT'd, Mangled

netfilter is the kernel's packet-processing framework. It defines **hook points** in the network stack and lets userspace install rules (chains) that run at each hook.

The classic five hooks for IPv4:

```
                          incoming packet
                                │
                                ▼
                       ┌─────────────────┐
                       │   PREROUTING    │  ← raw/mangle/nat/conntrack
                       │   (just arrived) │
                       └────────┬────────┘
                                │
                       routing decision: for me?
                                │
                ┌───────────────┴───────────────┐
                │                               │
                ▼                               ▼
           ┌─────────┐                     ┌─────────┐
           │  INPUT  │                     │ FORWARD │  (transit)
           │ (to local │                   │         │
           │  socket)  │                   │         │
           └────┬────┘                     └────┬────┘
                │                               │
        application                             │
                │                               │
                ▼                               │
           ┌─────────┐                          │
           │ OUTPUT  │ ◄── local socket sends   │
           │         │                          │
           └────┬────┘                          │
                │                               │
                ▼                               ▼
                       ┌─────────────────┐
                       │  POSTROUTING    │ ← nat (SNAT, MASQUERADE)
                       │ (about to leave) │
                       └────────┬────────┘
                                │
                                ▼
                         out to the wire
```

Each hook has multiple **tables** (filter, nat, mangle, raw, security), and each table has chains (built-in: matching the hook name; plus user-defined chains called from built-ins). A rule is `match` + `target`. Targets include ACCEPT, DROP, REJECT, DNAT (rewrite destination), SNAT, MASQUERADE (SNAT to outgoing interface IP), MARK, LOG, jumps to user chains, etc.

iptables / nftables example:

```
# Drop all incoming traffic on eth0 except SSH:
iptables -A INPUT -i eth0 -p tcp --dport 22 -j ACCEPT
iptables -A INPUT -i eth0 -j DROP

# NAT pods' outgoing traffic to host IP (so external services see the host):
iptables -t nat -A POSTROUTING -s 10.244.0.0/16 -o eth0 -j MASQUERADE

# Service VIP DNAT (this is what kube-proxy does):
iptables -t nat -A PREROUTING -p tcp -d 10.96.0.42 --dport 80 \
  -j DNAT --to-destination 10.244.1.7:8080
```

### 10.6 conntrack — Stateful Tracking

Connection tracking is what makes NAT and stateful firewalls work. The kernel maintains a hash table keyed by 5-tuple (`src ip, src port, dst ip, dst port, proto`) per direction. When a packet arrives, conntrack looks up its connection, decides its state (NEW, ESTABLISHED, RELATED, INVALID), and remembers reverse-NAT mappings.

```
$ cat /proc/sys/net/netfilter/nf_conntrack_max
262144
$ cat /proc/sys/net/netfilter/nf_conntrack_count
142387
$ conntrack -L | head
tcp  6  431999  ESTABLISHED  src=10.244.1.2 dst=10.96.0.10 sport=44320 dport=53 ...
udp  17 29      src=10.244.1.2 dst=8.8.8.8 sport=12345 dport=53 ...
```

Kubernetes' iptables-mode kube-proxy relies entirely on conntrack: a Service VIP packet hits PREROUTING, gets DNAT'd to a chosen pod IP, and conntrack remembers the mapping so the reply (from pod → originally-requested VIP) gets reverse-NAT'd back transparently.

Pathologies:
- **conntrack table full**: new connections fail with `nf_conntrack: table full, dropping packet`. Symptom: random TCP timeouts under load. Fix: raise `nf_conntrack_max` (`sysctl -w net.netfilter.nf_conntrack_max=1048576`).
- **NAT port exhaustion**: when SNAT'ing many pods to a single source IP toward the same external destination, the (src-port, dst-ip, dst-port) tuples can collide, causing intermittent failures.
- **conntrack hash collisions**: tune `net.netfilter.nf_conntrack_buckets` to be ~1/4 of `nf_conntrack_max`.

### 10.7 iptables vs nftables

iptables is the userspace tool that talks to xtables, an older kernel framework. nftables is the modern replacement, with a unified data model, better update performance, and atomic rule replacement.

Both fit into netfilter. The kernel supports both simultaneously (with care to avoid double-processing). Modern distros default to `nftables` and provide an `iptables-nft` compatibility shim that translates iptables rules into nftables rules.

For Kubernetes, kube-proxy can use:

| Mode | Implementation | Notes |
|---|---|---|
| iptables | xtables-legacy or iptables-nft | Default for years; O(n) rule list per packet |
| ipvs | IPVS (IP Virtual Server) | Hashed lookup; better at scale |
| nftables | Native nftables (1.31+) | Best of iptables-style but faster |
| eBPF | Cilium/kpng | Skip netfilter entirely; ch 14, 16 |

The iptables pathology: at 5000 Services × 5 endpoints each = 25,000 DNAT rules to traverse linearly per packet. nftables and IPVS use hash structures; eBPF uses program-attached maps. See ch 14 for the deep dive.

### 10.8 The cni0 / docker0 Bridge Model — End-to-End Packet Path

Putting it all together for a packet from `pod-A on node1 (10.244.1.2)` to `pod-B on node2 (10.244.2.5)` in a VXLAN overlay:

```
1. Pod A: socket sendmsg() → TCP → IP layer
   src=10.244.1.2 dst=10.244.2.5
2. IP routing in pod-A netns: default via 10.244.1.1 dev eth0
3. ARP for 10.244.1.1 → cni0's MAC
4. Frame leaves pod-A's eth0 (= veth-A-cnt) → appears on veth-A-host (host netns)
5. veth-A-host is attached to cni0 → bridge forwarding
6. cni0 is the gateway for 10.244.1.0/24; needs to route 10.244.2.0/24
7. Host routing table: 10.244.2.0/24 via 10.244.2.1 dev flannel.1
8. flannel.1 = VXLAN device → encapsulates:
     inner eth+ip+tcp = the pod packet
     outer eth+ip+udp(8472)+vxlan(VNI=1)
     dst MAC: from FDB (00:00:00:00:00:00 → dst=node2-IP)
9. Outer packet exits host's eth0 to physical network
10. Arrives at node2's eth0
11. Routed to flannel.1 (UDP/8472 → vxlan)
12. Decapsulated → inner packet src=10.244.1.2 dst=10.244.2.5
13. Routed: 10.244.2.0/24 dev cni0
14. ARP for 10.244.2.5 → pod-B's veth MAC (learned by bridge)
15. Bridge forwards to veth-B-host
16. Frame appears on veth-B-cnt in pod-B netns
17. Pod B's eth0 receives → IP → TCP → socket recvmsg()
```

At every hop a packet might be touched by netfilter (PREROUTING on entry, POSTROUTING on exit), conntrack (state tracking), and any installed iptables/nftables/eBPF rules. The end-to-end latency in a healthy cluster is ~100-200 μs cross-node, dominated by the physical NIC/wire.

This is **a lot** of work per packet, which is why high-performance CNIs (Cilium with BPF host routing, AWS VPC CNI with ENI-per-pod) collapse multiple steps or skip the bridge entirely.

---

## 11. eBPF: Hook Points, Maps, Verifier, CO-RE

eBPF (the kernel's "extended" BPF) is a sandboxed bytecode VM inside the Linux kernel. You write a small program in restricted C, the kernel JITs it to native code, and runs it at well-defined hook points whenever they fire. The kernel guarantees the program will terminate, not crash, and not touch memory it shouldn't — enforced by the **verifier** at load time.

This is the foundation of Cilium, Falco, Tetragon, Pixie, Tracee, Katran, and increasingly the Kubernetes dataplane. Ch 16 goes deep; here we cover just enough to make subsequent chapters coherent.

### 11.1 Hook Points

eBPF programs attach to specific kernel hook points. The major families:

| Family | Hook | When it fires | Used by |
|---|---|---|---|
| **kprobe / kretprobe** | Entry/return of (almost) any kernel function | Per call to that function | bcc tools, Falco syscall mode |
| **tracepoint** | Stable kernel tracepoints | When tracepoint executes | Most "trace this kernel event" tools |
| **fentry / fexit** | Modern, faster replacement for kprobe (BPF trampoline) | Per call (lower overhead) | Newer observability stacks |
| **uprobe** | Userspace function entry/return | Per call into userspace func | User-space tracing |
| **xdp** | NIC driver, before kernel networking stack | Per RX packet (earliest possible) | DDoS mitigation (Cloudflare, Katran) |
| **tc (clsact)** | tc qdisc, ingress/egress of interface | Per packet entering or leaving an interface | Cilium, Calico eBPF dataplane |
| **socket** (cgroup-attached) | bind/connect/recvmsg/sendmsg per socket | At socket-level operations | Cilium kube-proxy replacement (socket LB) |
| **cgroup** | cgroup-attached hooks (cgroup_sock, cgroup_skb, cgroup_device) | Per syscall or per packet for a cgroup | Per-pod policy |
| **lsm** | LSM hook points (kernel 5.7+) | At MAC decision points | KubeArmor, runtime security |
| **perf_event** | perf counters | On sample / overflow | Profilers (Parca, Pyroscope) |
| **sk_msg / sockmap** | At socket-buffer level | Per message | Service mesh acceleration |

```
Packet path with eBPF hooks:

NIC ──► driver RX ──► XDP ────────────► tc ingress ──► netfilter ──► IP routing
                       │                  │              (iptables/    │
                       │                  │               nftables)    │
                       │                  │                            ▼
                       │                  │                       socket LB
                       │                  │                       (cgroup)
                       │                  │                            │
                       │                  │                            ▼
                       │                  │                       application
                       │                  │
                XDP_DROP/XDP_REDIRECT     tc redirect/mirror
                (μs latency, line rate)   (slower than XDP but
                                          full skb context)
```

For Kubernetes' purposes the relevant hooks are tc (Cilium policy + dataplane), cgroup socket (Cilium service LB), LSM (runtime security), kprobe/tracepoint (Falco), and XDP (rare, used for ingress acceleration).

### 11.2 BPF Maps

A BPF program is short-lived per invocation, but it needs to share state across invocations and with userspace. **Maps** are key-value structures the kernel manages. Common types:

| Type | Use |
|---|---|
| `BPF_MAP_TYPE_HASH` | General K→V |
| `BPF_MAP_TYPE_ARRAY` | K=index |
| `BPF_MAP_TYPE_PERCPU_*` | Per-CPU instances, lockless |
| `BPF_MAP_TYPE_LPM_TRIE` | Longest-prefix-match (routing tables, network policy) |
| `BPF_MAP_TYPE_LRU_HASH` | Bounded hash with LRU eviction |
| `BPF_MAP_TYPE_RINGBUF` | Userspace consumer reading kernel-produced events |
| `BPF_MAP_TYPE_SK_STORAGE` | Per-socket key-value |
| `BPF_MAP_TYPE_PROG_ARRAY` | Tail calls (chain BPF programs) |

A Cilium agent, for example, populates maps with `(pod IP) → identity`, `(identity, identity, port) → allow/deny`, `(service VIP, port) → backend list`. The BPF program installed at tc consults these on every packet; the agent updates them in response to apiserver events.

Reading maps from userspace:

```
$ bpftool map list
$ bpftool map dump id 42
[
  { "key": "...", "value": "..." },
  ...
]
$ bpftool prog show
```

### 11.3 The Verifier

Before loading, the kernel's BPF verifier statically proves the program:

- Has bounded execution time (no loops without a known upper bound; bounded loops via `bpf_loop()` helper)
- Never dereferences a pointer without first verifying it's within a known-valid range
- Never reads uninitialized memory
- Respects map type semantics
- Has bounded stack depth (currently 512 bytes)
- Terminates on every path

The verifier is a *symbolic interpreter* — it tracks the range of every register on every path. A program that the verifier cannot prove safe is rejected. This is what makes BPF safe to load into the kernel from unprivileged userspace (with CAP_BPF; historically required CAP_SYS_ADMIN).

The cost is that BPF programming has a learning curve specific to "what the verifier accepts" — many constructs that would compile in C are rejected. Ch 16 covers this in detail; here it's enough to know the verifier exists.

### 11.4 BTF and CO-RE

A BPF program written against kernel struct layouts breaks every time the kernel changes (different version, different config). The original workaround was BCC, which shipped LLVM and recompiled at install time per kernel.

**BTF** (BPF Type Format) is debug-info-like type info shipped in the kernel image (`/sys/kernel/btf/vmlinux`). **CO-RE** (Compile Once, Run Everywhere) is the libbpf+LLVM mechanism that lets a program pre-bind to struct field *offsets* via BTF relocations at load time, automatically adapting to the target kernel's layout.

```
Old way (BCC):
  source.bpf.c → install on every node → invoke clang+llvm at runtime → load
  Per-node: 100+ MB of clang+llvm, 5-30s install delay.

CO-RE way (libbpf):
  source.bpf.c → compile once with bpf2c+BTF relocations → ship one binary
  At load: kernel resolves struct field offsets via local BTF
  Per-node: ~1 MB, near-instant load.
```

This is why modern eBPF tools (Cilium, Tetragon, Falco-modern, Pixie) ship as small static binaries instead of as BCC-based fat Python tools.

### 11.5 What Cilium, Falco, Tetragon Do with All This

Just enough to make ch 16/28 land:

- **Cilium** attaches BPF programs at tc ingress/egress of every veth, at cgroup-attached socket ops, and at XDP on host interfaces. Maps hold the policy graph, service VIP → backend mapping, identity → label mapping. Result: packets traverse a single eBPF program that does identity resolution, policy enforcement, service load-balancing, masquerade, all in ~1 μs, skipping iptables entirely.

- **Falco** (in its modern eBPF mode) attaches to syscall tracepoints. Each syscall is funneled into Falco's userspace engine, where Lua-style rules match against patterns ("a shell was spawned by a Pod containing nginx"). The performance challenge is that high-syscall-rate processes can generate millions of events/sec; Falco rate-limits, samples, and uses kernel-side filtering.

- **Tetragon** (Cilium's runtime security tool) uses BPF LSM hooks + kprobes to enforce policy in-kernel (kill or signal a process) rather than just observe. This is significantly faster than userspace-detection-then-respond because the response happens before the syscall completes.

### 11.6 Why This Matters for Kubernetes Networking

The push to replace iptables with eBPF in Kubernetes dataplanes is driven by:
- iptables rule-list scaling pathology (O(n) per packet at scale).
- iptables atomic-replacement limitation (large rule replacement takes seconds, during which the rule set is partial).
- iptables not letting policy and load-balancing share state.
- eBPF programs being attachable per cgroup, per interface, with shared maps that the agent can update incrementally.

Cilium reports ~10× throughput improvement over kube-proxy iptables mode for service traffic at high connection rates, and 4-5× lower latency. The argument for keeping iptables: it's debuggable with universally-known tools; eBPF requires bpftool, bcc, or vendor dashboards.

---

## 12. Putting It All Together: What `docker run` Actually Does

Concrete trace for `docker run -it --rm alpine sh`. Every step ties back to a kernel primitive above.

```
T+0    User: docker run -it --rm alpine sh
T+1ms  docker CLI → dockerd via /var/run/docker.sock (REST)
T+2ms  dockerd: image pull (skip; cached)
T+3ms  dockerd: create container record, allocate name, prepare spec
T+5ms  dockerd → containerd via /run/containerd/containerd.sock (gRPC)
       Request: containerd CreateContainer + StartContainer
T+8ms  containerd: snapshotter (overlayfs) prepares rootfs
       mount -t overlay overlay \
         -o lowerdir=/var/lib/containerd/.../snapshots/1/fs,
            upperdir=/var/lib/containerd/.../snapshots/active/N/fs,
            workdir=/var/lib/containerd/.../snapshots/active/N/work \
         /run/containerd/.../rootfs
T+12ms containerd starts a containerd-shim-runc-v2 process for this container
T+15ms shim invokes runc create with config.json:
       {
         "process": { "args": ["sh"], "terminal": true, ... },
         "root":    { "path": "/run/containerd/.../rootfs" },
         "linux": {
           "namespaces": [
             { "type": "pid" }, { "type": "network" }, { "type": "mount" },
             { "type": "uts" }, { "type": "ipc" }, { "type": "cgroup" }
           ],
           "resources": { "memory": {...}, "cpu": {...} },
           "cgroupsPath": "system.slice/docker-<id>.scope",
           "seccomp": { "defaultAction": "SCMP_ACT_ERRNO", "syscalls": [...] },
           "maskedPaths": ["/proc/kcore", "/proc/keys", ...],
           "readonlyPaths": ["/proc/asound", "/proc/bus", ...]
         }
       }
T+20ms runc: clone3() with flags
         CLONE_NEWPID | CLONE_NEWNET | CLONE_NEWNS |
         CLONE_NEWUTS | CLONE_NEWIPC | CLONE_NEWCGROUP |
         CLONE_INTO_CGROUP (fd to pre-created cgroup) |
         CLONE_PIDFD
       Child PID returned to parent; child is now PID 1 in new pid ns,
       in new netns (empty), in cgroup system.slice/docker-<id>.scope.
T+22ms runc parent: writes uid_map, gid_map (if userns), then signals child via pipe
T+23ms runc child:
       - sethostname() → applies in new uts ns
       - mount("none", "/", NULL, MS_REC|MS_PRIVATE, NULL)   # detach propagation
       - mount(rootfs over /)                                  # bind rootfs to /
       - mount("proc", "/proc", "proc", ...)                  # private /proc in pid ns
       - mount("sysfs", "/sys", "sysfs", ...)
       - bind-mounts for /dev/pts, /dev/shm, /dev/null, ...
       - pivot_root(".", "old_root")
       - umount2("/old_root", MNT_DETACH)
       - chdir("/")
       - apply MaskedPaths: bind /dev/null over /proc/kcore etc.
       - apply ReadonlyPaths: re-bind RO over /proc/asound etc.
T+28ms cgroup setup (done by parent before clone via CLONE_INTO_CGROUP, but if not):
       echo $CHILD_PID > /sys/fs/cgroup/.../docker-<id>.scope/cgroup.procs
       echo 100M     > .../memory.max
       echo "10000 100000" > .../cpu.max     # 10% of 1 cpu
       echo 1024     > .../pids.max
T+30ms capabilities reduction (parent writes via prctl + capset):
       drop everything not in image's permitted set:
         CAP_BND set to: CHOWN,DAC_OVERRIDE,FSETID,FOWNER,MKNOD,NET_RAW,
                         SETGID,SETUID,SETFCAP,SETPCAP,NET_BIND_SERVICE,
                         SYS_CHROOT,KILL,AUDIT_WRITE
       capset() applied to all five process sets.
T+32ms prctl(PR_SET_NO_NEW_PRIVS, 1)
T+33ms prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &filter)
       The ~50-rule default profile is loaded.
T+34ms apparmor_setprocattr() — load AppArmor profile "docker-default"
       (or SELinux setcon() for container_t with MCS labels)
T+35ms setresgid/setresuid (drop to UID specified in image, e.g., still 0)
T+36ms close all extra fds (anything > 2 not pre-marked)
T+37ms set_robust_list, arch_prctl, ...
T+38ms ── final step ──
       execve("/bin/sh", ["sh"], envp)
T+40ms /bin/sh runs as PID 1 inside its pid namespace, with
       - private mount tree rooted at the overlayfs merged dir
       - empty network namespace (no eth0 yet; networking depends on driver)
       - bounded to 100M memory, 10% cpu, 1024 pids
       - 14 capabilities permitted, no_new_privs set
       - seccomp filter blocking 50+ dangerous syscalls
       - AppArmor profile "docker-default" attached
T+42ms If --network bridge (default): docker's network plugin (libnetwork) creates
       veth pair, attaches to docker0 bridge, assigns IP from docker0's subnet,
       configures routes inside container.
T+45ms docker daemon attaches stdin/stdout/stderr (since -it) to the shim's
       pseudo-tty pair → user sees prompt.
```

What just happened, primitive by primitive:

| Step | Primitive | Section |
|---|---|---|
| Spec → runc | OCI runtime contract | (ch 01) |
| Mount overlayfs rootfs | OverlayFS | §9 |
| clone3 with CLONE_NEW* | Namespaces | §2 |
| pivot_root + mount /proc /sys | Mount ns | §2.3 |
| Set hostname | UTS ns | §2.4 |
| Write to cgroup files | cgroups v2 | §4-5 |
| capset() to bounding set | Capabilities | §6 |
| no_new_privs + seccomp filter | seccomp-bpf | §6.4, §7 |
| AppArmor / SELinux profile | LSM | §8 |
| veth + bridge attach | Networking | §10 |
| Finally execve | The container is "running" | — |

Now imagine all of the above happening for every container in every Pod on every node, orchestrated by kubelet via CRI (ch 10), with CNI doing the network bit (ch 15) and CSI doing volumes (ch 19). Kubernetes is the orchestration; *this* is the actual mechanism.

---

## 13. Pitfalls

The things that bite you only after you've shipped containers to production.

### 13.1 PID 1 and Zombies

PID 1 has two implicit kernel responsibilities:
1. **Reap zombies**: when any process in the namespace dies, if its parent has exited, it is reparented to PID 1, which must `waitpid()` it. If PID 1 doesn't reap, zombies accumulate in the process table indefinitely.
2. **Signal handling**: PID 1 receives only signals for which it has registered handlers. SIGTERM with no handler is *silently ignored*.

A shell as PID 1 (`/bin/sh` or `/bin/bash`) does neither well by default — `sh` doesn't handle SIGTERM unless your script traps it, and the shell only reaps children spawned by itself, not orphans reparented to it.

Symptoms: `kubectl delete pod` blocks for `terminationGracePeriodSeconds` and then SIGKILL; or `ps` in the container shows hundreds of `<defunct>` processes.

Fixes:
- Use a real init: `tini` (Docker's built-in `--init`), `dumb-init`, `s6`, `catatonit`, or shareProcessNamespace + `pause`. These reap orphans and forward signals.
- In Kubernetes: `spec.shareProcessNamespace: true` makes the pause container be PID 1 (which already reaps), and your app process becomes PID 2+. Pause reaps. Done.
- If you must be PID 1, install a SIGTERM handler that does graceful shutdown, and ensure your runtime (Go, JVM, Node) reaps children correctly (they all do).

### 13.2 /proc and /sys Leaks Across Namespaces

A new mount namespace alone does *not* give you a fresh `/proc` — you also need to `mount -t proc proc /proc` inside, otherwise the container sees the host's procfs (with all host PIDs!). `unshare --mount-proc` does this for you; runc does it as a normal step in container startup.

But even with a fresh `/proc`, some files reflect *host* state:
- `/proc/sys/*` — kernel sysctls, mostly host-wide (some are namespaced, like net.* in netns; most are not). Containers can read host values via `/proc/sys/kernel/hostname` etc.
- `/proc/cpuinfo`, `/proc/meminfo` — host values. Tools that auto-tune by `nproc` or `/proc/meminfo` will see the *host* sizes, not the container's cgroup limits. This is why Java needed `-XX:+UseContainerSupport` and Go added the GOMEMLIMIT/GOMAXPROCS-from-cgroup support.

Mitigations:
- `lxcfs` or virtual procfs implementations that present cgroup-adjusted values.
- For Kubernetes pods, the downward API exposes resource limits as env vars: `MY_MEM_LIMIT=$(cat /sys/fs/cgroup/memory.max)`.
- Software that's container-aware reads cgroup files directly.

### 13.3 cgroup-v1 vs cgroup-v2 Mixed Mode

Some kernels boot with `systemd.unified_cgroup_hierarchy=0` (legacy v1) or `=1` (full v2). A few historical configurations enable both — v2 mounted at `/sys/fs/cgroup/unified/` while v1 is at `/sys/fs/cgroup/*/`. This "hybrid mode" was supposed to ease migration; in practice it confuses everything: runtimes don't know which to use, kubelet feature flags depend on a clean v2 setup, some controllers exist only in v1, others only in v2.

The required modern config: full v2 (`systemd.unified_cgroup_hierarchy=1` or distro default). If you see `/sys/fs/cgroup/cgroup.controllers` *and* `/sys/fs/cgroup/memory/`, you're in hybrid mode — fix it.

### 13.4 Capability Creep via setuid Binaries

A container running as non-root with a reduced cap set looks safe — until you discover the image has `/usr/bin/su` (setuid root). Without no_new_privs, exec'ing su gains the bounding set's capabilities. With no_new_privs, the setuid bit is ignored.

This is why `allowPrivilegeEscalation: false` (which sets no_new_privs) is a hard requirement for the `restricted` Pod Security Standard. Audit container images for setuid binaries (`find / -perm -4000` at build time) and remove them.

### 13.5 User Namespace Surprises

- **Subuid exhaustion**: with the default of 65,536 subuids per user, you can run ~65 containers per user account before the allocator runs out. Allocate larger ranges (`usermod --add-subuids 100000-100065535 alice`).
- **Filesystem permissions**: a host file owned by UID 100000 appears as UID 0 inside the container; the reverse mapping means files the container creates land as UID 100000 on the host. Backups, host-side log collectors, monitoring agents that read container files need to understand the mapping.
- **NFS and user namespaces**: NFS authentication is UID-based; the server doesn't know about your container's user namespace mapping. Either use idmapped mounts (5.12+) or run NFS with no_root_squash (and pray).
- **CAP_NET_ADMIN scope**: CAP_NET_ADMIN in a user ns lets you manipulate interfaces *in netns'es owned by your user ns*. It does *not* let you touch host interfaces.

### 13.6 Seccomp Profile Drift

Building a tight seccomp profile is a one-time exercise — until an update to your application introduces a new syscall (e.g., upgrading glibc, switching to io_uring), at which point the workload starts failing with `EPERM` from random places. Symptoms are wildly varying: a missing `clone3` causes mysterious `fork: Operation not permitted`; a missing `statx` makes filesystem operations fail in strange ways.

Mitigations:
- Use `RuntimeDefault` unless you have a reason to be tighter.
- If using custom profiles, log denials (`SCMP_ACT_LOG` for unmatched, in addition to ERRNO) and watch the audit log.
- Use the Security Profiles Operator (KSPO) to roll profile changes alongside app changes.

### 13.7 Conntrack and NAT Exhaustion

A node with many short-lived outbound connections (e.g., a service that calls a single external API) can exhaust the (source-port, dest-IP, dest-port) tuple space when NAT'd to a single host IP. Symptoms: intermittent EAGAIN, connection-establishment timeouts, but only when calling specific external destinations.

Mitigations:
- Connection pooling at the app level (most languages have this).
- Use SNAT to multiple source IPs.
- Raise `net.ipv4.ip_local_port_range`.
- Replace NAT-based outbound with proper egress gateways or NodeIP-based egress.

### 13.8 OverlayFS and Large Writes

A container that writes a 10 GB file into a path that exists in a lower layer triggers a 10 GB copy-up. The container hangs (writing) for tens of seconds to minutes; the upper layer fills with the entire file even if only one byte changes. Same problem on tools that "patch" large files in place (some VM image manipulation tools).

Mitigation: never write large mutable files into the container's rootfs. Mount a volume.

### 13.9 Bridge Mode and Promiscuous Sniffing

A container on the cni0 / docker0 bridge can see broadcast frames (ARP, mDNS) from every other container on the same bridge. With CAP_NET_RAW it can sniff them (`tcpdump -i eth0`). This is rarely a confidentiality issue but constantly a surprise during debugging — "why does my pod see ARP for some other pod's IP?"

For real isolation between Pods on the same node, use a CNI that puts each Pod on its own subnet (Cilium with "endpoint routes", AWS VPC CNI) or strict NetworkPolicy enforcement.

### 13.10 Kernel Version Drift Between Nodes

Containers carry binaries built against some glibc version; the kernel ABI is supposed to be backward-compatible, but new syscalls (io_uring, statx, openat2, ...) aren't available on older kernels. A workload that uses `io_uring` will silently fall back to `epoll` on a kernel that doesn't support it; a workload that *requires* `clone3` will fail outright on a 4.x kernel.

Kubernetes itself requires increasingly modern kernels per release (1.28+ recommends 4.19+; 1.30+ recommends 5.4+ for full cgroup v2 + PSI). Mixed-version node pools (one node on 4.14, another on 6.6) cause workloads to behave differently depending on where they land. Standardize the kernel.

### 13.11 hostNetwork Pods and Port Conflicts

A Pod with `hostNetwork: true` runs in the host's network namespace — it gets the host's IPs and the host's port space. If two such pods both want port 8080, the second fails to start with "address already in use". DaemonSets that use hostNetwork (kube-proxy, CNI, ingress controllers) are usually fine because they're the only one; user workloads bypassing the Pod IP model invite this footgun.

### 13.12 fork() Without exec()

`fork()` in a container inherits all the security state: namespaces, cgroup membership, capabilities, seccomp filter, AppArmor profile, no_new_privs. `exec()` resets some of it (mainly: file descriptors with FD_CLOEXEC are closed). A common security-tools assumption — "we'll just spawn a helper from the container's init via fork" — needs to consider: the helper has the same namespaces and cgroup. If the helper is intended to be "outside the container", it must `setns()` and `unshare()` explicitly.

---

## 14. TL;DR

A container is one or more processes the kernel lies to (eight namespace types — pid, net, mnt, uts, ipc, user, cgroup, time, each created via `clone3`/`unshare`/`setns` and visible as inode magic-links in `/proc/$pid/ns/`), bounds (cgroup v2 unified hierarchy under `/sys/fs/cgroup/`, with `cpu`/`memory`/`io`/`pids`/`hugetlb`/`cpuset` controllers exposing `*.max`/`*.high`/`*.weight` files), reduces in authority (drop 41 capabilities down to ~14 by default, then to zero if you mean it; layer no_new_privs and a seccomp-bpf allowlist over the top), gates with an LSM (AppArmor's path-based profiles on Ubuntu; SELinux's label-based type enforcement with MCS categories on RHEL; both are kernel-enforced MAC on top of DAC), gives a private root via OverlayFS (lowerdir = stacked image layers read-only, upperdir = container's writes via copy-up, with whiteouts as character devices for deletions), and wires into the network via a private netns + veth pair into a bridge + VXLAN encap on udp/8472 + netfilter chains (PREROUTING for DNAT, POSTROUTING for SNAT/MASQUERADE) + conntrack for stateful reverse-NAT. Kubernetes' QoS classes map to a deterministic cgroup tree under `kubepods.slice/`, with Guaranteed at the top, Burstable in a sub-slice, BestEffort separately, and per-Pod and per-container scopes underneath. eBPF (programs attached at XDP, tc, cgroup-sock, kprobe, tracepoint, LSM hooks, with maps for shared state, verifier-proven safe at load time, BTF + CO-RE for kernel-version portability) is the modern dataplane and observability substrate — it is what Cilium uses to replace kube-proxy and iptables, what Falco/Tetragon use for runtime security, and what every "next generation" Kubernetes networking project is built on. A `docker run` is, in order: snapshotter mounts the overlay → runc clones a new process with the right `CLONE_NEW*` flags → child does pivot_root + mounts /proc + sethostname → parent writes cgroup files + capset + no_new_privs + seccomp filter + LSM profile → finally execve. Pitfalls cluster around PID 1 not reaping zombies, /proc and /sys leaking host state, cgroup v1/v2 hybrid mode, setuid binaries bypassing your capability drops, user-namespace UID mapping confusing filesystems, seccomp profile drift breaking apps on glibc upgrades, conntrack table exhaustion under outbound load, OverlayFS copy-up stalling on large file edits, bridge-mode containers seeing each other's ARP, kernel-version drift breaking modern syscalls, and hostNetwork pods colliding on ports. Master this layer and Kubernetes stops being magic — it becomes a 38-chapter orchestration around these exact knobs.
