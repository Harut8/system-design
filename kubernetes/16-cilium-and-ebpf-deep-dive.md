# Cilium and eBPF Deep Dive: What Comes After iptables

If chapter 15 was a tour of the CNI landscape, this chapter is a single-floor freight elevator straight down to the kernel. Cilium is the most ambitious CNI in production today: it does not just allocate Pod IPs and program rules; it tears out iptables, kube-proxy, the netfilter conntrack table, and most of the legacy Linux service-load-balancing machinery, and replaces them with a graph of eBPF programs attached at every interesting kernel hook between the NIC driver and the socket layer.

To understand Cilium you have to understand eBPF — not the "it's like DTrace for Linux" elevator pitch, but the actual mechanics: program types, the verifier, BTF, CO-RE, maps, tail calls, cgroup hooks, XDP versus TC, ringbuf versus perf_event_array. We dipped into the basics in chapter 00 (namespaces, cgroups, veth, netfilter, "eBPF exists"). We will now go deep enough that the names in `bpftool prog list` and the C in `cilium/cilium/bpf/bpf_lxc.c` stop being mysterious.

We will also explain *why* this is necessary. iptables works fine on a laptop. It collapses, audibly, at scale. The kube-proxy chapter (14) showed you the failure modes: O(N) rule matching, O(N²) reconcile time when an EndpointSlice changes, conntrack table explosion, full-table reload semantics that drop packets during an update. Cilium's bet is that *every part of the Linux network stack that is slow can be replaced with eBPF that is fast, safe, and observable*. So far that bet has paid off: GKE Dataplane v2 is Cilium, EKS now offers Cilium-mode, AKS has "Azure CNI Powered by Cilium", and the largest production Kubernetes fleets in the world run on it.

This is also where observability and security stop being bolt-ons. Hubble, Tetragon, and the WireGuard transparent-encryption story all live in the same datapath, so you get them for free once you have already paid the cost of installing Cilium. That is the throughline of this chapter: *put the smart code in the kernel, once, and let every other concern read out of the same maps*.

---

## Table of Contents

1. [The Story: Why eBPF in the Kernel Data Path](#1-the-story-why-ebpf-in-the-kernel-data-path)
2. [eBPF Primer (Deeper Than Chapter 00)](#2-ebpf-primer-deeper-than-chapter-00)
3. [The eBPF Verifier](#3-the-ebpf-verifier)
4. [BTF and CO-RE](#4-btf-and-co-re)
5. [Cilium Architecture](#5-cilium-architecture)
6. [The Endpoint and Identity Model](#6-the-endpoint-and-identity-model)
7. [Cilium Datapath Modes](#7-cilium-datapath-modes)
8. [kube-proxy Replacement (Deep)](#8-kube-proxy-replacement-deep)
9. [The cgroup Hooks in Detail](#9-the-cgroup-hooks-in-detail)
10. [Maglev Hashing](#10-maglev-hashing)
11. [NetworkPolicy Enforcement](#11-networkpolicy-enforcement)
12. [DNS-Aware Egress (toFQDNs)](#12-dns-aware-egress-tofqdns)
13. [Hubble: Observability in the Datapath](#13-hubble-observability-in-the-datapath)
14. [Tetragon: Runtime Security via eBPF](#14-tetragon-runtime-security-via-ebpf)
15. [Cilium Service Mesh (Sidecar-less)](#15-cilium-service-mesh-sidecar-less)
16. [Cluster Mesh: Multi-Cluster Cilium](#16-cluster-mesh-multi-cluster-cilium)
17. [WireGuard Transparent Encryption](#17-wireguard-transparent-encryption)
18. [Performance Characteristics](#18-performance-characteristics)
19. [Verifier-Bounded Complexity: Tail Calls and Map-in-Map](#19-verifier-bounded-complexity-tail-calls-and-map-in-map)
20. [XDP: eXpress Data Path](#20-xdp-express-data-path)
21. [Debugging eBPF and Cilium](#21-debugging-ebpf-and-cilium)
22. [Cilium Configuration Knobs](#22-cilium-configuration-knobs)
23. [Real-World Rollouts](#23-real-world-rollouts)
24. [Pitfalls](#24-pitfalls)
25. [TL;DR](#25-tldr)

---

## 1. The Story: Why eBPF in the Kernel Data Path

### 1.1 iptables Was Never Designed for Kubernetes

Recall from chapter 14 the shape of a kube-proxy iptables rule set for a single Service:

```
PREROUTING
  └─► KUBE-SERVICES
        ├─ -d 10.96.0.42/32 -p tcp --dport 80 → KUBE-SVC-AAAA
        ├─ -d 10.96.0.43/32 -p tcp --dport 80 → KUBE-SVC-BBBB
        ├─ -d 10.96.0.44/32 -p tcp --dport 80 → KUBE-SVC-CCCC
        ├─ ...                                                  ◄── linear scan, N rules
        └─ -d 10.96.0.999/32 -p tcp --dport 80 → KUBE-SVC-ZZZZ

KUBE-SVC-AAAA
  ├─ -m statistic --mode random --probability 0.33 → KUBE-SEP-1
  ├─ -m statistic --mode random --probability 0.50 → KUBE-SEP-2
  └─ -j KUBE-SEP-3

KUBE-SEP-1
  └─ -j DNAT --to-destination 10.244.1.42:80
```

The Linux netfilter framework was designed in 1999 for stateful firewalling on edge routers. It has three properties that are fine then and catastrophic now:

- **Linear matching.** Every rule in a chain is evaluated in order until one matches. With 5,000 Services × 3 endpoints each, a single packet on the OUTPUT chain may traverse ~15,000 rules. The kernel walks them with `nft_do_chain()` or `ipt_do_table()`. At 10 Gbps line rate with 64-byte packets, each microsecond of per-packet overhead halves your throughput.
- **Full-table reload on update.** kube-proxy in iptables mode does not surgically patch rules. It computes the entire desired rule set, writes it to a temp file, and calls `iptables-restore --noflush`. On a 5,000-Service cluster, generating that file alone takes ~30 seconds; loading it takes another minute. During the reload, the kernel takes the `xt_table` write lock — packets pile up.
- **Conntrack everywhere.** netfilter's DNAT requires conntrack entries to reverse the translation on reply packets. A cluster with 50,000 concurrent ClusterIP flows has a 50,000-entry conntrack table, with a hash table the kernel walks on every packet. Conntrack table exhaustion is a top-five Kubernetes incident class.

The kube-proxy IPVS mode helps with the linear-matching problem (IPVS uses a hash) but still requires conntrack and still requires iptables for source-NAT, masquerade, and node-port marks. The nftables mode in modern kube-proxy is faster than legacy iptables but the same architectural problems remain: kernel rules, per-packet processing, conntrack.

### 1.2 What If the Kernel Were Programmable?

The fundamental shift is this: instead of *configuring* a fixed set of kernel features (netfilter, ip route, tc filter), you *write* the code that runs in the kernel for the exact decision you need.

```
              CONVENTIONAL                           eBPF
       ┌─────────────────────────┐         ┌──────────────────────────┐
       │ kube-proxy (userspace)  │         │ Cilium agent (userspace) │
       │   reads Services        │         │   reads Services         │
       │   generates rules       │         │   updates BPF maps:      │
       │       │                 │         │     SVC → backends       │
       │       ▼                 │         │                          │
       │ iptables-restore        │         │ BPF programs already      │
       │   (write kernel rules)  │         │ loaded once at boot;      │
       │       │                 │         │ they look up the maps     │
       │       ▼                 │         │       │                  │
       │ Kernel: walks N rules   │         │ Kernel: 1 map lookup     │
       │   per packet            │         │   per packet             │
       └─────────────────────────┘         └──────────────────────────┘

       update path: O(N) rule reload         update path: O(1) map write
       data path:   O(N) rule scan           data path:   O(1) hash lookup
```

eBPF gives you three properties iptables cannot:

1. **Programmable.** Replace whatever logic you want with custom C compiled to BPF bytecode. iptables can only match on what netfilter's match modules expose.
2. **Safe.** A verifier proves the program cannot crash the kernel, cannot loop forever, cannot read uninitialized memory, and only calls helpers appropriate for its program type.
3. **Fast.** The bytecode is JIT-compiled to native machine code at load time. A BPF program is, in the steady state, a sequence of native instructions called directly from a kernel hook with no syscall, no copy, and no per-packet allocation.

### 1.3 Cilium's Bet

Cilium is the answer to the question *"if we had eBPF, what would we replace?"* The list, in rough order of impact:

| Replaced legacy mechanism | With eBPF program at hook |
|--------------------------|---------------------------|
| iptables KUBE-SERVICES (ClusterIP DNAT) | cgroup `connect4`/`connect6` socket LB |
| iptables KUBE-NODEPORTS (NodePort DNAT) | TC ingress on host netdev |
| iptables MASQUERADE | TC egress on host netdev |
| netfilter conntrack (for ClusterIP) | BPF `lru_hash` map of CT entries |
| kube-proxy IPVS | BPF `hash` map of services + Maglev backend selection |
| bridge / `cni0` packet forwarding | BPF `redirect_peer()` between veth pairs |
| netfilter NetworkPolicy | BPF policy map keyed by identity |
| L7 NetworkPolicy hairpin via Envoy sidecars | TC redirect to a per-node Envoy DaemonSet |
| `iptables -t mangle -j MARK` | BPF skb mark setters in TC programs |
| sysdig / falco system-call hooks | Tetragon tracepoint/kprobe BPF programs |
| Prometheus-side flow metrics | Hubble events emitted from BPF into ringbuf |

The result is that in a fully-cilium cluster, you can `iptables-save | wc -l` on a node and get *zero* rules from Cilium for ClusterIP traffic. The kernel is doing exactly what it needs to do, and nothing else.

### 1.4 The Cost

This power is not free. The tradeoffs:

- **You need a recent kernel.** Cilium's full feature set assumes 5.10+ for stable BTF, 5.13+ for `bpf_loop()`, 5.7+ for ring buffers, 5.4+ for cgroup `connect4`. Older distributions still common in enterprise (CentOS 7) cannot run modern Cilium.
- **Verifier rejection.** Programs that "obviously" work will be rejected because the verifier cannot prove them safe. You will spend afternoons golfing C until the verifier is happy.
- **Tooling has its own vocabulary.** `bpftool`, `bpftrace`, `cilium-dbg`, perf-events, ringbuf — none of which are taught in the LFCE. We'll cover the survival kit in §21.
- **You commit to one CNI for everything.** Cilium is not a side dish — when you turn on `kubeProxyReplacement=true`, kube-proxy stops doing anything useful. Migration is a node-by-node affair (§23).

### 1.5 The Source Tree You Will End Up Reading

The cilium/cilium repository on GitHub is laid out so you can navigate it by concern:

```
cilium/cilium/
├── bpf/                              # the BPF C source — the datapath
│   ├── bpf_lxc.c                     # per-endpoint program (TC ingress/egress on veth)
│   ├── bpf_host.c                    # per-host program (eth0 TC ingress/egress)
│   ├── bpf_overlay.c                 # tunnel device program
│   ├── bpf_sock.c                    # cgroup connect4/6, sendmsg4/6 — socket LB
│   ├── bpf_xdp.c                     # XDP LB program
│   ├── bpf_network.c                 # IPsec / WireGuard helpers
│   ├── lib/
│   │   ├── lb.h                      # load-balancer logic shared headers
│   │   ├── policy.h                  # policy map lookup helpers
│   │   ├── conntrack.h               # CT state machine in BPF
│   │   ├── nat.h                     # SNAT/DNAT helpers
│   │   ├── maps.h                    # all BPF map declarations
│   │   ├── ipcache.h                 # IPCache lookup
│   │   ├── encrypt.h                 # WG/IPsec hooks
│   │   └── ...
│   └── tests/                        # BPF unit tests using bpf_test_run
├── daemon/                           # cilium-agent Go code
│   ├── cmd/                          # main entry, REST API
│   ├── k8s/                          # apiserver watchers (Endpoints, Services, CNP, …)
│   └── ...
├── operator/                         # cilium-operator Go code
│   ├── pkg/                          # IPAM, identity GC, CES, etc.
│   └── ...
├── pkg/
│   ├── policy/                       # policy engine: CNP → identity-keyed map writes
│   ├── identity/                     # identity allocator (CRD and kvstore backends)
│   ├── endpoint/                     # per-endpoint state machine
│   ├── datapath/                     # the loader that compiles bpf/* with right defines
│   ├── proxy/                        # Envoy management
│   ├── hubble/                       # event decoder, gRPC server
│   ├── fqdn/                         # DNS proxy and FQDN policy logic
│   ├── maps/                         # Go bindings for all BPF maps
│   └── ...
├── plugins/cilium-cni/               # the CNI plugin binary
├── api/v1/                           # OpenAPI specs and gRPC for Hubble
└── ...
```

When this chapter says "the cgroup connect4 program does X," that program is in `bpf/bpf_sock.c`. When it says "the policy engine expands CNPs into the policy map," that's `pkg/policy/`. Knowing the layout makes the chapter actionable.

---

## 2. eBPF Primer (Deeper Than Chapter 00)

Chapter 00 covered the elevator pitch: eBPF is a register-based virtual machine inside the Linux kernel that can attach safe, verified, JIT-compiled programs to predefined hooks. Here is the depth you need to read Cilium's source tree.

### 2.1 Architecture: From C to Bytecode to Native Code

```
         clang -target bpf -O2 -g                              libbpf::bpf_object__load()
   ┌────┐  ────────────────────►   ┌────────┐   ────────────►   ┌────────────┐
   │.c  │                          │.o ELF  │                   │  kernel    │
   │    │                          │ (BPF   │                   │ verifier   │
   │    │                          │ insns) │                   │            │
   └────┘                          └────────┘                   └─────┬──────┘
                                                                     │ accepts
                                                                     ▼
                                                              ┌──────────────┐
                                                              │  JIT (x86_64,│
                                                              │  arm64, …)   │
                                                              └─────┬────────┘
                                                                    │ native code
                                                                    ▼
                                                              ┌──────────────┐
                                                              │ attached to  │
                                                              │ a hook       │
                                                              │ (TC, XDP,    │
                                                              │  cgroup, …)  │
                                                              └──────────────┘
```

A BPF ELF object is essentially a relocatable object file with extra sections:

- `.text` (and per-section like `.text/cls/handle_xdp`): BPF instructions, encoded as a 64-bit RISC-like ISA with 11 registers (`r0`-`r10`, `r10` is the read-only frame pointer).
- `.maps` or `.maps.<name>`: declarations of BPF maps (kernel data structures shared with userspace).
- `.BTF` and `.BTF.ext`: type information (more on this in §4).
- `.rel.<section>`: relocations the loader applies at load time.

The kernel BPF subsystem (`kernel/bpf/syscall.c`) accepts a series of `bpf(BPF_PROG_LOAD, ...)` syscalls with the instructions, sends them through `kernel/bpf/verifier.c`, and either rejects or hands off to `arch/x86/net/bpf_jit_comp.c` (or arm64/riscv equivalents) for JIT compilation. The result is a `struct bpf_prog` with a `bpf_func` pointer that is, at the end of the day, ordinary kernel code called inline from the hook.

### 2.2 Hooks: Where Programs Attach

This is the matrix you need to internalize. A *hook* is a kernel callout that, if a BPF program is attached, calls into it with a specific calling convention and context. The hook determines the *program type*, which determines what helper functions you can call and what fields of the context you can touch.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          eBPF Hook Map                                       │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   USERSPACE                                                                  │
│  ┌─────────────────────────────────────────────────┐                        │
│  │  syscalls                                       │                        │
│  └──┬──────────────────────────────────────────────┘                        │
│     │                                                                       │
│     ▼  syscall hooks: tracepoint:syscalls:sys_enter_*, raw_tracepoint, fentry│
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                    SYSCALL LAYER                                     │  │
│  │  ────────────────────────────────────────────────────────────────   │  │
│  │   connect(2), sendmsg(2), accept(2), bind(2), setsockopt(2)         │  │
│  └──┬───────────────────────────────────────────────────────────────────┘  │
│     │                                                                       │
│     ▼  cgroup hooks: BPF_PROG_TYPE_CGROUP_SOCK_ADDR (connect4/6,             │
│        sendmsg4/6, recvmsg4/6, getpeername4/6, getsockname4/6),              │
│        BPF_PROG_TYPE_CGROUP_SOCK (sock_create, post_bind),                   │
│        BPF_PROG_TYPE_CGROUP_SOCKOPT (getsockopt/setsockopt),                 │
│        BPF_PROG_TYPE_SOCK_OPS (TCP state machine events)                     │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                    SOCKET LAYER                                      │  │
│  │   AF_INET, AF_INET6, struct sock                                     │  │
│  └──┬───────────────────────────────────────────────────────────────────┘  │
│     │                                                                       │
│     ▼  BPF_PROG_TYPE_SK_LOOKUP: choose listening socket at packet-in time   │
│        BPF_PROG_TYPE_SK_MSG / SK_SKB: redirect between sockets via sockmap   │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                    TCP/IP STACK                                      │  │
│  │   ip_rcv, tcp_v4_rcv, udp_recvmsg                                    │  │
│  └──┬───────────────────────────────────────────────────────────────────┘  │
│     │                                                                       │
│     ▼  TC hooks: BPF_PROG_TYPE_SCHED_CLS at ingress/egress of a netdev      │
│        Attach point: clsact qdisc; ctx = struct __sk_buff                    │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                    NETDEV / qdisc (eth0, veth, cilium_host, ...)     │  │
│  └──┬───────────────────────────────────────────────────────────────────┘  │
│     │                                                                       │
│     ▼  XDP: BPF_PROG_TYPE_XDP at driver/poll-loop entry                     │
│        ctx = struct xdp_md (raw frame, before skb is allocated)              │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                    DRIVER (Mellanox mlx5, Intel ixgbe, virtio_net)   │  │
│  │       NAPI poll → page from RX ring → XDP program → skb or drop      │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ──── Orthogonal: tracing hooks ─────────────────────────────────────────   │
│                                                                              │
│   • kprobe / kretprobe: dynamic, function entry/exit anywhere                │
│   • uprobe / uretprobe: same, but in userspace binaries                      │
│   • tracepoint: static, predefined trace events (e.g., sched_switch)         │
│   • fentry / fexit: BPF trampoline at function entry/exit (5.5+, faster)     │
│   • perf_event: PMU counters, software events                                │
│   • LSM hooks: BPF_PROG_TYPE_LSM, attaches to security_* hooks (5.7+)        │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Why this diagram matters.** Cilium uses *every* hook in the diagram for something:

- `XDP` at driver: load-balancer hot path (LoadBalancer Service, §20).
- `TC ingress` on the host netdev: NodePort, host firewall.
- `TC ingress/egress` on every veth: per-pod policy enforcement, encap/decap.
- `cgroup connect4/6`: ClusterIP socket-level LB (the kube-proxy replacement, §8).
- `cgroup sendmsg4/6`: UDP socket-level LB.
- `sock_ops`: TCP fast path (sockmap-based same-node bypass).
- `tracepoint`: Hubble flow events.
- `kprobe / fentry`: Tetragon process-, file-, network-event recording.

### 2.3 Maps: The Shared Kernel ↔ Userspace Channel

A BPF program cannot allocate memory at runtime. It cannot keep state between invocations except through a *map*: a kernel data structure declared at load time, addressable from both BPF and userspace.

The catalog (selected, by `bpf/bpf_helpers.h`):

| Type | Key | Value | Notes |
|------|-----|-------|-------|
| `BPF_MAP_TYPE_HASH` | arbitrary | arbitrary | The workhorse. Hash table. |
| `BPF_MAP_TYPE_LRU_HASH` | arbitrary | arbitrary | Same, evicts LRU on full. Conntrack uses this. |
| `BPF_MAP_TYPE_ARRAY` | u32 index | arbitrary | Fixed-size array. Indexed in O(1). |
| `BPF_MAP_TYPE_PERCPU_HASH` / `ARRAY` | as above | one per CPU | No locking on writes; aggregate on read. |
| `BPF_MAP_TYPE_LPM_TRIE` | IP prefix | arbitrary | Longest-prefix match. CIDR rules. |
| `BPF_MAP_TYPE_HASH_OF_MAPS` / `ARRAY_OF_MAPS` | as outer | inner-map fd | Two-level lookup. Used for policy chains. |
| `BPF_MAP_TYPE_PROG_ARRAY` | u32 | program fd | Targets for `bpf_tail_call`. |
| `BPF_MAP_TYPE_PERF_EVENT_ARRAY` | u32 (cpu) | perf fd | Events from BPF to userspace (legacy). |
| `BPF_MAP_TYPE_RINGBUF` | n/a | n/a | Single MPSC ringbuf shared across CPUs (5.8+). Replaces perf_event_array. |
| `BPF_MAP_TYPE_SK_STORAGE` | sock ptr | arbitrary | Per-socket local storage. |
| `BPF_MAP_TYPE_DEVMAP` / `CPUMAP` | u32 | netdev/cpu | XDP redirect targets. |
| `BPF_MAP_TYPE_SOCKMAP` / `SOCKHASH` | u32/arb | sock fd | sockmap; redirect between sockets. |

Userspace interacts with maps through `bpf(BPF_MAP_LOOKUP_ELEM, ...)`, `BPF_MAP_UPDATE_ELEM`, `BPF_MAP_DELETE_ELEM`, etc., often via `libbpf`'s helpers. From BPF side, the helper functions are `bpf_map_lookup_elem(&map, &key)`, `bpf_map_update_elem(&map, &key, &val, flags)`, etc.

A trivial example, the ClusterIP services map from Cilium (`bpf/lib/maps.h` simplified):

```c
struct lb4_key {
    __be32 address;     /* IPv4 service address */
    __be16 dport;       /* L4 dest port */
    __u16  backend_slot;/* 0 = lookup; 1..N = backend by slot */
    __u8   proto;
    __u8   scope;       /* LB scope (external vs internal) */
    __u8   pad[2];
};

struct lb4_service {
    union {
        __u32 backend_id;       /* if backend_slot != 0 */
        __u32 affinity_timeout; /* if backend_slot == 0 (the "master" entry) */
    };
    __u16 count;          /* number of backends */
    __u16 rev_nat_index;  /* reverse NAT table index */
    __u8  flags;
    __u8  flags2;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __type(key, struct lb4_key);
    __type(value, struct lb4_service);
    __uint(max_entries, 65536);
    __uint(pinning, LIBBPF_PIN_BY_NAME);
} cilium_lb4_services_v2 SEC(".maps");
```

When the Cilium agent learns about a new Service from the apiserver, it computes the keys (master entry + one entry per backend) and writes them. The connect4 program then does:

```c
struct lb4_key key = {
    .address = orig_dst_ip,
    .dport   = orig_dst_port,
    .proto   = IPPROTO_TCP,
};
struct lb4_service *svc = map_lookup_elem(&cilium_lb4_services_v2, &key);
if (svc) {
    /* This destination is a Service VIP — pick a backend */
    __u16 slot = lb4_select_backend_id(ctx, svc, ...);
    key.backend_slot = slot;
    struct lb4_service *be = map_lookup_elem(&cilium_lb4_services_v2, &key);
    /* Rewrite the socket destination */
    ctx->user_ip4 = be->address;
    ctx->user_port = bpf_htons(be->port);
}
return SYS_PROCEED;
```

That is the entire kube-proxy replacement for ClusterIP, in flavor.

### 2.4 Program Types and Helper Sets

Each program type has a fixed context structure (`struct __sk_buff`, `struct xdp_md`, `struct bpf_sock_addr`, etc.) and a *whitelist* of helper functions it is allowed to call. The verifier enforces both. A few common types:

| Program type | Context | Common helpers | Typical use |
|--------------|---------|----------------|-------------|
| `BPF_PROG_TYPE_XDP` | `xdp_md` | `xdp_adjust_head`, `redirect`, `redirect_map`, `fib_lookup` | LB hot path, DDoS drop |
| `BPF_PROG_TYPE_SCHED_CLS` (TC) | `__sk_buff` | `skb_load_bytes`, `clone_redirect`, `redirect_peer`, `csum_diff` | Per-veth datapath, NodePort |
| `BPF_PROG_TYPE_CGROUP_SOCK_ADDR` | `bpf_sock_addr` | `get_cgroup_classid`, `sk_lookup_tcp`, `sk_storage_get` | Socket LB on connect/sendmsg |
| `BPF_PROG_TYPE_SOCK_OPS` | `bpf_sock_ops` | `sock_hash_update`, `setsockopt` | TCP fast-path, sockmap |
| `BPF_PROG_TYPE_KPROBE` | `pt_regs` | `probe_read`, `probe_read_user`, `get_current_pid_tgid` | Tracing, Tetragon |
| `BPF_PROG_TYPE_TRACEPOINT` | tracepoint args | as kprobe | Static events |
| `BPF_PROG_TYPE_LSM` | LSM hook args | `bpf_d_path`, `probe_read`, signal helpers | Security enforcement |

A helper used outside its program type makes the verifier reject the program — you cannot, for example, call `bpf_redirect_peer` from a cgroup program.

### 2.5 The Lifecycle of One BPF Object

Walking through, end to end, what happens when Cilium loads `bpf_lxc.o` onto a Pod's veth:

```
   cilium-agent reconcile loop sees endpoint 1234 (Pod created)
   │
   ▼ 1. Compile (one-time per endpoint, ~80ms with cached headers)
   clang -target bpf -O2 -g -c \
         -DSECCTX_FROM_IPCACHE=1 \
         -DENDPOINT_ID=1234 \
         -DSECLABEL=12345 \
         -DLXC_IP={ ...pod IPv4... } \
         -DNODE_MAC={ ...node MAC... } \
         bpf_lxc.c -o /var/run/cilium/state/1234/bpf_lxc.o
   │
   ▼ 2. Load via libbpf (open, load, attach)
   bpf_object__open_file("bpf_lxc.o")     # parse ELF, allocate prog/map objects
   bpf_object__load(obj)                   # for each prog:
                                           #   - relocate CO-RE references
                                           #   - syscall BPF_PROG_LOAD
                                           #   - kernel verifies, JITs
                                           # for each map:
                                           #   - syscall BPF_MAP_CREATE (or reuse pinned)
   │
   ▼ 3. Pin programs and maps under /sys/fs/bpf
   bpf_program__pin(prog, "/sys/fs/bpf/tc/globals/cil_from_container_1234")
   bpf_map__pin(map,   "/sys/fs/bpf/tc/globals/cilium_policy_1234")
   │
   ▼ 4. Attach to TC qdisc on the host-side veth
   tc qdisc add dev lxc12345 clsact
   tc filter add dev lxc12345 ingress bpf da fd <prog_fd>
   tc filter add dev lxc12345 egress  bpf da fd <prog_fd>
   │
   ▼ 5. Update the IPCache so other nodes know about this Pod
   write { 10.244.1.42/32 → identity=12345, tunnel=node1 } to cilium_ipcache
   │
   ▼ 6. Pod's veth is live; traffic flows
```

On endpoint deletion the sequence reverses: detach from TC, unpin, close fds, remove IPCache entry.

This is why an agent restart is *not* catastrophic. Programs and maps are pinned, so they keep working while the agent is down. The agent only needs to re-read the K8s state and reconcile any diffs. New connections continue to flow through the existing pinned programs.

---

## 3. The eBPF Verifier

The verifier is the most consequential and most frustrating piece of eBPF. It is the reason eBPF is *safe* — but it is also the reason a function that obviously terminates on paper can be rejected.

### 3.1 What the Verifier Does

The verifier (`kernel/bpf/verifier.c`, ~25,000 lines of C as of 6.x) performs an abstract interpretation of the BPF program *across every reachable control-flow path*, with the following invariants:

1. **All loads are bounded.** Every memory access must be provably within bounds — for context fields, within the context structure; for map values, within `value_size`; for the stack, within the 512-byte frame.
2. **All branches are bounded.** Conditional branches are followed both ways. The verifier tracks the possible *value ranges* of each register at every program point.
3. **Termination.** Until kernel 5.3 the program had to be loop-free — every backward edge was rejected. 5.3 introduced bounded loops (the verifier unrolls them up to a maximum iteration count). 5.13 introduced `bpf_loop()`, an explicit bounded-iteration helper, replacing painful `#pragma unroll` hacks.
4. **Type safety on pointers.** Pointers have *types* (`PTR_TO_MAP_VALUE`, `PTR_TO_PACKET`, `PTR_TO_STACK`, `PTR_TO_CTX`, etc.) and each type allows specific operations. Crossing types requires bounds checks the verifier can prove.
5. **No uninitialized reads.** Stack slots and registers are tracked as initialized or not; using uninitialized data is rejected.
6. **Total instruction budget.** Hard limit on instructions processed by the verifier (1M as of 5.6, was 4096 in old kernels). Programs that explode the path space are rejected even if they are correct.
7. **Helper call legality.** Each helper has a program-type allowlist and an argument-type signature; the verifier enforces both.

### 3.2 Verifier-Friendly Idioms

You can write C that *should* work but the verifier rejects, because it cannot prove what you know. Useful patterns:

**Bounds check before pointer dereference, every time.**

```c
void *data     = (void *)(long)ctx->data;
void *data_end = (void *)(long)ctx->data_end;
struct ethhdr *eth = data;
if ((void *)(eth + 1) > data_end)
    return TC_ACT_OK;
struct iphdr *ip = (void *)(eth + 1);
if ((void *)(ip + 1) > data_end)
    return TC_ACT_OK;
__be32 daddr = ip->daddr;
```

The verifier needs the explicit comparison before each access. Removing the check makes the program "pointer arithmetic outside bounds" and rejected.

**Use `bpf_loop()` instead of `for (i = 0; i < N; i++)` for large N.**

```c
struct iter_ctx { int sum; };
static int sum_one(__u32 idx, void *ctx) {
    struct iter_ctx *c = ctx;
    c->sum += idx;
    return 0; /* return 1 to break */
}
struct iter_ctx ctx = {};
bpf_loop(1024, sum_one, &ctx, 0);
```

Older code would `#pragma unroll` a loop, which generates 1024 lines of bytecode and might bust the instruction limit.

**Use `__sync_fetch_and_add()` for shared counters, not `+=`.** Pure increments race; atomics are accepted.

**Mark-and-check map lookups.** A lookup returns a pointer that may be NULL; the verifier requires the check before use.

```c
struct lb4_service *svc = bpf_map_lookup_elem(&services, &key);
if (!svc)
    return CTX_ACT_OK;
/* now you can dereference svc */
```

**Keep stack frames small (≤512 bytes).** Cilium splits work across functions using tail calls (§19) when one program approaches the budget.

### 3.3 Why Verifier Errors Are Usually Educational

Common verifier rejections and what they mean:

- `R0 invalid mem access 'inv'`: you dereferenced a pointer whose type the verifier doesn't recognize as memory — usually a missing NULL check on a map lookup, or an arithmetic computation on a packet pointer outside a bounds check.
- `back-edge from insn N to M`: you have a loop the verifier cannot bound. Add `#pragma unroll`, or rewrite using `bpf_loop()`.
- `invalid bpf_context access off=… size=…`: you accessed a context field that doesn't exist in this program type (e.g., trying to read `ctx->data` from a cgroup program).
- `processed N insns ... too many instructions`: complexity explosion. Hoist common subexpressions, split with tail calls.
- `unreachable insn`: dead code after a `return` or a constant-folded branch. Usually a sign of broken control flow.

The verifier log is verbose — `bpftool prog load file.o /sys/fs/bpf/foo dev eth0` will print every step it explored on failure, which is invaluable.

### 3.4 What the Verifier Actually Tracks

For every register and every stack slot, the verifier maintains a *type record* across the abstract interpretation:

```
struct bpf_reg_state {
    enum bpf_reg_type type;          /* SCALAR, PTR_TO_MAP_VALUE, PTR_TO_PACKET, … */
    s64 smin_value, smax_value;       /* signed range */
    u64 umin_value, umax_value;       /* unsigned range */
    u32 var_off;                       /* known bit patterns */
    int off;                           /* offset from base pointer */
    u32 range;                         /* for packet pointers, valid bytes ahead */
    struct bpf_map *map_ptr;           /* if pointing into a map value */
    /* … */
};
```

This is the source of the verifier's superpower: it doesn't merely check *types*, it tracks *value ranges*. If you write `if (x > 10) { /* use x */ }` the verifier knows on the true branch that `x >= 11`, and any subsequent comparison or array index is checked against that bound.

That's why patterns like:

```c
__u32 idx = ctx->user_ip4 % 256;
if (idx >= 256)                    /* impossible — but the verifier doesn't always know */
    return DROP;
val = array[idx];
```

…sometimes fail. The verifier may have proved `idx < 256` already (since `% 256` constrains it), making the check unreachable. Or it may not, in which case removing the check causes "invalid array access." Trial and error finds the path the verifier accepts.

The verifier walks every reachable path. At branch points, it forks the state. If two branches reach the same point with different states, it merges them (taking the union of ranges). The total path budget is 1,000,000 instructions explored — *not* program instructions executed, but the verifier's path-state expansions. A naive program with many branches can exhaust this budget while still being small in source form.

### 3.5 Loops and `bpf_loop()`

Before kernel 5.3:

```c
#pragma unroll
for (int i = 0; i < 8; i++) {        /* hardcoded small constant; clang unrolls */
    /* body */
}
```

The verifier sees 8 inlined copies of the body. Fine for small N; explosive for N=1024.

5.3+ bounded loops:

```c
for (int i = 0; i < n; i++) {        /* compiler emits a backward branch */
    /* body */
}
```

The verifier proves termination by symbolic execution. Works for constant or provably bounded N.

5.13+ `bpf_loop()`:

```c
static long body(__u32 idx, void *data) {
    /* return 0 to continue, 1 to break */
    return 0;
}

bpf_loop(1024, body, &data, 0);
```

The kernel implements the loop itself. The verifier verifies the *body* once. Massive complexity reduction, supports much larger iteration counts.

Cilium uses `bpf_loop()` heavily in newer kernels for things like walking a CIDR list, iterating policy entries, etc. On older kernels it falls back to unrolled patterns.

---

## 4. BTF and CO-RE

### 4.1 The Distribution Problem

You compiled a BPF program on a kernel that has `struct sk_buff` with field `priority` at offset 0x28. You ship the .o file. Customer A runs a kernel where the same field is at offset 0x30 because they have a different config. Your program reads garbage and the customer files an angry bug.

For ten years, the answer was *recompile from source on every kernel*. Tools like BCC shipped LLVM and kernel headers and compiled at startup. That made BCC tools slow (multi-second startup), fat (hundreds of MB of LLVM and headers), and unreliable (subtle differences in distro kernel headers).

CO-RE (Compile Once - Run Everywhere) is the way out. It rests on BTF.

### 4.2 BTF (BPF Type Format)

BTF is a compact, deduplicated encoding of C type information. Each kernel that supports BTF (5.2+) ships a `vmlinux` BTF blob, accessible at `/sys/kernel/btf/vmlinux`, that describes every kernel type as the kernel was built. A BPF object file can include its own `.BTF` section that describes the types it expects.

The kernel BTF answers questions like:
- How big is `struct task_struct` *on this kernel*?
- What is the offset of `task_struct->comm` *on this kernel*?
- What enum value is `IPPROTO_TCP` *on this kernel*?

### 4.3 CO-RE Relocations

When you compile a CO-RE-aware BPF program with clang, expressions like `BPF_CORE_READ(task, comm)` or `bpf_core_field_offset(struct sk_buff, priority)` do not bake in offsets at compile time. They emit *relocations* in the `.BTF.ext` section that the loader resolves *against the target kernel's BTF* when the program is loaded.

```
                                          libbpf::bpf_object__open
                                          ┌────────────────────────┐
   compile-time                            │  read program's .BTF   │
   ┌──────────────────────────┐            │  read kernel's BTF     │
   │ bpf_core_field_offset(   │            │  for each CO-RE reloc: │
   │   struct sk_buff,        │            │    look up the field   │
   │   priority);             │            │    in kernel BTF       │
   └─────────────┬────────────┘            │    rewrite the BPF     │
                 │                          │    instruction with    │
                 ▼                          │    the actual offset   │
   ┌──────────────────────────┐            └─────────┬──────────────┘
   │  BPF reloc:              │                      │
   │  "fetch offset of        │   ───load──►         ▼
   │   sk_buff.priority"      │              ┌───────────────────┐
   │  instr: ldw r0, [r1+??]  │              │ kernel JIT'd code │
   └──────────────────────────┘              │ ldw r0, [r1+0x28] │
                                             └───────────────────┘
```

The result: one .o file, identical bytes, works across kernel 5.10 → 6.x with different struct layouts and even (with `bpf_core_type_exists` + branch elision) different *enum values* and *field presence*.

### 4.4 Why This Matters Operationally

Cilium ships one set of BPF object files. Tetragon ships one. So does Falco's modern driver, Pixie, Parca, Inspektor Gadget. All of them load on:

- Bare-metal Ubuntu 22.04 (kernel 5.15)
- GKE Container-Optimized OS (kernel 6.1)
- Amazon Linux 2023 (kernel 6.1)
- Bottlerocket (kernel 6.1)
- Talos Linux (kernel 6.6)

…without recompilation. If you have ever managed Falco the old way, where the kernel module had to be rebuilt for every node's running kernel, you know the upgrade pain CO-RE eliminates.

### 4.5 What Happens When BTF Is Missing

Some older distributions did not enable `CONFIG_DEBUG_INFO_BTF=y`. Cilium has multiple fallback paths:

1. Look for kernel BTF at `/sys/kernel/btf/vmlinux`.
2. If absent, look for an *external* BTF blob (sometimes shipped by `kernel-debuginfo` packages).
3. Try BTFHub, a community-maintained repository of BTF blobs for kernels that lack them.

On a kernel without BTF and without external blobs, Cilium will refuse to start, or degrade to a smaller feature set. The pragmatic answer is *use a distro with BTF*.

### 4.6 CO-RE in Action: A Read Across Kernel Versions

```c
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>

SEC("kprobe/tcp_sendmsg")
int kprobe__tcp_sendmsg(struct pt_regs *ctx) {
    struct sock *sk = (struct sock *)PT_REGS_PARM1(ctx);

    /* Without CO-RE: bake in the offset at compile time. Wrong on most kernels. */
    /* u16 sport = sk->__sk_common.skc_num; */

    /* With CO-RE: emit a reloc; the loader rewrites the offset at load time. */
    u16 sport = BPF_CORE_READ(sk, __sk_common.skc_num);
    u16 dport = BPF_CORE_READ(sk, __sk_common.skc_dport);

    /* Field existence check, also CO-RE: */
    if (bpf_core_field_exists(struct sock, sk_priority)) {
        u32 prio = BPF_CORE_READ(sk, sk_priority);
        /* use prio */
    }

    bpf_printk("send from sport=%u to dport=%u\n", sport, bpf_ntohs(dport));
    return 0;
}
```

`BPF_CORE_READ` expands to a chain of `bpf_probe_read_kernel` calls with CO-RE relocations on each field offset. The bytecode emitted has placeholder offsets that the loader (via libbpf) fills in from the running kernel's BTF.

`bpf_core_field_exists()` lets you write programs that gracefully degrade on older kernels missing newer fields. This is what lets Cilium support a 4+ year range of kernels with one binary.

---

## 5. Cilium Architecture

### 5.1 The Components

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              CONTROL PLANE                                   │
│                                                                              │
│   ┌────────────────────┐       ┌──────────────────────────────────────────┐ │
│   │  kube-apiserver    │  ◄──► │  cilium-operator (Deployment, 1-3 reps) │ │
│   │                    │       │   • IPAM management (cluster-pool)       │ │
│   │  CRDs:             │       │   • CiliumIdentity GC                    │ │
│   │   CiliumEndpoint   │       │   • CES (CiliumEndpointSlice) compaction │ │
│   │   CiliumIdentity   │       │   • etcd connection management           │ │
│   │   CiliumNode       │       └──────────────────────────────────────────┘ │
│   │   CiliumNetworkPolicy                                                   │
│   │   CiliumClusterwideNetworkPolicy                                        │
│   │   CiliumEgressGatewayPolicy                                             │
│   │   CiliumLoadBalancerIPPool                                              │
│   │   CiliumL2AnnouncementPolicy                                            │
│   │   CiliumBGPPeeringPolicy                                                │
│   │   CiliumNodeConfig                                                      │
│   │   CiliumEndpointSlice                                                   │
│   │   CiliumExternalWorkload                                                │
│   └─────────┬──────────┘                                                    │
│             │ watch                                                          │
│             │                                                                │
│             ▼                                                                │
│   ┌─────────────────────────────────────────────────────────────────────┐  │
│   │  cilium-agent (DaemonSet — one per node)                            │  │
│   │  ───────────────────────────────────────                            │  │
│   │  • daemon: orchestrator, REST/Unix-socket API on /var/run/cilium    │  │
│   │  • policy engine: builds policy maps from CNP/CCNP                   │  │
│   │  • IPAM client: requests Pod IPs from operator's pool                │  │
│   │  • endpoint manager: per-Pod state                                   │  │
│   │  • datapath loader: compiles BPF C → loads → attaches to hooks       │  │
│   │  • CNI plugin (binary on host, IPC to agent over UDS)                │  │
│   │  • health endpoint, prometheus metrics, monitor (Hubble events)      │  │
│   │  • envoy proxy (in-process or DaemonSet sidecar) for L7              │  │
│   └─────────┬───────────────────────────────────────────────────────────┘  │
│             │ writes BPF maps + attaches programs                            │
│             ▼                                                                │
└──────────────────────────────────────────────────────────────────────────────┘
                                  ┌──────────────────────────────────┐
                                  │   Linux kernel (per node)        │
                                  │                                  │
                                  │   eth0 ── XDP ─── TC ─── netfilter│
                                  │   veth1 ── TC ─── pod1            │
                                  │   veth2 ── TC ─── pod2            │
                                  │   cgroup/v2 ── connect4/6 hook    │
                                  │                                  │
                                  │   BPF maps:                       │
                                  │     /sys/fs/bpf/tc/globals/       │
                                  │       cilium_lb4_services_v2     │
                                  │       cilium_lb4_backends_v3     │
                                  │       cilium_ipcache             │
                                  │       cilium_ct4_global          │
                                  │       cilium_policy_*            │
                                  │       cilium_events (ringbuf)    │
                                  │     /sys/fs/bpf/ip/...           │
                                  │                                  │
                                  └──────────────────────────────────┘
```

### 5.2 cilium-agent: What It Actually Does

The agent (`daemon/cmd/daemon.go` in `cilium/cilium`) is a Go process that, on startup:

1. Initializes the BPF filesystem mount (`/sys/fs/bpf`), creates subdirectories for pinned maps.
2. Reads its config from the ConfigMap (`cilium-config`) and the per-node `CiliumNode` CR.
3. Connects to the apiserver and starts watching: Endpoints, Services, Pods, Nodes, NetworkPolicies, CiliumNetworkPolicies, Identities, IPCache entries.
4. Discovers (or creates) BPF map files; pins them so they survive agent restarts (this is *crucial* — without map pinning, an agent restart would drop every packet for the time it takes to rebuild maps).
5. Compiles per-endpoint BPF programs. The agent ships a template `bpf_lxc.c` and uses clang to compile a customized version per endpoint, with `#define`s injected for the endpoint's identity, security policy slot, and so on. The resulting .o is loaded via `tc filter add ... bpf da obj ...` on the veth.
6. Hooks the host-side programs (`bpf_host.c` on the physical device, `bpf_overlay.c` on the tunnel device, `bpf_sock.c` on `/sys/fs/cgroup`).
7. Starts the REST/Unix-socket API (`cilium-dbg` clients talk to this), the Prometheus metrics server, and the Hubble monitor.

There are reconcilers for each of those resource types. Each one watches via client-go informers (chapter 08) and on change updates the relevant BPF maps. The actual BPF *programs* are reloaded only when their template-time configuration changes (which is rare). The *maps* are updated constantly — this is the "data plane stays put, state moves through maps" pattern, and it is why Cilium scales: program loading is slow, map updates are fast.

### 5.3 cilium-operator: Cluster-Wide Bookkeeping

A small Deployment (typically 2 replicas with leader election). Owns:

- **IPAM in `cluster-pool` mode.** When a node joins, the operator allocates a `/24` (or whatever you configure) out of the cluster pool to the node's CiliumNode object. The agent reads this and uses it for Pod IP assignment without consulting the operator on every Pod creation.
- **Identity garbage collection.** Identities are reference-counted by Endpoints; the operator periodically scans and reclaims unreferenced ones.
- **CiliumEndpointSlice compaction.** Large clusters generate huge volumes of CiliumEndpoint events; CES coalesces them.
- **etcd integration** (if you opt into etcd-backed identity allocation across very large clusters).
- **LoadBalancer IPAM** for the on-prem LB feature (`CiliumLoadBalancerIPPool`).

### 5.4 cilium-cli and Hubble CLI

`cilium-cli` is the operator-facing tool: `cilium install`, `cilium status`, `cilium connectivity test`, `cilium upgrade`. It talks to the apiserver and to in-cluster pods via `kubectl exec`.

`cilium-dbg` is the *agent-side* debugging tool, shipped inside the agent container. It is what you run via `kubectl exec -it cilium-xxxxx -- cilium-dbg ...`. Subcommands:

- `cilium-dbg status` — agent health, datapath mode, kube-proxy replacement
- `cilium-dbg endpoint list` — local endpoints + their identities
- `cilium-dbg service list` — local LB services
- `cilium-dbg bpf lb list` — what's in the BPF service maps
- `cilium-dbg bpf ct list global` — connection tracking entries
- `cilium-dbg bpf policy get <endpoint-id>` — policy enforcement state
- `cilium-dbg policy get` — installed policies
- `cilium-dbg map list` — all pinned maps + sizes
- `cilium-dbg monitor` — live BPF event stream (this is what Hubble consumes)

`hubble` is the observability CLI: `hubble observe`, `hubble status`. It talks to the local hubble-relay.

### 5.5 ConfigMap-Driven Configuration

Everything is in `cilium-config` ConfigMap. Some keys you'll see:

```
kube-proxy-replacement: "true"
tunnel-protocol: "vxlan"   # or "geneve" or "disabled" (= native routing)
routing-mode: "tunnel"     # or "native"
ipv4-native-routing-cidr: "10.244.0.0/16"
ipam: "cluster-pool"       # or "kubernetes", "eni", "azure", "crd"
cluster-pool-ipv4-cidr: "10.244.0.0/16"
cluster-pool-ipv4-mask-size: "24"
enable-ipv6: "false"
enable-bpf-masquerade: "true"
enable-host-routing: "true"
enable-l7-proxy: "true"
enable-endpoint-routes: "false"
auto-direct-node-routes: "false"
masquerade-protocols: "ipv4"
identity-allocation-mode: "crd"   # or "kvstore"
hubble.enabled: "true"
hubble.metrics.enabled: '["dns","drop","tcp","flow","port-distribution","icmp","http"]'
encryption.enabled: "true"
encryption.type: "wireguard"
```

Changing these via Helm and `cilium upgrade` triggers an agent restart that reloads programs with the new config baked in.

---

## 6. The Endpoint and Identity Model

### 6.1 What Is an Endpoint?

An *endpoint* is Cilium's per-Pod abstraction. Concretely, it is:

- A veth pair: one end (`lxcXXXX`) in the host netns, the other (`eth0`) in the Pod netns.
- A set of BPF programs attached to the host-side veth at TC ingress and egress.
- An IP address (IPv4 and/or IPv6).
- A set of labels copied from the Pod (and from the Namespace).
- An *identity* — a numeric ID assigned based on those labels.
- A policy state: which other identities can talk to it, on which ports, with which L7 policies.
- Local BPF map entries: the policy map, the per-endpoint config map, the local IP↔identity entry.

Endpoint state is reflected as a `CiliumEndpoint` (CEP) Custom Resource so other agents and tools can see it.

### 6.2 The Identity: Cilium's Killer Abstraction

In iptables-based segmentation, you write rules against IPs. Pods change IPs constantly (rescheduling, scaling), so you end up either reprogramming the firewall on every change (kube-proxy at scale) or using label-selectors translated into IPs on the fly (the NetworkPolicy controller in Calico).

Cilium takes a different approach. An **identity** is a numeric ID that represents *the set of security-relevant labels* on a Pod. Two Pods with identical relevant labels get the *same* identity.

Identity allocation:

```
Pod 1: labels = {k8s:app=frontend, k8s:env=prod, k8s:io.kubernetes.pod.namespace=shop}
Pod 2: labels = {k8s:app=frontend, k8s:env=prod, k8s:io.kubernetes.pod.namespace=shop}
Pod 3: labels = {k8s:app=backend,  k8s:env=prod, k8s:io.kubernetes.pod.namespace=shop}

Identity 12345 ← {app=frontend, env=prod, namespace=shop}  (Pod 1 + Pod 2)
Identity 12346 ← {app=backend,  env=prod, namespace=shop}  (Pod 3)
```

Identity allocation happens in one of two modes:

- **`identity-allocation-mode: crd`** (default): identities are CiliumIdentity CRs. Each agent independently computes a label SHA and either reads an existing CI or creates one via a deterministic allocation protocol with retry on conflict.
- **`identity-allocation-mode: kvstore`**: identities live in an external etcd. Used in very large clusters (>1000 nodes) where the apiserver becomes a bottleneck for identity-related watches.

Identities are scoped:

- **Numeric reserved**: 1-65535 are reserved for well-known categories — 1 (`host`), 2 (`world`, the internet), 3 (`unmanaged`), 4 (`health`), 5 (`init`), 6 (`remote-node`), 7 (`kube-apiserver`), 8 (`ingress`), etc.
- **Numeric local-scoped**: 16M-32M, allocated per-node, used for CIDR identities (each external CIDR gets its own identity number).
- **Numeric global-scoped**: 65536-16M, the workload identities, allocated cluster-wide.

### 6.3 The IPCache: Mapping IPs ↔ Identities

Every node maintains an *IPCache* (`cilium_ipcache`, an LPM trie keyed by IP/prefix) that maps every cluster-known IP to its identity. When a packet arrives at a node, the host BPF program looks up the source IP in `cilium_ipcache`, retrieves the source identity, then evaluates policy against the destination identity.

```
src_ip = 10.244.1.42   ──►  cilium_ipcache  ──►  identity 12345 (frontend)
dst_ip = 10.244.2.17   ──►  cilium_ipcache  ──►  identity 12346 (backend)

Policy map for endpoint 12346:
   { src=12345, dport=8080, proto=TCP }  →  ALLOW
   { src=12345, dport=*,    proto=*   }  →  L7 redirect
   ...

Lookup once, decide.
```

The IPCache is the synchronization point: every agent must learn about every other Pod's IP↔identity mapping. This is the highest-volume thing Cilium synchronizes across the cluster — it changes every time any Pod schedules anywhere. The CRD-mode flow uses CiliumEndpoint events; the kvstore mode uses etcd watches.

### 6.4 Why Identity Beats Per-IP

Two reasons:

1. **O(1) policy evaluation.** Policy is keyed by `(src_identity, dst_identity, dport, proto)`. One map lookup gives the verdict. iptables walks rules.
2. **Stable across IP churn.** Roll a Deployment: every Pod gets a new IP, but the identity does not change because the labels do not change. The policy map is unchanged. Compare to per-IP enforcement, where a rolling update reprograms the entire firewall N times.

The price is a more complex control plane: someone has to compute identities and distribute them. That is what the operator + agents do.

---

## 7. Cilium Datapath Modes

### 7.1 The Modes at a Glance

```
┌────────────────────────────────────────────────────────────────────────────┐
│                        TUNNEL MODE  (vxlan or geneve)                      │
│                                                                            │
│   Pod A (Node 1) ──► veth ──► cilium_host ──► cilium_vxlan (UDP 8472)      │
│                                                          │                 │
│                                                          ▼                 │
│                                      ┌─── underlay (any IP fabric) ───┐    │
│                                      └────────────────┬────────────────┘    │
│                                                       ▼                    │
│   cilium_vxlan ──► cilium_host ──► veth ──► Pod B (Node 2)                 │
│                                                                            │
│   Encap header: ETH + IP + UDP + VXLAN(8 bytes) + inner ETH + inner IP     │
│                                                                            │
│   Underlay only needs to route between node IPs. Pod CIDR can be anything. │
└────────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────────┐
│                       NATIVE ROUTING MODE                                  │
│                                                                            │
│   Pod A (Node 1, podIP 10.244.1.42)                                        │
│           │                                                                │
│           ▼                                                                │
│       veth ──► cilium_host (route lookup)                                  │
│           │                                                                │
│           ▼                                                                │
│       eth0 (node 1 NIC)                                                    │
│           │                                                                │
│           │   Underlay must route 10.244.0.0/16 (the Pod CIDR)             │
│           │   to the right nodes. Either via BGP (Cilium BGP, MetalLB,     │
│           │   ToR routers) or via cloud provider routes (auto-direct-     │
│           │   node-routes, GCP VPC routes, AWS VPC routes).                │
│           ▼                                                                │
│       eth0 (node 2 NIC)                                                    │
│           │                                                                │
│           ▼                                                                │
│       cilium_host ──► veth ──► Pod B                                       │
│                                                                            │
│   No encap overhead. MTU = underlay MTU. Underlay must understand the     │
│   Pod IP space.                                                            │
└────────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────────┐
│                          AWS ENI MODE                                       │
│                                                                            │
│   Pods get IPs directly from VPC subnet (secondary IPs on ENIs).           │
│   No tunnel needed; AWS VPC routes Pod traffic natively.                   │
│   cilium-operator manages ENI allocation (replaces AWS VPC CNI).           │
│                                                                            │
│   Pod A (Node 1, podIP = ENI secondary IP from VPC subnet)                 │
│        │                                                                   │
│        ▼                                                                   │
│       attached ENI directly ──► AWS VPC ──► Pod B's ENI                    │
└────────────────────────────────────────────────────────────────────────────┘
```

### 7.2 Tunnel Mode (VXLAN / Geneve)

In tunnel mode each node creates a `cilium_vxlan` (or `cilium_geneve`) virtual interface. Egress traffic for a remote Pod is BPF-redirected to that interface, where the kernel adds an outer UDP+VXLAN header with the destination Node IP. On the receiver, the kernel decaps, hands the inner packet to `cilium_host`, and the host TC ingress program looks up the destination endpoint and redirects to the veth.

Why tunnel:
- Underlay only needs node-to-node routing. Works on any network — cloud VPCs that don't allow non-VPC IPs, on-prem networks without BGP, even networks where you can't control routing.
- Pod CIDR can overlap with the underlay (the encap hides it).

Why not tunnel:
- Outer header overhead: 50 bytes (VXLAN) or 50+ bytes (Geneve with options). MTU drops accordingly.
- A second packet path costs cycles.
- Some hardware can't offload VXLAN checksum/segmentation, hurting throughput.

Geneve vs VXLAN:
- VXLAN is fixed 8-byte header, 24-bit VNI.
- Geneve has the same base but with **variable-length options** — a TLV mechanism where Cilium can stamp identity, security context, or trace IDs into the encapsulation, useful for some advanced features (cluster-mesh identity propagation).

### 7.3 Native Routing

`routing-mode: native` + `auto-direct-node-routes: true` (if all nodes share an L2) or BGP-driven routes. The kernel routes between nodes via the underlay; Cilium just makes sure the underlay knows about the Pod CIDRs.

Pros: no encap; full MTU; closer to baseline Linux performance.
Cons: requires routable Pod CIDRs in the underlay. On cloud you usually need cloud-routes integration (GKE's "VPC-native" mode, GCP route mode, AWS using VPC route tables — which has limits on number of routes). On-prem usually means BGP.

### 7.4 AWS ENI Mode

`ipam: eni` + `routing-mode: native`. Cilium operator talks to EC2 to attach ENIs to nodes and harvest secondary IPs. Each Pod IP is a real VPC IP. AWS handles routing.

Effectively this is "the AWS VPC CNI, but with Cilium's datapath, identity, and policy on top." Trade-off: Pod density is limited by ENI secondary-IP limits (large instance types max out around 200-400 IPs).

### 7.5 Choosing

| Constraint | Pick |
|------------|------|
| You don't control the underlay | Tunnel |
| You have BGP or cloud-routes | Native routing |
| You're on AWS and want VPC IPs | ENI mode |
| You're on Azure | Azure CNI in chained mode, or Azure-Cilium |
| You want to encode metadata in encap | Geneve |

---

## 8. kube-proxy Replacement (Deep)

This is the marquee feature: making kube-proxy obsolete by handling Service load balancing in BPF.

### 8.1 The Insight

A ClusterIP packet hits the kernel from a Pod. In the kube-proxy world, the kernel applies a DNAT at netfilter PREROUTING/OUTPUT, rewrites the destination IP/port to a backend Pod, and then routes the packet. This is per-packet, requires conntrack to reverse the translation on replies, and requires walking the iptables rule chain.

Cilium's observation: *the destination rewrite can happen at `connect(2)` time, in the socket address, before any packet exists.* The kernel then connects directly to the backend; every packet on the connection naturally has the backend IP as its destination; no per-packet DNAT, no conntrack for reverse translation.

### 8.2 The Sequence

```
┌────────────────────────────────────────────────────────────────────────────┐
│                  ClusterIP via cgroup connect4 hook                         │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│   Pod App (in netns + cgroup)                                              │
│   ────────────                                                             │
│      int fd = socket(AF_INET, SOCK_STREAM, 0);                             │
│      connect(fd, &sa, sizeof(sa))                                          │
│         sa.sin_addr = 10.96.0.42         /* ClusterIP                  */  │
│         sa.sin_port = htons(80);                                           │
│                                                                            │
│   1. syscall enters kernel                                                 │
│   2. __sys_connect → kernel's inet_stream_connect                          │
│   3. BPF cgroup hook fires: BPF_CGROUP_INET4_CONNECT                       │
│         ↓                                                                  │
│      bpf_sock_addr_t *ctx                                                  │
│         user_ip4   = 10.96.0.42                                            │
│         user_port  = 80                                                    │
│         protocol   = IPPROTO_TCP                                           │
│         ↓                                                                  │
│      bpf_lb4_lookup_service(&key);                                         │
│         key.address = 10.96.0.42, dport=80, proto=TCP, backend_slot=0      │
│         ↓ (hit)                                                            │
│      svc->count = 3                                                        │
│         ↓                                                                  │
│      pick a backend slot via Maglev (or random with affinity):             │
│         backend_id = 0x1234                                                │
│         ↓                                                                  │
│      lookup backend in cilium_lb4_backends_v3:                             │
│         backend.address = 10.244.1.42, backend.port = 8080                 │
│         ↓                                                                  │
│      REWRITE the context:                                                  │
│         ctx->user_ip4   = 10.244.1.42                                      │
│         ctx->user_port  = bpf_htons(8080)                                  │
│         ↓                                                                  │
│      return SYS_PROCEED;  /* SYS_PROCEED = 1 = allow with modifications */ │
│                                                                            │
│   4. inet_stream_connect proceeds with the REWRITTEN address.              │
│      The kernel performs a normal TCP three-way handshake to 10.244.1.42:8080│
│   5. Subsequent packets on this socket have                                │
│         saddr = pod's source IP, daddr = 10.244.1.42                       │
│      No DNAT needed. No conntrack entry needed.                            │
│                                                                            │
│   On the reply side: backend Pod receives connection with the source IP    │
│   of the client Pod — no SNAT happened. (Optionally Cilium can SNAT for    │
│   client-traffic-policy=Cluster cases.)                                    │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

### 8.3 Why This Is So Much Faster

| Step | iptables kube-proxy | Cilium socket LB |
|------|---------------------|------------------|
| Resolve VIP | walk KUBE-SERVICES chain (O(N)) | hash lookup (O(1)) |
| Pick backend | random number → traverse probabilistic rules | Maglev hash or random in O(1) |
| Apply DNAT | iptables nf_nat hook, every packet on the flow | once, at connect, address only |
| Reverse on replies | conntrack lookup, every reply packet | none — direct socket |
| SNAT to client | nf_nat MASQUERADE if needed | optional, in BPF |
| Update of backend list | iptables-restore (full rule set) | atomic map write |
| Memory | N rules + N×M conntrack entries | N map entries |

For an established connection serving a sustained 10 Gbps stream, the iptables path costs ~5-10 µs per packet through the netfilter chain (varies with rule count, conntrack pressure). Cilium socket LB costs *zero* per-packet — the rewrite happened once at connect. Same-node connections additionally get sockmap fast-path (see §9.4), which routes payload bytes directly between sockets via BPF, skipping the IP stack entirely.

### 8.4 What About NodePort and LoadBalancer?

The cgroup connect4 trick only works when the *client is on a node* (specifically, in a cgroup that has the BPF program attached). External clients hitting a NodePort or LoadBalancer have no opportunity to be intercepted at connect — they are sending packets, not opening sockets.

For those, Cilium handles the load balancing at TC ingress on the host's external NIC (or at XDP, if enabled). The TC program:

1. Identifies the packet as NodePort or LB-VIP traffic.
2. Picks a backend (Maglev).
3. Either:
   - DNATs to the backend Pod IP (if the backend is on this node, redirect to its veth via `bpf_redirect_peer`); or
   - DNATs *and* encapsulates (if the backend is on another node, send via the tunnel); or
   - DNATs and routes natively (native-routing mode).
4. Adds a conntrack entry to `cilium_ct4_global` so the reply gets reverse-translated.

The reply, on its way back through TC egress, is DNATed back to the LB VIP (the client expects to see it from the VIP, not from a Pod IP).

This is the only path that still needs conntrack — but it is a Cilium-managed BPF conntrack map, not the kernel netfilter conntrack. Cilium's CT supports its own state machine in BPF, scales linearly with cores via per-CPU maps, and does not collide with anything else.

### 8.5 Verifying the Win

```bash
$ kubectl exec -n kube-system cilium-xxxxx -- iptables-save | grep KUBE-SERVICES
# (empty if kubeProxyReplacement is fully active)

$ kubectl exec -n kube-system cilium-xxxxx -- cilium-dbg status | grep KubeProxyReplacement
KubeProxyReplacement:    True   [eth0   192.0.2.10/24 (Direct Routing)]

$ kubectl exec -n kube-system cilium-xxxxx -- cilium-dbg service list
ID   Frontend             Service Type   Backend
1    10.96.0.1:443        ClusterIP      1 => 192.0.2.10:6443 (active)
2    10.96.0.10:53        ClusterIP      1 => 10.244.0.5:53 (active)
                                         2 => 10.244.1.7:53 (active)
17   10.96.42.10:80       ClusterIP      1 => 10.244.1.42:8080 (active)
                                         2 => 10.244.2.17:8080 (active)
                                         3 => 10.244.3.91:8080 (active)

$ kubectl exec -n kube-system cilium-xxxxx -- cilium-dbg bpf lb list
SERVICE ADDRESS         BACKEND ADDRESS
10.96.42.10:80          0.0.0.0:0 (17) (0) [ClusterIP, non-routable]
                        10.244.1.42:8080 (17) (1)
                        10.244.2.17:8080 (17) (2)
                        10.244.3.91:8080 (17) (3)
```

---

## 9. The cgroup Hooks in Detail

Cilium uses several cgroup-attached BPF programs. They live in `bpf/bpf_sock.c` in the cilium source.

### 9.1 connect4 / connect6

Fires on `connect(2)`. Already covered in §8 — rewrites the destination socket address if it matches a service VIP. The hook is `BPF_CGROUP_INET4_CONNECT` (and v6 equivalent).

```c
SEC("cgroup/connect4")
int cil_sock4_connect(struct bpf_sock_addr *ctx) {
    if (ctx->user_family != AF_INET)
        return SYS_PROCEED;
    if (!is_cluster_service(ctx->user_ip4, ctx->user_port, ctx->protocol))
        return SYS_PROCEED;
    sock4_xlate(ctx);   /* the actual rewrite */
    return SYS_PROCEED;
}
```

### 9.2 sendmsg4 / sendmsg6

UDP and SCTP are connectionless — `connect(2)` is often not called. For UDP, each `sendto(2)` or `sendmsg(2)` carries a destination address. Cilium attaches `BPF_CGROUP_UDP4_SENDMSG` (and v6) and rewrites *per message*. This makes UDP services (CoreDNS!) work with the same socket-level LB story.

```c
SEC("cgroup/sendmsg4")
int cil_sock4_sendmsg(struct bpf_sock_addr *ctx) {
    if (ctx->protocol != IPPROTO_UDP)
        return SYS_PROCEED;
    sock4_xlate(ctx);
    return SYS_PROCEED;
}
```

There's also `recvmsg4`/`recvmsg6` to reverse-rewrite the source on incoming UDP datagrams so the application sees the VIP, not the backend.

### 9.3 getsockname / getpeername

When a Pod calls `getpeername(2)` on a connection that was redirected by connect4, the kernel returns the *real* backend address. Some applications check this and get confused. Cilium attaches `getpeername4`/`getpeername6` to rewrite the answer back to the VIP. Same for `getsockname`. This is "lying to the application for consistency" — gross, but necessary for legacy apps.

### 9.4 sock_ops + sockmap: Same-Node Fast Path

This is a beautiful trick. When two Pods on the same node talk to each other through a Service VIP:

1. The client's connect4 hook rewrites the destination to the local backend Pod's IP.
2. The kernel opens a normal TCP connection to that Pod over the loopback path.
3. The `BPF_PROG_TYPE_SOCK_OPS` program fires on each TCP state event (`BPF_SOCK_OPS_ACTIVE_ESTABLISHED_CB` on connect, `BPF_SOCK_OPS_PASSIVE_ESTABLISHED_CB` on accept). It calls `bpf_sock_hash_update()` to add both sockets to a sockhash map keyed by (saddr, sport, daddr, dport).
4. A `BPF_PROG_TYPE_SK_MSG` program is attached to that sockhash. On every `sendmsg`, it calls `bpf_msg_redirect_hash()` to redirect the payload bytes directly to the peer's socket — *bypassing the entire IP stack*.

The packet never hits IP, never hits TCP segmentation, never goes through netfilter. The payload bytes go directly from one socket buffer to the other. Latency drops by 1-2 µs per round-trip, and throughput goes up significantly.

Cilium documents this as "host-routing" and it is on by default with `enable-host-routing: true`.

### 9.5 getsockopt / setsockopt

`BPF_PROG_TYPE_CGROUP_SOCKOPT` programs can intercept these syscalls. Cilium uses them to:
- Optionally rewrite or reject certain socket options (e.g., enforcing `IP_TRANSPARENT` rules).
- Inject metadata into the socket via `sk_storage` for later use by other programs.

Mostly transparent; not a primary feature surface.

---

## 10. Maglev Hashing

### 10.1 Why Not Random?

The naive backend-selection policy for a service is "pick a random backend." But random hashing has a property: when the backend set changes, on average N/M of the existing flows get remapped (where M is the backend count). For a service with 100 backends and one going down, ~1% of connections move to a different backend. For TCP, this means RST and reconnect.

What you want is **consistent hashing**: when backends change, only a small, bounded fraction of flows move. For a backend leaving, only the flows that were on that backend should be disrupted; everything else should stay.

### 10.2 Maglev Specifically

Maglev is Google's consistent-hashing variant (published 2016, used in Google's network LBs). It builds a *lookup table* (typically 16,381 or 65,521 entries, prime numbers) where each entry points to a backend. Each backend gets a "preference list" of slot positions, and they take turns picking their next preferred unfilled slot, like a draft. The result is a near-uniform distribution where each backend gets table_size / M entries, and when one backend disappears, only ~table_size/M entries (its own) need to be reassigned.

Lookup is O(1):
```c
__u32 hash = jhash_2words(saddr, sport, salt) ^ jhash_2words(daddr, dport, salt);
__u32 slot = hash % LB4_MAGLEV_LOOKUP_SIZE;
__u32 backend_id = maglev_table[slot];
```

This is one BPF map lookup per packet (or per connection for ClusterIP). It is the default for Cilium LB at scale.

Enable with `loadBalancer.algorithm: maglev` (vs the default `random`).

### 10.3 Maglev Resilience to Backend Churn

Empirical numbers (from the Cilium docs and our own measurements):

- 100 backends, one removed: ~1.0% of flows remap (only the removed backend's flows; everyone else stays put).
- 100 backends, one added: ~1.0% of flows shift to the new one; the rest are stable.
- Compare random: each backend change moves ~1/M of *all* flows, but unpredictably — connection X could now hash to backend Y even though backend X is still up.

For long-lived flows (database connections, gRPC long-polls), Maglev is dramatically better.

---

## 11. NetworkPolicy Enforcement

### 11.1 The Policy Map

Each endpoint has an associated *policy map*, named `cilium_policy_NNNN` where NNNN is the endpoint ID. Its key is approximately:

```c
struct policy_key {
    __u32 sec_label;   /* peer identity */
    __u16 dport;       /* destination port */
    __u8  protocol;
    __u8  egress;      /* 0 = ingress, 1 = egress */
};

struct policy_entry {
    __u32 proxy_port;  /* nonzero = redirect to L7 proxy at this port */
    __u8  deny;        /* 1 if explicit deny */
    __u8  pad[3];
    __u64 packets;     /* counters */
    __u64 bytes;
};
```

When a packet arrives at the TC ingress of an endpoint's veth:

1. Read the source IP from the packet.
2. Look up the source identity in `cilium_ipcache`.
3. Build `policy_key = (src_identity, dport, proto, ingress=0)`.
4. Look up in `cilium_policy_NNNN`.
5. If hit and `deny==0`: pass (or redirect to proxy_port if nonzero).
6. If miss: default deny (when a policy is installed; default allow if no policy).

### 11.2 From CNP to Policy Map

A `CiliumNetworkPolicy` resource:

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: backend-from-frontend-only
  namespace: shop
spec:
  endpointSelector:
    matchLabels:
      app: backend
  ingress:
    - fromEndpoints:
        - matchLabels:
            app: frontend
      toPorts:
        - ports:
            - port: "8080"
              protocol: TCP
```

The agent's policy engine (in `pkg/policy/`) does the following on every CNP change:

1. Resolve `endpointSelector` to a set of local endpoints (the policy subjects).
2. Resolve `fromEndpoints.matchLabels` to a set of *identities* (anyone, on any node, that matches these labels gets the same identity).
3. For each subject endpoint, write entries `(src_identity, 8080, TCP, ingress=0) → allow` to its policy map.
4. Update the *policy realized* state in the CEP.

So the BPF map is the *materialized* form of the CNP, fully expanded to identities and ports. The expansion is the heavy work the agent does; the runtime lookup is one hash.

### 11.3 Standard NetworkPolicy vs CiliumNetworkPolicy

Vanilla `NetworkPolicy` (chapter 20) is implemented exactly the same way — the agent watches both `NetworkPolicy` and `CiliumNetworkPolicy`, expanding both into the same policy map. The CNP just has more expressive selectors:

| Feature | NetworkPolicy | CiliumNetworkPolicy |
|---------|---------------|---------------------|
| L3/L4 selectors | yes | yes |
| Pod label selectors | yes | yes |
| Namespace selectors | yes | yes |
| IP block (CIDR) | yes | yes |
| L7 (HTTP/gRPC/Kafka) | no | yes (via Envoy redirect) |
| FQDN egress | no | yes (`toFQDNs`) |
| ServiceAccount selectors | no | yes (`toServiceAccounts`) |
| Entity selectors (host, world, cluster) | no | yes |
| ICMP types | no | yes |
| Deny rules | no (allow-only) | yes |
| Default policies cluster-wide | no | yes (`CiliumClusterwideNetworkPolicy`) |

### 11.4 L7 Policy Enforcement

When a CNP rule includes `toPorts.rules.http`, Cilium needs to inspect HTTP requests, not just TCP. The datapath cannot do HTTP parsing in BPF (well, it can do limited things, but not full HTTP/2 framing). So the policy entry is rewritten as a *proxy redirect*:

```
policy_entry = { proxy_port = 0xC123, deny = 0, ... }
```

When the TC ingress program sees this entry, it sends the packet to the local Envoy via TPROXY (`bpf_redirect()` to the Envoy listener, with `IP_TRANSPARENT` set so Envoy sees the original src/dst). Envoy then parses the HTTP request and applies L7 rules:

```yaml
ingress:
  - fromEndpoints:
      - matchLabels: { app: frontend }
    toPorts:
      - ports: [{ port: "8080", protocol: TCP }]
        rules:
          http:
            - method: GET
              path: /api/products
            - method: POST
              path: /api/orders
              headers: ["Authorization: Bearer .*"]
```

Envoy enforces these rules and either forwards (back into the kernel, to the destination Pod) or returns 403.

Envoy in Cilium is *not* a per-Pod sidecar. It is either:
- An in-process Go-embedded Envoy in the agent (older deployments), or
- A separate per-node Envoy DaemonSet (`cilium-envoy`, the modern path).

Either way, traffic is hairpinned through Envoy on the same node — there is no extra network hop, no sidecar startup ordering, and no per-Pod resource overhead. It is the single biggest "sidecarless mesh" argument (§15).

### 11.5 Kafka and DNS L7

Cilium ships with non-HTTP L7 protocols too:

- **Kafka**: the proxy parses Kafka protocol frames and allows/denies per topic and per APIKey (e.g., "frontend identity can Produce to topic orders but not to topic billing").
- **DNS**: the proxy intercepts DNS queries and responses; this is the substrate for FQDN policies (§12).

You can also write custom Envoy filters and Cilium will hairpin to them.

---

## 12. DNS-Aware Egress (toFQDNs)

### 12.1 The Problem

You want to allow your `payments` Pod to reach `api.stripe.com` but nothing else on the internet. Stripe's API IPs change. Hard-coding CIDR ranges is a maintenance nightmare and they're often shared with other services.

### 12.2 The Mechanism

Cilium intercepts DNS responses (via the DNS proxy at L7) and learns the IP-set for each name:

```
1. Pod issues DNS query for api.stripe.com
2. Query hits the cilium DNS proxy (because the egress policy includes a
   DNS rule, which causes the agent to install a redirect from port 53
   to the proxy).
3. Proxy forwards the query upstream (CoreDNS).
4. Response comes back: api.stripe.com = [54.187.x.y, 54.187.a.b].
5. Proxy parses the response. If the policy says "toFQDNs: api.stripe.com",
   the proxy adds those IPs to the policy's allowed-IPs map for the source
   identity, with a TTL.
6. Proxy returns the response to the Pod.
7. Pod connects to 54.187.x.y. The L3/L4 policy check on egress now finds
   the IP in the allowed-IPs map → allow.
```

The proxy also enforces *which DNS names a Pod is allowed to even resolve* — so a Pod that's only allowed `api.stripe.com` can't successfully resolve `attacker.com`.

### 12.3 Example Policy

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: payments-egress
  namespace: shop
spec:
  endpointSelector:
    matchLabels:
      app: payments
  egress:
    # Allow DNS to the cluster DNS, with name filtering
    - toEndpoints:
        - matchLabels:
            k8s:io.kubernetes.pod.namespace: kube-system
            k8s:k8s-app: kube-dns
      toPorts:
        - ports: [{ port: "53", protocol: ANY }]
          rules:
            dns:
              - matchPattern: "*.stripe.com"
              - matchName: "api.example.com"
    # Allow egress to the resolved IPs
    - toFQDNs:
        - matchPattern: "*.stripe.com"
        - matchName: "api.example.com"
      toPorts:
        - ports: [{ port: "443", protocol: TCP }]
```

Note the *two* rules: one for DNS to actually do the lookup (with name filtering), one for the resolved IPs.

### 12.4 Gotchas

- **TTL expiry vs long connections.** If a Pod opens a long-lived connection and the DNS TTL expires, Cilium may evict the IP from the allow-set and drop the connection. The config `tofqdns-min-ttl` and `tofqdns-idle-connection-grace-period` mitigate this.
- **Round-robin DNS surprises.** If the upstream returns a different IP each query, and the Pod doesn't re-query for a while, you might allow the wrong subset. Use long enough min-ttl.
- **Connection-tracking via DNS.** If your app does its own caching, the DNS proxy never sees the query and the IP is never allowed. Use `enableIdentityMark` patterns or pre-populate IPs.

---

## 13. Hubble: Observability in the Datapath

### 13.1 What Hubble Is

Hubble is the observability layer for Cilium. It is built on the same datapath: every BPF program emits *events* (drops, allows, L7 records, NAT translations) into a ring buffer, which userspace consumes.

```
┌────────────────────────────────────────────────────────────────────────────┐
│                                                                            │
│   per-node:                                                                │
│                                                                            │
│       BPF programs                                                         │
│           │                                                                │
│           │  bpf_event_output_data → ringbuf("cilium_events")              │
│           ▼                                                                │
│       cilium-agent (monitor loop)                                          │
│           │                                                                │
│           │  decodes events, tags with identity labels                     │
│           │  exposes gRPC server on :4244 (hubble.sock)                    │
│           ▼                                                                │
│       hubble-relay (Deployment, cluster-wide)                              │
│           │                                                                │
│           │  fans out to all nodes, aggregates                             │
│           │  exposes gRPC :4245                                             │
│           ▼                                                                │
│       ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐    │
│       │ hubble CLI   │    │ Hubble UI    │    │  prometheus exporter │    │
│       │ (observe)    │    │ (browser)    │    │                       │    │
│       └──────────────┘    └──────────────┘    └──────────────────────┘    │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

### 13.2 What Events Look Like

A typical drop event has fields:

- timestamp
- verdict: FORWARDED / DROPPED / ERROR / AUDIT
- source: namespace, pod name, identity, labels
- destination: same
- L4: protocol, src port, dst port
- L7 (if applicable): HTTP method, URL, status; gRPC method; DNS query
- drop reason (if dropped): policy denied, invalid header, etc.

The labels come from the identity → labels mapping the agent already has in its local cache. So a Hubble event isn't `10.244.1.42 → 10.244.2.17`; it's `shop/frontend → shop/backend`.

### 13.3 Observing

```bash
# Watch flows live
$ hubble observe --follow
Oct 7 14:23:01 default/frontend-x4z9 → shop/backend-jk2p ESTABLISHED TCP 8080
Oct 7 14:23:02 default/frontend-x4z9 → shop/backend-jk2p HTTP GET /api/products 200
Oct 7 14:23:03 default/frontend-x4z9 → shop/billing-99fk DROPPED (Policy denied) TCP 9090

# Filter
$ hubble observe --to-namespace shop --verdict DROPPED
$ hubble observe --protocol http --http-status 5+
$ hubble observe --from-pod default/frontend-x4z9 --to-fqdn '*.stripe.com'

# Service map UI
# Hubble UI shows a live force-directed graph of identity-to-identity flows
```

### 13.4 Metrics

Hubble exports Prometheus metrics that are *L7-aware* and *identity-aware*:

```
hubble_http_requests_total{source="shop/frontend", destination="shop/backend",
                           method="GET", status="200"} 12345
hubble_drop_total{reason="Policy denied", protocol="TCP", source_identity="12345"} 17
hubble_tcp_flags_total{flag="SYN", source="shop/frontend"} 9999
```

This is the kind of dashboarding that previously required a sidecar mesh (Istio/Envoy with stats sinks). With Hubble it's "free" because the data was already in the datapath.

### 13.5 Tradeoffs

Hubble events are *sampled* when the rate is high (configurable). At very high packet rates the ringbuf can drop events under back pressure; the verdict is *never* affected (events are emitted after the packet is forwarded or dropped), only the observability is. For audit-quality logging you still need persistent stores (Loki, S3 via fluentbit, etc.).

---

## 14. Tetragon: Runtime Security via eBPF

### 14.1 What Tetragon Is

Tetragon is a separate but Cilium-family project: a runtime security framework built on eBPF tracing hooks (kprobes, tracepoints, LSM). It deploys as a DaemonSet alongside (or independent of) Cilium.

Where Hubble observes *network* flows, Tetragon observes:
- Process lifecycle (execve, exit, fork)
- File access (open, openat, openat2, unlink, rename)
- Network syscalls (connect, accept, bind, sendto)
- Setuid / capability changes
- Container escape primitives (mount, pivot_root)
- Arbitrary kernel functions you point it at

### 14.2 TracingPolicy: Declarative Tracing

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: detect-suspicious-exec
spec:
  kprobes:
    - call: "security_bprm_check"   # LSM hook fired during execve
      syscall: false
      args:
        - index: 0
          type: "linux_binprm"
      selectors:
        - matchArgs:
            - index: 0
              operator: "Postfix"
              values:
                - "/bin/sh"
                - "/bin/bash"
                - "/usr/bin/nc"
          matchActions:
            - action: Sigkill   # kill the process from the kernel
```

The selectors evaluate *in BPF*. Tetragon compiles policies into the BPF programs at policy-application time, so the policy decision happens entirely in the kernel — including the `bpf_send_signal(SIGKILL)` that terminates the process before it makes a syscall you didn't want.

### 14.3 Actions

- `Post` (default): emit an event to userspace
- `Sigkill`: kill the process via `bpf_send_signal`
- `FollowFD` / `UnfollowFD`: track open file descriptors for later matching
- `Override`: change syscall return value (e.g., return EPERM)
- `NotifyEnforcer` / `NoPost`: forward to enforcer chain or suppress

### 14.4 vs Falco

Falco is the canonical eBPF-era runtime security tool, but its model is different:

| | Falco | Tetragon |
|---|-------|----------|
| Architecture | eBPF (or kernel module) emits events; Falco userspace evaluates rules | BPF programs evaluate rules in-kernel; userspace gets only the matching events |
| Enforcement | Detect-only by default; reaction requires external machinery | Kill from the kernel, override syscall returns |
| Rule language | YAML DSL with macros; Sysdig-style conditions | TracingPolicy CRD, BPF-compiled selectors |
| Performance under load | Userspace must consume every event | In-kernel filtering means most events never reach userspace |
| Pod identity | After the fact (Falco enriches with pod labels) | Native Pod/namespace/identity tagging at event time |

Tetragon's promise is "you can kill the process from BPF before the syscall completes." Falco's promise is "you have a rich, audited stream of every relevant syscall." Many shops run both.

### 14.5 What Tetragon Hooks (Examples)

```yaml
# Detect any process opening /etc/shadow
spec:
  kprobes:
    - call: security_file_open
      args: [{ index: 0, type: file }]
      selectors:
        - matchArgs:
            - index: 0
              operator: Equal
              values: ["/etc/shadow"]
          matchActions: [{ action: Post }]

# Track all network connections from privileged containers
spec:
  kprobes:
    - call: tcp_connect
      args: [{ index: 0, type: sock }]
      selectors:
        - matchCapabilities: [{ type: Effective, operator: In, values: [CAP_NET_ADMIN] }]
          matchActions: [{ action: Post }]
```

---

## 15. Cilium Service Mesh (Sidecar-less)

The big question for chapter 17 is whether you need a mesh and which one. Cilium's pitch: *you have most of the mesh already*. Let's lay out what it provides without sidecars.

### 15.1 What a Mesh Provides, and Where Cilium Already Does It

| Mesh feature | Istio (sidecar) approach | Cilium approach |
|--------------|--------------------------|-----------------|
| Service discovery | Envoy reads xDS from istiod | Cilium reads Endpoints from apiserver; BPF maps |
| Load balancing | Envoy in sidecar | BPF socket LB |
| mTLS | Sidecar Envoy terminates and originates TLS | Per-node WireGuard between nodes (transparent) |
| L7 routing (canary, retry) | Sidecar Envoy | Cilium L7 policies + per-node Envoy DaemonSet |
| Traffic metrics | Sidecar emits stats | Hubble emits from BPF |
| Traces | Sidecar Envoy emits OTEL | (Limited; Hubble has some) |
| Multi-cluster service discovery | Istio replicates services via federated istiod | Cilium ClusterMesh (§16) |

### 15.2 The L7 Mesh: Per-Node Envoy

Cilium 1.13+ deploys a `cilium-envoy` DaemonSet — one Envoy per node, shared by every Pod on that node. When traffic needs L7 inspection (because a policy has L7 rules, or because the destination has L7 routing rules from the Gateway API), the BPF program redirects to this shared Envoy.

The argument vs Istio's per-Pod sidecar:

- **Resource cost**: 1 Envoy per node vs 1 per Pod. At 100 Pods/node and 100 MB/Envoy this is 10 GB vs 100 MB.
- **Startup ordering**: no need for `holdApplicationUntilProxyStarts`. The Envoy is already running.
- **Upgrades**: rolling a single per-node Envoy vs rolling every Pod in the cluster.
- **Failure isolation**: a node-Envoy crash affects all L7 traffic on that node; a sidecar crash only affects one Pod. This is the genuine downside.

Istio's "ambient mode" (covered in chapter 17) takes a similar approach with ztunnel (a per-node L4 mTLS proxy) and waypoint proxies (per-namespace L7 proxies). The architectures are converging.

### 15.3 Ingress Gateway via Gateway API

Cilium implements the Kubernetes Gateway API:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: shop-gateway
spec:
  gatewayClassName: cilium
  listeners:
    - name: http
      port: 80
      protocol: HTTP
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: shop-route
spec:
  parentRefs: [{ name: shop-gateway }]
  rules:
    - matches:
        - path: { type: PathPrefix, value: /api/v2 }
      backendRefs:
        - name: backend-v2
          port: 8080
    - matches:
        - path: { type: PathPrefix, value: / }
      backendRefs:
        - name: frontend
          port: 80
```

The Cilium operator watches Gateway/HTTPRoute resources and configures the cilium-envoy DaemonSet to terminate ingress on the right ports, route by path, and forward to the backend Service VIPs (which then go through the socket-LB path).

The LoadBalancer-IP that the Gateway listens on is provisioned either:
- By a cloud provider (CCM creates an ELB/GCE LB pointing to the nodes).
- By Cilium's built-in LB IPAM (`CiliumLoadBalancerIPPool`) with L2 announcement (ARP gratuitous on the node's NIC) or BGP.

---

## 16. Cluster Mesh: Multi-Cluster Cilium

### 16.1 The Goal

Two (or more) Kubernetes clusters, each running Cilium. You want:
- Pods in cluster A to reach Pods in cluster B by direct IP.
- ClusterIP Services to automatically include endpoints from both clusters when annotated.
- A consistent identity space, so a `frontend` Pod in cluster A and a `frontend` Pod in cluster B are the *same identity* for policy purposes.
- All over a single shared underlay (or via tunnels).

### 16.2 Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Cluster A                                                          │
│  ┌───────────────────────────┐                                      │
│  │ apiserver  cilium-agent  │                                      │
│  │           operator       │                                      │
│  │           clustermesh-   ──── exposes kvstore over NodePort/LB  │
│  │             apiserver     │                                      │
│  └──────────────┬────────────┘                                      │
└─────────────────│───────────────────────────────────────────────────┘
                  │ (etcd over TLS, mutual auth)
                  │
┌─────────────────│───────────────────────────────────────────────────┐
│                 ▼                                                   │
│  ┌───────────────────────────┐                                      │
│  │ Cluster B                 │                                      │
│  │  agents subscribe to A's  │                                      │
│  │  identity, endpoint, and  │                                      │
│  │  service updates          │                                      │
│  └───────────────────────────┘                                      │
└─────────────────────────────────────────────────────────────────────┘
```

`clustermesh-apiserver` is a small etcd-backed sync server that each cluster runs. Other clusters connect to its etcd over TLS and watch identities, endpoints, services. The agents in cluster B install IPCache entries for cluster A's Pod IPs, including the encap target (the source cluster's tunnel/native).

### 16.3 Global Services

Annotate a Service:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: payments
  annotations:
    service.cilium.io/global: "true"
    service.cilium.io/affinity: "local"  # or "remote" or "none"
```

Both clusters' Cilium agents add their local backends to the service's BPF LB map *and* the remote cluster's backends. Affinity controls preference: "local" keeps traffic in-cluster when local backends exist.

### 16.4 Identity Across Clusters

The shared kvstore distributes identities. A Pod with labels `app=frontend, env=prod` in cluster A and one in cluster B get the *same* identity. Policy written for that identity applies to both.

This is qualitatively different from federated mesh: it's not "two meshes that know about each other"; it's "two clusters whose datapaths and policies are unified."

---

## 17. WireGuard Transparent Encryption

### 17.1 The Promise

Node-to-node traffic on the underlay may go over networks you don't trust (cross-AZ, cross-region, on-prem links). You want everything between Cilium nodes encrypted, with no application changes.

### 17.2 How It Works

```
                                cilium_wg0 (WireGuard interface)
                                          │
                                          │  WG userspace key exchange
                                          │  via the cilium-agent (Curve25519)
                                          ▼
                                   wg-quick / kernel module
                                          │
                                          ▼
Pod A (Node 1) → BPF → cilium_wg0 → WG encap → eth0 → underlay → eth0 → ...
```

Each Cilium agent generates a WireGuard keypair, publishes its public key via the CiliumNode CR. Each agent reads the others' public keys, configures `cilium_wg0` with peers, one per remote node. Cilium's BPF program redirects Pod-to-Pod traffic that crosses nodes into `cilium_wg0`, which encrypts and forwards.

Configuration:

```yaml
encryption:
  enabled: true
  type: wireguard
```

### 17.3 Tradeoffs

- **Overhead**: WG adds 60-80 bytes per packet (depends on inner packet size). MTU must be reduced or path-MTU set carefully.
- **Throughput**: kernel WireGuard is fast (multi-gigabit per core) but still slower than no encryption. Expect 70-85% of unencrypted throughput on modern hardware.
- **CPU**: encryption is per-packet CPU work. On high-bandwidth nodes, expect a measurable increase.
- **Compatibility**: WG must be present in the kernel (5.6+ has it natively). Older kernels need DKMS.
- **Doesn't encrypt to outside**: only node-to-node. Egress from the cluster to the internet is in the clear unless you do something else (Istio mTLS to external, application-level TLS).

Alternative is `encryption.type: ipsec`. IPsec is older, more complex to configure, but works with cryptographic hardware offload on more NICs and has FIPS-validated implementations available. Most new deployments pick WireGuard.

---

## 18. Performance Characteristics

### 18.1 Per-Operation Numbers (Order of Magnitude)

These are not benchmarks; they are the right number of zeros.

| Operation | Cilium | iptables kube-proxy |
|-----------|--------|---------------------|
| ClusterIP packet (in established flow) | < 1 µs additional | 5-10 µs (rule walk + conntrack) |
| ClusterIP `connect()` resolution | ~5 µs (one connect-time BPF) | n/a (per packet) |
| NodePort packet | ~3 µs (TC + map lookup) | 10-20 µs |
| Policy evaluation on receive | < 1 µs (one map lookup) | n/a (iptables NetworkPolicy is similar to kube-proxy) |
| Service update (program backends) | milliseconds per service | ~minutes at 5000 services |
| New endpoint installed | milliseconds (one map write per affected ep) | seconds (iptables-restore) |

### 18.2 At Scale (5000 Nodes, 50,000 Services)

The scaling difference is qualitative, not quantitative:

- **kube-proxy iptables sync time**: ~5-15 minutes per change. The sync is throttled and the cluster is effectively unable to converge.
- **kube-proxy IPVS sync time**: ~30 seconds. Much better but conntrack table is still hot.
- **Cilium sync time**: milliseconds. Each service change is a per-node `BPF_MAP_UPDATE_ELEM` call.

The internal state at this scale:

- 50,000 services × 3 backends average = 150,000 LB map entries. At 8-byte key + 16-byte value, that's ~4 MB of kernel memory per node.
- IPCache: 1 entry per Pod IP × 500,000 Pods = 500,000 entries × ~24 bytes = ~12 MB per node.
- Identities: ~10,000 (workload identities) + reserved = some KB.

All comfortably within budget.

### 18.3 Latency Distribution

Same-node Pod-to-Pod via Service ClusterIP, p99 round-trip time:

- iptables kube-proxy: ~50 µs (varies with rule count)
- IPVS: ~30 µs
- Cilium socket LB (without sock-redirect): ~25 µs
- Cilium with sock-redirect (sockmap fast-path): ~15 µs (bypassing IP stack)

Cross-node, the encap or routing path dominates and the differences flatten out — but you still save the netfilter walk on each side.

### 18.4 Throughput Per Core

On a modern x86 server (Sapphire Rapids), a single core can push:

- Plain IP forwarding: ~10 Gbps
- iptables with 500 rules: ~6 Gbps
- iptables with 5000 rules: ~1.5 Gbps (or less if MASQ is involved)
- Cilium TC ingress + map lookup: ~9 Gbps
- Cilium XDP LB (for LoadBalancer): ~15-30 Gbps (close to NIC line rate, before the kernel allocates skbs)

### 18.5 Memory

Most of Cilium's memory in a steady state is in its BPF maps (kernel) and the agent's Go heap (userspace). Per-node memory at 100 endpoints, 1000 services:

- BPF maps: ~50 MB
- Agent Go heap: ~200-500 MB
- Envoy DaemonSet (if L7 enabled): ~100-300 MB

At 5000-node, 50000-service scale, agent memory rises to 1-2 GB per node, primarily because of identity caches and the apiserver watch state.

---

## 19. Verifier-Bounded Complexity: Tail Calls and Map-in-Map

### 19.1 The Problem

A NetworkPolicy evaluation in Cilium has multiple steps: check L3, check L4, check L7 redirect, check NAT, etc. Doing all of that in a single BPF program would blow the 1M-instruction verifier budget and the 512-byte stack.

### 19.2 Tail Calls

`bpf_tail_call(ctx, &prog_array, idx)` *replaces* the current program with another, in-place, with no stack growth. The verifier treats each program in the chain independently. Tail calls are limited to 33 deep (kernel constant) to bound the call chain.

```c
struct {
    __uint(type, BPF_MAP_TYPE_PROG_ARRAY);
    __uint(max_entries, 16);
    __type(key, __u32);
    __type(value, __u32);
} cilium_calls SEC(".maps");

SEC("tc/cls/from-container")
int cil_from_container(struct __sk_buff *skb) {
    /* L3/L4 parsing, identity lookup */
    if (need_policy_check) {
        bpf_tail_call(skb, &cilium_calls, CILIUM_CALL_POLICY);
        return TC_ACT_SHOT; /* unreachable on successful tail_call */
    }
    /* ... */
}

SEC("tc/cls/policy")
int cil_policy(struct __sk_buff *skb) {
    /* policy map lookup, L7 redirect logic */
}
```

The Cilium agent installs each tail-call target at the correct index in `cilium_calls` at program-load time. The chain typically goes:

```
from-container → policy → ipv4 → encap → to-overlay
```

Each step is its own verifier-bounded program, but they execute as one logical pipeline.

### 19.3 Map-in-Map

For hierarchical state (e.g., per-endpoint policy maps), you need a map whose values are themselves maps. `BPF_MAP_TYPE_HASH_OF_MAPS` does this:

```c
struct {
    __uint(type, BPF_MAP_TYPE_HASH_OF_MAPS);
    __type(key, __u32);   /* endpoint id */
    __type(value, __u32); /* fd of inner policy map */
    __uint(max_entries, 65536);
} cilium_policy_outer SEC(".maps");

/* From BPF: */
void *inner = bpf_map_lookup_elem(&cilium_policy_outer, &endpoint_id);
if (!inner)
    return DEFAULT_DENY;
struct policy_entry *e = bpf_map_lookup_elem(inner, &policy_key);
```

This lets the agent create a per-endpoint policy map without recompiling programs. The outer map maps endpoint IDs to inner-map fds; the inner map holds the actual policy entries.

---

## 20. XDP: eXpress Data Path

### 20.1 What XDP Is

XDP is *the lowest BPF hook on the receive path*. It fires from the NIC driver's `napi_poll` callback, before the kernel has allocated an `sk_buff`. You see the raw packet (just a pointer + length) and can:

- `XDP_PASS`: let the kernel proceed (alloc skb, normal path)
- `XDP_DROP`: drop the packet (zero memory pressure)
- `XDP_TX`: bounce it back out the same interface
- `XDP_REDIRECT`: send it to another netdev or AF_XDP socket
- `XDP_ABORTED`: signal error (also drops)

The verdict returns to the driver. No skb is built, no kernel network stack runs, no qdisc, no netfilter.

### 20.2 XDP Modes

- **XDP-native** (best): the driver supports XDP directly. Programs run in the NAPI poll loop on the RX queue. Tens of millions of pps per core.
- **XDP-offload**: the program is offloaded into the NIC firmware (Netronome Agilio; rare). Programs run in NIC silicon.
- **XDP-generic** (worst): generic kernel hook after the driver has built the skb. Slower than XDP-native, still faster than TC. Used when the driver doesn't support XDP-native.

### 20.3 Cilium's Use of XDP

Cilium uses XDP primarily for **LoadBalancer Service** packet processing on dedicated LB nodes (the `kube-proxy-replacement: strict` + dedicated-LB topology). The XDP program:

1. Parses the incoming packet.
2. Looks up the VIP in the BPF LB map.
3. Picks a backend (Maglev).
4. DNATs the packet.
5. Either:
   - `XDP_REDIRECT` to the egress NIC (if the backend is reachable directly).
   - `XDP_TX` back out the same NIC with rewritten dst MAC.
   - `XDP_PASS` if the backend is local (let the kernel handle the rest).

This is how Cilium claims tens-of-millions-of-pps LB throughput per node. It's also where, on appropriate NICs, you can do DDoS filtering at line rate.

### 20.4 XDP Limitations

- Not every NIC driver supports XDP-native (most mainstream ones do now).
- You cannot do L7 in XDP (no skb, no socket).
- Programs have a tighter context — `xdp_md` is just data, data_end, ingress_ifindex.
- The packet may not be contiguous; you may need `bpf_xdp_adjust_head` and head/meta manipulation for encap.

For *Pod-to-Pod* traffic Cilium uses TC, not XDP, because TC has access to skb metadata (sk_buff features, qdisc semantics) and works on the per-Pod veth interfaces.

---

## 21. Debugging eBPF and Cilium

### 21.1 bpftool — The Swiss Army Knife

`bpftool` is part of the kernel source tree (`tools/bpf/bpftool/`), shipped in most distros (`bpftool` package on Debian, `bpftool` from `bpf-tools` on RHEL). What you do with it:

```bash
# List all loaded programs
$ bpftool prog list
17: tracepoint  name handle__sched  tag a39b6f6f9d  gpl
        loaded_at 2024-10-07T12:33:11+0000  uid 0
        xlated 472B  jited 281B  memlock 4096B  map_ids 4,5
        btf_id 12
1234: cgroup_skb  name cil_sock4_conn  tag 9ef58a3...  gpl
        ...

# Dump a program's BPF instructions
$ bpftool prog dump xlated id 1234

# Dump the JITed (native) code
$ bpftool prog dump jited id 1234

# List maps
$ bpftool map list
17: hash  name cilium_lb4_servic  flags 0x0
        key 16B  value 12B  max_entries 65536
        memlock 1175552B
        btf_id 12

# Dump a map
$ bpftool map dump id 17

# Look up a specific key
$ bpftool map lookup id 17 key hex 0a 60 00 01 00 50 00 00 06 00 00 00

# Update a key
$ bpftool map update id 17 key ... value ...

# List BTF
$ bpftool btf list

# Show what's pinned in /sys/fs/bpf
$ bpftool prog show pinned /sys/fs/bpf/tc/globals/cil_from_container
```

### 21.2 cilium-dbg — Cilium-specific Introspection

Inside the agent pod:

```bash
$ kubectl exec -n kube-system cilium-xxxxx -- cilium-dbg status --verbose
KVStore:                Ok   Disabled
Kubernetes:             Ok   1.31 (v1.31.0)
KubeProxyReplacement:   True
        DirectRouting   Mode:    Native
        XDP Acceleration: Native
Cilium:                Ok   1.16.0
NodeMonitor:            Listening for events on 16 CPUs with 64x4096 of shared memory
Cilium health daemon:   Ok
IPAM:                   IPv4: 12/256 allocated from default
...

# What endpoints are on this node?
$ cilium-dbg endpoint list
ENDPOINT   POLICY (ingress)   POLICY (egress)   IDENTITY   LABELS         IPv4
1234       Enabled            Enabled           5678       k8s:app=fe     10.244.1.42
2345       Disabled           Disabled          7890       k8s:app=be     10.244.1.43

# What's in the policy map for endpoint 1234?
$ cilium-dbg bpf policy get 1234
POLICY   DIRECTION   LABELS (source:dest)        PORT/PROTO
Allow    Ingress     ID: 5678                    8080/TCP
...

# Connection tracking
$ cilium-dbg bpf ct list global

# Service load balancing maps
$ cilium-dbg bpf lb list

# IPCache
$ cilium-dbg bpf ipcache list

# Live event monitor (this is what Hubble consumes)
$ cilium-dbg monitor --type policy-verdict
```

### 21.3 hubble observe — Live Flow View

```bash
$ hubble observe --follow
$ hubble observe --since 1h --verdict DROPPED --pod default/frontend-xxx
$ hubble observe --protocol http --http-status 500
$ hubble observe --to-fqdn '*.stripe.com'
$ hubble observe --print-raw-filters     # show the underlying gRPC filter
```

For service maps:

```bash
$ hubble ui                # opens the UI (requires port-forward in some setups)
```

### 21.4 bpftrace One-Liners

When Cilium's tools aren't enough, drop into `bpftrace`:

```bash
# Trace every kfree_skb (where the kernel drops packets) with stack
# Useful when packets are getting dropped silently
$ bpftrace -e 'kprobe:kfree_skb { @[kstack] = count(); }'

# Count syscalls per process
$ bpftrace -e 'tracepoint:raw_syscalls:sys_enter { @[comm] = count(); }'

# Trace tcp connections
$ bpftrace -e 'kprobe:tcp_v4_connect { printf("%s pid=%d connecting\n", comm, pid); }'

# Histogram of map lookup latency
$ bpftrace -e 'kprobe:htab_map_lookup_elem { @start[tid] = nsecs; } 
              kretprobe:htab_map_lookup_elem /@start[tid]/ { 
                @us = hist((nsecs - @start[tid]) / 1000); 
                delete(@start[tid]); 
              }'
```

### 21.5 perf for BPF

```bash
# Find which BPF program is using CPU
$ perf top -e cycles --sort comm,dso | grep bpf
   8.42%  swapper          [k] bpf_prog_xxxxx_cil_from_container
```

### 21.6 cilium connectivity test

When you're not sure what's broken, run the canary:

```bash
$ cilium connectivity test
ℹ️  Monitor aggregation detected, will skip some flow validation steps
[=] [shop] Setting up echo and client services... done
[=] [shop] Running tests...
... 88 tests passed ...
```

This creates a set of test Pods, runs traffic across them, and verifies that connectivity, NetworkPolicy, and L7 enforcement all behave correctly. Run it after every Cilium install or upgrade.

### 21.7 The Drop Notifications Map

The single most useful debugging artifact: `cilium-dbg monitor -t drop`. Every dropped packet emits an event with the reason code. Common ones:

- `Reason: Policy denied` — explicit deny rule fired or no allow rule matched.
- `Reason: Invalid source IP` — IP not in IPCache; usually identity sync lag.
- `Reason: Stale or unroutable IP` — destination not known.
- `Reason: No mapping for NAT masquerade` — encap issue.

```bash
$ cilium-dbg monitor -t drop --hex
xx drop (Policy denied) flow 0x12345678 to endpoint 0, identity 12345->67890: 10.244.1.42:54321 -> 10.244.2.17:9090 tcp SYN
```

---

## 22. Cilium Configuration Knobs

The full list is hundreds of entries; here are the ones that actually matter for an operator.

### 22.1 Routing and Datapath

```yaml
kubeProxyReplacement: true          # full replacement
# kubeProxyReplacement: false       # do nothing, keep kube-proxy
# (older values "strict"/"partial"/"probe" — replaced by true/false in 1.14+)

routingMode: tunnel                 # or "native"
tunnelProtocol: vxlan               # or "geneve"
autoDirectNodeRoutes: false         # in native mode, auto-program node-to-node routes

ipv4NativeRoutingCIDR: 10.244.0.0/16  # in native mode, what's "in-cluster"
enableIPv6: false

bpf:
  masquerade: true                  # BPF-based masquerade instead of iptables MASQUERADE
  hostLegacyRouting: false          # use BPF host routing (sockmap), not iptables
  lbExternalClusterIP: false        # whether external clients can hit ClusterIPs (default no)
  lbMode: snat                      # "snat" or "dsr" (direct server return)

ipam:
  mode: cluster-pool                # or "kubernetes", "eni", "azure", "crd"
  operator:
    clusterPoolIPv4PodCIDRList: ["10.244.0.0/16"]
    clusterPoolIPv4MaskSize: 24
```

### 22.2 Service LB Algorithm

```yaml
loadBalancer:
  algorithm: maglev                 # or "random"
  mode: snat                        # or "dsr" or "hybrid"
  acceleration: native              # XDP acceleration ("native"/"generic"/"disabled")
maglev:
  tableSize: 16381                  # prime number; larger = smoother distribution
```

### 22.3 Encryption

```yaml
encryption:
  enabled: true
  type: wireguard                   # or "ipsec"
  nodeEncryption: false             # encrypt host-to-host as well as pod-to-pod
```

### 22.4 Hubble

```yaml
hubble:
  enabled: true
  relay:
    enabled: true
  ui:
    enabled: true
  metrics:
    enabled:
      - dns
      - drop
      - tcp
      - flow
      - port-distribution
      - icmp
      - http
```

### 22.5 Operator

```yaml
operator:
  replicas: 2
  rollOutPods: true                 # on config change, roll the operator
```

### 22.6 Useful Diagnostic Flags

```yaml
debug:
  enabled: false                    # increase log verbosity
debugVerbose: ""                    # comma-separated: "flow,kvstore,envoy,policy"
monitor:
  enabled: true                     # the Hubble event stream
prometheus:
  enabled: true                     # cilium-agent metrics on :9962
```

---

## 23. Real-World Rollouts

### 23.1 Greenfield: Cilium from Day 1

By far the easiest path. Provision the cluster (kubeadm, kOps, EKS, GKE) with no CNI (kubeadm: `--skip-phases=addon/kube-proxy`; EKS: deploy without the AWS CNI; GKE: use Dataplane v2 which is already Cilium).

```bash
$ cilium install --version 1.16.0 \
    --set kubeProxyReplacement=true \
    --set k8sServiceHost=$API_SERVER_IP \
    --set k8sServicePort=6443
$ cilium status --wait
$ cilium connectivity test
```

That's it. No kube-proxy, no Calico/Flannel, no AWS-CNI.

### 23.2 Migration from Calico or Flannel

Harder. You cannot run two CNIs simultaneously on the same Pod (CNI is a per-Pod choice). Approaches:

- **Node-by-node drain and reinstall.** Cordon a node, drain Pods, uninstall old CNI components from the node (carefully — keep Calico's etcd or Typha running for nodes still using it), install Cilium, uncordon. As Pods are recreated by their controllers, they land on Cilium nodes with Cilium-managed networking. Test thoroughly between batches.
- **Cilium per-node config (CiliumNodeConfig).** Cilium 1.13+ supports per-node config that allows running both CNIs simultaneously on different nodes during migration.
- **Blue-green cluster.** Stand up a new cluster with Cilium, migrate workloads via cross-cluster service discovery (Cilium ClusterMesh or a service mesh).

In all cases: test connectivity, test policies (vanilla NetworkPolicy will still work but you'll lose Calico-specific features like GlobalNetworkPolicy until you port them to CiliumClusterwideNetworkPolicy).

### 23.3 Managed Cilium

- **GKE Dataplane v2**: enable at cluster creation. GKE manages the Cilium version. You cannot tweak everything; the managed offering has guardrails.
- **EKS with Cilium**: AWS supports Cilium as a CNI option via the EKS add-on. Or self-managed: uninstall the AWS VPC CNI add-on (or run Cilium in chained mode on top of AWS VPC CNI).
- **AKS Azure CNI Powered by Cilium**: enabled at AKS create. Limited tunables.

In all managed cases, the L7 proxy, Hubble UI, and Tetragon are optional add-ons you opt into.

### 23.4 Upgrades

Cilium upgrades within a minor version (1.16.0 → 1.16.5) are non-disruptive: the operator rolls each cilium-agent Pod, datapath programs are reattached during pod restart with minimal traffic interruption (existing connections continue; new connections may briefly see drops as programs reload).

Cross-minor upgrades (1.15 → 1.16) need more care: read the release notes for datapath changes, check that the new version supports your kernel, run `cilium connectivity test` after each step. Cilium has `cilium upgrade` which orchestrates this.

---

## 24. Pitfalls

The list of mistakes you (and we) have made in production. Most are mis-configurations of an otherwise excellent system.

1. **`kubeProxyReplacement: false` / `Disabled` in the chart.** You think you have Cilium replacing kube-proxy. You don't. kube-proxy is still running, double-DNATing, and you have all the iptables-scale problems plus the Cilium overhead. Set it to `true` (or `strict` in older versions) and uninstall kube-proxy. Verify with `cilium status | grep KubeProxyReplacement`.

2. **Mixed tunnel + native routing config.** `routingMode: native` with `tunnel: vxlan` is contradictory and may silently produce nodes that can't reach each other (one side encaps, the other doesn't). Pick one consistently.

3. **Missing BPF filesystem mount.** `/sys/fs/bpf` must be a `bpf`-type fs mount for map pinning. On some distros this isn't set up; Cilium will attempt to mount it but if the host has constraints (read-only root, SELinux denial) it will fail. `mount | grep bpf` to verify.

4. **No BTF on the host kernel.** Older RHEL 7 / CentOS 7 derivatives don't have `CONFIG_DEBUG_INFO_BTF=y` and no external BTF. Cilium will refuse to load some programs or degrade features. Move to a distro with BTF (RHEL 8.6+, Ubuntu 20.04+ HWE, COS, Bottlerocket, Talos).

5. **Tight CNP without DNS allow.** You write:
   ```yaml
   egress:
     - toEndpoints: [{ matchLabels: { app: api } }]
   ```
   ...and forget to allow DNS. Now Pods can't resolve anything, including the API they're trying to reach. Always include a DNS-egress rule (often via a shared `default-allow-dns` policy).

6. **L7 policy on every Service.** L7 forces a hairpin through Envoy, which adds 100-500 µs per request. For latency-sensitive services (auth checks, rate-limiting in front of expensive paths), this is fine. For chatty internal RPC, it's a regression. Apply L7 *selectively*.

7. **Encryption with too-small MTU.** WireGuard adds ~80 bytes. If you've set `mtu: 1500` and your underlay actually supports 1500, the encrypted packet (1580 bytes) will fragment or get dropped. Either reduce the Cilium MTU (`MTU: 1420`) or ensure the underlay supports jumbo frames.

8. **Identity churn from label changes.** Every label change on a Pod can trigger a new identity allocation if the label is in the relevant set. A workload that constantly mutates Pod labels (e.g., a controller that writes a heartbeat label) will explode the identity space. Use a `LABELS_REGEXP` filter or pick label conventions that exclude noisy labels.

9. **Upgrades across major versions without datapath drain.** When the BPF datapath changes substantially (rare but happens), upgrading a node in place can leave in-flight connections with mismatched state. Drain the node, upgrade, uncordon. `cilium upgrade` does this for you; doing the helm upgrade manually does not.

10. **Running kube-proxy "for safety" alongside Cilium with `kubeProxyReplacement: true`.** kube-proxy programs iptables rules; Cilium does socket LB. Both apply to the same packet. You get double load balancing, sometimes asymmetric (kube-proxy picks backend X, Cilium picks backend Y, the conntrack table tracks one, the socket goes to the other). Symptoms range from "occasional weird drops" to "no traffic at all." Pick one.

11. **HostNetwork Pods don't get Cilium identity.** A Pod with `hostNetwork: true` uses the host's network namespace and bypasses the Pod-veth datapath. Its connections are tagged with the `host` identity, not the Pod's workload identity. Network policies you write against the Pod's labels won't apply to its traffic. This is correct behavior; it surprises people.

12. **External clients hitting NodePort with `externalTrafficPolicy: Local` and no local backend.** Cilium honors `externalTrafficPolicy: Local`. A node with no backend Pods will refuse traffic for that NodePort. Cloud LBs are supposed to health-check and skip those nodes, but if your LB is misconfigured (or you're using `hostNetwork: true` for the proxy), packets vanish. `cilium-dbg service list` shows which nodes have backends.

13. **`enable-bpf-masquerade: false` with a misconfigured masquerade-source-range.** Without BPF masq, you fall back to iptables MASQUERADE, which is fine. But if you also set the `ip-masq-agent`-style noMasqueradeCIDRs incorrectly, you can end up with Pod IPs leaking onto the external network. Use `cilium-dbg status` and `iptables -t nat -S` to verify the masq policy.

14. **CiliumClusterwideNetworkPolicy default-deny without an `entities: cluster` allow.** A CCNP with `default-deny` matched at the cluster scope, no explicit allow for cluster-internal traffic, breaks intra-cluster DNS, health checks, and kubelet→apiserver. Always pair default-deny with an `entities: cluster` allow for system traffic (or use `BaselineAdminNetworkPolicy` semantics for that).

15. **Hubble logs eating disk on busy clusters.** Hubble's ringbuf is sized per-CPU (default 4MB × N CPUs). On a 96-core node that's ~400 MB pinned. Hubble Relay aggregates everything. If you've turned on persistent logging, the disk fills fast. Sample (`hubble.metrics.flowSampleRate: 0.1`) for production.

16. **Tetragon with too-broad `kprobes`.** A TracingPolicy that hooks `security_file_open` for *every* file open generates millions of events per second on a node. Even with in-kernel filtering, the userspace event consumer chokes. Always include tight selectors.

17. **WireGuard key rotation during a node restart leaves stale peers.** The cilium-agent generates a new WG key at startup if the old one isn't on persistent state. If the node restarts and gets a new key, other nodes don't know about it for some seconds, dropping cross-node traffic. Persist the key file (`enable-encryption-strict-mode` and check the docs version).

18. **Forgetting that ClusterMesh requires unique cluster IDs.** Each cluster in a mesh needs a unique numeric ID (`cluster.id`). If two clusters share an ID, identity allocation collides catastrophically. `cluster.id: 1` and `cluster.id: 2` — don't both pick 1.

19. **Allowing `from: world` instead of specific external CIDRs.** `world` is the catch-all internet identity. Writing `fromEntities: ["world"]` allows traffic from the entire internet — convenient for ingress, dangerous everywhere else. Use specific CIDRs for known external services.

20. **Running Cilium without enough kernel memory budget.** BPF maps are kernel memory. With 50k services × 3 backends + 100k IPCache entries + 50k policy entries × N endpoints, you can easily use 1 GB of kernel memory for BPF maps alone. On small nodes (4 GB RAM) this matters; on a 256 GB node it doesn't. Plan accordingly and monitor `cilium-dbg map list` for memlock totals.

---

## 25. TL;DR

- **iptables and kube-proxy don't scale.** Linear rule matching, full-table reloads, conntrack explosion. At 5000 nodes / 50000 services they take minutes to converge, drop packets during reloads, and consume measurable per-packet CPU. Cilium replaces them.

- **eBPF is the new kernel programming model.** Userspace compiles C → BPF bytecode → kernel verifier → JIT → native code attached to a hook. Hooks include XDP (driver), TC (qdisc), cgroup (socket-layer), kprobe / fentry / LSM (tracing). Maps (`hash`, `lru_hash`, `lpm_trie`, `ringbuf`, `prog_array`) are the shared kernel↔userspace data structures.

- **The verifier is the cost of safety.** It proves termination, bounds memory accesses, type-checks pointers. Programs that "look fine" can be rejected because it can't prove them safe. Patterns: bounds-check every pointer deref, use `bpf_loop()` for variable iteration, tail-call to split large programs.

- **BTF + CO-RE means write-once-run-anywhere.** Kernel BTF describes its own type layout; the program's BTF describes its expectations; the loader rewrites field offsets at load time. One .o file works across kernel versions and distros.

- **Cilium = cilium-agent (DaemonSet) + cilium-operator (Deployment) + cilium-envoy (DaemonSet, for L7).** ConfigMap-driven. CRDs: CiliumEndpoint, CiliumIdentity, CiliumNode, CiliumNetworkPolicy, CiliumClusterwideNetworkPolicy, CiliumEgressGatewayPolicy, CiliumLoadBalancerIPPool, CiliumBGPPeeringPolicy.

- **Identity, not IP.** Each Pod's labels map to a numeric identity. Pods with the same labels share an identity. Policy is keyed by `(src_identity, dst_identity, port, proto)` → O(1) lookup. The IPCache (`cilium_ipcache`, LPM trie) maps every cluster IP to its identity for fast at-packet lookup.

- **Datapath modes**: tunnel (VXLAN/Geneve, encap, any underlay), native routing (no encap, needs routable Pod CIDRs, often BGP), AWS ENI (Pod IPs are VPC IPs).

- **kube-proxy replacement happens at `connect(2)` via the cgroup `connect4`/`connect6` BPF program.** ClusterIP is rewritten to a backend Pod IP in the socket address *before any packet exists*. Zero iptables rules, zero conntrack for ClusterIP. Per-packet cost: 0. NodePort/LoadBalancer still need TC ingress (and BPF conntrack) for unsolicited inbound.

- **Other cgroup hooks**: `sendmsg4/6` for UDP per-message LB; `getpeername4/6` for application consistency; `sock_ops` + `sk_msg` for same-node sockmap fast-path that bypasses the entire IP stack.

- **Maglev hashing** for consistent backend selection: a backend going away moves only its own flows, not 1/M of everything.

- **NetworkPolicy is realized as per-endpoint BPF policy maps.** L7 policies redirect to a per-node Envoy DaemonSet via TPROXY. DNS-aware egress (`toFQDNs`) is enforced by the DNS proxy learning IPs from DNS responses and adding them to the allow-set with TTL.

- **Hubble = observability built into the BPF datapath.** Every BPF program emits events into a ringbuf; the agent monitor decodes and tags them with identities; hubble-relay aggregates cluster-wide. No sidecars. `hubble observe`, Hubble UI service graph, Prometheus metrics that are L7- and identity-aware.

- **Tetragon = runtime security via tracing BPF.** TracingPolicy CRD declares "hook these kprobes/tracepoints/LSM points; if these args match, post / kill / override." Kill happens in BPF via `bpf_send_signal`. Compared to Falco: in-kernel enforcement vs userspace rule eval.

- **Cilium Service Mesh = sidecar-less.** L4 mTLS via per-node WireGuard. L7 routing/policy via per-node Envoy DaemonSet (one Envoy per node, not per Pod). Gateway API for ingress. Compared to Istio: lower memory cost, simpler startup ordering, but per-node failure blast radius.

- **ClusterMesh = multi-cluster Cilium.** Shared identity allocation via clustermesh-apiserver (TLS etcd). Global Services (`service.cilium.io/global: "true"`). Cross-cluster identity-based policy.

- **WireGuard transparent encryption.** Per-node WG mesh; BPF redirects pod-to-pod traffic into `cilium_wg0` on egress. ~80-byte overhead, 70-85% throughput of unencrypted on modern hardware. Use `encryption.type: wireguard`.

- **Performance**: ClusterIP packet ≈ 0 cost in steady state (one connect-time BPF on first packet). NodePort ≈ TC + map lookup. Policy ≈ one map lookup. At 5000-node / 50000-service: kube-proxy sync = minutes; Cilium sync = milliseconds.

- **Tail calls + map-in-map.** BPF programs split work across a chain of `bpf_tail_call`s using a `BPF_MAP_TYPE_PROG_ARRAY`. Per-endpoint state stored in `BPF_MAP_TYPE_HASH_OF_MAPS`.

- **XDP** runs in the driver, before skb allocation. Used for LoadBalancer Service at line rate. Per-core throughput in the tens of Mpps on appropriate NICs.

- **Debugging**: `bpftool` (kernel-side), `cilium-dbg` (Cilium-side), `hubble observe` (flow-side), `bpftrace` (everything-side), `cilium-dbg monitor -t drop` (the most useful single command), `cilium connectivity test` (the validation gate).

- **Config knobs you'll actually touch**: `kubeProxyReplacement`, `routingMode`, `tunnelProtocol`, `ipam.mode`, `bpf.masquerade`, `bpf.hostLegacyRouting`, `loadBalancer.algorithm`, `encryption.enabled`, `hubble.*`.

- **Rollouts**: greenfield is trivial. Migration from Calico/Flannel is node-by-node drain-and-reinstall. Managed: GKE Dataplane v2 (always Cilium), EKS Cilium add-on, AKS Azure CNI Powered by Cilium.

- **Pitfalls**: leaving `kubeProxyReplacement: false`, mixing tunnel and native, missing BPF fs mount, no kernel BTF, CNP without DNS allow, blanket L7 on every Service, MTU too small for encryption, identity churn from label spam, running Cilium and kube-proxy in parallel, default-deny CCNPs without cluster-internal allow.

The throughline: **put the smart code in the kernel, once, and let every other concern — load balancing, segmentation, observability, runtime security, encryption, multi-cluster — read out of the same maps.** That is what eBPF makes possible and what Cilium has made operational. iptables had a 25-year run. Its successor is here.
