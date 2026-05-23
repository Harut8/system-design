# Building a Kubernetes from Scratch: The `minik8s` Capstone

The previous 38 chapters dissected Kubernetes one organ at a time. This chapter sews them back together. We design a minimal Kubernetes-equivalent end-to-end — call it `minik8s` in spirit, the way `simpledb.py` is to a real database — and walk every line of the build order. The goal is not a runnable codebase (you can write that in a weekend once the shape is in your head) but a coherent picture: every data structure, every gRPC service, every control loop, every file path, in the order you would actually build them.

If chapters 00–37 each answered the question "what does this one thing do?", this chapter answers "if you sat down on a Monday morning and had nine months, how would you build the whole stack from `unshare(2)` up to a controller that reconciles Deployments?" The answer is **about 5000 lines of well-organized pseudocode**, the same way `simpledb.py`'s 2500 lines suffice to demonstrate every database concept from buffer pools to MVCC to LSM compaction.

The other 4,000,000 lines of real Kubernetes? We will account for every one of them in §17, and you will understand why each is there.

This is the synthesis chapter. Every previous chapter is referenced; every box on the ROADMAP diagram gets a paragraph; every "K8s is magic" moment from the last 37 chapters is reduced to a control loop you can read in one sitting.

Audience: staff engineers who have read chapters 00–37. The pace assumes you know what an informer is, what `unshare(CLONE_NEWNET)` does, what a PLEG transition looks like, and why the scheduler binds *to* nodes rather than letting nodes pull *from* a queue. If any of those land flat, return to the relevant chapter; we will not redefine.

---

## Table of Contents

1. [TL;DR](#1-tldr)
2. [The Thesis: 5,000 Lines vs 4,000,000](#2-the-thesis-5000-lines-vs-4000000)
3. [The Minimal Feature Set: What `minik8s` Will and Won't Do](#3-the-minimal-feature-set-what-minik8s-will-and-wont-do)
4. [The Component List and the Chapter Map](#4-the-component-list-and-the-chapter-map)
5. [The Build Order: Ten Phases](#5-the-build-order-ten-phases)
6. [Phase 1 — Build a Container by Hand](#6-phase-1--build-a-container-by-hand)
7. [Phase 2 — A CRI-Like Local gRPC](#7-phase-2--a-cri-like-local-grpc)
8. [Phase 3 — `minikv`: A Watchable KV Store](#8-phase-3--minikv-a-watchable-kv-store)
9. [Phase 4 — `mini-apiserver`: REST Over Typed Objects](#9-phase-4--mini-apiserver-rest-over-typed-objects)
10. [Phase 5 — The Watch Endpoint](#10-phase-5--the-watch-endpoint)
11. [Phase 6 — `mini-kubelet`: The Node Agent](#11-phase-6--mini-kubelet-the-node-agent)
12. [Phase 7 — `mini-scheduler`: Filter, Score, Bind](#12-phase-7--mini-scheduler-filter-score-bind)
13. [Phase 8 — `mini-controller-manager`: Informers and Reconcilers](#13-phase-8--mini-controller-manager-informers-and-reconcilers)
14. [Phase 9 — `mini-cni`: One Hundred Lines of Bridge](#14-phase-9--mini-cni-one-hundred-lines-of-bridge)
15. [Phase 10 — `mini-proxy`: Services via iptables](#15-phase-10--mini-proxy-services-via-iptables)
16. [What We Deliberately Skip and Why](#16-what-we-deliberately-skip-and-why)
17. [Where Did the Extra 99.9% Go?](#17-where-did-the-extra-999-go)
18. [End-to-End Demo: From `curl -X POST` to Running Container](#18-end-to-end-demo-from-curl--x-post-to-running-container)
19. [Reading Guide: Mapping `minik8s` to Real Kubernetes Source](#19-reading-guide-mapping-minik8s-to-real-kubernetes-source)
20. [What This Exercise Teaches](#20-what-this-exercise-teaches)
21. [Next-Level Projects After `minik8s`](#21-next-level-projects-after-minik8s)
22. [A Taxonomy of "Real Kubernetes Complexity"](#22-a-taxonomy-of-real-kubernetes-complexity)
23. [A Final Reflection: What K8s Got Right, Wrong, and What Comes Next](#23-a-final-reflection-what-k8s-got-right-wrong-and-what-comes-next)
24. [Forward From Here: SIGs, KEPs, and the Contributor Path](#24-forward-from-here-sigs-keps-and-the-contributor-path)

---

## 1. TL;DR

Kubernetes is **etcd plus N controllers that watch etcd**. Build the etcd-equivalent first (a watchable, revisioned, in-memory KV — ~300 lines). Wrap it in REST with optimistic concurrency on `resourceVersion` (the mini-apiserver — ~600 lines). Stream changes back to clients as an HTTP/2 event feed (watch — ~150 lines). Now write a node agent that watches Pods bound to its node and calls a local runtime (mini-kubelet — ~700 lines). Write a scheduler that watches unbound Pods and patches `spec.nodeName` (mini-scheduler — ~400 lines). Write a controller manager that watches Deployments and conjures ReplicaSets and Pods (mini-controller-manager — ~600 lines). Write a CNI plugin that hands out IPs from a file-backed pool and a bridge (mini-cni — ~100 lines). Write a proxy that rebuilds iptables rules on Service+Endpoints changes (mini-proxy — ~300 lines). Write a runtime that takes a pod spec and produces a process tree under `runc` (mini-runtime — ~400 lines). The total: **~5,000 lines**. Every concept from chapters 00–37 has a place to live in those 5,000 lines.

The rest of the chapter walks each phase in detail, then explains why the real codebase is 800× larger.

---

## 2. The Thesis: 5,000 Lines vs 4,000,000

Pull `kubernetes/kubernetes` at HEAD and you get roughly four million lines of Go. The aggregation layer alone is several hundred thousand. The cloud provider stubs were another million before they were excised in the 1.31 timeframe. `pkg/kubelet/cm` (the container manager subtree of the kubelet) is 100,000 lines on its own. `pkg/apis/core/validation/validation.go` is fifteen thousand lines of `if cond { errs = append(errs, …) }`.

But: take the source tree, delete every generated file (`zz_generated_*.go`, `zz_generated_deepcopy.go`, every `*-clientset/*`, every `openapi_generated.go`), every test (`*_test.go` plus `test/` directories), every backwards-compatibility shim (`v1beta1` → `v1` converters), every cloud provider, every CSI sidecar, every alpha feature gate, every metric registration, every audit annotation, every feature gate branch, every aggregation layer special case, every `subresource` handler (`/exec`, `/attach`, `/portforward`, `/log`, `/proxy`), every CPUManager/MemoryManager/TopologyManager/DeviceManager, every kube-proxy mode except one, every alpha scheduler plugin, every conversion between alpha/beta/stable APIs… and what is left? **About five thousand lines** of essential mechanism, distributed across the dozen-or-so components.

The thesis of this chapter is that those five thousand essential lines have a single coherent shape, that shape is teachable in one sitting, and once you can hold the shape in your head, the other 3,995,000 lines stop being intimidating: each one is doing *more of the same essential thing* in service of an extra integration, an extra version, an extra workload, an extra cloud, an extra knob, or an extra performance corner case.

This is exactly the move `simpledb.py` makes for databases. The real Postgres is 1.5 million lines of C. `simpledb.py` is 2500 lines of Python that exposes the same architecture: pager, slotted pages, buffer pool, WAL, B+Tree, MVCC, executor, lexer, parser, LSM, replication stub. The architecture is identical to Postgres. The vast majority of Postgres's million-and-a-half lines are *implementations* of the same architecture against decades of constraints, versions, and platforms. The architecture is small. The deployment-grade fidelity is what's enormous.

Kubernetes works the same way. Architecture: small. Deployment-grade fidelity: enormous. Our job in this chapter is to build the small thing, in the right order, so that when we say "watch fan-out" or "informer cache" or "PLEG event" or "CRI shim," there is a five-line snippet in `minik8s` you can point at.

```
┌──────────────────────────────────────────────────────────────────┐
│                    THE 5,000-LINE MINIK8S STACK                  │
├──────────────────────────────────────────────────────────────────┤
│  clients (kubectl-mini, curl)                                    │
├──────────────────────────────────────────────────────────────────┤
│  mini-apiserver        REST + watch + admission stub  (~600 LoC) │
│       │                                                          │
│       ▼                                                          │
│  minikv                In-mem watchable revisioned KV   (~300)   │
├──────────────────────────────────────────────────────────────────┤
│  mini-controller-manager   informer + workqueue + reconcile (~600)│
│  mini-scheduler            filter + score + bind        (~400)   │
├──────────────────────────────────────────────────────────────────┤
│  mini-kubelet (per node)   syncLoop + PLEG + status     (~700)   │
│       │ CRI gRPC                                                 │
│       ▼                                                          │
│  mini-runtime         runc shim + namespace setup       (~400)   │
│       │ CNI exec       │ no CSI — hostPath only                  │
│       ▼                                                          │
│  mini-cni             bridge + veth + IPAM              (~100)   │
├──────────────────────────────────────────────────────────────────┤
│  mini-proxy (per node)   iptables generator             (~300)   │
├──────────────────────────────────────────────────────────────────┤
│  Linux kernel         namespaces · cgroups · netfilter           │
│  runc                 OCI runtime (we shell out, not reimplement)│
└──────────────────────────────────────────────────────────────────┘
                       ≈ 5,000 lines total
```

This diagram is the entire chapter, drawn once. The rest is a guided tour of building it bottom up.

---

## 3. The Minimal Feature Set: What `minik8s` Will and Won't Do

A useful toy is one that demonstrates the architecture, not one that tries to be a stunted Kubernetes. We are explicit about scope.

### 3.1 Will Do

- **Typed declarative API.** GET/POST/PUT/PATCH/DELETE on `Pod`, `Node`, `Service`, `Endpoints`, `Deployment`, `ReplicaSet`. Optimistic concurrency on `resourceVersion`. JSON marshaling. URL routing of the form `/api/v1/namespaces/{ns}/{resource}[/{name}]`. (Chapters 05, 11, 12, 14.)
- **A single-node watchable KV.** All objects stored under string keys (`/registry/pods/default/foo`), monotonic revision counter, in-memory only, file-backed snapshot for crash recovery. No Raft. (Chapter 04 reference; we deliberately skip distribution.)
- **Server-side watch streams.** HTTP/2 SSE-like newline-delimited JSON events with `resourceVersion` cursor, `410 Gone` on compacted revisions, reconnect semantics. (Chapter 05.)
- **A controller pattern library.** Informer (list+watch), shared cache, indexer (by namespace and label), rate-limited workqueue, level-triggered reconcile. (Chapter 08.)
- **Built-in controllers.** Deployment → ReplicaSet, ReplicaSet → Pod, Endpoints controller. (Chapter 12 for workloads, 14 for endpoints.)
- **A scheduler.** Watch unbound pods, filter by `nodeName`/resources, score by least-allocated, bind via PATCH. (Chapter 09.)
- **A node agent.** Watch Pods bound to me, sync to runtime via CRI, generate PLEG events, write status back. (Chapter 10.)
- **A real container runtime.** `runc` invoked over a small CRI-like gRPC; namespaces, cgroups v2, seccomp default profile. (Chapters 00, 01.)
- **A CNI plugin.** Bridge + veth + file-backed IPAM. Every pod gets an IP, pods on the same node can talk. Cross-node connectivity by adding a static route per node (not VXLAN). (Chapter 15.)
- **A service abstraction.** ClusterIP only. iptables rules rebuilt per reconcile. (Chapter 14.)

### 3.2 Won't Do (and Where to Add Them)

- **HA control plane.** One process per role, one node ("control plane" and "worker" colocated for the demo). To add HA: replace `minikv`'s map with a Raft state machine (chapter 04), add leader election leases (chapter 08 §leader-election) to the controller-manager and scheduler.
- **CSI / persistent volumes.** Pods can use `hostPath` only. To add: define `PersistentVolume`, `PersistentVolumeClaim`, `StorageClass`, write a controller that binds PVCs to PVs and a kubelet plugin that mounts; then design the three-phase CSI lifecycle (chapter 19).
- **Ingress and service mesh.** No L7. To add: write a controller that watches `Ingress`/`HTTPRoute` objects and configures an Envoy/NGINX (chapter 17).
- **RBAC.** No AuthN, no AuthZ; every request runs as `system:masters`. To add: certificate-based AuthN, an RBAC policy evaluator over `(user, verb, resource, namespace)` tuples (chapter 07).
- **OIDC / workload identity.** Not even a hook. (Chapter 07.)
- **Admission webhooks.** Inline admission only (defaulting + structural validation). To add: a webhook chain that POSTs to an admission server, then a CEL evaluator for in-process admission (chapter 06).
- **Custom Resource Definitions.** Schema is hard-coded in Go structs. To add: a CRD object whose creation registers new GVR routes dynamically in the apiserver (chapter 23).
- **Aggregated apiservers.** No `APIService` registration. (Chapter 24.)
- **NetworkPolicy.** Bridge passes everything. (Chapter 20.)
- **Multi-cluster.** One cluster, period. (Chapter 26.)
- **Cloud integration.** No CCM. (Chapter 37.)

You should be able to read this list and, for every line, know roughly where it would slot into `minik8s` if you decided to add it. That recognition is the chapter's purpose.

---

## 4. The Component List and the Chapter Map

Eight binaries, four shared libraries, one runtime dependency. Below is the mapping from `minik8s` component to the real Kubernetes component to the chapter that explained it.

| `minik8s` binary/library | Real K8s analog | Chapter | Lines |
|---|---|---|---|
| `minikv` | etcd | 04 | ~300 |
| `mini-apiserver` | kube-apiserver | 05 (with stubs from 06, 07) | ~600 |
| `mini-informer` (lib) | client-go informers | 08 | ~250 |
| `mini-workqueue` (lib) | client-go workqueue | 08 | ~100 |
| `mini-controller-manager` | kube-controller-manager | 08, 12, 36 | ~600 |
| `mini-scheduler` | kube-scheduler | 09 | ~400 |
| `mini-kubelet` | kubelet | 10, 11 | ~700 |
| `mini-runtime` | containerd + CRI shim | 01, 02 | ~400 |
| `mini-cni` | Calico/Flannel/etc | 15, 16 | ~100 |
| `mini-proxy` | kube-proxy | 14 | ~300 |
| `kubectl-mini` | kubectl | 05 | ~150 |
| `runc` (external) | runc | 01 | n/a |

Notice how the chapter list collapses: chapters 00 (Linux primitives) and 21 (resource management) live inside `mini-runtime`'s namespace+cgroup setup; chapter 11 (pod internals) lives inside `mini-kubelet`'s syncLoop; chapter 36 (GC) is a controller inside `mini-controller-manager`. Chapters 13 (StatefulSet), 18 (DNS), 19 (CSI), 20 (NetworkPolicy), 22 (autoscaling), 23 (CRDs), 24 (aggregation), 25 (tenancy), 26 (multi-cluster), 27 (supply chain), 28 (runtime security), 29 (sandboxing), 30 (observability), 31 (GitOps), 32 (lifecycle), 33 (edge), 34 (custom schedulers), 35 (perf), 37 (cloud) are *all* additive — they would each be one or two extra controllers or one extra plugin slotted in cleanly above this foundation.

That is the punchline of the whole chapter, stated once early so the reader knows where we are headed.

---

## 5. The Build Order: Ten Phases

Mirroring the `MENTAL_MODEL.md` build order from the databases series, and condensing the ROADMAP's 24 phases into the 10 essential ones for the toy.

```
   PHASE     WHAT YOU BUILD                                     CHAPTER REF
   ──────    ────────────────────────────────────────────       ───────────
   0         Linux primitives, prerequisite reading              00
   1         A container by hand: unshare+pivot_root+cgroups     00, 01
   2         A CRI-like local gRPC around phase 1                01
   3         minikv: in-mem watchable revisioned KV              04
   4         mini-apiserver REST in front of minikv              05
   5         The watch endpoint (SSE/HTTP-stream)                05
   6         mini-kubelet: watch Pods bound here, drive runtime  10, 11
   7         mini-scheduler: filter+score+bind                    09
   8         mini-controller-manager: informer→workqueue→reconcile 08, 12, 36
   9         mini-cni: bridge + veth + IPAM from file pool       15
   10        mini-proxy: iptables rules per Service+Endpoints    14
```

The rule for the order is the same as the databases roadmap: **each phase must be runnable end-to-end before you move to the next**. After phase 1 you can `./minik8s-run-by-hand ./busybox-rootfs /bin/sh` and get a shell in a namespaced process. After phase 4 you can `curl -X POST mini-apiserver/api/v1/.../pods -d '{...}'` and the pod object lands in `minikv`. After phase 6 a hand-bound pod (where you set `nodeName` yourself in the JSON) actually runs as a container on the local node. After phase 7 the scheduler binds it for you. After phase 8 you POST a Deployment and three pods materialize. After phase 10 pods can reach each other via Service VIP.

Crucially: **phases 0–7 build a single-node cluster you could call a working Kubernetes**. Phases 8–10 add the things that make it interesting (multi-pod controllers, multi-node networking, services). The order mirrors the way Kubernetes itself grew: Brendan Burns's original Borg-inspired prototype was roughly the artifact of phases 0–7.

```
                  THE BUILD ORDER AS A DEPENDENCY DAG

          ┌─────────┐
          │ Phase 0 │  Linux primitives (read, don't build)
          └────┬────┘
               ▼
          ┌─────────┐
          │ Phase 1 │  Container by hand
          └────┬────┘
               ▼
          ┌─────────┐
          │ Phase 2 │  CRI-like local gRPC
          └────┬────┘
               │
   ┌───────────┼────────────────────────────┐
   │           ▼                            ▼
   │      ┌─────────┐               (parallelizable from
   │      │ Phase 3 │                here on)
   │      │ minikv  │
   │      └────┬────┘
   │           ▼
   │      ┌─────────┐
   │      │ Phase 4 │  REST apiserver
   │      └────┬────┘
   │           ▼
   │      ┌─────────┐
   │      │ Phase 5 │  Watch endpoint
   │      └────┬────┘
   │           │
   │     ┌─────┼─────────────┬────────────────┐
   │     ▼     ▼             ▼                ▼
   └──→ ┌─────────┐   ┌──────────┐    ┌───────────────┐
        │ Phase 6 │   │ Phase 7  │    │ Phase 8       │
        │ kubelet │   │ scheduler│    │ controllers   │
        └────┬────┘   └────┬─────┘    └──────┬────────┘
             │             │                  │
             ▼             ▼                  ▼
        ┌─────────┐                     ┌───────────┐
        │ Phase 9 │ ◄─── needed by ─────│ Phase 10  │
        │  CNI    │       kubelet       │  proxy    │
        └─────────┘                     └───────────┘
```

The next ten sections walk each phase in detail.

---

## 6. Phase 1 — Build a Container by Hand

Before you write a control plane, write a container. Not a `docker run`; a process tree that you, the engineer, summoned by combining the syscalls from chapter 00 in the right order.

### 6.1 What "Run a Container" Means, Sequenced

```
   The 11-step incantation:

   1.  Resolve image → rootfs path (we'll cheat: pre-extracted tarball)
   2.  Allocate a cgroup v2 directory under /sys/fs/cgroup/minik8s/<podID>/<ctrID>
   3.  Write cpu.max, memory.max, pids.max, io.max into the cgroup
   4.  fork() (or clone3 with CLONE_INTO_CGROUP for atomicity)
   5.  In child:
          unshare(CLONE_NEWPID | CLONE_NEWNET | CLONE_NEWNS |
                  CLONE_NEWUTS | CLONE_NEWIPC | CLONE_NEWCGROUP)
          (skip CLONE_NEWUSER for simplicity; can add later)
   6.     fork() again — required so that PID 1 inside is the new process
          (the first fork's child is still in the old PID ns view of init)
   7.        Mount /proc afresh (so PID ns is reflected)
             Bind mount the rootfs directory to a tmp mount
             pivot_root(new=rootfs_tmp, put_old=rootfs_tmp/.old)
             umount /.old
             chdir("/")
   8.        Set up the network: at this point only `lo` exists. We will
             call out to `mini-cni` later to add a veth pair.
   9.        Apply seccomp filter (load BPF allowlist)
  10.        Drop capabilities (keep only CAP_NET_BIND_SERVICE, drop the rest)
  11.        execve(argv[0], argv, envp)
```

If this looks like nine of the eleven items from chapter 00 §3 ("Building a Container by Hand with `unshare` and `nsenter`"), that is because it is. The only step `minik8s` adds is the cgroup write at step 2/3.

### 6.2 The Phase 1 Pseudocode (~300 lines)

We will write this as Go-flavored pseudocode. Real `runc` is 25,000 lines of Go; we are building the 300-line educational version.

```go
// File: minik8s/runtime/byhand/main.go
package main

import (
    "os"
    "os/exec"
    "syscall"
    "encoding/json"
    "fmt"
    "io/ioutil"
)

// ContainerSpec is the minimal config we accept (mirrors a slice of OCI runtime-spec).
type ContainerSpec struct {
    ID         string            // unique id (UUID)
    PodID      string            // pod sandbox id (for cgroup parent)
    Rootfs     string            // path to pre-extracted image filesystem
    Argv       []string          // command + args (must be relative to rootfs)
    Env        []string          // KEY=VAL strings
    CPUMax     string            // cgroup v2 cpu.max value, e.g., "100000 100000"
    MemMax     string            // cgroup v2 memory.max, e.g., "536870912"
    PidsMax    string            // cgroup v2 pids.max
    NetnsPath  string            // path to a *previously created* netns (or "" to create new)
    Caps       []string          // capability allowlist
    Seccomp    string            // seccomp profile name; "default" loads a tight allowlist
    Hostname   string            // uts ns hostname
}

func main() {
    // Re-exec trick: we run ourselves twice. First invocation as parent, second
    // (with env MINIK8S_CHILD=1) as the child inside the new namespaces.
    if os.Getenv("MINIK8S_CHILD") == "1" {
        runChild()
        return
    }
    runParent()
}

func runParent() {
    spec := readSpec(os.Args[1]) // path to spec.json on stdin or file

    // STEP 2-3: create cgroup, write limits.
    cgroupPath := fmt.Sprintf("/sys/fs/cgroup/minik8s/%s/%s", spec.PodID, spec.ID)
    must(os.MkdirAll(cgroupPath, 0755))
    if spec.CPUMax != ""  { writeFile(cgroupPath+"/cpu.max",  spec.CPUMax)  }
    if spec.MemMax != ""  { writeFile(cgroupPath+"/memory.max", spec.MemMax) }
    if spec.PidsMax != "" { writeFile(cgroupPath+"/pids.max",   spec.PidsMax) }
    // Enable the controllers in the parent before any task is moved.
    writeFile("/sys/fs/cgroup/minik8s/cgroup.subtree_control", "+cpu +memory +pids +io")

    // STEP 4-5: fork+unshare via clone3.
    // We use Go's syscall.SysProcAttr Cloneflags as a shorthand. Real runc uses
    // raw clone3 with CLONE_INTO_CGROUP so the child is born already inside the
    // target cgroup (no race window).
    cmd := exec.Command("/proc/self/exe", os.Args[1])
    cmd.Env = append(os.Environ(), "MINIK8S_CHILD=1")
    cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
    cmd.SysProcAttr = &syscall.SysProcAttr{
        Cloneflags: syscall.CLONE_NEWPID | syscall.CLONE_NEWNS |
                    syscall.CLONE_NEWUTS | syscall.CLONE_NEWIPC |
                    syscall.CLONE_NEWNET | syscall.CLONE_NEWCGROUP,
        // No user ns for simplicity.
    }
    must(cmd.Start())

    // After Start: pin pid to cgroup if not using CLONE_INTO_CGROUP.
    writeFile(cgroupPath+"/cgroup.procs", fmt.Sprintf("%d", cmd.Process.Pid))

    // Hand the kubelet/CRI side a handle on the netns so mini-cni can attach.
    fmt.Println(fmt.Sprintf("/proc/%d/ns/net", cmd.Process.Pid))

    // Wait — the container's lifetime is the lifetime of this parent.
    if err := cmd.Wait(); err != nil {
        os.Exit(extractExitCode(err))
    }
}

func runChild() {
    spec := readSpec(os.Args[1])

    // STEP 5b: set hostname in our new UTS namespace.
    must(syscall.Sethostname([]byte(spec.Hostname)))

    // STEP 6: a second fork so we become PID 1 in the new ns
    //         (the unshare's child is still PID 1 from its parent's PoV).
    //         Skipped in this snippet for brevity; assume the re-exec already did it.

    // STEP 7: pivot_root.
    // Make the mount propagation private so we don't pollute the host.
    must(syscall.Mount("", "/", "", syscall.MS_PRIVATE|syscall.MS_REC, ""))
    // Bind the rootfs onto itself so it's a mountpoint.
    must(syscall.Mount(spec.Rootfs, spec.Rootfs, "", syscall.MS_BIND|syscall.MS_REC, ""))
    must(os.MkdirAll(spec.Rootfs+"/.old", 0700))
    must(syscall.PivotRoot(spec.Rootfs, spec.Rootfs+"/.old"))
    must(os.Chdir("/"))
    must(syscall.Unmount("/.old", syscall.MNT_DETACH))
    must(os.Remove("/.old"))

    // Mount the standard set inside the new mount ns.
    must(syscall.Mount("proc", "/proc", "proc",
        syscall.MS_NOSUID|syscall.MS_NODEV|syscall.MS_NOEXEC, ""))
    must(syscall.Mount("sysfs", "/sys", "sysfs",
        syscall.MS_NOSUID|syscall.MS_NODEV|syscall.MS_NOEXEC|syscall.MS_RDONLY, ""))
    must(syscall.Mount("tmpfs", "/dev", "tmpfs",
        syscall.MS_NOSUID, "mode=755,size=65536k"))
    // Recreate /dev/null, /dev/zero, /dev/urandom by bind-mounting from host.
    for _, dev := range []string{"null", "zero", "urandom", "random", "tty"} {
        path := "/dev/"+dev
        f, _ := os.Create(path); f.Close()
        must(syscall.Mount("/host/dev/"+dev, path, "", syscall.MS_BIND, ""))
    }

    // STEP 9: seccomp. We load a hardcoded "default" profile that blocks
    //         keyctl, kexec_load, perf_event_open, etc. Real runc parses
    //         JSON. Here we link against libseccomp via cgo (omitted).
    loadSeccompProfile(spec.Seccomp)

    // STEP 10: drop capabilities.
    dropCapabilitiesTo(spec.Caps)

    // STEP 11: exec the entrypoint.
    must(syscall.Exec(spec.Argv[0], spec.Argv, spec.Env))
}

// --- helpers omitted (must, writeFile, readSpec, extractExitCode,
//     loadSeccompProfile, dropCapabilitiesTo) ---
```

That is the entire container runtime. ~300 lines if you write out the helpers. It supports one container per invocation, one cgroup, one set of namespaces. It does not handle image pull (you supply a rootfs), networking (you supply a netns path), or volumes (you bind-mount in the spec). It is the minimum viable runc.

### 6.3 What This Maps To in the Real World

This pseudocode is what `runc create` + `runc start` do, with a *lot* more error handling and the OCI runtime spec JSON. The real path through `runc` is:

```
runc create → libcontainer.Factory.Create() → process.go::start() →
   nsexec.c (CGo bootstrap in the new ns) → runc init →
   setupRootfs() → setupNetwork (almost nothing — kubelet/CNI does it) →
   finalizeNamespace() → execve()
```

Chapter 01 walks every step of that path. The `runc init` re-exec trick is exactly the `MINIK8S_CHILD=1` move above. The setup of `/proc`, `/sys`, `/dev`, the pivot_root sequence, the seccomp load — all match.

### 6.4 The Pod Sandbox Pattern

In Kubernetes a Pod is not one container; it is a *sandbox* (the pause container) plus N application containers that share the sandbox's namespaces. The pattern we use:

```
   Pod sandbox = a pause container that holds the network namespace
                 (and optionally pid, ipc, uts namespaces) for the pod.

   Pod sandbox creation:
       1. Start pause container — single process, sleeps forever.
       2. Capture /proc/<pause-pid>/ns/net into a saved path.
       3. Invoke mini-cni ADD against that netns path → veth + IP.

   App container creation (same pod):
       1. Start another container, but instead of CLONE_NEWNET,
          use setns(netns_fd, CLONE_NEWNET) on the saved path.
       2. Same for pid, ipc, uts if pod requests shareProcessNamespace.
       3. Mount the rootfs of the app image.
       4. Execve.
```

This is chapter 11 §pod-internals, the pause container §pod-sandbox, and chapter 10 §runtime-handler. Our `mini-runtime` will follow it.

---

## 7. Phase 2 — A CRI-Like Local gRPC

Phase 1 produced a runtime invokable on the command line. Phase 2 wraps it in a gRPC service so a separate `mini-kubelet` process can drive it. This is the CRI (Container Runtime Interface) boundary. Real CRI has ~30 RPC methods. Our minimal cut is 8.

### 7.1 The Eight-Method CRI

```protobuf
// File: minik8s/proto/cri.proto
syntax = "proto3";
package minik8s.cri.v1;

service RuntimeService {
    rpc RunPodSandbox(RunPodSandboxRequest) returns (RunPodSandboxResponse);
    rpc StopPodSandbox(StopPodSandboxRequest) returns (StopPodSandboxResponse);
    rpc RemovePodSandbox(RemovePodSandboxRequest) returns (RemovePodSandboxResponse);
    rpc PodSandboxStatus(PodSandboxStatusRequest) returns (PodSandboxStatusResponse);
    rpc CreateContainer(CreateContainerRequest) returns (CreateContainerResponse);
    rpc StartContainer(StartContainerRequest) returns (StartContainerResponse);
    rpc StopContainer(StopContainerRequest) returns (StopContainerResponse);
    rpc RemoveContainer(RemoveContainerRequest) returns (RemoveContainerResponse);
    rpc ContainerStatus(ContainerStatusRequest) returns (ContainerStatusResponse);
    rpc ListContainers(ListContainersRequest) returns (ListContainersResponse);
    rpc ListPodSandbox(ListPodSandboxRequest) returns (ListPodSandboxResponse);
}

service ImageService {
    rpc PullImage(PullImageRequest) returns (PullImageResponse);
    rpc ListImages(ListImagesRequest) returns (ListImagesResponse);
    rpc ImageStatus(ImageStatusRequest) returns (ImageStatusResponse);
}

message RunPodSandboxRequest {
    string pod_id   = 1;
    string namespace = 2;   // K8s namespace, not Linux namespace
    string name     = 3;
    map<string,string> labels = 4;
    PodNamespaceConfig ns_config = 5;
    string network_namespace_mode = 6; // "POD" or "NODE" (hostNetwork)
}

message PodNamespaceConfig {
    bool share_pid = 1;
    bool share_ipc = 2;
    bool share_net = 3; // usually true; false only for hostNetwork
}

message RunPodSandboxResponse {
    string pod_sandbox_id = 1;
    string netns_path     = 2;   // mini-cni will call ADD on this
}

message CreateContainerRequest {
    string pod_sandbox_id = 1;
    ContainerConfig config = 2;
}

message ContainerConfig {
    string name       = 1;
    Image  image      = 2;
    repeated string command = 3;
    repeated string args    = 4;
    repeated KeyValue env   = 5;
    repeated Mount mounts   = 6;
    LinuxContainerConfig linux = 7;
}

message LinuxContainerConfig {
    LinuxResources resources = 1;
    LinuxSecurityContext security_context = 2;
}

message LinuxResources {
    int64 cpu_period_us  = 1;
    int64 cpu_quota_us   = 2;
    int64 memory_limit   = 3;
    int64 pids_limit     = 4;
}

// ContainerStatus is the polled-status message — drives PLEG.
message ContainerStatus {
    string id     = 1;
    string state  = 2;   // CREATED, RUNNING, EXITED
    int64  started_at = 3;
    int64  finished_at = 4;
    int32  exit_code = 5;
    string reason = 6;
    string message = 7;
}
```

That is the contract. The implementation, on the `mini-runtime` side, is a thin server that:

1. **`RunPodSandbox`** — creates the pause container per phase 1 (just sleeps forever), saves `/proc/<pid>/ns/net` somewhere, returns the path.
2. **`CreateContainer`** — records the container config in a local map, returns an ID. Does *not* start the process yet (CRI semantics: create then start are separate so that the kubelet can attach hooks/probes).
3. **`StartContainer`** — runs phase 1 with the saved spec, attaching to the sandbox's netns via `setns`. Returns immediately; the started process is now PID `<some-host-pid>` and PID 1 inside.
4. **`ContainerStatus`** — checks `/proc/<pid>/status`, examines the exit code if dead. This is the polled signal that PLEG (chapter 10) consumes.

### 7.2 The In-Memory State

```go
// File: minik8s/runtime/server/state.go
type sandbox struct {
    id        string
    namespace string
    name      string
    pausePID  int
    netnsPath string
    createdAt time.Time
}

type container struct {
    id           string
    sandboxID    string
    config       *ContainerConfig
    state        string // CREATED|RUNNING|EXITED
    hostPID      int
    startedAt    time.Time
    finishedAt   time.Time
    exitCode     int
}

type RuntimeServer struct {
    mu         sync.Mutex
    sandboxes  map[string]*sandbox
    containers map[string]*container
    imageDir   string // where pre-extracted rootfs trees live
}
```

That is the whole runtime state. Every CRI method is twenty lines: lock, mutate map, return.

### 7.3 What This Maps To

Real containerd is a *much* fancier version of this. It has a content store (CAS for image layers — chapter 02), a snapshotter (overlayfs subvolume management — chapter 00 §9), a shim-per-pod (`containerd-shim-runc-v2`) that survives containerd restarts, a metadata DB (bbolt), and event subscription.

Our `mini-runtime` collapses all of that into:

- "Content store" → a directory of pre-extracted rootfs tarballs.
- "Snapshotter" → none (the rootfs is the image; mutations are confined inside the container's mount ns).
- "Shim" → none (the runtime process holds the children directly; on restart, all containers die — fine for a toy).
- "Metadata" → an in-memory map, with periodic snapshot to JSON for debugging.

You can see why containerd is ~150,000 lines and `mini-runtime` is ~400. The 400 demonstrates the CRI contract; the 150,000 makes it survivable.

---

## 8. Phase 3 — `minikv`: A Watchable KV Store

Now we leave the data plane and enter the control plane. The first piece is the storage layer: a key-value store with versioning and watch. This is the role etcd plays in real Kubernetes.

### 8.1 The Requirements

From chapter 04 (etcd internals):

- **Versioned puts**: each write gets a monotonically increasing `revision`.
- **Compare-and-swap**: writes can be conditioned on the current `modRevision` (this is what powers `resourceVersion` optimistic concurrency at the apiserver).
- **Range queries**: list all keys with a given prefix.
- **Watch**: stream all events since a given revision, including ones that occurred during the call (no client-visible "gap").
- **Compaction**: at some revision, drop the history. Clients watching from a compacted revision must get `410 Gone`.

We skip: Raft (single-node), leases (no TTL), text vs binary keys (always strings), transactions (only single-key CAS), authorization, gRPC (we use a Go-internal interface for simplicity — `mini-apiserver` is co-process or co-linked).

### 8.2 The Data Structure

```go
// File: minik8s/kv/store.go
package kv

import (
    "sync"
    "strings"
)

type Event struct {
    Type     string // PUT or DELETE
    Key      string
    Value    []byte
    Revision int64
    PrevRev  int64 // for DELETE, the rev at which the deleted value lived
}

type entry struct {
    value       []byte
    modRevision int64
    createRev   int64
}

type subscriber struct {
    prefix   string
    startRev int64
    ch       chan Event
    done     chan struct{}
}

type Store struct {
    mu          sync.RWMutex
    currentRev  int64
    compactRev  int64                  // <= this revision is no longer historical
    data        map[string]*entry
    history     []Event                // ring of recent events for watch catchup
    subscribers map[*subscriber]struct{}
    cond        *sync.Cond             // signaled on each Put/Delete
}

func New() *Store {
    s := &Store{
        data:        make(map[string]*entry),
        subscribers: make(map[*subscriber]struct{}),
    }
    s.cond = sync.NewCond(&s.mu)
    return s
}
```

### 8.3 Put, Get, Delete, Range

```go
// Put with optional CAS on modRevision. ifMatch=-1 disables the check.
func (s *Store) Put(key string, value []byte, ifMatch int64) (int64, error) {
    s.mu.Lock()
    defer s.mu.Unlock()

    cur, exists := s.data[key]
    if ifMatch >= 0 {
        if !exists && ifMatch != 0 {
            return 0, ErrPreconditionFailed
        }
        if exists && cur.modRevision != ifMatch {
            return 0, ErrPreconditionFailed
        }
    }

    s.currentRev++
    rev := s.currentRev
    prevRev := int64(0)
    if exists {
        prevRev = cur.modRevision
        cur.value = append([]byte(nil), value...)
        cur.modRevision = rev
    } else {
        s.data[key] = &entry{
            value:       append([]byte(nil), value...),
            modRevision: rev,
            createRev:   rev,
        }
    }

    ev := Event{Type: "PUT", Key: key, Value: value, Revision: rev, PrevRev: prevRev}
    s.history = append(s.history, ev)
    s.trimHistory()
    s.cond.Broadcast()
    return rev, nil
}

func (s *Store) Get(key string) ([]byte, int64, bool) {
    s.mu.RLock()
    defer s.mu.RUnlock()
    e, ok := s.data[key]
    if !ok { return nil, 0, false }
    return append([]byte(nil), e.value...), e.modRevision, true
}

func (s *Store) Delete(key string, ifMatch int64) (int64, error) {
    s.mu.Lock()
    defer s.mu.Unlock()
    cur, exists := s.data[key]
    if !exists {
        return 0, ErrNotFound
    }
    if ifMatch >= 0 && cur.modRevision != ifMatch {
        return 0, ErrPreconditionFailed
    }
    s.currentRev++
    rev := s.currentRev
    delete(s.data, key)
    ev := Event{Type: "DELETE", Key: key, Revision: rev, PrevRev: cur.modRevision}
    s.history = append(s.history, ev)
    s.trimHistory()
    s.cond.Broadcast()
    return rev, nil
}

// Range returns all values under prefix and the current revision (snapshot).
func (s *Store) Range(prefix string) ([]kvPair, int64) {
    s.mu.RLock()
    defer s.mu.RUnlock()
    out := []kvPair{}
    for k, e := range s.data {
        if strings.HasPrefix(k, prefix) {
            out = append(out, kvPair{
                Key: k, Value: append([]byte(nil), e.value...),
                ModRevision: e.modRevision, CreateRevision: e.createRev,
            })
        }
    }
    return out, s.currentRev
}
```

### 8.4 Watch

```go
// Watch subscribes to events on a prefix starting from startRev (exclusive).
// If startRev <= compactRev, returns ErrCompacted (apiserver maps to HTTP 410).
func (s *Store) Watch(prefix string, startRev int64) (<-chan Event, func(), error) {
    s.mu.Lock()
    if startRev > 0 && startRev <= s.compactRev {
        s.mu.Unlock()
        return nil, nil, ErrCompacted
    }
    sub := &subscriber{
        prefix:   prefix,
        startRev: startRev,
        ch:       make(chan Event, 256),
        done:     make(chan struct{}),
    }
    s.subscribers[sub] = struct{}{}

    // Catchup: replay events from history that are > startRev and match prefix.
    // (history is sorted by rev.)
    for _, ev := range s.history {
        if ev.Revision > startRev && strings.HasPrefix(ev.Key, prefix) {
            // Non-blocking send is wrong here — we MUST deliver. If the buffer
            // is full, we close the sub and force the client to reconnect with
            // a fresh startRev (this is how watch backpressure works in etcd).
            select {
            case sub.ch <- ev:
            default:
                close(sub.ch)
                delete(s.subscribers, sub)
                s.mu.Unlock()
                return nil, nil, ErrSlowConsumer
            }
        }
    }
    s.mu.Unlock()

    // Background fan-out: a goroutine that listens on cond and forwards
    // new events to this sub. Simplified single-goroutine version:
    go s.subscriberLoop(sub)

    cancel := func() {
        s.mu.Lock()
        defer s.mu.Unlock()
        if _, ok := s.subscribers[sub]; ok {
            delete(s.subscribers, sub)
            close(sub.done)
        }
    }
    return sub.ch, cancel, nil
}

func (s *Store) subscriberLoop(sub *subscriber) {
    s.mu.Lock()
    seen := s.currentRev
    for {
        // Wait until either a new event or cancellation.
        for s.currentRev == seen {
            select {
            case <-sub.done:
                s.mu.Unlock()
                return
            default:
            }
            s.cond.Wait()
        }
        // Drain history from seen+1 to currentRev.
        for _, ev := range s.history {
            if ev.Revision > seen && strings.HasPrefix(ev.Key, sub.prefix) {
                select {
                case sub.ch <- ev:
                default:
                    // Slow consumer: drop sub.
                    delete(s.subscribers, sub)
                    close(sub.ch)
                    s.mu.Unlock()
                    return
                }
            }
        }
        seen = s.currentRev
    }
}

func (s *Store) Compact(rev int64) {
    s.mu.Lock()
    defer s.mu.Unlock()
    if rev > s.compactRev {
        s.compactRev = rev
        // Drop history entries <= rev.
        cut := 0
        for i, ev := range s.history {
            if ev.Revision > rev { cut = i; break }
        }
        s.history = s.history[cut:]
    }
}
```

### 8.5 What This Maps To

This is ~300 lines and exposes the *exact* mental model etcd v3 uses: monotonic revision, prefix range, watch-with-catchup, compaction-with-410. The differences:

- **Persistence**: etcd uses bbolt (a B+Tree on disk) plus a WAL. Our store is in-memory with a periodic snapshot.
- **Distribution**: etcd is Raft-replicated. We have one node.
- **Performance**: etcd's MVCC has a sophisticated revision-indexed B+Tree; we walk the map.
- **Lease / TTL**: etcd has leases for events and node heartbeats (chapter 10 §node-lease). We omit.

But the contract — the surface the apiserver consumes — is identical. If you ported `mini-apiserver` from `minikv` to etcd v3, the apiserver code would be ~30 lines different (gRPC client instead of in-process call).

This is the storage layer where chapters 04 (etcd) and 35 (perf — apiserver watch cache) live in our model. The "watch cache" we will *not* build separately because we are single-process; in real K8s the apiserver maintains a per-resource cache to avoid hammering etcd with N watch streams. For `minik8s` the apiserver and kv are in the same process, so the watch is the cache.

---

## 9. Phase 4 — `mini-apiserver`: REST Over Typed Objects

Now we expose `minikv` as REST with typed objects. Every Kubernetes object follows the same shape:

```
type Object struct {
    TypeMeta   { Kind, APIVersion }
    ObjectMeta { Name, Namespace, UID, ResourceVersion, Labels, Annotations,
                 CreationTimestamp, DeletionTimestamp, Finalizers, OwnerReferences }
    Spec       <type-specific>
    Status     <type-specific>
}
```

`Spec` is what the user declares; `Status` is what controllers observe. The apiserver's job is to (1) route requests to the right resource handler, (2) run an admission chain, (3) validate, (4) persist to the KV with CAS on `ResourceVersion`, (5) hand back the persisted object, (6) fan watch events out.

### 9.1 Routing Table and Type Registry

```go
// File: minik8s/apiserver/registry.go
type ResourceInfo struct {
    Group     string
    Version   string
    Kind      string
    Plural    string         // "pods", "deployments"
    Namespaced bool
    Prototype func() Object   // returns a fresh empty object of this type
    Validate  func(Object) error
    Default   func(Object)
}

var registry = map[string]*ResourceInfo{} // key = "group/version/plural"

func register(r *ResourceInfo) { registry[key(r.Group, r.Version, r.Plural)] = r }

func init() {
    register(&ResourceInfo{
        Group: "", Version: "v1", Kind: "Pod", Plural: "pods", Namespaced: true,
        Prototype: func() Object { return &Pod{} },
        Validate:  validatePod,
        Default:   defaultPod,
    })
    register(&ResourceInfo{
        Group: "", Version: "v1", Kind: "Node", Plural: "nodes", Namespaced: false,
        Prototype: func() Object { return &Node{} },
        Validate:  validateNode,
    })
    register(&ResourceInfo{
        Group: "", Version: "v1", Kind: "Service", Plural: "services", Namespaced: true,
        Prototype: func() Object { return &Service{} },
        Validate:  validateService,
    })
    register(&ResourceInfo{
        Group: "", Version: "v1", Kind: "Endpoints", Plural: "endpoints", Namespaced: true,
        Prototype: func() Object { return &Endpoints{} },
    })
    register(&ResourceInfo{
        Group: "apps", Version: "v1", Kind: "Deployment", Plural: "deployments",
        Namespaced: true,
        Prototype: func() Object { return &Deployment{} },
        Validate:  validateDeployment,
    })
    register(&ResourceInfo{
        Group: "apps", Version: "v1", Kind: "ReplicaSet", Plural: "replicasets",
        Namespaced: true,
        Prototype: func() Object { return &ReplicaSet{} },
    })
}
```

### 9.2 The Handler Chain

```
   Request comes in:
       1.  Route parse       → resource info, namespace, name, verb
       2.  AuthN (stub)      → user identity (always "system:masters")
       3.  AuthZ (stub)      → allow all
       4.  Decode body       → typed object
       5.  Defaulting        → fill in zero fields per spec
       6.  Admission chain   → built-in mutators
       7.  Validation        → structural + semantic
       8.  Storage           → minikv Put with ifMatch=ResourceVersion
       9.  Encode response   → JSON
      10.  (Watch fan-out happens automatically because minikv broadcasts)
```

Implementation skeleton:

```go
// File: minik8s/apiserver/handler.go
func (s *APIServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
    info, name, ns, verb, err := parsePath(r.URL.Path, r.Method)
    if err != nil { http.Error(w, err.Error(), 404); return }

    user, err := s.authn(r)        // step 2
    if err != nil { http.Error(w, "unauth", 401); return }
    if !s.authz(user, verb, info, ns) {
        http.Error(w, "forbidden", 403); return
    }

    switch verb {
    case "get":     s.handleGet(w, r, info, ns, name)
    case "list":    s.handleList(w, r, info, ns)
    case "watch":   s.handleWatch(w, r, info, ns)
    case "create":  s.handleCreate(w, r, info, ns)
    case "update":  s.handleUpdate(w, r, info, ns, name)
    case "patch":   s.handlePatch(w, r, info, ns, name)
    case "delete":  s.handleDelete(w, r, info, ns, name)
    default:        http.Error(w, "method", 405)
    }
}

func (s *APIServer) handleCreate(w http.ResponseWriter, r *http.Request, info *ResourceInfo, ns string) {
    obj := info.Prototype()
    if err := json.NewDecoder(r.Body).Decode(obj); err != nil {
        http.Error(w, "bad body", 400); return
    }

    // Defaulting
    if info.Default != nil { info.Default(obj) }

    // Inline admission: assign UID, set creationTimestamp, set RV=0 for ifMatch.
    meta := obj.GetObjectMeta()
    meta.UID = uuid.New().String()
    meta.CreationTimestamp = time.Now().UTC().Format(time.RFC3339)
    meta.ResourceVersion = "" // server assigns
    if info.Namespaced { meta.Namespace = ns }

    // Run mutating admission (built-ins only — no webhooks in minik8s)
    for _, m := range s.mutators {
        if err := m.Mutate(obj); err != nil {
            http.Error(w, "admit:"+err.Error(), 422); return
        }
    }

    // Validation
    if info.Validate != nil {
        if err := info.Validate(obj); err != nil {
            http.Error(w, "validate:"+err.Error(), 422); return
        }
    }

    key := storageKey(info, ns, meta.Name) // /registry/pods/default/foo
    bytes, _ := json.Marshal(obj)
    rev, err := s.kv.Put(key, bytes, 0) // 0 = expect not-exists
    if err == kv.ErrPreconditionFailed {
        http.Error(w, "already exists", 409); return
    } else if err != nil {
        http.Error(w, "internal", 500); return
    }
    meta.ResourceVersion = fmt.Sprintf("%d", rev)
    bytes, _ = json.Marshal(obj) // re-encode with RV set
    w.Header().Set("Content-Type", "application/json")
    w.WriteHeader(201)
    w.Write(bytes)
}

func (s *APIServer) handleUpdate(w http.ResponseWriter, r *http.Request, info *ResourceInfo, ns, name string) {
    incoming := info.Prototype()
    json.NewDecoder(r.Body).Decode(incoming)
    meta := incoming.GetObjectMeta()
    if meta.ResourceVersion == "" {
        http.Error(w, "missing RV", 409); return
    }
    rv, _ := strconv.ParseInt(meta.ResourceVersion, 10, 64)
    // Defaulting + admission + validation as above.
    // ...

    key := storageKey(info, ns, name)
    bytes, _ := json.Marshal(incoming)
    rev, err := s.kv.Put(key, bytes, rv)
    if err == kv.ErrPreconditionFailed {
        http.Error(w, "conflict", 409); return
    }
    meta.ResourceVersion = fmt.Sprintf("%d", rev)
    // Encode back to client.
}
```

That's about 150 lines covering five verbs. The remaining ~450 lines of `mini-apiserver` are:

- **Validation rules** — type-specific (`validatePod`, `validateService`, etc.), ~30 lines each.
- **Defaulters** — assign `restartPolicy=Always`, `terminationGracePeriodSeconds=30`, ClusterIP allocation.
- **The path parser** (`/api/v1/...` vs `/apis/<group>/<version>/...`).
- **Discovery** — `/api`, `/apis`, `/api/v1` endpoints listing what's available (so `kubectl-mini` can do introspection).
- **The watch handler** (next section).

### 9.3 The `resourceVersion` Contract

This is where most apiserver subtlety lives. The rules `mini-apiserver` enforces:

1. **Listing returns a `resourceVersion`** — the highest revision at the moment of the list snapshot. This is the `metadata.resourceVersion` on the *list*, not on individual items. (Per-item RVs are different and reflect when that item last changed.)
2. **A watch with `resourceVersion=X`** delivers events starting *strictly after* revision `X`. The list-then-watch sequence (list at RV=L, then watch from L) is gap-free because watch's start-exclusive matches list's snapshot-at-L.
3. **A watch with no RV** is "start from now" — only new events.
4. **A watch with `resourceVersion=0`** means "start from any point you have; I just want to receive any events you've got" — useful for informers that don't care about catchup.
5. **A compacted RV** returns `410 Gone`. Clients must re-list and re-watch.

Chapter 05 (apiserver) §watch-semantics walks every edge case. Chapter 08 (controller pattern) §relist-on-error explains how informers handle the `410 Gone`.

### 9.4 Optimistic Concurrency

Every write path goes through `kv.Put(key, body, ifMatchRV)`. If two clients PUT the same object concurrently, one wins and the other gets HTTP 409 Conflict. The client is expected to (a) re-GET to see the current state, (b) re-apply its intent, (c) retry the PUT with the new RV.

This is *the* concurrency model of Kubernetes. Controllers don't lock objects; they observe, decide, write with CAS, and retry on conflict. The only thing that needs distributed locking (leader election among multiple controller-manager replicas) uses a `Lease` object with the same CAS mechanism.

---

## 10. Phase 5 — The Watch Endpoint

The watch endpoint is what makes Kubernetes *eventual but bounded*: every interested client subscribes once, then receives a continuous stream of events. Without watch, every controller would poll, the apiserver would melt, and the cluster would scale to ~5 nodes.

### 10.1 The Wire Format

Real Kubernetes uses HTTP/2 with a chunked JSON stream (one event per chunk). The body is:

```
{"type":"ADDED","object":{...full object...}}
{"type":"MODIFIED","object":{...}}
{"type":"DELETED","object":{...}}
{"type":"BOOKMARK","object":{"metadata":{"resourceVersion":"12345"}}}
{"type":"ERROR","object":{"status":"Failure","code":410,"reason":"Gone"}}
```

We will use the same format, just over HTTP/1.1 chunked transfer encoding (HTTP/2 is a deployment detail).

### 10.2 The Handler

```go
func (s *APIServer) handleWatch(w http.ResponseWriter, r *http.Request, info *ResourceInfo, ns string) {
    rvStr := r.URL.Query().Get("resourceVersion")
    startRev, _ := strconv.ParseInt(rvStr, 10, 64)
    prefix := storagePrefix(info, ns)

    flusher, ok := w.(http.Flusher)
    if !ok { http.Error(w, "no streaming", 500); return }

    ch, cancel, err := s.kv.Watch(prefix, startRev)
    if err == kv.ErrCompacted {
        // Emit a single ERROR event, then close.
        w.Header().Set("Content-Type", "application/json")
        w.WriteHeader(200)
        emitErr(w, 410, "Gone", "the resourceVersion is too old")
        flusher.Flush()
        return
    } else if err != nil {
        http.Error(w, err.Error(), 500); return
    }
    defer cancel()

    w.Header().Set("Content-Type", "application/json")
    w.Header().Set("Transfer-Encoding", "chunked")
    w.WriteHeader(200)

    ticker := time.NewTicker(30 * time.Second) // bookmark interval
    defer ticker.Stop()

    for {
        select {
        case <-r.Context().Done():
            return
        case <-ticker.C:
            // Send a BOOKMARK event — tells client the latest RV they're synced to.
            emitBookmark(w, s.kv.CurrentRev())
            flusher.Flush()
        case ev, ok := <-ch:
            if !ok {
                // Slow consumer or store closed — emit 410 and bail.
                emitErr(w, 410, "Gone", "watch closed: slow consumer or compaction")
                flusher.Flush()
                return
            }
            obj := info.Prototype()
            if ev.Type == "PUT" || ev.Type == "DELETE" {
                if ev.Type == "DELETE" {
                    // For DELETE we need the prior value — could maintain a side
                    // map of pre-delete snapshots. For brevity, emit minimal.
                    json.Unmarshal(ev.Value, obj) // may be empty
                } else {
                    json.Unmarshal(ev.Value, obj)
                }
                obj.GetObjectMeta().ResourceVersion = fmt.Sprintf("%d", ev.Revision)
            }
            kind := map[string]string{"PUT": "MODIFIED", "DELETE": "DELETED"}[ev.Type]
            if ev.PrevRev == 0 && ev.Type == "PUT" {
                kind = "ADDED"
            }
            emitEvent(w, kind, obj)
            flusher.Flush()
        }
    }
}
```

### 10.3 The 410 Gone Contract

Chapter 05 explains why this matters. If a client falls behind (its `resourceVersion` is older than the apiserver's compaction point), there is no way to deliver events from `[oldRV, currentRV)` because they have been dropped. The protocol is:

```
   Client       Apiserver
     │ GET .../pods?watch=true&resourceVersion=12345
     ▼
                Look up watch from rev 12345
                If compactRev >= 12345 → 410 Gone in stream
     ◄────────  ERROR event {code: 410}
     │ Connection stays open until client closes it (or closes immediately
     │ depending on impl). Client recovers by:
     │
     ▼
   1. LIST the resource at the current RV
   2. Reconcile local cache: items not in LIST → delete; items in LIST → upsert
   3. WATCH from the LIST's resourceVersion
```

The informer in chapter 08 implements this exact recovery loop. Our `mini-informer` will too.

### 10.4 Bookmarks

Every ~30s we send a `BOOKMARK` event whose only payload is the current `resourceVersion`. The reason: a client that's been idle for 10 minutes (no events on its prefix) needs to advance its RV so that when it eventually reconnects, it doesn't ask for an RV that's been compacted. Bookmarks let watchers progress without traffic.

Chapter 05 §watch-bookmarks describes how this saves enormous amounts of work at scale: a controller watching `Events` in a quiet cluster would otherwise have its RV stay frozen and need to re-list on every reconnect.

---

## 11. Phase 6 — `mini-kubelet`: The Node Agent

Now we cross back to the node. The kubelet is the *only* component that runs containers; everything else is a controller that scribbles objects.

### 11.1 The syncLoop

The kubelet has one main loop that consumes pod state changes from several sources, merges them, and reconciles to the runtime.

```
                       ┌────────────────────────────────┐
                       │     syncLoop (single goroutine)│
                       └──────────────┬─────────────────┘
                                      │
        ┌─────────────────────────────┼──────────────────────────────┐
        ▼                             ▼                              ▼
   ┌──────────┐               ┌──────────────┐               ┌──────────────┐
   │ apiserver│               │ PLEG (runtime│               │ probe results│
   │  watch   │               │  state poll) │               │ (readiness,  │
   │  events  │               │              │               │  liveness)   │
   └──────────┘               └──────────────┘               └──────────────┘
        │                             │                              │
        └────────────┬────────────────┴──────────────┬───────────────┘
                     ▼                               ▼
              ┌────────────┐                  ┌──────────────┐
              │ pod worker │ ── per pod ──►   │ pod worker   │
              └─────┬──────┘                  └──────┬───────┘
                    │                                │
                    ▼ syncPod(pod)                   ▼
              ┌────────────────────────────────────────────────┐
              │ 1. ensure sandbox (RunPodSandbox if needed)    │
              │ 2. ensure each container (Create+Start)        │
              │ 3. write pod.status back via apiserver         │
              └────────────────────────────────────────────────┘
```

This is chapter 10 in one diagram. The PLEG (Pod Lifecycle Event Generator) is a goroutine that polls the runtime every second, computes a diff against the last poll, and emits events of the form `(podID, containerID, oldState, newState)`. The pod worker for that pod consumes those events and recomputes the desired actions.

### 11.2 The Pod Cache and Pod Worker

```go
// File: minik8s/kubelet/kubelet.go
type Kubelet struct {
    nodeName    string
    apiserver   *Client          // typed client to mini-apiserver
    runtime     cri.RuntimeClient
    cni         *CNIInvoker      // exec'd binary
    podCache    *PodCache        // desired state, indexed by UID
    runtimeCache *RuntimeCache   // last-observed runtime state from PLEG
    workers     map[types.UID]*podWorker
    statusMgr   *StatusManager
    probeMgr    *ProbeManager
}

type podWorker struct {
    pod        *Pod
    inbox      chan workEntry
    syncing    bool
}

type workEntry struct {
    kind   string // "sync" | "kill" | "update"
    update *Pod
}

func (k *Kubelet) Run() {
    // 1. Watch Pods scoped to this node.
    go k.watchAssignedPods()
    // 2. Run PLEG.
    go k.runPLEG()
    // 3. Run the status manager (batches status updates to apiserver).
    go k.statusMgr.Run()
    // 4. Run the probe manager.
    go k.probeMgr.Run()
    // 5. syncLoop fans events to per-pod workers.
    for {
        select {
        case ev := <-k.podCache.events:
            k.dispatch(ev.uid, workEntry{kind: "update", update: ev.pod})
        case ev := <-k.runtimeCache.events:
            // PLEG transition; re-sync the affected pod.
            k.dispatch(ev.podUID, workEntry{kind: "sync"})
        case ev := <-k.probeMgr.results:
            k.statusMgr.updateContainerReadiness(ev.podUID, ev.containerName, ev.ready)
        }
    }
}

func (k *Kubelet) dispatch(uid types.UID, w workEntry) {
    pw, ok := k.workers[uid]
    if !ok {
        pw = &podWorker{inbox: make(chan workEntry, 8)}
        k.workers[uid] = pw
        go k.runPodWorker(pw)
    }
    select {
    case pw.inbox <- w:
    default:
        // Coalesce: if a sync is already queued, no need to enqueue another.
    }
}
```

### 11.3 syncPod

The heart of the kubelet:

```go
func (k *Kubelet) syncPod(pod *Pod) error {
    // 1. Look up runtime state.
    sandbox, _ := k.runtime.GetSandboxByPodID(pod.UID)
    runningContainers, _ := k.runtime.ListContainers(pod.UID)

    // 2. Sandbox: create if missing.
    if sandbox == nil {
        resp, err := k.runtime.RunPodSandbox(&cri.RunPodSandboxRequest{
            PodID: pod.UID, Namespace: pod.Namespace, Name: pod.Name,
            NsConfig: derivePodNs(pod),
        })
        if err != nil { return err }
        sandbox = &cri.Sandbox{ID: resp.PodSandboxID, NetnsPath: resp.NetnsPath}

        // 3. Invoke CNI ADD on the new sandbox netns.
        podIP, err := k.cni.Add(pod, sandbox.NetnsPath)
        if err != nil {
            k.runtime.StopPodSandbox(sandbox.ID)
            k.runtime.RemovePodSandbox(sandbox.ID)
            return err
        }
        pod.Status.PodIP = podIP
    }

    // 4. For each desired container in pod.Spec.Containers + InitContainers:
    desired := pod.Spec.InitContainers
    desired = append(desired, pod.Spec.Containers...)
    have := indexByName(runningContainers)
    for _, c := range desired {
        rc, exists := have[c.Name]
        if !exists {
            // Create + start.
            crResp, _ := k.runtime.CreateContainer(&cri.CreateContainerRequest{
                PodSandboxID: sandbox.ID,
                Config:       containerSpec(pod, &c),
            })
            k.runtime.StartContainer(&cri.StartContainerRequest{ContainerID: crResp.ContainerID})
            continue
        }
        if rc.State == "EXITED" && pod.Spec.RestartPolicy == "Always" {
            k.runtime.RemoveContainer(rc.ID)
            crResp, _ := k.runtime.CreateContainer(&cri.CreateContainerRequest{
                PodSandboxID: sandbox.ID, Config: containerSpec(pod, &c),
            })
            k.runtime.StartContainer(&cri.StartContainerRequest{ContainerID: crResp.ContainerID})
        }
    }

    // 5. Containers that are running but not in desired: stop & remove.
    desiredNames := nameSet(desired)
    for name, rc := range have {
        if !desiredNames[name] {
            k.runtime.StopContainer(rc.ID, 30)
            k.runtime.RemoveContainer(rc.ID)
        }
    }

    // 6. Update pod.status.
    k.statusMgr.SetPodStatus(pod, computeStatus(pod, sandbox, runningContainers))
    return nil
}
```

This is ~80 lines that demonstrate every essential kubelet behavior. The real kubelet has thousands of lines because it also handles:

- Init container ordering (must complete before app containers start) — chapter 11.
- Lifecycle hooks (`preStop`, `postStart`) — chapter 11.
- Volume mounts (chapter 19).
- Image pull (chapter 02).
- CPU/Memory/Topology managers (chapter 21).
- Device plugin allocation (chapter 21).
- Probe execution (chapter 11).
- Eviction (chapter 21).
- Log rotation (chapter 30).

Each of those is *additive*. Drop them in and `syncPod` keeps the same shape.

### 11.4 PLEG

```go
// File: minik8s/kubelet/pleg.go
type PLEG struct {
    runtime  cri.RuntimeClient
    lastObs  map[string]map[string]string // podID → containerID → state
    interval time.Duration
    events   chan plegEvent
}

func (p *PLEG) Run() {
    for range time.Tick(p.interval) {
        pods, _ := p.runtime.ListPodSandbox()
        newObs := map[string]map[string]string{}
        for _, pod := range pods {
            containers, _ := p.runtime.ListContainers(pod.ID)
            states := map[string]string{}
            for _, c := range containers {
                states[c.ID] = c.State
            }
            newObs[pod.ID] = states
        }
        // Diff with lastObs and emit events.
        for podID, states := range newObs {
            prev := p.lastObs[podID]
            for cid, st := range states {
                if prev[cid] != st {
                    p.events <- plegEvent{
                        podUID: podID, containerID: cid,
                        oldState: prev[cid], newState: st,
                    }
                }
            }
            // Containers gone.
            for cid := range prev {
                if _, ok := states[cid]; !ok {
                    p.events <- plegEvent{podUID: podID, containerID: cid, newState: "REMOVED"}
                }
            }
        }
        p.lastObs = newObs
    }
}
```

The "relisting" pattern is exactly what real PLEG does. Chapter 10 §PLEG explains why this design (polling+diff) is preferred over event-driven (every CRI runtime would need to emit events; the OCI contract has none). The downside is the 1-second polling latency before the kubelet notices that a container crashed.

### 11.5 Status Manager

The kubelet batches `status` updates:

```go
type StatusManager struct {
    pending map[types.UID]*PodStatus
    api     *Client
    mu      sync.Mutex
}

func (s *StatusManager) Run() {
    for range time.Tick(100 * time.Millisecond) {
        s.mu.Lock()
        batch := s.pending
        s.pending = map[types.UID]*PodStatus{}
        s.mu.Unlock()
        for uid, status := range batch {
            pod, _ := s.api.GetPodByUID(uid)
            if pod == nil { continue } // pod deleted
            if statusEqual(pod.Status, *status) { continue } // no change
            pod.Status = *status
            s.api.UpdatePodStatus(pod) // PATCH /api/v1/.../pods/<name>/status
        }
    }
}
```

The batching exists because real clusters have 30 status fields that flap every probe interval. Without batching, the apiserver gets one PATCH per probe per pod. With batching plus equality check, it gets one PATCH per *meaningful* change. Chapter 35 §apiserver-load explains why this is the single largest source of write load on a real apiserver.

### 11.6 What This Maps To

The real kubelet is in `pkg/kubelet/` and is ~150,000 lines. Our `mini-kubelet` is ~700. The 200× ratio is the missing managers (CPU/Memory/Topology/Device), volume manager (chapter 19), probe protocol implementations (HTTP, TCP, gRPC, exec — chapter 11), eviction with all its thresholds (chapter 21 §eviction), image GC (chapter 02 §image-gc), node lease management (chapter 04 §lease), the kubelet HTTP server for exec/attach/portforward (chapter 30 §kubelet-api), runtime version negotiation, feature gates, and ~30,000 lines of error handling for things that go wrong on real systems.

But the *shape* — syncLoop, PLEG, pod workers, status manager — is unchanged. If you read those four files in `pkg/kubelet/` you will recognize every structure from our 700-line version.

---

## 12. Phase 7 — `mini-scheduler`: Filter, Score, Bind

The scheduler is the simplest core component. It watches unbound pods, picks a node, and PATCHes the pod with `spec.nodeName`. That is the entire job.

### 12.1 The Scheduling Cycle

```
   Pod arrives with spec.nodeName == "":
       1. PreFilter: collect candidate set = all nodes
       2. Filter:    for each node, run feasibility checks in parallel
                       - NodeName matches if set
                       - Has enough CPU/memory (sum existing pods' requests)
                       - Tolerates taints (skipped in minik8s)
                     drop infeasible nodes
       3. Score:     for each remaining node, score 0..100
                       - least-allocated: prefer nodes with more free capacity
                     pick max score (break ties by name for determinism)
       4. Reserve:   speculatively account the pod's resources on chosen node
       5. Bind:      PATCH /api/v1/namespaces/<ns>/pods/<name>/binding
                     with spec.nodeName = chosen
                     (binding is a subresource so it doesn't conflict with
                      kubelet's status PATCHes)
```

### 12.2 The Pseudocode

```go
// File: minik8s/scheduler/scheduler.go
type Scheduler struct {
    api       *Client
    podLister cache.Lister
    nodeLister cache.Lister
    queue     workqueue.RateLimitingInterface
    cache     *schedulerCache // running tally of resources per node
}

func (s *Scheduler) Run(ctx context.Context) {
    // Informers for pods and nodes.
    podInf := newPodInformer(s.api, "")
    nodeInf := newNodeInformer(s.api)
    podInf.AddEventHandler(cache.ResourceEventHandlerFuncs{
        AddFunc: func(obj interface{}) {
            pod := obj.(*Pod)
            if pod.Spec.NodeName == "" {
                s.queue.Add(key(pod))
            }
        },
        UpdateFunc: func(old, new interface{}) {
            np := new.(*Pod)
            if np.Spec.NodeName == "" { s.queue.Add(key(np)) }
        },
    })
    go podInf.Run(ctx)
    go nodeInf.Run(ctx)
    podInf.WaitForCacheSync(ctx)
    nodeInf.WaitForCacheSync(ctx)

    // Worker.
    for {
        k, quit := s.queue.Get()
        if quit { return }
        if err := s.scheduleOne(k); err != nil {
            s.queue.AddRateLimited(k)
        } else {
            s.queue.Forget(k)
        }
        s.queue.Done(k)
    }
}

func (s *Scheduler) scheduleOne(podKey string) error {
    pod, err := s.podLister.Get(podKey)
    if err != nil || pod == nil { return nil }
    if pod.Spec.NodeName != "" { return nil } // already scheduled

    // 1. Snapshot nodes.
    nodes, _ := s.nodeLister.List()

    // 2. Filter.
    feasible := []*Node{}
    for _, n := range nodes {
        if !s.fits(pod, n) { continue }
        feasible = append(feasible, n)
    }
    if len(feasible) == 0 {
        s.recordEvent(pod, "FailedScheduling", "no feasible nodes")
        return errNoFit
    }

    // 3. Score.
    bestScore := int64(-1)
    var best *Node
    for _, n := range feasible {
        sc := s.score(pod, n)
        if sc > bestScore || (sc == bestScore && (best == nil || n.Name < best.Name)) {
            bestScore = sc
            best = n
        }
    }

    // 4. Reserve.
    s.cache.assume(pod, best.Name)

    // 5. Bind via subresource.
    binding := &Binding{
        Metadata: ObjectMeta{Name: pod.Name, Namespace: pod.Namespace},
        Target:   ObjectReference{Kind: "Node", Name: best.Name},
    }
    if err := s.api.CreateBinding(binding); err != nil {
        s.cache.forget(pod, best.Name)
        return err
    }
    return nil
}

func (s *Scheduler) fits(pod *Pod, node *Node) bool {
    if pod.Spec.NodeName != "" && pod.Spec.NodeName != node.Name { return false }
    used := s.cache.usedOnNode(node.Name)
    needCPU, needMem := totalRequests(pod)
    return used.cpu+needCPU <= node.Status.Allocatable.CPU &&
        used.memory+needMem <= node.Status.Allocatable.Memory
}

func (s *Scheduler) score(pod *Pod, node *Node) int64 {
    // Least allocated: 100 - (utilization%).
    used := s.cache.usedOnNode(node.Name)
    cpuUtil := (used.cpu * 100) / node.Status.Allocatable.CPU
    memUtil := (used.memory * 100) / node.Status.Allocatable.Memory
    return (200 - cpuUtil - memUtil) / 2
}
```

### 12.3 The Binding Subresource

On the apiserver side we need a new route:

```
POST /api/v1/namespaces/{ns}/pods/{name}/binding
   body: {"target": {"kind": "Node", "name": "node-1"}}
   semantics: server PATCHes spec.nodeName, does not return the pod
```

This is exactly how real Kubernetes does it. The reason it's a subresource: a `bind` action is conceptually different from a generic `update` to the pod (different RBAC, different validation, atomic write to one specific field). Chapter 09 §binding-vs-update covers the rationale.

### 12.4 What We Don't Do

The real scheduler has the Scheduler Framework (chapter 09) with ~20 plugin types and ~30 built-in plugins: `NodeAffinity`, `NodePorts`, `VolumeBinding`, `PodTopologySpread`, `InterPodAffinity`, `TaintToleration`, `ImageLocality`, etc. We have two: feasibility-by-resources and score-by-least-allocated. Each real plugin is ~200-500 lines and slots into the same `fits`/`score` extension points we have. Adding `TaintToleration` to `mini-scheduler` is a one-day project: add tolerations to the pod spec, taints to the node spec, evaluate during `fits`.

Preemption (chapter 09 §preemption), gang scheduling (chapter 34 §volcano), scheduling gates (chapter 09 §gates), the descheduler (chapter 09 §descheduler) — all are layered on. None changes the fundamental loop.

---

## 13. Phase 8 — `mini-controller-manager`: Informers and Reconcilers

The controller manager is multiple controllers in one binary. Each controller follows the same pattern: list+watch, cache, workqueue, reconcile. We build the pattern library once and instantiate it three times: Deployment, ReplicaSet, Endpoints.

### 13.1 The Informer

```go
// File: minik8s/lib/informer/informer.go
type Informer struct {
    lister   func() ([]Object, string, error)         // returns objects + resourceVersion
    watcher  func(rv string) (<-chan Event, error)
    cache    *ThreadSafeStore                          // namespace/name → object
    handlers []ResourceEventHandler
    indexer  *Indexer                                  // optional secondary indices
    rvLatest string
    synced   chan struct{}
}

type ResourceEventHandler interface {
    OnAdd(obj Object)
    OnUpdate(old, new Object)
    OnDelete(obj Object)
}

func (inf *Informer) Run(ctx context.Context) {
    for {
        // 1. LIST.
        objs, rv, err := inf.lister()
        if err != nil { time.Sleep(time.Second); continue }
        inf.cache.Replace(objs)
        inf.rvLatest = rv
        for _, o := range objs {
            for _, h := range inf.handlers { h.OnAdd(o) }
        }
        close(inf.synced) // first sync done

        // 2. WATCH.
        ch, err := inf.watcher(rv)
        if err != nil { continue }
        for ev := range ch {
            switch ev.Type {
            case "ADDED":
                if existing, ok := inf.cache.Get(key(ev.Object)); ok {
                    inf.cache.Update(ev.Object)
                    for _, h := range inf.handlers { h.OnUpdate(existing, ev.Object) }
                } else {
                    inf.cache.Add(ev.Object)
                    for _, h := range inf.handlers { h.OnAdd(ev.Object) }
                }
                inf.rvLatest = rvOf(ev.Object)
            case "MODIFIED":
                old, _ := inf.cache.Get(key(ev.Object))
                inf.cache.Update(ev.Object)
                for _, h := range inf.handlers { h.OnUpdate(old, ev.Object) }
                inf.rvLatest = rvOf(ev.Object)
            case "DELETED":
                inf.cache.Delete(key(ev.Object))
                for _, h := range inf.handlers { h.OnDelete(ev.Object) }
                inf.rvLatest = rvOf(ev.Object)
            case "ERROR":
                // 410 Gone — relist from scratch.
                inf.synced = make(chan struct{})
                goto relist
            }
        }
    relist:
        continue
    }
}
```

The "ERROR → relist" branch is the entire recovery model. The `cache.Replace(objs)` is *not* destructive in real informers — it computes a diff and emits synthetic events for items that disappeared. Our simplified version replays everything; in `mini-informer` we add a `Replace` that diffs.

### 13.2 The Workqueue

```go
// File: minik8s/lib/workqueue/queue.go
type RateLimitingInterface struct {
    queue       []string
    dirty       map[string]struct{}   // keys waiting to be processed
    processing  map[string]struct{}   // keys currently being processed
    delayed     map[string]time.Time  // key → next-eligible time
    rateLimit   map[string]int        // exponential backoff counter
    mu          sync.Mutex
    cond        *sync.Cond
}

func (q *RateLimitingInterface) Add(key string) {
    q.mu.Lock()
    defer q.mu.Unlock()
    if _, ok := q.dirty[key]; ok { return } // coalesce
    if _, ok := q.processing[key]; ok {
        q.dirty[key] = struct{}{}
        return // will re-queue after Done
    }
    q.dirty[key] = struct{}{}
    q.queue = append(q.queue, key)
    q.cond.Signal()
}

func (q *RateLimitingInterface) AddRateLimited(key string) {
    q.mu.Lock()
    q.rateLimit[key]++
    delay := time.Duration(min(1<<q.rateLimit[key], 60)) * time.Second
    q.delayed[key] = time.Now().Add(delay)
    q.mu.Unlock()
    // A background goroutine moves delayed keys back into queue when time arrives.
}

func (q *RateLimitingInterface) Get() (string, bool) {
    q.mu.Lock()
    defer q.mu.Unlock()
    for len(q.queue) == 0 { q.cond.Wait() }
    key := q.queue[0]
    q.queue = q.queue[1:]
    delete(q.dirty, key)
    q.processing[key] = struct{}{}
    return key, false
}

func (q *RateLimitingInterface) Done(key string) {
    q.mu.Lock()
    delete(q.processing, key)
    if _, ok := q.dirty[key]; ok {
        // Re-enqueue: a new event arrived during processing.
        q.queue = append(q.queue, key)
        q.cond.Signal()
    }
    q.mu.Unlock()
}

func (q *RateLimitingInterface) Forget(key string) {
    q.mu.Lock(); defer q.mu.Unlock()
    delete(q.rateLimit, key)
    delete(q.delayed, key)
}
```

The four invariants:
1. **Coalescing**: if a key is enqueued twice before processing, only one entry exists.
2. **Mutex-with-processing**: a key cannot be processed twice concurrently; if events arrive during processing, they're held in `dirty` and re-enqueued on `Done`.
3. **Rate limiting**: failed reconciles retry with exponential backoff (`AddRateLimited`).
4. **Forget on success**: clear backoff state.

These are *exactly* the invariants of `client-go/util/workqueue`. Chapter 08 §workqueue covers them line by line.

### 13.3 The Reconciler

A controller is now ~50 lines:

```go
// File: minik8s/controller/replicaset/controller.go
type ReplicaSetController struct {
    rsLister  cache.Lister
    podLister cache.Lister
    api       *Client
    queue     workqueue.RateLimitingInterface
}

func (c *ReplicaSetController) Run(ctx context.Context) {
    // Wire informers — both RS and Pod (pods may be deleted out from under us).
    rsInf := newReplicaSetInformer(c.api)
    podInf := newPodInformer(c.api, "")

    rsInf.AddEventHandler(cache.ResourceEventHandlerFuncs{
        AddFunc:    func(o interface{}) { c.queue.Add(key(o)) },
        UpdateFunc: func(_, n interface{}) { c.queue.Add(key(n)) },
        DeleteFunc: func(o interface{}) { c.queue.Add(key(o)) },
    })
    // Pod events: if a pod belongs to one of our RS (via ownerRef), enqueue
    // that RS for re-reconcile.
    podInf.AddEventHandler(cache.ResourceEventHandlerFuncs{
        AddFunc:    func(o interface{}) { c.enqueueOwnerRS(o.(*Pod)) },
        UpdateFunc: func(_, n interface{}) { c.enqueueOwnerRS(n.(*Pod)) },
        DeleteFunc: func(o interface{}) { c.enqueueOwnerRS(o.(*Pod)) },
    })

    go rsInf.Run(ctx); go podInf.Run(ctx)
    rsInf.WaitForCacheSync(ctx); podInf.WaitForCacheSync(ctx)

    for {
        k, quit := c.queue.Get()
        if quit { return }
        if err := c.reconcile(k); err != nil {
            c.queue.AddRateLimited(k)
        } else {
            c.queue.Forget(k)
        }
        c.queue.Done(k)
    }
}

func (c *ReplicaSetController) reconcile(rsKey string) error {
    rs, _ := c.rsLister.Get(rsKey)
    if rs == nil { return nil } // deleted

    // 1. Find all pods matching this RS's selector.
    allPods, _ := c.podLister.List()
    owned := []*Pod{}
    for _, p := range allPods {
        if isOwnedBy(p, rs) && labelsMatch(p.Labels, rs.Spec.Selector) {
            owned = append(owned, p)
        }
    }

    // 2. Filter out terminating.
    alive := []*Pod{}
    for _, p := range owned {
        if p.DeletionTimestamp == nil { alive = append(alive, p) }
    }

    // 3. Diff against desired.
    desired := rs.Spec.Replicas
    actual := int32(len(alive))
    diff := desired - actual

    switch {
    case diff > 0:
        // Create pods.
        for i := int32(0); i < diff; i++ {
            pod := buildPodFromTemplate(rs)
            if err := c.api.CreatePod(pod); err != nil { return err }
        }
    case diff < 0:
        // Delete excess pods. Pick the youngest first (or unhealthy first).
        sort.Slice(alive, func(i, j int) bool {
            return alive[i].CreationTimestamp.After(alive[j].CreationTimestamp)
        })
        for i := int32(0); i < -diff; i++ {
            if err := c.api.DeletePod(alive[i]); err != nil { return err }
        }
    }

    // 4. Update RS status.
    rs.Status.Replicas = int32(len(owned))
    rs.Status.ReadyReplicas = countReady(owned)
    rs.Status.AvailableReplicas = countAvailable(owned)
    return c.api.UpdateReplicaSetStatus(rs)
}
```

Read this carefully. Every line maps to a real K8s concept:

- `isOwnedBy(p, rs)` — owner references (chapter 36).
- `labelsMatch` — label selector evaluation (chapter 05).
- `DeletionTimestamp == nil` — exclude terminating pods (chapter 36).
- `buildPodFromTemplate(rs)` — `Spec.Template` materialization (chapter 12).
- Status update separate from spec update — the spec/status discipline (chapter 23).

The Deployment controller is the same pattern, one level up: instead of pods, it manages ReplicaSets. The Endpoints controller is the same pattern, watching Services + Pods and writing Endpoints.

### 13.4 The Deployment Controller (Rolling Update)

This is where the toy gets fun. A Deployment rollout means:

```
   1. New Deployment.Spec.Template hash differs from existing RS's.
   2. Create new RS with replicas=0.
   3. Scale new RS up by surge (e.g., +1) while scaling old RS down by 1.
   4. Wait until new RS's ReadyReplicas matches its replicas.
   5. Repeat 3–4 until new RS = desired and old RS = 0.
   6. Mark old RS for deletion (or keep for revision history).
```

Pseudocode:

```go
func (c *DeploymentController) reconcile(depKey string) error {
    dep, _ := c.depLister.Get(depKey)
    if dep == nil { return nil }
    templateHash := hashTemplate(dep.Spec.Template)

    // 1. Find or create the new RS.
    rss := c.findOwnedRS(dep)
    var newRS *ReplicaSet
    var oldRSs []*ReplicaSet
    for _, rs := range rss {
        if rs.Labels["pod-template-hash"] == templateHash {
            newRS = rs
        } else {
            oldRSs = append(oldRSs, rs)
        }
    }
    if newRS == nil {
        newRS = buildRS(dep, templateHash)
        newRS, _ = c.api.CreateRS(newRS)
    }

    // 2. Compute the rolling step.
    desired := dep.Spec.Replicas
    maxSurge := computeMaxSurge(dep)
    maxUnavailable := computeMaxUnavailable(dep)
    currentReady := countReadyAcrossAll(append(oldRSs, newRS))

    // Scale up new RS by surge.
    if newRS.Spec.Replicas < desired {
        room := desired + maxSurge - totalReplicas(append(oldRSs, newRS))
        if room > 0 {
            newRS.Spec.Replicas += min(room, desired-newRS.Spec.Replicas)
            c.api.UpdateRS(newRS)
        }
    }

    // Scale down old RSs as new becomes ready.
    if newRS.Status.ReadyReplicas > 0 && len(oldRSs) > 0 {
        available := currentReady - (desired - maxUnavailable)
        if available > 0 {
            for _, old := range oldRSs {
                if old.Spec.Replicas > 0 {
                    reduce := min(available, old.Spec.Replicas)
                    old.Spec.Replicas -= reduce
                    c.api.UpdateRS(old)
                    available -= reduce
                    if available == 0 { break }
                }
            }
        }
    }

    // 3. Update Deployment status.
    dep.Status.Replicas = totalReplicas(append(oldRSs, newRS))
    dep.Status.ReadyReplicas = currentReady
    dep.Status.UpdatedReplicas = newRS.Status.ReadyReplicas
    return c.api.UpdateDeploymentStatus(dep)
}
```

This is chapter 12 §deployments distilled. The real Deployment controller has more edge cases (paused rollouts, progress deadlines, history revisions, automatic rollback) but the spine is this 30-line loop.

### 13.5 The Endpoints Controller

```go
func (c *EndpointsController) reconcile(svcKey string) error {
    svc, _ := c.svcLister.Get(svcKey)
    if svc == nil {
        // Service deleted — delete corresponding Endpoints.
        return c.api.DeleteEndpoints(svc.Namespace, svc.Name)
    }
    pods := c.podLister.ListMatching(svc.Spec.Selector, svc.Namespace)
    subsets := []EndpointSubset{}
    for _, port := range svc.Spec.Ports {
        addresses := []EndpointAddress{}
        for _, p := range pods {
            if !isReady(p) { continue }
            addresses = append(addresses, EndpointAddress{IP: p.Status.PodIP, NodeName: &p.Spec.NodeName})
        }
        subsets = append(subsets, EndpointSubset{
            Addresses: addresses,
            Ports:     []EndpointPort{{Name: port.Name, Port: port.TargetPort}},
        })
    }
    eps := &Endpoints{
        Metadata: ObjectMeta{Namespace: svc.Namespace, Name: svc.Name},
        Subsets:  subsets,
    }
    return c.api.UpsertEndpoints(eps)
}
```

That's the entire Endpoints controller. Real K8s also has the EndpointSlice controller (chapter 14 §endpointslice) for scalability beyond ~1000 endpoints — same loop, slightly different output shape.

### 13.6 The GC Controller (Optional in `minik8s`)

If we want OwnerReferences-driven cascade deletes (chapter 36), we add a simple GC controller:

```go
func (gc *GCController) Run() {
    // Watch every type. Build owner graph: child UID → list of owner refs.
    // On parent delete, find children, schedule deletion (respecting policy).
}
```

For the toy demo we hard-code the cascade in the parent controllers (Deployment deletes its RSs; RS deletes its Pods). The real GC controller is generic over all owner-ref graphs and handles cycles, finalizers, and the foreground/background/orphan policies (chapter 36). Adding it later is mechanical.

---

## 14. Phase 9 — `mini-cni`: One Hundred Lines of Bridge

The Container Network Interface contract is delightfully simple: the kubelet exec's a binary, passes JSON on stdin (and env vars), gets JSON on stdout. The binary's job is to "do whatever networking is needed" inside the netns it was told to operate on.

### 14.1 The Contract

```
   CNI plugin invocation:
     argv: /opt/cni/bin/bridge
     env:  CNI_COMMAND=ADD            (or DEL, CHECK, VERSION)
           CNI_CONTAINERID=<podID>
           CNI_NETNS=/var/run/netns/<podID>
           CNI_IFNAME=eth0
           CNI_PATH=/opt/cni/bin
           CNI_ARGS=K8S_POD_NAMESPACE=default;K8S_POD_NAME=foo;…
     stdin: network config JSON
     stdout: result JSON
```

Per chapter 15 §cni-spec, the result for an ADD contains the assigned IP, gateway, DNS, etc.

### 14.2 The 100-Line Plugin

```go
// File: minik8s/cni/bridge/main.go
package main

import (
    "encoding/json"
    "fmt"
    "net"
    "os"
    "github.com/vishvananda/netlink"
    "github.com/vishvananda/netns"
)

type NetConf struct {
    CNIVersion string `json:"cniVersion"`
    Name       string `json:"name"`
    Type       string `json:"type"`
    Bridge     string `json:"bridge"`
    IPAM       struct {
        Type   string `json:"type"`
        Subnet string `json:"subnet"`
        Range  string `json:"rangeStart-rangeEnd"`
        StateFile string `json:"stateFile"` // we hardcode: file-backed pool
    } `json:"ipam"`
}

type Result struct {
    CNIVersion string `json:"cniVersion"`
    IPs []struct {
        Address string `json:"address"`
        Gateway string `json:"gateway"`
    } `json:"ips"`
}

func main() {
    cmd := os.Getenv("CNI_COMMAND")
    var cfg NetConf
    json.NewDecoder(os.Stdin).Decode(&cfg)
    switch cmd {
    case "ADD":  cmdAdd(&cfg)
    case "DEL":  cmdDel(&cfg)
    case "CHECK": os.Exit(0)
    }
}

func cmdAdd(cfg *NetConf) {
    netnsPath := os.Getenv("CNI_NETNS")
    ifname := os.Getenv("CNI_IFNAME")
    containerID := os.Getenv("CNI_CONTAINERID")

    // 1. Ensure bridge exists.
    br, err := netlink.LinkByName(cfg.Bridge)
    if err != nil {
        br = &netlink.Bridge{LinkAttrs: netlink.LinkAttrs{Name: cfg.Bridge}}
        netlink.LinkAdd(br)
        // Assign gateway IP to bridge.
        gw := firstIP(cfg.IPAM.Subnet)
        netlink.AddrAdd(br, &netlink.Addr{IPNet: &net.IPNet{IP: gw, Mask: subnetMask(cfg.IPAM.Subnet)}})
        netlink.LinkSetUp(br)
    }

    // 2. Allocate IP from file-backed pool.
    podIP := allocateIP(cfg.IPAM.StateFile, cfg.IPAM.Subnet, containerID)

    // 3. Create veth pair: hostend stays in host ns, podend moves to pod ns.
    hostName := "veth" + containerID[:8]
    veth := &netlink.Veth{
        LinkAttrs: netlink.LinkAttrs{Name: hostName},
        PeerName:  "eth-tmp-" + containerID[:8],
    }
    netlink.LinkAdd(veth)
    netlink.LinkSetMaster(veth, br.(*netlink.Bridge))
    netlink.LinkSetUp(veth)

    // 4. Move peer end into pod netns.
    peer, _ := netlink.LinkByName(veth.PeerName)
    nsHandle, _ := netns.GetFromPath(netnsPath)
    netlink.LinkSetNsFd(peer, int(nsHandle))

    // 5. Inside pod ns: rename to eth0, assign IP, route to gateway.
    runInNS(netnsPath, func() error {
        link, _ := netlink.LinkByName(veth.PeerName)
        netlink.LinkSetName(link, ifname)
        netlink.AddrAdd(link, &netlink.Addr{
            IPNet: &net.IPNet{IP: podIP, Mask: subnetMask(cfg.IPAM.Subnet)},
        })
        netlink.LinkSetUp(link)
        // Default route via bridge IP.
        netlink.RouteAdd(&netlink.Route{
            LinkIndex: link.Attrs().Index,
            Gw:        firstIP(cfg.IPAM.Subnet),
            Dst:       defaultRouteDst(),
        })
        // Bring up loopback.
        lo, _ := netlink.LinkByName("lo")
        netlink.LinkSetUp(lo)
        return nil
    })

    // 6. Emit result.
    res := Result{CNIVersion: cfg.CNIVersion}
    res.IPs = []struct{Address, Gateway string}{
        {Address: podIP.String() + "/24", Gateway: firstIP(cfg.IPAM.Subnet).String()},
    }
    json.NewEncoder(os.Stdout).Encode(res)
}

func cmdDel(cfg *NetConf) {
    containerID := os.Getenv("CNI_CONTAINERID")
    // 1. Release IP.
    releaseIP(cfg.IPAM.StateFile, containerID)
    // 2. Delete veth host end (the peer in pod ns dies with the netns).
    hostName := "veth" + containerID[:8]
    link, err := netlink.LinkByName(hostName)
    if err == nil { netlink.LinkDel(link) }
}
```

### 14.3 The IPAM

File-backed IPAM is shockingly simple:

```go
// File: minik8s/cni/ipam/file.go
type IPAMState struct {
    Allocations map[string]string `json:"allocations"` // containerID → IP
}

func allocateIP(stateFile, subnet, containerID string) net.IP {
    lockFile(stateFile)
    defer unlockFile(stateFile)
    state := readState(stateFile)
    if existing, ok := state.Allocations[containerID]; ok {
        return net.ParseIP(existing) // idempotent
    }
    cidr, _ := net.ParseCIDR(subnet)
    for ip := nextIP(cidr.IP); cidr.Contains(ip); ip = nextIP(ip) {
        if ip.Equal(firstIP(subnet)) { continue } // skip gateway
        if !inUse(state, ip) {
            state.Allocations[containerID] = ip.String()
            writeState(stateFile, state)
            return ip
        }
    }
    panic("ip pool exhausted")
}
```

Twenty lines. Cross-node IPAM in real CNI plugins is harder (you need a node-aware partitioner so two nodes don't allocate the same IP); that's what Calico's IPAM controller or Cilium's IP pool CRD do (chapters 15, 16). For `minik8s` single-node, the file is the source of truth.

### 14.4 Cross-Node Connectivity

We're single-node by default. To make `minik8s` multi-node, give each node a `/24` from a `/16` pool, run the bridge plugin per node, and add a *static route* on every node:

```
   node-1: pod CIDR 10.244.1.0/24, node IP 192.168.0.11
   node-2: pod CIDR 10.244.2.0/24, node IP 192.168.0.12

   On node-1: ip route add 10.244.2.0/24 via 192.168.0.12
   On node-2: ip route add 10.244.1.0/24 via 192.168.0.11
```

This is the simplest "underlay" routing — no overlay, no VXLAN, no BGP. The route controller in real K8s would be a controller that watches Nodes and reconciles host routes accordingly. We hand-wire it.

For VXLAN encapsulation (Flannel), BGP (Calico, Cilium), or eBPF-based routing (Cilium), see chapters 15 and 16. None of them change the *CNI plugin contract* — they only change what happens inside `cmdAdd`.

### 14.5 What This Maps To

Real CNI plugins (`containernetworking/plugins/plugins/main/bridge`) are about 800 lines because they handle:

- IPv6 (`mini-cni` is IPv4 only).
- VLAN tagging.
- Hairpin mode (for pods to talk to themselves via Service VIP).
- MTU adjustments for overlays.
- Promiscuous-mode arping during IP collision detection.
- NAT for traffic *leaving* the pod CIDR.

But the core ADD/DEL contract — create veth, attach to bridge, set up netns — is identical to ours.

---

## 15. Phase 10 — `mini-proxy`: Services via iptables

The final piece. We have pods with IPs. We have Services declared. We need: when an in-cluster client sends a packet to `<ServiceClusterIP>:<port>`, the kernel should DNAT it to one of the Service's ready endpoints.

### 15.1 The iptables Plan

For each Service we need rules of the form:

```
   # Top-level chain: catch packets to any service VIP.
   -t nat -A KUBE-SERVICES -d <svc1.clusterIP>/32 -p tcp --dport <svc1.port> -j KUBE-SVC-<hash1>
   -t nat -A KUBE-SERVICES -d <svc2.clusterIP>/32 -p tcp --dport <svc2.port> -j KUBE-SVC-<hash2>

   # Per-service chain: probabilistic dispatch to endpoint chains.
   # For 3 endpoints, probabilities 1/3, 1/2, 1.
   -t nat -A KUBE-SVC-<hash1> -m statistic --mode random --probability 0.333 -j KUBE-SEP-<sep1>
   -t nat -A KUBE-SVC-<hash1> -m statistic --mode random --probability 0.5   -j KUBE-SEP-<sep2>
   -t nat -A KUBE-SVC-<hash1> -j KUBE-SEP-<sep3>

   # Per-endpoint chain: DNAT to actual pod IP.
   -t nat -A KUBE-SEP-<sep1> -p tcp -j DNAT --to-destination <podIP1>:<targetPort>

   # Hook KUBE-SERVICES into the standard chains.
   -t nat -A PREROUTING -j KUBE-SERVICES
   -t nat -A OUTPUT     -j KUBE-SERVICES
```

### 15.2 The Reconcile

```go
// File: minik8s/proxy/proxy.go
type Proxy struct {
    svcInformer *Informer
    epsInformer *Informer
    queue       workqueue.RateLimitingInterface
    nodeName    string
}

func (p *Proxy) reconcile() error {
    services, _ := p.svcInformer.List()
    endpoints, _ := p.epsInformer.List()
    epsByName := indexByName(endpoints)

    // 1. Build the desired rule set in memory.
    var rules []string
    rules = append(rules, "*nat")
    rules = append(rules, ":KUBE-SERVICES - [0:0]")
    for _, svc := range services {
        svcChain := "KUBE-SVC-" + hash(svc.Namespace+"/"+svc.Name)
        rules = append(rules, ":"+svcChain+" - [0:0]")
        ep := epsByName[svc.Namespace+"/"+svc.Name]
        if ep == nil || len(allAddrs(ep)) == 0 {
            // No endpoints — reject with REJECT or just no rule.
            continue
        }
        // KUBE-SERVICES → svc chain.
        for _, port := range svc.Spec.Ports {
            rules = append(rules, fmt.Sprintf(
                "-A KUBE-SERVICES -d %s/32 -p %s --dport %d -j %s",
                svc.Spec.ClusterIP, port.Protocol, port.Port, svcChain))
        }
        // SEP chains.
        addrs := allAddrs(ep)
        for i, addr := range addrs {
            sepChain := "KUBE-SEP-" + hash(svc.Namespace+"/"+svc.Name+"/"+addr.IP)
            rules = append(rules, ":"+sepChain+" - [0:0]")
            prob := 1.0 / float64(len(addrs)-i)
            if i < len(addrs)-1 {
                rules = append(rules, fmt.Sprintf(
                    "-A %s -m statistic --mode random --probability %f -j %s",
                    svcChain, prob, sepChain))
            } else {
                rules = append(rules, fmt.Sprintf("-A %s -j %s", svcChain, sepChain))
            }
            for _, port := range svc.Spec.Ports {
                rules = append(rules, fmt.Sprintf(
                    "-A %s -p %s -j DNAT --to-destination %s:%d",
                    sepChain, port.Protocol, addr.IP, port.TargetPort))
            }
        }
    }
    rules = append(rules, "COMMIT")

    // 2. Atomically replace via iptables-restore.
    return exec.Command("iptables-restore", "--noflush").Run().WithStdin(strings.Join(rules, "\n"))
}

func (p *Proxy) Run(ctx context.Context) {
    // Set up KUBE-SERVICES hook from PREROUTING and OUTPUT (one-time).
    exec.Command("iptables", "-t", "nat", "-A", "PREROUTING", "-j", "KUBE-SERVICES").Run()
    exec.Command("iptables", "-t", "nat", "-A", "OUTPUT", "-j", "KUBE-SERVICES").Run()

    // Watch + enqueue.
    p.svcInformer.AddEventHandler(...)
    p.epsInformer.AddEventHandler(...)
    go p.svcInformer.Run(ctx)
    go p.epsInformer.Run(ctx)

    for {
        _, quit := p.queue.Get()
        if quit { return }
        if err := p.reconcile(); err != nil {
            p.queue.AddRateLimited("reconcile")
        }
        p.queue.Done("reconcile")
    }
}
```

The pattern: on any Service or Endpoints change, enqueue a single "reconcile" key. The reconcile rebuilds the *entire* iptables ruleset from scratch and atomically swaps. This is exactly what `kube-proxy` in iptables mode does — chapter 14 §iptables-mode.

### 15.3 The Scaling Problem

At ~5000 services, iptables rule reconciliation becomes O(N²) wall-clock time (each rule traversal at packet receive is also O(N) — that's why real installations switch to IPVS, nftables, or eBPF at scale; chapter 14 §scaling, chapter 16 §cilium-replacement). For `minik8s`'s 10 services demo, we're fine.

### 15.4 What This Maps To

Real `kube-proxy` (`pkg/proxy/iptables`) is ~10,000 lines because it has:

- IPVS mode (chapter 14 §ipvs).
- nftables mode (1.31+).
- EndpointSlice support (chapter 14 §endpointslice).
- Session affinity (sticky sessions by client IP).
- ExternalTrafficPolicy=Local (preserve source IP, drop cross-node).
- NodePort, LoadBalancer (we only do ClusterIP).
- Topology-aware routing.
- Health-check probes for external LB integration.

All of which is, again, additive on top of the same "watch Services + Endpoints, rebuild rules" loop.

---

## 16. What We Deliberately Skip and Why

Inventory of things `minik8s` does not do, why we skip them, and where each one slots in if you want to add it.

| Feature | Chapter | Why we skip | Where it would slot in |
|---|---|---|---|
| HA control plane | 04, 32 | Distributed consensus is its own multi-week project | Replace `minikv` map with Raft (Hashicorp raft lib + bbolt); add lease-based leader election to controller-manager and scheduler |
| RBAC depth | 07 | Multi-tenant safety is orthogonal to the architecture | New `mini-rbac` library evaluating `(user, verb, resource, namespace)` against ClusterRole/ClusterRoleBinding/Role/RoleBinding objects, called at step 3 of the apiserver handler chain |
| OIDC / workload identity | 07 | External IdP plumbing | Mutating admission webhook that injects projected SA tokens; new TokenRequest API for `BoundServiceAccountTokenVolume` |
| Mutating webhooks | 06 | Network-call ordering hell | Add a webhook chain to the apiserver between defaulting and validation, with `failurePolicy`, `timeoutSeconds`, `matchConditions` (CEL) |
| ValidatingAdmissionPolicy (CEL) | 06 | CEL needs a parser/evaluator | Embed `cel-go`; admission step that compiles policies and evaluates per request |
| Conversion webhooks | 23 | Multi-version CRDs require call-out | Per-CRD conversion endpoint dispatcher in the apiserver |
| CRDs (dynamic types) | 23 | Type registry is hard-coded | New `CustomResourceDefinition` type; creation registers a new `ResourceInfo` in the registry; structural schema stored as JSONSchema |
| Aggregated API | 24 | Routing to external apiservers | `APIService` object that adds a reverse-proxy route to the apiserver path tree |
| CSI / persistent volumes | 19 | Three-phase lifecycle is intricate | New `VolumeManager` in kubelet that talks gRPC to a CSI plugin; PV/PVC binding controller in controller-manager |
| NetworkPolicy | 20 | Requires iptables ipset or eBPF policy engine | Per-pod chains in `mini-proxy` that gate ingress/egress by source/destination pod labels |
| Ingress / Gateway API | 17 | Requires an Envoy/NGINX configurer | New controller that watches `Ingress`/`HTTPRoute` and configures a sidecar L7 proxy; cross-references chapter 17 §gateway-api |
| Service mesh | 17 | Massive scope (sidecar injection, mTLS, xDS) | Mutating webhook for sidecar injection + xDS server + per-pod Envoy |
| Multi-cluster | 26 | Whole separate problem | ClusterAPI-style provisioning + Karmada-style workload propagation across clusters |
| Cloud LBs | 37 | Per-cloud integrations | Cloud Controller Manager binary; per-cloud plugin implementing `CloudProvider` interface |
| HPA / VPA | 22 | Needs metrics pipeline first | Controller that reads from a metrics API (extension API server) and PATCHes Deployment replicas |
| Cluster autoscaler / Karpenter | 22 | Needs cloud node provisioning | Watch unschedulable pods, decide a new node shape, call cloud API to provision |
| GitOps | 31 | A separate product | ArgoCD-style controller that watches a Git repo and applies manifests |
| Pod sandboxing (gVisor/Kata) | 29 | Requires alternative OCI runtimes | RuntimeClass selecting between `runc`, `runsc` (gVisor), `kata-runtime` |
| CPU/Memory/Topology managers | 21 | NUMA-aware allocation is its own subsystem | Plug into kubelet between syncPod and CRI CreateContainer |
| Audit | 30 | Operational add-on | Apiserver handler middleware writing per-request audit events |
| API Priority and Fairness | 35 | Throughput governance | Apiserver request-rate fairness layer in front of the handler chain |

The point of this table is *not* "look how much we skip." The point is: every skipped item has a *known place* in the architecture. The reader who has internalized phases 0–10 can locate any K8s feature on the map.

---

## 17. Where Did the Extra 99.9% Go?

If `minik8s` is 5,000 lines and the real Kubernetes monorepo is 4 million, the ratio is 800×. Where did the other 3,995,000 lines go? Let's tour them.

### 17.1 Generated Code (~30%)

Run `grep -rln 'AUTO-GENERATED' kubernetes/` and you'll find ~1.2 million lines. The major categories:

- **`zz_generated_deepcopy.go`** — every Go type has a `DeepCopy()` method, auto-generated by `deepcopy-gen`. About 200K lines.
- **`zz_generated_conversion.go`** — apiserver converts between API versions (v1beta1 ↔ v1) field-by-field. ~150K lines.
- **`zz_generated_defaults.go`** — defaulting functions per type. ~50K lines.
- **`openapi_generated.go`** — OpenAPI schemas for every type. ~300K lines.
- **`clientset/*`** — client-go clientsets (`type DeploymentInterface interface { Create…; Update…; Delete…; Get…; List…; Watch… }`) for every group/version. ~150K lines.
- **`listers/*`, `informers/*`** — typed listers and informers, one per group/version/resource. ~150K lines.
- **Protobuf-generated `pb.go`** — every API type also has a protobuf representation. ~80K lines.

`minik8s` skips all of this because we have one version, we use reflection instead of typed clients, and we use plain JSON. Real K8s pays the generated-code tax in exchange for compile-time type safety on every operation.

### 17.2 Tests (~25%)

`*_test.go` files plus `test/` (integration), `test/e2e/` (Ginkgo end-to-end), and `staging/src/.../testing/` fixtures. Approximately 1M lines. The e2e suite alone runs ~2,000 test cases against a real cluster. `minik8s` has no tests in this chapter (a real implementation would; the build-it exercise is more valuable than the test exercise here).

### 17.3 Backwards Compatibility (~10%)

Every API surface in Kubernetes promises N-1 minor version compatibility at the wire level. That requires:

- Multi-version registration for every resource (`v1alpha1`, `v1beta1`, `v1` simultaneously available).
- Conversion functions per pair.
- Field-by-field "internal" types used as a hub between versions.
- Storage-version migration controllers.

When a `Pod.Spec.SchedulingGates` field is added in 1.27, the v1 storage type gains the field, conversion to/from "internal" type adds the field, defaulting handles old objects that don't have it, validation rejects invalid combinations. Multiply by every field added over 10 years (~5,000 fields).

### 17.4 Cloud Provider Stubs (Historical) (~5%)

Before 1.31 the cloud-provider code lived in-tree: AWS, GCE, Azure, OpenStack, vSphere, Oracle, IBM, AliCloud, DigitalOcean. About 200K lines, now extracted. The kubelet, controller-manager, and apiserver still have "in-tree volume plugin" code for backwards compatibility (the EBS, GCE PD, AzureDisk, Cinder, vSphereVolume volume types). Each is being migrated to out-of-tree CSI drivers (chapter 19 §csi-migration) and the in-tree stubs will eventually disappear.

### 17.5 Volume Plugins (~3%)

Every storage backend before CSI was an in-tree plugin: `pkg/volume/awsebs`, `pkg/volume/azuredisk`, `pkg/volume/cephfs`, `pkg/volume/configmap`, `pkg/volume/csi`, `pkg/volume/downwardapi`, `pkg/volume/emptydir`, `pkg/volume/fc`, `pkg/volume/flocker`, `pkg/volume/gcepd`, `pkg/volume/glusterfs`, `pkg/volume/hostpath`, `pkg/volume/iscsi`, `pkg/volume/local`, `pkg/volume/nfs`, `pkg/volume/portworxvolume`, `pkg/volume/projected`, `pkg/volume/quobyte`, `pkg/volume/rbd`, `pkg/volume/scaleio`, `pkg/volume/secret`, `pkg/volume/storageos`, `pkg/volume/vsphere_volume`. Each is ~5K lines, ~25 plugins, ~125K lines.

### 17.6 Subresource Handlers (~3%)

`/exec`, `/attach`, `/portforward`, `/log`, `/proxy` — each is a fully-featured streaming protocol layer on top of the apiserver. SPDY upgrade negotiation, WebSocket framing, demuxing stdout/stderr/stdin streams. About 80K lines across apiserver and kubelet.

### 17.7 Resource Managers (~3%)

`pkg/kubelet/cm/`: CPU manager (static binding to specific cores), memory manager (NUMA-aware allocation), device manager (GPU/SmartNIC allocation), topology manager (joint policy over CPU/memory/devices), DRA (Dynamic Resource Allocation, the new way for accelerators). About 100K lines.

### 17.8 Three kube-proxy Modes (~2%)

`pkg/proxy/iptables`, `pkg/proxy/ipvs`, `pkg/proxy/nftables`, plus the legacy `userspace` mode now removed. Each is ~10K-20K lines because each handles all the things iptables/IPVS/nftables have specialized for: connection tracking, NAT, conntrack flushing, hairpin masquerading.

### 17.9 Aggregation Layer (~2%)

`staging/src/k8s.io/apiserver/pkg/server/options/` plus the aggregator. Adds:

- `APIService` registration.
- Reverse proxying to external apiservers (with auth delegation).
- Discovery merging across multiple apiservers.
- Health checking of aggregated backends.

About 80K lines.

### 17.10 APF, Admission Plugins, Audit (~2%)

API Priority and Fairness (chapter 35 §apf) is ~30K lines of flow-schema-based fair queueing. The built-in admission plugins (`pkg/admission/`) are ~30K lines: `NamespaceLifecycle`, `LimitRanger`, `ServiceAccount`, `DefaultStorageClass`, `DefaultTolerationSeconds`, `RuntimeClass`, `MutatingAdmissionWebhook`, `ValidatingAdmissionWebhook`, etc. Audit (`staging/src/.../audit/`) is ~20K lines.

### 17.11 kubeadm, kube-controller-manager Bootstrap (~2%)

`cmd/kubeadm/` is ~80K lines: certificate generation, kubeconfig generation, etcd bootstrap, control-plane manifest writing, upgrade flows, init/join/reset commands, MachineConfig/Kubelet config rendering, addon installation. This is *day 0* — `minik8s` skips it because we bring everything up by hand.

### 17.12 Addons (~3%)

CoreDNS (chapter 18), kube-proxy (we have a tiny version), the dashboard, metrics-server, CSI sidecars (external-provisioner, external-attacher, external-resizer, external-snapshotter), node-problem-detector. These ship as part of every cluster but live in their own repos (~150K lines summed).

### 17.13 Everything Else (~10%)

Feature gates and their guarded code paths, the audit framework, etcd compaction policy, garbage collection, namespace controller, node lifecycle controller (taint, NotReady, eviction), token controller, service-account controller, certificate-signing-request controller, the bootstrap-token controller, lease-based identity (chapter 04 §lease) … a long tail of "another small loop on the same pattern."

### 17.14 The Summary Pie Chart

```
                Where the 4M lines went

      Generated code         ████████████████████   30%
      Tests                  █████████████████      25%
      Backcompat conversion  ███████                10%
      Cloud providers        ████                    5%
      Volume plugins         ██                      3%
      Subresources           ██                      3%
      Resource managers      ██                      3%
      kube-proxy modes       █                       2%
      Aggregation layer      █                       2%
      APF/admission/audit    █                       2%
      kubeadm                █                       2%
      Addons                 ██                      3%
      Everything else        ██████                 10%

      Essential architecture (≈ minik8s)             0.1%
```

So when you read a single chapter of this series and think "this is way too much for one component," the response is: the component *itself* is small. What you read about is the assembly of fifty years of operating systems, distributed systems, and platform engineering pressure on that small thing. The chapter is about the pressure, not the small thing. The small thing is in this capstone.

---

## 18. End-to-End Demo: From `curl -X POST` to Running Container

Let us trace what happens when, on a single Linux box, we bring up all of `minik8s` and ask it to run an nginx pod.

### 18.1 The Boot Sequence

```
   Terminal 1:
     $ ./minikv --listen :2379 --snapshot /var/lib/minikv/snapshot
     [minikv] listening on :2379, revision=0

   Terminal 2:
     $ ./mini-apiserver --kv localhost:2379 --listen :8080
     [apiserver] handlers registered: pods, nodes, services, endpoints,
                                      deployments, replicasets, bindings
     [apiserver] listening on :8080

   Terminal 3:
     $ ./mini-controller-manager --apiserver http://localhost:8080
     [controllers] starting deployment, replicaset, endpoints controllers
     [informer] list pods rev=0 returned 0 items
     [informer] watch pods from rev=0

   Terminal 4:
     $ ./mini-scheduler --apiserver http://localhost:8080
     [scheduler] starting
     [informer] list nodes rev=0 returned 0 items, list pods returned 0

   Terminal 5 (this terminal is "the node"):
     $ ./mini-runtime --listen unix:///var/run/minik8s-cri.sock
     [runtime] CRI server ready

   Terminal 6 (also "the node"):
     $ ./mini-kubelet --apiserver http://localhost:8080 \
                      --node-name node-1 \
                      --cri unix:///var/run/minik8s-cri.sock \
                      --cni /opt/cni/bin/bridge
     [kubelet] node-1: registering with apiserver
     [kubelet] node registered, allocatable cpu=4 mem=8Gi
     [kubelet] watching pods bound to node-1
     [pleg] starting, interval 1s
     [status] starting batcher

   Terminal 7 (also "the node"):
     $ ./mini-proxy --apiserver http://localhost:8080 --node-name node-1
     [proxy] watching services + endpoints
     [proxy] initial reconcile: 0 services
```

That is the entire control plane plus one node. ~5 minutes from cold start to "cluster ready."

### 18.2 The Trace

```
   Terminal 8 (the user):
     $ cat > nginx-deployment.json <<EOF
     {
       "apiVersion": "apps/v1",
       "kind": "Deployment",
       "metadata": {"name": "nginx", "namespace": "default"},
       "spec": {
         "replicas": 2,
         "selector": {"matchLabels": {"app": "nginx"}},
         "template": {
           "metadata": {"labels": {"app": "nginx"}},
           "spec": {
             "containers": [{"name": "nginx", "image": "nginx:1.27",
                             "ports": [{"containerPort": 80}]}]
           }
         }
       }
     }
     EOF
     $ curl -X POST -H 'Content-Type: application/json' \
            --data @nginx-deployment.json \
            http://localhost:8080/apis/apps/v1/namespaces/default/deployments
     {"apiVersion":"apps/v1","kind":"Deployment","metadata":{"name":"nginx",
      "namespace":"default","uid":"d4f...","resourceVersion":"1",
      "creationTimestamp":"2026-05-23T..."}, ... }
```

Tail every terminal. Within ~3 seconds:

```
   [apiserver]  T+0ms   POST /apis/apps/v1/.../deployments
   [apiserver]  T+1ms   defaulting, admission, validation OK
   [apiserver]  T+2ms   minikv.Put(/registry/deployments/default/nginx, body, 0) → rev=1
   [apiserver]  T+3ms   201 Created returned

   [minikv]     T+2ms   Watch fan-out: 1 subscriber on /registry/deployments → push event

   [controllers] T+4ms  deployment-controller: OnAdd(nginx), enqueue "default/nginx"
   [controllers] T+5ms  reconcile default/nginx:
                          - no existing RS
                          - create RS nginx-7f8a4 (templateHash=7f8a4) replicas=2
   [apiserver]  T+6ms   POST /apis/apps/v1/.../replicasets → rev=2

   [controllers] T+7ms  replicaset-controller: OnAdd(nginx-7f8a4), enqueue
   [controllers] T+8ms  reconcile: desired=2, actual=0, create 2 pods
   [apiserver]  T+9ms   POST pods/nginx-7f8a4-abc12 → rev=3
   [apiserver]  T+10ms  POST pods/nginx-7f8a4-def34 → rev=4

   [scheduler]  T+11ms  OnAdd(nginx-7f8a4-abc12) — spec.nodeName=="" → enqueue
   [scheduler]  T+11ms  OnAdd(nginx-7f8a4-def34) — spec.nodeName=="" → enqueue
   [scheduler]  T+12ms  scheduleOne(abc12): filter → [node-1], score → 95, bind to node-1
   [apiserver]  T+13ms  POST /api/v1/.../pods/abc12/binding → PATCH spec.nodeName=node-1 → rev=5
   [scheduler]  T+14ms  scheduleOne(def34): same → node-1 (cache assumes abc12 already there)
   [apiserver]  T+15ms  POST def34/binding → rev=6

   [kubelet]    T+16ms  watch event: abc12 now spec.nodeName=node-1, enqueue podworker
   [kubelet]    T+17ms  syncPod(abc12):
                          - sandbox missing → RunPodSandbox
   [runtime]    T+18ms  RunPodSandbox: clone pause, save netns at /proc/1234/ns/net
   [kubelet]    T+20ms  exec /opt/cni/bin/bridge with CNI_COMMAND=ADD, CNI_NETNS=/proc/1234/ns/net
   [cni-bridge] T+21ms  bridge cni0 exists, allocate IP 10.244.0.2, create veth, attach
   [cni-bridge] T+30ms  return {"ips":[{"address":"10.244.0.2/24","gateway":"10.244.0.1"}]}
   [kubelet]    T+31ms  podIP=10.244.0.2
   [kubelet]    T+32ms  CreateContainer(nginx) → ctrID
   [runtime]    T+33ms  read image rootfs from /var/lib/minik8s/images/nginx-1.27/
                        (in real life: PullImage first; for demo, pre-staged)
   [kubelet]    T+34ms  StartContainer(ctrID)
   [runtime]    T+35ms  fork/unshare/pivot_root/exec /docker-entrypoint.sh nginx -g 'daemon off;'
   [runtime]    T+50ms  child running, PID 1 inside, host PID 5678

   [kubelet]    T+50ms  syncPod for def34 (in parallel)
                        (… same sequence, IP 10.244.0.3, ctr starts at host PID 5701)

   [pleg]       T+1000ms list runtime: abc12 RUNNING, def34 RUNNING
   [pleg]                emit events; pod workers re-sync; status updated

   [status]     T+1100ms PATCH /api/v1/.../pods/abc12/status → phase=Running, podIP=10.244.0.2
   [status]              PATCH .../def34/status                → phase=Running, podIP=10.244.0.3

   [controllers] T+1200ms endpoints-controller: no Service for app=nginx — no-op
   [controllers]          replicaset-controller: ReadyReplicas=2 — update RS status
   [controllers]          deployment-controller: ReadyReplicas=2 — Available condition true
```

Now add a Service:

```
   $ curl -X POST -H 'Content-Type: application/json' http://localhost:8080/api/v1/namespaces/default/services \
       -d '{"apiVersion":"v1","kind":"Service","metadata":{"name":"nginx"},
            "spec":{"selector":{"app":"nginx"},
                    "ports":[{"port":80,"targetPort":80}],
                    "clusterIP":"10.96.0.42"}}'

   [apiserver]  T+0    POST services → rev=12
   [controllers] T+2ms endpoints-controller: OnAdd(nginx), reconcile
                       pods matching app=nginx → 10.244.0.2:80, 10.244.0.3:80
                       upsert Endpoints
   [apiserver]  T+5ms  PUT endpoints/default/nginx → rev=13
   [proxy]      T+6ms  OnAdd(svc), OnAdd(eps) → reconcile
   [proxy]      T+7ms  build ruleset:
                         KUBE-SERVICES -d 10.96.0.42/32 --dport 80 -j KUBE-SVC-abc
                         KUBE-SVC-abc --probability 0.5 -j KUBE-SEP-ep1
                         KUBE-SVC-abc -j KUBE-SEP-ep2
                         KUBE-SEP-ep1 -j DNAT --to-destination 10.244.0.2:80
                         KUBE-SEP-ep2 -j DNAT --to-destination 10.244.0.3:80
   [proxy]      T+8ms  iptables-restore applied
```

And finally, from inside a pod:

```
   $ curl http://10.96.0.42/
   <html>... welcome to nginx ...</html>
```

The packet path:
1. Pod issues `connect(10.96.0.42:80)` → kernel sends SYN through veth.
2. SYN hits host's `cni0` bridge, then up to host netns routing.
3. `OUTPUT -t nat` chain runs `KUBE-SERVICES`, matches `10.96.0.42:80`, jumps to `KUBE-SVC-abc`.
4. `KUBE-SVC-abc` with 0.5 probability picks `KUBE-SEP-ep1`, which DNATs to `10.244.0.2:80`.
5. Packet routed back through `cni0` → veth → pod netns of nginx pod 1.
6. SYN-ACK returns; reverse-DNAT applied by conntrack.

That is your full cluster, running, end to end, with every line of code in this chapter.

```
                         END-TO-END DATA PATH
                         (one HTTP request via Service VIP)

         client pod (10.244.0.7)
                │
                ▼
        socket  → kernel netns of pod
                │  veth1 ───► cni0 bridge (host ns)
                ▼
         OUTPUT (nat) chain
                │  KUBE-SERVICES → KUBE-SVC-abc → KUBE-SEP-ep1
                │  DNAT to 10.244.0.2:80
                ▼
          routing decision
                │  10.244.0.2 is on cni0 (local /24)
                ▼
         cni0 bridge
                │  port = veth-to-server-pod
                ▼
         kernel netns of server pod (10.244.0.2)
                │
                ▼
              nginx process (PID 1 inside)
```

---

## 19. Reading Guide: Mapping `minik8s` to Real Kubernetes Source

For each `minik8s` component, the corresponding entry points in the real codebase:

| `minik8s` | Real K8s source path | What to read first |
|---|---|---|
| `minikv` | `etcd-io/etcd` `server/etcdserver/`, `server/storage/mvcc/` | `mvcc/kvstore.go` (transactions), `mvcc/watcher.go` (watch) |
| `mini-apiserver` | `kubernetes/kubernetes/staging/src/k8s.io/apiserver/` | `pkg/server/genericapiserver.go`, `pkg/endpoints/handlers/`, `pkg/storage/etcd3/` |
| `mini-controller-manager` | `kubernetes/kubernetes/pkg/controller/` | `deployment/deployment_controller.go`, `replicaset/replica_set.go`, `endpoint/endpoints_controller.go`, `garbagecollector/garbagecollector.go` |
| `mini-scheduler` | `kubernetes/kubernetes/pkg/scheduler/` | `scheduler.go` (the cycle), `framework/runtime/framework.go` (plugin invocation), `framework/plugins/` (built-ins) |
| `mini-kubelet` | `kubernetes/kubernetes/pkg/kubelet/` | `kubelet.go` (syncLoop), `pleg/generic.go` (PLEG), `pod_workers.go`, `status/status_manager.go` |
| `mini-runtime` | `containerd/containerd` + `opencontainers/runc` | containerd `pkg/cri/server/`, runc `libcontainer/process_linux.go` |
| `mini-cni` | `containernetworking/plugins/plugins/main/bridge/` | `bridge.go` (single file, ~700 lines) |
| `mini-proxy` | `kubernetes/kubernetes/pkg/proxy/iptables/` | `proxier.go` (the giant reconcile function) |
| `mini-informer` lib | `kubernetes/kubernetes/staging/src/k8s.io/client-go/tools/cache/` | `reflector.go`, `shared_informer.go`, `delta_fifo.go`, `thread_safe_store.go` |
| `mini-workqueue` lib | `kubernetes/kubernetes/staging/src/k8s.io/client-go/util/workqueue/` | `queue.go` (~100 lines), `rate_limiting_queue.go`, `delaying_queue.go` |
| `kubectl-mini` | `kubernetes/kubernetes/staging/src/k8s.io/kubectl/` | `pkg/cmd/apply/apply.go` for the SSA flow |

The reading order for someone who finishes this chapter and wants to spend a year understanding the real codebase:

1. **`client-go/tools/cache/`** — informers are the most copied pattern in the K8s ecosystem; read this *carefully*. Cross-reference chapter 08.
2. **`pkg/scheduler/scheduler.go`** — the scheduler cycle is the cleanest single-loop reconcile in the codebase.
3. **`pkg/kubelet/kubelet.go`** + `pleg/generic.go` + `status/status_manager.go` — the kubelet is the most "node OS" piece; read these in chapter 10's order.
4. **`pkg/controller/replicaset/replica_set.go`** — the canonical built-in controller; reading this teaches you the controller idiom.
5. **`staging/src/k8s.io/apiserver/pkg/server/`** — the apiserver is the densest. Start at `genericapiserver.go` and work outward.
6. **`pkg/proxy/iptables/proxier.go`** — to see a really big reconcile loop in real code.

Bookmark every chapter referenced in the table above as you read; the chapters explain the *why* and the source explains the *how*.

---

## 20. What This Exercise Teaches

Two dozen lessons that fall out of building `minik8s`:

### 20.1 Why Each Abstraction Exists

- **etcd separate from apiserver**: so the apiserver is stateless and can be scaled horizontally; so all storage backups are just etcd backups.
- **Apiserver separate from controllers**: so admission/auth is centralized; so any controller can be swapped without touching storage.
- **Scheduler separate from controllers**: so scheduling policy is a single place, not scattered; so you can swap schedulers per workload type (chapter 34).
- **Kubelet separate from runtime**: so the runtime can be swapped (containerd/CRI-O/Docker shim of yore); so node-local logic (probes, eviction) lives near the kernel.
- **CRI separate from OCI**: so the kubelet doesn't need to know runc's command-line; so containerd can manage images independently.
- **CNI separate from CRI**: so networking can be swapped per cluster; so the runtime knows nothing about IP allocation.
- **CSI separate from kubelet**: so storage backends can be deployed independently; so node-plugin and controller-plugin can be in different binaries.
- **Reconcile separate from event handlers**: so reconciles are idempotent and level-triggered; so missing an event doesn't break correctness.
- **Spec separate from status**: so users own intent and controllers own observation; so they don't fight via SSA managers.
- **Finalizers separate from delete**: so external resources have a chance to clean up; so namespace deletion can wait for tenant-owned cleanup.
- **OwnerRefs separate from labels**: so deletion cascades are structural (UID-based), not selector-based; so an orphaned set of labels doesn't accidentally adopt children.

Each item is a chapter (or several) in this series. Once you build `minik8s`, the *necessity* of each separation becomes self-evident — because if you don't have them, your toy works for one user and one workload, and breaks the moment you scale either.

### 20.2 Where Bugs Hide

Building `minik8s` exposes the failure modes that every K8s engineer eventually hits:

- **Edge-triggered controllers miss events.** If you wire up `OnAdd → reconcile` but not `OnUpdate` and not `OnDelete`, every restart of your controller "forgets" everything. The level-triggered fix: always queue the key, re-read the cached object, recompute desired state from scratch.
- **Boundary mismatches.** The CRI returns `ContainerStatus.State="EXITED"` but the kubelet's pod cache still says `Running` because the status manager hasn't flushed. Two views of reality momentarily disagree; the reconciler must converge them rather than treating either as authoritative.
- **Watch silently failing.** A TCP RST closes the connection; the informer's `for ev := range ch` exits without an error. If you don't have a re-list-on-error path, the cache freezes at the moment of disconnect.
- **Workqueue starvation.** A handler that holds a lock during reconcile blocks the worker pool. Production patterns use the lock for the smallest possible critical section; everything else is just reads from the cache.
- **CAS retry storm.** Two controllers fight over the same object via update. Each retries on 409 Conflict. Without backoff, they pin a CPU. With backoff, one of them eventually wins. The cure is `Server-Side Apply` with disjoint `fieldManager`s.
- **Status flapping.** Probes oscillate; status updates fire every second; apiserver gets pounded. The cure is batching + diff-check before write.
- **Finalizer deadlocks.** Controller A adds finalizer F1; controller B adds F2; both wait on each other to clean up. Result: object stuck Terminating forever (chapter 36).

Every one of these is a single edit to a `minik8s` component to demonstrate. The mistake is encountering them in production for the first time on a 5000-node cluster.

### 20.3 K8s "Magic" Reduced to Patterns

After building `minik8s`, every "K8s magic" thing reduces to one of six patterns:

1. **Watch + cache + reconcile.** Every controller. Every operator. The scheduler. Even the kubelet.
2. **CAS on resourceVersion.** Every write. Conflict-driven retry is the entire concurrency model.
3. **Owner refs.** Cascade deletes, lifecycle, lineage. The graph is in `metadata`; the GC controller walks it.
4. **Finalizers.** "Don't actually delete until everyone says ok." The two-phase delete contract.
5. **Subresources.** `/status` vs `/spec` separation, `/bind` for scheduler, `/exec` for kubectl. Authorization and conflict avoidance fall out of subresource granularity.
6. **Extension via API objects.** Every new feature is a new CRD or built-in type + a new controller. There is no "out-of-band" path.

Hold those six in your head and Kubernetes stops feeling magical and starts feeling like a library you could have written. You did write it. It is `minik8s`.

---

## 21. Next-Level Projects After `minik8s`

Sequenced extensions once the base is working.

### 21.1 Add CRDs

Add a `CustomResourceDefinition` type to the apiserver. On its creation, register a new `ResourceInfo` in the registry whose `Validate` function compiles the structural schema (a JSONSchema subset) and validates against it. Chapter 23 walks the storage version selection, the `additionalPrinterColumns`, subresources (`status`, `scale`), and conversion strategy. ~500 lines added.

### 21.2 Add an Aggregated API Server

Define `APIService` registration. When created, the apiserver adds a reverse-proxy route for `/apis/<group>/<version>` pointing to a second backend. Auth tokens are delegated (the aggregator runs `TokenReview` against its own apiserver). Build `mini-metrics-apiserver` that serves `metrics.k8s.io/v1beta1` from in-memory samples. Chapter 24 covers the gotchas (auth caching, discovery merging). ~300 lines.

### 21.3 Leader Election + Multi-Replica Control Plane

Add a `Lease` object type (chapter 04 §lease, chapter 08 §leader-election). The controller-manager and scheduler each try to renew the lease every N seconds with a CAS. The winner reconciles; the losers stand by. Wire `minikv`'s in-memory map up to multiple replicas via a 3-node Raft (Hashicorp's `hashicorp/raft` lib). The apiserver becomes stateless and trivially replicable. ~1000 lines.

### 21.4 Add a WAL + bbolt for minikv

Replace the in-memory map with bbolt; write each Put/Delete to a WAL before applying to the tree (chapter 14 §wal). On crash recovery, replay the WAL. Add periodic snapshot + compaction. This is the move from `simpledb.py`'s buffer pool to its WAL chapter, applied here. ~500 lines.

### 21.5 Implement HPA on a Resource Metric

Build `mini-metrics-server`: a DaemonSet that scrapes cAdvisor and stores per-pod CPU usage. Build the HPA controller: watch HPAs, query metrics-server, compute desired replicas, PATCH the Deployment's replicas. Chapter 22 covers the stabilization window, the algorithm, the failure modes. ~400 lines.

### 21.6 A Custom Scheduler Plugin

Pick a real plugin from chapter 09: `PodTopologySpreadConstraints` is a good one. Implement it as a separate filter+score in `mini-scheduler`. Or better: build a *second* scheduler that schedules only pods with `schedulerName: my-sched`. Both schedulers coexist; each reconciles its share of unscheduled pods. ~300 lines.

### 21.7 A CSI Driver for ramdisk

Build a CSI controller plugin and CSI node plugin (chapter 19). The driver provisions a `ramfs` mount on each node for a PVC; node-publish bind-mounts it into the pod. Implement the three-phase lifecycle (CreateVolume → ControllerPublishVolume → NodePublishVolume). Wire `mini-kubelet`'s volume manager to call CSI gRPC. ~1000 lines, and the most you'll learn from any of these extensions.

### 21.8 NetworkPolicy

Build a controller that watches `NetworkPolicy` and updates iptables rules per pod. Chapter 20 §enforcement covers the matrix of selector kinds (podSelector, namespaceSelector, ipBlock). ~500 lines if you keep it iptables-based; ~2000 if you switch to eBPF.

### 21.9 A Simple Ingress Controller

Watch `Ingress` objects; configure an NGINX with one server block per host, one location per path. Reload NGINX on every change. Chapter 17. ~300 lines.

### 21.10 Audit + RBAC

Two together, since both add request middleware. Implement `Role`/`ClusterRole`/`RoleBinding`/`ClusterRoleBinding` types and an evaluator. Add audit-event emission per request. ~600 lines.

Doing all ten of these gets you to roughly 10,000 lines and ~70% of real Kubernetes's functional surface. The remaining 30% is the operational hardening and the deep tail of cloud/storage/network integrations.

---

## 22. A Taxonomy of "Real Kubernetes Complexity"

A way to categorize every chapter in the series, and every feature you encounter in the wild, into one of six buckets. This is the closing organizing schema.

### 22.1 Essential (Chapters 00–10)

The 5,000-line core we just built. Cannot remove anything without destroying the architecture:

- Chapter 00: Linux primitives
- Chapter 01: Container runtimes
- Chapter 02: Images & registries
- Chapter 03: K8s architecture overview
- Chapter 04: etcd
- Chapter 05: kube-apiserver
- Chapter 06: Admission (basic chain)
- Chapter 07: AuthN/AuthZ (basic)
- Chapter 08: Controller pattern
- Chapter 09: Scheduler
- Chapter 10: Kubelet

If you understand these chapters and `minik8s`, you understand Kubernetes.

### 22.2 Workload Surface (Chapters 11–13, 21–22)

How the abstraction maps to user workloads:

- Chapter 11: Pod internals
- Chapter 12: Workload controllers
- Chapter 13: StatefulSets
- Chapter 21: Resource management + QoS
- Chapter 22: Autoscaling

These are not architecturally new — every one is a controller + a kubelet feature — but the policies (rolling update, ordered teardown, surge, QoS, throttling, HPA control loop) are workload-shape-specific.

### 22.3 Extension Surface (Chapters 06, 23, 24, 28, 34)

How users extend the core without forking:

- Chapter 06: Admission webhooks + ValidatingAdmissionPolicy
- Chapter 23: CRDs, operators, controller-runtime
- Chapter 24: Aggregated APIs
- Chapter 28: Policy engines (Gatekeeper, Kyverno, CEL)
- Chapter 34: Custom schedulers

The genius move of Kubernetes is that *the extension API is just more API*. CRDs are objects; controllers are processes; webhooks are services. The extension surface uses the same patterns as the core.

### 22.4 Operational (Chapters 21, 22, 30, 32, 35, 36)

Day 2 operations and the things you only learn the hard way:

- Chapter 30: Observability internals
- Chapter 32: Cluster lifecycle and day-2
- Chapter 35: Performance and scaling
- Chapter 36: Garbage collection and object lifecycle

These are where the "real K8s complexity" lives in operator experience: backup/restore, upgrades, version skew, etcd defrag, watch cache sizing, finalizer footguns.

### 22.5 Network (Chapters 14–18, 20)

The hardest cross-cutting concern:

- Chapter 14: Services and kube-proxy
- Chapter 15: CNI and pod networking
- Chapter 16: Cilium and eBPF
- Chapter 17: Ingress, Gateway, service mesh
- Chapter 18: DNS and CoreDNS
- Chapter 20: NetworkPolicy

Networking is hard because it touches the kernel, the L2/L3 dataplane, the policy engine, the LB integration, and the L7 protocol stack — all of which evolve at different rates. This is the cluster of chapters where "K8s does X" hides "for some value of X depending on which CNI and which mode and which kernel and which cloud."

### 22.6 Storage (Chapter 19)

Storage is its own continent. CSI's three-phase lifecycle (provision → attach → mount) and the sidecar architecture (external-provisioner, external-attacher, external-resizer, external-snapshotter) and the access mode matrix (RWO/ROX/RWX/RWOP) and the dynamic-vs-static provisioning split — all live in chapter 19.

### 22.7 Cloud / Multi-cluster (Chapters 25, 26, 33, 37)

Beyond a single cluster:

- Chapter 25: Multi-tenancy (within a cluster)
- Chapter 26: Multi-cluster and fleet
- Chapter 33: Edge distributions
- Chapter 37: Cloud provider integration

The hard problem here is not Kubernetes; it is "Kubernetes plus another distributed system" — Git for GitOps, ClusterAPI for provisioning, Karmada for workload propagation, cloud APIs for everything. Each is its own course.

### 22.8 Supply Chain / Security (Chapters 07, 20, 27–29)

The defense-in-depth surface:

- Chapter 07: AuthN/AuthZ (deep)
- Chapter 20: NetworkPolicy
- Chapter 27: Supply-chain (Sigstore, SBOM)
- Chapter 28: Runtime security and policy
- Chapter 29: Sandboxing (gVisor, Kata, ConfidentialContainers)

Security is layered: identity (07), network segmentation (20), build-time integrity (27), runtime enforcement (28), kernel isolation (29). Skipping any layer leaves a class of attacks intact.

### 22.9 Tooling (Chapters 31, 32)

The human-facing surface:

- Chapter 31: GitOps, Helm, Kustomize
- Chapter 32: Cluster lifecycle (kubeadm, etcd backup/restore)

The pattern is that humans don't `kubectl apply` directly; pipelines do. Helm and Kustomize are about rendering YAML; GitOps engines are about driving it.

### 22.10 The Taxonomy as a Map

```
                       REAL K8S COMPLEXITY TAXONOMY

   ┌──────────────────┐
   │     ESSENTIAL    │  ch 00–10   minik8s lives here
   │  (5K LoC total)  │
   └────────┬─────────┘
            │ depends on
            ▼
   ┌──────────────────┐
   │  WORKLOAD        │  ch 11–13, 21–22
   │  (controllers +  │
   │   kubelet feats) │
   └────────┬─────────┘
            │
            ▼
   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
   │  EXTENSION       │  │  NETWORK         │  │  STORAGE         │
   │  (CRDs, webhooks,│  │  (CNI, mesh,     │  │  (CSI, PV/PVC,   │
   │   custom scheds) │  │   policy, DNS)   │  │   snapshots)     │
   │  ch 06, 23, 24,  │  │  ch 14–18, 20    │  │  ch 19           │
   │  28, 34          │  │                  │  │                  │
   └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
            │                     │                     │
            └─────────┬───────────┴─────────────────────┘
                      ▼
   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
   │  OPERATIONAL     │  │  SECURITY        │  │  CLOUD/MULTI     │
   │  (perf, lifecycle│  │  (authn/z,       │  │  (CCM, fleet,    │
   │   obs, GC)       │  │   supply chain,  │  │   edge, vCluster)│
   │  ch 21, 22, 30,  │  │   sandboxing)    │  │  ch 25, 26, 33,  │
   │  32, 35, 36      │  │  ch 07, 20, 27–29│  │  37              │
   └──────────────────┘  └──────────────────┘  └──────────────────┘
                                │
                                ▼
                       ┌──────────────────┐
                       │     TOOLING      │
                       │  (GitOps, Helm,  │
                       │   day-2 ops)     │
                       │  ch 31, 32       │
                       └──────────────────┘
```

The pyramid: essential at the base, workload above it, then extension/network/storage as the three faces of "what you do with the cluster," then operational/security/cloud as the three faces of "what the cluster does to you," capped by tooling. Every chapter slots in.

---

## 23. A Final Reflection: What K8s Got Right, Wrong, and What Comes Next

The capstone of capstones. What does the architecture we just built teach us about the architecture's own merits and faults?

### 23.1 What Kubernetes Got Right

**The controller pattern**. The single most-copied idea in modern systems software. "Watch some objects, reconcile to a desired state" is now the standard pattern in operators, CD pipelines, infrastructure tools (Terraform's reconciliation, Pulumi's drift detection, Crossplane), service meshes (Istio's xDS), and even outside the cloud-native world. Once you internalize that every problem is a reconcile loop, you start writing better software in every language.

**The declarative API**. Even with all the YAML hate, the model — "describe what you want; the system makes it true" — is correct. It is what differentiates K8s from Borg (which was imperative job submission) and from earlier cluster managers (Mesos, Nomad — somewhere between imperative and declarative). Declarative-with-reconcile is the right design.

**The watch primitive**. Polling kills systems at scale. Push-with-resumable-cursor (etcd's MVCC-watch, mirrored at the apiserver) is what makes 10,000-node clusters feasible. Without `WATCH`, every controller would poll every other controller; the apiserver would melt at 100 nodes.

**etcd as the only stateful component**. Backups are trivial. Disaster recovery is trivial. Every other component is a cache + an actor over the same source of truth. The architecture has *one* hard piece, and that piece is well-defined.

**The CRI/CNI/CSI separation**. Three orthogonal interfaces for three orthogonal concerns. Containerd evolves independently of Calico evolves independently of Ceph. K8s didn't have to know about any of them; they all conform to a contract. This is the unsung architectural triumph: in 2014 it was "K8s + Docker"; by 2020 the entire data-plane was pluggable.

**Custom Resources**. The single feature that turned Kubernetes from "container orchestrator" into "platform for platforms." Operators (Postgres, Kafka, Cassandra, Cert-Manager, ArgoCD itself) made it possible for K8s to be the substrate of every cloud-native stateful workload, not just stateless web apps.

### 23.2 What Kubernetes Got Wrong

**YAML as the user surface**. Whitespace-sensitive, no comments in JSON-compatible mode, no schema enforcement at edit time, no type checking. Helm and Kustomize are entire ecosystems built to paper over YAML; CUE/Pkl/Jsonnet are emerging because YAML alone is untenable for nontrivial config. The K8s API itself is fine (it's JSON over the wire); the user surface should have been JSON-Schema-validated typed config from day one.

**RBAC verbosity**. Every role is `apiGroups + resources + verbs + (optionally) resourceNames`. Multi-tenant clusters end up with thousands of role objects. CEL-based ABAC (CelAuthorization, proposed but not stable) would have been a better choice from the start.

**In-tree cloud providers**. Putting AWS, GCE, Azure, vSphere, OpenStack code directly in the apiserver/controller-manager binary made K8s a graveyard of cloud-vendor logic. The multi-year migration to out-of-tree CCM (chapter 37) was necessary but painful and dragged the project for ~five years.

**In-tree volume plugins**. Same mistake, separately. CSI fixed it; the migration took even longer.

**In-tree kube-proxy modes**. iptables, IPVS, nftables, three implementations of the same idea, in the same binary, none deprecated cleanly. Cilium kube-proxy replacement is the actual correct answer; making it standard would have simplified the project enormously.

**Admission webhooks**. Network calls in the write path were a poor choice for almost everything. CEL-based ValidatingAdmissionPolicy (now in stable) is the correct answer and arrived seven years late. Mutating webhooks are still a pain point; an in-process CEL-based mutator would be a net win.

**Multi-tenancy ambiguity**. The "namespace is not a security boundary" message took years to land; many production clusters are still misconfigured because of the early ambiguity. The vCluster pattern (chapter 25) is a workaround for what should have been a first-class hard-tenancy primitive.

**Long-lived ServiceAccount tokens**. The original SA tokens (chapter 07) were unbounded JWTs mounted by default into every pod. The `BoundServiceAccountTokenVolume` projected-token model fixed it, but old behaviors are sticky; many clusters still ship long-lived tokens by default.

**Two delete primitives** (`metadata.deletionTimestamp` + finalizers vs immediate row removal) without a unified UX. Operators routinely confuse "is deleting" with "is deleted" and write controllers that interpret deletion incorrectly. Chapter 36 §finalizer-footguns enumerates the pain.

### 23.3 What Successors Might Do

If you were designing the next system today, with K8s as the legacy:

**KCP-style logical clusters.** Why have one apiserver per cluster when you could have one apiserver serving many *workspaces*? KCP (the kcp.io project) implements multi-tenant control planes with workspace isolation — a tenant has the illusion of their own cluster on shared infrastructure. The vCluster pattern is a partial implementation. The endgame is that "a Kubernetes cluster" stops being a hardware unit and becomes a logical workspace.

**Sidecarless mesh as default.** Istio ambient mode (ztunnel + waypoint, chapter 17) eliminates sidecar containers from the data plane; cilium service mesh does the same via eBPF. The cost of sidecars (one Envoy per pod = 100MB RAM, 50ms startup latency, mTLS handshake at every hop) was always too high. The next system should make service mesh a node-level facility, not a pod-level one.

**eBPF for everything.** kube-proxy → eBPF socket load balancing (Cilium does this). NetworkPolicy → eBPF cgroup hooks (also Cilium). Probes → eBPF observation rather than HTTP/TCP round-trips. Observability → eBPF instead of cAdvisor's directory walk. Each replacement is faster, more accurate, and lower-overhead. Chapter 16 covers this revolution.

**CEL replacing webhooks.** In-process expression evaluation is faster than RPC, more predictable than admission webhook chains, and easier to reason about for policy authors. ValidatingAdmissionPolicy (chapter 06) is the first wave; expect MutatingAdmissionPolicy, scheduler-plugin-as-CEL, and CEL-based authorization next.

**Workload identity from day one.** SPIFFE/SPIRE-style cryptographic identity per workload, mounted as a SVID, used for mTLS and resource authorization. The current SA-token/IRSA/Workload Identity matrix is a patchwork; a clean design has one identity primitive.

**Strongly typed config language**. CUE, Pkl, Dhall, or a similar successor — type-checked, composable, expression-based. YAML for storage, but not for authoring. Chapter 31 hints at this; the future leans hard into it.

**Better stateful primitives**. StatefulSets are 80% solution. Operators fill the 20% with thousands of lines per database. A first-class "ordered, identity-stable, persistent-storage-managed workload" abstraction with built-in backup/restore/replication would absorb half of the operator ecosystem.

**Multi-cluster as a first-class API**. ClusterAPI, Karmada, Fleet, KCP — all are bolted on. The next system should treat clusters as objects from the start: a `Workload` may declare `placement: { clusters: [...] }` and propagate accordingly.

**Less generated code**. ~30% of the K8s codebase is auto-generated. A reflective or runtime-typed system (or, more realistically, a language with better metaprogramming) could eliminate most of it. Generics in Go 1.18+ already enable a lot of this; a clean-slate redesign in 2026 would benefit even more.

### 23.4 The Sentence to End On

Kubernetes is, at its essential core, **a watchable KV store with eight controllers and a node agent**. Every concept layered on top — Services, Ingress, RBAC, CRDs, operators, mesh, autoscaling, multi-cluster — is one more controller, one more API object, one more pattern instance of the same six ideas you can now write down on a napkin.

The architecture is small. The fidelity is large. The ecosystem is enormous. But the architecture, the small thing, is what `minik8s` shows you. And once you can hold the small thing in your head, everything else fits.

---

## 24. Forward From Here: SIGs, KEPs, and the Contributor Path

You finished a 38-chapter deep dive and built (or now could build) `minik8s`. Where next?

### 24.1 The Special Interest Groups (SIGs)

Kubernetes is governed by SIGs. Each SIG owns specific subdirectories of the codebase and specific KEPs. The major ones:

- **sig-api-machinery** — apiserver, etcd integration, watch, CRDs, aggregation, admission. (Chapters 04, 05, 06, 23, 24.) Mailing list: `sig-api-machinery@kubernetes.io`. Slack: `#sig-api-machinery`.
- **sig-apps** — Deployment, StatefulSet, DaemonSet, Job, CronJob. (Chapters 12, 13.) `#sig-apps`.
- **sig-architecture** — cross-cutting design, code organization, API conventions. `#sig-architecture`.
- **sig-auth** — AuthN, AuthZ, RBAC, secrets, certificates, ServiceAccounts. (Chapter 07.) `#sig-auth`.
- **sig-autoscaling** — HPA, VPA, cluster-autoscaler, KEDA upstream coordination. (Chapter 22.) `#sig-autoscaling`.
- **sig-cli** — kubectl. `#sig-cli`.
- **sig-cluster-lifecycle** — kubeadm, ClusterAPI, cluster upgrades. (Chapters 32, 26.) `#sig-cluster-lifecycle`.
- **sig-instrumentation** — metrics, logs, traces, audit. (Chapter 30.) `#sig-instrumentation`.
- **sig-network** — kube-proxy, Services, Ingress, Gateway API, CNI (interface, not plugins), DNS, NetworkPolicy. (Chapters 14, 15, 17, 18, 20.) `#sig-network`.
- **sig-node** — kubelet, container runtime interface, pod lifecycle, resource managers. (Chapters 00, 01, 10, 11, 21.) `#sig-node`.
- **sig-release** — release process and milestones.
- **sig-scalability** — performance SLOs, scale testing, the 5K-node cluster. (Chapter 35.) `#sig-scalability`.
- **sig-scheduling** — kube-scheduler, scheduler framework. (Chapters 09, 34.) `#sig-scheduling`.
- **sig-security** — supply chain, runtime security, vulnerability handling, sandboxing. (Chapters 27, 28, 29.) `#sig-security`.
- **sig-storage** — CSI, PV/PVC, volume snapshots, volume expansion. (Chapter 19.) `#sig-storage`.

Each SIG has a weekly Zoom meeting (calendar at `https://www.kubernetes.dev/resources/calendar/`), a meeting notes doc, a Slack channel, and a charter. Pick one that matches the chapter you found most interesting; lurk for a month; then attend a meeting.

### 24.2 The KEP Process

Significant changes to Kubernetes are proposed as KEPs (Kubernetes Enhancement Proposals). Each lives at `kubernetes/enhancements/keps/sig-<sig>/<NNNN-name>/README.md`. Structure:

- **Summary** — one paragraph.
- **Motivation** — why now, why this design.
- **Proposal** — the technical detail.
- **Design Details** — API, behavior, alternatives considered.
- **Test Plan** — required for graduation alpha → beta → stable.
- **Graduation Criteria** — explicit milestones.
- **Drawbacks** — honest enumeration.
- **Alternatives** — other approaches considered.

The KEP template is at `kubernetes/enhancements/keps/NNNN-kep-template/README.md`. Read 5–10 KEPs before writing one; the conventions are tight.

Notable KEPs to read as exemplars of good design discussion:

- **KEP-3477** (CEL-based admission policies) — chapter 06.
- **KEP-2876** (CRD validation rules using CEL) — chapter 23.
- **KEP-3329** (Retriable and non-retriable Pod failures for Jobs) — chapter 12.
- **KEP-3325** (Scheduler dynamic resource allocation) — chapters 09, 21.
- **KEP-3998** (Job pod failure policy) — chapter 12.
- **KEP-1287** (In-place pod vertical scaling) — chapter 22.

Pick one that excites you. Read its history (the PR that merged it, the SIG meeting notes that discussed it, the alpha → beta graduation discussions). You will learn more from one well-followed KEP than from another textbook chapter.

### 24.3 The Contributor Path

If you want to write code for upstream Kubernetes:

1. **Sign the CLA** at `https://kubernetes.io/docs/contribute/`.
2. **Pick a SIG**. Match it to the chapter you most enjoyed.
3. **Find an issue tagged `good first issue`** in `kubernetes/kubernetes`. There are usually 50–100 open.
4. **Read the contributor guide**: `kubernetes/community/contributors/guide/`. Especially the bit on `release-notes-required` labels and `kind/*` labels.
5. **Build kubernetes locally**: `git clone kubernetes/kubernetes; make`. The first build takes ~30 minutes. Subsequent incremental builds are seconds.
6. **Run an E2E test against a kind cluster**: `kind create cluster; export KUBECONFIG=$(kind get kubeconfig-path); make WHAT=test/e2e/e2e.test; ./_output/bin/e2e.test --kubeconfig=$KUBECONFIG --ginkgo.focus="<your-area>"`.
7. **Make a small PR**. Documentation typo, e2e test improvement, validation message clarification — anything that gets you through the review pipeline once.
8. **Iterate**. After 5–10 small PRs you'll have a sense of the codebase rhythm.
9. **Pick a bigger thing**. Work with your SIG to scope it. If it's significant, write a KEP.

The codebase is enormous but the *changes* are usually surgical. You will not be rewriting `pkg/kubelet/kubelet.go`; you will be adding a 50-line method to one file and 200 lines of tests.

### 24.4 The Adjacent Ecosystem

Most innovation in cloud-native happens *around* kubernetes/kubernetes:

- **client-go** (`staging/src/k8s.io/client-go`) — referenced from chapter 08; the library every controller uses.
- **controller-runtime** (`kubernetes-sigs/controller-runtime`) — kubebuilder's foundation; chapter 23.
- **kubebuilder** (`kubernetes-sigs/kubebuilder`) — operator scaffolding.
- **operator-sdk** (`operator-framework/operator-sdk`) — Red Hat's operator scaffolding.
- **Cluster API** (`kubernetes-sigs/cluster-api`) — chapter 26.
- **Cilium** (`cilium/cilium`) — chapter 16; the most important data plane innovation of the last five years.
- **Karpenter** (`kubernetes-sigs/karpenter`) — chapter 22; node autoscaler.
- **Knative** (`knative/`) — serverless on top of K8s.
- **Crossplane** (`crossplane/crossplane`) — chapter 26; multi-cloud control plane.
- **ArgoCD** (`argoproj/argo-cd`) and **Flux** (`fluxcd/flux2`) — chapter 31.
- **Kyverno** (`kyverno/kyverno`) and **Gatekeeper** (`open-policy-agent/gatekeeper`) — chapter 28.
- **Istio** (`istio/istio`) and **Linkerd** (`linkerd/linkerd2`) — chapter 17.

Each is its own community. Many are governed by the CNCF (`cncf.io`). The CNCF Technical Advisory Groups (TAGs) — Storage, Network, Runtime, Security, Observability — coordinate cross-project standards.

### 24.5 Conferences

- **KubeCon + CloudNativeCon** — three times a year (NA, EU, Asia/Pacific). The main event.
- **Contributor Summit** — pre-KubeCon. SIG meetings IRL; KEP working sessions.
- **Cloud Native Rejekts** — the alternative track; talks rejected from KubeCon. Often the best technical content.

Watching past KubeCon talks (all free on YouTube) is the second-best use of your time after reading source.

### 24.6 The Mental Frame Going Forward

You have now built the entire stack, or at least the model of it, in your head. The rest of your Kubernetes life is:

- Encountering a new abstraction (a new resource, a new mode, a new dataplane).
- Locating it on the taxonomy in §22.
- Identifying which existing pattern it instantiates (watch+reconcile, CAS, owner-refs, finalizers, subresources, extension).
- Reading the source or KEP that introduced it.
- Adding it to your mental `minik8s` as one more controller or one more handler.

That is the trajectory. Kubernetes is no longer "a thing you learn"; it is "a substrate you build on, augmented incrementally as new pieces appear." The 38 chapters of this series are the snapshot of the substrate as of 2026; the SIGs and KEPs are where it evolves.

You are now equipped to follow that evolution. Welcome to the long arc.

---

**End of Chapter 38. End of the series.**

If you read all 38 chapters in order: thank you. If you skipped around: that's how technical references are meant to be used. The ROADMAP.md at the root of `kubernetes/` is the map; this chapter is the synthesis. Use them as the index when production teaches you something the chapters did not, and update your own mental `minik8s` accordingly.

The cluster runs because someone, somewhere, wrote ~5,000 lines of essential mechanism and another 3,995,000 lines of hardening, integration, and backwards compatibility on top of it. You can now read either layer. That is the entire goal of this series.
