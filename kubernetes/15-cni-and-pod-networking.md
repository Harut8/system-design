# CNI and Pod Networking

How a Pod actually gets an IP, why every Pod can reach every other Pod without NAT, and what the CNI plugin is doing for the 30–200 ms between `RunPodSandbox` and the first packet. This chapter is the pod-to-pod layer of Kubernetes networking. Chapter 14 (Services + kube-proxy) sits *on top* of it (Service VIPs route to the pod IPs allocated here). Chapter 16 (Cilium / eBPF) is *one specific implementation* of what this chapter abstracts. Chapter 20 (NetworkPolicy) is the segmentation overlay that most CNIs enforce as a side responsibility.

If you can recite the four-rule Kubernetes networking model from memory, sketch the byte layout of a VXLAN encapsulated frame, write a minimal `conflist` from scratch, and explain what happens when MTU is wrong by exactly 8 bytes, you can debug 95% of production networking incidents. This chapter aims for that.

---

## Table of Contents

1. [The Kubernetes Networking Model: Four Rules](#1-the-kubernetes-networking-model-four-rules)
2. [Where CNI Sits in the Pod-Bringup Path](#2-where-cni-sits-in-the-pod-bringup-path)
3. [The CNI Specification: An Executable Interface](#3-the-cni-specification-an-executable-interface)
4. [Plugin Chains, Lists, and the `conflist` File](#4-plugin-chains-lists-and-the-conflist-file)
5. [Plugin Categories: main, IPAM, meta](#5-plugin-categories-main-ipam-meta)
6. [Anatomy of a CNI ADD: The veth + Bridge Model](#6-anatomy-of-a-cni-add-the-veth--bridge-model)
7. [IPAM Models: host-local, calico-ipam, cloud-native, dhcp](#7-ipam-models-host-local-calico-ipam-cloud-native-dhcp)
8. [Overlays: VXLAN and Geneve](#8-overlays-vxlan-and-geneve)
9. [Underlays: BGP and Native Routing](#9-underlays-bgp-and-native-routing)
10. [Hybrid Dataplanes: IPinIP, VXLAN-over-BGP, host-gw](#10-hybrid-dataplanes-ipinip-vxlan-over-bgp-host-gw)
11. [Calico Deep Look](#11-calico-deep-look)
12. [Cilium Overview (the eBPF Path)](#12-cilium-overview-the-ebpf-path)
13. [Flannel: The Minimalist](#13-flannel-the-minimalist)
14. [Weave Net: Mesh and Sunset](#14-weave-net-mesh-and-sunset)
15. [AWS VPC CNI: Native VPC IPs](#15-aws-vpc-cni-native-vpc-ips)
16. [Azure CNI: VNET-Native and Overlay](#16-azure-cni-vnet-native-and-overlay)
17. [GKE Dataplane v2 and Other Cloud CNIs](#17-gke-dataplane-v2-and-other-cloud-cnis)
18. [Writing a CNI Plugin From Scratch](#18-writing-a-cni-plugin-from-scratch)
19. [MTU Math and the 8-Byte Outage](#19-mtu-math-and-the-8-byte-outage)
20. [Dual-Stack IPv4/IPv6](#20-dual-stack-ipv4ipv6)
21. [Egress Patterns and SNAT](#21-egress-patterns-and-snat)
22. [Cross-AZ Networking Costs](#22-cross-az-networking-costs)
23. [NetworkPolicy Enforcement (Forward Reference)](#23-networkpolicy-enforcement-forward-reference)
24. [CNI Version Compatibility and the Runtime/CNI Contract](#24-cni-version-compatibility-and-the-runtimecni-contract)
25. [Pod IP Lifecycle and Idempotency](#25-pod-ip-lifecycle-and-idempotency)
26. [Static IPs, Pinned IPs, and Pod Annotations](#26-static-ips-pinned-ips-and-pod-annotations)
27. [Multus: Multiple Interfaces per Pod](#27-multus-multiple-interfaces-per-pod)
28. [SR-IOV and Kernel-Bypass Networking](#28-sr-iov-and-kernel-bypass-networking)
29. [Observability: Metrics, Counters, conntrack](#29-observability-metrics-counters-conntrack)
30. [CNI Failure Modes](#30-cni-failure-modes)
31. [Choosing a CNI: A Decision Table](#31-choosing-a-cni-a-decision-table)
32. [Pitfalls](#32-pitfalls)
33. [TL;DR](#33-tldr)

---

## 1. The Kubernetes Networking Model: Four Rules

Kubernetes does not define *how* pods talk; it defines *what must be true* once they do. The core specification (`kubernetes/design-proposals-archive/network/networking.md`, then frozen into the API definition) is four rules that every CNI plugin must satisfy. Memorize these. They are the entire contract.

```
┌───────────────────────────────────────────────────────────────────────────┐
│  RULE 1.  Every Pod gets a unique cluster-wide IP.                        │
│           No two Pods anywhere in the cluster share an IP.                │
│                                                                           │
│  RULE 2.  All Pods can communicate with all other Pods                    │
│           WITHOUT NAT, in any direction.                                  │
│                                                                           │
│  RULE 3.  All Nodes can communicate with all Pods (and vice versa)        │
│           WITHOUT NAT.                                                    │
│                                                                           │
│  RULE 4.  The IP a Pod sees itself as (via `ip addr` inside the pod)      │
│           is the IP other Pods see it as.                                 │
│           No NAT means no rewriting on the source side either.            │
└───────────────────────────────────────────────────────────────────────────┘
```

These are **constraints, not implementations**. They forbid the Docker-classic model (where every container is NAT'd behind the host's IP) and the Mesos-classic model (where ports must be unique per host). They make every Pod look like a first-class network citizen — like a tiny VM with its own IP, port space, and routing table.

Consequences that fall straight out of the four rules:

- **A Service is not necessary for pod-to-pod traffic.** You can `curl 10.244.5.27:8080` from any pod and reach pod 10.244.5.27 directly. Services are a *stable abstraction* over a *changing set of pod IPs*, layered on top of the four-rule plane — not a routing prerequisite.
- **Port conflicts within a pod are real; across pods they don't exist.** Two pods can both bind to port 80 because each has its own network namespace and its own IP. This is why "the container ports parameter" in the PodSpec is documentation, not enforcement (ch 11).
- **The cluster is a single flat L3 network from the pods' perspective.** Even when the underlying nodes are spread across AZs or datacenters, every pod is one IP hop away. The CNI hides the topology.
- **Source IP is preserved.** A web server logging `X-Forwarded-For` doesn't need to fight kube-proxy unless a Service or LoadBalancer is in the middle. Inside the pod-to-pod plane, the source IP *is* the pod IP.
- **Identity == IP, but only briefly.** Pod IPs are recycled aggressively — a pod restart can take seconds, and the IP can be reused minutes later. Services + DNS exist because pod IPs are not stable identities.

```
                     ┌──────────────────────────────────────────┐
                     │  Pod A on Node 1                         │
                     │  IP = 10.244.1.42                        │
                     │                                          │
                     │   $ ip addr show eth0                    │
                     │   eth0: 10.244.1.42/32                   │
                     │                                          │
                     │   $ curl 10.244.7.5:8080                 │   ← rule 2
                     │                                          │
                     └──────────────────────────────────────────┘
                                       │
                                       │ (no NAT, no rewrite)
                                       ▼
                     ┌──────────────────────────────────────────┐
                     │  Pod B on Node 4                         │
                     │  IP = 10.244.7.5                         │
                     │                                          │
                     │   tcpdump: src=10.244.1.42 dst=10.244.7.5│   ← rule 4
                     │                                          │
                     └──────────────────────────────────────────┘
```

The CNI plugin's job is to make rules 1–4 *appear true* on top of whatever the underlying network actually does. Sometimes that is trivial (Pod CIDRs are real subnets, the underlying network routes them — AWS VPC CNI). Sometimes it requires encapsulation (VXLAN tunnels between every pair of nodes — Flannel default). Sometimes it requires announcing pod CIDRs with BGP into the underlying fabric (Calico). All three are valid; all three satisfy the four rules.

This is the entire intellectual content of CNI: **how do I cheaply, scalably, and securely build a flat L3 network for ephemeral processes on top of a substrate that does not natively know about them?**

---

## 2. Where CNI Sits in the Pod-Bringup Path

CNI is not invoked by `kubectl`. It is not invoked by the apiserver. It is invoked **by the container runtime, on behalf of the kubelet, exactly twice per pod**: once on creation, once on deletion. The diagram below puts CNI in its place — between the CRI shim and the Linux kernel — and identifies which moment of pod bringup actually does the work.

```
   ┌────────────────────────────────────────────────────────────────────┐
   │ apiserver: Pod object created, spec.nodeName=node-2                │
   └─────────────────────────────────────┬──────────────────────────────┘
                                         │ watch event
                                         ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ kubelet on node-2 (syncLoop)                                       │ ch 10
   │   sees new bound pod, sends RunPodSandbox to CRI                   │
   └─────────────────────────────────────┬──────────────────────────────┘
                                         │ gRPC RunPodSandboxRequest
                                         ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ containerd CRI plugin                                              │ ch 01
   │   creates network namespace: unshare(CLONE_NEWNET) inside pause    │
   │   creates pause container, holds the netns open                    │
   └─────────────────────────────────────┬──────────────────────────────┘
                                         │ fork+exec /opt/cni/bin/<plugin>
                                         │ CNI_COMMAND=ADD
                                         │ CNI_CONTAINERID=...
                                         │ CNI_NETNS=/proc/$pause_pid/ns/net
                                         │ CNI_IFNAME=eth0
                                         │ stdin = conflist JSON
                                         ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ CNI plugin chain executes (this chapter)                           │
   │   main plugin: create veth, attach to bridge/route,                │
   │                ask IPAM plugin for an IP, assign to pod end        │
   │   meta plugins: portmap, bandwidth, tuning, sysctl, sbr            │
   │   result: pod has eth0 = 10.244.1.42/24, default route, MTU set   │
   └─────────────────────────────────────┬──────────────────────────────┘
                                         │ stdout: result JSON
                                         │   { "ips": [...], "routes": [...] }
                                         ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ containerd records pod sandbox + IP, replies to kubelet            │
   │ kubelet proceeds: PullImage → CreateContainer → StartContainer     │
   └────────────────────────────────────────────────────────────────────┘

   (later, on pod delete)
   kubelet → CRI StopPodSandbox →
     containerd → /opt/cni/bin/<plugin> CNI_COMMAND=DEL → release IP, delete veth
```

Three things to internalize from this picture:

1. **kubelet does not exec the CNI plugin directly.** It tells the container runtime to set up a sandbox; the runtime (containerd, CRI-O) is the entity that loads `/etc/cni/net.d/*.conflist`, forks the binary, pipes JSON in, reads JSON out. This is why CNI config goes on every *node*, not on the apiserver — it's a runtime config file, not a Kubernetes object. (Search `containerd/internal/cri/server/sandbox_run.go` for `setupPodNetwork` to see the actual fork.)
2. **The network namespace exists before CNI runs.** The runtime has already created the netns (it's the pause container's netns) and CNI is handed `/proc/$pid/ns/net` as a path to operate on. CNI's job is to plumb wires *into* an existing netns, not create the netns itself.
3. **CNI runs synchronously in the pod-bringup critical path.** If CNI takes 200 ms (typical), that 200 ms is added to your pod startup latency. If CNI hangs (a frequent failure mode — see §30), the pod is stuck in `ContainerCreating` forever, because the runtime cannot answer kubelet's `RunPodSandbox` until CNI returns.

The result the plugin writes to stdout is parsed by the runtime and recorded as the pod's IP. The kubelet eventually patches `pod.status.podIP` and `pod.status.podIPs[]` after seeing this IP through CRI status calls (ch 10).

---

## 3. The CNI Specification: An Executable Interface

The CNI spec (current version 1.0.0, from `containernetworking/cni`) is famously tiny. It does not define datapaths, encapsulation, IP allocation, or policy. It defines exactly one thing:

> **A CNI plugin is an executable. The container runtime invokes it with a verb (set via env var `CNI_COMMAND`), JSON config on stdin, and a few env vars describing the target container. The plugin writes JSON to stdout.**

That is the whole interface. The rest of the spec is the JSON schemas.

### Environment Variables (the verb + the target)

When the runtime invokes a CNI plugin it sets:

| Env var | Meaning | Required for |
|---|---|---|
| `CNI_COMMAND` | `ADD`, `DEL`, `CHECK`, `VERSION`, `GC`, `STATUS` | all |
| `CNI_CONTAINERID` | Opaque ID from the runtime (matches across ADD/DEL) | ADD, DEL, CHECK |
| `CNI_NETNS` | Path to the network namespace (`/proc/$pid/ns/net` or a bind-mount) | ADD, DEL, CHECK |
| `CNI_IFNAME` | Interface name to create inside the netns (always `eth0` from kubelet) | ADD, DEL, CHECK |
| `CNI_PATH` | Colon-separated paths to look up sub-plugins (`/opt/cni/bin`) | all |
| `CNI_ARGS` | Optional `KEY=VAL;KEY=VAL` (used by Kubernetes to pass pod metadata) | optional |

Kubernetes always passes pod identity through `CNI_ARGS` so CNI plugins can resolve `K8S_POD_NAMESPACE`, `K8S_POD_NAME`, `K8S_POD_UID`, and `K8S_POD_INFRA_CONTAINER_ID`. This is how a plugin like Calico knows *which* pod is being networked, lets it look up labels, applies the right NetworkPolicy, etc.

### The Six Verbs

| Verb | What it does | When |
|---|---|---|
| `ADD` | Attach interface, assign IP, write routes. Return the result. | RunPodSandbox |
| `DEL` | Tear down interface, release IP. Must be **idempotent**. | StopPodSandbox |
| `CHECK` | Verify configuration is still correct for an existing container | Periodic, runtime-defined |
| `VERSION` | Print supported CNI versions; called once at plugin discovery | Runtime startup |
| `GC` | (1.1+) Garbage-collect IP leases for containers no longer present | Periodic |
| `STATUS` | (1.1+) Plugin self-health; e.g. "datastore reachable" | Pre-ADD readiness check |

The two interesting verbs in practice are `ADD` and `DEL`. `CHECK` is rarely used because it's expensive and kubelet doesn't drive it; `GC` and `STATUS` are new and adoption is uneven. The classic flow is just ADD-then-DEL.

### Stdin: The Network Config

The runtime writes JSON to the plugin's stdin describing *what network to attach to*. For a single plugin invocation it looks like:

```json
{
  "cniVersion": "1.0.0",
  "name": "k8s-pod-network",
  "type": "bridge",
  "bridge": "cni0",
  "isGateway": true,
  "ipMasq": false,
  "ipam": {
    "type": "host-local",
    "ranges": [
      [{ "subnet": "10.244.1.0/24" }]
    ],
    "routes": [{ "dst": "0.0.0.0/0" }]
  }
}
```

Several conventions are pinned by the spec:

- `cniVersion` is the version of the spec the runtime is using. Plugin must support that version or error.
- `name` is the network name. A node can have multiple networks (rare in Kubernetes — Multus changes this; see §27).
- `type` is the binary to exec. If `type == "bridge"`, the runtime runs `/opt/cni/bin/bridge`.
- All other fields are plugin-specific. There is no enforced schema beyond the top-level four.

### Stdout: The Result Object

After a successful ADD the plugin writes a result back to stdout:

```json
{
  "cniVersion": "1.0.0",
  "interfaces": [
    { "name": "eth0", "mac": "0a:58:0a:f4:01:2a", "sandbox": "/proc/12345/ns/net" }
  ],
  "ips": [
    { "address": "10.244.1.42/24", "gateway": "10.244.1.1", "interface": 0 }
  ],
  "routes": [
    { "dst": "0.0.0.0/0", "gw": "10.244.1.1" }
  ],
  "dns": {}
}
```

The runtime caches this result on disk (`/var/lib/cni/results/<network>-<containerID>-<ifname>`). On `DEL`, the runtime hands the **same JSON** back to the plugin so it knows exactly which IP to release and which interface to remove. This caching is critical for idempotent DEL (§25).

### Source-of-Truth Pointers

- Spec text: `containernetworking/cni/SPEC.md`
- Reference plugins (`bridge`, `host-local`, `loopback`, `ipvlan`, `macvlan`, `portmap`, etc.): `containernetworking/plugins/plugins/`
- libcni (Go library every runtime uses to invoke CNI plugins): `containernetworking/cni/libcni/`
- Containerd's CNI integration: `containerd/internal/cri/server/sandbox_run_linux.go`

If you read one piece of code while studying this chapter, read `libcni.CNIConfig.AddNetwork()` — it is 100 lines and contains the entire mechanism.

---

## 4. Plugin Chains, Lists, and the `conflist` File

A single network rarely needs a single plugin. You want one plugin to create the veth, one to allocate an IP, one to install iptables rules for hostPort mappings, one to set MTU, and so on. CNI supports this through **plugin chains**: a list of plugins invoked in order, each consuming the previous one's result and (optionally) modifying it.

### The conflist File

CNI configuration on every node lives in `/etc/cni/net.d/`. Files are read in lexical order; the first valid `*.conflist` wins. (Files ending in `.conf` are single-plugin configs — legacy, still supported.)

A realistic conflist with three plugins:

```json
{
  "cniVersion": "1.0.0",
  "name": "k8s-pod-network",
  "plugins": [
    {
      "type": "bridge",
      "bridge": "cni0",
      "isGateway": true,
      "isDefaultGateway": true,
      "forceAddress": false,
      "ipMasq": false,
      "hairpinMode": true,
      "mtu": 1450,
      "ipam": {
        "type": "host-local",
        "ranges": [[{ "subnet": "10.244.1.0/24" }]],
        "routes": [{ "dst": "0.0.0.0/0" }]
      }
    },
    {
      "type": "portmap",
      "capabilities": { "portMappings": true }
    },
    {
      "type": "bandwidth",
      "capabilities": { "bandwidth": true }
    }
  ]
}
```

Notes worth pausing on:

- `name` is shared across the whole chain. The chain is "the network".
- The first plugin in the list is the **main plugin** — it creates the interface. The rest are typically meta plugins that augment what the main plugin built.
- The `capabilities` blocks expose per-plugin knobs that the runtime fills in dynamically. `portMappings` is populated from `pod.spec.containers[*].ports[*].hostPort`. `bandwidth` is populated from the `kubernetes.io/ingress-bandwidth` / `egress-bandwidth` pod annotations. The runtime computes these and injects them into the per-plugin input.
- MTU here is 1450 — see §19 for why. A bridge plugin running over a VXLAN tunnel on a 1500-MTU underlay subtracts 50 bytes of overhead.

### Plugin Chain Execution (ADD)

Walked step by step:

```
            stdin:
            { cniVersion, name, type=bridge, bridge:cni0, ipam:{...} }
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │ /opt/cni/bin/bridge                                             │
   │   1. ensure bridge cni0 exists, up, with isGateway              │
   │   2. create veth pair veth_pod_X / veth_host_X                  │
   │   3. move pod-end to CNI_NETNS, rename to eth0                  │
   │   4. attach host-end to cni0                                    │
   │   5. exec /opt/cni/bin/host-local (IPAM) with the same stdin    │
   │      → receives { ips:[10.244.1.42/24], gw:10.244.1.1, ... }    │
   │   6. assign 10.244.1.42/24 to pod's eth0, install default route │
   │   7. set MTU=1450                                                │
   │   stdout: result { interfaces, ips, routes, mac, sandbox }      │
   └─────────────────────────────────────────────────────────────────┘
                              │  prev result passed via stdin
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │ /opt/cni/bin/portmap                                            │
   │   reads previous result + runtime-injected portMappings         │
   │   for each (containerPort, hostPort, protocol):                 │
   │     iptables -t nat -A CNI-HOSTPORT-DNAT -p tcp --dport 8080    │
   │       -j DNAT --to 10.244.1.42:80                                │
   │   stdout: same result (plus any annotations)                    │
   └─────────────────────────────────────────────────────────────────┘
                              │  prev result passed via stdin
                              ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │ /opt/cni/bin/bandwidth                                          │
   │   reads previous result + runtime-injected bandwidth limits     │
   │   sets up tc qdisc + tbf on host-side veth for shaping          │
   │   ingress shaped via ifb (intermediate functional block)        │
   │   stdout: same result                                           │
   └─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                final result returned to runtime
                runtime caches it in /var/lib/cni/results/
```

Key contract: **each plugin receives the previous plugin's result as `prevResult` inside its stdin JSON**. This is how `portmap` knows the pod's IP without hitting an API — `bridge` already populated it.

### Plugin Chain Execution (DEL)

DEL runs in **reverse order**. The runtime reads the cached result, hands it to the *last* plugin in the chain, then the second-to-last, and so on. Each plugin gets to undo its work in reverse order of creation. Bandwidth tears down qdiscs, portmap removes iptables rules, bridge tears down the veth and asks IPAM to release the IP.

If any plugin in the DEL chain fails the runtime logs and continues — DEL is best-effort. This is intentional. Anti-idempotent DEL plugins are how you accumulate orphan IPs.

---

## 5. Plugin Categories: main, IPAM, meta

The CNI plugin ecosystem splits into three buckets by purpose. There is no enforcement in the spec — it's a community convention — but every plugin you'll encounter belongs cleanly to one.

```
┌────────────────────────────────────────────────────────────────────────┐
│  MAIN PLUGINS   (create the interface, define the dataplane)           │
│                                                                        │
│  bridge        veth + Linux bridge, classic "Docker-style" L2          │
│  ptp           point-to-point veth, no bridge, host-side routing       │
│  macvlan       child interface on parent NIC, separate MAC             │
│  ipvlan        child interface, shares parent MAC, L2 or L3            │
│  loopback      lo inside the netns (always runs first, implicitly)     │
│  host-device   move an existing host device into the netns             │
│  vlan          802.1Q VLAN interface                                   │
│  flannel       Flannel's wrapper that delegates to bridge or ipvlan    │
│  calico        Calico's main plugin (veth + per-pod routes)            │
│  cilium-cni    Cilium's main plugin (veth + eBPF attach)               │
│  weave-net     Weave's main plugin (sleeve mesh or fastdp)             │
│  aws-cni       AWS VPC CNI (ENI secondary IP attached as veth)         │
│  azure-vnet    Azure CNI (transparent NAT or VNET-native)              │
│  ovn-kubernetes OVN-based main plugin                                  │
├────────────────────────────────────────────────────────────────────────┤
│  IPAM PLUGINS   (allocate IPs; main plugins exec these)                │
│                                                                        │
│  host-local    per-node CIDR slice, state in /var/lib/cni/networks/    │
│  static        operator-specified static IP per pod                    │
│  dhcp          plumb DHCP into the pod (rare; bare-metal labs)         │
│  calico-ipam   Calico's leased-block allocator (etcd-backed)           │
│  cilium-ipam   Cilium's allocator (kvstore-backed or cluster-pool)     │
│  whereabouts   cluster-wide IPAM coordinated through Kubernetes CRDs   │
├────────────────────────────────────────────────────────────────────────┤
│  META PLUGINS   (modify the result; chained after the main plugin)     │
│                                                                        │
│  portmap       hostPort → DNAT iptables rules                          │
│  bandwidth     ingress/egress traffic shaping via tc                   │
│  tuning        sysctl knobs per pod (net.ipv4.tcp_*, etc.)             │
│  firewall      open per-pod ports in firewalld/iptables                │
│  sbr           source-based routing per pod                            │
│  vrf           place pod in a Linux VRF                                │
└────────────────────────────────────────────────────────────────────────┘
```

A few things to know that aren't obvious from the names:

- `loopback` is always invoked first by the runtime — the spec requires every pod's netns to have `lo` up before any other plugin runs. You don't list it in your conflist; it is implicit.
- `host-local` keeps state on disk under `/var/lib/cni/networks/<network-name>/`. Each file there is named after an allocated IP and contains the container ID. If the node's disk is wiped while pods exist, this state is lost and the allocator can re-issue the same IP to a new pod, producing a conflict. (Pitfall — §32.)
- `bandwidth` uses Linux Token Bucket Filter (`tc qdisc add ... tbf`) on egress; for ingress shaping it injects an ifb device into the host netns and redirects packets. This means the pod cannot bypass the limit — shaping is at the host's veth-end.
- Plugins are versioned independently. Your `bridge` could be 1.0, your `portmap` could be 0.4, and they coexist as long as the conflist's declared `cniVersion` is supported by all of them.

---

## 6. Anatomy of a CNI ADD: The veth + Bridge Model

The "canonical" CNI dataplane — what the reference `bridge` plugin does, what Docker uses, what Flannel uses by default, and a useful baseline to understand any other CNI — is one bridge per node, one veth pair per pod, with host-side routing to other nodes.

```
                       NODE 1                                          NODE 2
   ┌──────────────────────────────────────────┐    ┌──────────────────────────────────────────┐
   │                                          │    │                                          │
   │  Pod A netns        Pod B netns          │    │  Pod C netns        Pod D netns          │
   │  ┌──────────┐       ┌──────────┐         │    │  ┌──────────┐       ┌──────────┐         │
   │  │  eth0    │       │  eth0    │         │    │  │  eth0    │       │  eth0    │         │
   │  │10.244.1.4│       │10.244.1.5│         │    │  │10.244.2.4│       │10.244.2.5│         │
   │  └────┬─────┘       └────┬─────┘         │    │  └────┬─────┘       └────┬─────┘         │
   │       │ veth pair         │ veth pair    │    │       │                  │               │
   │       │                   │              │    │       │                  │               │
   │  ┌────┴─────┐       ┌─────┴────┐         │    │  ┌────┴─────┐       ┌────┴─────┐         │
   │  │vethXXXX  │       │vethYYYY  │         │    │  │vethAAAA  │       │vethBBBB  │         │
   │  └────┬─────┘       └────┬─────┘         │    │  └────┬─────┘       └────┬─────┘         │
   │       │                  │               │    │       │                  │               │
   │       ▼                  ▼               │    │       ▼                  ▼               │
   │  ┌────────────────────────────────┐      │    │  ┌────────────────────────────────┐      │
   │  │         cni0 bridge            │      │    │  │         cni0 bridge            │      │
   │  │ ip: 10.244.1.1/24 (gateway)    │      │    │  │ ip: 10.244.2.1/24 (gateway)    │      │
   │  └───────────────┬────────────────┘      │    │  └───────────────┬────────────────┘      │
   │                  │                       │    │                  │                       │
   │       route: 10.244.2.0/24 via tun       │    │       route: 10.244.1.0/24 via tun       │
   │                  │                       │    │                  │                       │
   │  ┌───────────────┴────────────────┐      │    │  ┌───────────────┴────────────────┐      │
   │  │  eth0 (host NIC)               │      │    │  │  eth0 (host NIC)               │      │
   │  │  ip: 192.168.10.1              │      │    │  │  ip: 192.168.10.2              │      │
   │  └───────────────┬────────────────┘      │    │  └───────────────┬────────────────┘      │
   └──────────────────┼───────────────────────┘    └──────────────────┼───────────────────────┘
                      │                                               │
                      └──────────────── underlay network ─────────────┘
                                  (cloud VPC, datacenter L2/L3,
                                   tunnel, or BGP-routed fabric)
```

### What the `bridge` plugin does on ADD, in detail

1. **Ensure the bridge exists.** If `cni0` is not present, create it with `ip link add name cni0 type bridge`, set `up`, assign the gateway IP (`10.244.1.1/24`), and enable promiscuous mode.
2. **Create a veth pair.** `ip link add veth_pod_X type veth peer name eth0_temp`. Veth is a kernel construct: two virtual interfaces wired back-to-back. Bytes in one end come out the other.
3. **Move the pod-side end into the netns.** `ip link set eth0_temp netns /proc/12345/ns/net`. The host-side `veth_pod_X` stays in the host netns.
4. **Inside the netns, rename and configure.** Rename to `eth0` (always — `CNI_IFNAME` from kubelet is always `eth0`). Set MAC, set up, assign IP, install default route.
5. **Attach host end to bridge.** `ip link set veth_pod_X master cni0`, then `ip link set veth_pod_X up`.
6. **Set MTU.** Both ends. Critical (§19).
7. **Optionally enable hairpin mode.** `bridge link set dev veth_pod_X hairpin on` — allows a pod to reach itself via Service VIP (otherwise the bridge drops the reflected frame).
8. **Call the IPAM plugin** (already done by step 4 in implementation; the design separates it for clarity). IPAM returned the IP, gateway, and routes.

### How packets actually flow

**Same-node pod-to-pod (A → B):**
1. Pod A sends to 10.244.1.5. Its default route says "all 10.244.1.0/24 is on-link via eth0".
2. ARP for 10.244.1.5 — the request goes out vethXXXX, into cni0 bridge.
3. cni0 is L2; it floods the ARP. vethYYYY (pod B) replies with B's MAC.
4. Frame is forwarded by cni0 from vethXXXX to vethYYYY. No routing decision, no IP-level processing.
5. Pod B receives. tcpdump on cni0 shows the original src/dst IPs unchanged.

This is the "rule 2 with no NAT" working at full L2 speed. Same-node traffic is approximately host loopback fast.

**Cross-node pod-to-pod (A on node 1 → C on node 2):**
1. Pod A sends to 10.244.2.4. Its default route is via the bridge gateway 10.244.1.1.
2. Frame leaves veth, enters cni0 bridge, which (as it has the gateway IP) delivers to the host's network stack.
3. Host routing table: `10.244.2.0/24 via <tunnel-or-gateway>`. This is where the CNI flavor diverges:
   - **Overlay (VXLAN, IPinIP, Geneve)**: encap the frame, send to node 2's host IP. §8.
   - **Underlay (BGP, native VPC routing)**: just route — the underlying network knows where 10.244.2.0/24 lives. §9.
   - **Flannel host-gw**: like underlay but the routes are managed by Flannel through ARP, only works on an L2 segment. §10.
4. Reaches node 2's host eth0. Reverse decap (or just routing).
5. Host 2 routes to `10.244.2.4` via `cni0`, bridge delivers to vethAAAA, pod C receives.

**Pod-to-node (A → 192.168.10.1):**
- Routed through the host's main routing table; reaches the host's eth0 like any normal node-to-node traffic. Rule 3 satisfied because the host can see the bridge's interfaces and respond.

**Pod-to-external (A → 8.8.8.8):**
- Hits the host's default route, goes out eth0. But! The packet's source IP is 10.244.1.4, which the outside world cannot route back. So the host applies SNAT (`iptables -t nat -A POSTROUTING -s 10.244.0.0/16 ! -d 10.244.0.0/16 -j MASQUERADE`) and the packet leaves with the node's IP. Reply comes back, is reverse-NAT'd, returns to the pod. §21.

That last line — SNAT for off-cluster traffic — is the *only* exception to "no NAT" in the entire Kubernetes networking model. Inside the cluster, no NAT. Going out, MASQUERADE so the world has a return path. CNIs implement this with `ipMasq: true` (bridge plugin) or as a built-in feature (Calico, Cilium, AWS VPC CNI do it differently).

---

## 7. IPAM Models: host-local, calico-ipam, cloud-native, dhcp

The CNI plugin that creates the interface doesn't know how to pick an IP. That job belongs to a separate **IPAM plugin** that the main plugin calls (or that the CNI runtime invokes as part of the chain). There are four common IPAM strategies in production Kubernetes, each with very different operational characteristics.

### host-local: per-node CIDR slice

The simplest model. Each node gets a `podCIDR` (e.g., `10.244.7.0/24`, allocated by the controller manager from the cluster pod CIDR). The IPAM plugin walks the CIDR in order, hands out IPs, persists state on the node's disk.

```
Cluster pod CIDR:  10.244.0.0/16        ← whole cluster
                        │
                        ▼  controller-manager (--allocate-node-cidrs=true,
                        │   --node-cidr-mask-size=24) carves /24s
                        │
                   ┌────┴────┬────────┬────────┐
                   ▼         ▼        ▼        ▼
                 node1     node2    node3    node4
              10.244.1/24  /24     /24      /24

   On node3, host-local persists state under:
   /var/lib/cni/networks/k8s-pod-network/
   ├── 10.244.3.2   ← contains container ID
   ├── 10.244.3.3
   ├── 10.244.3.4
   └── last_reserved_ip.0   ← next IP to try
```

Pros: trivially simple, no API calls, no datastore. Pulls one IP per ADD.

Cons:
- **State on disk → state lost if disk is wiped or node is replaced.** Pods come back with conflicting IPs. (See §32 pitfall.)
- **Capacity is fixed per node.** A `/24` = 256 IPs (≈ 250 usable). If pods per node > 250 you've outgrown a `/24`. The `node-cidr-mask-size` is set at cluster creation; resizing means a cluster rebuild.
- **Fragmentation.** Half the pods restart and get new IPs from low numbers; the rest stay at high numbers; no compaction.

Used by: Flannel default, kubeadm reference setup, any plugin that just wants "give me an IP within this range, fast."

### calico-ipam: leased block allocation

Calico ditches per-node static CIDRs in favor of a more flexible model: blocks. A **block** is a small CIDR (default `/26` = 64 IPs) leased from the cluster pool to a node. A node may hold many blocks (popular nodes), and blocks may be freed and re-leased as pod density changes.

```
Cluster pool: 10.244.0.0/16
              │
              ├── block 10.244.0.0/26 → node1 (affinity)
              ├── block 10.244.0.64/26 → node1
              ├── block 10.244.0.128/26 → node2
              ├── block 10.244.0.192/26 → node3
              ├── block 10.244.1.0/26 → unallocated
              └── ...
```

Each block is stored as a `IPAMBlock` CRD in the datastore (etcd or the Kubernetes API). Allocation is a transaction: claim an IP within a block, increment a counter. If a block is exhausted, claim a new block.

Pros:
- **Datastore-backed → state survives node loss.** Re-creating a node doesn't lose allocations.
- **Sparse pod CIDRs.** A node that runs 5 pods holds one block (64 IPs reserved) instead of an entire /24 worth of address space.
- **Cluster pool ≠ node pool.** Need more space? Add another pool. No node-mask resize required.

Cons:
- API calls per allocation. (Mitigated: a Calico node pre-allocates an entire block before serving from it; subsequent allocations within the block are local.)
- More moving parts to debug. `calicoctl ipam show --show-blocks` becomes a habit.

Used by: Calico (obviously), also Tigera Enterprise.

### Cloud-native: VPC-routable IPs

AWS VPC CNI, GKE's native VPC CNI, and Azure CNI (VNET-native mode) all use a fundamentally different model: **pod IPs come from the underlying cloud's IP address space**. There is no overlay, no per-node CIDR slice. Each pod gets a real VPC IP that the cloud network natively routes.

```
VPC: 10.0.0.0/16
  subnet-a: 10.0.1.0/24      ← node1 attached, ENI primary IP 10.0.1.10
  subnet-b: 10.0.2.0/24
  
  AWS VPC CNI on node1:
    primary ENI IP: 10.0.1.10
    secondary IPs:  10.0.1.20, 10.0.1.21, 10.0.1.22, ...
                    (pre-allocated, attached to ENI as secondary IPs;
                     each one assigned to a pod via veth)
    
    Pod1 = 10.0.1.20  ← a real VPC IP, the AWS router knows about it
    Pod2 = 10.0.1.21
    Pod3 = 10.0.1.22
```

Pros:
- **Pods are first-class VPC participants.** Can reach RDS, S3 endpoints, SQS, etc. without SNAT.
- **Security groups apply at pod granularity** (via "security groups for pods" in EKS).
- **No encapsulation tax** — wire-rate networking, no MTU subtraction.
- **Cross-AZ pod IP visibility** — peered VPCs see pod IPs.

Cons:
- **ENI/IP limits.** Each EC2 instance type allows a fixed number of ENIs and secondary IPs per ENI. `m5.large` = 3 ENIs × 10 IPs = 30 IPs ≈ 30 pods. AWS introduced **prefix delegation** (a /28 per ENI slot, 16 IPs per slot) in VPC CNI 1.18+ to relax this dramatically.
- **VPC subnet exhaustion is a real concern** at scale. Plan subnets large.
- **Pod IPs leak into the cloud's logs, flow logs, etc.** — sometimes good, sometimes a privacy concern.

Used by: AWS VPC CNI, Azure CNI (VNET mode), GKE Standard mode.

### dhcp: bring your own DHCP

The CNI `dhcp` IPAM plugin plumbs DHCP discovery from inside the pod netns out to an external DHCP server. Rarely used in modern Kubernetes. Common in bare-metal or telco environments where the existing network has DHCP infrastructure that should own pod IP allocation.

Pros: lets the existing network team's IP management tooling handle pods like any other host.

Cons: every pod startup waits for DHCP DISCOVER/OFFER/REQUEST/ACK. Slow. Pods don't gracefully renew leases. Pod IP exhaustion is now the DHCP server's problem.

### Comparison

| | host-local | calico-ipam | cloud-native | dhcp |
|---|---|---|---|---|
| State location | node disk | datastore (etcd/API) | cloud control plane | external DHCP |
| Survives node wipe | no | yes | yes (cloud-side) | yes |
| Per-node capacity | fixed CIDR mask | dynamic via blocks | fixed by instance type | DHCP-limited |
| Allocation speed | µs (local file) | first allocation in a new block: ms (API); rest: µs | µs (warm pool) or seconds (cold) | seconds (DHCP) |
| Cluster-pool flexible | limited (mask at create) | yes (multiple pools) | tied to VPC subnets | tied to DHCP scope |
| Right for | Flannel, small/simple clusters | Calico, big clusters | EKS, AKS, GKE native | bare-metal labs |

---

## 8. Overlays: VXLAN and Geneve

When the underlying network does *not* know how to route Pod CIDRs (the typical case: every cloud VPC, every multi-subnet datacenter), the CNI must build an overlay — a tunnel — between every pair of nodes. Pod packets get encapsulated as the *payload* of a packet between nodes; the underlay only sees node-to-node traffic.

**VXLAN** (Virtual eXtensible LAN, RFC 7348) is the most common encapsulation in Kubernetes overlays. It wraps an L2 Ethernet frame inside a UDP/IP packet.

### Byte layout of a VXLAN-encapsulated frame

```
   Underlay IP packet (sent between nodes; underlay routes this)
   ┌─────────────────────────────────────────────────────────────────────┐
   │ Ethernet header (14 B)                                              │
   │   dst MAC = next-hop in underlay                                    │
   │   src MAC = node's NIC                                              │
   │   ethertype = 0x0800 (IPv4)                                         │
   ├─────────────────────────────────────────────────────────────────────┤
   │ Outer IP header (20 B)                                              │
   │   src = node1 IP (192.168.10.1)                                     │
   │   dst = node2 IP (192.168.10.2)                                     │
   │   protocol = 17 (UDP)                                                │
   ├─────────────────────────────────────────────────────────────────────┤
   │ Outer UDP header (8 B)                                              │
   │   src port = hash(inner flow), spreads ECMP across underlay         │
   │   dst port = 8472 (Linux default; IANA assigned 4789)               │
   ├─────────────────────────────────────────────────────────────────────┤
   │ VXLAN header (8 B)                                                  │
   │   flags  (1 B)  = 0x08  (I-bit set, VNI valid)                      │
   │   rsvd   (3 B)  = 0                                                  │
   │   VNI    (3 B)  = e.g. 0x000001  (virtual network id)               │
   │   rsvd   (1 B)  = 0                                                  │
   ├═════════════════════════════════════════════════════════════════════┤
   │  Inner Ethernet header (14 B)                                       │
   │    dst MAC = destination pod's veth MAC                             │
   │    src MAC = source pod's veth MAC                                  │
   │  Inner IP header (20 B)                                             │
   │    src = pod A IP (10.244.1.4)                                      │
   │    dst = pod C IP (10.244.2.4)                                      │
   │  Inner TCP / UDP / ICMP / payload                                   │
   └─────────────────────────────────────────────────────────────────────┘

   Total encapsulation overhead = 14 + 20 + 8 + 8 = 50 bytes
                                  (eth + IP + UDP + VXLAN)
   If outer is IPv6: 14 + 40 + 8 + 8 = 70 bytes
   GENEVE: same skeleton, header size 8–66 B (options), so 50–108 B
```

The outer eth+IP+UDP makes the packet look like ordinary UDP between two nodes. Underlay routers, firewalls, and load balancers see only node-to-node UDP on port 8472 (or 4789). They don't even know the packet is "carrying" something.

### How a VXLAN tunnel is set up on Linux

Linux has native VXLAN support since kernel 3.7. The CNI configures a VXLAN device:

```
ip link add vxlan.calico type vxlan id 4096 dstport 4789 \
            local 192.168.10.1 nolearning
ip link set vxlan.calico up
ip addr add 10.244.1.0/32 dev vxlan.calico
```

Then for every remote node, install a **forwarding database (FDB) entry** telling the kernel "to reach MAC `00:00:0a:f4:02:01` on this VXLAN, encapsulate to `192.168.10.2`":

```
bridge fdb add 00:00:0a:f4:02:01 dev vxlan.calico dst 192.168.10.2 self permanent
ip route add 10.244.2.0/24 via 10.244.2.0 dev vxlan.calico onlink
ip neigh add 10.244.2.0 lladdr 00:00:0a:f4:02:01 dev vxlan.calico
```

Now any packet to `10.244.2.0/24` goes out `vxlan.calico` with the right inner MAC, the kernel matches the FDB, encapsulates, sends to `192.168.10.2`. The receiving node has the same VXLAN device, decapsulates, hands the inner frame to its bridge, which routes to the pod's veth.

```
   pod A (10.244.1.4)
      │ raw IP packet
      ▼
   pod's eth0 → veth → cni0 → host routing table:
      10.244.2.0/24 dev vxlan.calico
      │
      ▼  kernel VXLAN encap
      ┌──────────────────────────────────┐
      │ outer eth: ?? → ??               │
      │ outer IP : 192.168.10.1 → .10.2  │
      │ outer UDP: srcHash → 4789        │
      │ VXLAN VNI: 4096                  │
      │ inner eth: pod A MAC → pod C MAC │
      │ inner IP : 10.244.1.4 → 10.244.2.4│
      │ inner TCP: payload               │
      └──────────────────────────────────┘
      │ sent out node1 eth0
      ▼
   underlay routes UDP between 192.168.10.1 → .10.2
      │
      ▼ arrives at node2 eth0
   kernel sees UDP on 4789, looks up VXLAN device by VNI,
   strips outer headers, hands inner frame to vxlan.calico
      │
      ▼  host routing: dst=10.244.2.4 → cni0 bridge
   cni0 → vethCCC → pod C eth0 → pod C receives
```

### Tradeoffs

**Pros:**
- Works on any L3 underlay. The cloud doesn't have to know about pod CIDRs.
- Fully transparent to underlay routers, firewalls (as long as they don't inspect VXLAN content).
- Native kernel support, decent performance (typically 10–20% throughput cost vs underlay).
- Multi-tenant via VNI — different VNIs are different virtual networks.

**Cons:**
- **50-byte overhead per packet.** MTU must be 50 less than the underlay path MTU (§19). Forget this and large packets are dropped silently.
- **Source-port hash is the only ECMP knob.** If the underlay uses only outer 5-tuple for ECMP, all VXLAN flows from node1 to node2 hash to the *same* underlay path because outer IPs are the same. Linux randomizes the outer source port per inner flow to fix this.
- **Some firewalls block UDP 4789/8472.** Particularly across cloud-region peerings or between cloud and on-prem. Symptom: pods on one node can ping pods on the same node but not pods on another node.
- **VXLAN offload is hit-or-miss.** Older NICs don't checksum-offload encapsulated traffic; CPU spikes on heavy traffic. Modern NICs (Mellanox CX-5, AWS ENA) have VXLAN offload.

### Geneve

GENEVE (Generic Network Virtualization Encapsulation, RFC 8926) is essentially "VXLAN with extensible options". The header includes a variable-length option list that can carry tenant ID, metadata, service routing hints, etc. AWS Transit Gateway uses GENEVE under the hood. In Kubernetes, Cilium can use GENEVE as an alternative to VXLAN; functionally similar from the pod's perspective.

### Performance: what overlays actually cost

Real numbers from production tuning sessions, ballpark, for a 25 Gbps NIC on modern CPUs:

| Scenario | Throughput | CPU per Gbps |
|---|---|---|
| Bare-metal node-to-node (no encap) | ~24 Gbps | very low |
| Pod-to-pod via cni0 bridge, same node | ~30+ Gbps (loopback-like) | low |
| Cross-node, native routing (BGP or VPC) | ~22 Gbps | low |
| Cross-node, VXLAN with NIC offload | ~20 Gbps | moderate |
| Cross-node, VXLAN without offload | ~10–14 Gbps | high (kernel saturates a core) |
| Cross-node, IPinIP with offload | ~22 Gbps | low |
| Cross-node, WireGuard | ~10–15 Gbps | high (crypto is the bottleneck) |
| Cross-node, IPSec ESP (AES-GCM, hardware) | ~18 Gbps | moderate |

Two consequences:

1. **VXLAN offload matters a lot.** Confirm with `ethtool -k eth0 | grep -i vxlan`. If `tx-udp_tnl-segmentation: off`, you're software-encapping every packet and paying ~30% throughput.
2. **The cost of WireGuard is real.** Don't enable cluster-wide WireGuard "just for safety" — encrypt only what compliance requires. Most pod traffic crosses an already-trusted underlay.

### The decap path on receive

A subtle but critical detail. When a VXLAN-encapsulated packet arrives at the receiving node:

1. The kernel matches the destination IP (the node's host IP) and protocol UDP.
2. Looks up the UDP socket bound to port 4789.
3. Finds the VXLAN device's socket, hands the payload up.
4. The VXLAN device strips the outer headers, hands the *inner Ethernet frame* up to the netif input path again.
5. The frame is dispatched as if it just arrived on the VXLAN device.
6. Forwarded based on inner destination MAC / IP through the host's routing or bridging.

Because the inner frame re-enters the netif input path, **all the standard rx hooks apply twice**: once for the outer, once for the inner. eBPF programs attached to TC ingress see the outer flow, then the inner. tcpdump on `eth0` shows VXLAN UDP; tcpdump on `vxlan.calico` shows the inner pod-to-pod frame. This is how Hubble can see "pod A → pod C" on the receiving node — it taps the post-decap point.

---

## 9. Underlays: BGP and Native Routing

If the underlying network *can* route pod CIDRs, you don't need encapsulation. Save the 50 bytes, save the CPU, get native performance. This is the **underlay** model. The trick is convincing the underlying network's routers that the pod CIDRs are real and belong on a particular node.

The dominant mechanism is **BGP** (Border Gateway Protocol). Each node runs a BGP speaker that advertises "I own the pod CIDR for the pods on me." Routers in the fabric learn these advertisements and route accordingly.

### Topology and roles

```
                        ┌───────────────────────┐
                        │ Top-of-Rack router    │
                        │ (or cloud route table │
                        │  if cloud-native BGP) │
                        │                       │
                        │  routes (learned):    │
                        │   10.244.1.0/24 → n1  │
                        │   10.244.2.0/24 → n2  │
                        │   10.244.3.0/24 → n3  │
                        └─┬─────────┬─────────┬─┘
                          │ BGP     │ BGP     │ BGP
                          │         │         │
                  ┌───────┴──┐  ┌───┴─────┐  ┌┴────────┐
                  │ node1    │  │ node2   │  │ node3   │
                  │ BGP      │  │ BGP     │  │ BGP     │
                  │ speaker  │  │ speaker │  │ speaker │
                  │ (BIRD,   │  │         │  │         │
                  │  GoBGP,  │  │         │  │         │
                  │  FRR)    │  │         │  │         │
                  │ advert:  │  │ advert: │  │ advert: │
                  │ 10.244.  │  │ 10.244. │  │ 10.244. │
                  │  1.0/24  │  │  2.0/24 │  │  3.0/24 │
                  │  via me  │  │ via me  │  │ via me  │
                  └──────────┘  └─────────┘  └─────────┘
```

### Full mesh vs route reflectors

- **Full mesh**: every node peers with every other node. For N nodes, N×(N-1)/2 BGP sessions. Works fine up to ~50 nodes. Above that, the session count explodes (5000 nodes = 12.5M sessions, completely impractical).
- **Route reflectors (RR)**: dedicated nodes (or pods) that aggregate BGP routes. Each leaf node peers only with the RRs. The RRs reflect routes between leaves. Linear scaling. Standard at production scale.

Calico defaults to full mesh and recommends switching to RRs above 50 nodes. Cilium with BGP control-plane mode supports the same.

### A real bird.conf snippet (Calico-style)

Calico runs BIRD (or BGP-via-its-own implementation) inside the `calico-node` DaemonSet. A node's `bird.cfg` looks like:

```
router id 192.168.10.1;

# Local pod CIDR advertisement
protocol static {
   include "/etc/calico/confd/config/static_routes.conf";
   # static_routes.conf contains:
   #   route 10.244.1.0/24 blackhole;
}

# Peer with route reflectors
template bgp bgp_template {
   description "BGP session to route reflector";
   local 192.168.10.1 as 64512;
   neighbor 192.168.10.10 as 64512;
   multihop;
   gateway recursive;
   import all;
   export filter calico_export_to_bgp_peers;
   add paths on;
   graceful restart;
   connect retry time 5;
}

protocol bgp 'rr_node_192_168_10_10' from bgp_template { }
protocol bgp 'rr_node_192_168_10_11' from bgp_template { }

filter calico_export_to_bgp_peers {
   if ( net = 10.244.1.0/24 ) then accept;
   reject;
}
```

The session is iBGP within ASN 64512 (private ASN). When pods come and go, only the route `10.244.1.0/24` is advertised — pod-granular `/32` routes are unusual at scale because they'd flood the BGP tables; Calico aggregates by node CIDR.

### `ip route` after BGP convergence

On node1, after the BGP session is up and node2/node3 have advertised their CIDRs:

```
$ ip route
default via 192.168.10.254 dev eth0
10.244.1.0/24 dev cni0 proto kernel scope link src 10.244.1.1
10.244.2.0/24 via 192.168.10.2 dev eth0 proto bird onlink
10.244.3.0/24 via 192.168.10.3 dev eth0 proto bird onlink
192.168.10.0/24 dev eth0 proto kernel scope link src 192.168.10.1
```

The `proto bird` lines came from BGP. Now a packet to `10.244.2.4` is routed directly: no encapsulation, just `dev eth0 via 192.168.10.2`. The cloud's router (or top-of-rack switch) does the same — it learned the route via its own BGP session.

### Where BGP fits / doesn't fit

**Works where:**
- On-prem datacenters where you control the top-of-rack switches. Add a BGP peering with each leaf.
- Cloud environments with "BGP into VPC" features: AWS Cloud WAN, Google Cloud Routes via Network Connectivity Center, Azure Route Server.
- Internal cloud networks where the cluster owns its own route tables (advanced GKE).

**Doesn't work where:**
- Standard AWS/Azure/GCP VPCs without route programmability. The VPC's router doesn't accept BGP from arbitrary instances. Calico in this environment must fall back to IPinIP or VXLAN.
- Networks where pod CIDR overlaps with something else. BGP will advertise it; the destination won't be reachable.
- Anywhere the underlying network's anti-spoofing rules drop packets with src/dst in the pod CIDR. (Cloud VPCs do this — they expect packets to use VPC IPs.)

### Tradeoffs

**Pros:**
- **Zero encapsulation overhead.** Full MTU.
- **Underlay visibility.** Routers see pod IPs in their flow logs, ACLs work as expected, no hidden tunnel.
- **High performance.** Native L3 routing at line rate.
- **Compatible with hardware offload.** Standard IP packets, every NIC handles them at wire speed.

**Cons:**
- **Underlay must cooperate.** Most managed clouds don't let you BGP into them, except via dedicated offerings.
- **Operational complexity.** BGP misconfig (wrong AS number, wrong neighbor IP) takes down cluster networking with cryptic errors.
- **No multi-tenancy isolation built-in.** All pod CIDRs are visible to the underlay; segmentation is by NetworkPolicy, not by tunnel.

### BGP failure modes worth memorizing

- **Session in `Active` state forever**: the local side keeps trying TCP/179 but the peer never responds. Almost always: ACL/firewall blocking, or wrong neighbor IP, or peer not configured for our AS number.
- **Session in `OpenSent`, flapping**: AS-number mismatch or authentication failure (BGP MD5 secret not matching).
- **Session `Established` but no routes**: route filters dropping advertisements, or `export filter` rejecting our pod-CIDR prefix. `birdcl show route export <peer>` is the diagnostic.
- **Routes installed but blackhole**: BGP says the route is via `192.168.10.2`, but the underlay doesn't actually have a path to `192.168.10.2` from this node (split-horizon, asymmetric routing). `traceroute` from the node tells you.
- **Route flapping during reconcile**: Calico's confd regenerates bird.cfg, reloads bird, BGP sessions briefly tear down. Symptom: brief connectivity blip on every IPPool change. Mitigation: Calico's `gracefulRestart` config in BGPConfiguration to keep routes during reload.

### eBPF + BGP: the modern pattern

A growing pattern in 2024+ is to use BGP for routing announcements but eBPF for the dataplane. Cilium's BGP control plane mode (powered by GoBGP) advertises pod CIDRs to the underlay while the eBPF programs do the actual policy/forwarding. Best of both worlds: native routing with eBPF-grade observability and policy.

```
   Cilium BGP control plane    eBPF dataplane
   ┌──────────────────┐        ┌──────────────────┐
   │ GoBGP daemon     │        │ TC ingress eBPF  │
   │ advertises       │        │ matches identity │
   │ 10.244.1.0/24    │        │ enforces policy  │
   │ to ToR switch    │        │ forwards         │
   └──────────────────┘        └──────────────────┘
            │                            │
            └────────── per-node ────────┘
```

---

## 10. Hybrid Dataplanes: IPinIP, VXLAN-over-BGP, host-gw

Real production CNIs blend the patterns. Calico's claim to fame is that it supports several dataplanes and switches automatically based on where the destination is.

### Calico IPinIP

IP-in-IP encapsulation (RFC 2003) wraps an inner IP packet inside an outer IP packet with `protocol = 4` (or 41 for IPv6). Lighter than VXLAN: only 20 bytes of overhead (one outer IP header), no UDP, no VXLAN header.

```
   ┌────────────────────────────────────────────────────┐
   │ Outer IP header (20 B)                             │
   │   src = node1 IP, dst = node2 IP                   │
   │   protocol = 4 (IPv4-in-IPv4)                      │
   ├────────────────────────────────────────────────────┤
   │ Inner IP header (20 B)                             │
   │   src = 10.244.1.4, dst = 10.244.2.4               │
   │ + payload                                          │
   └────────────────────────────────────────────────────┘
   Overhead: 20 B (vs VXLAN 50 B, GENEVE 50–66 B)
```

Used by Calico when the underlay can't route pod CIDRs but can route node IPs. The trick: Calico still runs BGP between nodes for **control-plane** discovery (which nodes own which pod CIDR), but uses IPinIP for the **dataplane** because the underlay won't accept native pod routes.

Tradeoffs:
- 20 bytes saved per packet vs VXLAN.
- **No ECMP variation** — only the outer 3-tuple changes (proto + src/dst IP), so all flows between two nodes follow the same underlay path. (VXLAN's randomized source port doesn't apply.)
- **Some cloud underlays drop proto-4 traffic.** AWS allows IPinIP but rate-limits it. Azure VNets don't allow it at all → Calico on Azure uses VXLAN.

### Calico VXLAN mode (BGP-free)

If you don't have or don't want BGP, Calico can do everything via VXLAN: BGP-less control plane discovery (uses the K8s API for "who owns which CIDR") + VXLAN data plane. Slightly heavier than IPinIP but works everywhere.

### Flannel host-gw

The "simplest possible underlay": no encapsulation, no BGP. Flannel watches the K8s API for nodes, reads each node's `podCIDR`, and installs an `ip route` on every node:

```
$ ip route
10.244.2.0/24 via 192.168.10.2 dev eth0
10.244.3.0/24 via 192.168.10.3 dev eth0
...
```

That's it. The host's routing table sends pod-to-pod traffic to the destination node's host IP, the destination node's host receives the (unencapsulated) packet because the dest IP `10.244.2.4` matches its `cni0` interface route.

**Constraint:** every node must be in the same L2 broadcast domain. If node1 is in subnet A and node2 is in subnet B, the underlying router will see packets to `10.244.2.4` from node1, won't know what to do with them (it has no route for `10.244.0.0/16` because no BGP), and will drop them. host-gw works on a flat L2 cluster (a single VLAN, a single AWS subnet, etc.) but doesn't work across L3 boundaries.

This makes it ideal for small bare-metal clusters, brittle for anything else.

### Why hybrids exist

The matrix of "underlay capabilities" vs "what the CNI can offer" produces a 2D map. Hybrid CNIs let you pick a row:

| Underlay can route pod CIDRs? | Underlay accepts BGP? | Best Calico mode |
|---|---|---|
| Yes (peered VPC, on-prem with BGP to ToR) | Yes | BGP, no encapsulation |
| Yes | No | Static routes, no encapsulation (rarely seen) |
| No | Yes | BGP control, VXLAN/IPinIP data |
| No | No | VXLAN end-to-end (BGP-less) |

Pick the lightest mode the underlay supports. Default to VXLAN if unsure.

---

## 11. Calico Deep Look

Calico has been the broadest-feature CNI for years and is the right baseline to understand multi-dataplane CNIs. The architecture is layered.

```
        kube-apiserver                           etcd or KDD (Kubernetes
            │                                    Datastore Driver)
            │ watch                              │
            │                                    │
            ▼                                    │
   ┌────────────────────────────────────────────┐│
   │ calico-typha (optional)                    ││  ch 11 / §11
   │   fan-out proxy between many calico-nodes  ││
   │   and the apiserver. >50 nodes:            ││
   │   without typha, each calico-node opens    ││
   │   its own watch → apiserver melts.         ││
   │   with typha, only typha watches; each     ││
   │   calico-node connects to typha.           ││
   └────────────┬───────────────────────────────┘│
                │  TLS                            │
                │                                 │
       ┌────────┴──────────────────────────────┐  │
       │           on each node                │  │
       │  ┌──────────────────────────────────┐ │  │
       │  │  calico-node (DaemonSet pod)     │ │  │
       │  │  ┌────────────────────────────┐  │ │  │
       │  │  │  Felix                     │  │ │  │
       │  │  │  - policy/dataplane agent  │  │ │  │
       │  │  │  - programs iptables / nft │  │ │  │
       │  │  │     / eBPF / IPVS          │  │ │  │
       │  │  │  - watches policy CRDs     │  │ │  │
       │  │  └────────────────────────────┘  │ │  │
       │  │  ┌────────────────────────────┐  │ │  │
       │  │  │  BIRD (or GoBGP)           │  │ │  │
       │  │  │  - BGP speaker             │  │ │  │
       │  │  │  - advertises pod CIDRs    │  │ │  │
       │  │  └────────────────────────────┘  │ │  │
       │  │  ┌────────────────────────────┐  │ │  │
       │  │  │  confd                      │  │ │  │
       │  │  │  - generates bird.cfg from │  │ │  │
       │  │  │    BGPPeer CRDs            │  │ │  │
       │  │  └────────────────────────────┘  │ │  │
       │  └──────────────────────────────────┘ │  │
       └───────────────────────────────────────┘  │
                                                  │
   ┌──────────────────────────────────────────────┘
   │ calico CNI binary (in /opt/cni/bin/)
   │   invoked by containerd on pod create/delete
   │   talks to calico-ipam for IP, to felix-via-IPC
   │   for workload-endpoint creation
   └──────
```

### Components in detail

- **Felix**: the policy/dataplane agent. Watches `NetworkPolicy`, `GlobalNetworkPolicy`, `HostEndpoint`, `WorkloadEndpoint` (Calico's own CRD representation of pods), and programs the host's dataplane (iptables, nftables, eBPF, or IPVS depending on mode). It runs as a goroutine inside the calico-node pod. Source: `projectcalico/calico/felix/`.
- **BIRD / GoBGP**: BGP daemon. Advertises pod CIDRs to peers. Receives advertisements and installs routes via netlink. Required for BGP mode; not needed for VXLAN-only.
- **confd**: a watcher that turns Calico CRDs (like `BGPPeer`, `BGPConfiguration`) into bird config files and reloads bird. Necessary because BIRD reads config from disk, not from an API.
- **calico-ipam**: the IPAM plugin invoked by the CNI binary. Manages IPAM blocks (§7).
- **calico-typha**: an optional fan-out proxy. Felix and confd connect to typha; typha holds a single watch on the apiserver. Without typha, 500 Felix instances = 500 watches = apiserver overload.
- **calico-kube-controllers**: a separate Deployment running policy controllers, IPAM garbage collector, node status reporter, etc. Not in the dataplane critical path.

### Dataplane modes

Felix can program four different dataplanes:

| Mode | How pod traffic is enforced | Performance |
|---|---|---|
| iptables (default) | rules in OUTPUT/INPUT/FORWARD chains | OK, scales poorly past ~5k pods |
| nftables (newer) | nft rules | Slightly better than iptables |
| eBPF | TC-attached eBPF programs on each veth | Best, also replaces kube-proxy |
| IPVS | LVS rules for service routing | Mostly historical |

The eBPF mode is Calico's modern path; it sidesteps iptables for policy and (optionally) kube-proxy for services. See §16 of the roadmap (ch 16 for Cilium's eBPF) — Calico eBPF is conceptually similar but doesn't replace the entire kube-proxy fanout pattern as comprehensively.

### Calico-specific CRDs

- `IPPool`: a chunk of the cluster's address space (e.g., `10.244.0.0/16`). Multiple pools allow you to mix encapsulated and unencapsulated traffic.
- `IPAMBlock`: a leased CIDR block on a node (§7).
- `BGPPeer` / `BGPConfiguration`: who to peer with, AS numbers, route reflectors.
- `NetworkPolicy` (Calico's enhanced version, supports more than core K8s).
- `GlobalNetworkPolicy`: cluster-wide policy, not namespaced.
- `HostEndpoint`: lets Calico enforce policy on **host network interfaces** as well as pods. Effectively a host firewall managed by Calico.
- `Tier`: order of policy evaluation. Lets you say "platform team's policies always evaluate before tenant policies."

### When Calico is the right answer

- You need BGP into your fabric.
- You need a host firewall (HostEndpoint) managed alongside pod policy.
- You need GlobalNetworkPolicy and tiered policy ordering.
- You're on-prem and want the most flexibility.
- You don't want to commit to eBPF.

### When it isn't

- You already chose Cilium for the eBPF / Hubble / service mesh story (ch 16). They overlap.
- You're on a managed cloud and want the simplest path (use the cloud's native CNI).

---

## 12. Cilium Overview (the eBPF Path)

Chapter 16 is the deep dive; this section is the brief mention required to make the CNI landscape make sense.

Cilium's bet is that **iptables doesn't scale**. At 5000 services and 10000 pods, iptables-mode kube-proxy has tens of thousands of rules, every packet traverses them linearly, and reconcile time on Service changes becomes minutes. Cilium replaces all of it with eBPF programs attached to TC (traffic control) hooks on each veth and to cgroup sockets.

```
                pod A                          pod B
              ┌──────┐                       ┌──────┐
              │ eth0 │                       │ eth0 │
              └───┬──┘                       └───┬──┘
                  │ veth                         │ veth
                  ▼                              ▼
        ┌──────────────────┐           ┌──────────────────┐
        │ host vethXXX     │           │ host vethYYY     │
        │   ┌────────────┐ │           │   ┌────────────┐ │
        │   │ TC ingress │◄┘           └───┤ TC ingress │ │
        │   │ eBPF prog  │                 │ eBPF prog  │ │
        │   └────────────┘                 └────────────┘ │
        │   ┌────────────┐                 ┌────────────┐ │
        │   │ TC egress  ├─────────────────┤ TC egress  │ │
        │   │ eBPF prog  │                 │ eBPF prog  │ │
        │   └────────────┘                 └────────────┘ │
        └──────────────────┘           └──────────────────┘
                  │                              │
                  └────────── eBPF maps ─────────┘
                              (identity → policy verdict cache,
                               endpoint lookup, service backends)

   Per-pod identity = hash(labels). Policy is expressed in terms of
   identities, not IPs. Match is a single map lookup per packet.
```

Capsule summary (ch 16 has the full breakdown):

- **No iptables for pod/service traffic.** eBPF programs match in O(1) per packet.
- **Identity-based policy**: each pod's labels are hashed into an integer "identity"; policy maps identity → allowed identities. Source IP → identity, destination → identity, allow/deny.
- **Native kube-proxy replacement.** Service VIPs are translated by socket-level eBPF (when the pod calls `connect()`, the eBPF replaces the destination with an actual backend pod IP) or by TC-level NAT at the veth.
- **Hubble**: an observability layer that emits flow events from the eBPF programs. Becomes the "what's happening in my network right now" tool.
- **Native overlay (VXLAN or Geneve) or native routing (BGP via FRR / direct routing).** Same matrix as Calico.

Why mention it here: Cilium *is* a CNI. It implements the spec, has its own `cilium-cni` binary in `/opt/cni/bin/`, uses `cilium-ipam`, and so on. The fact that everything beyond the IP allocation runs in eBPF rather than iptables is an implementation detail at the CNI-spec level, but a huge operational difference. Default for GKE Dataplane v2, default for many large EKS clusters, supported on AKS, common on bare-metal.

### The eBPF advantage in one number

Consider a cluster with 5000 Services × 10 endpoints each. kube-proxy iptables installs roughly:

```
   number of iptables nat rules ≈ 4 * services * endpoints
                                = 4 * 5000 * 10
                                = 200,000 rules
```

Every packet to a Service VIP traverses this table linearly (with some optimization). Reconcile time, when a new endpoint joins, requires `iptables-save | iptables-restore` of the entire ruleset — measured in seconds at this scale, sometimes tens of seconds. The "the entire cluster pauses Service updates for 30 seconds" failure mode is real.

Cilium's eBPF service map is a hash table: O(1) lookup, no linear scan, no full-table reconcile. Adding one endpoint is one map entry. Cluster Service-update lag drops from seconds to milliseconds.

### What Cilium gives up

Nothing comes free:

- **eBPF verifier limits**. Programs must terminate in finite time, fit in stack/instruction budgets. Very complex policies can hit verifier limits. Cilium's authors spend nontrivial effort working around verifier quirks.
- **Kernel version dependency**. Different features require different kernels. Cilium 1.14 wants 5.4+ for the basics, 5.10+ for many features, 5.15+ for the latest. Distros that lag on kernel versions (older RHEL, conservative Ubuntu LTS) constrain Cilium's feature set.
- **Visibility into "where did this packet go?"** is excellent (Hubble), but **"why did the eBPF program take this path?"** is harder than iptables, where you can `iptables -t nat -L -v -n` and read it.
- **Less of a "throw a single iptables rule" escape hatch.** If you want to add one allow rule manually, you're writing or editing eBPF maps, not running `iptables -A`.

---

## 13. Flannel: The Minimalist

Flannel is the original "just give me pod networking, please" CNI. ~5000 lines of Go. No policy. No fancy features. Just IPAM + a dataplane.

```
   Flannel design (per node):

   ┌─────────────────────────────────────────────────────┐
   │ flanneld (DaemonSet pod, hostNetwork=true)          │
   │   - watches the K8s API for Node objects            │
   │   - reads each node's spec.podCIDR                  │
   │   - configures local dataplane (vxlan / host-gw)    │
   │   - writes /run/flannel/subnet.env for the CNI bin  │
   └─────────────────────────────────────────────────────┘
                          │
                          │ subnet.env: FLANNEL_NETWORK=10.244.0.0/16
                          │             FLANNEL_SUBNET=10.244.7.1/24
                          │             FLANNEL_MTU=1450
                          ▼
   ┌─────────────────────────────────────────────────────┐
   │ /opt/cni/bin/flannel                                 │
   │   - reads subnet.env                                 │
   │   - delegates to /opt/cni/bin/bridge (or whatever)   │
   │   - bridge plugin handles veth + IPAM (host-local)   │
   └─────────────────────────────────────────────────────┘
```

The Flannel CNI plugin is a *wrapper*: it reads the subnet file and then invokes the bridge plugin with the right config. Flannel itself does very little; it leverages the reference plugins.

### Backends

Flannel's dataplane is pluggable via the `backend` field in its config:

| Backend | What it does |
|---|---|
| `vxlan` (default) | One VXLAN device per node, FDB entries to remote nodes |
| `host-gw` | Plain `ip route` (requires L2 adjacency) — §10 |
| `wireguard` | Encrypted overlay via WireGuard |
| `ipsec` | Encrypted overlay via IPSec ESP |
| `udp` | Userspace UDP encap (slow, mostly historical) |
| `alivpc`, `aws-vpc`, `gce` | Program the cloud's native routes directly |

The `aws-vpc` backend is interesting: it updates the EC2 VPC route table on each instance change, putting `10.244.X.0/24 → eni-XXX` into AWS's router. This achieves "no encapsulation" without BGP. Limited to 50 routes per VPC route table in the old days; AWS raised this to 100 then 250.

### Why pick Flannel

- **You want simplicity.** No CRDs to learn. Almost nothing to misconfigure.
- **You don't need NetworkPolicy.** Or you pair Flannel with Calico-policy-only (Canal — the historical name for this combo).
- **Small cluster, modest scale.** Flannel is fine up to a few hundred nodes.

### Why not

- **No NetworkPolicy enforcement.** Pods are flat-and-open. Pair with Calico if needed.
- **Limited debugging tools.** No `flannelctl status` equivalent.
- **Minimal community velocity.** Compared to Calico/Cilium, Flannel evolves slowly.

Source: `flannel-io/flannel`.

---

## 14. Weave Net: Mesh and Sunset

Weave Net (from Weaveworks, RIP 2024) was an early mesh-style CNI with two features that made it stand out:

1. **Gossip-based control plane.** Nodes discover each other through gossip; no central datastore needed. Every node knew the topology.
2. **Encryption by default.** Built-in IPSec on every inter-node link.

The dataplane was a userspace bridge (slow, the "sleeve mode") or a kernel fast-data-path using ODP/OVS (fastdp mode). Weave allocated IPs through a distributed allocator (a Highest Random Weight scheme) — no need for per-node CIDR carving.

Weaveworks (the company) closed in 2024; the project is now community-maintained but development has slowed. You'll still find it running in clusters built between 2017 and 2021. New deployments rarely pick Weave.

If you encounter Weave, the relevant knobs are:
- `--ipalloc-range`: cluster pool.
- `--password`: enables IPSec encryption.
- `weave status`: the operational CLI.

---

## 15. AWS VPC CNI: Native VPC IPs

EKS's default CNI. The aws-vpc-cni-k8s project (`aws/amazon-vpc-cni-k8s`). The design choice is unusual and worth understanding even if you don't run on EKS, because it shapes how you think about pod density on the cloud.

### How it works

AWS allows an EC2 instance to have multiple **Elastic Network Interfaces (ENIs)**, each of which can hold multiple **secondary private IPs**. Both ENI count and IPs-per-ENI are capped per instance type. The VPC CNI uses this directly: each pod gets one of the instance's secondary IPs.

```
   EC2 instance (m5.large)
   ┌─────────────────────────────────────────────┐
   │  Primary ENI (eth0)                         │
   │    Primary IP:    10.0.1.10  (instance IP)  │
   │    Secondary IPs: 10.0.1.20  ← pod A        │
   │                   10.0.1.21  ← pod B        │
   │                   10.0.1.22  ← warm         │
   │                   10.0.1.23  ← warm         │
   │                                              │
   │  Secondary ENI (eth1)                       │
   │    Primary IP:    10.0.1.30                 │
   │    Secondary IPs: 10.0.1.31  ← pod C        │
   │                   10.0.1.32  ← warm         │
   │                                              │
   │  Tertiary ENI (eth2)                        │
   │    ... etc                                  │
   └─────────────────────────────────────────────┘

   Pod A's veth → host eth0 (primary ENI), with policy routing
   so that source IP 10.0.1.20 always egresses via eth0.

   AWS VPC sees pod A as a real, fully-routable VPC IP.
```

### Components

- `aws-node` DaemonSet: runs the IPAM agent (`ipamd`) on every node. ipamd manages the ENI pool, pre-warms IPs, calls EC2 APIs to attach/detach.
- `/opt/cni/bin/aws-cni`: the CNI binary. On ADD, asks ipamd for an available IP, sets up the veth, configures policy routing.
- ENI configuration: each ENI gets policy routing so that traffic with source IP X always egresses via ENI Y. This avoids asymmetric routing through different ENIs.

### Pod density math

| Instance | ENIs | IPs per ENI | Max secondary IPs | Approx max pods |
|---|---|---|---|---|
| t3.medium | 3 | 6 | 17 (one is reserved for the node) | 17 |
| m5.large | 3 | 10 | 29 | 29 |
| m5.4xlarge | 8 | 30 | 234 | 234 |
| c5.18xlarge | 15 | 50 | 737 | 737 |

(One IP per ENI is the ENI's primary; pods get the rest.)

So a `m5.large` is capped at ~29 pods, regardless of memory/CPU headroom. This was the famous EKS pod-density complaint until 2021.

### Prefix delegation (CNI 1.18+)

Instead of attaching individual secondary IPs, attach **/28 IPv4 prefixes** to each ENI. Each /28 is 16 IPs, attached as one "slot". An ENI that previously held 10 secondary IPs can now hold ~10 prefixes × 16 IPs = ~160 pods.

The dataplane is unchanged — VPC still routes each /28 to the right ENI. The host pulls one /28 from the VPC subnet's available IPs, then sub-allocates 16 pod IPs from it locally without further API calls.

Enable via `ENABLE_PREFIX_DELEGATION=true` on the aws-node DaemonSet.

### Warm pool and pre-allocation

ipamd keeps a pool of pre-attached IPs. The pool is configured via:
- `WARM_IP_TARGET`: target free IPs to keep ready (default 1)
- `WARM_ENI_TARGET`: target free ENIs (default 1)
- `MINIMUM_IP_TARGET`: hard floor

Without warming, every pod creation would block on an EC2 `AssignPrivateIpAddresses` API call (hundreds of ms, sometimes throttled). With warming, allocations are instant.

### What you give up

- **Pod limit per instance is the most binding constraint** (not CPU/memory). Misconfiguring instance type → tiny pod ceiling.
- **VPC subnet must be large enough** for all pods × pod restarts × ENI overhead. Plan /20s, not /24s.
- **Cross-AZ traffic is expensive** because pods are now first-class VPC IPs in their AZ's subnet; routing across AZs hits AWS's data-transfer pricing (§22).

### What you gain

- **Pods can reach AWS managed services natively.** RDS, ElastiCache, MSK, NLBs — all see the pod IP, security groups apply at pod granularity.
- **No encapsulation tax.** Wire-rate networking.
- **VPC flow logs include pod traffic.** Auditable. (Also: pod IPs leak.)
- **Pod-level security groups** (EKS feature). Each pod can have its own SG.

---

## 16. Azure CNI: VNET-Native and Overlay

Azure has two CNI modes for AKS, plus a newer Cilium-powered overlay.

### Azure CNI (VNET-native)

Conceptually mirrors AWS VPC CNI. Each pod gets an IP from the VNET subnet, directly routable inside the VNET. The CNI plugin (`azure-vnet`) configures the host so that each pod's IP is reachable via the host's NIC.

```
   VNET 10.0.0.0/16
     subnet-aks: 10.0.0.0/22  (1022 IPs)
       node1: 10.0.0.10 (host) + pre-allocated pod IPs 10.0.0.20–10.0.0.50
       node2: 10.0.0.51 (host) + ...
```

Each pod uses a VNET IP, no NAT, full Azure-native reachability.

Constraints:
- **Subnet exhaustion is the killer.** Pre-allocation defaults: 30 IPs per node. 1022-IP subnet → 30 nodes. Pick subnets carefully.
- **VNET peering, ExpressRoute, and on-prem routes all see pod IPs.** Useful for hybrid; potentially noisy.

### Azure CNI Overlay

A more recent mode (GA 2023). Pods are on a **separate pod CIDR** (not from the VNET). Traffic between pods on different nodes is encapsulated; traffic from pods to the VNET is **NAT'd to the host's IP**.

```
   VNET 10.0.0.0/24       Pod CIDR 192.168.0.0/16 (overlay)
     node1: 10.0.0.10                 (pods 192.168.1.0/24 on node1)
     node2: 10.0.0.11                 (pods 192.168.2.0/24 on node2)

   pod-to-pod on different nodes: VXLAN-style encap
   pod-to-Azure-service: SNAT to node IP, exits via VNET like normal
```

Pros vs VNET-native:
- VNET subnet doesn't fill up.
- Pod density isn't constrained by subnet size.
- Higher cluster scale.

Cons:
- Pod IPs are not VNET-routable. Pods can't be the target of an Azure Load Balancer directly.
- Encapsulation cost (50 bytes, MTU subtraction).

### Cilium-powered Azure CNI Overlay

Microsoft's newer offering: Azure CNI Overlay with Cilium as the dataplane. Same overlay model, but eBPF instead of iptables underneath. Adds Hubble observability, kube-proxy replacement, identity-based policies.

This is becoming the recommended path for new AKS clusters at scale.

---

## 17. GKE Dataplane v2 and Other Cloud CNIs

### GKE Dataplane v2

Google's modern dataplane for GKE Standard and GKE Autopilot. Underneath: **Cilium**. The branding is Google's; the engine is Cilium.

```
GKE Dataplane v2 = Cilium + custom IPAM + GCE-aware networking
   - Native VPC routing via GCE network alias IPs
     (analogous to AWS secondary IPs, but tighter)
   - NetworkPolicy enforced by eBPF
   - kube-proxy replaced by eBPF (no iptables for services)
   - Hubble UI for observability
   - Anthos additions: multi-cluster service discovery, etc.
```

### Other cloud CNIs

- **GKE Routes-based mode** (legacy): pre-Dataplane v2; used host routes installed via the GCE API. Still around for older clusters.
- **OCI VCN CNI**: Oracle Cloud's, similar pattern to AWS VPC CNI (secondary IPs on VNICs).
- **AKS Kubenet**: the old default before Azure CNI. Uses host routes + NAT; very limited; deprecated.
- **OpenShift SDN / OVN-Kubernetes**: Red Hat's OpenShift CNIs. OVN-Kubernetes is the current default — uses Open Virtual Network for declarative networking, supports Multus natively, BGP, EVPN, multi-tenancy.

---

## 18. Writing a CNI Plugin From Scratch

The CNI spec is intentionally tiny so that writing a plugin is approachable. The canonical workflow:

1. Read `CNI_COMMAND` from env.
2. Read JSON from stdin.
3. Do the work (manipulate netns, call sub-plugins, talk to APIs).
4. Write JSON to stdout.
5. Exit 0 on success, nonzero on failure (with a structured error JSON on stdout — see spec).

### A minimal sysctl-applying plugin

A useful example: a plugin that applies pod-specific sysctl values inside the pod netns. Run it as a meta plugin (chain after the main).

```go
// File: cmd/k8s-sysctl/main.go (toy example, not production)
package main

import (
    "encoding/json"
    "fmt"
    "os"

    "github.com/containernetworking/cni/pkg/skel"
    "github.com/containernetworking/cni/pkg/types"
    cnv "github.com/containernetworking/cni/pkg/types/100"
    "github.com/containernetworking/cni/pkg/version"
    "github.com/containernetworking/plugins/pkg/ns"
)

type Conf struct {
    types.NetConf
    Sysctls map[string]string `json:"sysctls"`
}

func parseConf(stdinData []byte) (*Conf, *cnv.Result, error) {
    conf := &Conf{}
    if err := json.Unmarshal(stdinData, conf); err != nil {
        return nil, nil, fmt.Errorf("parse config: %v", err)
    }
    // PrevResult is required for a chained plugin
    if conf.RawPrevResult == nil {
        return nil, nil, fmt.Errorf("must be chained")
    }
    res, err := cnv.NewResultFromResult(conf.PrevResult)
    if err != nil {
        return nil, nil, err
    }
    return conf, res, nil
}

func cmdAdd(args *skel.CmdArgs) error {
    conf, prevResult, err := parseConf(args.StdinData)
    if err != nil {
        return err
    }

    netns, err := ns.GetNS(args.Netns)
    if err != nil {
        return fmt.Errorf("open netns %s: %v", args.Netns, err)
    }
    defer netns.Close()

    err = netns.Do(func(_ ns.NetNS) error {
        for k, v := range conf.Sysctls {
            path := fmt.Sprintf("/proc/sys/%s",
                replace(k, ".", "/"))
            if err := os.WriteFile(path, []byte(v), 0644); err != nil {
                return fmt.Errorf("set %s=%s: %v", k, v, err)
            }
        }
        return nil
    })
    if err != nil {
        return err
    }

    // Pass through the previous result unchanged
    return types.PrintResult(prevResult, conf.CNIVersion)
}

func cmdDel(_ *skel.CmdArgs) error { return nil } // sysctls vanish with netns

func cmdCheck(_ *skel.CmdArgs) error { return nil }

func main() {
    skel.PluginMainFuncs(skel.CNIFuncs{
        Add:   cmdAdd,
        Del:   cmdDel,
        Check: cmdCheck,
    }, version.All, "k8s-sysctl v0.1")
}

func replace(s, old, new string) string { /* trivial */ return s }
```

Place the compiled binary in `/opt/cni/bin/k8s-sysctl`, add it as the last element of the chain in `/etc/cni/net.d/10-mynet.conflist`:

```json
{
  "type": "k8s-sysctl",
  "sysctls": {
    "net.ipv4.tcp_keepalive_time": "300",
    "net.core.somaxconn": "65535"
  }
}
```

Every new pod now gets these sysctls applied inside its netns.

### Tooling

- **libcni** (Go) — the only sane way to invoke CNI from a runtime in code. `containernetworking/cni`.
- **cnitool** — a CLI wrapper around libcni for manual testing. `cnitool add <network> <netns>` runs your plugin against a real netns. Indispensable when developing.
- **plugins/pkg/skel** — the skeleton library that turns your `cmdAdd/Del/Check` funcs into a proper plugin binary. Handles env parsing, stdin reading, error JSON, etc.

### Testing patterns

1. `ip netns add testns`
2. `cnitool add mynet /var/run/netns/testns`
3. `ip netns exec testns ip addr` to verify
4. `cnitool del mynet /var/run/netns/testns` to clean up

In CI, you can spin up a kind cluster, configure your plugin in `/etc/cni/net.d`, and run integration tests against real pods.

---

## 19. MTU Math and the 8-Byte Outage

If there is one numeric value every CNI operator should burn into their head, it's the MTU. Misconfiguring it by a single byte produces the most frustrating failure mode in Kubernetes networking: small packets work, large packets don't, and the symptom looks like "TLS handshake hangs" or "HTTP/1.1 GET works but file uploads fail."

### The chain

```
   underlay path MTU      ← what the underlying network can carry end-to-end
            │
            ▼
        - encapsulation overhead  (50 B VXLAN, 20 B IPinIP, 80 B WireGuard, ...)
            │
            ▼
   effective MTU for the pod's eth0
            │
            ▼
   - TCP/IP headers (40 B for TCPv4)
            │
            ▼
   TCP MSS the pod can send without fragmentation
```

### Per-encap overhead reference

| Encapsulation | Overhead |
|---|---|
| None (BGP, host-gw, AWS VPC CNI) | 0 |
| IPinIP (IPv4 in IPv4) | 20 B |
| IPinIP (IPv4 in IPv6) | 40 B |
| VXLAN (IPv4) | 50 B (14 eth + 20 IP + 8 UDP + 8 VXLAN) |
| VXLAN (IPv6) | 70 B |
| GENEVE (IPv4, no options) | 50 B |
| GENEVE (IPv4, max options) | 50 + 252 B = 302 B (rare) |
| WireGuard | 80 B |
| IPsec ESP (transport) | 50–73 B |
| IPsec ESP (tunnel) | 70–93 B |
| GRE | 24 B |

### Common configurations

| Underlay | Encap | Pod MTU |
|---|---|---|
| 1500 (cloud VPC default) | none | 1500 |
| 1500 | VXLAN | 1450 |
| 1500 | IPinIP | 1480 |
| 1500 | WireGuard | 1420 |
| 9001 (AWS jumbo) | VXLAN | 8951 |
| 9001 | none | 9001 |
| 9001 | WireGuard | 8921 |
| 1450 (Azure VNet default!) | VXLAN over Azure | 1400 |

Azure VNet's default MTU is 1450, not 1500 — Azure inserts 50 bytes of its own encapsulation for its software-defined network. **If you configure your CNI for 1500 underlay MTU on Azure, you've already lost.** This is why AKS docs are emphatic about MTU.

### Why the failure is so painful

TCP works through a process called **Path MTU Discovery** (PMTUD): when a router along the path can't forward a packet because it's too big and the DF (Don't Fragment) bit is set, the router sends an ICMP "Fragmentation Needed" message back to the sender. The sender shrinks its MSS for that connection and retries.

The problem: **PMTUD requires ICMP to flow back end-to-end.** Many cloud environments, security groups, and Network Policies drop ICMP. The ICMP "frag needed" message gets dropped, the sender never finds out, and the connection hangs at the first packet larger than what fits.

Result: a TCP handshake (small packets — SYN, SYN-ACK, ACK) completes. The first byte of HTTP (small POST) works. The first 1400 bytes of a TLS server's "ServerHello" go through. Then the certificate chain (~3 KB), exceeding pod MTU, gets dropped, ICMP fails to reach the sender, and the client waits forever. The browser shows "loading"; the curl command shows nothing; the log shows "TLS handshake timeout."

### Mitigations

1. **Set the CNI's MTU correctly.** Most CNIs auto-detect the underlay MTU on startup (read `eth0`'s MTU, subtract their encap overhead). Verify on the node: `ip link show vxlan.calico` and `ip link show cni0` should show MTU 1450 (or whatever).
2. **Allow ICMP**, especially `ICMP frag-needed` (type 3, code 4) and IPv6 PTB (type 2). Cloud security groups must allow it.
3. **TCP MSS clamping.** A common defensive measure: on the host eth0 (or veth), install an iptables rule that forcibly rewrites the MSS in every outgoing SYN to a known-safe value.
   ```
   iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
   ```
   Many CNIs (Calico, Flannel, Cilium) install this by default.
4. **Test with `ping -M do -s 1472 <peer>`** before declaring success. `1472 = 1500 - 20 (IP) - 8 (ICMP)` — if this works, you can send full-MTU IP packets. Repeat with the encapsulated path to verify end-to-end.

### Real wreckage examples

- A pod-to-pod path that works for `kubectl exec ... wget google.com` (small response) but fails for `wget large-static-asset.example.com` (returns hundreds of KB). Cause: MTU misconfigured, ICMP filtered.
- A service mesh sidecar (Envoy) accepting connections but failing on the *backend* call when the backend returns a certificate. Cause: MTU OK for ingress, broken for inter-pod (the next hop has different MTU).
- VPN-connected on-prem network: pods route to the on-prem subnet via a WireGuard tunnel; the pod CNI is configured for 1500 MTU; the WireGuard tunnel only takes 1420; cross-VPN flows break. Fix: lower the CNI MTU to 1420 cluster-wide.

---

## 20. Dual-Stack IPv4/IPv6

Kubernetes has stable support for dual-stack since 1.21. Every component — CNI, kube-proxy, services, DNS — must support both families or the cluster runs in degraded single-stack mode.

### The model

A pod has:
- `pod.status.podIP`: a single string. **The primary IP.** Kept for legacy compatibility.
- `pod.status.podIPs`: a list of `{ ip }` objects, one per family, primary first.

Example:

```yaml
status:
  podIP: 10.244.1.42
  podIPs:
  - ip: 10.244.1.42
  - ip: fd00:1::42
```

The order of `podIPs` is determined by the order of `pod.spec.ipFamilies` (which defaults from the Service's `ipFamilyPolicy` / `ipFamilies`).

### Cluster-wide configuration

Set at install time:
- `--cluster-cidr=10.244.0.0/16,fd00:1::/64` (note the comma-separated dual CIDRs)
- `--service-cluster-ip-range=10.96.0.0/12,fd00:96::/108`

The controller manager allocates *two* `podCIDR` ranges per node — one per family.

### CNI requirements

- The IPAM plugin must allocate from both pools.
- The main plugin must configure both addresses on the pod's eth0.
- Routes for both families must be installed.

`host-local` IPAM supports dual-stack via multi-range config:

```json
"ipam": {
  "type": "host-local",
  "ranges": [
    [{ "subnet": "10.244.1.0/24" }],
    [{ "subnet": "fd00:1::/64" }]
  ]
}
```

Calico, Cilium, and Flannel (vxlan backend) support dual-stack. AWS VPC CNI supports dual-stack in IPv6-only mode within a dual-stack VPC.

### Gotchas

- **Single-stack and dual-stack nodes cannot mix.** A pod might be scheduled to a single-stack node and lose its IPv6 IP. The cluster must be uniformly configured.
- **Pods must be designed for it.** Apps that hard-code IPv4 socket creation (`AF_INET`) won't accept IPv6 connections. Many apps need recompilation or runtime flags.
- **DNS records.** CoreDNS must serve A and AAAA records for services. Most modern CoreDNS configs do; older ones need an update.
- **NetworkPolicy semantics.** Each rule applies to whichever family matches. A rule referencing an IPv4 CIDR doesn't restrict IPv6 traffic. Plan both.

---

## 21. Egress Patterns and SNAT

The four-rule model is silent about pod-to-external traffic. The pod has IP `10.244.1.42`. It tries to reach `github.com`. The answer must come back. But `10.244.1.42` is private — the internet can't route it back. What to do?

### Default: SNAT to node IP

Almost every CNI installs an iptables MASQUERADE rule:

```
iptables -t nat -A POSTROUTING -s 10.244.0.0/16 ! -d 10.244.0.0/16 -j MASQUERADE
```

Read: "for any packet with source in the pod CIDR going to a destination *not* in the pod CIDR, rewrite the source to the host's egress interface IP."

The destination sees traffic from the node's IP. The reply comes back to the node, conntrack remembers the mapping, the host rewrites the destination back to the pod IP, and the pod sees a normal reply.

This is the *only* place NAT happens in the Kubernetes networking model.

### NoSNAT for routable destinations

In cloud-native CNIs (AWS VPC CNI), pod IPs are real VPC IPs. They can reach VPC-internal targets (RDS, internal load balancers) natively. The CNI configures the SNAT to skip those:

```
iptables -t nat -A POSTROUTING -s 10.244.0.0/16 -d 10.0.0.0/16 -j ACCEPT
iptables -t nat -A POSTROUTING -s 10.244.0.0/16 ! -d 10.0.0.0/16 -j MASQUERADE
```

Or, for AWS VPC CNI specifically, `AWS_VPC_K8S_CNI_EXTERNALSNAT=true` disables SNAT for VPC destinations.

### Egress gateway: route via specific nodes

When you want pod-to-external traffic to look like it came from a *known IP* — for whitelisting at a third-party service, for compliance audit logs — you need an **egress gateway**: route all egress through a specific pool of nodes whose IPs you pin.

- **Calico Egress Gateway**: a dedicated pod (acts as a NAT exit) handles egress for a labeled set of namespaces. Traffic enters the gateway pod, exits via its host with that host's IP. The gateway nodes get static, allow-listed IPs.
- **Cilium Egress Gateway**: similar concept, eBPF-driven. The egress policy selects pods by label; matching traffic is steered to a gateway node before exiting.

These are essential when you have policies like "only IPs `203.0.113.10` and `.11` are allowed to call our partner API" — you put your egress gateway pods on those two host IPs.

### Egress with NetworkPolicy

NetworkPolicy can restrict egress at the pod level (which destinations a pod is allowed to talk to). Combined with egress gateways, this gives you fine-grained "this namespace only egresses to these external IPs via these gateway nodes" control. Covered in ch 20.

---

## 22. Cross-AZ Networking Costs

A practical reality nobody likes: in every major cloud, **traffic that crosses availability zone boundaries is charged**, even within a single VPC. The numbers (as of late 2025):

| Cloud | Cross-AZ data transfer cost |
|---|---|
| AWS | $0.01/GB in each direction (so effectively $0.02/GB total when both sides bill) |
| GCP | $0.01/GB |
| Azure | $0.01/GB |

A 1 Gbps continuous chatty workload across AZs:
```
1 Gbps × 86400 s/day × 0.125 GB/Gb = 10800 GB/day
× $0.01/GB                          = $108/day
                                    = $39,420/year
```

Or, for a more realistic always-on microservice cluster doing 100 Mbps cross-AZ steady-state: ~$3,942/year. Per service. Multiply by ten chatty services and the line item is real.

### Why it bites Kubernetes harder than VMs

- Pods are scheduled wherever there's room. Two pods that chat constantly may land in different AZs by accident.
- Services load-balance across all backend pods, including those in other AZs. Half your requests cross AZ boundaries on average.
- Without explicit topology hints, kube-proxy makes random choices.

### Mitigations

1. **Topology-aware routing** (ch 14): the Service controller hints to kube-proxy that endpoints in the same zone should be preferred. The hint is conservative: if the local zone doesn't have enough endpoints, it falls back to cross-AZ.
2. **Pod anti-affinity by zone**: keep replica pods spread, but keep client and server in the same zone. Combine with `internalTrafficPolicy: Local` on the Service to force same-node, falling back to same-zone.
3. **Zone-local caching**. CoreDNS NodeLocalCache, Memcached per-zone, etc. Reduce the *amount* of cross-AZ traffic instead of fighting the *cost per byte*.
4. **Pin chatty workloads** with `topologySpreadConstraints` + `topologyKey: topology.kubernetes.io/zone` to be deliberate about co-location.
5. **Single-AZ stateful workloads.** Postgres primary in zone-a, replicas in zone-b. Reads stay zonal; writes go cross-AZ but are limited.

### Detecting the leak

- AWS VPC flow logs filtered by `srcAZ != dstAZ`.
- GCP VPC flow logs with the `region/zone` field comparison.
- Service mesh tools (Istio, Linkerd) emit topology-aware metrics; sort traffic by `source_zone` × `destination_zone`.

### A real anecdote-shaped lesson

A team noticed their AWS bill increased by $30k/year after deploying a new microservice. Investigation: the new service called Redis 5000 times per second; Redis was a single-pod Service ClusterIP; Redis pod was in zone-a; the new service had pods spread across all three zones; two-thirds of all Redis traffic was cross-AZ. Fix: a NodeLocal Redis sidecar (or topology-aware Service routing). $30k/year vanished.

---

## 23. NetworkPolicy Enforcement (Forward Reference)

The `NetworkPolicy` resource type is core Kubernetes API, but **kubernetes itself does not enforce it**. Enforcement is the CNI's job. If you install Flannel and create a NetworkPolicy, *nothing happens* — Flannel ignores it. Pods can still talk freely.

Which CNI does what:

| CNI | NetworkPolicy enforcement |
|---|---|
| Flannel | none |
| Weave | yes (legacy, sparsely maintained) |
| Calico | yes (iptables or eBPF) |
| Cilium | yes (eBPF, identity-based) |
| AWS VPC CNI | partial (delegates to Calico when enabled, otherwise SG-for-Pods) |
| Azure CNI | yes (when Azure NetworkPolicy or Calico is enabled) |
| OVN-Kubernetes | yes (OVN rules) |

Calico enforces by translating `NetworkPolicy` selectors into iptables rules per pod (one chain per pod, jumps from FORWARD). Cilium enforces by translating selectors into identity sets and storing them in eBPF maps — at packet time, look up source identity and destination identity in a 2D map for the allow/deny verdict.

The deep dive on policy semantics, default-deny patterns, GlobalNetworkPolicy, AdminNetworkPolicy, DNS-based egress, and zero-trust east-west is ch 20. For now: remember that **picking a CNI is also picking a NetworkPolicy engine**, and a cluster without policy enforcement is a flat trust boundary.

---

## 24. CNI Version Compatibility and the Runtime/CNI Contract

The CNI spec has three versions in wild use:

| Version | Released | Notable |
|---|---|---|
| 0.3.0 / 0.3.1 | 2017 | First widely-deployed version; the `prevResult` chaining contract |
| 0.4.0 | 2018 | Added `CHECK` command |
| 1.0.0 | 2020 | Stable. Renamed Result format. Most current CNIs |
| 1.1.0 | 2023 | Added `GC`, `STATUS` |

### How the runtime picks the version

The conflist file declares `cniVersion: "1.0.0"`. The runtime (containerd's CRI plugin via libcni) reads this and:
- Verifies its libcni supports that version. If not, error.
- Invokes each plugin in the chain with the same `cniVersion`.
- Each plugin announces via `VERSION` command what versions it supports.
- A negotiation picks the highest mutually supported.

If your conflist says 1.0.0 but a plugin in the chain only supports 0.4.0, the runtime errors at plugin discovery time, before any pods can start. This is a common upgrade footgun: pull a newer `portmap` binary that bumped to 1.0.0, but `bridge` is still 0.4.0 from an older release — chain fails.

### Backwards compatibility

The Result format changed between 0.3 and 1.0. libcni and the plugin SDK auto-convert between formats. Plugins written for 0.3 should still work; plugins written for 1.0 reading prevResult from a 0.3-producing plugin will see an auto-upgraded format. The community has been careful about this.

### Who calls CNI: runtime, not kubelet

This is a frequent point of confusion. **kubelet does not directly invoke CNI plugins.** Kubelet calls CRI (`RunPodSandbox` / `StopPodSandbox`); the runtime invokes CNI. The chain is:

```
kubelet  →  CRI gRPC (RunPodSandbox)  →  containerd cri plugin
            →  cni library (containernetworking/cni/libcni)
                →  /opt/cni/bin/<plugin>  (fork + exec)
```

containerd ships with a built-in CNI module (`containerd/internal/cri/server/`); CRI-O has its own integration with the same libcni. Both end up invoking the same on-disk plugin binaries from `/opt/cni/bin/`.

Implication: when CNI is "broken," you debug the runtime's logs (`journalctl -u containerd`), not kubelet's. Kubelet just sees "RunPodSandbox failed" with whatever error containerd surfaces.

---

## 25. Pod IP Lifecycle and Idempotency

A pod's IP comes into existence on `RunPodSandbox` and is released on `StopPodSandbox`. The detailed flow:

```
   Pod created (apiserver) → spec.nodeName set
        │
        ▼
   kubelet starts pod worker → RunPodSandbox via CRI
        │
        ▼
   containerd creates pause container with new netns
        │
        ▼
   containerd invokes CNI ADD
        │ → IPAM allocates IP (10.244.1.42)
        │ → main plugin creates veth, assigns IP
        │ → meta plugins (portmap, bandwidth) configure
        │
        ▼
   containerd stores Result in /var/lib/cni/results/<key>
        │
        ▼
   kubelet sees PodSandboxStatus → patches pod.status.podIP
        │
        ▼
   ... pod runs ...
        │
        ▼
   Pod deleted (apiserver) → kubelet drains, then StopPodSandbox
        │
        ▼
   containerd reads cached Result, invokes CNI DEL
        │ → meta plugins tear down (reverse order)
        │ → main plugin deletes veth
        │ → IPAM releases IP
        │
        ▼
   containerd deletes the cached Result, exits sandbox
```

### Why DEL must be idempotent

Several things can cause DEL to run twice (or more) for the same pod:

1. **Kubelet restart during pod deletion.** The pod was marked for deletion; kubelet restarted; on resume, it sees a pod sandbox that should be gone and calls StopPodSandbox again. CNI DEL is called again.
2. **Containerd restart.** Same flow on the runtime side.
3. **Node reboot.** The pod's sandbox is gone; the entire pod will be torn down and recreated. Some CNIs run a "stale entry cleanup" at startup, which is morally CNI DEL.
4. **Manual operator action.** `crictl rmp <sandbox-id> --force` invokes DEL.

A plugin that errors on "this IP is not allocated" or "this veth doesn't exist" because it was already removed during the first DEL breaks the orchestration. The CNI spec explicitly says:

> Plugins SHOULD ensure that operations are idempotent (e.g. ADD should not error if the network is already configured, DEL should not error if the network is not configured).

In practice, plugins should:
- On DEL: ignore "not found" errors when removing interfaces.
- On DEL: ignore "address not allocated" errors when releasing IPs.
- On ADD: detect "already exists" and either return the existing result or replace it.

### What happens when DEL fails

If DEL leaves an IP in the IPAM database, it leaks. Eventually you exhaust the pool and new pods can't get IPs. Investigation:

```
$ ls /var/lib/cni/networks/k8s-pod-network/ | wc -l
# count of currently-allocated IPs by the host-local plugin

$ crictl pods | wc -l
# count of running sandboxes on this node

# huge mismatch (e.g. 200 allocated IPs, 30 sandboxes) = leak
```

For host-local IPAM, the fix is manual: stop pods, delete stale files from `/var/lib/cni/networks/`, restart. For Calico/Cilium, the controller-side allocator garbage-collects orphans periodically.

### Pod IP reuse and the conntrack gotcha

A subtle, mostly-invisible operational concern: pod IPs are aggressively recycled. Pod A dies at 12:00:00, releases IP `10.244.1.42`. Pod B is created at 12:00:05 and gets the same `10.244.1.42`. But: the host's conntrack table still has entries for connections to "10.244.1.42" — pointing at the now-dead pod A.

Most cases this is harmless because pod A is gone, the entries time out. The pathological case:

- Long-lived TCP connection from outside the cluster (via a LoadBalancer Service) was open to pod A.
- Pod A dies, IP recycled to pod B.
- Conntrack still tracks the connection, refers to "10.244.1.42:8080".
- A new packet for that connection arrives, conntrack matches, delivers to "10.244.1.42:8080" — which is now pod B.
- Pod B receives a packet for a TCP connection it knows nothing about. Replies RST. Client gets RST.

Mitigations:
- **kube-proxy's `--conntrack-tcp-be-liberal`** to be less aggressive about reusing conntrack entries.
- **TCP_USER_TIMEOUT** in the client app so dead connections detect quickly.
- **Conntrack flush on pod delete**: some CNIs (Calico, Cilium) flush conntrack entries matching the deleted pod's IP. Confirm: `conntrack -L -d 10.244.1.42` before and after a pod restart.

### Pod IP lifecycle vs Pod object lifecycle

The pod *object* lives in etcd; the pod *IP* lives on a node. Two timelines that mostly overlap but can drift:

- A pod that's `Terminating` for `terminationGracePeriodSeconds` (e.g., 30 s) still **holds its IP**. EndpointSlice removes it from Service load-balancing, but other pods that already cached the IP can still send to it. The pod's containers run during this window.
- A pod object can be **force-deleted with `--grace-period=0 --force`** while the kubelet's sandbox is still running. The apiserver "forgets" the pod; kubelet eventually catches up and runs CNI DEL. During the gap, the IP is held without an authoritative owner.
- A node that becomes `NotReady` doesn't release pod IPs — the kubelet isn't running, can't call CNI DEL. The pod-eviction-controller (NodeLifecycleController) deletes the pod objects after `pod-eviction-timeout` (5 minutes default), but the IPs on the dead node remain allocated until the IPAM datastore is reconciled. This is why Calico's `calico-kube-controllers` includes a "stale allocation cleaner".

These edges matter at production scale: a 100-node cluster with one bad node will quietly accumulate orphan IPs over weeks.

---

## 26. Static IPs, Pinned IPs, and Pod Annotations

Standard Kubernetes does *not* allow specifying a pod's IP. The PodSpec has no `spec.podIP` field. This is by design — pods are ephemeral, IPs are fungible.

But several CNIs offer an escape hatch via annotations:

### Calico: pinning an IP

```yaml
metadata:
  annotations:
    cni.projectcalico.org/ipAddrs: '["10.244.7.42"]'
```

Calico's IPAM honors this annotation and assigns the requested IP if available. If unavailable (in use, outside any pool, etc.), pod creation fails.

### Calico: choosing an IP pool

```yaml
metadata:
  annotations:
    cni.projectcalico.org/ipv4pools: '["pool-blue"]'
```

Pod gets an IP from `pool-blue` instead of the default pool. Useful for "this namespace's pods should land in a specific address range" patterns.

### Cilium: similar pattern via labels

Cilium can route IPAM by `io.cilium.podlabels.k8s.io/policy=app` and other label-based pool selectors. The annotation `io.cilium.podannotations.k8s.io/ipv4-pool-name` directly picks the pool.

### When you'd want a static IP

- Stateful databases that must register with a specific IP in an external system.
- License servers that key on a specific IP.
- Workloads that pre-existed Kubernetes and have hard-coded IPs elsewhere.

### When you wouldn't

- Anywhere a Service abstraction works. Use the Service VIP, not a pinned pod IP.
- Anywhere autoscaling matters. Pinned IPs cap replica count to 1.
- Anywhere disaster recovery means re-scheduling. The IP becomes unavailable if the original pool runs out.

Treat static pod IPs as a "break glass" — a last resort, with operational debt attached.

---

## 27. Multus: Multiple Interfaces per Pod

Kubernetes assumes one CNI per cluster: one pod = one eth0 from one plugin chain. For some workloads — telco NFs, SR-IOV-accelerated apps, multi-network appliances — you need multiple interfaces per pod, possibly from different CNI providers.

**Multus** (`k8snetworkplumbingwg/multus-cni`) is a "meta-CNI" that delegates to other CNIs. The cluster's primary CNI is configured normally; Multus is configured as the default-default and is what containerd actually exec's. Multus then:

1. Calls the **default network plugin** (the regular CNI: Calico, Cilium, Flannel) to create the pod's primary eth0.
2. Reads the pod's annotations:
   ```yaml
   metadata:
     annotations:
       k8s.v1.cni.cncf.io/networks: macvlan-conf,sriov-net@net1
   ```
3. For each requested attachment, finds the matching `NetworkAttachmentDefinition` CRD, which contains *another* CNI conflist.
4. Invokes that CNI to create a second interface (`net1`, `net2`, ...) in the pod.

Result: a pod with `eth0` (cluster Pod IP, regular CNI) plus `net1` (a macvlan interface from the host), plus `net2` (an SR-IOV virtual function). Each interface satisfies independent requirements.

### NetworkAttachmentDefinition

```yaml
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: macvlan-conf
spec:
  config: |
    {
      "cniVersion": "0.3.1",
      "type": "macvlan",
      "master": "eth1",
      "mode": "bridge",
      "ipam": {
        "type": "host-local",
        "subnet": "192.168.50.0/24"
      }
    }
```

### Use cases

- **Telco** (5G UPF, IMS): user-plane traffic on a high-throughput SR-IOV interface; control plane on the cluster CNI.
- **Storage**: a Ceph cluster pod with a dedicated network for replication separate from public traffic.
- **Service-chain NFV**: a pod that bridges two networks.

### Gotchas

- Multus only handles the *invocation*. Address management, MTU, policy enforcement on the secondary interfaces are the responsibility of whichever CNI you delegated to.
- The primary interface's IP is what Services route to. Pods accessible via Services use eth0 only.
- Many cluster tools don't understand multi-interface pods. Service mesh sidecars typically intercept eth0 traffic; secondary interfaces bypass.

---

## 28. SR-IOV and Kernel-Bypass Networking

For applications where kernel networking is the bottleneck (40+ Gbps line rate, microsecond latencies), the kernel itself becomes overhead. The escape is to give the pod direct access to a hardware NIC.

**SR-IOV** (Single Root I/O Virtualization) splits a physical NIC into multiple **Virtual Functions (VFs)**. Each VF appears to the kernel as its own PCI device but is implemented in hardware on the NIC. A VF can be assigned to a pod, and the pod's traffic bypasses the host's kernel stack entirely.

```
   ┌──────────────────────────────────────────────┐
   │  Physical NIC                                │
   │  ┌──────────────┐                            │
   │  │ Physical Fn  │ ← controlled by the host   │
   │  └──────────────┘                            │
   │  ┌──────────────┐  ┌──────────────┐          │
   │  │ Virtual Fn 1 │  │ Virtual Fn 2 │  ...     │
   │  │ ↓ assigned   │  │ ↓ assigned   │          │
   │  │ to pod A     │  │ to pod B     │          │
   │  └──────────────┘  └──────────────┘          │
   └──────────────────────────────────────────────┘
```

The mechanics:

1. The SR-IOV device plugin (`k8snetworkplumbingwg/sriov-network-device-plugin`) advertises VFs as Kubernetes resources (`intel.com/sriov_node_a = 16`).
2. A pod requests a VF in its resource limits.
3. The SR-IOV CNI (chained via Multus) attaches the VF to the pod's netns.
4. The pod sees a real network interface with all the NIC's offloads available.

Pods using SR-IOV can do DPDK or AF_XDP for kernel-bypass. They escape Linux's `sk_buff` overhead entirely.

Tradeoffs:

- **Hardware bound.** The pod is pinned to the node with the NIC.
- **No live migration.** SR-IOV state can't be checkpointed.
- **NetworkPolicy doesn't apply on the SR-IOV interface** (it's not on the kernel path).
- **Operational complexity.** Driver pinning, firmware versions, NUMA alignment.

Used by: telco, HPC, high-frequency trading. Not for general workloads. Ch 10 (device plugins) covers the resource model in more depth.

---

## 29. Observability: Metrics, Counters, conntrack

Operating CNI is mostly invisible until it isn't. The signals to watch:

### Per-CNI metrics

- **Calico**: `calico-node` exposes `felix_*` Prometheus metrics (`felix_route_table_list_seconds`, `felix_active_local_endpoints`, `felix_iptables_save_seconds`). `calico-kube-controllers` exposes IPAM-block utilization.
- **Cilium**: extensive `cilium_*` metrics. `cilium_drop_count_total`, `cilium_bpf_map_ops_total`, `cilium_endpoint_count`, `cilium_policy_endpoint_enforcement_status`. Plus Hubble flow-level metrics.
- **Flannel**: minimal. `/metrics` on the flanneld daemon shows backend state.
- **AWS VPC CNI**: ipamd exposes metrics: `awscni_assigned_ip_addresses`, `awscni_total_ip_addresses`, `awscni_eni_allocated`, `awscni_aws_api_latency_ms`.

### Kernel-level metrics

Independent of which CNI you run:

- **Per-interface byte/packet counters**:
  ```
  $ ip -s link show veth34
  $ ip -s link show cni0
  $ ip -s link show vxlan.calico
  ```
  Look at RX/TX errors, drops, overruns. Persistent drops on `vxlan.calico` often mean MTU issues; drops on a pod veth often mean policy denies.

- **conntrack table size**:
  ```
  $ sysctl net.netfilter.nf_conntrack_count
  $ sysctl net.netfilter.nf_conntrack_max
  ```
  If `count` approaches `max`, the kernel starts dropping new connections. Symptom: random connection refused / timeouts. Tune `nf_conntrack_max` up (kernel default is too low for busy nodes — often 256K, should be 1M+).

- **iptables rule count**:
  ```
  $ iptables -t nat -L -n | wc -l
  $ iptables-save | wc -l
  ```
  At thousands of services with Calico iptables mode + kube-proxy iptables, expect tens of thousands of rules. Each packet traverses them.

- **Tunnel up/down**:
  - WireGuard: `wg show` — latest-handshake per peer.
  - IPsec: `ip xfrm state`, `ipsec statusall`.
  - VXLAN FDB: `bridge fdb show dev vxlan.calico`.

### Common alerts

- **`calico_ipam_blocks_used / calico_ipam_blocks_total > 0.85`**: running out of IPAM blocks. Add an IPPool.
- **`awscni_assigned_ip_addresses / awscni_total_ip_addresses > 0.85`**: pod limit on this node about to hit; consider prefix delegation or larger instances.
- **`cilium_drop_count_total[5m] > X`**: policy drops or other failures. Investigate by reason label.
- **conntrack utilization > 80%**: connection limits about to bite.
- **iptables-restore latency** (Felix metric): if iptables save/restore takes > 5 s, the dataplane reconcile is lagging behind policy changes; switch to eBPF mode.

### Diagnostic command catalog

A short cheat sheet of commands every CNI operator should know:

```
# What CNI is actually running on this node?
ls /etc/cni/net.d/
cat /etc/cni/net.d/*.conflist

# What IPs are allocated by host-local?
ls /var/lib/cni/networks/k8s-pod-network/

# What is the cached result for a sandbox?
cat /var/lib/cni/results/k8s-pod-network-<sandboxid>-eth0

# What's on the bridge?
bridge link show
bridge fdb show
ip link show type bridge

# VXLAN devices and FDB?
ip -d link show type vxlan
bridge fdb show dev vxlan.calico

# All veth pairs on the host?
ip link show type veth

# Routes including pod CIDRs?
ip route show
ip route show table all | grep proto bird  # Calico
ip route show table all | grep proto bgp   # Generic

# Pod's view from outside its netns?
nsenter -t <pause_pid> -n ip addr show
nsenter -t <pause_pid> -n ip route show

# kube-proxy / Service rules?
iptables-save -t nat | grep KUBE-
ipvsadm -L -n        # if kube-proxy is in IPVS mode

# Cilium status?
kubectl -n kube-system exec ds/cilium -- cilium status
kubectl -n kube-system exec ds/cilium -- cilium endpoint list
kubectl -n kube-system exec ds/cilium -- cilium service list
kubectl -n kube-system exec ds/cilium -- hubble observe

# Calico status?
kubectl -n kube-system exec ds/calico-node -- calicoctl node status
kubectl -n kube-system exec ds/calico-node -- birdcl show protocols
kubectl -n kube-system exec ds/calico-node -- birdcl show route

# Flannel status?
kubectl -n kube-system logs ds/kube-flannel-ds
cat /run/flannel/subnet.env
```

These commands are the "first 5 minutes of an incident" toolkit. Memorize them.

### What to graph

A production observability dashboard for CNI should include:

1. **Per-node**: pod count, IP utilization (allocated / total per pool), conntrack utilization, iptables rule count (if applicable), VXLAN rx/tx packets and drops.
2. **Per-cluster**: total pods, total Services, total NetworkPolicies, BGP session count and state distribution.
3. **Latency**: time to CNI ADD (kubelet event delta from `PodScheduled` to `PodIP` set), time to NetworkPolicy programmed (Felix metric or Cilium metric).
4. **Errors**: CNI ADD failure count by reason, CNI DEL failure count, dataplane reconcile error count.

Most CNIs publish enough metrics to build this; what's missing is usually a Grafana dashboard with sensible thresholds. Build one.

---

## 30. CNI Failure Modes

The failure-mode taxonomy you'll memorize after a year on call.

### Pod stuck in `ContainerCreating`

Most common cause: CNI ADD failing. Diagnose:

```
$ kubectl describe pod <name>
Events:
  Warning  FailedCreatePodSandBox  ...  failed to setup network for pod ...:
      plugin type="calico" failed: ...
```

Look at containerd logs:

```
$ journalctl -u containerd | grep -i cni
```

Common sub-causes:
- IPAM exhausted (no IPs left in the pool).
- Datastore unreachable (Calico can't talk to apiserver/etcd).
- Plugin binary missing from `/opt/cni/bin/` (e.g., after node upgrade).
- conflist file invalid JSON.
- MTU detection failed during plugin startup.

### Pod IP not freed after deletion

Symptom: `host-local`'s `/var/lib/cni/networks/<net>/` has files for IPs that no running pod claims. Investigation:

```
$ ls /var/lib/cni/networks/k8s-pod-network/ | sort | head
$ crictl pods --no-trunc -o yaml | grep -A1 "podIp"
```

Compare. Stale entries → CNI DEL was never called or failed. For host-local, delete the stale files manually. For Calico, run `calicoctl ipam release --ip=<ip>` (or wait for the GC controller).

### Cross-node traffic broken after node reboot

Symptom: same-node pod-to-pod works, cross-node fails.

Causes:
- VXLAN FDB entries weren't restored. Restart calico-node / flanneld to re-program.
- Routes weren't restored (host-gw mode, no BGP). Look at `ip route`; the cluster-CIDR routes should be installed by the CNI.
- BGP session didn't re-establish. Check `birdcl show protocols all` in the calico-node pod.
- VXLAN UDP port blocked by a firewall (host-level firewalld, or external).

### MTU mismatch

Symptom: TCP handshake works, large transfers stall. See §19 in detail.

Quick diagnosis:
```
$ kubectl exec -it pod-a -- ping -M do -s 1472 pod-b-ip
PING ... 1472(1500) bytes of data.
ping: local error: message too long
```

That error tells you the path can't carry full-MTU packets. Lower the pod MTU.

### conntrack table full

Symptom: spontaneous connection refused, timeouts under load, dmesg shows `nf_conntrack: table full, dropping packet`.

Cause: too many open connections (legitimate, or a leak), too small `nf_conntrack_max`.

Fix:
```
$ sysctl -w net.netfilter.nf_conntrack_max=1048576
$ sysctl -w net.netfilter.nf_conntrack_buckets=262144
```

Persist in `/etc/sysctl.d/`.

### Pod can ping its own IP but nothing else

The veth pair is up but the host-side veth isn't attached to the bridge correctly, or the route from `cni0` to the rest of the host is broken. Look at `bridge link show` and `ip route`.

### Random pod-to-pod packet loss

Often: an MTU issue affecting only large frames (TCP small frames work, jumbo doesn't), or a conntrack TUPLE collision (rare), or an eBPF/iptables policy that's flapping (a CNI controller reconciling repeatedly because of resync churn).

### Two CNIs installed by accident

If `/etc/cni/net.d/` has multiple conflist files, the runtime picks the first one alphabetically. You may install a new CNI without removing the old, and your cluster suddenly switches at the next pod startup. Always clean up `/etc/cni/net.d/` to a single file when transitioning.

### Cilium and kube-proxy both running

Cilium can replace kube-proxy, but if both are installed, the iptables rules from kube-proxy and the eBPF rules from Cilium fight. Service routing becomes nondeterministic. Always either: install Cilium with `kubeProxyReplacement: true` *and* uninstall kube-proxy, or run Cilium in iptables-compatible mode.

---

## 31. Choosing a CNI: A Decision Table

| Criterion | Calico | Cilium | AWS VPC CNI | Flannel | Weave |
|---|---|---|---|---|---|
| **Maturity** | Very high | High | High (cloud-specific) | High | Decreasing |
| **Cloud / on-prem fit** | Both | Both | EKS only | Both | Both |
| **Default dataplane** | iptables (or eBPF, opt-in) | eBPF | Native VPC | iptables/vxlan | iptables/userspace |
| **NetworkPolicy** | Yes (rich) | Yes (rich, identity-based) | Limited (or via Calico) | No | Yes |
| **GlobalNetworkPolicy** | Yes | Yes | No | No | No |
| **kube-proxy replacement** | Optional (eBPF mode) | Yes (eBPF) | No | No | No |
| **BGP** | Yes (native) | Yes (FRR) | No | No | No |
| **VXLAN overlay** | Yes | Yes (or GENEVE) | N/A | Yes (default) | Yes (sleeve mode) |
| **IPinIP** | Yes | No | No | No | No |
| **Native cloud routing** | Yes (EKS+, partial) | Yes (cloud-aware) | Yes (the whole point) | Limited (cloud backends) | No |
| **Observability** | Felix metrics | Hubble (excellent) | Basic | Basic | Basic |
| **Service mesh integration** | Standalone | Cluster Mesh, Hubble | Via overlay | No | No |
| **Right for…** | Broad features, hybrid envs | eBPF-first, large clusters, observability | EKS, native VPC needs | Tiny clusters, learning | Legacy clusters only |

### Decision shortcuts

```
   Are you on EKS and the workload needs native AWS service access?
     → AWS VPC CNI (with Calico for policy if needed)

   Are you on GKE and want the supported path?
     → GKE Dataplane v2 (which is Cilium)

   Are you on AKS at scale?
     → Azure CNI Overlay (with Cilium dataplane recommended)

   On-prem / bare metal, BGP-capable fabric, broad feature set?
     → Calico

   On-prem / bare metal, want eBPF and modern observability?
     → Cilium

   Tiny cluster, just need pods to talk?
     → Flannel

   Brownfield cluster running Weave?
     → Plan migration; new clusters shouldn't pick Weave
```

The choice cascades into everything else. Picking Cilium is also picking eBPF maintenance burden, Hubble for observability, identity-based policy as the mental model, and "kube-proxy is dead" as a stance. Picking Calico is picking iptables (or eBPF) as the dataplane, IPAM-blocks as the allocator, BGP as the routing, and "Felix watches everything" as the architecture. These are big decisions; once made, they shape your cluster's operations for years.

---

## 32. Pitfalls

The mistakes you (and every team) will make on the first CNI deployment.

1. **Two conflist files in `/etc/cni/net.d/`**. You upgraded the CNI and the old config didn't get cleaned. The runtime picks the *first lexically*. Symptom: silent dataplane swap on next pod create. Fix: ensure exactly one conflist on every node.

2. **MTU misconfiguration**. Either the CNI's MTU is too high for the underlay (large packets dropped) or too low (wastes bandwidth on small fragments). The CNI's auto-detect is usually right on cloud VPCs, frequently wrong on hybrid networks (WireGuard tunnels, IPSec, etc.). §19.

3. **Node `podCIDR` exhaustion**. Cluster was sized for ~30 pods/node; some node hosts a busy DaemonSet plus 40 pods; IPAM runs out. Symptom: new pods can't schedule on that node. Fix: increase `node-cidr-mask-size` (cluster-wide) or migrate to a CIDR-block-based IPAM (Calico, Cilium).

4. **Expecting NetworkPolicy with Flannel**. Flannel doesn't enforce policy. Until you add Calico's policy-only mode or a sidecar enforcer, your "deny-all" policies are ignored and pods talk freely. §23.

5. **IPAM state loss on node disk wipe**. Reimaged a node; `/var/lib/cni/networks/` was wiped; host-local allocator now hands out IPs that other surviving pods *still hold*. Result: IP conflicts. Fix: use a datastore-backed IPAM (Calico, Cilium); for host-local, treat node-state as fragile.

6. **Calico BGP but the underlying network drops BGP packets**. You configured `BGPPeer`, the session never establishes, `birdcl show protocols` shows `Idle` forever, pods can't reach across racks. Fix: confirm the underlay accepts BGP on TCP/179 from the calico-node IPs, and that you have a valid AS number.

7. **AWS VPC CNI on a tiny instance type**. `t3.small` allows 4 secondary IPs total. Three Daemonsets + your app pod = node full. New pods unschedulable for "InsufficientIPs" reason. Fix: pick a beefier instance or enable prefix delegation.

8. **VXLAN blocked across firewalls**. Multi-region cluster, inter-region traffic crosses a cloud firewall that allows TCP but not UDP/4789 (or 8472). Symptom: half the pods unreachable. Fix: allow VXLAN, or switch dataplane.

9. **iptables explosion at scale**. 5000 services + 50000 endpoints + kube-proxy iptables + Calico policy iptables = hundreds of thousands of rules. Reconcile takes minutes. Symptoms: kube-proxy CPU pegged, Felix lag, slow Service updates. Fix: switch kube-proxy to IPVS / eBPF, switch Calico to eBPF mode, or migrate to Cilium.

10. **conntrack overflow**. Steady connection rate is well below the default `nf_conntrack_max`, but a burst (one bad job opening 200k connections) overflows. New connections drop. Fix: bump `nf_conntrack_max` and `nf_conntrack_buckets` proportionally, and add observability.

11. **Pod IPs leaking after node restart**. CNI didn't clean up cached results on graceful shutdown; on restart the IPAM thinks IPs are still in use. Most CNIs reconcile periodically, but the leak window is ugly. Fix: ensure your CNI runs a startup reconcile.

12. **Multus + service mesh confusion**. A pod has eth0 (cluster CNI) + net1 (SR-IOV). Service mesh sidecar intercepts eth0 traffic via iptables; net1 traffic bypasses. Mesh observability misses half the pod's traffic. Fix: explicitly configure the mesh to be eth0-only.

13. **Dual-stack misconfig**. Cluster claims dual-stack but kube-proxy is single-stack (older version). Services have ClusterIPv4 and ClusterIPv6 but kube-proxy only programs v4 rules. v6 connections fail. Fix: version skew check; upgrade kube-proxy.

14. **MASQUERADE on routable destinations**. AWS VPC CNI default config SNATs pod-to-pod traffic crossing VPCs (peered VPC). Lost source IP at the destination. Fix: `AWS_VPC_K8S_CNI_EXTERNALSNAT=true` or carefully configure the MASQUERADE rules.

15. **CNI plugin path inconsistency**. `/opt/cni/bin/` on one node has version 1.2 of `bridge`; another has 1.0. Both work, but a chained plugin spec at 1.1 fails on the older node. Pods schedule but networking is broken. Fix: ensure CNI binaries are managed by a DaemonSet that pushes consistent versions.

16. **MTU clamping interfering with PMTUD**. MSS clamping at the host doesn't help if a *later* hop has a smaller MTU. Clamp aggressively (e.g., to 1300) for safety, or rely on PMTUD with ICMP allowed everywhere. §19.

17. **Forgetting `hairpinMode`**. Pod tries to reach its own Service VIP; kube-proxy DNATs to the pod's own IP; the bridge drops the reflected frame because it would loop. Fix: `hairpinMode: true` on the bridge plugin (or the equivalent for your CNI). Some CNIs default this on, some off.

18. **NodePort + hostPort + portmap collisions**. Two pods on the same node both request hostPort 8080. Second pod fails to schedule with `port already in use`, or worse, the iptables rule conflict makes both unreachable. Fix: don't use hostPort (use Service NodePort), or schedule with pod anti-affinity on hostPort.

19. **Egress gateway pods not labeled correctly**. Egress gateway requires labeling specific nodes; you forgot one; traffic still SNATs to that node's IP, bypassing your allowlist. Compliance audit fails. Fix: validate egress policy with a synthetic test (curl an external service from each namespace, confirm source IP).

20. **NetworkPolicy creates "default deny" but missed a port**. Coredns can't be reached because UDP/53 wasn't in the egress allow list. Half the cluster's DNS fails. Symptom: `nslookup` from inside pods hangs. Fix: every cluster-default-deny policy needs a baseline allow for DNS, kube-apiserver, and observability sinks.

21. **AWS VPC CNI security-groups-for-pods on the wrong instance type**. SGPP requires nitro instances; if you're on non-nitro, the SG-per-pod feature silently no-ops. Pods think they're SG-isolated; they're actually open. Fix: confirm nitro support; verify with an actual cross-SG ping test.

22. **CNI version drift between nodes**. You upgraded the CNI DaemonSet but a few nodes were unreachable; they still have v1.10 of the CNI while the rest are v1.12. Cross-version traffic has subtle bugs (e.g., new label-based identity vs old IP-based). Fix: rolling-upgrade with a verification gate per node.

23. **Pod-to-Service traffic blackholed during CNI reconcile**. Calico is reconciling its iptables ruleset; for ~500 ms the rules are partially applied; some Service VIPs are blackholes. Symptom: 1-in-100 connections fail. Fix: use atomic rule replacement (newer Felix versions, or eBPF mode where this isn't an issue).

24. **Network policy doesn't apply to host network pods**. `hostNetwork: true` pods are in the host's netns; the CNI doesn't see them; NetworkPolicy doesn't enforce on them. They can talk to anyone, regardless of policy. Calico's `HostEndpoint` is the fix for this gap.

25. **DNS-based egress policy without a DNS-aware CNI**. You wrote `NetworkPolicy` with `to: domain: example.com`. Standard NetworkPolicy doesn't understand DNS. Only Cilium (via FQDN policies) or Calico Enterprise. On a plain CNI: silently does nothing.

---

## 33. TL;DR

**The four-rule Kubernetes networking model** is the only thing every CNI must satisfy: unique pod IPs, no-NAT pod-to-pod, no-NAT node-to-pod, and the IP a pod sees itself as is the IP others see. Everything else (overlays, BGP, eBPF, secondary IPs) is an implementation strategy.

**CNI is an executable interface, not a library.** A plugin is a binary in `/opt/cni/bin/` that receives JSON on stdin + a few `CNI_*` env vars and writes JSON on stdout. The container runtime (containerd's CRI plugin, CRI-O), not kubelet, invokes CNI plugins. A `conflist` defines a chain of plugins executed in order on `ADD`, reverse order on `DEL`. `DEL` must be idempotent because kubelet/containerd will re-issue it across restarts.

**Plugin categories**: *main* plugins create the interface (bridge, ptp, calico, cilium-cni, aws-cni), *IPAM* plugins assign IPs (host-local, calico-ipam, AWS-VPC, dhcp), *meta* plugins augment the result (portmap, bandwidth, tuning, firewall, sbr). The canonical dataplane is veth + bridge + host routing — Docker-style — and every other CNI is a variation on it.

**Three dataplane families**: *overlays* (VXLAN, GENEVE, IPinIP) encapsulate pod traffic inside node-to-node tunnels, work on any L3 underlay, cost 20–80 bytes of overhead; *underlays* (BGP, native VPC routing, host-gw) route pod CIDRs directly, zero encap tax, require an underlay that knows how to route them; *hybrids* mix the two (Calico VXLAN-with-BGP-control-plane is the common pattern).

**IPAM is the secret-sauce difference**: host-local keeps state on disk (fast, fragile), Calico's leased-block IPAM is datastore-backed (resilient, slightly slower first allocation), cloud-native CNIs (AWS VPC CNI, Azure CNI) use cloud-issued IPs (no encap, capped pod density, pod IPs are real VPC IPs).

**MTU is the silent killer**. Every encapsulation costs bytes: VXLAN −50, IPinIP −20, WireGuard −80. Misconfigure by even 8 bytes and small packets work while large ones drop, producing "TLS handshake hangs" failures. PMTUD requires ICMP everywhere; cloud firewalls love to drop it. Use MSS clamping defensively.

**Cross-AZ traffic costs $0.01/GB and adds up to tens of thousands of dollars per year** in chatty clusters. Topology-aware routing, zone-local caching, anti-affinity, and zone-pinning are the levers.

**NetworkPolicy is enforced by the CNI, not by Kubernetes itself.** Flannel doesn't enforce it. Calico (iptables or eBPF) and Cilium (identity-based eBPF) do. Picking a CNI is also picking your policy engine; pick consciously.

**Specific CNIs**:
- *Calico* — broadest features, BGP, multiple dataplanes, host firewall via HostEndpoint, eBPF mode optional.
- *Cilium* — eBPF-first, identity-based policy, kube-proxy replacement, Hubble observability; default for GKE Dataplane v2.
- *AWS VPC CNI* — pods get real VPC IPs via ENI secondary IPs; pod density capped by instance type unless prefix delegation is enabled.
- *Azure CNI* — VNET-native or Overlay (Cilium-powered).
- *Flannel* — minimalist; pick for small/learning clusters; no NetworkPolicy.
- *Weave* — sunsetting; legacy only.

**Common failure modes**: pod stuck `ContainerCreating` (CNI ADD failing), pod IPs leaking (`/var/lib/cni/networks/` orphans), cross-node broken after reboot (FDB/routes not restored), MTU misconfig (large packets dropped), conntrack table full (random connection refusals), two conflist files (silent dataplane swap), Calico+BGP-against-uncooperative-underlay (BGP sessions stuck Idle).

**Decision checklist**: on EKS → AWS VPC CNI (+ Calico for policy if needed). On GKE → Dataplane v2. On AKS at scale → Azure CNI Overlay (Cilium). On-prem with BGP → Calico. On-prem with eBPF + observability ambitions → Cilium. Tiny lab → Flannel. Migrate off Weave.

**The whole chapter in one sentence**: *Kubernetes specifies four pod-networking rules; CNI is a tiny executable interface; the plugin chain creates a veth, allocates an IP from some IPAM, optionally tunnels traffic across nodes (VXLAN/IPinIP) or natively routes it (BGP/native VPC), enforces NetworkPolicy if the CNI cares about that, and gets MTU right; almost every production incident is one of those steps misconfigured.*
