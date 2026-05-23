# DNS and CoreDNS

Every Kubernetes feature that says "use the service name" is secretly a DNS feature. `psql -h postgres` works because something resolved `postgres` to a ClusterIP. `kubectl exec ... -- curl http://api.svc.cluster.local` works for the same reason. Service discovery in Kubernetes is **not** a magic in-kernel thing; it is a perfectly ordinary unicast DNS server (CoreDNS) backed by a perfectly ordinary `/etc/resolv.conf` injected into every Pod by the kubelet, with one twist that makes the whole thing fast and one twist that makes the whole thing slow. This chapter is about both twists.

The fast twist is the **kubernetes plugin** inside CoreDNS: a watch-driven, in-memory cache of every Service, Endpoint, and Pod in the cluster, served back as A / AAAA / SRV / PTR records at sub-millisecond latency. The slow twist is **ndots:5**: the default Linux resolver behavior that turns one external lookup like `www.google.com` into six sequential DNS queries, five of which are guaranteed NXDOMAIN. Every staff engineer who has ever debugged "DNS is slow" in a Kubernetes cluster has met ndots, conntrack, and the parallel-resolver race in some combination. This chapter unpacks all three.

We will start from the **cluster DNS contract** — what kubelet writes to a Pod's `/etc/resolv.conf` and why — then walk through **CoreDNS's plugin chain** Corefile by Corefile, dissect the **kubernetes plugin's** watch loop and record synthesis, examine **headless Service** and **StatefulSet pod-DNS** semantics, install and tune **NodeLocalDNSCache**, scale CoreDNS itself, integrate **ExternalDNS** for cloud DNS-as-code, and finish with the canonical *"DNS is slow"* troubleshooting tree, performance tuning recipes, and the long list of pitfalls every operator eventually steps on.

This chapter sits between [ch 14 (Services and kube-proxy)](14-services-and-kube-proxy.md), which gives every Service a stable VIP, and [ch 17 (Ingress / Gateway / Mesh)](17-ingress-gateway-and-service-mesh.md), which routes external traffic *to* those VIPs. The headless-Service material extends [ch 13 (StatefulSet)](13-statefulset-deep-dive.md). The CNI side — how DNS packets actually reach CoreDNS — extends [ch 15 (CNI)](15-cni-and-pod-networking.md). The NodeLocalDNSCache section borrows conntrack and netfilter context from [ch 00 (Linux primitives)](00-linux-primitives-for-containers.md).

If you only remember one sentence: **CoreDNS is a plugin-chained authoritative server for `cluster.local` and a recursive forwarder for everything else, and the most expensive thing in your cluster's DNS bill is `ndots:5` plus a single CoreDNS replica plus no node-local cache.**

---

## Table of Contents

1.  [The Cluster DNS Contract](#1-the-cluster-dns-contract)
2.  [`/etc/resolv.conf`, Search Paths, and `ndots`](#2-etcresolvconf-search-paths-and-ndots)
3.  [Why `ndots:5` Is a Latency Trap](#3-why-ndots5-is-a-latency-trap)
4.  [CoreDNS Architecture](#4-coredns-architecture)
5.  [The Corefile: Server Blocks and Plugin Chains](#5-the-corefile-server-blocks-and-plugin-chains)
6.  [The `kubernetes` Plugin: Watches, Records, Synthesis](#6-the-kubernetes-plugin-watches-records-synthesis)
7.  [Service Records: A, AAAA, SRV, PTR, CNAME](#7-service-records-a-aaaa-srv-ptr-cname)
8.  [Headless Services and Per-Pod A Records](#8-headless-services-and-per-pod-a-records)
9.  [StatefulSet Pod DNS and Stable Identity](#9-statefulset-pod-dns-and-stable-identity)
10. [Pod Hostnames: `spec.hostname`, `spec.subdomain`, `hostAliases`](#10-pod-hostnames-spechostname-specsubdomain-hostaliases)
11. [`dnsPolicy` and `spec.dnsConfig`](#11-dnspolicy-and-specdnsconfig)
12. [`forward`, `cache`, `prometheus`, `errors`, `log`, `health`, `ready`, `autopath`](#12-forward-cache-prometheus-errors-log-health-ready-autopath)
13. [NodeLocalDNSCache](#13-nodelocaldnscache)
14. [Scaling CoreDNS](#14-scaling-coredns)
15. [ExternalDNS: DNS-as-Code for Cloud Providers](#15-externaldns-dns-as-code-for-cloud-providers)
16. [Performance Tuning](#16-performance-tuning)
17. [The "DNS Is Slow" Troubleshooting Tree](#17-the-dns-is-slow-troubleshooting-tree)
18. [Custom DNS: Stub Domains, Custom Resolvers, Split Horizon](#18-custom-dns-stub-domains-custom-resolvers-split-horizon)
19. [DNS-Aware Egress: Blocking, Splitting, Policy](#19-dns-aware-egress-blocking-splitting-policy)
20. [Observability: Metrics, Alerts, Audit](#20-observability-metrics-alerts-audit)
21. [CoreDNS Extensions: `k8s_external`, `pods`, Custom Plugins](#21-coredns-extensions-k8s_external-pods-custom-plugins)
22. [Pitfalls](#22-pitfalls)
23. [TL;DR](#23-tldr)

---

## 1. The Cluster DNS Contract

Before any line of Corefile, the **cluster DNS contract** is the part you have to internalize. It is a four-way agreement between (a) the kubelet running on each node, (b) the CoreDNS Deployment, (c) the kube-dns Service that fronts CoreDNS, and (d) the Pod itself. Every other piece of cluster-DNS behavior derives from this contract.

The contract in one paragraph: **The kubelet, when it admits a Pod whose `dnsPolicy` is `ClusterFirst` (the default), writes an `/etc/resolv.conf` into the Pod's mount namespace whose only `nameserver` is the ClusterIP of the kube-dns Service, whose `search` list includes `<namespace>.svc.cluster.local`, `svc.cluster.local`, `cluster.local`, plus the node's own `search` entries, and whose `options` line includes `ndots:5`.** That is the entire mechanism. Everything else — service discovery, headless Service resolution, ExternalName CNAMEs, StatefulSet pod-DNS — is a consequence of CoreDNS answering queries that match the records the kubernetes plugin synthesizes from its watch on the apiserver.

```
                ┌────────────────────────────────────────────────┐
                │ apiserver (ch 05)                              │
                │   Services + EndpointSlices + Pods (watches)   │
                └─────────────┬─────────────┬────────────────────┘
                              │             │
                  watch       │             │   watch
                              ▼             ▼
                ┌─────────────────────┐  ┌─────────────────────┐
                │ CoreDNS replica 1   │  │ CoreDNS replica 2   │
                │ kubernetes plugin   │  │ kubernetes plugin   │
                │ in-memory cache     │  │ in-memory cache     │
                └──────────┬──────────┘  └──────────┬──────────┘
                           │  EndpointSlice          │
                           ▼  (kube-dns Service)     ▼
                ┌────────────────────────────────────────────────┐
                │  Service "kube-dns" — ClusterIP 10.96.0.10     │
                │  (the cluster DNS VIP)                         │
                └─────────────────────┬──────────────────────────┘
                                      │  53/udp + 53/tcp
                                      ▼
                ┌────────────────────────────────────────────────┐
                │  Every Pod's /etc/resolv.conf points here      │
                │  (kubelet writes this on Pod admission)        │
                └────────────────────────────────────────────────┘
```

A few subtleties hide in that paragraph that you have to know:

- **The Service name `kube-dns` is hard-coded** by kubelet and by the migration history from the original `kube-dns` Deployment (now retired). CoreDNS *replaced* kube-dns, but the **Service** is still called `kube-dns` so the kubelet doesn't need to know which DNS server is actually behind it. See `pkg/kubelet/kubelet.go` for the `clusterDNS` flag plumbing; `pkg/kubelet/network/dns/dns.go` for the resolv.conf generator.
- **The ClusterIP `10.96.0.10`** (or whatever your `--service-cluster-ip-range` first /N is) is a convention, *not* a guarantee. The kubelet doesn't look it up; it is configured via the kubelet flag `--cluster-dns` (a list of IPs), and that flag is set by your cluster bootstrapper (kubeadm, kops, EKS bootstrap script, GKE startup-script, kind, k3s, etc.) to match the IP of the `kube-dns` Service.
- **The kubelet does not query CoreDNS.** It writes a static file; the Pod's resolver library (glibc, musl, Go's `net.DefaultResolver`, Java's `InetAddress`, etc.) is what actually issues DNS queries. CoreDNS sees those queries as anonymous UDP/TCP traffic on port 53.
- **Pods inherit the node's resolver behavior unless overridden.** With `dnsPolicy: Default`, the kubelet copies the node's `/etc/resolv.conf` verbatim into the Pod. With `dnsPolicy: ClusterFirst`, the kubelet writes the cluster-DNS resolv.conf instead. The full matrix is in §11.

### 1.1 The Contract in Source

The kubelet code that writes a Pod's resolv.conf lives in `pkg/kubelet/network/dns/dns.go`. The interesting function is `getPodDNS`, which returns a `PodDNSConfig` struct (`Servers`, `Searches`, `Options`) that downstream code marshals into a `/etc/resolv.conf`. Pseudocode:

```go
func (c *Configurer) getPodDNS(pod *v1.Pod) (*PodDNSConfig, error) {
    podDNSType, err := c.getPodDNSType(pod)  // ClusterFirst / ClusterFirstWithHostNet / Default / None
    // …
    switch podDNSType {
    case podDNSCluster:
        // assemble from c.clusterDNS, c.clusterDomain
        servers  = c.clusterDNS               // e.g. [10.96.0.10]
        searches = []string{
            fmt.Sprintf("%s.svc.%s", pod.Namespace, c.clusterDomain),
            fmt.Sprintf("svc.%s", c.clusterDomain),
            c.clusterDomain,
        }
        options  = []string{"ndots:5"}
        // then append host /etc/resolv.conf's searches (deduped & length-limited)
    case podDNSHost:
        // copy host /etc/resolv.conf
    case podDNSNone:
        // empty; rely on spec.dnsConfig
    }
    // merge spec.dnsConfig if present
    return assemble(servers, searches, options), nil
}
```

Notice three things that the contract *does not* include and that surprise people:

1. **No fallback to the node's nameserver.** A Pod with `dnsPolicy: ClusterFirst` does not have the node's upstream DNS in its resolv.conf. If CoreDNS is unreachable, the Pod cannot resolve anything, even `8.8.8.8` (which it would have to resolve via DNS first — wait, you don't; `8.8.8.8` is a literal IP, but `dns.google` is not). External resolution is **delegated** to CoreDNS, which forwards onward via its `forward` plugin (see §12.1).
2. **No DNSSEC.** Cluster DNS is unauthenticated; CoreDNS does not validate signatures on upstream responses by default. If your threat model includes a tampered upstream resolver, you must terminate DoT/DoH at CoreDNS (the `forward` plugin supports `tls://` upstreams) or run a separate validating resolver.
3. **No automatic IPv6.** Even on dual-stack clusters, the `clusterDNS` list controls which families are written. If you only configure `10.96.0.10`, IPv6-only Pods cannot resolve cluster DNS; you must also configure `fd00:96::a` (or your IPv6 equivalent) and the kube-dns Service must advertise both families (`ipFamilyPolicy: PreferDualStack`).

### 1.2 What the Pod Actually Sees

On a vanilla cluster, `cat /etc/resolv.conf` from inside a Pod looks like:

```
search default.svc.cluster.local svc.cluster.local cluster.local us-east-1.compute.internal
nameserver 10.96.0.10
options ndots:5
```

That's it. Three lines. The behavior of every DNS query in the cluster — including the ones that cause production incidents — is a consequence of those three lines plus the resolver library in the application's runtime.

---

## 2. `/etc/resolv.conf`, Search Paths, and `ndots`

The Linux resolver (POSIX-ish, defined by RFC 1034/1035 plus glibc-specific extensions) consults `/etc/resolv.conf` to decide how to resolve a name. Three lines matter: `nameserver`, `search`, and `options`. We unpack each.

### 2.1 `nameserver`

```
nameserver 10.96.0.10
```

The resolver sends queries to each `nameserver` entry in order, with timeout `options timeout:N` (default 5s) and retry `options attempts:N` (default 2). If `options rotate` is set, it round-robins; otherwise it sticks to the first.

The number of allowed nameservers is **three** (`MAXNS=3` in `<resolv.h>`). If you write more in resolv.conf, the resolver silently ignores entries past index 2. This matters because the *order* of nameservers determines failure mode: with two CoreDNS replicas behind one VIP, your Pods have effectively one nameserver and rely on kube-proxy's session-less LB to distribute queries across CoreDNS Pods.

```c
/* glibc-internal: include/resolv.h */
#define MAXNS    3
#define MAXDFLSRCH 3   /* default search depth */
#define MAXDNSRCH  6   /* maximum search depth */
```

### 2.2 `search`

```
search default.svc.cluster.local svc.cluster.local cluster.local us-east-1.compute.internal
```

The `search` list is the resolver's **suffix expansion list**. When the application calls `gethostbyname("postgres")`, the resolver may try `postgres.default.svc.cluster.local`, `postgres.svc.cluster.local`, `postgres.cluster.local`, `postgres.us-east-1.compute.internal`, and finally `postgres.` (the absolute query) before giving up. *May*, because the actual decision depends on `ndots` and on whether the name being queried is "absolute" (trailing dot).

Like nameservers, the search list has caps: **6 entries**, **256 characters total** (`MAXDFLSRCH` vs `MAXDNSRCH`; glibc raises the default cap to 6 since glibc 2.26+, and on modern systems it's effectively 6). musl has its own (smaller) caps. If you write more, entries are truncated.

The first entry the kubelet adds is `<namespace>.svc.cluster.local` so that a Pod in namespace `default` can call `postgres` and it resolves first as `postgres.default.svc.cluster.local` — i.e., the same-namespace Service. That's the "name a Service like a hostname" convenience, and it is *only* a convenience: the canonical FQDN is always available as `<svc>.<ns>.svc.cluster.local`.

### 2.3 `options ndots:N`

The most consequential line. `ndots:N` controls **when the resolver tries the search list versus the absolute name**.

The rule: if the name being looked up contains **fewer than `N` dots**, treat it as a *relative* name and apply the search list **before** trying the name as absolute. If it contains **`N` or more dots**, try it as absolute *first*, and only fall back to the search list on NXDOMAIN.

The Kubernetes default is `ndots:5`. Examples:

| Query              | Dots | `ndots:5` behavior                                                                 |
|--------------------|------|-------------------------------------------------------------------------------------|
| `postgres`         | 0    | search-list first: 5 tries (4 search entries + absolute), 1st often hits           |
| `api.default`      | 1    | search-list first                                                                  |
| `api.default.svc`  | 2    | search-list first                                                                  |
| `www.google.com`   | 2    | search-list first → 4 NXDOMAINs, then absolute → SUCCESS                           |
| `pod-0.svc.cluster.local` | 3 | search-list first                                                              |
| `a.b.c.d.e.f`      | 5    | absolute first (5 ≥ 5), succeed or NXDOMAIN                                       |
| `www.google.com.`  | 2 (trailing dot) | absolute only, no search expansion (trailing dot = fully qualified)    |

The choice of 5 was deliberate: the longest *intra-cluster* Service FQDN is `<svc>.<ns>.svc.cluster.local` = 4 dots. To make sure that intra-cluster names resolve through the search list (so `postgres` becomes `postgres.default.svc.cluster.local`), `ndots` has to be **strictly greater** than the dot count of those intra-cluster names. 5 > 4. QED.

But this means **anything with fewer than 5 dots will try the search list first** — including external names like `www.google.com` (2 dots) and `api.github.com` (2 dots). For external names, the search-list expansion is wasted work: every entry will return NXDOMAIN before the resolver finally tries the absolute name. We dig into this in §3.

### 2.4 Other resolv.conf options

- `options timeout:N` — per-query timeout in seconds. Default 5. CoreDNS clients should probably lower this to 1 or 2.
- `options attempts:N` — query retries per server. Default 2.
- `options rotate` — round-robin across nameservers.
- `options single-request` — issue A and AAAA queries sequentially over one socket (cheaper, slower).
- `options single-request-reopen` — same socket reuse policy, with reopen on each attempt; mitigates the parallel-resolver port-conflict race in `glibc < 2.10`. See §17.
- `options use-vc` — always use TCP for DNS queries.
- `options no-tld-query` — don't query single-label names; relevant if you have a flat `search`.
- `options edns0` — enable EDNS(0) for larger responses; default on modern glibc.

### 2.5 The Resolver Implementations Disagree

The above describes glibc. The runtimes you actually deploy do not all use glibc:

| Runtime               | Resolver               | Reads `/etc/resolv.conf`? | Honors `ndots`? | Honors `search`? |
|-----------------------|------------------------|---------------------------|-----------------|------------------|
| Linux + glibc apps    | glibc resolver          | yes                       | yes             | yes              |
| Alpine + musl         | musl resolver           | yes                       | yes (since 1.2.4) | yes            |
| Go (`net.DefaultResolver`) | Go pure-Go resolver | yes                       | yes             | yes              |
| Go with `GODEBUG=netdns=cgo` | cgo → glibc       | yes                       | yes             | yes              |
| Java (older JDK)      | JNDI / `InetAddress`    | depends on JNI hop        | partial         | partial          |
| Node.js               | c-ares + libuv          | yes                       | yes             | yes              |
| Python (`socket`)     | glibc (via `getaddrinfo`)| yes                      | yes             | yes              |
| Rust (`std::net`)     | glibc (via `getaddrinfo`)| yes                      | yes             | yes              |
| Rust + Tokio (default)| `trust-dns-resolver` or system | varies              | varies          | varies           |

Two notes that bite people:

- **musl < 1.2.4 did not implement `search` or `ndots` at all.** Pods built on `alpine:3.17` and earlier would issue *only* the absolute name. `postgres` would NXDOMAIN immediately because there is no record for the literal name `postgres` at the root.
- **Go's pure-Go resolver issues A and AAAA queries in parallel by default.** On dual-stack-disabled clusters this generates the (in)famous AAAA-NXDOMAIN burst (§17.3).

---

## 3. Why `ndots:5` Is a Latency Trap

This is the single most important section of the chapter, because it explains roughly 60% of all "DNS is slow" incidents in real Kubernetes clusters.

### 3.1 The Anatomy of a "Simple" External Lookup

Consider an application Pod in namespace `default` calling `https://www.google.com/`. The HTTP client calls the resolver with `www.google.com`. That name has 2 dots; `ndots:5` says "fewer than 5, search-list first."

```
Resolver sees: www.google.com (2 dots, < 5)
Search list: default.svc.cluster.local, svc.cluster.local, cluster.local, us-east-1.compute.internal
Strategy: try each search-suffix, then absolute

1.  Query: www.google.com.default.svc.cluster.local. (A and AAAA)
    CoreDNS: NXDOMAIN
2.  Query: www.google.com.svc.cluster.local. (A and AAAA)
    CoreDNS: NXDOMAIN
3.  Query: www.google.com.cluster.local. (A and AAAA)
    CoreDNS: NXDOMAIN
4.  Query: www.google.com.us-east-1.compute.internal. (A and AAAA)
    CoreDNS: forwards to /etc/resolv.conf's upstream → NXDOMAIN
5.  Query: www.google.com. (A and AAAA)
    CoreDNS: forwards → SUCCESS, returns A 142.250.190.36
```

That is **10 DNS queries** (each of 5 names, doubled for A+AAAA) to resolve one external name. With glibc's default `attempts:2`, retries could push it to 20 packets in the worst case. With `single-request` off (default), A and AAAA go in parallel and may collide on the same source port (§17.3). With node-local DNS missing, those 10 queries traverse iptables conntrack, get DNAT'd to CoreDNS Pods on possibly remote nodes, and each insertion into the conntrack table is a write that contends with every other DNS query.

```
Without NodeLocalDNS:
  Pod → veth → cni0 → iptables PREROUTING → KUBE-SERVICES → DNAT → CoreDNS Pod
  Every DNS query inserts a conntrack entry (proto=udp, dst=10.96.0.10:53)
  conntrack entries default to 30s nf_conntrack_udp_timeout

With ndots:5 + dual-stack-disabled + no NodeLocalDNS:
  1 application "lookup www.google.com" =
    5 search expansions × 2 families = 10 conntrack inserts × 30s TTL
  At 1000 lookups/sec/pod × 100 pods = 1M conntrack entries from DNS alone
  Default nf_conntrack_max is 262144 → conntrack overflow → packet drops
```

### 3.2 The Three Failure Modes

The `ndots:5` trap manifests in three distinct ways depending on the mix of dual-stack settings, NodeLocalDNS presence, CoreDNS scale, and conntrack settings:

**Failure mode A — slow p99**: Every external lookup gets the full search-list traversal. Each NXDOMAIN burns ~1ms (CoreDNS lookup + forward + miss). 5 expansions × 2 families = 10ms median added latency on every external resolve. Caches help on hot names; cold names see the full cost. p99 of `http_client.duration` grows because some fraction of requests do a fresh DNS lookup.

**Failure mode B — conntrack overflow**: At high QPS, the conntrack table fills with DNS UDP entries (default 30s TTL). New connections (any TCP/UDP, not just DNS) start being dropped. Symptoms: random connection timeouts, `nf_conntrack: table full, dropping packet` in dmesg, `nf_conntrack_count` near `nf_conntrack_max`. The fix is NodeLocalDNS (§13), which bypasses conntrack entirely via the `--no-conntrack` iptables rule and a TCP forward to CoreDNS.

**Failure mode C — CoreDNS overload**: CoreDNS Pods CPU-pin at ~80% serving NXDOMAINs. p99 of DNS responses climbs. EndpointSlice unhealthy markers flip CoreDNS Pods in and out of the kube-dns Service. Symptoms: `coredns_dns_request_duration_seconds_bucket` shows a tail moving right; cache-miss rate dominates. The fix is multi-pronged: lower ndots where possible (§3.3), add NodeLocalDNS, scale CoreDNS, enable negative caching (`cache 30 .` with `denial 9984 5`).

### 3.3 The Mitigations

There are four mitigations, in increasing order of invasiveness:

1. **Use trailing dots in source code** (`www.google.com.`). The trailing dot tells the resolver "this is absolute, do not search." This is the cheapest fix — but you have to own the code, and your config schemas have to allow trailing dots (Kubernetes Service annotations and many YAMLs do not).
2. **Lower `ndots` per Pod** via `spec.dnsConfig` (§11.2):
   ```yaml
   spec:
     dnsConfig:
       options:
       - name: ndots
         value: "2"
   ```
   This means external names with ≥ 2 dots (most of them) go absolute first. Intra-cluster short names (`postgres` with 0 dots) still search-expand. The risk: same-namespace short references like `mysvc.myns` (1 dot) no longer search-expand, so they need to be the FQDN. In practice, most apps use either `mysvc` (works) or `mysvc.myns.svc.cluster.local` (works) and `ndots:2` is safe.
3. **NodeLocalDNSCache** (§13). Doesn't eliminate the search expansions, but caches the NXDOMAINs locally at sub-millisecond cost and bypasses conntrack.
4. **`autopath` plugin** (§12.7) in CoreDNS. Instructs CoreDNS to *itself* walk the search path and return a CNAME chain, collapsing 5 queries into 1. Powerful but has caveats (must list every Pod by namespace, doesn't compose with multiple resolvers, has been deprecated-ish since the rise of NodeLocalDNS).

### 3.4 Real `dig` Output Illustrating the Trap

```
# From a Pod in namespace 'default', no NodeLocalDNS, default ndots:5
$ time getent hosts www.google.com
142.250.190.36  www.google.com

real    0m0.041s   ← 41ms for one "simple" lookup
user    0m0.003s
sys     0m0.005s

$ tcpdump -ni eth0 udp port 53 -tt | head -20
1700000000.001 IP 10.244.1.5.51234 > 10.96.0.10.53: A? www.google.com.default.svc.cluster.local.
1700000000.001 IP 10.244.1.5.51234 > 10.96.0.10.53: AAAA? www.google.com.default.svc.cluster.local.
1700000000.003 IP 10.96.0.10.53 > 10.244.1.5.51234: NXDOMAIN
1700000000.003 IP 10.96.0.10.53 > 10.244.1.5.51234: NXDOMAIN
1700000000.004 IP 10.244.1.5.51235 > 10.96.0.10.53: A? www.google.com.svc.cluster.local.
1700000000.004 IP 10.244.1.5.51235 > 10.96.0.10.53: AAAA? www.google.com.svc.cluster.local.
1700000000.006 IP 10.96.0.10.53 > 10.244.1.5.51235: NXDOMAIN
1700000000.006 IP 10.96.0.10.53 > 10.244.1.5.51235: NXDOMAIN
1700000000.007 IP 10.244.1.5.51236 > 10.96.0.10.53: A? www.google.com.cluster.local.
1700000000.007 IP 10.244.1.5.51236 > 10.96.0.10.53: AAAA? www.google.com.cluster.local.
1700000000.009 IP 10.96.0.10.53 > 10.244.1.5.51236: NXDOMAIN
1700000000.009 IP 10.96.0.10.53 > 10.244.1.5.51236: NXDOMAIN
1700000000.010 IP 10.244.1.5.51237 > 10.96.0.10.53: A? www.google.com.us-east-1.compute.internal.
1700000000.010 IP 10.244.1.5.51237 > 10.96.0.10.53: AAAA? www.google.com.us-east-1.compute.internal.
1700000000.029 IP 10.96.0.10.53 > 10.244.1.5.51237: NXDOMAIN  ← 19ms (upstream RTT)
1700000000.029 IP 10.96.0.10.53 > 10.244.1.5.51237: NXDOMAIN
1700000000.030 IP 10.244.1.5.51238 > 10.96.0.10.53: A? www.google.com.
1700000000.030 IP 10.244.1.5.51238 > 10.96.0.10.53: AAAA? www.google.com.
1700000000.040 IP 10.96.0.10.53 > 10.244.1.5.51238: A 142.250.190.36
1700000000.040 IP 10.96.0.10.53 > 10.244.1.5.51238: AAAA 2607:f8b0:4004:c1f::6a
```

20 packets. 41ms. Multiply by every external HTTP call your application makes.

Same lookup with `ndots:2`:

```
1700000010.001 IP 10.244.1.5.51301 > 10.96.0.10.53: A? www.google.com.
1700000010.001 IP 10.244.1.5.51301 > 10.96.0.10.53: AAAA? www.google.com.
1700000010.011 IP 10.96.0.10.53 > 10.244.1.5.51301: A 142.250.190.36
1700000010.011 IP 10.96.0.10.53 > 10.244.1.5.51301: AAAA 2607:f8b0:4004:c1f::6a
```

4 packets. 11ms. **3.7× faster, 5× fewer packets, fewer conntrack entries.**

---

## 4. CoreDNS Architecture

CoreDNS is a Go binary built around a **plugin chain model**. There is no built-in DNS logic for any particular zone; everything is a plugin. The binary at `github.com/coredns/coredns/coremain` does little more than parse the Corefile, instantiate the plugin chain per zone, and serve DNS over UDP/TCP (and optionally TLS, gRPC, HTTPS).

```
                            CoreDNS process
        ┌──────────────────────────────────────────────────────┐
        │  Server (one per listen address, per zone)            │
        │  ┌─────────────────────────────────────────────────┐  │
        │  │  Plugin chain (Corefile order, top-to-bottom)   │  │
        │  │                                                  │  │
        │  │  errors  ──►  log     ──►  health     ──►       │  │
        │  │  ready   ──►  prometheus  ──►  kubernetes ──►   │  │
        │  │  forward ──►  cache   ──►  loop  ──►  reload    │  │
        │  │  loadbalance                                     │  │
        │  └─────────────────────────────────────────────────┘  │
        │                                                       │
        │  Each plugin is a Handler:                            │
        │    type Handler interface {                           │
        │        ServeDNS(ctx, w, r) (rcode, error)             │
        │        Name() string                                  │
        │    }                                                  │
        │                                                       │
        │  Plugins call h.Next.ServeDNS(…) to fall through      │
        │  or w.WriteMsg(reply) to short-circuit.               │
        └──────────────────────────────────────────────────────┘
                                  │
                                  ▼
              UDP 53 · TCP 53 · DoT 853 · DoH 443 · gRPC
```

### 4.1 The Plugin Interface

From `coredns/plugin/plugin.go`:

```go
// Handler is like net/http.Handler but for DNS.
type Handler interface {
    ServeDNS(ctx context.Context, w dns.ResponseWriter, r *dns.Msg) (int, error)
    Name() string
}

// Plugin is the type of the function returned by setup() that wraps
// the next handler in the chain.
type Plugin func(Handler) Handler
```

Each plugin's `setup()` (in its own subpackage, e.g. `plugin/kubernetes/setup.go`) is called when the Corefile is parsed; it reads the plugin's stanza and registers the plugin into the chain. At query time, the server invokes the **first** plugin's `ServeDNS`, which decides whether to answer, mutate, observe, or fall through to `Next`.

The chain has two flavors:
- **Middleware-style plugins** (`errors`, `log`, `prometheus`, `cache`, `loadbalance`) wrap the call to `Next` and observe/mutate the response.
- **Backend plugins** (`file`, `kubernetes`, `forward`, `etcd`) terminate the chain by writing a response.

The chain ends with an implicit "send NXDOMAIN" if nothing wrote a response.

### 4.2 The Plugin Tree (selected)

From `plugin.cfg` (which controls the link-order at build time):

```
metadata:metadata           ← attaches metadata to the request context
geoip:geoip                 ← geo-IP lookups
cancel:cancel               ← cancel context
tls:tls                     ← TLS for upstream/listen
reload:reload               ← reload Corefile on change
nsid:nsid                   ← NSID EDNS option
bufsize:bufsize             ← clamp EDNS bufsize
bind:bind                   ← bind to specific addr
debug:debug                 ← logging tweaks
trace:trace                 ← OpenTelemetry/OpenTracing
ready:ready                 ← /ready endpoint
health:health               ← /health endpoint
pprof:pprof                 ← go pprof endpoint
prometheus:metrics          ← /metrics endpoint
errors:errors               ← error logging
log:log                     ← query logging
dnstap:dnstap               ← dnstap protocol export
acl:acl                     ← per-source-IP filtering
any:any                     ← respond to ANY queries with HINFO
chaos:chaos                 ← respond to CH version.bind queries
loadbalance:loadbalance     ← shuffle answer RRsets
tsig:tsig                   ← TSIG signing
cache:cache                 ← response cache
rewrite:rewrite             ← rewrite queries/responses
header:header               ← rewrite DNS message headers
template:template           ← regex-based synthetic answers
hosts:hosts                 ← /etc/hosts-style records
route53:route53             ← AWS Route53 backend
k8s_external:k8s_external   ← external resolution of ingresses
kubernetes:kubernetes       ← THE plugin we care most about
file:file                   ← zone file backend
auto:auto                   ← auto-load zone files
secondary:secondary         ← AXFR/IXFR from primary
loop:loop                   ← loop detection
forward:forward             ← forward to another resolver
grpc:grpc                   ← forward over gRPC
erratic:erratic             ← testing plugin
whoami:whoami               ← echo client info
on:on                       ← lifecycle hooks
sign:sign                   ← DNSSEC signing
```

The **order in `plugin.cfg` defines the order of execution**. You cannot reorder plugins at runtime; the Corefile's order within a server block does *not* override `plugin.cfg`. This is a deliberate choice: it prevents users from putting `cache` before `errors` and then losing error logging when a cached response is served.

### 4.3 Two Mental Models for the Chain

**Model 1: short-circuit chain.** Each plugin gets the request; the first one to `WriteMsg` ends the chain. Middleware (`log`, `prometheus`, `cache`) wraps that by intercepting the `ResponseWriter`.

**Model 2: layered onion.** Observers (`errors` → `log` → `prometheus`) wrap the inner core. Backends (`kubernetes`, `forward`) are the core. `cache` sits between observers and backends so that cache hits never reach the backend.

Either model produces the right execution order; pick the one that fits your debugging mindset.

---

## 5. The Corefile: Server Blocks and Plugin Chains

The Corefile is a small DSL. Its grammar is documented in `coredns/coredns/coremain/run.go` and parsed by `caddyfile.Parse`. A Corefile is a sequence of *server blocks*. Each server block declares one or more zones it is authoritative for, the port to listen on, and a sequence of plugin stanzas.

### 5.1 The Default In-Cluster Corefile

The Corefile that the Kubernetes upstream installs (via kubeadm or the cluster-autoscaler-installer manifests) looks like this:

```
.:53 {
    errors
    health {
        lameduck 5s
    }
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
        pods insecure
        fallthrough in-addr.arpa ip6.arpa
        ttl 30
    }
    prometheus :9153
    forward . /etc/resolv.conf {
        max_concurrent 1000
    }
    cache 30 {
        disable success cluster.local
        disable denial cluster.local
    }
    loop
    reload
    loadbalance
}
```

Reading top-to-bottom:

- `.:53` — the server block. The `.` is the zone (root, matches everything). `:53` is the listen address/port.
- `errors` — log errors to stderr.
- `health { lameduck 5s }` — expose `/health` on port 8080 (default); during shutdown, return unhealthy for 5s before exiting so kube-proxy / endpoints can drain.
- `ready` — expose `/ready` on port 8181; goes ready when all plugins that have a `ReadinessChecker` (notably `kubernetes`) signal ready.
- `kubernetes cluster.local in-addr.arpa ip6.arpa { … }` — the kubernetes plugin, owning these three zones. `pods insecure` enables Pod-IP-name resolution (more in §6). `fallthrough` says "if I don't have an answer for in-addr.arpa or ip6.arpa, fall through to the next plugin." `ttl 30` sets the TTL of synthesized records.
- `prometheus :9153` — expose `/metrics` on `:9153`.
- `forward . /etc/resolv.conf { max_concurrent 1000 }` — for any name not handled by `kubernetes`, forward to the nameservers listed in the **CoreDNS Pod's** `/etc/resolv.conf` (which is the *node's* upstream resolver, because the CoreDNS Pod has `dnsPolicy: Default`).
- `cache 30 { … }` — cache responses for 30s. The `disable success cluster.local` / `disable denial cluster.local` says "do not cache responses for the cluster zone" — because the kubernetes plugin itself has near-zero latency and we'd rather have a fresh answer.
- `loop` — detect forwarding loops at startup (it sends a probe query and verifies the response).
- `reload` — watch the Corefile for changes and hot-reload.
- `loadbalance` — randomize the order of records in multi-record responses (e.g., headless Service A records).

### 5.2 Server-Block Anatomy

The full syntax of a server block:

```
ZONES [PORT] {
    PLUGIN_NAME [args…] {
        SUBDIRECTIVE [args…]
        …
    }
    …
}
```

Multiple server blocks can coexist:

```
# Authoritative for cluster.local
cluster.local:53 {
    errors
    kubernetes cluster.local
    cache 30
}

# Authoritative for example.internal (a stub domain)
example.internal:53 {
    errors
    forward . 10.0.0.10 10.0.0.11
    cache 60
}

# Everything else
.:53 {
    errors
    forward . 8.8.8.8 8.8.4.4
    cache 300
}
```

Each block listens on the same port (53) but the *first* match (most-specific zone match) wins. Conflict resolution is documented in `caddy/caddyhttp/httpserver/server.go` — CoreDNS reuses Caddy's server-matching logic.

### 5.3 The Health-Ready-Lameduck Dance

A small but important detail. The `health` plugin's `lameduck` directive:

```
health {
    lameduck 5s
}
```

When CoreDNS receives SIGTERM (during a rolling update), the `health` plugin starts returning 503 on `/health` immediately. The kubelet's readiness probe (which targets `/health`) sees this and marks the Pod NotReady, which causes the EndpointSlice controller to drop the Pod from the kube-dns Service. After 5s (the lameduck), CoreDNS actually exits. The 5s window gives kube-proxy time on every node to remove the iptables/IPVS rule that DNATs to this Pod, ensuring no Pod sees a "connection refused" from a half-drained CoreDNS.

Without `lameduck`, rolling updates would drop ~10% of queries during the rollout. With it, packet loss is near zero.

---

## 6. The `kubernetes` Plugin: Watches, Records, Synthesis

The `kubernetes` plugin is the soul of cluster DNS. It is a CoreDNS plugin (`plugin/kubernetes/`) that maintains an in-memory cache of Services, Endpoints/EndpointSlices, and (optionally) Pods, populated from apiserver watches, and synthesizes DNS records on the fly. There is **no zone file**; the records are generated at query time from the live cache.

### 6.1 Watch Loop

On startup, the plugin (`plugin/kubernetes/controller.go`) calls `client-go` to create informers for:

- `Services` (always)
- `EndpointSlices` (preferred since CoreDNS 1.10; replaces the older `Endpoints` watch)
- `Pods` (only if `pods verified` is set; see §6.3)
- `Namespaces` (to validate namespace existence on PTR queries)

Each informer is a standard reflector + DeltaFIFO + indexer (ch 08). The plugin's reaction to events is *just to update its local index*; it does not enqueue work. DNS queries hit the index directly.

```
apiserver
   │  watch /api/v1/services
   │  watch /apis/discovery.k8s.io/v1/endpointslices
   │  watch /api/v1/pods         (optional)
   │  watch /api/v1/namespaces
   ▼
client-go reflector ──► DeltaFIFO ──► Indexer
                                          │
                                          ▼
                                 plugin/kubernetes
                                 in-memory lookup tables:
                                   svcIndex[ns/name] → *Service
                                   epIndex[ns/name]  → []*Endpoint
                                   podIndex[ip]      → *Pod
                                   nsIndex[name]     → *Namespace
```

When a query arrives, the plugin parses the QNAME, decomposes it into `(record, [port], [proto], <svc>, <ns>, svc, <zone>)`, looks up the relevant index, and assembles a `dns.Msg`. No locking on the request path beyond the standard read lock on the index.

### 6.2 Record Decomposition

For a query like `_http._tcp.api.default.svc.cluster.local`, the plugin parses:

```
                       SRV format
  _http._tcp.api.default.svc.cluster.local
   │     │  │    │     │      └─── zone (must match configured zone)
   │     │  │    │     └────────── "svc" — service record type
   │     │  │    └──────────────── namespace
   │     │  └───────────────────── service name
   │     └──────────────────────── protocol (tcp/udp)
   └────────────────────────────── port name
```

`A`/`AAAA` queries: `<svc>.<ns>.svc.<zone>` for clusterIP services, `<hostname>.<svc>.<ns>.svc.<zone>` for headless.

`PTR` queries: `42.10.96.10.in-addr.arpa` → reverse to `<svc>.<ns>.svc.<zone>` (if it's a ClusterIP).

The full grammar lives in `plugin/kubernetes/parse.go`'s `parseRequest`.

### 6.3 The `pods` Sub-Directive

```
kubernetes cluster.local {
    pods MODE
}
```

`MODE` can be:

- `disabled` (default in some setups) — no pod-IP-based A records. Pod-IP queries return NXDOMAIN.
- `insecure` — synthesize an A record for any pod-IP-shaped query. The Pod is not verified to exist; the IP is taken from the QNAME. Format: `10-244-1-5.default.pod.cluster.local` → `10.244.1.5`. This is *cheap* (no Pod watch needed) but anyone can query for any IP and get an answer.
- `verified` — only return an A record if the Pod actually exists at that IP. Requires a `Pods` informer (memory cost ~150 bytes/Pod plus event traffic) but provides authentication. Rarely used in production because the Pod informer is the single largest memory consumer in CoreDNS.

The default in kubeadm Corefile is `pods insecure` because the security gain of `verified` is small (the Pod IPs are not secret) and the memory cost is large.

### 6.4 The `fallthrough` Sub-Directive

```
kubernetes cluster.local in-addr.arpa ip6.arpa {
    fallthrough in-addr.arpa ip6.arpa
}
```

By default, a plugin that owns a zone is *authoritative* for that zone: if it doesn't have an answer, it returns NXDOMAIN and the chain stops. `fallthrough` says "for these specific sub-zones, if I don't have an answer, let the chain continue." This is critical for `in-addr.arpa` because the kubernetes plugin only answers reverse queries for cluster IPs — every other reverse query (e.g., for `8.8.8.8`) needs to fall through to `forward` and reach the upstream resolver.

You can also pass `fallthrough` with no args to enable fall-through for *all* sub-zones owned by the plugin, but this is risky for `cluster.local` because it would make every NXDOMAIN inside the cluster zone queryable from the upstream resolver (which is usually not what you want).

### 6.5 Endpoint Selection

For a ClusterIP Service, the synthesized A record points to the **Service's ClusterIP**, not to a Pod. kube-proxy then DNATs.

For a **headless** Service (`clusterIP: None`), there is no VIP, so the kubernetes plugin returns one A record per Ready endpoint (§8).

For **ExternalName** Services, it returns a CNAME to `spec.externalName`.

The plugin honors the EndpointSlice's `ready` and `serving` conditions: only `ready: true` endpoints are returned for normal queries. `serving: true` but `ready: false` endpoints (e.g., shutting-down Pods) can be returned via the `endpoint_pod_names` directive only if the `publishNotReadyAddresses: true` is set on the Service. The full logic lives in `plugin/kubernetes/endpoint.go`.

---

## 7. Service Records: A, AAAA, SRV, PTR, CNAME

Every Service in Kubernetes maps to a defined set of DNS records. The mapping is part of the Kubernetes DNS-based service discovery spec (which CoreDNS implements). The spec is at `https://github.com/kubernetes/dns/blob/master/docs/specification.md` and lives in CoreDNS at `plugin/kubernetes/`.

### 7.1 ClusterIP Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: postgres
  namespace: prod
spec:
  clusterIP: 10.96.42.10
  ports:
  - name: pg
    port: 5432
    targetPort: 5432
    protocol: TCP
```

Records:

```
postgres.prod.svc.cluster.local.        30  IN  A     10.96.42.10
postgres.prod.svc.cluster.local.        30  IN  AAAA  (if dual-stack)
_pg._tcp.postgres.prod.svc.cluster.local. 30 IN SRV 0 100 5432 postgres.prod.svc.cluster.local.
42.10.96.10.in-addr.arpa.               30  IN  PTR   postgres.prod.svc.cluster.local.
```

The SRV's port name (`_pg`) comes from the **named port** in the Service spec. Unnamed ports do not get SRV records (you cannot have a meaningful SRV without a name). The SRV format is `_<port-name>._<protocol>.<svc>.<ns>.svc.<zone>`.

`dig` from a Pod:

```
$ dig +noall +answer +nocomment +nostats \
    @10.96.0.10 postgres.prod.svc.cluster.local

postgres.prod.svc.cluster.local. 30 IN  A   10.96.42.10

$ dig +noall +answer @10.96.0.10 \
    SRV _pg._tcp.postgres.prod.svc.cluster.local

_pg._tcp.postgres.prod.svc.cluster.local. 30 IN  SRV  0 100 5432 \
    postgres.prod.svc.cluster.local.

$ dig +noall +answer @10.96.0.10 -x 10.96.42.10
42.10.96.10.in-addr.arpa. 30  IN  PTR  postgres.prod.svc.cluster.local.
```

### 7.2 ExternalName

```yaml
apiVersion: v1
kind: Service
metadata:
  name: github-api
  namespace: default
spec:
  type: ExternalName
  externalName: api.github.com
```

Records:

```
github-api.default.svc.cluster.local.  30  IN  CNAME  api.github.com.
```

The CoreDNS plugin returns the CNAME and *does not* chase it; resolution of `api.github.com` is the client's problem (which means the client issues another query, which goes through the `forward` plugin, which goes upstream). This is by design — chasing the CNAME inside CoreDNS would force CoreDNS to be a recursive resolver for the rest of the world, which it is not.

### 7.3 IPv6 / Dual-Stack

If the Service has dual-stack ClusterIPs (`spec.clusterIPs: [10.96.42.10, fd00::42:10]` and `ipFamilyPolicy: RequireDualStack` or `PreferDualStack`), the kubernetes plugin synthesizes both A and AAAA records. The AAAA record points to the IPv6 ClusterIP. PTR queries against `ip6.arpa` work analogously.

On a **single-stack IPv4 cluster** (no IPv6 ClusterIPs allocated), an AAAA query returns NOERROR with an empty answer section — **not NXDOMAIN**. This distinction matters: Go's pure-Go resolver, glibc's `getaddrinfo`, and node-c-ares all treat NOERROR-empty as "no AAAA, but the domain exists" and *do not* fall back to the search list. NXDOMAIN, in contrast, would trigger search-list fallback. CoreDNS correctly returns NOERROR-empty for AAAA on IPv4-only Services, avoiding the search-list NXDOMAIN storm for AAAA. (Some older third-party plugins returned NXDOMAIN; this was a bug.)

### 7.4 The PTR Story

```
$ dig +noall +answer @10.96.0.10 -x 10.96.42.10
42.10.96.10.in-addr.arpa. 30  IN  PTR  postgres.prod.svc.cluster.local.
```

Reverse DNS works for Service ClusterIPs and (with `pods insecure/verified`) for Pod IPs. The reason your monitoring tools (Prometheus targets, traces with sender hostnames) can sometimes show `postgres.prod.svc.cluster.local` even though the connection was opened to the IP is *this* PTR record.

Caveats:
- PTR works only for IPs *within* the service-CIDR or pod-CIDR. Off-cluster IPs PTR-query through the `forward` plugin to the upstream resolver.
- A headless Service's clusterIP is `None`, so it has no PTR. Only the per-Pod IPs (from EndpointSlices) have PTRs, and only via the `pods` setting.

---

## 8. Headless Services and Per-Pod A Records

A **headless Service** is a Service with `clusterIP: None`. It is the escape hatch for cases where:

- You want **all** the endpoints' IPs, not a load-balanced VIP.
- You need per-Pod identity (StatefulSet pattern).
- You're building a client-side load balancer (gRPC, custom drivers) that does its own pool management.
- You want service-discovery semantics without a NAT layer.

### 8.1 Records for a Headless Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: cassandra
  namespace: data
spec:
  clusterIP: None
  selector:
    app: cassandra
  ports:
  - name: cql
    port: 9042
```

Suppose 3 Pods match the selector with IPs `10.244.1.10`, `10.244.2.11`, `10.244.3.12`. The records:

```
cassandra.data.svc.cluster.local.  30  IN  A  10.244.1.10
cassandra.data.svc.cluster.local.  30  IN  A  10.244.2.11
cassandra.data.svc.cluster.local.  30  IN  A  10.244.3.12

_cql._tcp.cassandra.data.svc.cluster.local. 30 IN SRV 0 33 9042 10-244-1-10.cassandra.data.svc.cluster.local.
_cql._tcp.cassandra.data.svc.cluster.local. 30 IN SRV 0 33 9042 10-244-2-11.cassandra.data.svc.cluster.local.
_cql._tcp.cassandra.data.svc.cluster.local. 30 IN SRV 0 33 9042 10-244-3-12.cassandra.data.svc.cluster.local.

10-244-1-10.cassandra.data.svc.cluster.local. 30 IN A 10.244.1.10
10-244-2-11.cassandra.data.svc.cluster.local. 30 IN A 10.244.2.11
10-244-3-12.cassandra.data.svc.cluster.local. 30 IN A 10.244.3.12
```

```
              ASCII view of what a client sees:

   ┌─────────────────────────────────────────────────────────┐
   │  dig cassandra.data.svc.cluster.local                    │
   │  → returns 3 A records (one per Ready endpoint)          │
   │                                                          │
   │  cassandra.data.svc.cluster.local.  A  10.244.1.10       │
   │  cassandra.data.svc.cluster.local.  A  10.244.2.11       │
   │  cassandra.data.svc.cluster.local.  A  10.244.3.12       │
   │                                                          │
   │  Client's resolver returns ALL three. Application logic  │
   │  decides which one to connect to (or all three).         │
   └─────────────────────────────────────────────────────────┘
```

### 8.2 Random-Order, Not Load-Balanced

The order of A records returned is **randomized by the `loadbalance` plugin** (the last plugin in the default Corefile chain). The first record returned changes per query. But this is **not load balancing** in any meaningful sense:

- The client typically uses only the first A record it sees (RFC 6724 sort, glibc-style).
- The client may cache the response for the TTL (30s default), so the "rotation" only happens once every 30s, not per request.
- If one Pod is overloaded, randomization makes the situation worse, not better.

The mental error is to think `dig` returning 3 records means clients spread load. They don't. **For real load distribution, use a ClusterIP Service** and let kube-proxy do the LB. Headless is for client-aware logic (gRPC's `dns:///` resolver, Cassandra's contact-point list, etcd's member discovery, Kafka's bootstrap-server list).

### 8.3 SRV Records and Hostnames

Note the `<dashed-ip>.<svc>.<ns>.svc.<zone>` form in the SRV target. For a **headless** Service, each endpoint gets a per-Pod hostname:

- If the Pod has `spec.hostname` and `spec.subdomain` set, and `spec.subdomain` matches the Service name, the hostname is `<spec.hostname>.<svc>.<ns>.svc.<zone>` (see §10).
- Otherwise, the kubernetes plugin synthesizes `<dashed-ip>` from the endpoint's IP (`10-244-1-10`).

For StatefulSets (§9), the controller automatically sets `spec.hostname = <pod-name>` and `spec.subdomain = <service-name>`, producing stable per-Pod DNS names like `cassandra-0.cassandra.data.svc.cluster.local`.

### 8.4 The `publishNotReadyAddresses` Footgun

```yaml
spec:
  publishNotReadyAddresses: true
```

When set, the EndpointSlice controller includes Pods with `Ready=False` (and `Serving=True`) in the EndpointSlice. The kubernetes plugin then returns their A records in DNS responses. Intended for cases like quorum joins (etcd, Cassandra) where a Pod must become resolvable *before* it's ready in order to bootstrap.

The footgun: setting this on a non-headless Service exposes not-ready pods to traffic via kube-proxy. Some controllers default this on, e.g., the etcd-operator. Audit it carefully.

### 8.5 Number of A Records vs DNS Message Size

A UDP DNS message is capped at 512 bytes (RFC 1035) without EDNS(0), or 4096 bytes with EDNS(0) (RFC 6891). A headless Service with 50 Ready Pods generates 50 A records ≈ 50 × ~30 bytes = 1500 bytes. With EDNS(0), this fits. Without EDNS(0), the response is **truncated** (TC=1) and the client retries over TCP. Modern resolvers (glibc, Go, musl, c-ares) all support EDNS(0), so this is rarely a problem in practice — but if you see TC=1 responses, suspect a headless Service with many endpoints.

CoreDNS's response sizing is controlled by the `bufsize` plugin (defaults to 4096). A headless Service with ~120 endpoints is the practical ceiling before truncation; beyond that, clients should query the SRV record (which has compression and named-port semantics) or switch to a non-headless Service.

---

## 9. StatefulSet Pod DNS and Stable Identity

StatefulSets (ch 13) are the canonical consumer of headless-Service DNS. The combination of `serviceName: <name>` on a StatefulSet, a headless Service of the same `<name>`, and the per-Pod hostname/subdomain produces stable DNS identities for each ordinal replica.

### 9.1 The Pattern

```yaml
apiVersion: v1
kind: Service
metadata:
  name: cassandra
  namespace: data
spec:
  clusterIP: None
  selector:
    app: cassandra
  ports:
  - name: cql
    port: 9042
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: cassandra
  namespace: data
spec:
  serviceName: cassandra
  replicas: 3
  selector:
    matchLabels:
      app: cassandra
  template:
    metadata:
      labels:
        app: cassandra
    spec:
      containers:
      - name: cassandra
        image: cassandra:4.1
        ports:
        - containerPort: 9042
          name: cql
```

The StatefulSet controller sets `spec.hostname = cassandra-N` and `spec.subdomain = cassandra` on each Pod. CoreDNS then synthesizes:

```
cassandra-0.cassandra.data.svc.cluster.local.  30  IN  A  10.244.1.10
cassandra-1.cassandra.data.svc.cluster.local.  30  IN  A  10.244.2.11
cassandra-2.cassandra.data.svc.cluster.local.  30  IN  A  10.244.3.12

cassandra.data.svc.cluster.local.              30  IN  A  10.244.1.10
cassandra.data.svc.cluster.local.              30  IN  A  10.244.2.11
cassandra.data.svc.cluster.local.              30  IN  A  10.244.3.12

_cql._tcp.cassandra.data.svc.cluster.local. 30 IN SRV 0 33 9042 cassandra-0.cassandra.data.svc.cluster.local.
_cql._tcp.cassandra.data.svc.cluster.local. 30 IN SRV 0 33 9042 cassandra-1.cassandra.data.svc.cluster.local.
_cql._tcp.cassandra.data.svc.cluster.local. 30 IN SRV 0 33 9042 cassandra-2.cassandra.data.svc.cluster.local.
```

### 9.2 Why Stable DNS Matters for Stateful Workloads

Stateful systems hard-code peer identity into their gossip / Raft / consensus protocols. Cassandra's seed nodes, etcd's `--initial-cluster`, Zookeeper's `myid`, Kafka's `broker.id`, Postgres replication slots, MongoDB replica-set members — every one of these takes a **hostname**, not an IP. When the Pod restarts, the IP changes (under most CNIs), but the DNS name `cassandra-0.cassandra.data.svc.cluster.local` persists and re-resolves to the new IP.

This is why the StatefulSet's headless-Service requirement is **not** optional. The controller refuses to create Pods if `serviceName` doesn't point to an existing headless Service.

### 9.3 Pod Hostname Visibility

Inside `cassandra-0`'s container:

```
$ hostname
cassandra-0

$ hostname -f
cassandra-0.cassandra.data.svc.cluster.local

$ cat /etc/hostname
cassandra-0
```

The `hostname -f` is honored because the Pod's resolv.conf has the `search` list that includes `data.svc.cluster.local`. The `hostname` (short) is the value of `spec.hostname`, written by the kubelet into `/etc/hostname`.

### 9.4 The Network-Identity Caveats

Two:

1. **Pods come back at different IPs.** DNS resolves to the *current* IP; clients with cached DNS answers (TTL 30s, plus glibc nscd, plus Java's `networkaddress.cache.ttl=Long.MAX_VALUE`) hit a stale IP. Java's default DNS cache TTL is *forever* until you set `networkaddress.cache.ttl` to a finite value. This is the cause of countless "after Pod restart, my Java app keeps hitting the old IP."
2. **DNS propagation is not instantaneous.** When a Pod restarts:
   - The kubelet updates the Pod's `status.podIP`.
   - The EndpointSlice controller updates the EndpointSlice.
   - CoreDNS's informer receives the update.
   - The new A record is served.
   - Cached responses at NodeLocalDNS, glibc nscd, and the application's runtime expire after their TTL.

   End-to-end propagation: ~0.5s (cluster control plane) + up to 30s (CoreDNS TTL) + up to whatever the client's DNS cache TTL is. In Java with default settings, it's never. In Go's pure resolver, it's per-process and respects DNS TTL but doesn't cache across connections.

---

## 10. Pod Hostnames: `spec.hostname`, `spec.subdomain`, `hostAliases`

The Pod spec has three fields that influence its DNS identity, separate from `dnsPolicy`/`dnsConfig`:

### 10.1 `spec.hostname` and `spec.subdomain`

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: my-pod
  namespace: default
spec:
  hostname: my-host
  subdomain: my-svc
  containers:
  - name: app
    image: nginx
```

For this Pod, if there exists a headless Service named `my-svc` in the same namespace, the Pod gets the DNS name `my-host.my-svc.default.svc.cluster.local` resolvable to its IP. Inside the container, `hostname -f` returns the same.

This is the manual version of what StatefulSet automates. You almost never set this directly; the value is in understanding what StatefulSet does under the hood.

### 10.2 `hostAliases`

```yaml
spec:
  hostAliases:
  - ip: "127.0.0.1"
    hostnames:
    - "foo.local"
    - "bar.local"
  - ip: "10.1.2.3"
    hostnames:
    - "legacy-api.internal"
```

The kubelet appends these entries to the Pod's `/etc/hosts`. The Pod's `/etc/hosts` already has entries for `localhost`, `127.0.1.1 <pod-name>`, and the Pod's own IP; `hostAliases` adds more.

```
$ cat /etc/hosts
# Kubernetes-managed hosts file.
127.0.0.1   localhost
::1         localhost ip6-localhost ip6-loopback
fe00::0     ip6-localnet
fe00::0     ip6-mcastprefix
fe00::1     ip6-allnodes
fe00::2     ip6-allrouters
10.244.1.5  my-pod
127.0.0.1   foo.local bar.local
10.1.2.3    legacy-api.internal
```

`/etc/hosts` is consulted **before** DNS by the glibc `nsswitch.conf` default (`hosts: files dns`). Use cases:

- Override DNS for testing without a sidecar.
- Map legacy names that don't have DNS records.
- Map self-references (`127.0.0.1 my-service`) for client-aware apps.

Limitations:

- `hostAliases` is per-Pod, not per-Service; no way to share across namespaces.
- It does not interact with the CoreDNS plugin chain — you cannot use `hostAliases` to resolve external names through CoreDNS.
- Container-level overrides via Docker's `--add-host` are *not* equivalent; in Kubernetes, only the Pod-level `hostAliases` is honored.

### 10.3 Headless-Service Side Effect

If the Pod has `spec.subdomain` matching a Service name in the same namespace, the kubernetes plugin synthesizes a per-Pod A record for `<hostname>.<subdomain>.<ns>.svc.<zone>`. The Service does not need to have a selector matching the Pod — the subdomain match alone is enough. This is the mechanism that makes StatefulSet pod-DNS work without the StatefulSet controller writing any DNS configuration directly.

---

## 11. `dnsPolicy` and `spec.dnsConfig`

The cluster DNS contract (§1) describes the **default** behavior. `dnsPolicy` lets you opt out; `dnsConfig` lets you customize fine-grained.

### 11.1 The Four `dnsPolicy` Values

| Value                   | Effect                                                                                          |
|-------------------------|------------------------------------------------------------------------------------------------|
| `ClusterFirst`          | (Default) Cluster DNS + search list + `ndots:5`. The contract described in §1.                  |
| `ClusterFirstWithHostNet` | Same as `ClusterFirst`, but for `hostNetwork: true` Pods. Without this, hostNetwork Pods would use the *node's* resolv.conf because they share the host network namespace. |
| `Default`               | Copy the node's `/etc/resolv.conf` verbatim. No cluster DNS, no cluster search list. Useful for system DaemonSets that should resolve like the node.        |
| `None`                  | Do not write any resolv.conf entries from the kubelet's defaults; only honor `spec.dnsConfig`. Most flexible, requires you to provide everything. |

### 11.2 `spec.dnsConfig`

```yaml
spec:
  dnsPolicy: ClusterFirst
  dnsConfig:
    nameservers:
    - 1.1.1.1
    searches:
    - my.team.example.com
    options:
    - name: ndots
      value: "2"
    - name: timeout
      value: "1"
    - name: attempts
      value: "3"
    - name: single-request-reopen
```

The `dnsConfig` *merges* with whatever `dnsPolicy` produces:

- `nameservers`: **appended** to the policy's list (up to 3 total).
- `searches`: **appended** (up to 6 total).
- `options`: **merged**, with `dnsConfig` overriding `dnsPolicy` defaults when the same option name appears.

The merge result is what gets written to `/etc/resolv.conf`.

### 11.3 `dnsPolicy: None` for Maximum Control

```yaml
spec:
  dnsPolicy: None
  dnsConfig:
    nameservers:
    - 10.96.0.10
    searches:
    - default.svc.cluster.local
    - svc.cluster.local
    options:
    - name: ndots
      value: "2"
```

This produces a minimal resolv.conf with `ndots:2`, no node-search list, only cluster searches. Used by Pods that:

- Want strict separation from the node's DNS (e.g., to prevent leaks of the cluster's queries to the cloud provider's resolver).
- Want a non-default `ndots` cluster-wide (and can't change the cluster's default).
- Need to point at NodeLocalDNS specifically (e.g., `nameservers: [169.254.20.10]`).

### 11.4 The Default Mutation: Choose Your Default Carefully

The kubelet's default `dnsPolicy` for a Pod whose spec omits the field is `ClusterFirst`. This means every Pod in the cluster gets the trap-laden `ndots:5` by default. Some operators inject a mutating admission webhook that rewrites every Pod's `dnsConfig.options` to set `ndots:2`. This is a one-line policy change in Kyverno or Gatekeeper that, in practice, saves enormous CPU and conntrack table space across the cluster:

```yaml
# Kyverno mutate policy
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: lower-ndots
spec:
  rules:
  - name: lower-ndots
    match:
      any:
      - resources:
          kinds:
          - Pod
    mutate:
      patchStrategicMerge:
        spec:
          dnsConfig:
            options:
            - name: ndots
              value: "2"
```

Tradeoff: `mysvc.myns` (1 dot) no longer search-expands. In modern codebases, this is rarely an issue — most clients use either the short name or the FQDN.

---

## 12. `forward`, `cache`, `prometheus`, `errors`, `log`, `health`, `ready`, `autopath`

We've seen the kubernetes plugin in depth (§6). The other plugins in the default chain deserve the same treatment.

### 12.1 `forward`

```
forward . 1.1.1.1 8.8.8.8 {
    max_concurrent 1000
    policy random
    health_check 5s
    expire 10s
    prefer_udp
}
```

The forward plugin is CoreDNS's recursive-resolver substitute: when no upstream plugin handles the query, it forwards to one of the configured nameservers. The defaults are aggressive:

- `max_concurrent` — limits in-flight upstream queries to prevent CoreDNS from exhausting socket file descriptors. The default in newer versions is 10000.
- `policy` — `random` (default), `round_robin`, or `sequential`. With `random`, the plugin picks one upstream per query, which spreads load and isolates upstream failures.
- `health_check` — interval at which to probe upstreams; failed upstreams are skipped.
- `expire` — how long to maintain a dead-upstream marker.
- `prefer_udp` — UDP first, TCP fallback only on truncation.

The wire-level protocol can be UDP, TCP, DNS-over-TLS (`tls://`), or gRPC. Example with DoT:

```
forward . tls://1.1.1.1 tls://1.0.0.1 {
    tls_servername cloudflare-dns.com
    health_check 5s
}
```

The forward plugin uses **connection pooling** internally; it maintains a pool of persistent connections to upstreams to amortize TLS handshakes. The pool sizing and connection reuse logic is in `plugin/forward/connect.go` and is the difference between "DNS is slow" and "DNS is fast" for clusters with hot external-resolution workloads.

### 12.2 `cache`

```
cache {
    success 9984 30
    denial  9984 5
    prefetch 10 1m 10%
}
```

The cache plugin sits between `kubernetes`/`forward` and the response writer. It looks up the QNAME+QTYPE in its LRU cache; on hit, returns immediately; on miss, calls the next plugin and stores the response.

Directives:

- `success NUM TTL` — cache up to NUM positive responses, max TTL TTL seconds.
- `denial NUM TTL` — cache up to NUM negative responses (NXDOMAIN, NODATA), max TTL TTL seconds.
- `prefetch AMOUNT DURATION PERCENTAGE` — when a cached entry has been accessed AMOUNT times within DURATION of its expiration, refresh it asynchronously. Default off; turning it on helps for hot names.

The cache **honors response TTL** (`min(response.TTL, configured-max)`). This is important: synthesized records from the kubernetes plugin have TTL 30 (default), so cache entries for cluster names also expire in 30s, ensuring rapid propagation of Service updates. External names from `forward` may have much longer TTLs (Google's `A` for `www.google.com` has TTL 300).

Negative caching (denial) is critical for the ndots:5 trap: with `denial 9984 5`, the four guaranteed NXDOMAINs per external lookup are cached for 5s, so a hot-path lookup hits the cache after the first miss. This is one reason NodeLocalDNS exists: it gets *its own* cache, even faster than CoreDNS's.

### 12.3 `prometheus`

```
prometheus :9153
```

Exposes Prometheus-formatted metrics on `:9153/metrics`. Key metrics (covered in §20):

- `coredns_dns_request_count_total{server,zone,type}` — query counter.
- `coredns_dns_request_duration_seconds_bucket{server,zone}` — histogram of query latency.
- `coredns_cache_hits_total{type}` / `coredns_cache_misses_total{type}` — cache effectiveness.
- `coredns_forward_request_duration_seconds_bucket{to}` — upstream latency.
- `coredns_health_request_failures_total` — health-check failures.

These metrics are the bread and butter of every CoreDNS dashboard.

### 12.4 `errors`

```
errors
```

Logs DNS errors (parse failures, plugin-internal errors) to stderr. Always include; never expensive.

A more verbose form for debugging:

```
errors {
    consolidate 5m '.*'
}
```

Coalesces repeated error messages to avoid log spam.

### 12.5 `log`

```
log
```

Logs every query (NAME, QTYPE, RCODE, latency) to stderr. **Off by default** because the volume is enormous (1000s/sec/replica). Use carefully:

```
log {
    class denial error
}
```

Logs only denial (NXDOMAIN) and error responses. Useful for catching misconfigurations without drowning in logs.

### 12.6 `health` and `ready`

```
health :8080 {
    lameduck 5s
}
ready :8181
```

`health` exposes `/health` — returns 200 OK if the process is alive, 503 during lameduck shutdown.

`ready` exposes `/ready` — returns 200 OK only when **every plugin that implements `ReadinessChecker` reports ready**. The kubernetes plugin is the key checker: it reports ready when its informer has synced. This means CoreDNS won't be added to the kube-dns EndpointSlice until it has a fresh view of all Services. Without `ready`, a freshly started CoreDNS replica would return NXDOMAIN for every cluster name until the informer caught up, causing a query storm of misses.

The two ports (8080 and 8181) are separate because health is "process up" and ready is "ready to serve." `health` should be the *liveness* probe target; `ready` should be the *readiness* probe target. Conflating them causes the rolling-update outage (§5.3).

### 12.7 `autopath`

```
autopath @kubernetes
```

The clever plugin. `autopath` instructs CoreDNS to **walk the search path itself** and return a CNAME chain, collapsing N queries into 1. When the kubernetes plugin recognizes the source IP as a Pod and knows the Pod's namespace, it can compute the search list the Pod's resolv.conf would use, then synthesize a CNAME from `www.google.com.default.svc.cluster.local` directly to `www.google.com.` — skipping the four NXDOMAIN expansions.

Caveats:
- Requires the `pods verified` mode in `kubernetes`, which is expensive (Pod informer).
- Doesn't compose well with multi-resolver setups.
- Slightly obscure semantics confuse some clients.
- Has been **partially deprecated** in favor of NodeLocalDNS, which solves the same problem more universally.

If you can deploy NodeLocalDNS, do that instead. `autopath` is the older fix from the pre-NodeLocalDNS era.

### 12.8 `loop`

```
loop
```

Probes the upstream at startup with a unique query and ensures the answer doesn't come back to CoreDNS itself. Catches the case where CoreDNS is configured to `forward . /etc/resolv.conf` and its own `/etc/resolv.conf` points back to itself (a forwarding loop). Aborts startup on detection.

### 12.9 `reload`

```
reload 30s
```

Watches the Corefile for changes; if modified, hot-reloads the plugin chain after 30s. This is how ConfigMap-mounted Corefile changes propagate: edit the ConfigMap, the kubelet syncs the mount, `reload` notices, plugins re-instantiate.

The 30s interval is to debounce rapid changes. Set to `0s` to disable.

### 12.10 `loadbalance`

```
loadbalance round_robin
```

Shuffles the order of records in multi-record responses. Modes: `round_robin` (per-query rotation) and `random` (per-query random). Affects headless A records, multi-AAAA responses, and any record set with multiple values. As noted in §8.2, this is *not* load balancing; it's just record-set shuffling.

### 12.11 `template`

```
template ANY ANY example.local {
    rcode NXDOMAIN
}
```

A regex-templated synthetic-answer plugin. Used for things like "always NXDOMAIN for `example.local`" or "synthesize SOA for our internal zone." Rarely needed but powerful.

### 12.12 `hosts`

```
hosts /etc/coredns/hosts cluster.local {
    fallthrough
}
```

A `/etc/hosts`-format static map served as DNS. Useful for one-off overrides without touching upstream zones.

### 12.13 `rewrite`

```
rewrite name regex ^old\.example\.com$ new.example.com
```

Rewrites the QNAME before passing to subsequent plugins. Used for migration scenarios and for stripping/adding suffixes.

---

## 13. NodeLocalDNSCache

NodeLocalDNSCache is a DaemonSet that runs a small CoreDNS instance on **every node**, bound to a link-local IP (`169.254.20.10` by convention), and acts as a caching forwarder in front of the cluster's CoreDNS Service. It is the single biggest performance win for cluster DNS.

### 13.1 Why It Exists

Five reasons:

1. **Conntrack bypass.** Every UDP DNS query from a Pod to the kube-dns ClusterIP creates a conntrack entry. At high QPS, this fills the table and causes packet drops. NodeLocalDNS terminates the Pod's DNS query at a link-local IP on the same node, with an iptables rule (`-j NOTRACK`) that skips conntrack entirely. The forwarded query from NodeLocalDNS to the actual CoreDNS Pods goes over **TCP** (keepalive-pooled), which doesn't conntrack-pollute and amortizes setup cost.
2. **Cache hit.** NodeLocalDNS holds its own cache. On a hot path (same Service queried 1000x/sec/Pod), the second-through-millionth query is a node-local memory lookup, not a cross-node UDP roundtrip.
3. **ndots:5 mitigation.** The NXDOMAIN search-list expansions for external names are cached node-locally with sub-millisecond hit times. The 10-packet trap of §3.1 becomes 4 packets on cold and 0 packets on warm.
4. **Resilience.** If the central CoreDNS deployment becomes briefly unreachable (etcd hiccup, control-plane upgrade, network partition), NodeLocalDNS serves cached responses for the entire cache TTL. Pods continue resolving Services.
5. **Predictable latency.** With NodeLocalDNS, cluster-DNS p99 collapses to single-digit ms even under load.

### 13.2 The Topology

```
                  ┌──────────────────────────────────────────────────────┐
                  │   NODE                                                │
                  │                                                       │
                  │  Pod A     Pod B     Pod C                            │
                  │   │         │         │                                │
                  │   │  UDP 53 │         │                                │
                  │   ▼         ▼         ▼                                │
                  │  ┌─────────────────────────────────────────────────┐  │
                  │  │ iptables: dst=10.96.0.10 → DNAT 169.254.20.10   │  │
                  │  │           dst=169.254.20.10 → NOTRACK            │  │
                  │  └────────────────────────┬────────────────────────┘  │
                  │                            ▼                            │
                  │  ┌─────────────────────────────────────────────────┐  │
                  │  │  NodeLocal DNS Pod (hostNetwork: true)           │  │
                  │  │  Listen: 169.254.20.10:53 (UDP+TCP)              │  │
                  │  │  Listen: <kube-dns-ip>:53 (UDP+TCP)              │  │
                  │  │  Plugin chain:                                   │  │
                  │  │    errors → cache (large) → forward (TCP to      │  │
                  │  │       cluster.local CoreDNS, kept-alive pool)    │  │
                  │  └────────────────────────┬────────────────────────┘  │
                  │                            │  TCP, keepalive          │
                  └────────────────────────────┼──────────────────────────┘
                                               │
                                               ▼
                  ┌──────────────────────────────────────────────────────┐
                  │   CoreDNS Pods (in some other node)                   │
                  │   Listen: 10.96.0.10 (via kube-dns Service)           │
                  │   Authoritative for cluster.local                     │
                  └──────────────────────────────────────────────────────┘
```

The DaemonSet specifies `hostNetwork: true` and `--localip=169.254.20.10` so that the link-local IP is bound on the host network namespace. Every Pod on the node, regardless of CNI, can reach `169.254.20.10` (link-local is the entire /16 reserved for self-config, and routes are configured by the DaemonSet's init or by the CNI).

### 13.3 The iptables Rules

The DaemonSet installs (via init container or as part of the main container):

```
# NodeLocal DNS install (simplified)
iptables -t raw -A PREROUTING -p udp --dst 169.254.20.10 --dport 53 -j NOTRACK
iptables -t raw -A OUTPUT     -p udp --src 169.254.20.10 --sport 53 -j NOTRACK
iptables -t raw -A PREROUTING -p tcp --dst 169.254.20.10 --dport 53 -j NOTRACK
iptables -t raw -A OUTPUT     -p tcp --src 169.254.20.10 --sport 53 -j NOTRACK

# Also re-listen on the kube-dns IP to catch traffic that bypasses iptables modification
iptables -t nat -I OUTPUT -p udp --dst 10.96.0.10 --dport 53 -j DNAT --to-destination 169.254.20.10:53
iptables -t nat -I OUTPUT -p tcp --dst 10.96.0.10 --dport 53 -j DNAT --to-destination 169.254.20.10:53
```

The `NOTRACK` is the key: it tells netfilter to skip conntrack for these flows. The DNAT redirects in-namespace queries (from Pods that have `10.96.0.10` as their nameserver) to the link-local NodeLocalDNS.

For Pods that have already been mutated by `dnsConfig` to use `169.254.20.10` directly, the DNAT step is a no-op and only the NOTRACK matters.

### 13.4 The NodeLocalDNS Corefile

```
cluster.local:53 {
    errors
    cache {
        success 9984 30
        denial 9984 5
    }
    reload
    loop
    bind 169.254.20.10 10.96.0.10
    forward . __PILLAR__CLUSTER_DNS__ {
        force_tcp
    }
    prometheus :9253
    health 169.254.20.10:8080
}

in-addr.arpa:53 {
    errors
    cache 30
    reload
    loop
    bind 169.254.20.10 10.96.0.10
    forward . __PILLAR__CLUSTER_DNS__ {
        force_tcp
    }
    prometheus :9253
}

ip6.arpa:53 {
    errors
    cache 30
    reload
    loop
    bind 169.254.20.10 10.96.0.10
    forward . __PILLAR__CLUSTER_DNS__ {
        force_tcp
    }
    prometheus :9253
}

.:53 {
    errors
    cache 30
    reload
    loop
    bind 169.254.20.10 10.96.0.10
    forward . __PILLAR__UPSTREAM_SERVERS__ {
        force_tcp
    }
    prometheus :9253
}
```

Notes:
- Four server blocks: one each for `cluster.local`, `in-addr.arpa`, `ip6.arpa`, and `.` (catch-all).
- `bind 169.254.20.10 10.96.0.10` — listens on both the link-local and the kube-dns ClusterIP. The latter is for hostNetwork Pods and for traffic that DNAT didn't catch.
- `forward . __PILLAR__CLUSTER_DNS__ { force_tcp }` — for cluster.local, forward to the central CoreDNS deployment (the placeholder is substituted at install time). `force_tcp` keeps a TCP connection pool open; TCP doesn't conntrack-bloat.
- For the catch-all `.`, the forward target is `__PILLAR__UPSTREAM_SERVERS__`, usually `/etc/resolv.conf` (the node's resolver, which is the cloud provider's resolver).

### 13.5 Pod-Side Configuration

For NodeLocalDNS to actually be used, Pods need to send DNS to `169.254.20.10` (or have iptables DNAT redirect them). Two approaches:

**Approach A: rely on iptables DNAT** (default install). Pods keep `nameserver 10.96.0.10` in their resolv.conf; iptables rewrites the destination. Works for every Pod transparently.

**Approach B: change `nameserver` to `169.254.20.10`** explicitly via `dnsPolicy: None` + `dnsConfig`:

```yaml
spec:
  dnsPolicy: None
  dnsConfig:
    nameservers:
    - 169.254.20.10
    searches:
    - default.svc.cluster.local
    - svc.cluster.local
    - cluster.local
    options:
    - name: ndots
      value: "2"
```

Approach B is marginally faster (skips the iptables hit) and is more explicit, but requires every Pod's spec to be modified. Approach A is the standard upstream recommendation.

### 13.6 Sizing

NodeLocalDNS Pods are tiny: ~30 MiB RAM, ~10m CPU per node. The cache holds maybe 10k entries by default. On nodes with hundreds of Pods and high external-resolution QPS, bump the cache size:

```
cache {
    success 65536 30
    denial 65536 5
}
```

### 13.7 Failure Modes

What happens when NodeLocalDNS dies? The Pod is a DaemonSet replica; if it crashes, the kubelet restarts it. During the restart:

- New DNS queries time out (5s default), then retry. Application connection-pool sees blip.
- The iptables rules remain installed (they're durable across Pod restarts because they were created at the host-network level by the init container).
- Cached responses are lost; first lookups after restart are full forwards.

For production-grade safety, NodeLocalDNS supports **dual binding**: it listens on both `169.254.20.10` and `10.96.0.10` on the host network, and the iptables DNAT routes Pod traffic to `169.254.20.10`. If NodeLocalDNS is down, the DNAT still points at it, so queries fail — there is **no automatic fallback to central CoreDNS**. Some operators add a secondary nameserver via `dnsConfig` to the central CoreDNS service IP as a backup; others accept the brief outage as an acceptable failure mode given DaemonSet self-heal speed.

---

## 14. Scaling CoreDNS

A single CoreDNS Pod can handle ~10k QPS on a modern CPU (depending on cache hit rate and the depth of the plugin chain). Beyond that, you scale out. Three mechanisms.

### 14.1 Static Replicas

The simplest: set `replicas: N` on the CoreDNS Deployment. Two replicas are the bare minimum for any production cluster (one for HA, one for redundancy during rolling updates). Three is a common default for medium clusters. Large clusters (>500 nodes) often run 5–10 replicas.

Anti-affinity is mandatory:

```yaml
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        labelSelector:
          matchLabels:
            k8s-app: kube-dns
        topologyKey: kubernetes.io/hostname
```

Without anti-affinity, all replicas may schedule onto the same node, and a single node failure takes down the entire cluster DNS.

### 14.2 HPA (Horizontal Pod Autoscaler)

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: coredns
  namespace: kube-system
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: coredns
  minReplicas: 2
  maxReplicas: 20
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
```

HPA on CPU works fine for CoreDNS because CPU is well-correlated with QPS. Set `averageUtilization` to 60–70% so the cluster can absorb spikes during the ~30s HPA scale-up delay.

### 14.3 cluster-proportional-autoscaler (a.k.a. kube-dns-autoscaler)

The cluster-proportional-autoscaler is a small DaemonSet-less controller that scales CoreDNS proportionally to **cluster size**, not load. Configurable in a ConfigMap:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: coredns-autoscaler
  namespace: kube-system
data:
  linear: |
    {
      "coresPerReplica": 256,
      "nodesPerReplica": 16,
      "min": 2,
      "max": 50
    }
```

The controller computes `replicas = max(min, min(max, max(nodes/nodesPerReplica, cores/coresPerReplica)))`. This is heuristic ("you have N nodes → you probably need replicas/16 CoreDNS") and is useful when CPU-based HPA underestimates (e.g., because CoreDNS spends most of its time in cache hits at low CPU).

In practice, **either HPA or cluster-proportional, not both.** They fight.

### 14.4 Vertical Scaling

CoreDNS is single-threaded *per query handler*, but Go's runtime parallelizes across cores. A CoreDNS Pod with `cpu: 4` can absorb more QPS than one with `cpu: 1`. Vertical scaling is useful when you want to keep replica count small (fewer cache copies, less informer overhead on the apiserver).

Memory scales with informer cache. For a 1000-Service cluster with `pods insecure`, expect ~100MB. With `pods verified`, ~500MB or more.

### 14.5 Informer Scale Considerations

Every CoreDNS replica opens watches against the apiserver. At 10 replicas × (Services + EndpointSlices + Namespaces) watches × N items, the watch fan-out is non-trivial. For very large clusters (>5000 nodes), the apiserver's watch cache and APF (ch 05) need to be tuned. The trade-off favors **fewer, larger CoreDNS Pods** at very high scale, because each replica's informer is the apiserver's cost, not the QPS.

---

## 15. ExternalDNS: DNS-as-Code for Cloud Providers

ExternalDNS is a controller that **watches Kubernetes resources (Services, Ingresses, Gateways)** and **mutates external DNS providers** (Route53, Cloud DNS, Azure DNS, Cloudflare, RFC2136, et al.) to keep records in sync. It is the bridge between cluster internal DNS (CoreDNS) and the public internet.

```
                  ┌─────────────────────────────────────────────────┐
                  │    Kubernetes cluster                            │
                  │                                                  │
                  │  Service / Ingress / Gateway                     │
                  │    annotations:                                   │
                  │      external-dns.alpha.kubernetes.io/hostname:  │
                  │         api.example.com                          │
                  │      external-dns.alpha.kubernetes.io/ttl: "60"  │
                  │                                                  │
                  │           │ watch                                  │
                  │           ▼                                        │
                  │  ┌─────────────────────────────────────┐          │
                  │  │  ExternalDNS controller              │          │
                  │  │  - reads Service.status.loadBalancer │          │
                  │  │  - computes desired DNS records      │          │
                  │  │  - reconciles to provider            │          │
                  │  └────────────┬─────────────────────────┘          │
                  └───────────────┼──────────────────────────────────┘
                                  │  AWS API / GCP API / …
                                  ▼
                  ┌─────────────────────────────────────────────────┐
                  │  Route53 / Cloud DNS / Azure DNS / Cloudflare    │
                  │                                                  │
                  │  api.example.com  A  1.2.3.4  (the LB IP)        │
                  │  api.example.com  TXT  "heritage=external-dns,…" │
                  └─────────────────────────────────────────────────┘
```

### 15.1 The Heritage TXT Record

ExternalDNS owns only records it created. To track ownership, it writes a sibling TXT record per A record with the value `heritage=external-dns,external-dns/owner=<owner>,external-dns/resource=ingress/default/my-ingress`. On reconcile, it reads the TXT, confirms ownership, then proceeds. Records without the TXT (or with a different owner) are not modified.

The `--txt-owner-id` flag is the unique identifier for this ExternalDNS instance; multiple ExternalDNS instances can coexist in one cluster (e.g., different teams' subdomains) by using different owner IDs.

### 15.2 Modes: Sync vs Upsert-Only

Two reconciliation modes:
- `--policy=sync` — full reconciliation. Records not present in Kubernetes are deleted from the provider.
- `--policy=upsert-only` — only create or update; never delete. Safer (no risk of cascading delete on a configuration mistake) but allows record sprawl.

For production, start with `upsert-only` until you trust the source-of-truth pipeline. Move to `sync` when you have GitOps in front of it (ch 31).

### 15.3 Sources

ExternalDNS can read from:
- `service` — Service `status.loadBalancer.ingress[].ip/.hostname`.
- `ingress` — Ingress `status.loadBalancer.ingress[].ip/.hostname` and `spec.rules[].host`.
- `gateway-httproute` — Gateway API HTTPRoute `spec.hostnames`.
- `crd` — A generic CRD-driven source for custom workflows.
- `node` — Per-node DNS (`<node>.example.com` → external IP).

A typical install handles `service,ingress,gateway-httproute` together.

### 15.4 Race Conditions

ExternalDNS is eventually consistent with the cluster. Three races:
1. **LB-not-yet-provisioned race.** A Service of type `LoadBalancer` doesn't have an LB IP until the cloud provider has provisioned it (could take a minute). During that window, ExternalDNS sees an empty `status.loadBalancer` and creates no record (or deletes the existing one if in `sync` mode). The DNS dangles. Mitigation: `--policy=upsert-only`.
2. **Stale-Pod-IP race.** ExternalDNS-with-headless-Service writes per-Pod A records. If Pods churn rapidly, records flap. Most external DNS providers rate-limit; you'll hit limits at high churn. Use ClusterIP Services for ExternalDNS targets when possible.
3. **Two-controller race.** If two ExternalDNS instances claim the same record, they fight on every reconcile. The TXT ownership check prevents this for the *same record*, but the fight still happens if you got the configuration wrong.

### 15.5 ExternalDNS vs CoreDNS

These are orthogonal:
- **CoreDNS** answers queries from inside the cluster. Authoritative for `cluster.local`.
- **ExternalDNS** writes records to *external* DNS providers (Route53, etc.) so that clients **outside** the cluster can resolve to your LBs.

They never interact. CoreDNS doesn't ask ExternalDNS anything; ExternalDNS doesn't write to CoreDNS.

---

## 16. Performance Tuning

We have already seen many tuning knobs. This section consolidates them with concrete defaults you can copy.

### 16.1 The Cache Sizes

```
cache {
    success 65536 30
    denial 65536 5
    prefetch 10 1m 10%
}
```

- `success`: 65536 entries × ~250 bytes = ~16MB. Enough for most workloads.
- `denial`: same size as success; aggressive negative caching mitigates ndots:5.
- `prefetch`: when a hot entry (≥10 accesses in the last minute of its lifetime) is about to expire, refresh it asynchronously so clients never see a miss.

### 16.2 Forward Plugin Concurrency

```
forward . /etc/resolv.conf {
    max_concurrent 10000
    expire 10s
    policy random
    health_check 5s
    prefer_udp
}
```

`max_concurrent` caps the number of in-flight upstream queries. Set well above peak QPS; below it, you'll see `coredns_forward_max_concurrent_rejects_total` increment.

### 16.3 Resolver Tuning per Pod

```yaml
spec:
  dnsConfig:
    options:
    - name: ndots
      value: "2"
    - name: timeout
      value: "1"
    - name: attempts
      value: "2"
    - name: single-request-reopen
```

- `ndots:2` — biggest single-line latency win.
- `timeout:1` — fail fast on a wedged resolver; let the application's retry budget handle it.
- `single-request-reopen` — mitigate parallel-resolver port-conflict bugs (see §17.3).

### 16.4 Cluster-Side Knobs

- **Increase `nf_conntrack_max`** on every node to 1M or more. Set `nf_conntrack_udp_timeout` to 30s (default), `nf_conntrack_udp_timeout_stream` to 120s.
- **Tune kube-proxy** to use IPVS or replace with Cilium kube-proxy-replacement (ch 14, 16) to reduce per-packet iptables cost.
- **Pin CoreDNS Pods** to dedicated nodes (taints/tolerations + node selectors) to isolate from noisy-neighbor pods.

### 16.5 Java Java Java

Java's default DNS cache TTL is **infinite**. Set:

```
networkaddress.cache.ttl=30
networkaddress.cache.negative.ttl=10
```

Without this, a Java app holds the first-ever resolution forever. Pod restarts will cause connection failures with the cached IP.

```
$ cat /opt/openjdk-17/conf/security/java.security | grep network
networkaddress.cache.ttl=30
networkaddress.cache.negative.ttl=10
```

For containerized Java apps: ensure your image's `java.security` (or `JAVA_TOOL_OPTIONS`) sets these.

---

## 17. The "DNS Is Slow" Troubleshooting Tree

When the on-call gets paged with "DNS is slow," the cause is *always* one of seven things. This is the decision tree.

```
                        DNS is slow / timing out
                                  │
                                  ▼
         ┌──────────── Is it everyone, or just some Pods? ────────────┐
         │                                                              │
       Everyone                                                    Some Pods
         │                                                              │
         ▼                                                              ▼
   ┌─────────────┐                                            ┌────────────────┐
   │ CoreDNS     │                                            │  Pod-specific  │
   │ replicas?   │                                            │  resolv.conf?  │
   │ All healthy?│                                            │  ndots? config?│
   └──────┬──────┘                                            └────────┬───────┘
          │                                                            │
   ┌──────┴──────────────┐                                ┌────────────┴────────┐
   │ Healthy             │                                │ Wrong ns or         │
   │ but slow?           │                                │ ndots:5?            │
   └──────┬──────────────┘                                └─────────────────────┘
          │
   ┌──────┴───────┐
   │ Cache miss?  │
   │ Forward slow?│
   │ Conntrack?   │
   └──────────────┘
```

### 17.1 Check 1: Is It the ndots Trap?

```
$ kubectl exec -n default mypod -- cat /etc/resolv.conf
search default.svc.cluster.local svc.cluster.local cluster.local us-east-1.compute.internal
nameserver 10.96.0.10
options ndots:5

$ kubectl exec -n default mypod -- time getent hosts www.google.com
```

If the time is > 20ms, you've found the trap. Fix: lower `ndots` via `dnsConfig` (§3.3).

### 17.2 Check 2: Is It Conntrack Overflow?

```
$ ssh node-1
$ cat /proc/sys/net/netfilter/nf_conntrack_count
260000
$ cat /proc/sys/net/netfilter/nf_conntrack_max
262144

$ dmesg | grep conntrack | tail
[12345.6789] nf_conntrack: table full, dropping packet
```

If `nf_conntrack_count` is close to `nf_conntrack_max`, you're dropping packets. Fix: install NodeLocalDNS (§13), raise `nf_conntrack_max` to 1M+.

### 17.3 Check 3: Is It the AAAA Burst on IPv4-Only?

The Linux kernel has a known race in `connect()` from a multi-threaded process when using UDP: glibc < 2.10 issued A and AAAA queries in parallel from the *same source port*, and on rare collisions the answers got mis-routed. This was fixed in glibc 2.10 with `single-request-reopen`.

Symptoms: occasional 5s pauses in DNS (the resolver waits for the answer to query 1, eventually times out, retries). Distinct from ndots in that the latency is exactly 5s (one resolver timeout), not 20ms (many quick NXDOMAINs).

Fix: `options single-request-reopen` in resolv.conf, *or* upgrade your base image to a glibc that has the kernel SO_REUSEPORT-based fix.

For IPv4-only clusters: configure CoreDNS to respond NOERROR-empty for AAAA, *not* NXDOMAIN. CoreDNS's kubernetes plugin already does this correctly. If you see NXDOMAIN for AAAA, suspect a misconfigured plugin chain.

### 17.4 Check 4: Single CoreDNS Replica

```
$ kubectl get deploy -n kube-system coredns
NAME      READY   UP-TO-DATE   AVAILABLE   AGE
coredns   1/1     1            1           90d
```

One replica is a hard outage waiting to happen and a load bottleneck. Scale up immediately.

### 17.5 Check 5: CoreDNS CPU Saturation

```
$ kubectl top pod -n kube-system -l k8s-app=kube-dns
NAME                       CPU(cores)   MEMORY(bytes)
coredns-7d8d4f7b88-abcde   1900m        100Mi
coredns-7d8d4f7b88-fghij   1850m        100Mi
```

At 1900m of a 2-core limit, CoreDNS is CPU-bound. Scale out (HPA / replicas / cluster-proportional). Also check `coredns_forward_request_duration_seconds_bucket` for slow upstreams.

### 17.6 Check 6: Slow Upstream

```
$ kubectl exec -n kube-system coredns-... -- \
    wget -qO- http://localhost:9153/metrics | grep forward_request_duration | grep "0.5"
coredns_forward_request_duration_seconds_bucket{to="10.0.0.10",le="0.5"} 12345
coredns_forward_request_duration_seconds_bucket{to="10.0.0.10",le="0.5"} 99999
```

If a high fraction of upstream queries take > 500ms, your upstream resolver is slow. Switch to a different upstream, add another upstream for round-robin, or move closer (e.g., NodeLocal cache).

### 17.7 Check 7: Cache TTL = 0 Footgun

If a misconfigured Corefile has `cache 0` (or no cache plugin), every query goes through the entire chain to backend or upstream. Common during initial setup; always check the rendered Corefile:

```
$ kubectl get cm coredns -n kube-system -o yaml | yq '.data.Corefile'
```

### 17.8 The Whole Tree

```
   "DNS slow" pager
        │
        ├── ndots:5 + external lookups   → §3.3 lower ndots
        ├── conntrack overflow           → §13 NodeLocalDNS
        ├── AAAA + parallel resolver     → §17.3 single-request-reopen
        ├── 1 CoreDNS replica            → §14 scale
        ├── CoreDNS CPU saturation       → §14 HPA / VPA
        ├── Slow upstream                → §12.1 forward tuning
        ├── No cache / TTL=0             → §12.2 cache 30
        ├── Java's infinite DNS cache    → §16.5
        ├── Stale Pod IP (post-restart)  → check CoreDNS readiness sync
        ├── EndpointSlice not updating   → check kube-controller-manager
        └── NodeLocalDNS itself crashed  → check DaemonSet status, fall back
```

---

## 18. Custom DNS: Stub Domains, Custom Resolvers, Split Horizon

Real-world clusters often need to resolve names not handled by CoreDNS by default:
- A corporate domain (`corp.example.com`) backed by an internal Active Directory DNS.
- A legacy environment (`legacy.dc1.internal`) with its own DNS.
- A staging environment whose Services should be visible from the prod cluster.

### 18.1 Stub Domains via Corefile

Add a server block:

```
corp.example.com:53 {
    errors
    cache 60
    forward . 10.0.0.10 10.0.0.11
}

legacy.dc1.internal:53 {
    errors
    cache 30
    forward . 10.20.30.40
}

.:53 {
    errors
    health
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
        pods insecure
        fallthrough in-addr.arpa ip6.arpa
    }
    prometheus :9153
    forward . /etc/resolv.conf
    cache 30
    loop
    reload
}
```

CoreDNS's server-block matching is *most-specific-zone-first*. A query for `db1.corp.example.com` matches the `corp.example.com:53` block first; a query for `www.google.com` falls through to `.:53`.

### 18.2 Per-Pod Custom Resolvers

When only some Pods need a custom resolver (e.g., a debugging utility Pod that talks to a specific test resolver):

```yaml
spec:
  dnsPolicy: None
  dnsConfig:
    nameservers:
    - 10.0.99.100        # custom resolver
    - 10.96.0.10         # fall back to cluster DNS
    searches:
    - default.svc.cluster.local
    - cluster.local
    options:
    - name: ndots
      value: "2"
```

### 18.3 Split-Horizon (Same Name, Different Answers)

Some clusters need a Service named `api.example.com` to resolve to the cluster's LB *outside* the cluster, but to a ClusterIP *inside* the cluster. Two ways:

1. **External DNS + CoreDNS rewrite**: ExternalDNS writes `api.example.com → 1.2.3.4` to the public DNS. CoreDNS's `rewrite` plugin rewrites in-cluster queries:
   ```
   rewrite name api.example.com api.default.svc.cluster.local
   ```
   In-cluster queries for `api.example.com` become queries for `api.default.svc.cluster.local`, served by the kubernetes plugin as the ClusterIP.

2. **k8s_external plugin** (§21.1): exposes Ingress / LB Service FQDNs via CoreDNS, but only for in-cluster querying. External resolution still goes through ExternalDNS / public DNS.

---

## 19. DNS-Aware Egress: Blocking, Splitting, Policy

DNS is also a security and policy surface. Three patterns.

### 19.1 Egress Blocking via Forward Policy

```
.:53 {
    errors
    log
    acl {
        block type ANY net 10.244.0.0/16   # block ANY queries
    }
    template ANY ANY badsite.example.com {
        rcode NXDOMAIN
    }
    forward . /etc/resolv.conf
    cache 30
}
```

- `acl` blocks specific QTYPEs from specific source nets.
- `template` returns synthetic responses for specific names (here, NXDOMAIN for `badsite.example.com`).

### 19.2 Per-Namespace Resolvers

CoreDNS doesn't natively support "if Pod's source IP is in namespace X, use resolver Y" because it can't easily map source IP to namespace. The workaround is to use **two CoreDNS deployments** with different Corefiles, expose them via different Services, and use Pod `dnsConfig` per namespace to point at the right one. Complex but possible.

### 19.3 DNS Egress Audit

For SOC/security needs, log every external DNS query:

```
.:53 {
    log {
        class success error
    }
    forward . /etc/resolv.conf
}
```

Pipe stdout to a log shipper. Query volume is high (1000s/sec), so budget the storage.

### 19.4 Combining with NetworkPolicy

NetworkPolicy (ch 20) operates at L3/L4. It cannot allow "egress to www.google.com" because it doesn't see DNS. To enforce DNS-based egress, use:

- **Calico's GlobalNetworkPolicy** with `egress.destinations.domains` (DNS-aware policy).
- **Cilium L7 policy** with `dns.matchPattern` (parses DNS responses and updates policy maps).
- A **DNS egress gateway** that proxies queries and applies policy based on QNAME.

The simplest pattern: CoreDNS forwards to an internal allow-listing resolver, which returns NXDOMAIN for any name not on the allow-list. Pods can't connect to a name they can't resolve.

---

## 20. Observability: Metrics, Alerts, Audit

### 20.1 Core Metrics

CoreDNS exposes a Prometheus endpoint via `prometheus :9153`. Key series:

| Metric                                                | What it tells you                                       |
|-------------------------------------------------------|---------------------------------------------------------|
| `coredns_dns_requests_total{server,zone,type,proto}`  | Total queries. Rate gives QPS.                          |
| `coredns_dns_responses_total{server,zone,rcode}`      | Responses by RCODE. NXDOMAIN rate indicates ndots trap. |
| `coredns_dns_request_duration_seconds_bucket{server,zone}` | Histogram: p50/p95/p99 of query latency.            |
| `coredns_dns_request_size_bytes_bucket`               | Request size distribution.                              |
| `coredns_dns_response_size_bytes_bucket`              | Response size: headless Services with many endpoints.   |
| `coredns_cache_hits_total{type}`                      | Cache hits (success or denial).                         |
| `coredns_cache_misses_total{type}`                    | Cache misses.                                           |
| `coredns_cache_entries{type}`                         | Current cache size.                                     |
| `coredns_forward_request_duration_seconds_bucket{to}` | Upstream latency by upstream.                           |
| `coredns_forward_requests_total{to,rcode}`            | Upstream queries by target and result.                  |
| `coredns_forward_healthcheck_failures_total{to}`      | Upstream health failures.                               |
| `coredns_forward_max_concurrent_rejects_total`        | Queries dropped due to max_concurrent.                  |
| `coredns_health_request_failures_total`               | Self-health probe failures.                             |
| `coredns_panics_total`                                | Process panic count (should be 0).                      |
| `coredns_plugin_enabled{server,zone,name}`            | Which plugins are active per zone.                      |
| `coredns_kubernetes_dns_programming_duration_seconds` | Time from API change → DNS record served (the SLI).     |
| `coredns_build_info{version,goversion,revision}`      | Build info.                                             |

### 20.2 Cache Effectiveness Ratio

The hit ratio:

```
sum(rate(coredns_cache_hits_total[5m])) /
(sum(rate(coredns_cache_hits_total[5m])) + sum(rate(coredns_cache_misses_total[5m])))
```

Should be > 0.7 for healthy clusters. Below 0.5 indicates very chatty clients or undersized cache.

### 20.3 Latency SLO

```
histogram_quantile(0.99,
  sum(rate(coredns_dns_request_duration_seconds_bucket[5m])) by (le, server)
)
```

A common SLO: p99 < 10ms for `cluster.local`, p99 < 50ms for external (after NodeLocalDNS, both should be < 5ms).

### 20.4 The DNS-Programming SLI

The Kubernetes scalability SIG defines an SLI: time from a Service / EndpointSlice update being observed in the apiserver to the corresponding DNS record being returnable by CoreDNS. Measured by `coredns_kubernetes_dns_programming_duration_seconds`. Target: p99 < 5s.

### 20.5 Useful Alerts

```yaml
# Replicas missing
- alert: CoreDNSReplicaDown
  expr: kube_deployment_status_replicas_available{deployment="coredns"} < 2
  for: 5m

# CPU saturated
- alert: CoreDNSCPUHigh
  expr: rate(container_cpu_usage_seconds_total{pod=~"coredns-.*"}[5m]) > 0.9
  for: 5m

# Latency tail
- alert: CoreDNSLatencyHigh
  expr: histogram_quantile(0.99, sum(rate(coredns_dns_request_duration_seconds_bucket[5m])) by (le)) > 0.05
  for: 10m

# Upstream failing
- alert: CoreDNSUpstreamFailures
  expr: rate(coredns_forward_healthcheck_failures_total[5m]) > 0
  for: 5m

# Panic
- alert: CoreDNSPanic
  expr: rate(coredns_panics_total[5m]) > 0

# NXDOMAIN storm (ndots:5 trap)
- alert: CoreDNSNXDomainStorm
  expr: rate(coredns_dns_responses_total{rcode="NXDOMAIN"}[5m])
        / rate(coredns_dns_requests_total[5m]) > 0.5
  for: 15m
```

The NXDOMAIN storm alert is the signal that you should investigate ndots:5 or aggressive AAAA on IPv4-only.

### 20.6 Tracing

CoreDNS supports OpenTelemetry tracing via the `trace` plugin:

```
trace prod-collector.observability.svc.cluster.local:4317
```

Each query becomes a span; spans nest through the plugin chain. Useful for diagnosing which plugin in a long chain is the latency culprit.

---

## 21. CoreDNS Extensions: `k8s_external`, `pods`, Custom Plugins

### 21.1 `k8s_external`

The `kubernetes` plugin only serves the cluster zone. For external domains like `api.example.com`, you'd normally rely on the upstream resolver — but if you want CoreDNS to resolve them based on **Ingress** or **LoadBalancer** Service FQDNs, use `k8s_external`:

```
example.com:53 {
    errors
    cache 30
    k8s_external example.com
    forward . /etc/resolv.conf
}
```

This makes CoreDNS authoritative for `*.example.com`, resolving names like `api.example.com` to whatever Ingress/Service in the cluster has that hostname annotation. Useful for split-horizon (§18.3).

### 21.2 The `pods` Modes Revisited

```
kubernetes cluster.local {
    pods MODE  # disabled / insecure / verified
}
```

- `disabled` — minimal memory; no Pod-IP DNS.
- `insecure` — Pod-IP DNS available but no verification; cheap.
- `verified` — Pod-IP DNS only if Pod exists; expensive Pod informer.

For cluster sizes >5k Pods, `verified` becomes the largest cost in CoreDNS memory. Use `insecure` unless you specifically need authenticated reverse-lookups.

### 21.3 Custom Plugins

CoreDNS is intentionally pluggable. To add a custom plugin:

1. Implement the `Handler` interface (§4.1).
2. Register a `setup()` function in `plugin/myplugin/setup.go`.
3. Add `myplugin:myplugin` to `plugin.cfg`.
4. Re-build CoreDNS with `go build`.

Examples of useful third-party plugins:
- `policy` — per-query policy evaluation (Themis / OPA-based).
- `route53` — serve a Route53 zone as if it were local.
- `redisc` — Redis-backed cache (shared across replicas).
- `dnstap` — emit dnstap protocol for query auditing.

### 21.4 The Custom Corefile via ConfigMap

In a managed Kubernetes (EKS, GKE, AKS), the CoreDNS Deployment mounts the Corefile from a ConfigMap named `coredns`. To customize:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: coredns
  namespace: kube-system
data:
  Corefile: |
    .:53 {
        errors
        health {
            lameduck 5s
        }
        ready
        kubernetes cluster.local in-addr.arpa ip6.arpa {
            pods insecure
            fallthrough in-addr.arpa ip6.arpa
            ttl 30
        }
        prometheus :9153
        forward . /etc/resolv.conf {
            max_concurrent 1000
        }
        cache 30 {
            disable success cluster.local
            disable denial cluster.local
        }
        loop
        reload
        loadbalance
    }
    corp.example.com:53 {
        errors
        cache 60
        forward . 10.0.0.10
    }
```

Apply via `kubectl apply -f`; the `reload` plugin picks up the change within 30s.

For EKS-specific behavior: the `coredns` add-on (managed) may overwrite your ConfigMap on add-on updates. Use the EKS console / API to set the configuration permanently, or pin your add-on version.

### 21.5 The `kubernetes` plugin's `endpoint_pod_names` Directive

```
kubernetes cluster.local {
    endpoint_pod_names
}
```

When set, headless Service A records use the Pod name (`cassandra-0`) instead of the dashed-IP (`10-244-1-10`) as the per-Pod hostname. This is the default for StatefulSet-driven headless Services because the Pods have `spec.hostname` set, but `endpoint_pod_names` ensures it for non-StatefulSet headless Services too.

---

## 22. Pitfalls

A staff engineer's compendium of DNS pitfalls. Each appears in the wild; each has a fix.

### 22.1 `ndots:5` Default

The kubelet writes `ndots:5` by default. Every external lookup costs ~10 packets. **Fix**: lower to `ndots:2` per-Pod via `dnsConfig`, or cluster-wide via a Kyverno mutate webhook. Always discuss this with the application teams — it has migration implications for code that uses `mysvc.myns` (1 dot).

### 22.2 Search-Path Exhaustion

The resolv.conf search list is capped at 6 entries / 256 chars. The kubelet adds 3 (namespace.svc, svc, cluster) plus any node entries. If the node already has 4+ entries (some cloud providers do), the cluster's search entries get truncated. **Fix**: trim node-level searches via kubelet's `--resolv-conf=/etc/k8s-resolv.conf` pointing at a smaller file, or set `dnsConfig.searches` explicitly.

### 22.3 Single-Namespace Fallback Confusion

A Pod in `default` calls `mysvc`. Resolves to `mysvc.default.svc.cluster.local` (same-namespace). Move that Pod to `staging` namespace — now `mysvc` resolves to `mysvc.staging.svc.cluster.local`, which may not exist. **Fix**: educate developers to use FQDNs when crossing namespaces (`mysvc.default.svc.cluster.local`), or use Service DNS aliases.

### 22.4 IPv6 AAAA on IPv4-Only Clusters

Every `getaddrinfo` issues both A and AAAA. On IPv4-only, AAAA returns NOERROR-empty (good) but still goes through the search list first because `ndots:5`. That's 5 wasted AAAA queries per external lookup. **Fix**: lower ndots, or use `single-request-reopen`, or `AI_ADDRCONFIG` in getaddrinfo (Go's pure resolver respects this; glibc respects it; musl < 1.2.4 does not).

### 22.5 Single CoreDNS Replica

The default kubeadm install ships with 2 replicas. Some managed K8s ships with 1. **Fix**: always run ≥ 2, with anti-affinity to spread across nodes.

### 22.6 No NodeLocalDNS at High QPS

Above ~5k QPS per node, the conntrack table and central CoreDNS replica budget collapse. **Fix**: install NodeLocalDNS as a baseline DaemonSet for any cluster > ~200 Pods/node or > 1k QPS/Pod.

### 22.7 Conntrack Overflow

`nf_conntrack: table full, dropping packet` in dmesg means new connections are silently dropped. **Fix**: NodeLocalDNS (eliminates DNS conntrack entirely via NOTRACK), raise `nf_conntrack_max` to 1M+.

### 22.8 Headless A Records Expecting Load Balancing

Developers query a headless Service expecting "DNS-based load balancing." It isn't. Clients use the first record (or all). **Fix**: use ClusterIP Service for LB, headless Service for client-aware logic only.

### 22.9 Search-Path Collision Between Cluster and External

A team registers `prod.svc.cluster.local` as an external domain (an actual public DNS zone). Pods in any namespace querying for `something.prod` get search-expanded to `something.prod.svc.cluster.local` first; if NXDOMAIN, fall through to upstream. If a malicious actor controls `prod.svc.cluster.local` on public DNS, they can poison cluster queries when CoreDNS misses. **Fix**: never use `cluster.local` or its sub-zones as external domains; firewall outbound DNS to known resolvers.

### 22.10 CoreDNS OOM Under Cache Pressure

The cache plugin's defaults are bounded, but a misconfigured Corefile (`cache { success 1000000 ... }`) or the `pods verified` mode can blow up memory. **Fix**: cap cache sizes, prefer `pods insecure`, monitor `coredns_cache_entries`.

### 22.11 `autopath` Misuse

`autopath` requires `pods verified`, requires Pods to be in the watched namespace, and silently fails for off-cluster source IPs. The CNAME-chain answer it returns can confuse some DNS clients. **Fix**: prefer NodeLocalDNS for the same effect.

### 22.12 TTL=0 with Chatty Clients

Setting `ttl 0` on the `kubernetes` plugin (sometimes done because "I want fresh data") makes every query a full lookup. Java apps with `networkaddress.cache.ttl=-1` will then re-query CoreDNS on every connection. CoreDNS CPU climbs to 100%. **Fix**: leave TTL at 30s default; if you genuinely need <30s churn, scale CoreDNS and NodeLocalDNS rather than disable caching.

### 22.13 Stale DNS in Java

Java's `InetAddress` cache defaults to forever. After Pod restart with new IP, Java apps keep hitting the dead IP. **Fix**: `networkaddress.cache.ttl=30` in `java.security` or via `JAVA_TOOL_OPTIONS=-Dnetworkaddress.cache.ttl=30`.

### 22.14 `dnsConfig` + `dnsPolicy` Misinterpretation

`dnsPolicy: ClusterFirst` + `dnsConfig.nameservers: [1.1.1.1]` does *not* replace cluster DNS — it appends. The Pod has both `10.96.0.10` and `1.1.1.1` as nameservers; if CoreDNS is wedged, queries fall through to `1.1.1.1`, which doesn't know cluster.local. **Fix**: read the docs for merge semantics (§11.2); use `dnsPolicy: None` for full control.

### 22.15 ExternalDNS Deletion on Annotation Removal

If you delete the `external-dns.alpha.kubernetes.io/hostname` annotation from a Service, ExternalDNS-with-`policy=sync` *deletes* the public record. If the Service is the public entrypoint, all traffic dies. **Fix**: start with `policy=upsert-only`; switch to `sync` only with GitOps gating.

### 22.16 ClusterFirstWithHostNet Forgotten on hostNetwork Pods

A Pod with `hostNetwork: true` uses the node's resolv.conf by default — *unless* `dnsPolicy: ClusterFirstWithHostNet` is set. Without it, the Pod can't resolve cluster Services. **Fix**: always set `ClusterFirstWithHostNet` on hostNetwork Pods that need cluster DNS.

### 22.17 The `kubernetes` Plugin's Watch Outage Window

When CoreDNS restarts (rolling update), the `ready` plugin holds it out of the kube-dns Service until the informer is synced. But during the catch-up, the *other* (still-running) replica handles all traffic. If you have only 2 replicas and lose one to a node failure during a rolling update, you have a brief 0-replica window. **Fix**: 3+ replicas; PodDisruptionBudget with `minAvailable: 2`.

### 22.18 The musl Resolver

Alpine-based images use musl. Before musl 1.2.4, the resolver didn't implement search or ndots. A Pod that uses `mysvc` gets NXDOMAIN immediately because musl issues only the absolute query. **Fix**: use FQDNs in Alpine images, or upgrade base image, or switch to `glibc`-based images.

### 22.19 `loop` Plugin False Positives

When CoreDNS's `/etc/resolv.conf` points to `127.0.0.1` or `127.0.0.53` (systemd-resolved on the host), the `loop` plugin may detect a forwarding loop and refuse to start. **Fix**: use `--resolv-conf` to point CoreDNS at a different file, or mount the actual upstream resolver list.

### 22.20 EndpointSlice Mirror Lag

CoreDNS watches EndpointSlices, not Endpoints. If you have an older controller still writing Endpoints (not EndpointSlices), CoreDNS won't see updates until the EndpointSlice mirror controller (which lives in kube-controller-manager) syncs. The lag is usually < 1s but can spike to 10s under apiserver load. **Fix**: ensure EndpointSlices are enabled cluster-wide.

### 22.21 Upstream DNS Has TTL 0

Some cloud providers' internal resolvers return TTL 0 on many records. CoreDNS's `cache` plugin respects min(response.TTL, max) — so TTL 0 means no caching. **Fix**: configure `cache` with a *minimum* TTL via plugin extension (CoreDNS has a `--ttl-min` proposal but not yet merged; in practice, rewrite TTL with the `rewrite` plugin or use NodeLocalDNS's own cache, which has its own min).

### 22.22 Service Name Collision with TLDs

Naming a Service `local` (yes, people have) makes its FQDN `local.<ns>.svc.cluster.local`. Queries for `<ns>.local` may collide with mDNS or with the `.local` reserved TLD. **Fix**: avoid Service names that match common TLDs (`local`, `internal`, `corp`, `home`, `lan`).

### 22.23 ExternalName CNAME Loops

```yaml
apiVersion: v1
kind: Service
metadata:
  name: foo
spec:
  type: ExternalName
  externalName: bar.default.svc.cluster.local
```

```yaml
apiVersion: v1
kind: Service
metadata:
  name: bar
spec:
  type: ExternalName
  externalName: foo.default.svc.cluster.local
```

Two ExternalNames pointing at each other. The CoreDNS plugin returns CNAMEs unchanged; the client follows them and loops until the recursion limit. **Fix**: never CNAME between cluster names; CNAME only to external FQDNs.

### 22.24 NodeLocalDNS Missing PodDisruptionBudget

NodeLocalDNS is a DaemonSet, so PDBs don't apply to it the way they do to Deployments — but its failure mode (Pod restart during install of an iptables rule) is brief. The bigger issue is *upgrades*: if you roll NodeLocalDNS pods rapidly, each restart causes a brief outage on that node. **Fix**: set `updateStrategy: RollingUpdate` with `maxUnavailable: 10%` (not 25% — too aggressive for DNS).

---

## 23. TL;DR

**The cluster DNS contract**: every Pod's `/etc/resolv.conf` has `nameserver = kube-dns ClusterIP`, `search = ns.svc.cluster.local svc.cluster.local cluster.local + node-search`, `options ndots:5`. CoreDNS, behind that ClusterIP, is authoritative for `cluster.local` and forwards everything else.

**The ndots:5 trap**: every external name with fewer than 5 dots triggers full search-list expansion — 5 search suffixes × 2 (A+AAAA) = 10 packets per "simple" lookup. Mitigate with `ndots:2` per Pod, NodeLocalDNS, or `autopath`.

**CoreDNS architecture**: a Go binary with a plugin chain. The chain order is fixed in `plugin.cfg`. Server blocks own zones; the most-specific zone wins. The default Corefile has `errors → health → ready → kubernetes → prometheus → forward → cache → loop → reload → loadbalance`.

**The kubernetes plugin**: watches Services + EndpointSlices + Namespaces (and Pods if `pods verified`) via client-go informers, synthesizes A/AAAA/SRV/PTR/CNAME records on the fly. No zone file. Records have configurable TTL (30s default). The `fallthrough` directive lets PTR queries for non-cluster IPs reach the `forward` plugin.

**Service DNS records**: ClusterIP Service → A/AAAA + SRV (per named port) + PTR. ExternalName → CNAME. Headless Service → multiple A records, one per Ready endpoint, plus SRV with per-Pod hostnames. AAAA on IPv4-only returns NOERROR-empty (not NXDOMAIN — important for avoiding search expansions).

**Headless service resolution**: `clusterIP: None` → CoreDNS returns one A record per endpoint. Not load-balanced (client picks first). For StatefulSets, the controller sets `spec.hostname = <pod-name>` + `spec.subdomain = <service-name>` → stable per-Pod DNS like `cassandra-0.cassandra.data.svc.cluster.local`.

**Pod DNS controls**: `spec.hostname` + `spec.subdomain` for per-Pod identity; `spec.hostAliases` for `/etc/hosts` overrides. `dnsPolicy` chooses contract (`ClusterFirst` / `ClusterFirstWithHostNet` / `Default` / `None`); `dnsConfig` merges with the chosen contract for fine-grained control (custom nameservers, searches, options like `ndots:2`).

**NodeLocalDNS**: DaemonSet on every node, bound to link-local `169.254.20.10`, with iptables rules that (a) DNAT pod queries to it, (b) NOTRACK its traffic (bypass conntrack), (c) forward TCP-with-keepalive to central CoreDNS. Eliminates the conntrack-overflow failure mode and adds a node-local cache layer; reduces cluster-DNS p99 by 5-10×.

**Scaling**: minimum 2 replicas with anti-affinity. HPA on CPU (target 60-70%) or cluster-proportional-autoscaler. Vertical scaling helps at very large cluster sizes where informer cost dominates.

**ExternalDNS**: separate controller; watches Services/Ingresses/Gateways and updates **external** DNS providers (Route53, Cloud DNS, etc.). Uses a TXT record to track ownership. Modes: `sync` (full reconcile) or `upsert-only` (safer). Orthogonal to CoreDNS.

**Performance tuning**: cache success/denial at 65k entries × 30s/5s TTL with prefetch; forward max_concurrent 10k; per-Pod `ndots:2 timeout:1 single-request-reopen`; raise `nf_conntrack_max` to 1M+; install NodeLocalDNS.

**Troubleshooting tree**: ndots:5? conntrack overflow? AAAA parallel-resolver race? Single CoreDNS replica? CoreDNS CPU saturated? Slow upstream? Cache disabled? Java's infinite cache TTL? NodeLocalDNS down? Each has a specific signal and a specific fix.

**Custom DNS**: stub-domain server blocks in the Corefile; per-Pod overrides via `dnsConfig`; split-horizon via `rewrite` or `k8s_external`.

**Observability**: `coredns_dns_request_duration_seconds_bucket` (latency SLO), `coredns_cache_hits_total / misses` (cache effectiveness), `coredns_forward_request_duration_seconds_bucket` (upstream latency), `coredns_dns_responses_total{rcode="NXDOMAIN"}` (NXDOMAIN storm = ndots:5 alert), `coredns_kubernetes_dns_programming_duration_seconds` (the scalability-SIG SLI).

**Pitfalls**: 24 of them; the top three are (1) leaving `ndots:5` on, (2) running a single CoreDNS replica, and (3) not installing NodeLocalDNS at any meaningful scale. Each of the three is the cause of more pages than any single application bug.

The one sentence to keep in your head: **CoreDNS is a plugin-chained server (kubernetes plugin = watch-driven in-memory cache, forward plugin = recursive resolver, cache plugin = LRU), the cluster DNS contract is three lines of `/etc/resolv.conf`, the slowness comes from `ndots:5`, the safety comes from NodeLocalDNS, and the rest of this chapter is the matrix of variations.**
