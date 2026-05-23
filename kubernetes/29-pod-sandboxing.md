# Pod Sandboxing: gVisor, Kata Containers, Confidential Containers, and RuntimeClass

The staff-engineer reference for *what isolation actually is on Kubernetes* once you stop trusting the host kernel. Chapter 00 explained namespaces and cgroups; chapter 01 explained how the CRI shim hands a `config.json` to an OCI runtime; chapter 25 explained why namespaces are not a security boundary; chapter 28 explained the policy-and-detection side of runtime security. This chapter is the *isolation* side: how to swap `runc` for a runtime that does not share the host kernel — `runsc` (gVisor), `kata-runtime` (Kata Containers), or a Confidential Containers stack on top of Intel TDX, AMD SEV-SNP, or ARM CCA — and how Kubernetes' `RuntimeClass` resource selects which one runs each pod.

If you read chapter 28 and walked away thinking "Pod Security Admission and Falco are great, but a kernel CVE still owns the host," this chapter is the answer. *Policy* tells the kernel which syscalls a workload may make. *Isolation* makes a different kernel handle them, or encrypts the memory the host kernel reads. Defense in depth pairs the two: PSA + seccomp + AppArmor + Falco at the kernel boundary, and gVisor / Kata / CoCo at the *trust* boundary.

The chapter is intentionally long because the topic is operationally treacherous. RuntimeClass is a five-line YAML; that five-line YAML in production reshapes scheduling, cost, debuggability, image pulling, networking, storage, and your tolerance for kernel CVEs. We walk all of it.

---

## Table of Contents

1. [Why Sandboxing Exists](#1-why-sandboxing-exists)
2. [The Sandbox Spectrum: runc → gVisor → Kata → microVM → Confidential VM](#2-the-sandbox-spectrum-runc--gvisor--kata--microvm--confidential-vm)
3. [Threat Model: What the Sandbox Boundary Stops, and What It Does Not](#3-threat-model-what-the-sandbox-boundary-stops-and-what-it-does-not)
4. [`RuntimeClass`: The Kubernetes Wiring](#4-runtimeclass-the-kubernetes-wiring)
5. [RuntimeClass Scheduling: `nodeSelector` and Tolerations](#5-runtimeclass-scheduling-nodeselector-and-tolerations)
6. [RuntimeClass Overhead Accounting](#6-runtimeclass-overhead-accounting)
7. [containerd Configuration for Multiple Runtimes](#7-containerd-configuration-for-multiple-runtimes)
8. [gVisor (`runsc`): The Userspace Kernel](#8-gvisor-runsc-the-userspace-kernel)
9. [gVisor Architecture: Sentry + Gofer](#9-gvisor-architecture-sentry--gofer)
10. [gVisor Platforms: ptrace, KVM, systrap](#10-gvisor-platforms-ptrace-kvm-systrap)
11. [gVisor Syscall Coverage and Compatibility Gaps](#11-gvisor-syscall-coverage-and-compatibility-gaps)
12. [gVisor Performance Profile](#12-gvisor-performance-profile)
13. [Kata Containers: VM-per-Pod Isolation](#13-kata-containers-vm-per-pod-isolation)
14. [Kata Architecture: shim, agent, hypervisor, virtio-fs](#14-kata-architecture-shim-agent-hypervisor-virtio-fs)
15. [Kata Hypervisor Choices: QEMU, Cloud Hypervisor, Firecracker, Dragonball](#15-kata-hypervisor-choices-qemu-cloud-hypervisor-firecracker-dragonball)
16. [Firecracker: The microVMM Behind Lambda and Fargate](#16-firecracker-the-microvmm-behind-lambda-and-fargate)
17. [Cloud Hypervisor: virtio-fs Native, Rust](#17-cloud-hypervisor-virtio-fs-native-rust)
18. [VM-per-Pod vs Container-per-VM](#18-vm-per-pod-vs-container-per-vm)
19. [Confidential Containers (CoCo): TDX, SEV-SNP, CCA](#19-confidential-containers-coco-tdx-sev-snp-cca)
20. [Attestation: Trustee, KBS, and the Secret-Release Flow](#20-attestation-trustee-kbs-and-the-secret-release-flow)
21. [Encrypted Container Images](#21-encrypted-container-images)
22. [Choosing: gVisor vs Kata vs CoCo](#22-choosing-gvisor-vs-kata-vs-coco)
23. [Performance Benchmarks: cpu, fileio, syscall-heavy, cold start](#23-performance-benchmarks-cpu-fileio-syscall-heavy-cold-start)
24. [Cluster Deployment Patterns: Tainted Node Pools, GKE Sandbox, EKS+Bottlerocket](#24-cluster-deployment-patterns-tainted-node-pools-gke-sandbox-eksbottlerocket)
25. [Use Case: gVisor in CI and Multi-Tenant Code Execution](#25-use-case-gvisor-in-ci-and-multi-tenant-code-execution)
26. [Use Case: Kata in Regulated Workloads (PCI, HIPAA)](#26-use-case-kata-in-regulated-workloads-pci-hipaa)
27. [What Sandboxes Do Not Protect Against](#27-what-sandboxes-do-not-protect-against)
28. [Picking the Isolation Boundary](#28-picking-the-isolation-boundary)
29. [Limitations and Gotchas](#29-limitations-and-gotchas)
30. [Operating a Sandbox-Enabled Cluster](#30-operating-a-sandbox-enabled-cluster)
31. [Other Sandbox Approaches: Nabla, Sysbox, Fargate](#31-other-sandbox-approaches-nabla-sysbox-fargate)
32. [gVisor Source Map](#32-gvisor-source-map)
33. [Kata Source Map](#33-kata-source-map)
34. [Confidential Containers Source Map](#34-confidential-containers-source-map)
35. [Migration Path: From All-runc to Selective Sandboxing](#35-migration-path-from-all-runc-to-selective-sandboxing)
36. [Observability for Sandbox Runtimes](#36-observability-for-sandbox-runtimes)
37. [Pitfalls and Anti-Patterns](#37-pitfalls-and-anti-patterns)
38. [TL;DR](#38-tldr)

---

## 1. Why Sandboxing Exists

The single sentence reason: **a container is a process; a process shares the host kernel; a kernel CVE in any container's reachable syscall surface is a host compromise.**

Chapter 00 made this explicit: `unshare(CLONE_NEWPID | CLONE_NEWNET | CLONE_NEWNS | …)` plus a cgroup is a "container". The kernel is the same kernel the host runs. Every syscall the container makes is handled by code paths that *also* run for the host. A bug in those code paths — an out-of-bounds read in `io_uring`, a use-after-free in the netfilter conntrack module, a logic error in user-namespaces with `CAP_SYS_ADMIN`, an unchecked length in eBPF's verifier — is a kernel bug. A kernel bug in code reachable from an unprivileged container is a *container escape*.

The historical record is brutal:

- **CVE-2019-5736** (runc): a malicious container could overwrite the runc binary on the host by exploiting how `/proc/self/exe` was resolved during `runc exec`. One curl-and-execute on any pod where the attacker had `exec` privileges → root on the host.
- **CVE-2022-0185** (kernel, `legacy_parse_param`): heap overflow in the filesystem context API, reachable from an unprivileged user namespace. Container escape demonstrated within days.
- **CVE-2022-0492** (kernel, cgroup-v1 `release_agent`): cgroup-v1 misuse allowed escape from a container by writing to `release_agent` from inside a privileged-like context.
- **CVE-2024-1086** (kernel, netfilter `nf_tables`): use-after-free reachable via unprivileged user namespaces; weaponized for container escape within weeks of disclosure.
- **CVE-2024-0193** (kernel, netfilter): similar pattern, similar escape path.
- **Dirty Pipe (CVE-2022-0847)**: write to read-only files, including host files visible from a container, via a flaw in pipe splice.

Every one of these is the *same architectural fact*: the kernel is shared. seccomp can narrow which syscalls are reachable, AppArmor/SELinux can narrow which paths are reachable, capabilities can drop privileges, but the kernel code servicing whatever you *do* let through is one binary, and a bug in that binary is a bug for everyone on the box.

The sandbox-runtime answer is one of two:

1. **Reduce the surface area** by intercepting syscalls in userspace and only forwarding a small, audited subset to the host kernel. This is gVisor's approach: the *Sentry* implements the Linux syscall ABI in Go, in userspace; it only ever calls a hardened, allow-listed set of host syscalls. A bug in the host kernel's `keyctl` implementation is unreachable from a gVisor pod because the Sentry never calls `keyctl` on the host.
2. **Run the workload in a separate kernel** by booting a lightweight VM per pod. This is Kata's approach: a stripped Linux kernel boots inside a VM (QEMU, Cloud Hypervisor, Firecracker, or Dragonball); the workload's syscalls hit *that* kernel. A bug in *that* kernel still compromises *that* VM, but the host kernel and other tenants are unaffected.

Confidential Containers extend (2) by encrypting the VM's memory in hardware so that even the host kernel and hypervisor cannot read it, defending against a privileged attacker on the node (rogue admin, compromised hypervisor, cloud provider in regulated scenarios).

The cost: gVisor is 2–10× slower per syscall; Kata adds a 100–500 ms cold start and per-pod RAM overhead; CoCo adds attestation infrastructure and the operational burden of measured boot. The tradeoff is *not free*. The decision is per-workload, which is exactly why Kubernetes exposes it via `RuntimeClass` rather than as a cluster-wide setting.

---

## 2. The Sandbox Spectrum: runc → gVisor → Kata → microVM → Confidential VM

Sandboxing is not binary; it is a spectrum of trust-boundary placements, each with a cost.

```
                  ISOLATION STRENGTH  →

  weakest ◄──────────────────────────────────────────► strongest
  cheapest                                            most expensive
  ┌─────────┐ ┌─────────┐ ┌──────────┐ ┌─────────────┐ ┌──────────────┐
  │  runc   │ │ gVisor  │ │  Kata    │ │ Firecracker │ │ Confidential │
  │         │ │ (runsc) │ │ (QEMU/   │ │   microVM   │ │   VM (TDX/   │
  │ shares  │ │ user-   │ │  Cloud   │ │   minimal   │ │   SEV-SNP)   │
  │  host   │ │ space   │ │  Hyp/FC) │ │   devices   │ │   encrypted  │
  │ kernel  │ │ kernel  │ │ separate │ │  ~125ms     │ │   memory +   │
  │         │ │ + gofer │ │  kernel  │ │  boot       │ │  attestation │
  └─────────┘ └─────────┘ └──────────┘ └─────────────┘ └──────────────┘
  trust =     trust =     trust =      trust =         trust =
  host        sentry      VM kernel    VM kernel       hardware root
  kernel      (Go impl)   + minimal    + minimal       of trust
              + ~30       hypervisor   hypervisor      (CPU vendor)
              host
              syscalls

  syscall     intercept,  hypercall    hypercall       hypercall
  flow:       Sentry      → VM kernel  → minimal       → VM kernel
  →host       handles     → host       VMM             (memory
   kernel     in user-    kernel       → host          encrypted)
              space, calls (KVM only)  kernel          host can't
              <30 host                                 read RAM
              syscalls

  cold start: <100ms     ~200ms       ~500ms          ~200ms          ~500ms-1s
  per-syscall: 1×        2–10×        ~1.05–1.5×      ~1.05×          ~1.1×
  per-pod RAM
  overhead:   ~0          ~15-30 MB    ~50-150 MB     ~5 MB           ~50-200 MB
  needs nested
  virt:       no          no (KVM     yes (unless    yes (unless     yes (TDX/
                          mode opt.)   bare metal)    bare metal)     SNP host)
```

Reading this diagram is the entire chapter in one image. As you move right:

- The *trusted computing base* shrinks (host kernel → sentry → VM kernel → VM kernel with measured boot).
- The *attack surface from the workload* shrinks (full Linux syscall → ~30 host syscalls → hypercalls → minimal hypercalls).
- The *cost* grows (compute, memory, boot time, infrastructure).
- The *compatibility cost* grows (gVisor breaks some apps; Kata breaks some K8s features; CoCo breaks some images).

A real cluster typically runs **runc for trusted workloads (your own services), gVisor for untrusted code execution (CI runners, notebooks, untrusted PR builds, code playgrounds), Kata for regulated workloads or those needing strong kernel isolation, and CoCo for workloads where you do not trust the cloud provider's hypervisor or host operators (regulated tenants, key material, blockchain validators).**

Picking a single point on this spectrum cluster-wide is almost always wrong: trusted workloads pay isolation cost they do not need; untrusted workloads inherit isolation weaker than they deserve. The point of RuntimeClass is precisely that you do not have to pick one.

---

## 3. Threat Model: What the Sandbox Boundary Stops, and What It Does Not

Before YAML, pin down the threat model. A sandbox is not a panacea; it is an answer to a specific class of attacks.

### 3.1 What a sandbox stops

- **Host kernel CVEs reached from the workload.** A user-namespace-reachable kernel bug (the dominant class of container escapes 2018–2025) does not reach the host kernel when the workload's syscalls are handled by the Sentry (gVisor) or the VM kernel (Kata/CoCo).
- **Privileged operations escaping namespaces.** If the workload is given (or grabs) elevated privileges inside its sandbox, those privileges apply to the *sandbox* — the Sentry's userspace context, or the VM's kernel. They do not give it anything on the host.
- **Side-channel reads of other tenants' memory by the workload** (for CoCo specifically). Memory is encrypted at the page level; a snooping VM cannot peek into another VM's pages even with a hypervisor compromise (within the limits of the hardware's threat model).
- **Cloud-operator or host-admin reads of memory** (for CoCo only). Intel TDX and AMD SEV-SNP encrypt guest RAM with a key only the CPU knows, so a compromised hypervisor or a curious host admin cannot read the workload's RAM.

### 3.2 What a sandbox does not stop

- **Network attacks.** The workload still talks to other services over the cluster network. NetworkPolicy (ch 20), service mesh mTLS (ch 17), and authentication on the application layer are still required. The sandbox does not help with SSRF, with a leaked credential in an environment variable, with a vulnerable HTTP route.
- **Application bugs.** SQL injection, deserialization, prompt injection — the sandbox doesn't see those. It only contains the *blast radius* once the application is compromised, by preventing further pivot to host-kernel-mediated resources.
- **Supply-chain attacks.** A malicious base image runs *inside* the sandbox just fine. The sandbox doesn't stop a build-time backdoor from connecting out, exfiltrating data, or attacking the rest of your network. Image signing (ch 27), SBOM, and admission verification handle this; the sandbox is orthogonal.
- **Misconfigured RBAC.** A pod with kubelet-level RBAC, an over-permissive ServiceAccount, or a mounted host kubeconfig is dangerous independent of what runtime is executing it. RBAC and ServiceAccount hygiene (ch 07) are independent.
- **Hypervisor CVEs (for VM-based sandboxes).** Kata and CoCo reduce attack surface to a much smaller hypervisor (Firecracker is ~50k lines of Rust vs ~30M lines of Linux), but a CVE *in the hypervisor* still escapes. CoCo additionally trusts the CPU's confidential-computing implementation (TDX module, SEV firmware).
- **Sentry CVEs (for gVisor).** The Sentry is the trusted thing; a bug in the Sentry's syscall implementation is a sandbox escape. The bar is much higher than the host kernel's (Go memory safety, narrow attack surface, fewer LOC), but it is not zero — gVisor has had its own CVE history, just shorter.

The slogan: **a sandbox replaces one trusted computing base with a smaller, more auditable one. It does not eliminate trust; it relocates it.**

---

## 4. `RuntimeClass`: The Kubernetes Wiring

`RuntimeClass` is the Kubernetes API resource that names a non-default container runtime; pods opt in per-pod via `spec.runtimeClassName`. The full spec is in `node.k8s.io/v1` and has lived there since Kubernetes 1.20 (it was beta from 1.14).

### 4.1 The minimal RuntimeClass

```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
handler: runsc
```

That's it for the minimum. The `handler` is a **CRI runtime handler name** — a string that the container runtime (containerd, CRI-O) must be configured to recognize. We will see the containerd side in §7. The RuntimeClass object itself is global (cluster-scoped); pods reference it by name.

### 4.2 The full RuntimeClass spec

```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: kata-qemu
handler: kata-qemu          # CRI runtime handler name; must match containerd config
scheduling:
  nodeSelector:             # only schedule onto nodes labeled like this
    runtime-isolation: kata
  tolerations:              # tolerate the taint we put on kata-capable nodes
    - key: runtime
      operator: Equal
      value: kata
      effect: NoSchedule
overhead:                   # resources consumed by the VM/sandbox itself
  podFixed:
    cpu: "250m"
    memory: "120Mi"
```

The four fields:

- `handler` (required, immutable). The CRI handler name. Tied to your runtime configuration on the node.
- `scheduling.nodeSelector` (optional). Merged into every pod's `spec.nodeSelector` at admission time. Restricts pods to nodes that have the runtime installed.
- `scheduling.tolerations` (optional). Added to every pod's tolerations. Lets pods land on nodes you have tainted for sandbox-only workloads.
- `overhead.podFixed` (optional). Fixed CPU/memory overhead the scheduler accounts for *in addition to* the pod's requests. Critical for Kata; less critical for gVisor; meaningless for runc.

### 4.3 A pod opting in

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: untrusted-ci-job
spec:
  runtimeClassName: gvisor
  containers:
    - name: builder
      image: registry.internal/builder:1.2.3
      resources:
        requests:
          cpu: "1"
          memory: "1Gi"
        limits:
          cpu: "2"
          memory: "2Gi"
```

The kubelet sees `runtimeClassName: gvisor`, looks up the RuntimeClass object, learns the handler is `runsc`, and passes `runtime_handler: "runsc"` in the CRI `RunPodSandbox` request to containerd. containerd looks up its config to find which OCI runtime binary handles `runsc`, and invokes `runsc create` instead of `runc create`. The rest of the pod's lifecycle is identical from kubelet's perspective.

### 4.4 What admission and the scheduler do

A pod with `spec.runtimeClassName` going through admission:

1. **RuntimeClass admission plugin** (built into the apiserver, default-on since 1.16) looks up the RuntimeClass by name. If it doesn't exist, the pod is rejected at admission time. If it does, the plugin merges `spec.runtimeClassName`'s `nodeSelector` into `pod.spec.nodeSelector` and its `tolerations` into `pod.spec.tolerations`.
2. **Resource accounting**: the same plugin adds `overhead.podFixed` to the pod's effective resource requests. This becomes visible to the scheduler (so it counts against node allocatable) and to QoS classification (chapter 21).
3. **Scheduler** filters and scores using the post-merge spec. Pods land only on nodes that match the selector and tolerate the taint.

What the user does not need to do: set `nodeSelector` or `tolerations` themselves, or know which nodes have which runtime. The RuntimeClass is the routing primitive. This is the entire payoff: developers say "I want a sandbox", platform owners decide what that means and where it runs.

---

## 5. RuntimeClass Scheduling: `nodeSelector` and Tolerations

Sandbox runtimes are *not free to install on every node*. They require kernel features (KVM for gVisor's KVM platform; nested virt or bare metal for Kata/Firecracker; TDX/SEV-SNP CPUs for CoCo) and additional daemons. The common pattern is: one or more **dedicated node pools** carry the sandbox runtimes; the rest of the cluster is plain runc.

### 5.1 The taint-and-label pattern

A typical setup:

```bash
# label and taint the gvisor-capable node pool
kubectl label node gv-node-1 runtime-isolation=gvisor
kubectl taint node gv-node-1 runtime=gvisor:NoSchedule

# label and taint the kata-capable node pool
kubectl label node kata-node-1 runtime-isolation=kata
kubectl taint node kata-node-1 runtime=kata:NoSchedule
```

The taint means: no pod lands here unless it explicitly tolerates the taint. The label means: the scheduler can identify these nodes by selector.

The RuntimeClass binds both:

```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
handler: runsc
scheduling:
  nodeSelector:
    runtime-isolation: gvisor
  tolerations:
    - key: runtime
      operator: Equal
      value: gvisor
      effect: NoSchedule
```

Now any pod with `runtimeClassName: gvisor`:
- Has `nodeSelector: { runtime-isolation: gvisor }` injected (so it can only land on gvisor nodes).
- Has the `runtime=gvisor:NoSchedule` toleration injected (so it *will* land on those nodes, which are otherwise taint-blocked).

A pod *without* `runtimeClassName: gvisor` will not match the selector, will not tolerate the taint, and will be kept off these specialized nodes. The cluster pays the cost of sandbox-capable hardware only for workloads that asked for it.

### 5.2 Why this matters: failure modes

Get this wrong and one of two things happens:

- **No nodeSelector on RuntimeClass**: a gVisor pod can be scheduled onto a node that has no `runsc` binary. The kubelet calls `RunPodSandbox` with handler `runsc`; containerd has no such handler configured; the pod stays in `ContainerCreating` indefinitely, with an opaque "failed to find runtime" error in the kubelet log.
- **No taint on the node pool**: every other pod in the cluster competes for the sandbox nodes. The expensive nested-virt instances become general compute capacity and the workloads that needed isolation get evicted when the node fills up.

Both failures are operator-side; the RuntimeClass admission plugin will not catch them.

### 5.3 The DaemonSet workaround for installation

A common pattern: a DaemonSet installs `runsc` or `kata-runtime` onto every node tagged for it (using `nodeSelector: runtime-isolation: gvisor`). On each node, the DaemonSet writes the binary, updates `/etc/containerd/config.toml`, and sends `SIGHUP` to containerd. The GKE Sandbox installer does exactly this; the kata-deploy daemonset does the same for Kata.

Until the DaemonSet finishes installing on a node, RuntimeClass admission lets pods through, but pods fail to start. You either gate on a startupProbe or accept the race — most large operators put the DaemonSet behind a node label that the installer itself sets after success (`gvisor-ready=true`), and the RuntimeClass selector requires that label.

---

## 6. RuntimeClass Overhead Accounting

A Kata pod boots an entire microVM: a stripped Linux kernel plus the `kata-agent` plus virtio-fs and virtio-vsock plus the hypervisor's user-process overhead. That is real RAM and real CPU, attributable to the *pod* but not to any *container* in the pod. If the scheduler doesn't know about it, it will pack nodes tightly and the kubelet will start OOM-killing.

`RuntimeClass.overhead.podFixed` is the answer.

### 6.1 The semantics

```yaml
overhead:
  podFixed:
    cpu: "250m"
    memory: "120Mi"
```

At admission, the RuntimeClass plugin records this on the pod (`pod.spec.overhead`). At scheduling, the scheduler treats this as an additional, mandatory resource request: a pod with `requests: { cpu: 1, memory: 1Gi }` + overhead `{ cpu: 250m, memory: 120Mi }` consumes 1.25 CPU and 1.12 GiB of node allocatable.

The QoS classifier (chapter 21) excludes overhead from the QoS decision — the pod's QoS class is determined purely from container requests/limits. But the eviction manager *does* count overhead against node memory pressure. (See `kubernetes/pkg/kubelet/qos` and `kubernetes/pkg/scheduler/framework/plugins/noderesources`.)

### 6.2 Numbers for real runtimes

Approximate, measured on a x86_64 node with default configurations:

| Runtime | CPU overhead per pod | Memory overhead per pod | Notes |
|---|---|---|---|
| runc | 0 | 0 | (the pause container is tiny but counted normally) |
| gVisor (runsc, ptrace) | ~50 m | ~15 MiB | Sentry process; one per pod sandbox |
| gVisor (runsc, KVM) | ~50 m | ~25 MiB | Sentry + KVM context |
| Kata QEMU (default kernel) | ~250 m | ~120 MiB | VM kernel + kata-agent + qemu |
| Kata QEMU (minimal kernel) | ~150 m | ~60 MiB | with stripped guest kernel |
| Kata Cloud Hypervisor | ~150 m | ~80 MiB | smaller VMM, virtio-fs |
| Kata Firecracker | ~50 m | ~5 MiB | Firecracker's minimal device model |
| CoCo (TDX, Kata + TDVM) | ~250 m | ~150 MiB | Kata overhead + TDX guest |

These numbers move year over year as the runtimes shrink. Always measure on your kernel and your VM image — the kata-containers project publishes a `kata-collect-data` script, and the kata-deploy chart will set sensible defaults.

### 6.3 The over-packing failure mode

Forgetting `overhead.podFixed` is the single most common operational mistake with Kata. The cluster runs fine for a week. Then a deployment grows from 5 to 50 replicas and three nodes pack to apparent full utilization. The Kata pods now share what looks like 100% of node memory but is actually 130%. The kubelet's eviction manager fires on `memory.available < 100Mi`; the oldest VM pods die; the workload reschedules; the new nodes pack the same way; the cycle repeats.

The fix is the YAML above. Put it in your platform's default RuntimeClass for Kata. Add an admission policy (Kyverno, VAP) that *rejects* a Kata RuntimeClass that omits overhead.

---

## 7. containerd Configuration for Multiple Runtimes

The CRI side of the wiring is in `/etc/containerd/config.toml`. containerd maps each runtime handler name to a binary and a set of options.

### 7.1 The plugin section

```toml
version = 2

[plugins."io.containerd.grpc.v1.cri".containerd]
  default_runtime_name = "runc"

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes]

  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc]
    runtime_type = "io.containerd.runc.v2"
    [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]
      BinaryName = "/usr/local/sbin/runc"
      SystemdCgroup = true

  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
    runtime_type = "io.containerd.runsc.v1"
    [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc.options]
      TypeUrl = "io.containerd.runsc.v1.options"
      ConfigPath = "/etc/containerd/runsc.toml"

  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.kata-qemu]
    runtime_type = "io.containerd.kata.v2"
    privileged_without_host_devices = true
    pod_annotations = ["io.katacontainers.*"]
    [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.kata-qemu.options]
      ConfigPath = "/opt/kata/share/defaults/kata-containers/configuration-qemu.toml"

  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.kata-fc]
    runtime_type = "io.containerd.kata.v2"
    privileged_without_host_devices = true
    pod_annotations = ["io.katacontainers.*"]
    [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.kata-fc.options]
      ConfigPath = "/opt/kata/share/defaults/kata-containers/configuration-fc.toml"
```

The mapping:

- The TOML *key* (`runc`, `runsc`, `kata-qemu`, `kata-fc`) is the **handler name** that the RuntimeClass's `handler` field must match.
- `runtime_type` selects containerd's shim implementation. `io.containerd.runc.v2` is the standard runc shim; `io.containerd.runsc.v1` is gVisor's shim (ships with gVisor); `io.containerd.kata.v2` is Kata's shim (ships with kata-containers, supports all Kata hypervisors).
- `options.ConfigPath` is a runtime-specific config file (gVisor's runsc options, Kata's hypervisor/kernel/agent options).

### 7.2 What happens at pod start

```
kubelet                                              containerd
  │                                                    │
  │  RunPodSandbox(handler="runsc")                    │
  ├───────────────────────────────────────────────────►│
  │                                                    │
  │                                                    │ look up "runsc"
  │                                                    │ in runtimes table
  │                                                    │ → io.containerd.runsc.v1
  │                                                    │
  │                                                    │ fork shim:
  │                                                    │   containerd-shim-runsc-v1
  │                                                    │     (gVisor's shim binary)
  │                                                    │
  │                                                    │ shim calls runsc create
  │                                                    │   passing config.json
  │                                                    │   from OCI spec
  │                                                    │
  │  PodSandboxId="abc123"                             │
  │◄───────────────────────────────────────────────────┤
  │                                                    │
  │  CreateContainer(sandbox=abc123, ...)              │
  ├───────────────────────────────────────────────────►│
  │                                                    │ shim already running
  │                                                    │ → exec into sentry
  │                                                    │ via Sentry's gofer
  │  ContainerId="def456"                              │
  │◄───────────────────────────────────────────────────┤
```

containerd's `runtimes` table is the routing layer. The same containerd binary, on the same node, can simultaneously run runc, runsc, and Kata pods. The RuntimeClass name flows through unmodified as the CRI `runtime_handler` field.

### 7.3 CRI-O equivalent

CRI-O uses `/etc/crio/crio.conf` (or drop-ins under `/etc/crio/crio.conf.d/`):

```toml
[crio.runtime]
default_runtime = "runc"

[crio.runtime.runtimes.runc]
runtime_path = "/usr/bin/runc"
runtime_type = "oci"

[crio.runtime.runtimes.runsc]
runtime_path = "/usr/local/bin/runsc"
runtime_type = "oci"

[crio.runtime.runtimes.kata]
runtime_path = "/usr/bin/kata-runtime"
runtime_type = "vm"
runtime_root = "/run/vc"
```

Semantics are the same. RuntimeClass `handler: kata` maps to the `[crio.runtime.runtimes.kata]` block.

---

## 8. gVisor (`runsc`): The Userspace Kernel

gVisor (project repo `google/gvisor`, binary `runsc`) is a userspace implementation of the Linux kernel ABI, written in Go. A workload running under gVisor never makes a syscall directly to the host kernel; every syscall is intercepted and serviced by a userspace process called the **Sentry**. The Sentry implements `open`, `read`, `write`, `mmap`, `epoll`, `futex`, `socket`, etc. in Go. For operations it cannot or will not perform itself (most prominently filesystem I/O), it delegates to a separate userspace process called the **Gofer**.

The Sentry is allowed to call a tiny, curated set of host syscalls — about thirty in the default platform — to do the actual work of executing memory operations, scheduling, and I/O on the host. Everything else the workload tries to do gets handled in Go, in userspace, never reaching the host kernel.

That is the entire idea. The host kernel surface area reachable from the workload shrinks from "all of Linux's ~400 syscalls plus all of their ioctl variants" to "the ~30 syscalls the Sentry uses, in the ways the Sentry uses them." A kernel CVE in `keyctl` is not reachable; a CVE in `bpf` is not reachable; a CVE in the kernel's `io_uring` is not reachable; a CVE in `userfaultfd` is not reachable. The reachable kernel surface is dominated by `mmap`, `mprotect`, `read`, `write`, `epoll_pwait`, `futex`, `clone`, `rt_sigaction`, `tgkill`, and a handful more — and *those* host syscalls are made by Sentry code, not by attacker-controlled code.

### 8.1 What gVisor is *not*

- **Not a VM.** No hypercalls, no second kernel. The Sentry is a Go process; the Gofer is a Go process. They communicate with the host kernel via syscalls and with the workload via signals/ptrace or KVM.
- **Not seccomp+++.** seccomp restricts which syscalls the workload may make. gVisor *handles* the workload's syscalls itself; what the host sees is the Sentry's behavior, not the workload's syscall numbers translated. This is a categorically different model.
- **Not a complete Linux implementation.** Some kernel features are missing, incomplete, or stubbed. Compatibility is good but not perfect (see §11).

### 8.2 Why Go

The Sentry is written in Go for memory safety. A Sentry CVE is the trust boundary; Go's GC and bounds-checked slice semantics make whole classes of CVE (use-after-free, buffer overflow) much rarer than in C. The performance cost is real (Go function calls, GC pauses) but smaller than the cost of being wrong about isolation. The team has hardened the Go runtime itself (e.g., custom scheduler, custom memory allocator) to reduce the attack surface from the runtime.

---

## 9. gVisor Architecture: Sentry + Gofer

```
                          HOST USERSPACE
  ┌───────────────────────────────────────────────────────────────────┐
  │                                                                   │
  │  containerd-shim-runsc-v1                                         │
  │    │ fork                                                         │
  │    ▼                                                              │
  │  runsc                                                            │
  │    │  fork                                                        │
  │    ├──────────────────┬────────────────────────────┐              │
  │    │                  │                            │              │
  │    ▼                  ▼                            ▼              │
  │  ┌──────────────┐   ┌────────────────────────────────────────┐    │
  │  │   GOFER      │   │   SENTRY                               │    │
  │  │   (Go)       │   │   (Go)                                 │    │
  │  │              │   │                                        │    │
  │  │ chroot to    │   │   ┌────────────────────────────────┐  │    │
  │  │ rootfs;      │   │   │  Workload                      │  │    │
  │  │ serves       │   │   │  (the container's processes)   │  │    │
  │  │ 9P/lisafs    │   │   │                                │  │    │
  │  │ over UDS     │◄──┼───┤  every syscall intercepted     │  │    │
  │  │ from Sentry; │   │   │  by Sentry via                 │  │    │
  │  │ no host fs   │   │   │  ptrace, KVM, or systrap       │  │    │
  │  │ access       │   │   └────────────────────────────────┘  │    │
  │  │ outside      │   │                                        │    │
  │  │ chroot       │   │   Sentry implements:                  │    │
  │  └──────────────┘   │     fs, net (netstack, Go TCP/IP),    │    │
  │                     │     proc, futex, signals, threads,    │    │
  │                     │     ipc, mounts, mmap, …               │    │
  │                     │                                        │    │
  │                     │   Sentry calls allow-listed host       │    │
  │                     │   syscalls (~30): read/write,          │    │
  │                     │   mmap/mprotect, futex, epoll_pwait,   │    │
  │                     │   clone, sigaction, tgkill, …          │    │
  │                     └────────────────────────────────────────┘    │
  │                                                                   │
  └──────────────────────────────┬────────────────────────────────────┘
                                 │ ~30 allow-listed host syscalls,
                                 │ enforced by an inner seccomp
                                 ▼
  ┌───────────────────────────────────────────────────────────────────┐
  │  HOST KERNEL (Linux)                                              │
  │  (the only thing actually scheduling CPU, doing the disk I/O,     │
  │   handling the host's hardware)                                   │
  └───────────────────────────────────────────────────────────────────┘
```

### 9.1 The Sentry

- A Go process per pod sandbox (one Sentry per pod, like one pause container per pod).
- Implements the Linux ABI: about 200 syscalls fully, another ~70 partially, the rest ENOSYS.
- Owns the workload's *threads* (depending on platform; see §10) — the kernel scheduler sees Sentry threads, not workload threads directly.
- Maintains a per-sandbox state: file descriptors, mount table, network stack (Go-implemented TCP/IP via the `netstack` library — also reused by gVisor's testing, Fuchsia, and some Tailscale builds).
- Enforces an **inner seccomp filter** on its own host syscalls. Even the Sentry is only allowed to call the ~30 host syscalls it needs; if a hypothetical Sentry bug tried to use `keyctl` it would be killed by seccomp.

### 9.2 The Gofer

- A separate Go process per pod sandbox (technically one per container, though usually one per pod since pods share namespaces).
- Holds open file descriptors to the container's rootfs.
- Has been `chroot`'d into the rootfs at startup and dropped privileges.
- Speaks the **lisafs** protocol (formerly 9P2000.L) over a Unix-domain socket to the Sentry.
- Performs every filesystem operation on behalf of the Sentry: when the workload does `open("/etc/passwd")`, the Sentry asks the Gofer over lisafs; the Gofer performs the open against its chrooted view.
- Cannot escape the rootfs because of the chroot + a strict seccomp profile.

This split exists because the Sentry should not have host filesystem access. If the Sentry were compromised, it could only access what the Gofer is willing to do for it (filesystem I/O restricted to the rootfs), and could only escape the host via the ~30 syscalls already gated by seccomp.

### 9.3 The pod-sandbox model under gVisor

A multi-container pod under gVisor:

```
┌────────────────────────────────────────────────────────────┐
│  Pod sandbox                                               │
│                                                            │
│   1× Sentry         ◄── shared by all containers           │
│   1× Gofer          ◄── per container (or per pod)         │
│   N× containers     (sidecar + app + …)                    │
│                                                            │
│   All container processes run "inside" the Sentry's        │
│   view of Linux: same Sentry handles all syscalls.         │
│   Containers in the pod share the Sentry's network         │
│   namespace (netstack), pid namespace, ipc namespace.      │
└────────────────────────────────────────────────────────────┘
```

This is consistent with the OCI Pod model: a Pod is one sandbox, multiple containers, shared net + ipc + (optionally) pid. gVisor implements the sandbox; runc-style namespaces are emulated *inside* the Sentry rather than created on the host.

---

## 10. gVisor Platforms: ptrace, KVM, systrap

The interesting piece: how does the Sentry actually *intercept* the workload's syscalls? Three platforms exist; the choice trades performance for kernel-privilege requirements.

### 10.1 ptrace platform

Default until 2023ish; broadest compatibility, slowest.

- The Sentry attaches to each workload thread with `PTRACE_SEIZE`.
- Every syscall trap (or every group-stop, depending on configuration) suspends the thread; the Sentry inspects the registers, services the syscall in Go, writes the return value into the thread's registers, and resumes it with `PTRACE_SYSCALL`.
- No kernel privileges required beyond what containerd already has.
- Per-syscall overhead: 5–10× a native syscall (multiple ptrace stops, context switches, signal delivery).

The math: every workload syscall costs at least two context switches (workload → Sentry → workload) plus the actual host syscall the Sentry makes. For a `getpid()` (essentially free natively), gVisor-ptrace is ~5 µs vs native's ~50 ns. For a syscall that does substantive work (a `read` from a cached file), the overhead is proportionally smaller.

### 10.2 KVM platform

Fastest, requires `/dev/kvm` access on the node (host kernel privilege).

- The Sentry creates a KVM VM where the *workload* runs as the guest at ring 3.
- The Sentry runs in ring 0 of that VM.
- When the workload makes a syscall, control traps to the Sentry directly (VMEXIT), no host kernel involvement.
- The Sentry services the syscall in its own ring-0 context.
- Per-syscall overhead: 1.5–3× native. Much closer to runc.

This is what gVisor's "KVM platform" means: KVM is used as a *syscall interception* mechanism, not to virtualize a separate kernel. The same Sentry is running; only the way it sees the workload's syscalls changes.

KVM platform requires the node has `/dev/kvm` exposed to the runtime. On managed Kubernetes that means either bare-metal nodes or nested-virt-capable instance types (most cloud providers offer this for a price). It is GKE Sandbox's default since 2023.

### 10.3 systrap platform

Newer (mainline since 2023); a compromise.

- Uses seccomp's `SECCOMP_RET_TRAP` plus signal-based interception, rather than ptrace.
- Per-syscall overhead: 2–4× native.
- No kernel privileges beyond ptrace's requirements.
- Default in newer gVisor versions when KVM isn't available.

Documented in `runsc/platform/systrap`. Conceptually: every syscall the workload makes raises `SIGSYS`; the Sentry's signal handler catches and services it. This avoids the ptrace stop-and-resume overhead while remaining KVM-free.

### 10.4 Picking a platform

| Platform | When | runsc flag |
|---|---|---|
| KVM | Bare metal or nested-virt nodes | `--platform=kvm` |
| systrap | Default modern; no KVM | `--platform=systrap` |
| ptrace | Older kernels; broadest compat | `--platform=ptrace` |

The runsc.toml on the node selects:

```toml
[runsc_config]
platform = "kvm"     # or "systrap" or "ptrace"
network = "host"     # or "sandbox" (netstack)
debug = false
strace = false
```

---

## 11. gVisor Syscall Coverage and Compatibility Gaps

The honest fact about gVisor: it implements *most* of Linux but not *all*. The Sentry is many tens of thousands of lines of Go; the upstream Linux kernel is millions of lines of C plus subsystems (drivers, networking, filesystems) the Sentry simply has not reimplemented.

### 11.1 What works

- Standard glibc/musl userspace: `open`, `read`, `write`, `mmap`, `fork`/`clone`, `execve`, signals, threads.
- POSIX networking (BSD sockets, epoll, basic TCP/UDP).
- Most common filesystems via the Gofer (overlay, tmpfs internally, bind mounts).
- Standard Linux process model: pid namespaces, signal handling, ptrace within the sandbox.
- Java, Python, Node.js, Go, Rust standard runtimes.
- Most web frameworks, most build tools, most CI workloads.

### 11.2 What doesn't, partially or not at all

- **`io_uring`**: not implemented. Workloads using io_uring fall back to `epoll` or get `ENOSYS`.
- **`bpf`**: not implemented (intentionally — the Sentry won't expose eBPF to untrusted code).
- **`keyctl`**: not implemented.
- **`userfaultfd`**: not implemented (security-sensitive).
- **`fanotify`**: not implemented.
- **Some `fcntl` variants**: `F_OFD_*` may be incomplete; specific `F_SETLEASE`/`F_NOTIFY` semantics differ.
- **Large pages (`MAP_HUGETLB`)**: limited or absent depending on platform.
- **Certain `ioctl`s**: most device-specific ioctls return `ENOTTY`. Workloads that talk to `/dev/random` and `/dev/null` are fine; workloads that talk to GPUs, NVMe, or device files generally are not.
- **GPU/CUDA**: gVisor's GPU support landed in 2024 for NVIDIA via the `nvproxy` mechanism, but compatibility is per-driver-version. Most production GPU workloads are not in gVisor.
- **KubeVirt and nested virtualization inside the sandbox**: not supported.
- **Some database internals**: anything that uses `O_DIRECT` heavily, raw block devices, or io_uring will be slow or broken. Postgres works; high-throughput configurations may degrade. RocksDB-with-io_uring will fall back to threadpool.
- **`/proc` and `/sys` are reimplemented**: most files are there but some (e.g., `/proc/PID/maps` corner cases, `/sys/fs/cgroup` writes) behave differently.

The gVisor compatibility docs are the canonical reference (`gvisor.dev/docs/user_guide/compatibility/`). The test suite under `test/syscalls/` is the most accurate description of what works — if it has a passing test, it works.

### 11.3 How compatibility issues manifest

The user-visible failure is usually:

- `ENOSYS` from a syscall the application doesn't expect to fail → applications crash or fall back to slow paths.
- `EINVAL`/`EPERM` from an ioctl → applications crash or fall back.
- Silent slow paths: io_uring falls back to epoll-based threadpools; the application "works" but at 5× lower throughput.
- Performance cliffs that look like load problems — for example, "the same workload that handled 10k RPS on runc handles 800 RPS on gVisor" because syscall overhead dominates.

The defensive move: pilot gVisor on a *non-production* version of the workload before rolling it out. Run a representative load test. Watch `runsc strace`-style traces (gVisor logs every unsupported syscall it returns ENOSYS for) to find compatibility gaps early.

---

## 12. gVisor Performance Profile

gVisor's headline cost is *per-syscall overhead*. Workloads that are compute-bound (numeric computation, image processing, JIT'd code that touches the kernel rarely) run within a few percent of native. Workloads that are syscall-bound (high-throughput networking, lots of small file I/O, heavy `fork`/`exec`) run 2–10× slower depending on the platform.

### 12.1 Rough rules of thumb

| Workload class | gVisor cost vs runc |
|---|---|
| CPU-bound (matmul, hash, compress) | <5% |
| Memory-bound | <10% |
| Large-block sequential disk I/O | ~10–30% |
| Small-block random disk I/O | 2–3× slower |
| Network-bound steady-state (large connections, big buffers) | ~10–30% slower |
| Network-bound short-connection (high connection rate) | 2–5× slower |
| Process-spawn-heavy (fork/exec in a loop) | 3–10× slower |
| Web server doing small responses (nginx 1KB) | 2–3× slower |

### 12.2 The numbers people quote

Approximate measured results from the gVisor team's published benchmarks (and confirmed independently by GKE Sandbox SREs):

```
sysbench cpu (10s, single-thread):           native 100%   gVisor 95-97%
sysbench fileio (random read, 1MB blocks):    native 100%   gVisor 65-80%
sysbench fileio (random read, 4KB blocks):    native 100%   gVisor 25-40%
nginx (4KB response, ab -c 100):              native 100%   gVisor 40-50%
nginx (100KB response):                       native 100%   gVisor 80-90%
redis (1KB GET):                              native 100%   gVisor 30-40%
fork+exec hello-world (loop):                 native 100%   gVisor 15-25%
```

Numbers are approximate and vary by platform (KVM > systrap > ptrace), kernel version, and gVisor version. Newer gVisor (mid-2024 onward) closed many gaps; benchmarks from 2020 are pessimistic.

### 12.3 What this means for placement

- **CI workers running compilers, tests, builds**: bursty CPU, occasional fork/exec, moderate file I/O. gVisor is fine; overhead is dominated by build time itself.
- **PR-preview environments running an app server briefly**: short-lived, low traffic. gVisor cost invisible in the noise.
- **Jupyter / notebook servers / code playgrounds**: idle most of the time; brief computation bursts. gVisor cost irrelevant.
- **Public-facing web frontend handling 50k RPS**: gVisor will hurt; this is the wrong workload to sandbox without compensating capacity.
- **Postgres with 100k IOPS workload**: probably wrong runtime; consider Kata if isolation is required, or runc with strict host hardening if it isn't.

---

## 13. Kata Containers: VM-per-Pod Isolation

Kata Containers (`kata-containers/kata-containers`) takes the opposite approach from gVisor: rather than reimplementing the kernel in userspace, **boot a real Linux kernel inside a lightweight virtual machine** and run the workload there. Each Pod becomes its own VM; the host kernel and other tenants are protected by the hypervisor boundary.

The trade-off is the inverse of gVisor:

- Compatibility is *better* than gVisor (it really is Linux running inside, with most of the kernel features the workload expects).
- Per-syscall overhead is *much lower* than gVisor (workload syscalls hit the VM kernel directly).
- Cold start is *slower* (the VM has to boot — 100–500 ms versus runc's <100 ms or gVisor's ~200 ms).
- Per-pod memory overhead is *higher* (the VM kernel takes 30–150 MiB on top of the workload).
- Requires nested virtualization (or bare metal): the host must expose KVM to the runtime.

### 13.1 The architectural picture

```
                              HOST
  ┌────────────────────────────────────────────────────────────────┐
  │                                                                │
  │  containerd-shim-kata-v2          ┌──────────────────────────┐ │
  │    │                              │  HYPERVISOR              │ │
  │    │ launch hypervisor            │  (QEMU / cloud-hyp /     │ │
  │    │ + initial kernel             │   Firecracker /          │ │
  │    │ + initrd containing          │   Dragonball)            │ │
  │    │ kata-agent                   │                          │ │
  │    │                              │  ┌────────────────────┐  │ │
  │    │                              │  │  GUEST VM          │  │ │
  │    │ vsock                        │  │                    │  │ │
  │    ├──────────────────────────────┼──┤  guest kernel      │  │ │
  │    │                              │  │  (Linux, stripped) │  │ │
  │    │                              │  │                    │  │ │
  │    │                              │  │  kata-agent (PID 1)│  │ │
  │    │                              │  │   listens vsock    │  │ │
  │    │                              │  │   manages container│  │ │
  │    │                              │  │   processes        │  │ │
  │    │                              │  │                    │  │ │
  │    │  virtio-fs daemon            │  │  workload          │  │ │
  │    ├─(virtiofsd) ─────────────────┼──┤  containers        │  │ │
  │    │                              │  │  (multiple per pod)│  │ │
  │    │  virtio-blk for raw disks    │  │                    │  │ │
  │    ├──────────────────────────────┼──┤                    │  │ │
  │    │                              │  └────────────────────┘  │ │
  │    │  TAP device for networking   │                          │ │
  │    └──────────────────────────────┼──────────────────────────┘ │
  │                                                                │
  │  HOST KERNEL                                                   │
  │   - schedules hypervisor process                               │
  │   - serves KVM ioctls                                          │
  │   - virtio-fs daemon, TAP, vsock                               │
  └────────────────────────────────────────────────────────────────┘
```

The pieces:

- **kata-runtime / kata-shim** (`src/runtime` in the kata repo): the CRI shim. containerd talks to it via `io.containerd.kata.v2`. Translates CRI requests into hypervisor lifecycle + agent gRPC calls.
- **Hypervisor**: QEMU (full-featured, large), Cloud Hypervisor (Rust, smaller), Firecracker (minimal, AWS-origin), or Dragonball (Rust, Alibaba-origin, in-process). Boots the VM with the guest kernel.
- **Guest kernel**: a stripped Linux kernel, typically <10 MB, configured with only the drivers needed (virtio-fs, virtio-blk, virtio-net, virtio-vsock).
- **kata-agent** (`src/agent`): a small Rust (formerly Go) daemon that runs as PID 1 inside the VM. Listens on vsock for commands from the shim. Manages the lifecycle of workload containers inside the VM using standard Linux mechanisms (namespaces, cgroups via runc-equivalent in-VM).
- **virtio-fs / virtiofsd**: shares the workload's rootfs and any volume mounts from the host into the VM as a filesystem. Replaced the older 9P-based approach for better performance.
- **vsock**: a virtio-based AF_VSOCK socket lets the shim talk to the agent without using TCP/IP, avoiding the cluster network entirely.

### 13.2 The two-PID model

A Kata pod has *two* PID 1s:

- On the host, the **hypervisor process** is the "container" from containerd's perspective. Its PID is what kubelet sees in cgroup stats.
- Inside the VM, the **kata-agent** is PID 1 of the guest. It in turn `fork`s/`exec`s the workload's containers (using namespaces inside the VM).

This means `kubectl exec` does something more elaborate than for runc: the request goes through the kata-shim, over vsock to kata-agent, and the agent `exec`s a process inside the guest's container namespace. Logs are read by the agent and shipped back over vsock. From kubectl's perspective it looks identical; the path is materially different.

### 13.3 The kernel CVE answer

When CVE-2024-1086 dropped (netfilter use-after-free, escape via unprivileged user namespace), every runc cluster needed urgent host-kernel patching. A Kata cluster needed urgent *guest-kernel* patching: rebuild the VM's kernel image with the fix, redeploy the kata-deploy DaemonSet, restart Kata pods to pick up the new kernel. The host kernel didn't necessarily need it (the workload couldn't reach it) — though most operators patched both for defense in depth. The blast radius of an unpatched window was an order of magnitude smaller.

---

## 14. Kata Architecture: shim, agent, hypervisor, virtio-fs

Five components matter, and they all live on the same host.

### 14.1 kata-shim (`containerd-shim-kata-v2`)

Source: `kata-containers/kata-containers/src/runtime`. Written in Go (the shim) plus Rust (newer components).

- Implements the containerd shim v2 protocol; one shim per pod.
- On `RunPodSandbox`: launches the hypervisor, starts virtiofsd, configures TAP networking, waits for kata-agent to come up on vsock.
- On `CreateContainer`: sends a `CreateContainer` gRPC over vsock to kata-agent.
- On `StartContainer`: sends `StartContainer` over vsock.
- On `ExecSync`/`Exec`: proxies into the VM over vsock; the agent exec's the requested process.
- On `RemoveContainer`/`StopPodSandbox`: graceful shutdown of containers, then VM, then cleans up TAP and virtiofsd.

### 14.2 kata-agent

Source: `kata-containers/kata-containers/src/agent`. Originally Go, rewritten in Rust for binary size and memory safety. Lives inside the guest VM's initrd; runs as PID 1.

- Listens on AF_VSOCK port 1024 for gRPC from kata-shim.
- Manages container lifecycle inside the VM: creates namespaces (the VM has its *own* PID, mount, network namespaces), sets up cgroups, calls a slim runc-equivalent (`agent-ctl` or built-in) to launch the workload.
- Streams stdout/stderr back to the shim via vsock.
- Cleans up on stop.

### 14.3 The hypervisor

The kata-shim launches one of:

- **QEMU**: full-featured, large process, large attack surface, broadest device support. Default for many Kata deployments.
- **Cloud Hypervisor** (`cloud-hypervisor/cloud-hypervisor`): Rust-based, smaller than QEMU, native virtio-fs support.
- **Firecracker** (`firecracker-microvm/firecracker`): minimal device model (just net, block, serial, vsock, no PCI), originally built for AWS Lambda/Fargate. Smallest binary, lowest overhead. No virtio-fs in upstream Firecracker (uses virtio-blk-backed images); Kata's fork or external solutions handle FS sharing differently.
- **Dragonball**: Rust, in-process (linked into the shim — no separate hypervisor process). Smallest overhead; relatively new.

The hypervisor selection is per-RuntimeClass: you can have `kata-qemu`, `kata-clh`, `kata-fc`, and `kata-dragonball` RuntimeClasses on the same node, all backed by the same Kata shim with different `ConfigPath`.

### 14.4 virtio-fs and rootfs sharing

Sharing the workload's rootfs and volume mounts from the host into the VM is non-trivial because:

- The rootfs is unpacked on the host (containerd's snapshotter — overlay2, by default).
- The VM needs a *filesystem* view of that rootfs.
- Block device approaches (virtio-blk on a loop file) are slow and complex.
- 9P (the older approach) is wire-protocol-heavy and slow.

**virtio-fs** is a Linux-kernel filesystem (`fs/fuse/virtio_fs.c`) plus a userspace daemon (`virtiofsd`) plus a virtio transport. Performance is close to native, and it understands POSIX semantics (mmap, hard links, extended attributes) that 9P struggled with. virtiofsd runs on the host, opens the rootfs path, and serves it to the VM via a virtio device. The VM mounts it as `/`.

For volumes:

- `emptyDir`, `configMap`, `secret`, `downwardAPI`, `projected`: shared via virtio-fs from the host.
- `hostPath`: typically **denied** for Kata pods at admission, because exposing a host path *inside* the VM defeats the isolation (a compromised guest could write to the host path, harming the host). Some policies allow `hostPath` for trusted system paths; most disable it.
- `persistentVolumeClaim` with block volumes: virtio-blk attach.
- `persistentVolumeClaim` with filesystem volumes: usually virtio-fs of an already-mounted path on the host.

### 14.5 Networking

The pod's network namespace is set up on the host by CNI as usual. Kata then plumbs the namespace's veth into the VM through one of:

- **TAP / TC redirect**: a TAP device in the host namespace, redirected to the pod's veth via tc rules. Default for most Kata deployments.
- **macvtap**: macvtap on the pod's interface, attached to the VM.
- **VFIO**: pass through a real NIC's VF to the VM. Highest performance, requires SR-IOV.

The choice is configured in the kata configuration file and trades performance for compatibility.

---

## 15. Kata Hypervisor Choices: QEMU, Cloud Hypervisor, Firecracker, Dragonball

This is the second-most-important Kata configuration choice (after "Kata yes/no"). Each hypervisor's strengths and weaknesses:

### 15.1 QEMU

- **Pros**: most mature; broadest device support; well-understood debugging.
- **Cons**: large attack surface (~3M LOC of C); 200+ MB of binary on disk; ~100–150 MiB RAM overhead per VM; slowest boot (~500 ms).
- **Use when**: you need to attach unusual devices (GPUs via VFIO, custom PCI), or when broad operator familiarity matters.

### 15.2 Cloud Hypervisor

- **Pros**: Rust, ~50k LOC; small attack surface; native virtio-fs; boot ~200–300 ms; ~80 MiB overhead.
- **Cons**: less battle-tested than QEMU; smaller device set (no PCI passthrough for most things).
- **Use when**: you want strong isolation + reasonable boot + I/O performance, on Intel/AMD x86_64 or ARM64.

### 15.3 Firecracker

- **Pros**: minimal device model (5 devices: virtio-net, virtio-block, virtio-vsock, serial, i8042 keyboard for shutdown); ~50 MB binary; <125 ms boot; <5 MiB overhead per microVM.
- **Cons**: no virtio-fs in upstream; limited to bridged networking via TAP; no PCI; no GPU; no nested virt.
- **Use when**: you have many short-lived workloads where cold start and density dominate (FaaS, CI runners). The fact that AWS Lambda and Fargate are built on Firecracker is the production proof.

### 15.4 Dragonball

- **Pros**: in-process with kata-shim (no separate hypervisor process); Rust; smallest startup cost.
- **Cons**: newest; smaller ecosystem; Alibaba-origin (governance and roadmap somewhat insulated from broader VMM community).
- **Use when**: you want the lightest possible overhead and are happy on the bleeding edge.

### 15.5 The actual decision tree

```
Need GPU passthrough or unusual hardware?
  yes → QEMU
  no  → Need shortest cold start?
          yes → Firecracker or Dragonball
          no  → Cloud Hypervisor (good default)
```

---

## 16. Firecracker: The microVMM Behind Lambda and Fargate

Firecracker deserves its own section because it is the most influential microVMM of the last decade and the proof point for the entire "VM-per-pod" pattern.

### 16.1 What Firecracker is

- A *userspace VMM*, written in Rust, that uses KVM for the hardware boundary.
- Designed for **multi-tenant FaaS workloads** where you boot many small, short-lived VMs.
- Released by AWS in 2018; the production runtime under Lambda (since ~2018) and Fargate (since ~2019).
- Approximately 50,000 lines of Rust. Open source under Apache 2.

### 16.2 The minimal device model

Firecracker exposes exactly five virtio devices to the guest:

- `virtio-net`: a single network interface.
- `virtio-block`: one or more block devices (the guest's rootfs and any data disks).
- `virtio-vsock`: AF_VSOCK socket (for Kata-shim ↔ kata-agent communication).
- `serial`: a serial console for early-boot debugging.
- `i8042 keyboard controller`: not for typing — for sending CTRL+ALT+DEL to trigger guest shutdown.

That's it. No PCI bus, no USB, no graphics, no sound, no ACPI (in older versions). The attack surface is dramatically smaller than QEMU's: a CVE in QEMU's USB stack does not exist in Firecracker because USB does not exist in Firecracker.

### 16.3 Boot performance

- Kernel boot to userspace: ~125 ms with a properly stripped kernel image.
- VMM startup: ~5–10 ms.
- Per-VM memory overhead: ~5 MB (the VMM process itself).

The Lambda model: pre-warm pools of Firecracker microVMs, hibernate them, snapshot the guest's memory, and "restore" a snapshot in <10 ms on cold start. Snapshot/restore landed in Firecracker upstream around 2021 (`/snapshot/create`, `/snapshot/load` API endpoints).

### 16.4 Integration patterns

Firecracker is consumed several ways:

- **Kata + Firecracker**: Kata's `kata-fc` profile, where the Kata shim launches Firecracker. Standard for Kubernetes integration.
- **firecracker-containerd** (`firecracker-microvm/firecracker-containerd`): a dedicated containerd plugin/shim that talks to Firecracker directly, bypassing Kata. Older but still used in some bespoke deployments.
- **AWS Fargate**: managed Firecracker microVM per task (per pod, effectively). You don't see Firecracker; the EKS-Fargate or ECS-Fargate scheduler runs it for you.
- **AWS Lambda**: Firecracker per execution environment; you don't even see "containers" or "pods", just function invocations.

### 16.5 The Jailer

Firecracker ships a companion process called **jailer** that runs the VMM with extreme privilege drops: a separate PID/mount/net/ipc namespace, a chroot, dropped capabilities, a seccomp filter on the VMM itself. The defense-in-depth here is: even if the VMM is compromised, the jailer prevents it from reaching the host.

This is a pattern worth borrowing: every sandbox runtime worth running has hardened its own daemons (Sentry inner seccomp in gVisor, jailer for Firecracker, etc.). Don't rely on the host's security; expect the sandbox to be self-sandboxing too.

---

## 17. Cloud Hypervisor: virtio-fs Native, Rust

Cloud Hypervisor (`cloud-hypervisor/cloud-hypervisor`) is Intel-led, with co-sponsorship from Microsoft, Arm, and others, also written in Rust and using KVM. Compared to Firecracker:

- **Larger device set**: includes virtio-fs (Firecracker does not), PCI, and a more featureful virtio-net (multi-queue, offloads).
- **Slightly slower boot**: 200–300 ms vs Firecracker's 125 ms.
- **Slightly larger memory overhead**: ~50–80 MB vs Firecracker's 5.
- **Better I/O performance**: virtio-fs native makes filesystem-heavy workloads materially faster than Firecracker's "use a block device" approach.

For Kubernetes, Cloud Hypervisor is often the *better default* than Firecracker because real Kubernetes workloads use ConfigMaps, Secrets, projected SA tokens, and emptyDir — all of which want to be filesystem mounts. virtio-fs lets Kata expose them efficiently. With Firecracker, you usually end up with a virtio-blk image containing the rootfs and bind-mount fiddling.

---

## 18. VM-per-Pod vs Container-per-VM

A subtle architectural question: when Kata boots a VM, how many *containers* does it host?

### 18.1 VM-per-Pod (the default)

One VM per Kubernetes Pod. The VM hosts all containers in the Pod (init, sidecar, app). They share the VM's network and PID namespaces just as containers in a runc Pod share the host's namespaces.

- **Pros**: matches the Pod abstraction exactly; multi-container Pods (sidecars!) work naturally; cgroup accounting per container inside the VM; one VM boot per Pod creation.
- **Cons**: the entire Pod's blast radius is the VM. A sidecar compromise can reach the app container inside the same VM (via shared netns, /tmp, etc.) — the *Pod* is the sandbox, not the container.

This is Kata's default for Kubernetes. The Pod is the unit of isolation, matching the security model upstream Kubernetes already assumes (Pods are trusted units, containers within a Pod are co-deployed).

### 18.2 VM-per-Container (rare)

Some experimental configurations or specialty runtimes treat each container as its own VM. This duplicates VM overhead per sidecar and breaks Pod-internal communication patterns. Not recommended; not the upstream Kata default.

### 18.3 Multi-Pod-per-VM (rejected)

Conceivably: one VM hosting *multiple* pods. Some proposals existed (kata's "sandbox factory"). Rejected because:

- Defeats the per-Pod isolation guarantee.
- Cross-Pod scheduling is then a Kata concern, not Kubernetes'.
- Resource accounting becomes ambiguous.

Production Kata is VM-per-Pod. Memorize this and you won't get confused by old documentation.

---

## 19. Confidential Containers (CoCo): TDX, SEV-SNP, CCA

Confidential Containers is the third axis. gVisor and Kata protect against a *malicious workload* compromising the host. CoCo protects against a *malicious host* compromising the workload. The threat model includes the cloud provider, the on-prem host admin, the hypervisor itself, and any side-channel observer with kernel-level access on the host.

### 19.1 What CoCo provides

- **Memory encryption**: guest RAM is encrypted by the CPU with a key the host cannot read. Intel TDX, AMD SEV-SNP, and ARM CCA all do this in hardware (each with a slightly different model). The hypervisor can schedule the VM, deliver interrupts, give it memory, but cannot *read* the encrypted pages.
- **Integrity protection**: pages cannot be tampered with by the host without detection.
- **Attestation**: the VM produces a hardware-signed quote of its boot measurements (kernel hash, initrd hash, configuration). A relying party can verify the quote and decide whether to release secrets.
- **Encrypted images**: the container image itself can be encrypted, with the decryption key released only after attestation.

### 19.2 The trust chain

```
   CPU vendor (Intel / AMD / Arm)
        │ root of trust: TDX module / SEV firmware
        ▼
   Hardware (encrypts guest memory)
        │
        ▼
   Guest VM (boots a TD / SEV-SNP guest)
        │ measures kernel + initrd + config
        ▼
   Attestation report (signed by CPU)
        │ sent over network to verifier
        ▼
   Trustee / KBS (your relying party)
        │ verifies report against policy
        ▼
   Releases secrets to the guest:
     - image decryption key
     - image-pull credentials
     - application secrets (DB passwords, …)
```

The host kernel, the hypervisor, and the cloud provider's operators are *not* in the trust chain. Even if they are fully compromised, they cannot read the workload's memory.

### 19.3 Hardware comparison

| Feature | Intel TDX | AMD SEV-SNP | Arm CCA |
|---|---|---|---|
| Generation | Xeon SPR (4th gen) + | EPYC Milan + | Armv9-A |
| Granularity | TD (Trust Domain) = a guest VM | SNP guest = a guest VM | Realm = an isolated workload |
| Memory encryption | Per-TD ephemeral key | Per-VM ephemeral key | Per-realm |
| Integrity | yes (RTMR + paging structures) | yes (RMP) | yes (GPT) |
| Attestation source | TDX module → CPU | PSP (Platform Security Processor) | RMM (Realm Management Monitor) |
| Public-cloud availability | Azure (DCesv5/ECesv5), GCP (C3), AWS (announced) | Azure (DCasv5/ECasv5), GCP, AWS (M6a/C6a SNP) | None production yet (2025/2026) |

CoCo support varies by hardware generation; check vendor docs.

### 19.4 The CoCo stack

CoCo (`confidential-containers/`) is *not* a new runtime. It is a *configuration of Kata* plus additional components:

- **kata-containers** with a TDX/SEV-SNP-aware hypervisor (QEMU with confidential-VM support, or Cloud Hypervisor patched accordingly).
- **A confidential guest image** containing a measured kernel + initrd + kata-agent + the confidential-data-hub.
- **Trustee / Key Broker Service (KBS)**: an off-cluster (or in-cluster but separately secured) verifier that holds policies and secrets, releases them after attestation.
- **CoCo operator** (`confidential-containers/operator`): a Kubernetes operator that deploys CoCo runtimes (RuntimeClasses), the confidential-data-hub, and configuration to nodes.
- **Encrypted images**: built with cosign + container image encryption tooling.

### 19.5 What it costs

- Hardware: confidential-computing CPUs (premium SKUs in cloud).
- Boot time: TDX/SEV guests take longer to boot (~500 ms to 1 s) due to measurement + attestation roundtrip.
- Memory: encrypted memory has slightly higher per-access overhead (cache invalidation patterns differ); 5–10% workload slowdown is typical.
- Operational complexity: running a KBS, managing attestation policies, building encrypted images — each is its own ops surface.

CoCo is not "more secure Kata for everyone." It is "Kata with a defense against the host, for the small set of workloads that cannot trust the host." That is regulated workloads (PCI, HIPAA at the highest classification), key material custodians, blockchain-validator-like attestation-sensitive workloads, and some sovereign-cloud scenarios.

---

## 20. Attestation: Trustee, KBS, and the Secret-Release Flow

The hardest operational piece of CoCo is the *attestation flow*. The mechanics:

```
   GUEST VM (TDX/SEV-SNP)             KBS / TRUSTEE (off-VM)
   ─────────────────────────          ──────────────────────────

   1. Boot completes; kernel +
      initrd hash + configuration
      → measurement registers
                  │
                  ▼
   2. confidential-data-hub asks
      CPU for attestation quote
                  │
                  ▼
   3. CPU signs measurement
      with vendor key
                  │
                  ▼
   4. Quote sent over network
      to KBS (over TLS)              ─► 5. KBS receives quote
                                              │
                                              ▼
                                         6. KBS verifies
                                              - vendor signature
                                              - measurement matches
                                                policy
                                              - any additional
                                                policy (geo, time,
                                                workload identity)
                                              │
                                              ▼
                                         7. If pass: KBS releases
                                              secrets:
                                              - image pull token
                                              - image decryption key
                                              - app secrets (PG pw)
                                              │
                                              │ TLS, mTLS, or DICE
                                              ▼
   8. Guest receives secrets    ◄────
                  │
                  ▼
   9. Guest pulls image (using
      pull token), decrypts
      image layers (using decrypt
      key), starts workload (with
      app secrets in env / FS)

   10. Workload runs in
      encrypted memory; even
      host admin cannot read
      RAM or steal secrets
```

### 20.1 Trustee components

`confidential-containers/trustee` is the canonical implementation, providing:

- **KBS (Key Broker Service)**: HTTP API that receives attestation quotes, calls a Verifier, applies Policy, and releases secrets from its Resource Store.
- **AS (Attestation Service)**: the actual verifier — checks the quote's CPU vendor signature, parses the measurements, and decides whether the boot was "expected."
- **RVPS (Reference Value Provider Service)**: stores the *expected* measurements (golden kernel hash, golden initrd hash, etc.). Updated when you rebuild your guest image.
- **Policy engine**: Rego (OPA) policies that decide which secrets a given quote is entitled to.

### 20.2 The deployment topology

You typically run Trustee/KBS *outside the cluster being protected* — on a separate management cluster, an on-prem appliance, or a cloud HSM-backed service. The point: a host-admin on the workload cluster cannot exfiltrate secrets from a host they own; the secrets live elsewhere and are only released after they prove they are the right thing.

For development clusters you can run Trustee inside the cluster, but you have effectively put the security back on the cluster's RBAC and you no longer get the "untrusted host" property.

### 20.3 Where it gets operationally painful

- **Reference values**: every time you update the guest kernel, initrd, or kata-agent, the measurement changes. RVPS must be updated, atomically, or pods fail to start because the new measurement doesn't match policy. This is a CI pipeline problem.
- **Quote freshness**: replay-attack resistance requires nonces in the quote flow. Misconfiguration here is silent.
- **Network availability**: pods can't start if they can't reach KBS. Treat KBS like a critical control-plane dependency.
- **Key rotation**: rotating the CPU-vendor keys (rare but happens) requires updating verifier roots.

For 90% of operators, the right operational model is: managed CoCo via a cloud provider (Azure Confidential Containers, Red Hat OpenShift sandboxed containers with CoCo), where the attestation infra is operated for you. Running your own Trustee is hard.

---

## 21. Encrypted Container Images

A natural pairing with CoCo: if the workload memory is hidden from the host, but the workload image is pulled in cleartext from a registry the host can read, you have only solved half the problem (the host can read the image, learn the binary, and study it for vulnerabilities or extract embedded secrets).

**Encrypted container images** encrypt the layer tarballs at build time, with a key released only after attestation.

### 21.1 The cosign / OCI encryption flow

OCI image encryption (`containers/image-encryption` reference design; tooling in `cosign`, `imgcrypt`):

```
   BUILD TIME                            REGISTRY
   ─────────────                         ────────────

   docker build -t myapp:v1
        │
        ▼
   cosign encrypt --key kms://... \      ─►  myapp:v1 (encrypted)
     myapp:v1                              layers are AES-encrypted
                                            with per-layer DEKs
                                            wrapped by a KEK
                                            (held by KMS / KBS)

   PULL TIME (only inside a CoCo VM):

   guest: pull myapp:v1
        │ layers download (encrypted)
        ▼
   guest: ask KBS for KEK → attestation
        │
        ▼
   KBS verifies attestation, releases KEK
        │
        ▼
   guest unwraps DEKs, decrypts layers,
   container starts normally
```

### 21.2 The CoCo image policy

A pod uses an encrypted image by referencing it normally and adding an annotation that tells the confidential-data-hub which images to decrypt:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: confidential-payments
  annotations:
    io.containerd.cri.runtime-handler: kata-tdx
    io.katacontainers.config.pre_attestation.enabled: "true"
spec:
  runtimeClassName: kata-tdx
  containers:
    - name: app
      image: registry.internal/payments-encrypted:v3.2.1
      env:
        - name: DB_PASSWORD
          valueFrom:
            secretKeyRef:                  # released by KBS after attestation
              name: payments-secret
              key: db-pw
```

And a corresponding KBS policy (Rego):

```rego
package kbs.policy
import future.keywords.if

default allow := false

allow if {
    input.tee == "tdx"
    input.measurements.kernel == "sha256:abc123..."
    input.measurements.initrd == "sha256:def456..."
    input.workload == "payments-encrypted"
    input.namespace == "payments-prod"
}
```

This pattern — image encryption + attestation policy — is what makes CoCo more than "Kata with extra steps." Without it, you've merely encrypted memory; the binary and the secrets you put into the environment are still extractable. With it, the *whole supply chain* is gated on attested boot.

---

## 22. Choosing: gVisor vs Kata vs CoCo

A decision matrix that takes into account both threat model and operational reality.

### 22.1 The matrix

| Question | gVisor | Kata | CoCo |
|---|---|---|---|
| Threat: host kernel CVE | mitigated | mitigated | mitigated |
| Threat: malicious workload escaping | mitigated | mitigated | mitigated |
| Threat: malicious host/cloud admin reads memory | not mitigated | not mitigated | **mitigated** |
| Threat: side-channel attacks against memory | not mitigated | not mitigated | partially |
| Per-syscall overhead | 2-10× | ~1.05-1.5× | ~1.1-1.5× |
| Cold start | ~200 ms | 100–500 ms | 500 ms – 1 s |
| Per-pod memory overhead | 15–30 MiB | 50–150 MiB | 50–200 MiB |
| Needs nested virtualization | no (KVM platform optional) | yes (or bare metal) | yes (and confidential CPU) |
| Compatibility | most apps; some kernel features missing | almost all apps | almost all apps (image must be encryption-compatible) |
| Operational complexity | low (one binary + runtimeclass) | medium (kata-deploy + nested virt) | high (KBS + reference values + encrypted images) |
| GPU support | partial (nvproxy) | yes (VFIO passthrough) | nascent |
| Mature production use | Google (GKE Sandbox), CI providers | OpenShift, Azure, Alibaba | early adopters, regulated tenants |

### 22.2 The decision tree

```
Do you need to defend against the cloud provider / host admin?
│
├── yes → CoCo (TDX/SEV-SNP via Kata)
│
└── no → Does the workload need full Linux kernel compatibility?
         │
         ├── yes (uses io_uring, BPF, KubeVirt, weird ioctls)
         │   → Kata
         │
         └── no
              │
              ├── Is the cluster mostly small, short-lived,
              │   syscall-light workloads (CI, sandbox code
              │   execution, notebooks)?
              │   → gVisor
              │
              ├── Is per-pod cold start a hard constraint?
              │   <200 ms → gVisor (with KVM platform)
              │   <500 ms → Kata + Firecracker / Dragonball
              │   <1 s    → Kata + QEMU is fine
              │
              └── Is the workload syscall-heavy
                  (databases, network proxies)?
                  → Kata (gVisor's syscall overhead will hurt)
```

### 22.3 Mixing them

A real platform often runs all three:

- runc for trusted internal services (90% of pods).
- gVisor for untrusted code execution paths (CI runners, notebooks, public-facing risky paths).
- Kata for tenants who paid for "VM isolation" or who run kernel-feature-heavy workloads.
- CoCo for the regulated subset of tenants who require it (banking, healthcare PHI of the highest classification).

Different RuntimeClasses; different node pools; different pricing tiers if you're a platform team. The decision is per-workload, which is exactly the point.

---

## 23. Performance Benchmarks: cpu, fileio, syscall-heavy, cold start

Numbers people quote; treat them as order-of-magnitude not precise. (Sources: kata-containers/kata-containers test suites, gVisor's published benchmarks, AWS Firecracker whitepaper, the K8s sandbox SIG.)

### 23.1 sysbench cpu (compute-bound, 10-second test)

```
runc       100% (baseline)
gVisor      95–97%   (close to native; syscall count low)
Kata        95–98%   (close to native; one kernel boundary)
Firecracker 95–98%   (same; Firecracker is a Kata back-end)
CoCo        88–94%   (memory encryption overhead)
```

### 23.2 sysbench fileio (4 KB random read)

```
runc       100%
gVisor      25–40%   (every read goes through Sentry + Gofer)
Kata        65–80%   (virtio-fs; one extra copy)
Firecracker 50–70%   (virtio-blk; no virtio-fs upstream)
CoCo        60–75%   (Kata + memory encryption)
```

### 23.3 Syscall-heavy (nginx 1 KB response, ab -c 100)

```
runc       100%
gVisor      40–55%
Kata        80–90%
Firecracker 75–85%
CoCo        70–80%
```

### 23.4 Cold start (time from CreateContainer to /proc/1 running)

```
runc          <100 ms
gVisor         150–250 ms (Sentry + Gofer startup)
Kata QEMU      400–700 ms (full QEMU boot)
Kata CLH       250–400 ms (Cloud Hypervisor)
Kata FC        150–300 ms (Firecracker)
Kata FC + snapshot   <50 ms (snapshot/restore where supported)
CoCo TDX       600 ms – 1.5 s (boot + measurement + attestation)
```

### 23.5 Memory overhead per pod (above the workload itself)

```
runc          ~0
gVisor         15–30 MiB
Kata QEMU      100–200 MiB
Kata CLH       60–150 MiB
Kata FC        5–60 MiB
CoCo TDX       100–250 MiB (Kata + TDX VM metadata)
```

### 23.6 What to actually measure

These published numbers are years old by the time you read them; everything trends down over time. Always run your *actual* workload through the sandbox and measure:

- p50 / p99 request latency.
- Throughput at saturation.
- Cold-start distribution (this is where percentiles really matter; cold start under load can spike).
- Memory consumption under steady state (the overhead per pod compounds at fleet scale).

For a 5000-pod cluster, an extra 80 MiB per Kata pod is 400 GiB of fleet memory. Decide accordingly.

---

## 24. Cluster Deployment Patterns: Tainted Node Pools, GKE Sandbox, EKS+Bottlerocket

### 24.1 The pattern: tainted node pools

The dominant production pattern is a *segregated node pool* tagged for the runtime, taint-protected from non-sandbox workloads:

```
                   Cluster
                      │
        ┌─────────────┼─────────────┬───────────────┐
        ▼             ▼             ▼               ▼
   default-pool   gvisor-pool   kata-pool       coco-pool
   runc only      runc+gVisor   runc+Kata       runc+Kata+TDX

   labels:        labels:        labels:         labels:
   (none special) runtime=gvisor runtime=kata    runtime=coco-tdx

   taints:        taints:        taints:         taints:
   (none)         runtime=gvisor runtime=kata    runtime=coco-tdx
                  :NoSchedule    :NoSchedule     :NoSchedule

   instance:      instance:      instance:       instance:
   e.g.,          KVM-capable    nested-virt     SPR / SNP-capable
   c5.2xlarge     (m5)           (c5.metal)      (c3-tdx)
```

This pattern is supported natively by managed Kubernetes:

- **GKE Sandbox** (`google_container_node_pool` with `sandbox_config { sandbox_type = "gvisor" }`): GKE installs runsc, configures containerd, adds the RuntimeClass, taints the pool.
- **EKS with Bottlerocket**: Bottlerocket OS supports Kata via `kata-containers` configuration; EKS managed node groups can run Bottlerocket variants; you label/taint the node groups.
- **AKS Confidential Containers**: managed CoCo runtimes on TDX/SEV-SNP node pools; KBS is provided as a managed service.

### 24.2 Karpenter / cluster autoscaler integration

If you use Karpenter, you express sandbox node pools as `NodePool` resources with taints and labels:

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: gvisor
spec:
  template:
    metadata:
      labels:
        runtime-isolation: gvisor
    spec:
      taints:
        - key: runtime
          value: gvisor
          effect: NoSchedule
      requirements:
        - key: karpenter.k8s.aws/instance-family
          operator: In
          values: ["c5", "m5"]
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
      startupTaints:
        - key: gvisor-installing
          effect: NoSchedule
          value: "true"
      nodeClassRef:
        name: gvisor-installer
```

The `startupTaints` keeps pods off the node until a DaemonSet finishes installing runsc and removes the taint, avoiding the race where pods land before the runtime is ready.

### 24.3 Day-2: upgrading the runtime

Sandbox runtimes have CVEs too. Plan for:

- gVisor: rolling node-pool replacement when a new runsc is released. The runsc binary needs reinstall; containerd needs a restart; running pods need to be drained.
- Kata: kata-agent inside running VMs is hard to upgrade. Reboot the VM (drain + restart pod) or live-migrate (rarely supported).
- CoCo: when reference values change (kernel or agent update), KBS policy must be updated *atomically* with the rollout.

A staff-eng playbook: treat sandbox-runtime upgrades as you treat host-kernel upgrades — drain, replace, monitor. Not as you treat application image updates.

---

## 25. Use Case: gVisor in CI and Multi-Tenant Code Execution

The single largest production use of sandbox runtimes is "running untrusted code." Every platform that executes user-supplied code uses *some* form of sandbox; gVisor is the dominant choice for many.

### 25.1 The pattern

- A user (anonymous or weakly authenticated — public PR author, free-tier signup, browser-tab visitor) submits code.
- The platform runs that code in a container.
- The container must not be able to:
  - read other users' data on the same host.
  - exfiltrate cloud credentials from the node.
  - escape to the host kernel.
- Cold start budget is tight (sub-second to a few seconds).
- Compatibility is fine as long as common languages (Python, Node, Bash) work.

This matches gVisor's strengths exactly: kernel CVE protection, broad-enough compatibility, modest cold start.

### 25.2 Real-world examples

- **GitHub Codespaces** — sandboxed environments per user; rumored Kata-based but published sources indicate variants.
- **Replit** — gVisor for every user repl.
- **GitPod** — runs user workspaces with strong isolation; previously gVisor.
- **Render preview environments, Vercel, Netlify**: each preview from a PR can run in a sandbox.
- **Bytebase, Hex.tech, observable.com** — anywhere user-supplied code touches a shared cluster.

### 25.3 The minimal CI-runner-on-gVisor pod

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: pr-1234-build
  labels:
    workload-type: untrusted-build
  annotations:
    io.containerd.cri.runtime-handler: runsc
spec:
  runtimeClassName: gvisor
  restartPolicy: Never
  serviceAccountName: untrusted-runner   # no projected K8s token!
  automountServiceAccountToken: false
  enableServiceLinks: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 65534
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: builder
      image: registry.internal/ci-runner:1.4.2
      command: ["/bin/sh", "-c", "git fetch ... && make build"]
      resources:
        requests:
          cpu: "1"
          memory: "1Gi"
        limits:
          cpu: "2"
          memory: "2Gi"
          ephemeral-storage: "10Gi"
      env:
        - name: PR_REPO
          value: "user/repo"
        - name: PR_REF
          value: "refs/pull/1234/head"
      volumeMounts:
        - name: workspace
          mountPath: /workspace
  volumes:
    - name: workspace
      emptyDir:
        sizeLimit: 10Gi
```

Notes:

- `runtimeClassName: gvisor` is the sandbox boundary.
- No automount of the ServiceAccount token — the workload can't talk to the apiserver even if it wanted to.
- `enableServiceLinks: false` keeps cluster-internal service environment variables out of the env.
- `runAsNonRoot` + `seccompProfile: RuntimeDefault` are the *complement* — chapter 28 policy stuff that still applies on top of gVisor.

### 25.4 What you still owe

Even with gVisor:

- NetworkPolicy restricting egress (don't let the build pod scan the cluster).
- Resource quotas (a malicious build can still try to exhaust node resources).
- Image whitelisting (the build image should be one you control).
- Audit logging (you want a record of what ran).

gVisor stops the *host kernel* compromise. NetworkPolicy + admission + audit stop the rest.

---

## 26. Use Case: Kata in Regulated Workloads (PCI, HIPAA)

The second-biggest production use is regulated industries that need an *auditable* hardware-isolation boundary between tenants on shared infrastructure.

### 26.1 Why hardware isolation specifically

Regulatory frameworks (PCI-DSS, HIPAA, FedRAMP at moderate/high, financial regulators in various jurisdictions) often require that tenant data be isolated by *more than* logical separation. The auditor's question is "what physically prevents tenant A from reading tenant B's data?" "Linux namespaces" is not always an acceptable answer; "each tenant runs in its own VM, with a hypervisor enforced boundary" usually is.

Kata gives you that without re-architecting your application; the Pod abstraction is preserved, the VM boundary is invisible to the workload, and the audit story becomes "every PCI-scope pod runs as a separate VM under the kata-qemu runtime class."

### 26.2 The pattern

- Tag PCI-scope namespaces with a label `scope=pci`.
- A Kyverno or VAP policy mutates pods in `scope=pci` namespaces to set `runtimeClassName: kata-qemu`.
- The kata-qemu RuntimeClass schedules onto a dedicated PCI-scope node pool with confidential-computing-grade configuration.
- Network segmentation isolates the PCI namespace's pods at L3 (NetworkPolicy, calico GlobalNetworkPolicy).
- Audit logging captures every API operation against PCI resources.

Audit evidence: "every pod with PCI data was scheduled with `runtimeClassName: kata-qemu`, which causes a per-pod VM; we can show the audit log of pod creates, the runtime configuration, and the hardware-virtualization-enabled node attestation."

### 26.3 The escalation to CoCo

If the regulator also doesn't trust the cloud provider (sovereign clouds, certain banking jurisdictions), CoCo becomes mandatory. The host operator literally cannot read tenant memory. This is the regulatory pitch for CoCo: it converts "trust the provider" into "trust Intel's TDX module" (or "AMD's SEV firmware"), which for some regulators is acceptable when "trust the provider" is not.

---

## 27. What Sandboxes Do Not Protect Against

A recap, because this is where teams get burned operationally.

1. **Network attacks.** A compromised workload can still talk to your internal services, attempt SSRF, reach external endpoints. Required complement: NetworkPolicy (ch 20), egress controls, mesh mTLS (ch 17).
2. **Identity leakage.** A workload with a ServiceAccount token can still call the apiserver. Required complement: bound projected SA tokens (ch 07), `automountServiceAccountToken: false` for untrusted workloads, RBAC scoped tightly.
3. **Environment-variable credentials.** A compromised workload reads its own env. If you put a DB password there, it leaks. Required complement: workload identity (IRSA, Workload Identity), Vault sidecar injection, short-lived credentials.
4. **Application-layer compromise.** Sandboxes don't see SQL injection, deserialization bugs, prompt injection. They contain the blast radius *after* compromise; they don't prevent it. Required complement: WAF, application hardening, dependency scanning.
5. **Supply-chain compromise.** A malicious image runs inside the sandbox just fine. Required complement: image signing (ch 27), SBOM, admission verification.
6. **Misconfigured RBAC.** A privileged ServiceAccount lets a compromised pod do anything its role allows. Required complement: least-privilege RBAC, scoped roles, audit.
7. **Sidecar trust.** Containers in the same Pod share the sandbox. A compromised sidecar can attack the app container *inside* the VM/Sentry. Required complement: pod design — keep untrusted containers out of pods with trusted ones.
8. **Hypervisor / Sentry CVEs.** Smaller surface, not zero. Required complement: keep the runtime updated; defense in depth (NetworkPolicy + PSA + sandbox).
9. **Side-channels.** Memory-encryption-based defenses (CoCo) still face Spectre-like microarchitectural side channels in some configurations. Required complement: vendor microcode updates, secure-VM configuration, awareness.

The slogan again: **a sandbox is a kernel-isolation boundary. It is not a substitute for the other security boundaries you owe.**

---

## 28. Picking the Isolation Boundary

The general principle behind everything in this chapter: **isolation has many layers, each with a cost; pick the layer that matches your trust boundary.**

```
   Trust boundary             Mechanism                    Cost
   ────────────────           ─────────────                ──────

   Inside one process         language sandbox (V8, JVM)   minimal
                              (e.g., npm package
                              boundary)
   ────────────────           ─────────────                ──────
   Between processes,         Linux namespaces +           negligible
   same kernel                cgroups (runc)
   ────────────────           ─────────────                ──────
   Between syscall            seccomp + AppArmor /         minimal
   surfaces                   SELinux profiles
   ────────────────           ─────────────                ──────
   Different kernel           gVisor (userspace            modest
   surfaces, same             kernel)                      (2-10× syscalls)
   trust domain
   ────────────────           ─────────────                ──────
   Different kernels          Kata VM                      moderate
                                                           (boot + RAM)
   ────────────────           ─────────────                ──────
   Different memory            CoCo (TDX / SEV-SNP)         significant
   trust domain                                            (CPU SKU +
                                                           attestation
                                                           infra)
   ────────────────           ─────────────                ──────
   Different cluster           vCluster / separate         high
                              cluster (ch 25, 26)
   ────────────────           ─────────────                ──────
   Different network          dedicated VPC / separate     very high
                              cloud accounts
   ────────────────           ─────────────                ──────
   Different physical          dedicated bare-metal /       maximum
   machine                    air-gap
```

A useful exercise for an architecture review: write down the trust boundary you actually have ("I trust my own engineers; I don't trust user-uploaded code"), and find the cheapest mechanism on this table that crosses it. Anything cheaper isn't enough; anything more expensive is paying for isolation you don't need.

Most security incidents in Kubernetes happen because the mechanism picked is *cheaper than the trust boundary requires*. A few happen because someone picked *more expensive* than needed and the system became unmaintainable.

---

## 29. Limitations and Gotchas

A non-exhaustive list of the rough edges you will hit.

### 29.1 gVisor

- **No `io_uring`.** Modern Postgres, ScyllaDB, RocksDB with `--use-direct-io-for-flush-and-compaction` etc. all benefit from io_uring. gVisor doesn't expose it; you'll silently fall back to threadpool I/O.
- **No `bpf`.** No eBPF observability inside the sandbox; no in-workload tools like bcc or bpftrace.
- **No KubeVirt or nested virt.** Don't try to run VMs inside gVisor.
- **GPU support is fragile.** `nvproxy` works for some NVIDIA driver versions; assume incompatibility until you've tested.
- **`/proc` quirks.** Files exist but with subtly different content (e.g., `/proc/self/maps` may not show JIT mappings as expected); JIT-heavy workloads sometimes break.
- **Some packages assume kernel >= X.** Sentry advertises a kernel version, but specific features (e.g., `userfaultfd`-based syscall facades, `clone3`-specific flags) may not be present. Apps that probe with `uname()` and assume features work get surprised.
- **Performance cliffs.** A workload that profiles as "fine" on average can have terrible p99 if it does occasional bursts of small syscalls (process spawn, signal storms).

### 29.2 Kata

- **Needs nested virtualization** — managed K8s typically charges premium for this.
- **`hostPath` is denied.** Default kata-runtime configuration rejects pods with `hostPath` mounts because exposing host paths into the VM defeats the isolation. Same for `hostNetwork`, `hostPID`, `hostIPC`. Privileged DaemonSets won't run as Kata.
- **Privileged containers fall back to runc** (in some configurations) or are rejected. Kata + `privileged: true` is a contradiction in many setups.
- **Cold start cost compounds.** A Deployment scaling from 0 to 100 replicas takes seconds longer per replica. Fine for steady state, painful for autoscaling bursts.
- **VM vCPU sizing matters.** Kata defaults to a fixed vCPU count for the VM; if you under-size it, the workload throttles inside the VM in ways that don't show up in the pod's CPU.max metric.
- **Live migration is rare.** A Kata pod can't migrate its VM to another node; for upgrades you drain and re-create.
- **Networking quirks.** TAP, macvtap, vhost-user-net each have different MTU defaults and multicast/IPv6 behavior; CNI-Kata interaction is a known source of mystery bugs.

### 29.3 CoCo

- **Attestation backend (Trustee/KBS) is mandatory and a SPOF.** No KBS → no secret release → workload doesn't start. Treat it like etcd.
- **Encrypted images add a build-pipeline step.** Every release: encrypt + sign + publish; every guest update: re-measure + update reference values.
- **Reference value drift.** A guest kernel update without a coordinated KBS policy update bricks the pod. Build them together.
- **Cost.** TDX/SEV-SNP nodes are premium. Memory-encryption overhead applies.
- **Cloud-provider scope.** CoCo's guarantees against a "rogue cloud admin" are only as strong as the CPU vendor's threat model. Read the Intel TDX / AMD SEV-SNP threat-model documentation carefully.

### 29.4 RuntimeClass admission gotchas

- Missing RuntimeClass → pod admission rejected (good).
- RuntimeClass with no nodeSelector → pod schedules onto incompatible node → stuck in ContainerCreating (bad; silently).
- RuntimeClass with overhead but you forgot to update the pool's allocatable → over-packing (silent; eviction storm).
- RuntimeClass merged with conflicting tolerations from the user's pod spec → unexpected node placement.

### 29.5 The "sandbox as policy" trap

A common mistake: using a sandbox as a *policy enforcement mechanism* rather than an *isolation* mechanism. Examples of misuse:

- "gVisor doesn't allow `io_uring`, so I'll use it to prevent io_uring." (No — use seccomp, which is purpose-built; gVisor is for isolation, not policy.)
- "Kata can't read host paths, so I'll use it as a hostPath-prevention mechanism." (No — use PSA/Kyverno/VAP at admission.)

The sandbox is a *consequence* of trust boundary, not a *substitute* for explicit policy.

---

## 30. Operating a Sandbox-Enabled Cluster

The operational disciplines that you have to layer on once you have sandboxed pods.

### 30.1 Node lifecycle

- **Kata pods can't migrate.** Treat sandbox nodes as *cattle* — drain, terminate, replace. Don't try to be clever with VM live migration.
- **Drain order matters.** Drain a sandbox node like you would any node: PDBs, graceful eviction, retry. Sandbox pods often take longer to terminate (graceful shutdown inside the VM).
- **Image GC** on the node still applies; sandbox runtimes use the same containerd snapshotter so image dedup still works.

### 30.2 Debugging

- **`kubectl exec` into a Kata pod** goes through the shim and over vsock; works the same to the user, but a hung kata-agent or a hung virtiofsd will make it hang. Have a path to ssh into the host as a fallback.
- **Logs**: stdout/stderr come back over vsock from kata-agent → kata-shim → containerd → CRI log path. A broken vsock means broken logs. `crictl logs` works the same; the path on disk is the same.
- **`kubectl describe pod` shows the same things**; the runtime field tells you which runtime.
- **`crictl inspectp <sandbox>` will tell you `runtimeHandler`** — confirm what actually ran, not what you asked for.
- **`runsc debug` and `kata-runtime`/`kata-collect-data`** are the debug paths into the runtime itself.
- **gVisor strace mode** (`runsc --strace`) logs every syscall the workload makes — invaluable for compatibility debugging.

### 30.3 Resource accounting

- `RuntimeClass.overhead.podFixed` must reflect actual usage; over-pack at your peril.
- The QoS class (Guaranteed/Burstable/BestEffort, chapter 21) is computed from container requests/limits only; overhead doesn't affect it. But the *node-level* accounting includes overhead.
- cAdvisor reports cgroup stats for the runtime's host-side cgroups; for Kata that's the hypervisor process, not the workload inside the VM. Inside-the-VM metrics need kata-agent integration or in-VM agents.

### 30.4 Compliance and audit

- Every sandbox pod's `runtimeClassName` shows up in audit logs (chapter 28); you can prove the runtime selection for any pod, retroactively.
- If you use CoCo, the KBS logs attestation events — *that* is the auditable "this workload actually attested" trail.

### 30.5 Cost

- Track per-RuntimeClass cost: number of pods, node-pool spend, overhead RAM. Sandboxed pods are typically 1.5–3× the cost of runc pods at fleet scale. Justify them per workload.

---

## 31. Other Sandbox Approaches: Nabla, Sysbox, Fargate

A short survey of the also-rans and adjacent technologies; useful for context, mostly *not* what you should run.

### 31.1 Nabla Containers

- IBM Research, ~2017–2019.
- Library-OS approach: take rumprun/unikernel concepts, run as a normal process with an extreme seccomp filter (only ~9 host syscalls).
- Workload must be linked against Nabla's library OS — not a drop-in for arbitrary container images.
- Research direction; not a production runtime.

### 31.2 Sysbox

- Originally Nestybox, acquired by Docker, now under various stewardship.
- Builds on top of runc; adds capabilities to safely run Docker-in-Docker, systemd-in-container, nested containers.
- *Not* a sandbox in the gVisor/Kata sense (still shares the host kernel).
- *Is* a useful tool when you need to run dev/test workloads that themselves expect to be inside a VM (e.g., CI runners running `docker build`).

### 31.3 AWS Fargate

- Managed Firecracker microVM per task (per pod, in the EKS case).
- You don't see the runtime; AWS operates it.
- Effectively "Kubernetes with managed Kata + Firecracker."
- Higher per-pod cost, no node management, strong isolation.
- Limitations: no privileged, no DaemonSets, no certain CNIs, no GPUs in some configurations.

### 31.4 Azure Container Instances / GCP Run

- Similar pattern: managed VM-per-workload, exposed via different APIs (not always K8s-native).
- Useful for "run a single container with strong isolation" without operating a cluster.

### 31.5 Crun + libkrun

- `containers/libkrun`: a library to run containers inside a microVM (KVM-based) directly via a small VMM linked into crun.
- Similar concept to Kata + microVM, simpler integration model.
- Not yet upstream-default for any major distribution; watch this space.

---

## 32. gVisor Source Map

For when you need to read the code.

Repo: `github.com/google/gvisor`. Mostly Go, some C bindings.

| Path | What |
|---|---|
| `runsc/` | The CLI binary, OCI-runtime compatible (drop-in for runc) |
| `runsc/cmd/` | runc-equivalent commands: create, start, kill, delete, exec |
| `runsc/cgroup/` | cgroup setup for the Sentry |
| `runsc/sandbox/` | sandbox lifecycle: launch Sentry, launch Gofer, manage process |
| `pkg/sentry/` | The userspace kernel ("Sentry") |
| `pkg/sentry/kernel/` | task / thread model, scheduling, signals, ptrace |
| `pkg/sentry/syscalls/` | per-syscall implementations (huge: hundreds of files, one per syscall) |
| `pkg/sentry/fsimpl/` | filesystem implementations: ext, fuse, kernfs, overlay, proc, sys, tmpfs, … |
| `pkg/sentry/socket/netstack/` | integration with the netstack TCP/IP stack |
| `pkg/sentry/platform/` | the platform interface (ptrace, kvm, systrap) |
| `pkg/sentry/platform/ptrace/` | ptrace platform impl |
| `pkg/sentry/platform/kvm/` | KVM platform impl |
| `pkg/sentry/platform/systrap/` | systrap platform impl |
| `pkg/tcpip/` | netstack: a Go TCP/IP stack (also used outside gVisor) |
| `pkg/p9/`, `pkg/lisafs/` | 9P / lisafs protocol implementations (the Sentry↔Gofer wire) |
| `runsc/fsgofer/` | The Gofer binary's filesystem implementation |
| `runsc/gofer/` | Gofer launching logic |
| `test/syscalls/` | the syscall conformance test suite — the definition of "what works" |
| `g3doc/` | architecture documents |

Reading order for understanding: `runsc/main.go` → `runsc/cmd/run.go` → `runsc/sandbox/sandbox.go` → `pkg/sentry/syscalls/linux/` (pick a syscall) → `pkg/sentry/platform/<platform>/`.

---

## 33. Kata Source Map

Repo: `github.com/kata-containers/kata-containers`. Mix of Go and Rust; trending Rust.

| Path | What |
|---|---|
| `src/runtime/` | Go: the CRI shim (`containerd-shim-kata-v2`), the kata-runtime CLI |
| `src/runtime-rs/` | Rust: a newer Rust rewrite of the runtime, in progress |
| `src/agent/` | Rust: the kata-agent (PID 1 inside the VM) |
| `src/dragonball/` | Rust: the in-process Dragonball VMM |
| `src/libs/` | shared Rust libraries (logging, types, ipc) |
| `src/tools/` | utilities: kata-collect-data, kata-monitor, kata-ctl |
| `tests/` | integration tests |
| `docs/` | architecture and usage documentation |
| `tools/packaging/` | guest image build scripts (the kernel, initrd, agent baked into a rootfs) |
| `tools/packaging/kernel/` | kernel build configuration (the stripped guest kernel) |

The wire protocol between shim and agent is in `src/agent/protocols/protos/` (protobuf definitions for the agent's gRPC API: CreateSandbox, CreateContainer, ExecProcess, …).

For Kata + Firecracker: also `firecracker-microvm/firecracker` (Rust, the VMM itself).
For Kata + Cloud Hypervisor: `cloud-hypervisor/cloud-hypervisor` (Rust).

---

## 34. Confidential Containers Source Map

Repo: `github.com/confidential-containers/` (an organization with multiple repos).

| Repo | What |
|---|---|
| `confidential-containers/operator` | Kubernetes operator that deploys CoCo runtimes onto a cluster |
| `confidential-containers/trustee` | The KBS / AS / RVPS server stack |
| `confidential-containers/guest-components` | components that run inside the confidential VM: confidential-data-hub, attestation-agent, api-server-rest, image-rs (encrypted image pull) |
| `confidential-containers/kata-containers` | the CoCo fork of kata-containers (now upstreaming much of this back) |
| `confidential-containers/td-shim` | a measured-boot shim for Intel TDX |
| `confidential-containers/cloud-api-adaptor` | adapter for "peer-pods" — running CoCo guests on cloud-provider managed VMs rather than nested virt |

The peer-pods architecture (cloud-api-adaptor) is increasingly important: rather than nested-virt-on-a-K8s-node, the CoCo runtime creates a *separate cloud VM* as the workload's confidential environment. The K8s node becomes a control plane only; the actual workload runs on a different VM. This gets around nested-virt limitations in cloud providers.

---

## 35. Migration Path: From All-runc to Selective Sandboxing

A concrete plan for "we currently run all runc and want to start sandboxing."

### Step 1: PSA, then start

If you haven't done chapter 28 — Pod Security Admission, NetworkPolicy default-deny, image signing — do that first. Sandbox is an *additional* layer; you want the others underneath.

### Step 2: Identify what *needs* it

Make a list. Typical categories:

- Untrusted code execution (CI runners, notebooks, public-facing exec endpoints).
- Multi-tenant workloads where tenants can submit jobs.
- Workloads handling regulated data (PCI scope, PHI).
- Workloads that ran exploitable software with poor patching history.

For each, decide what the trust boundary actually is, and which of gVisor/Kata/CoCo fits.

### Step 3: Pilot one workload, one RuntimeClass

Pick a non-critical untrusted-code path. Set up a tainted node pool, install gVisor via a DaemonSet (or use GKE Sandbox / similar managed offering), create a RuntimeClass, add `runtimeClassName: gvisor` to the pilot pod.

Watch for:

- Cold-start regression (compare metrics before/after).
- Throughput regression.
- Compatibility errors (`ENOSYS` showing up in app logs).
- Cost increase (sandbox nodes cost more per allocatable).

### Step 4: Expand by similarity

If the pilot works, expand to similar workloads first. Don't try to sandbox everything at once; pick workload families where the compatibility profile is known.

### Step 5: Add Kata for the kernel-feature-heavy class

If your environment also has workloads that need real Linux (io_uring, eBPF, KubeVirt, certain databases), add a `kata-clh` or `kata-qemu` RuntimeClass on a separate node pool.

### Step 6: CoCo only when justified

Only introduce CoCo when there's a concrete regulatory or trust requirement. The operational complexity (KBS, reference values, encrypted images) is real; don't take it on speculatively.

### Step 7: Make defaults

Once stable, make sandbox the *default* for the relevant namespaces via admission (Kyverno mutation, or a custom controller that adds `runtimeClassName` to all pods in `scope=untrusted` namespaces). The goal: developers don't have to opt in; the platform routes their pods to the right runtime by namespace.

### Step 8: Don't sandbox everything

Trusted internal services do not need sandboxing. The cost (boot time, RAM overhead, debugging complexity) is not justified for workloads where the trust boundary is "your own engineers ship this code." Over-isolating is a real mistake; it slows everything and trains engineers to ignore the runtime, which weakens the cases where sandboxing matters.

---

## 36. Observability for Sandbox Runtimes

What you should be exporting.

### 36.1 Per-RuntimeClass metrics

- `pod count by RuntimeClass` — track adoption.
- `pod start time by RuntimeClass (p50/p99)` — sandbox runtimes have measurably different cold start; track it.
- `container start failures by RuntimeClass` — sandbox-specific errors (missing handler, missing runtime binary).

A simple kube-state-metrics + Prometheus alert: if `container_start_failures{runtimeClass="kata-qemu"}` is higher than `runc`, something is wrong with the Kata node pool.

### 36.2 gVisor-specific

- Sentry's `/runsc-metrics` (if exposed) reports syscall counts, page faults, network packets.
- ENOSYS counter — how many times the workload asked for a syscall the Sentry doesn't implement. Spike = compatibility issue.

### 36.3 Kata-specific

- VM boot time histogram.
- Hypervisor process count (should equal Kata pod count; mismatch = orphan VM).
- virtiofsd CPU/memory.
- vsock connect failures (broken agent-shim communication).

### 36.4 CoCo-specific

- KBS request rate, error rate, latency.
- Attestation pass/fail count.
- Reference value version (track to detect drift).

### 36.5 Cross-cutting: Falco / Tetragon

Sandbox + runtime detection (chapter 28) are complementary. Falco's eBPF probes still see the *host-kernel* side of sandbox runtimes: they see syscalls from the Sentry to the host, hypervisor process behavior, vsock activity. A Falco rule that triggers on "kata-shim spawned an unexpected child process" is a useful tripwire.

For inside-the-sandbox visibility, you need different tools: an audit agent inside the Kata VM (kata-agent has hooks), or a Sentry strace-style trace for gVisor. Most operators settle for outside-the-sandbox observability and accept reduced visibility inside; the security model assumes the sandbox is the boundary.

---

## 37. Pitfalls and Anti-Patterns

A long list. Most operators hit at least half of these before the system stabilizes.

1. **No `overhead.podFixed` on a Kata RuntimeClass.** Scheduler over-packs nodes; eviction storm; pages. Always set overhead.
2. **RuntimeClass without `scheduling.nodeSelector`.** Pod lands on a node without the runtime → stuck in `ContainerCreating` with cryptic kubelet logs.
3. **`runsc` installed but containerd not reloaded.** The handler isn't registered until containerd reloads its config (SIGHUP). Plan for it in the DaemonSet installer.
4. **gVisor with apps that use `io_uring`.** Silent fallback to slow paths; throughput tanks; no obvious error message. Plan compatibility testing.
5. **Kata pods with `hostPath` mounts.** Admission allows it in some configurations and the pod schedules but the kata-runtime denies → stuck pod. Use Kyverno/VAP to reject `hostPath` for Kata pods at admission.
6. **Kata vCPU count too low.** The VM defaults to 1 vCPU; if the workload wants 4 it throttles inside the VM in ways that don't show on the host cgroup. Tune via the kata config and verify under load.
7. **Firecracker on a node without nested virt support.** Won't start. The kata-shim error is non-obvious. Verify `/dev/kvm` access on the node and CPU feature flags.
8. **CoCo without a working KBS.** Pod can pull image (if not encrypted) but can't fetch secrets → app crashloop with no obvious cause. Make KBS health a cluster-critical dependency.
9. **Encrypted image but reference values not updated.** KBS rejects attestation → pod can't get decryption key → image never decrypts. Reference values must be updated in lock-step with image builds.
10. **Mixing `privileged: true` with a sandbox RuntimeClass.** Many runtimes either fall back to runc (silently!) or reject. Either is a footgun. Use admission to reject this combination.
11. **gVisor pod hitting IO-heavy code path.** Throughput cliff. The fix is usually "don't run this workload under gVisor" — sandbox the *risky* parts, not the database.
12. **Cold start exceeding pod startup probe timeout.** Kata + first-time image pull + first-time VM boot can take 3–5 s easily. Set startupProbe.failureThreshold appropriately.
13. **`kubectl exec` debugging across vsock failures.** When vsock breaks, exec hangs. Have a host-level fallback (SSH + ps + nsenter on the Sentry / hypervisor process).
14. **Networking quirks: MTU mismatch between veth and TAP.** Kata's tap setup may use a different MTU than the CNI's veth. Path MTU discovery breaks; intermittent packet drops; mystery latency spikes.
15. **Using a sandbox as a policy mechanism rather than isolation.** Wrong tool. Sandboxes contain compromise; policies prevent it. You need both.
16. **Sandboxing trusted workloads "to be safe."** Cost without benefit. Pay it only where the trust boundary justifies it.
17. **Per-namespace mutation that flips trusted workloads into sandboxes by accident.** Kyverno rule that targets the wrong selector. Mass cold-start regression. Have a canary.
18. **Forgetting to update the RuntimeClass when runtime versions change.** The `handler` name typically stays the same, but the underlying binary changes. If `runsc` is updated to a version with different behavior, you find out via pod failures, not RuntimeClass change events.
19. **Not testing the sandbox under load.** Sandbox runtimes degrade differently than runc under saturation (e.g., Sentry's Go GC under high syscall churn). Pilot with realistic load.
20. **Conflicting RuntimeClass + manual nodeSelector on the pod.** RuntimeClass adds a nodeSelector; if the user's pod also has a nodeSelector for a different label, the resulting AND may match no nodes. Diagnose by reading `pod.spec.nodeSelector` after admission.
21. **CoCo without out-of-cluster KBS.** Running Trustee inside the cluster it secures means a compromise of the cluster compromises secret release. Run KBS elsewhere for real defense.
22. **DaemonSets that install the runtime racing with workload pods.** Workload schedules onto a "ready" node before the installer finishes; pod fails. Use `startupTaints` (Karpenter) or initial node taint that the installer removes on success.
23. **Audit telling you the wrong runtime.** A pod whose admission added `runtimeClassName: kata-qemu` may still run as runc if the kata-runtime rejected it and the kubelet fell back. Check `crictl inspectp` for the actual handler used.
24. **Sandbox cold-start tail latency under autoscaling.** Karpenter spins up a Kata node + a Kata pod cold-start = several seconds. HPA may scale up further before the first pod is ready. Tune scaling stabilization windows accordingly.
25. **No drain plan for sandbox node upgrades.** Sandboxed pods take longer to terminate; PDBs that allow no disruption block forever. Plan PodDisruptionBudgets with realistic minAvailable.

---

## 38. TL;DR

**The boundary problem.** A container is a process; a process shares the host kernel; a kernel CVE reachable from any pod is a host compromise. PSA, seccomp, AppArmor, and Falco narrow what the workload can *do*, but a bug in what you *let it do* is still a bug.

**Two answers.** Reduce the host-kernel surface (gVisor's Sentry handles syscalls in userspace, calls only ~30 host syscalls), or run the workload in its own kernel (Kata boots a stripped Linux inside a lightweight VM per Pod). Confidential Containers extends the VM approach with hardware memory encryption (TDX, SEV-SNP, CCA) so even the host operator cannot read workload RAM.

**RuntimeClass is the wiring.** A cluster-scoped `node.k8s.io/v1` resource names a `handler` that maps to a CRI runtime configured on each node (containerd: `[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.<handler>]`). Pods opt in via `spec.runtimeClassName`. `scheduling.nodeSelector` and `tolerations` route pods to nodes that have the runtime; `overhead.podFixed` makes the scheduler account for the runtime's overhead.

**Spectrum.** runc (full host kernel) → gVisor (userspace kernel, ~30 host syscalls) → Kata (lightweight VM, separate kernel) → Firecracker microVM (most stripped VMM, 5 devices, 125 ms boot) → Confidential VM (encrypted memory + attestation). Each step shrinks the trusted computing base, grows the cost, narrows compatibility.

**gVisor**: Sentry (Go, syscall implementation) + Gofer (filesystem proxy, chrooted). Three platforms: ptrace (slow, broad compat), KVM (fast, needs `/dev/kvm`), systrap (modern default, no KVM). Compatibility good but not perfect — no `io_uring`, no `bpf`, partial GPU, some `ioctl`s. Per-syscall 2–10× slower; CPU-bound workloads near-native; syscall-heavy workloads degrade.

**Kata**: per-Pod VM via a hypervisor (QEMU, Cloud Hypervisor, Firecracker, Dragonball) + kata-agent inside the VM + virtio-fs for the rootfs + vsock for shim↔agent. Needs nested virt (or bare metal). Compatibility close to native Linux; cold start 100–500 ms; per-pod RAM 50–150 MiB.

**Firecracker** is the microVMM behind Lambda/Fargate; 5 virtio devices, 5 MB binary, <125 ms boot, <5 MiB overhead. Integrated via Kata (`kata-fc`) or via dedicated controllers (`firecracker-containerd`).

**Cloud Hypervisor** is Intel-led, Rust, similar to Firecracker but with native virtio-fs; usually the better default for Kubernetes workloads that use ConfigMaps, Secrets, projected volumes (i.e. all of them).

**CoCo**: Kata + TDX/SEV-SNP + attestation. The guest VM measures its boot, sends a hardware-signed quote to a Key Broker Service, KBS verifies and releases secrets (image decryption key, image-pull cert, app secrets). Encrypted container images extend the supply chain: image layers are encrypted at build time, decrypted in the guest after attestation. Operational complexity is real: Trustee/KBS, reference values, encrypted-image pipeline.

**Choosing**: gVisor for untrusted code with modest syscall density and tight cold-start budgets (CI runners, notebooks, multi-tenant code execution — Replit, GitPod, Bytebase pattern). Kata for kernel-feature-heavy workloads or regulated tenants needing a hardware-isolation auditable boundary (PCI, HIPAA). CoCo when you cannot trust the host operator or cloud provider (regulated industries, sovereign cloud, key material).

**Performance shape**: CPU near-native everywhere; fileio worst in gVisor; syscall-heavy worst in gVisor; cold start worst in Kata-QEMU and best in Kata-Firecracker with snapshot.

**What sandboxing does NOT protect against**: network attacks (still need NetworkPolicy + mesh mTLS), credentials in env (still need workload identity), application bugs (still need WAF + AppSec), supply chain (still need image signing), misconfigured RBAC (still need least privilege), hypervisor/Sentry CVEs (smaller surface, not zero).

**Operating**: tainted node pools per runtime; DaemonSet installers; `RuntimeClass.scheduling` routes pods; `overhead.podFixed` accounts for VM overhead; sandbox nodes are cattle (no live migration); debug via `crictl inspectp`, `runsc debug`, `kata-collect-data`; cost track per RuntimeClass.

**Pitfalls**: missing overhead → over-pack; missing nodeSelector → orphan pods; `hostPath` on Kata → admission denied or stuck; KBS down → no CoCo pod starts; cold-start exceeds startupProbe; sandbox without complement (NetPol, RBAC, image signing) is half-secure; sandboxing trusted workloads pays cost without benefit; sandboxing the wrong workload (heavy I/O on gVisor) cliffs performance silently.

**The core sentence.** *Sandboxing replaces one trusted computing base with a smaller, more auditable one. Pick the smallest one that still covers your trust boundary. Pair it with policy (PSA, seccomp), network segmentation (NetworkPolicy, mesh), identity (bound SA tokens), supply chain (signed images), and detection (Falco/Tetragon). The sandbox alone is not security; the sandbox plus the rest, layered to your trust model, is.*
