# Services and kube-proxy

The Service is the single abstraction that makes Kubernetes networking *useful*. Pods come and go — they crash, get rescheduled, scale up, scale down, and recycle IPs. If clients had to track those IPs directly, no Kubernetes deployment would survive its first rolling update. The Service interposes a **stable virtual IP** and a **DNS name** between clients and a churning fleet of pod IPs, and **kube-proxy** is the per-node agent that programs the kernel so that traffic to that VIP actually reaches one of the backing pods.

This chapter is about the data plane of cluster-internal load balancing: how a packet leaves a pod with `dst = 10.96.42.10:80` and ends up in another pod with `dst = 10.244.7.13:8080`, at the level of netfilter chains, conntrack entries, IPVS hash tables, and eBPF socket-lookup programs. It is the kernel-side complement to [chapter 11 (Pods)](11-pod-internals.md), which described what makes a pod *reachable*, and a precursor to [chapter 15 (CNI)](15-cni-deep-dive.md), which describes pod-to-pod networking — the layer kube-proxy sits *on top of*. The eBPF replacement story (Cilium's kube-proxy-free mode) is forward-referenced to [chapter 16](16-cilium-ebpf.md); DNS resolution of Service names to VIPs is [chapter 18](18-dns-coredns.md); L7 (Ingress / Gateway API) is [chapter 17](17-ingress-gateway.md).

The kernel mechanics (netfilter hooks, conntrack, NAT, the `iptables` vs `nftables` vs IPVS subsystems) were introduced in [databases ch 00 §netfilter](../databases/00-os-and-hardware-internals.md) for a different reason; we will lean on them heavily here and re-introduce only what is necessary.

If you only remember one sentence from this chapter: **a Service is a controller-maintained mapping from a virtual IP to a set of pod endpoints; kube-proxy turns that mapping into kernel forwarding rules on every node, and is *not* in the data path — once the rules are programmed, the kernel does all the work.**

---

## Table of Contents

1. [What a Service Actually Is](#1-what-a-service-actually-is)
2. [The Five Service Types](#2-the-five-service-types)
3. [The Selector → Endpoints Pipeline](#3-the-selector--endpoints-pipeline)
4. [EndpointSlice: Sharding Endpoints for Scale](#4-endpointslice-sharding-endpoints-for-scale)
5. [Endpoint Conditions: ready, serving, terminating](#5-endpoint-conditions-ready-serving-terminating)
6. [kube-proxy: Role and Lifecycle](#6-kube-proxy-role-and-lifecycle)
7. [kube-proxy Mode: iptables](#7-kube-proxy-mode-iptables)
8. [kube-proxy Mode: IPVS](#8-kube-proxy-mode-ipvs)
9. [kube-proxy Mode: nftables](#9-kube-proxy-mode-nftables)
10. [kube-proxy Replaced: eBPF Socket-Level LB](#10-kube-proxy-replaced-ebpf-socket-level-lb)
11. [A Packet's Journey Through iptables Mode](#11-a-packets-journey-through-iptables-mode)
12. [Session Affinity](#12-session-affinity)
13. [externalTrafficPolicy](#13-externaltrafficpolicy)
14. [internalTrafficPolicy](#14-internaltrafficpolicy)
15. [Topology-Aware Routing](#15-topology-aware-routing)
16. [Dual-Stack Services (IPv4 + IPv6)](#16-dual-stack-services-ipv4--ipv6)
17. [Multi-Port Services](#17-multi-port-services)
18. [The kubernetes.default Service](#18-the-kubernetesdefault-service)
19. [NodePort: Range and Reservation](#19-nodeport-range-and-reservation)
20. [LoadBalancer: Cloud Integration](#20-loadbalancer-cloud-integration)
21. [The End-to-End External Traffic Picture](#21-the-end-to-end-external-traffic-picture)
22. [Hairpin: Pod → Service → Self](#22-hairpin-pod--service--self)
23. [conntrack and Service Traffic](#23-conntrack-and-service-traffic)
24. [Debugging Services in Practice](#24-debugging-services-in-practice)
25. [The Replacement Path: iptables → nftables → eBPF](#25-the-replacement-path-iptables--nftables--ebpf)
26. [Observability and Alerts](#26-observability-and-alerts)
27. [Pitfalls](#27-pitfalls)
28. [TL;DR](#28-tldr)

---

## 1. What a Service Actually Is

A Service is, in the spec, just three things:

1. A **selector** (a set of labels) that picks out which pods are backends.
2. A **port list** (one or more `port → targetPort` mappings, plus a protocol).
3. A **type** (ClusterIP / NodePort / LoadBalancer / ExternalName, plus the special `clusterIP: None` "headless" variant).

In the cluster it becomes four things:

1. A **stable virtual IP** (the ClusterIP), allocated by the apiserver out of `--service-cluster-ip-range`, written into `spec.clusterIP`.
2. A **DNS name** (`<svc>.<ns>.svc.cluster.local`) maintained by CoreDNS, resolving to that VIP. See [ch 18](18-dns-coredns.md).
3. An **Endpoints** (legacy) and one or more **EndpointSlice** objects, kept in sync by the endpoints-controller and endpointslice-controller, listing the pod IPs that currently match the selector and are Ready.
4. A set of **kernel forwarding rules** on every node, programmed by kube-proxy from the EndpointSlices, that DNAT the VIP to a chosen backend.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              The Service Plane                            │
│                                                                           │
│  apiserver                                                                │
│    │                                                                      │
│    │ Service { selector: app=web, port: 80, type: ClusterIP }            │
│    │                                                                      │
│    ▼                                                                      │
│  ┌──────────────────────┐         ┌──────────────────────────┐           │
│  │  Allocator           │         │  EndpointSlice controller│           │
│  │  ClusterIP from      │         │  watches Pods + Service │           │
│  │  --service-cluster-  │         │  → writes EndpointSlice  │           │
│  │  ip-range            │         │     (1..N per service)   │           │
│  └──────────┬───────────┘         └────────────┬─────────────┘           │
│             │ clusterIP = 10.96.42.10           │                         │
│             ▼                                   ▼                         │
│  ┌──────────────────────────────────────────────────────────┐            │
│  │                          etcd                              │            │
│  │  Service / Endpoints / EndpointSlice / Pod                │            │
│  └──────────────────────────────────────────────────────────┘            │
│             │                                   │                         │
│             ▼ watch                             ▼ watch                  │
│  ┌─────────────────────┐              ┌────────────────────┐            │
│  │  CoreDNS            │              │  kube-proxy        │            │
│  │  web.default.svc    │              │  on every node     │            │
│  │     → 10.96.42.10   │              │  iptables/IPVS/nft │            │
│  └─────────────────────┘              └─────────┬──────────┘            │
│                                                  │                       │
│                                                  ▼                       │
│                            ┌─────────────────────────────────────┐      │
│                            │  Kernel forwarding rules per node:  │      │
│                            │  10.96.42.10:80 ──DNAT──► pod IP    │      │
│                            └─────────────────────────────────────┘      │
└──────────────────────────────────────────────────────────────────────────┘
```

The key conceptual move is **decoupling**: clients name a Service, never a Pod. The set of pod IPs behind the Service can churn arbitrarily — rolling updates, autoscaler events, evictions, node failures — and the only thing that has to propagate to clients is *nothing*, because clients never knew the pod IPs in the first place. They knew the VIP. The kernel handles the change.

This decoupling has a price. Every Service requires:

- An IP allocation (a finite resource — see §19 on the cluster-ip range).
- A small amount of etcd storage (the Service + 1..N EndpointSlices).
- Rules in *every node's* kernel (iptables/IPVS/nft) that have to be rewritten whenever the endpoint set changes.

The third cost is what makes kube-proxy a scaling concern. With 5,000 Services × 10 endpoints, a node has 50,000 forwarding rules. In iptables mode each packet linearly walks those rules; in IPVS / nftables / eBPF, dispatch is O(1) via hash tables or verdict maps. We will spend most of the chapter on that distinction.

### 1.1 Why Not Just Use DNS?

A natural question: if every Service has a DNS name, why not skip the VIP entirely and have DNS return the current set of pod IPs directly? That's exactly what a *headless* Service does (§2.5), and the answer to "why not always" is:

1. **DNS caching is the enemy of churn.** Clients cache DNS for the TTL (typical: 30s minimum, often longer due to broken resolvers). When a pod restarts and gets a new IP, clients that already have the old IP cached will keep trying it for the TTL. A VIP never changes, so client-side DNS caching becomes harmless.
2. **DNS doesn't carry port-level liveness.** A pod IP returned by DNS might be perfectly resolvable but the pod could be terminating, draining, or unhealthy. kube-proxy filters EndpointSlices by readiness *every reconcile*. DNS clients have no such signal short of re-resolving.
3. **DNS load balancing is weak.** Most resolvers either pick the first record or round-robin within the response set, both poorly. kube-proxy can implement proper random/least-conn/sessionAffinity.
4. **DNS resolution is per-syscall, kernel routing is per-packet.** A VIP-based world resolves the name *once* at connect time; the kernel handles the per-packet load balancing thereafter. DNS-based round-robin requires the application to re-resolve frequently — which clients almost never do correctly.

Headless Services are still useful when the *client* wants per-pod addressability (StatefulSet ordinal access, peer discovery in clustered databases). For everything else, VIP > DNS-only.

### 1.2 The VIP Is Not Routed

A subtle point that confuses staff engineers coming from traditional networking: the ClusterIP is not routed. It does not exist on any interface. No machine on the network "owns" it. There is no ARP responder for it. It is a purely *fictional* address that exists only inside the netfilter / IPVS / eBPF rules on each node.

A packet to `10.96.42.10` doesn't get *delivered* anywhere; it gets *rewritten* (DNATed) to a real pod IP before it ever leaves the node. The rewrite happens in the OUTPUT chain (for packets originating on the node) or PREROUTING chain (for packets entering the node from a pod's veth or from a NodePort). After the rewrite, the destination is a real pod IP, which *is* routed by the CNI.

This is why `ping 10.96.42.10` typically fails (depending on mode) — there's nothing to reply, and ICMP doesn't carry a port so the DNAT rules don't match. Services are TCP/UDP/SCTP only. `kubectl get svc` tells you the VIP but you cannot reach it from outside the cluster's kube-proxy-programmed nodes.

---

### 1.3 The Three Identities of a Service

A staff-level insight: a Service is simultaneously three things, depending on which layer you're looking at.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       Three Faces of a Service                           │
│                                                                          │
│   Layer 7 (application)                                                 │
│   ─ Name: "web.default.svc.cluster.local"                              │
│   ─ Resolution: CoreDNS A record                                       │
│   ─ Stability: forever (until you delete the Service)                  │
│                                                                          │
│   Layer 4 (transport / kube-proxy)                                      │
│   ─ Name: 10.96.42.10:80                                                │
│   ─ Resolution: kernel netfilter / IPVS / nftables / eBPF rule         │
│   ─ Stability: cluster lifetime (clusterIP is sticky across restarts)  │
│                                                                          │
│   Layer 3 (network / endpoint set)                                     │
│   ─ Identity: { 10.244.1.10:8080, 10.244.2.11:8080, ... }              │
│   ─ Resolution: endpointslice-controller picks pods matching selector  │
│   ─ Stability: per-reconcile, milliseconds                             │
└─────────────────────────────────────────────────────────────────────────┘
```

Each layer talks to the one below via an indirection. DNS → VIP. VIP → endpoint. Endpoint → pod. The Service is what *connects* these layers, and the entire job of kube-proxy is the middle arrow.

When debugging "the Service doesn't work", you have to know which layer is failing. DNS not resolving? That's CoreDNS, not kube-proxy. VIP resolving but connection refused? That's kube-proxy rules or empty endpoints. Endpoints populated but pod unreachable? That's CNI / pod networking. Knowing the three faces is the single biggest debugging skill.

---

## 2. The Five Service Types

The `type` field selects one of four variants in the API (plus the orthogonal "headless" mode triggered by `clusterIP: None`).

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       Service Types                                      │
│                                                                          │
│  ┌──────────────┐    ClusterIP only, internal                           │
│  │  ClusterIP   │    VIP from --service-cluster-ip-range                │
│  │  (default)   │    DNS: <svc>.<ns>.svc.cluster.local → VIP            │
│  └──────────────┘                                                        │
│                                                                          │
│  ┌──────────────┐    ClusterIP + a port on every node (30000-32767)     │
│  │  NodePort    │    NodeIP:nodePort → DNAT → ClusterIP path            │
│  │              │    Reachable from outside the cluster                 │
│  └──────────────┘                                                        │
│                                                                          │
│  ┌──────────────┐    NodePort + cloud LB provisioned by CCM             │
│  │ LoadBalancer │    LB.public_ip:port → all nodes' NodePort            │
│  │              │    status.loadBalancer.ingress[] populated            │
│  └──────────────┘                                                        │
│                                                                          │
│  ┌──────────────┐    No VIP, no proxy. CoreDNS returns a CNAME          │
│  │ ExternalName │    pointing at spec.externalName (e.g. db.aws.com).   │
│  │              │    Pure DNS-level redirect.                           │
│  └──────────────┘                                                        │
│                                                                          │
│  ┌──────────────┐    clusterIP: None                                    │
│  │   Headless   │    No VIP. CoreDNS returns one A record per pod.     │
│  │              │    Clients see real pod IPs (often used by clients   │
│  │              │    that want to do their own LB or by StatefulSets).  │
│  └──────────────┘                                                        │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.1 ClusterIP (the default)

A VIP, cluster-internal only. This is 90% of all Services in a normal cluster.

```yaml
apiVersion: v1
kind: Service
metadata: { name: web, namespace: default }
spec:
  type: ClusterIP            # default; can be omitted
  selector: { app: web }
  ports:
    - name: http
      port: 80               # the VIP's port
      targetPort: 8080       # the pod's port (defaults to port if omitted)
      protocol: TCP
```

After creation:

```
$ kubectl get svc web
NAME   TYPE        CLUSTER-IP     EXTERNAL-IP   PORT(S)   AGE
web    ClusterIP   10.96.42.10    <none>        80/TCP    3s
```

That VIP is reachable from any pod in the cluster (and from the host netns of any node, since kube-proxy programs the OUTPUT chain on each node).

### 2.2 NodePort

A NodePort Service is a ClusterIP Service *plus* a port (default range 30000–32767, configurable via `--service-node-port-range` on the apiserver) opened on **every node**. Traffic to `<any node IP>:<nodePort>` is DNATed to the same backend set as the ClusterIP.

```yaml
spec:
  type: NodePort
  selector: { app: web }
  ports:
    - port: 80
      targetPort: 8080
      nodePort: 30080        # optional; otherwise random in range
```

Why "every node"? Because there is no information at the cloud LB layer about *which* nodes have pods. Even nodes with no pods accept the NodePort and forward to other nodes (in `externalTrafficPolicy: Cluster`). That cross-node forwarding is what makes NodePort robust but also what makes srcIP get clobbered by SNAT (see §13).

A NodePort Service still has a ClusterIP — internal traffic uses the VIP, external traffic uses NodeIP:NodePort. They share the same endpoint set.

### 2.3 LoadBalancer

NodePort plus a cloud LB. The cloud-controller-manager (CCM) watches LoadBalancer Services, provisions a load balancer in the provider (an ELB on AWS, an NLB on GCP, an Azure LB, a MetalLB-allocated VIP for bare metal), and populates `status.loadBalancer.ingress[]` with the LB's address.

```yaml
spec:
  type: LoadBalancer
  selector: { app: web }
  ports: [ { port: 80, targetPort: 8080 } ]
```

```
$ kubectl get svc web
NAME   TYPE           CLUSTER-IP     EXTERNAL-IP        PORT(S)        AGE
web    LoadBalancer   10.96.42.10    a1b2c3.elb.aws...  80:30080/TCP   12s
```

Note three IPs / ports now: the LB's external address, the ClusterIP, and the NodePort. The LB is configured to forward to all nodes' NodePort. A node with no local pod will still accept the connection and forward it to a node that *does* have one (in `Cluster` policy) — see §13 and §21.

Cloud-specific behavior is steered by annotations:

```yaml
metadata:
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: nlb
    service.beta.kubernetes.io/aws-load-balancer-scheme: internal
    service.beta.kubernetes.io/aws-load-balancer-backend-protocol: tcp
```

We discuss this stack in [ch 37 (cloud controllers)](37-cloud-controllers.md).

### 2.4 ExternalName

ExternalName is the odd one out: no proxying, no VIP, no kube-proxy. It is a pure DNS-level CNAME. CoreDNS returns the value of `spec.externalName` whenever the Service name is looked up.

```yaml
spec:
  type: ExternalName
  externalName: my-db.us-east-1.rds.amazonaws.com
```

`web.default.svc.cluster.local` → CNAME → `my-db.us-east-1.rds.amazonaws.com`. The pod's resolver chases the CNAME and connects directly. Useful for "lift in-cluster, abstract the external DB behind a name we can change later" patterns.

### 2.5 Headless (`clusterIP: None`)

A Service with `clusterIP: None` has no VIP and no kube-proxy involvement at all. Its DNS name resolves to one A record per Ready endpoint pod IP (an A-record list). Used heavily by StatefulSets, where clients want stable per-pod DNS names (`web-0.web.default.svc.cluster.local`) and may want to do their own load balancing (Cassandra, Kafka, etcd, etc.).

```yaml
spec:
  clusterIP: None
  selector: { app: cassandra }
  ports: [ { port: 9042 } ]
```

```
$ kubectl exec -it pod -- dig +short cassandra.default.svc.cluster.local
10.244.1.10
10.244.2.11
10.244.3.12
```

EndpointSlices are still created for headless Services; CoreDNS reads them to build the A records. kube-proxy explicitly skips headless Services when programming rules.

---

## 3. The Selector → Endpoints Pipeline

The bridge from "Service spec" to "set of pod IPs" is a controller in kube-controller-manager called the **endpoints-controller** (and its newer sibling, the **endpointslice-controller**). They watch:

- All Services (to know what selectors and ports are wanted).
- All Pods (to compute which pods match each selector).
- All Nodes (to populate the `nodeName` field on EndpointSlice).

For each Service, the controller:

1. Lists pods matching `spec.selector` in the same namespace.
2. Filters to pods with `status.phase == Running` and a Ready condition (or, more precisely, applies the `Ready` / `Serving` / `Terminating` filters — see §5).
3. Writes an Endpoints object (legacy) and one or more EndpointSlice objects with the resulting addresses.

```
┌──────────────────────────────────────────────────────────────────────┐
│                  Endpoints / EndpointSlice Controller                 │
│                                                                       │
│   ┌─────────────────────┐                                             │
│   │  watch Services     │──┐                                          │
│   └─────────────────────┘  │                                          │
│                            │                                          │
│   ┌─────────────────────┐  │     ┌───────────────────────────┐       │
│   │  watch Pods         │──┼────►│ for each Service:          │       │
│   └─────────────────────┘  │     │  - find matching pods      │       │
│                            │     │  - filter by ready/serving│       │
│   ┌─────────────────────┐  │     │  - shard into slices      │       │
│   │  watch Nodes (zone) │──┘     │  - write EndpointSlice(s) │       │
│   └─────────────────────┘        └───────────────────────────┘       │
└──────────────────────────────────────────────────────────────────────┘
```

The pre-1.21 legacy `Endpoints` object is still maintained (for backwards compatibility with controllers that watch it) but `EndpointSlice` is the source of truth for kube-proxy and CoreDNS.

### 3.1 The Legacy Endpoints Object: Why It Doesn't Scale

```yaml
apiVersion: v1
kind: Endpoints
metadata:
  name: web
  namespace: default
subsets:
  - addresses:
      - { ip: 10.244.1.10, nodeName: node-a, targetRef: { kind: Pod, name: web-abc } }
      - { ip: 10.244.2.11, nodeName: node-b, targetRef: { kind: Pod, name: web-def } }
    ports:
      - { name: http, port: 8080, protocol: TCP }
```

This worked fine for small clusters. It broke at three levels for big ones:

1. **etcd object size limit (~1 MB).** Each address is ~150 bytes; you hit the limit at roughly 5,000 endpoints per service. Beyond that, the apiserver rejects the update with `request entity too large`.
2. **Write storm.** Every time *one* pod becomes Ready or Terminating, the *entire* Endpoints object is rewritten in etcd, sent to every watcher (every kube-proxy and every CoreDNS). On a 1000-node cluster with a 5000-endpoint Service, a single pod restart triggers 1000 × ~750 KB ≈ 750 MB of watch traffic.
3. **Atomic update.** The Endpoints object has to be valid as a whole. There's no way to update just the part that changed.

EndpointSlice was created to solve all three.

---

### 3.2 The Endpoints Controller Reconcile Loop

Source: `pkg/controller/endpoint/endpoints_controller.go`. The loop:

```
on Service add/update/delete:
  enqueue serviceKey
on Pod add/update/delete:
  for each Service whose selector matches:
    enqueue serviceKey
on Node update (zone/region change):
  for each Service whose endpoints reference this Node:
    enqueue serviceKey

processNextWorkItem:
  serviceKey = queue.Get()
  pods = lister.Pods(ns).List(svc.Selector)
  desired = filterReadyAndCompute(pods, svc.Ports)
  current = lister.Endpoints(ns).Get(svc.Name)
  if !equal(desired, current):
    update or create Endpoints(ns, svc.Name)
  queue.Done(serviceKey)
```

The labels-to-pods lookup is O(matching pods) thanks to indexed informers — each Service's selector is matched against a pre-indexed pod set in the SharedIndexInformer. The cost driver in big clusters is not the selector match but the **diff + serialization** of large Endpoints objects, which is exactly what EndpointSlice was designed to mitigate.

### 3.3 What Counts as "Ready"

The pod-readiness gate has multiple layers:

1. **Pod phase**: must be `Running` (not Pending, Succeeded, Failed, Unknown). Terminating pods (phase=Running but with DeletionTimestamp) get special handling.
2. **All container statuses**: `ContainersReady == true`.
3. **ReadinessProbe**: each container's readiness probe must be passing (if defined).
4. **ReadinessGates**: any user-defined readiness gates in `spec.readinessGates` must be `True` in `status.conditions`. This lets external controllers gate readiness (e.g., a load-balancer-target-group controller marks the pod ready only when registered with the ALB).
5. **`spec.publishNotReadyAddresses`**: a Service-level override that publishes endpoints regardless of readiness. Used by StatefulSets so peer discovery works before all pods are individually Ready.

A subtle gotcha: if no readiness probe is defined, pods are considered Ready as soon as their containers are ContainersReady (which roughly means "process started"). This is faster than you might want for slow-starting apps. Always define a readiness probe for real workloads.

---

## 4. EndpointSlice: Sharding Endpoints for Scale

EndpointSlice (v1, default since 1.21) shards a single Service's endpoints across multiple smaller objects, each capped at a configurable size (default 100, configurable via `--max-endpoints-per-slice` on kube-controller-manager).

```yaml
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: web-x7k2p                           # Service name + random suffix
  namespace: default
  labels:
    kubernetes.io/service-name: web         # links back to Service
    endpointslice.kubernetes.io/managed-by: endpointslice-controller.k8s.io
addressType: IPv4                            # IPv4 / IPv6 / FQDN
ports:
  - name: http
    port: 8080
    protocol: TCP
endpoints:
  - addresses: [ "10.244.1.10" ]
    conditions: { ready: true, serving: true, terminating: false }
    nodeName: node-a
    zone: us-east-1a
    targetRef: { kind: Pod, name: web-abc, uid: "..." }
    hints:
      forZones: [ { name: us-east-1a } ]    # populated by topology-aware routing
  - addresses: [ "10.244.2.11" ]
    conditions: { ready: true, serving: true, terminating: false }
    nodeName: node-b
    zone: us-east-1b
    targetRef: { kind: Pod, name: web-def, uid: "..." }
```

Key properties:

- **Multiple slices per service.** A Service with 1,000 endpoints might have ~10 slices of ~100 endpoints each (the controller fills slices up to capacity and creates new ones as needed; it doesn't strictly fill, to avoid thrashing when single endpoints come and go — see "slot reuse" below).
- **AddressType-partitioned.** Dual-stack services have at least two slices (one IPv4, one IPv6).
- **Port-set-partitioned.** A multi-port service whose ports diverge across endpoints (rare but possible) gets multiple slices, one per port-set.
- **Incremental updates.** A single pod becoming Ready rewrites *one slice* (~10 KB), not the entire endpoint set. Watch fan-out drops by an order of magnitude.

```
                  Service "web" (selects ~250 pods)
                          │
                          ▼
        ┌─────────────────┴──────────────────┐
        │                                    │
   EndpointSlice                       EndpointSlice                 EndpointSlice
   web-x7k2p                           web-9abcd                     web-mnopq
   addressType: IPv4                   addressType: IPv4             addressType: IPv4
   100 endpoints                       100 endpoints                 50 endpoints
   ▲                                                                   ▲
   │                                                                   │
   pod becomes Ready ─────► only this slice updated ◄─── pod terminates
```

### 4.1 The EndpointSlice Controller's Algorithm

Source: `pkg/controller/endpointslice/reconciler.go`.

For each Service in the work queue:

1. List all existing EndpointSlices owned by this Service (label selector `kubernetes.io/service-name=<svc>`).
2. List all Ready/Serving/Terminating pods matching the selector.
3. Compute the desired endpoint set, partitioned by (AddressType, PortSet).
4. Diff against existing slices:
   - Endpoints to add → fill existing under-capacity slice, or create new one.
   - Endpoints to remove → delete from the slice they're in.
   - Endpoints to update (e.g., readiness change) → patch the slice they're in.
5. Issue **at most one Create + one Update + one Delete per reconcile** to limit etcd write amplification (the "rate-limited" reconciler). The controller will requeue if more changes are needed; this smooths churn at the cost of slightly longer convergence under storms.

The controller also avoids "ping-ponging" between slices when an endpoint flaps: an endpoint that goes NotReady stays in its slice (with `ready: false`) rather than being deleted and re-added on the next Ready, so flapping does not generate slice-create / slice-delete storms.

### 4.2 Why This Scales

Concrete numbers (from real large clusters):

| Cluster size           | Endpoints | Legacy Endpoints object | EndpointSlices                |
| ---------------------- | --------- | ----------------------- | ------------------------------ |
| 100 nodes, 1k pods/svc | 1,000     | 1 object × 150 KB       | 10 slices × ~15 KB each        |
| 1000 nodes, 5k pods/svc| 5,000     | At etcd object limit    | 50 slices × ~15 KB each        |
| 1000 nodes, 50k pods/svc| 50,000   | Impossible              | 500 slices × ~15 KB each       |

One pod change at 50k endpoints:

- Legacy: rewrite 7 MB object → reject (too big), or with smaller endpoints rewrite 5 MB → 1000 watchers × 5 MB = 5 GB of watch fanout.
- EndpointSlice: rewrite 15 KB → 1000 watchers × 15 KB = 15 MB of watch fanout. **333x reduction.**

---

### 4.3 Mirror Slices vs Custom Slices

EndpointSlices fall into two categories:

1. **Controller-managed** (the common case): owned by the endpointslice-controller, derived from Service selector. Labeled `endpointslice.kubernetes.io/managed-by=endpointslice-controller.k8s.io`.
2. **User/operator-managed**: created by other controllers (a load-balancer controller, a service-mesh controller, or hand-rolled YAML). Labeled with a different `managed-by`. The endpointslice-controller leaves these alone.

This is how operators expose "Services backed by something other than pods" — e.g., a Service representing an external database, fronted by a static set of EndpointSlices pointing at the DB's IPs. kube-proxy and CoreDNS read both kinds the same way; the source doesn't matter to them.

### 4.4 The "Endpoint vs EndpointSlice" Migration Reality

EndpointSlice has been default since 1.21, but the legacy `Endpoints` object is still maintained for backwards compatibility:

- Controllers built before EndpointSlice (older versions of nginx-ingress-controller, MetalLB, certain operators) only watched `Endpoints`. The endpoints-controller still runs alongside the endpointslice-controller to keep `Endpoints` in sync.
- kube-proxy itself switched to watching EndpointSlice in 1.19; older kube-proxy versions watched Endpoints.
- CoreDNS switched to EndpointSlice in v1.9.0.

If you see different endpoint sets between `kubectl get endpoints` and `kubectl get endpointslices`, you're catching the controllers mid-reconcile — they're independently maintained and eventual-consistent. The discrepancy should resolve within the next sync.

The `Endpoints` object is deprecated for removal but no removal date is set. It will probably linger for many years for compatibility.

---

## 5. Endpoint Conditions: ready, serving, terminating

An EndpointSlice address has three boolean conditions, and the difference between them is one of the most underappreciated subtleties in Kubernetes networking.

```
┌──────────────────────────────────────────────────────────────────────┐
│                  EndpointSlice Conditions                             │
│                                                                       │
│  ready       The pod is fully Ready: ContainersReady AND             │
│              readinessProbe passing AND not terminating.              │
│              kube-proxy: program this endpoint by default.           │
│                                                                       │
│  serving     The pod is willing to accept *new* connections.          │
│              True ⇔ readinessProbe passing (regardless of            │
│              terminating). False during preStop / draining.          │
│                                                                       │
│  terminating The pod has a DeletionTimestamp set.                    │
│              Used to identify "in the middle of shutdown" pods.      │
└──────────────────────────────────────────────────────────────────────┘
```

The interactions:

| State                   | ready | serving | terminating |
| ----------------------- | ----- | ------- | ----------- |
| Fully Ready             | true  | true    | false       |
| Failing readiness probe | false | false   | false       |
| Draining (in preStop)   | false | true    | true        |
| Drained (preStop done)  | false | false   | true        |
| Pod just created, not yet Ready | false | false | false  |

The **draining** state is the critical one. A pod entering termination still has open connections, possibly long-running ones (gRPC streams, websockets, large downloads). It should not receive *new* traffic, but the *existing* connections must continue to be served until they complete or until `terminationGracePeriodSeconds` elapses.

The default kube-proxy behavior is to include only endpoints with `ready=true`. KEP-1669 (Proxy Terminating Endpoints, GA in 1.28) added the `serving` condition and made kube-proxy fall back to `serving=true, terminating=true` endpoints **only when no ready endpoints exist** — preventing a brief outage during rolling updates when all old pods become terminating before new pods become ready.

This same condition set is also what `publishNotReadyAddresses: true` on the Service exposes: it tells kube-proxy to include even `ready=false, serving=false` endpoints, used historically by StatefulSets (so peer pods can discover each other before they're individually Ready).

---

### 5.1 The Rolling-Update Outage Pattern That Conditions Fix

Before KEP-1669 added the `serving` condition (pre-1.20), a rolling Deployment update could have a brief outage:

```
Time   Old pod state    New pod state    Endpoints
T+0s   Ready            -                [old]              ← steady state
T+1s   Terminating      Pending          []                 ← OUTAGE
T+2s   Terminating      Pending          []
...
T+30s  -                Ready            [new]              ← recovered
```

During the gap, the old pod is removed from endpoints because it's terminating, and the new pod isn't yet Ready. Any traffic during this window sees empty endpoints → connection refused.

After KEP-1669, with the `serving` condition:

```
Time   Old pod state              New pod state    Endpoints (filtered)
T+0s   Ready                      -                [old]
T+1s   Terminating, serving=true  Pending          [old (terminating)]
T+2s   Terminating, serving=true  Pending          [old (terminating)]
T+30s  Terminated                 Ready            [new]
```

kube-proxy includes the old pod's IP *as long as it's still serving*, even though it's terminating, so traffic continues to flow until the new pod is Ready. The application sees no outage.

For this to work the app must implement a clean shutdown: receive SIGTERM, mark itself unhealthy for *new* readiness probes (so kube-proxy eventually stops routing once new pods are up), but continue serving in-flight and new connections during the grace period. This is the **preStop hook + graceful shutdown** pattern, see [ch 11 §14](11-pod-internals.md).

---

## 6. kube-proxy: Role and Lifecycle

kube-proxy is the per-node agent that translates EndpointSlices into kernel forwarding rules. It is typically deployed as a DaemonSet (kubeadm creates it as one) or, in some distros, as a static pod or systemd unit. It runs with hostNetwork=true, capabilities=NET_ADMIN, and access to the kernel's iptables/IPVS/nftables interfaces.

```
┌──────────────────────────────────────────────────────────────────────┐
│                    kube-proxy on a single node                        │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  Watchers (client-go)                                           │  │
│  │    - Service                                                    │  │
│  │    - EndpointSlice                                              │  │
│  │    - Node (for topology / zones)                                │  │
│  └─────────────────────┬──────────────────────────────────────────┘  │
│                        │                                              │
│                        ▼                                              │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  proxy.Provider (mode-specific implementation)                  │  │
│  │    - iptables: pkg/proxy/iptables/proxier.go                    │  │
│  │    - ipvs:     pkg/proxy/ipvs/proxier.go                        │  │
│  │    - nftables: pkg/proxy/nftables/proxier.go                    │  │
│  │    - kernelspace (Windows only): pkg/proxy/winkernel/           │  │
│  │                                                                 │  │
│  │  - OnServiceAdd/Update/Delete                                   │  │
│  │  - OnEndpointSliceAdd/Update/Delete                             │  │
│  │  - syncProxyRules()  ← the actual rule-programming loop         │  │
│  └─────────────────────┬──────────────────────────────────────────┘  │
│                        │ syncProxyRules() throttled to               │
│                        │ minSyncPeriod (default 1s)                  │
│                        ▼                                              │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  Kernel state (programmed via netlink / iptables-restore)       │  │
│  │    iptables chains  /  IPVS table  /  nft tables                │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

### 6.1 kube-proxy Is *Not* in the Data Path

This is the single most important fact about kube-proxy and the source of constant confusion. **kube-proxy does not forward packets.** It is a control-plane process that *programs* the kernel and then watches for changes. Once the rules are in place, packets traverse iptables / IPVS / nft in the kernel directly, with kube-proxy not involved.

That means:

- If kube-proxy crashes, **existing traffic keeps flowing.** The kernel rules persist until something rewrites them. You typically lose only the ability to handle Service / EndpointSlice churn.
- kube-proxy can be restarted with no traffic impact (it picks up the existing rules on startup and reconciles to current state).
- The kube-proxy process's CPU usage is *not* per-packet; it's per-reconcile. Big spikes correlate with big endpoint set churn, not with traffic volume.

### 6.2 The Sync Loop

`syncProxyRules()` is the hot loop. It runs:

- Whenever a watch event arrives (Service / EndpointSlice / Node change), *throttled* by `--min-sync-period` (default 1s) — multiple changes within 1s coalesce into a single sync.
- At least every `--sync-period` (default 30s, can be raised), as a safety net to repair any drift between kube-proxy's view and the kernel's actual rules (e.g., another tool stomping on the chains).

In iptables mode, each `syncProxyRules` builds a complete `iptables-restore` blob containing *all* of kube-proxy's rules and ships it in one transaction. This is atomic but slow at scale (see §7.5). nftables and IPVS modes do incremental updates and are dramatically faster.

### 6.3 Lifecycle in kubeadm

Source: `cmd/kube-proxy/app/server.go`. kube-proxy reads a ConfigMap (`kube-proxy` in `kube-system`) that specifies mode, sync periods, and per-mode tuning. The DaemonSet mounts the ConfigMap and the host's `/run/xtables.lock` (for iptables coordination with other tools), `/lib/modules` (for IPVS kernel modules), and `/proc/sys` (for sysctl access).

```
$ kubectl -n kube-system get ds kube-proxy
NAME         DESIRED   CURRENT   READY   UP-TO-DATE   AVAILABLE   NODE SELECTOR
kube-proxy   200       200       200     200          200         kubernetes.io/os=linux
```

Per-node health is exposed at `:10256/healthz` (the liveness endpoint) and metrics at `:10249/metrics` (sync duration, programmed-rules count, etc.).

---

## 7. kube-proxy Mode: iptables

iptables mode has been the default since Kubernetes 1.2 (2016). It is by far the most-deployed mode and the model every other mode emulates.

Source: `pkg/proxy/iptables/proxier.go`.

### 7.1 The Chain Layout

kube-proxy installs a custom set of chains in the `nat` and `filter` tables, hooked off the standard PREROUTING and OUTPUT chains.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                   iptables Chain Graph (nat table)                        │
│                                                                           │
│  PREROUTING ──► KUBE-SERVICES ──┬──► KUBE-SVC-AAAA (svc A) ──► KUBE-SEP-1│
│  OUTPUT     ──►                 │                              KUBE-SEP-2│
│                                 ├──► KUBE-SVC-BBBB (svc B) ──► KUBE-SEP-3│
│                                 │                              KUBE-SEP-4│
│                                 ├──► KUBE-SVC-CCCC (svc C) ──► ...      │
│                                 └──► KUBE-NODEPORTS  ──► (NodePort svcs)│
│                                                                           │
│  POSTROUTING ──► KUBE-POSTROUTING ──► MASQUERADE selected packets        │
└──────────────────────────────────────────────────────────────────────────┘
```

Concretely, the top-level hookups look like:

```
*nat
-A PREROUTING -m comment --comment "kubernetes service portals" -j KUBE-SERVICES
-A OUTPUT     -m comment --comment "kubernetes service portals" -j KUBE-SERVICES
-A POSTROUTING -m comment --comment "kubernetes postrouting rules" -j KUBE-POSTROUTING
```

`KUBE-SERVICES` is the dispatcher. It has one rule per Service, matched on `(destIP, destPort, protocol)`:

```
-A KUBE-SERVICES -d 10.96.42.10/32 -p tcp -m tcp --dport 80 \
   -m comment --comment "default/web cluster IP" -j KUBE-SVC-XPTPB7777ATXXXXX
-A KUBE-SERVICES -d 10.96.0.10/32  -p udp -m udp --dport 53 \
   -m comment --comment "kube-system/kube-dns:dns cluster IP" -j KUBE-SVC-TCOU7JCQXEZGVUNU
-A KUBE-SERVICES -d 10.96.0.10/32  -p tcp -m tcp --dport 53 \
   -m comment --comment "kube-system/kube-dns:dns-tcp cluster IP" -j KUBE-SVC-ERIFXISQEP7F7OF4
-A KUBE-SERVICES ... -j KUBE-NODEPORTS                   # fallthrough for NodePort
```

When a packet matches one of these, control jumps to the per-Service `KUBE-SVC-XXXX` chain.

### 7.2 The Per-Service Chain (Endpoint Selection)

The per-Service chain implements load balancing using the `statistic` module with `mode random` — each rule fires with a probability that, in sequence, achieves uniform distribution.

For a Service with 3 endpoints:

```
-A KUBE-SVC-XPTPB7777ATXXXXX -m comment --comment "default/web -> 10.244.1.10:8080" \
   -m statistic --mode random --probability 0.33333333349 \
   -j KUBE-SEP-AAAAAAAAAAAAAAAA

-A KUBE-SVC-XPTPB7777ATXXXXX -m comment --comment "default/web -> 10.244.2.11:8080" \
   -m statistic --mode random --probability 0.50000000000 \
   -j KUBE-SEP-BBBBBBBBBBBBBBBB

-A KUBE-SVC-XPTPB7777ATXXXXX -m comment --comment "default/web -> 10.244.3.12:8080" \
   -j KUBE-SEP-CCCCCCCCCCCCCCCC
```

The probabilities are *conditional*: the first rule has p=1/3, the second p=1/2 *of the remaining*, the third is unconditional. Multiplied out: each endpoint has total probability 1/3. The kernel evaluates the rules top to bottom and stops at the first match, so the `statistic` module rolls a fresh die for each rule.

### 7.3 The Per-Endpoint Chain (DNAT)

Each `KUBE-SEP-YYYY` chain does the actual destination-NAT and, if needed, marks the packet for SNAT.

```
-A KUBE-SEP-AAAAAAAAAAAAAAAA -s 10.244.1.10/32 -j KUBE-MARK-MASQ
-A KUBE-SEP-AAAAAAAAAAAAAAAA -p tcp -m tcp -j DNAT --to-destination 10.244.1.10:8080
```

Two rules:

1. The first matches packets whose *source* is the same as the destination pod — i.e., the hairpin case (a pod connecting to a service whose backend turns out to be itself). It marks the packet for MASQUERADE so the return path works. See §22.
2. The second is the DNAT itself: rewrite `dst = (10.96.42.10, 80)` → `dst = (10.244.1.10, 8080)`.

After DNAT, the kernel creates a conntrack entry capturing the original tuple, so the return path can undo the NAT (see §11).

### 7.4 A Real iptables-save Excerpt

For a tiny cluster with one Service `default/web` (ClusterIP 10.96.42.10, 3 endpoints), the relevant kube-proxy-installed rules are:

```
*nat
:KUBE-SERVICES - [0:0]
:KUBE-NODEPORTS - [0:0]
:KUBE-POSTROUTING - [0:0]
:KUBE-MARK-MASQ - [0:0]
:KUBE-SVC-XPTPB7777ATXXXXX - [0:0]
:KUBE-SEP-AAAAAAAAAAAAAAAA - [0:0]
:KUBE-SEP-BBBBBBBBBBBBBBBB - [0:0]
:KUBE-SEP-CCCCCCCCCCCCCCCC - [0:0]

-A PREROUTING -m comment --comment "kubernetes service portals" -j KUBE-SERVICES
-A OUTPUT     -m comment --comment "kubernetes service portals" -j KUBE-SERVICES
-A POSTROUTING -m comment --comment "kubernetes postrouting rules" -j KUBE-POSTROUTING

-A KUBE-POSTROUTING -m mark ! --mark 0x4000/0x4000 -j RETURN
-A KUBE-POSTROUTING -j MARK --xor-mark 0x4000
-A KUBE-POSTROUTING -m comment --comment "kubernetes service masq" -j MASQUERADE --random-fully

-A KUBE-MARK-MASQ -j MARK --or-mark 0x4000

-A KUBE-SERVICES -d 10.96.42.10/32 -p tcp -m comment --comment "default/web cluster IP" \
   -m tcp --dport 80 -j KUBE-SVC-XPTPB7777ATXXXXX
-A KUBE-SERVICES -m comment --comment "kubernetes service nodeports; NOTE: this must be the last rule in this chain" \
   -m addrtype --dst-type LOCAL -j KUBE-NODEPORTS

-A KUBE-SVC-XPTPB7777ATXXXXX -m statistic --mode random --probability 0.33333333349 \
   -j KUBE-SEP-AAAAAAAAAAAAAAAA
-A KUBE-SVC-XPTPB7777ATXXXXX -m statistic --mode random --probability 0.50000000000 \
   -j KUBE-SEP-BBBBBBBBBBBBBBBB
-A KUBE-SVC-XPTPB7777ATXXXXX -j KUBE-SEP-CCCCCCCCCCCCCCCC

-A KUBE-SEP-AAAAAAAAAAAAAAAA -s 10.244.1.10/32 -j KUBE-MARK-MASQ
-A KUBE-SEP-AAAAAAAAAAAAAAAA -p tcp -m tcp -j DNAT --to-destination 10.244.1.10:8080

-A KUBE-SEP-BBBBBBBBBBBBBBBB -s 10.244.2.11/32 -j KUBE-MARK-MASQ
-A KUBE-SEP-BBBBBBBBBBBBBBBB -p tcp -m tcp -j DNAT --to-destination 10.244.2.11:8080

-A KUBE-SEP-CCCCCCCCCCCCCCCC -s 10.244.3.12/32 -j KUBE-MARK-MASQ
-A KUBE-SEP-CCCCCCCCCCCCCCCC -p tcp -m tcp -j DNAT --to-destination 10.244.3.12:8080

COMMIT
```

This is *one Service*. A 5,000-Service cluster has 5,000 rules in KUBE-SERVICES plus 5,000 KUBE-SVC chains plus N × 5,000 KUBE-SEP chains. For N=10 endpoints/service that's ~55,000 chains and ~110,000 rules.

### 7.5 The Performance Cliff

iptables uses a linear list of rules per chain. Every packet entering KUBE-SERVICES has to be tested against every rule until one matches.

```
KUBE-SERVICES: linear scan
┌────────────────────────────────────────────────────────────┐
│ rule 1:  dst=10.96.42.10  → KUBE-SVC-...                  │
│ rule 2:  dst=10.96.42.11  → KUBE-SVC-...                  │
│ rule 3:  dst=10.96.42.12  → KUBE-SVC-...                  │
│ ...                                                         │
│ rule 4999: dst=10.96.99.99 → KUBE-SVC-...                 │
│ rule 5000: fallthrough → KUBE-NODEPORTS                    │
└────────────────────────────────────────────────────────────┘
                          O(N) per packet
```

Two costs:

1. **Per-packet O(N) match cost.** On a node receiving 100k pps to many different VIPs, with 5k rules in KUBE-SERVICES, the kernel does ~500M comparisons/sec. Not free.
2. **O(N) update cost.** Adding or removing a rule means rebuilding the whole table (xtables locks the table for the duration). At 50k+ rules, a single full `iptables-restore` can take **multiple seconds** of CPU and lock the table the entire time.

The update cost matters more than the per-packet cost in practice. During a rolling Deployment update at 5k services, kube-proxy reconciles get throttled (the lock is held), rule sync latency p99 climbs to seconds, and Service-to-pod traffic continues to hit *stale* endpoints (recently terminated pods, missing newly-Ready pods) until the next sync completes. This is the canonical "kube-proxy doesn't scale" problem.

Mitigations within iptables mode:

- **`--minSyncPeriod`**: throttle sync to once per N seconds (default 1s). Higher values reduce sync overhead but increase staleness.
- **Incremental sync (1.26+)**: only rebuild the chains for Services whose endpoints actually changed, rather than the entire table. Helped a lot but didn't fix the fundamental O(N) per-packet cost.
- **Sharding the cluster.** Split into multiple smaller clusters once you start hitting the cliff at ~5–10k Services. Some teams do this as the cheap option.

The real fix is to switch modes. IPVS, nftables, or eBPF all replace the O(N) match with O(1) hash-table or map lookups.

---

## 8. kube-proxy Mode: IPVS

IPVS (IP Virtual Server, also known as LVS — Linux Virtual Server) is a kernel module for L4 load balancing that predates Kubernetes by ~20 years. It uses a kernel hash table indexed by `(VIP, port, protocol)` for O(1) service lookup. kube-proxy's IPVS mode (`--proxy-mode=ipvs`, beta in 1.9, GA in 1.11) uses IPVS for the load balancing and a small set of iptables rules (via `ipset` for set-based matching) for the pre/post-processing.

Source: `pkg/proxy/ipvs/proxier.go`.

### 8.1 The IPVS Data Structures

```
┌──────────────────────────────────────────────────────────────────────┐
│                       IPVS Kernel State                               │
│                                                                       │
│  Virtual Server Table (hash by VIP:port:proto, O(1) lookup)         │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  10.96.42.10:80 TCP  scheduler=rr  ──┐                         │  │
│  │                                       │                         │  │
│  │  10.96.0.10:53  UDP  scheduler=rr  ──┼──┐                      │  │
│  │  ...                                  │  │                      │  │
│  └───────────────────────────────────────┼──┼──────────────────────┘  │
│                                          ▼  ▼                          │
│                  ┌─────────────────────────────────────────────┐      │
│                  │  Real Server List (per virtual server)       │      │
│                  │  ┌─────────────────────────────────────┐    │      │
│                  │  │ 10.244.1.10:8080  weight=1  active │    │      │
│                  │  │ 10.244.2.11:8080  weight=1  active │    │      │
│                  │  │ 10.244.3.12:8080  weight=1  active │    │      │
│                  │  └─────────────────────────────────────┘    │      │
│                  │  scheduler picks one per connection         │      │
│                  └─────────────────────────────────────────────┘      │
│                                                                        │
│  Connection Table (in netfilter conntrack, IPVS uses ip_vs_conn)      │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │  (clientIP, clientPort, VIP, port) → real server, expires    │    │
│  │  Existing connections stick to their original real server.   │    │
│  └──────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

A single VIP lookup is a hash probe — O(1) regardless of cluster size. This is the fundamental scaling win over iptables.

### 8.2 ipsets and the iptables Bridge

IPVS by itself does not match arbitrary destination addresses on PREROUTING — it expects the VIP to be on a *local* interface. kube-proxy adds the VIP to the dummy interface `kube-ipvs0` and uses **ipsets** (kernel set data structures) to identify which destination IP / NodePort tuples belong to Kubernetes Services, for which it then needs masquerade or specific handling.

```
$ ip addr show kube-ipvs0
kube-ipvs0: <BROADCAST,NOARP> mtu 1500 qdisc noop state DOWN
    link/ether 16:fb:9f:9f:38:ad brd ff:ff:ff:ff:ff:ff
    inet 10.96.0.1/32 scope global kube-ipvs0
    inet 10.96.0.10/32 scope global kube-ipvs0
    inet 10.96.42.10/32 scope global kube-ipvs0
    ...
```

Every ClusterIP in the cluster is bound to `kube-ipvs0` as a /32. This is what makes IPVS willing to LB to it: the kernel sees `10.96.42.10` as "one of mine."

The handful of iptables rules kube-proxy installs alongside IPVS reference ipsets like `KUBE-CLUSTER-IP`, `KUBE-LOOP-BACK`, `KUBE-EXTERNAL-IP`:

```
-A KUBE-SERVICES -m set --match-set KUBE-CLUSTER-IP dst,dst -j KUBE-MARK-MASQ
```

Single rule, regardless of how many Services. The ipset is updated incrementally as Services come and go — adding/removing one entry is an O(1) hash operation.

### 8.3 ipvsadm Output

`ipvsadm -ln` shows the live IPVS state:

```
$ ipvsadm -ln
IP Virtual Server version 1.2.1 (size=4096)
Prot LocalAddress:Port Scheduler Flags
  -> RemoteAddress:Port           Forward Weight ActiveConn InActConn
TCP  10.96.0.1:443 rr
  -> 192.168.1.10:6443            Masq    1      4          0
  -> 192.168.1.11:6443            Masq    1      6          0
  -> 192.168.1.12:6443            Masq    1      3          0
TCP  10.96.42.10:80 rr
  -> 10.244.1.10:8080             Masq    1      12         3
  -> 10.244.2.11:8080             Masq    1      15         1
  -> 10.244.3.12:8080             Masq    1      11         2
UDP  10.96.0.10:53 rr
  -> 10.244.0.5:53                Masq    1      0          0
  -> 10.244.0.6:53                Masq    1      0          0
```

- `Prot LocalAddress:Port`: the VIP + service port.
- `Scheduler`: the algorithm (see §8.4).
- `Forward`: `Masq` for NAT mode (the only mode used by kube-proxy).
- `Weight`: per-real-server weight; kube-proxy sets all weights to 1 (no weighted LB at the Service level).
- `ActiveConn` / `InActConn`: live and TIME_WAIT connection counts.

### 8.4 Schedulers (Load-Balancing Algorithms)

IPVS supports many schedulers; kube-proxy exposes a subset via `--ipvs-scheduler`:

| Scheduler | Name | Behavior |
| --------- | ---- | -------- |
| `rr`  | Round Robin | Default. Sequential rotation through real servers. |
| `lc`  | Least-Connection | Send to the real server with the fewest active connections. |
| `dh`  | Destination Hashing | Hash by destination IP; same dst → same RS. (Not useful for Services since dst is always the VIP — pointless here.) |
| `sh`  | Source Hashing | Hash by source IP; same client → same RS. This is what kube-proxy uses for `sessionAffinity: ClientIP`. |
| `sed` | Shortest Expected Delay | Like `lc` but weighted; rarely useful in K8s. |
| `nq`  | Never Queue | If any RS is idle, send there; else fall back to `sed`. |
| `wrr` | Weighted Round Robin | Weighted variant; weights are always 1 in kube-proxy. |
| `wlc` | Weighted Least-Connection | Weighted `lc`. |
| `mh`  | Maglev Hashing | Consistent hashing (great for affinity + scaling). |

For most clusters, `rr` is fine. Switch to `mh` if you need consistent hashing for cache-like workloads, or `sh` is automatic for `sessionAffinity: ClientIP`.

### 8.5 IPVS Tradeoffs

**Pros:**

- O(1) lookup; scales to tens of thousands of Services with no degradation.
- Many more scheduling algorithms than iptables (which only has "random").
- Faster rule updates (no full table rebuild).
- Better visibility (`ipvsadm` gives per-RS connection counts).

**Cons:**

- Connection-table memory: every active connection has an `ip_vs_conn` entry. At a million connections per node, this is ~150 MB of kernel memory. Sizeable.
- Still uses iptables for masquerade, source filtering, and external-IP handling — debugging requires understanding both subsystems.
- `kube-ipvs0` dummy interface having every ClusterIP bound to it confuses `tcpdump` and routing tools.
- Per-flow scheduling state (which RS each client→VIP flow currently routes to) is in kernel memory, not durable across reboots, but does survive kube-proxy restarts.

IPVS was the production-recommended mode for large clusters from ~2018 through ~2024. nftables and eBPF are now superseding it.

---

## 9. kube-proxy Mode: nftables

`nftables` is the modern replacement for `iptables` — same netfilter substrate, completely different rule syntax and dispatcher. The kube-proxy nftables backend (beta in 1.29, GA in 1.33) gives you the iptables-mode mental model with O(1) dispatch via **verdict maps**.

Source: `pkg/proxy/nftables/proxier.go`.

### 9.1 What nftables Gives You

The key nftables primitive is a **verdict map**: a hash table whose keys are arbitrary (IP, port, protocol) tuples and whose values are *verdicts* (`jump <chain>`, `accept`, `drop`). A single map lookup picks the right per-Service chain in O(1), replacing iptables' linear KUBE-SERVICES scan.

```
nftables: O(1) verdict map dispatch
┌────────────────────────────────────────────────────────────┐
│ chain services:                                            │
│   ip daddr . meta l4proto . th dport  vmap @service-map   │
└────────────────────────────────────────────────────────────┘
                          │
                          ▼  (hash lookup)
┌────────────────────────────────────────────────────────────┐
│ map service-map:                                           │
│   10.96.42.10 . tcp . 80   : jump svc-XPTPB                │
│   10.96.0.10  . udp . 53   : jump svc-TCOU7                │
│   10.96.0.10  . tcp . 53   : jump svc-ERIFX                │
│   ...                                                       │
└────────────────────────────────────────────────────────────┘
```

A small nftables snippet (heavily abbreviated):

```
table ip kube-proxy {
    map service-ips {
        type ipv4_addr . inet_proto . inet_service : verdict
        elements = {
            10.96.42.10 . tcp . 80   : jump service-XPTPB,
            10.96.0.10  . udp . 53   : jump service-TCOU7,
        }
    }

    chain services {
        ip daddr . meta l4proto . th dport  vmap @service-ips
    }

    chain service-XPTPB {
        numgen random mod 3 vmap {
            0 : jump endpoint-AAAA,
            1 : jump endpoint-BBBB,
            2 : jump endpoint-CCCC
        }
    }

    chain endpoint-AAAA {
        ip saddr 10.244.1.10 jump mark-masq
        dnat to 10.244.1.10:8080
    }
    ...
}
```

Endpoint selection is *also* a verdict map keyed by a random number — O(1).

### 9.2 Why nftables Wins Over iptables

- **O(1) Service dispatch via verdict maps** vs O(N) linear scan in iptables.
- **Incremental updates** are first-class: add/remove a single map entry without rewriting the whole table. Atomic transactions are still available.
- **Single syntax** for IPv4 and IPv6 (`table inet` for dual-stack), vs separate iptables / ip6tables.
- **Sets and maps as first-class objects**, replacing the need for `ipset` as a sidecar (which IPVS mode required).
- **Faster rule sync**: full reconcile of 10k services takes ~100ms in nftables vs multiple seconds in iptables.

### 9.3 Why It's the Future Default

nftables mode preserves the iptables operator mental model (chains, DNAT, masquerade) while giving you O(1) dispatch *and* fast updates *and* sane dual-stack — all of IPVS's wins without IPVS's awkward dummy-interface and connection-table-memory issues. KEP-3866 lays out the path to making it the default in upcoming releases (1.34+). Once that lands, the iptables backend will be a legacy mode preserved for clusters running ancient kernels.

For staff engineers running 1.33+, switching from iptables → nftables is the highest-leverage scaling change available short of replacing kube-proxy entirely.

---

## 10. kube-proxy Replaced: eBPF Socket-Level LB

The most radical option is to **not run kube-proxy at all**. Cilium (and to a lesser degree Calico's eBPF dataplane) replaces kube-proxy with a set of eBPF programs that perform Service load balancing at a *completely different layer* of the kernel: the `connect(2)` syscall, before any packet is constructed.

We cover this in depth in [ch 16 (Cilium eBPF)](16-cilium-ebpf.md). Here is the executive summary.

### 10.1 The Idea: Socket-Level Load Balancing

```
┌────────────────────────────────────────────────────────────────────┐
│              Traditional kube-proxy (iptables/IPVS/nft)             │
│                                                                      │
│  app code:  connect(10.96.42.10:80)                                 │
│       │                                                              │
│       ▼                                                              │
│  socket created with dst = VIP                                       │
│       │                                                              │
│       ▼ SYN packet built with dst=VIP                                │
│  network stack → IP layer → netfilter PREROUTING/OUTPUT             │
│       │                                                              │
│       ▼  iptables/IPVS rule fires, DNATs dst from VIP → podIP       │
│  conntrack entry created                                             │
│       │                                                              │
│       ▼ SYN with dst=podIP leaves the host                          │
└────────────────────────────────────────────────────────────────────┘
                            VS

┌────────────────────────────────────────────────────────────────────┐
│              Cilium eBPF Socket-Level LB                             │
│                                                                      │
│  app code:  connect(10.96.42.10:80)                                 │
│       │                                                              │
│       ▼  eBPF program at BPF_CGROUP_INET4_CONNECT hook              │
│         hash-lookup VIP in Service map → pick endpoint              │
│         REWRITE sockaddr to (10.244.1.10, 8080) BEFORE             │
│         the socket is even initialized                              │
│       │                                                              │
│       ▼                                                              │
│  socket created with dst = podIP (already)                          │
│       │                                                              │
│       ▼ SYN packet built with dst=podIP                              │
│  network stack → IP layer → NO netfilter rule needed                │
│       │                                                              │
│       ▼ SYN with dst=podIP leaves the host                          │
│  NO conntrack entry needed for Service traffic                      │
└────────────────────────────────────────────────────────────────────┘
```

The eBPF program runs as a *cgroup hook* — Linux installs it into the cgroup hierarchy and the kernel runs it on every `connect(2)` made by any process in that cgroup. The program does:

1. Look up the destination address in an eBPF map (`cilium_lb4_services_v2`) containing all Service VIPs.
2. If matched, pick a backend (the same map structure also stores backend lists).
3. Rewrite the `sockaddr` pointer the kernel uses to initialize the outgoing socket.

From the application's perspective, it called `connect(VIP, 80)` and the socket is now connected to `(podIP, 8080)`. No NAT happens at all on the packet path because the packet was never destined for the VIP — by the time IP packets are built, the destination has already been rewritten at the socket layer.

### 10.2 Why This Is a Big Deal

- **Zero conntrack entries for ClusterIP traffic.** conntrack overflow is no longer a Service-scale concern.
- **No netfilter / iptables / IPVS rules to maintain.** All Service state lives in eBPF maps, updated via map operations (no chain rebuilds).
- **Sub-microsecond dispatch.** A hash-map lookup in eBPF is faster than even nftables verdict-map dispatch.
- **Works for any pod**: the cgroup hook covers all processes in the kubepods cgroup hierarchy.

### 10.3 Limitations

- **Only works for connections originating on a node where Cilium runs.** For ingress NodePort / LoadBalancer traffic *entering* a node from outside, Cilium uses XDP / tc-bpf programs at the NIC level (a different eBPF hook), which is similarly fast but a different code path.
- **Some older kernels lack the cgroup hooks.** Linux 4.10+ is required for v4, 5.7+ for full feature parity.
- **Debugging is harder** — you can't `iptables-save | grep` to inspect the state; you have to `cilium bpf lb list` and similar tooling. The on-ramp is steeper.
- **Per-flow stickiness with DSR (Direct Server Return) requires more careful design.**

For most large clusters in 2025+, the choice is increasingly between nftables-mode kube-proxy and Cilium eBPF kube-proxy-free. Both are O(1); eBPF wins on conntrack avoidance but loses on debuggability.

---

## 11. A Packet's Journey Through iptables Mode

To make all of this concrete, let's trace a single TCP connection from `pod-A` (10.244.1.50) on node-A to Service `web` (ClusterIP 10.96.42.10, port 80), which has three backends on three different nodes.

We'll follow the SYN packet, the SYN/ACK response, and the conntrack table state.

### 11.1 Setup

```
node-A (192.168.1.10)                  node-B (192.168.1.11)
┌───────────────────────────────┐     ┌───────────────────────────────┐
│  pod-A: 10.244.1.50           │     │  pod-B (backend): 10.244.2.11 │
│  veth0 ─┐                     │     │  veth0 ─┐                     │
│         │ cni0 bridge          │     │         │ cni0 bridge          │
│         │ 10.244.1.1           │     │         │ 10.244.2.1           │
│         │                      │     │         │                      │
│  host netns                    │     │  host netns                    │
│  eth0: 192.168.1.10            │     │  eth0: 192.168.1.11            │
└───────────────────────────────┘     └───────────────────────────────┘
                          \                /
                           \              /
                            underlay network
```

`web` Service:
- ClusterIP 10.96.42.10:80 → endpoints {10.244.1.10:8080, 10.244.2.11:8080, 10.244.3.12:8080}
- pod-B (10.244.2.11) is one of the backends.

### 11.2 The Full Trace: SYN Packet

```
Step 1 — pod-A application code:
  connect("10.96.42.10", 80)
  ↓
  Linux network stack inside pod-A's netns:
    - looks up routing table in netns
    - default route via 10.244.1.1 (cni0)
    - builds SYN: src=10.244.1.50:54321, dst=10.96.42.10:80

Step 2 — SYN leaves pod-A's veth → enters host netns:
  Packet now in host netns inbound at cni0
  
  netfilter PREROUTING runs:
    iptables nat PREROUTING ── jump ──► KUBE-SERVICES
      KUBE-SERVICES: match on dst=10.96.42.10/32, dport=80 → KUBE-SVC-XPTPB
        KUBE-SVC-XPTPB: 
          first rule p=1/3 (random) — say it fires, jump KUBE-SEP-BBBB
            (selecting backend 10.244.2.11:8080)
          KUBE-SEP-BBBB:
            saddr != 10.244.2.11 → KUBE-MARK-MASQ NOT taken (no hairpin)
            DNAT: dst rewritten from 10.96.42.10:80 → 10.244.2.11:8080
            ↓
            conntrack entry created:
              orig:  src=10.244.1.50:54321  dst=10.96.42.10:80
              reply: src=10.244.2.11:8080   dst=10.244.1.50:54321
              (i.e., when a packet comes back matching reply tuple,
               undo the DNAT)

Step 3 — kernel routing decision (post-DNAT):
  dst is now 10.244.2.11
  routing table on node-A says: 10.244.2.0/24 → via underlay → node-B
  (this is the CNI's pod-network routing — see ch 15)

Step 4 — netfilter POSTROUTING:
  KUBE-POSTROUTING:
    mark not set (no MASQ requested) → RETURN
  (so srcIP stays as 10.244.1.50 — internal pod IPs are routable
   between nodes thanks to the CNI)

Step 5 — SYN leaves node-A's eth0:
  src=10.244.1.50:54321  dst=10.244.2.11:8080
  (packet traverses underlay network)

Step 6 — SYN arrives at node-B's eth0:
  CNI routes 10.244.2.11 via cni0 → veth into pod-B

Step 7 — SYN enters pod-B's netns:
  src=10.244.1.50:54321  dst=10.244.2.11:8080
  pod-B's TCP stack accepts on port 8080
  responds with SYN/ACK: src=10.244.2.11:8080  dst=10.244.1.50:54321
```

### 11.3 The Return Path (SYN/ACK)

```
Step 1 — SYN/ACK leaves pod-B's veth → enters node-B's host netns:
  src=10.244.2.11:8080  dst=10.244.1.50:54321

Step 2 — routed via underlay back to node-A:
  routing: 10.244.1.0/24 → node-A

Step 3 — SYN/ACK arrives at node-A:
  enters host netns
  
  conntrack lookup: matches reply tuple of the entry created in Step 2 above
  → undo DNAT: rewrite src from 10.244.2.11:8080 back to 10.96.42.10:80

Step 4 — packet delivered into pod-A's netns:
  src=10.96.42.10:80  dst=10.244.1.50:54321
  
  pod-A's TCP stack sees a SYN/ACK from the VIP it originally connected to.
  Handshake completes. Application is happy.
```

### 11.4 Why conntrack Is Essential

Without conntrack, the return packet would arrive with `src=10.244.2.11:8080` and pod-A would have no idea what to do with it — its socket is waiting for a response from `10.96.42.10:80`. The conntrack table is what makes the bidirectional NAT *transparent* to the application.

Every Service-routed connection consumes one conntrack entry on the originating node. At ~300 bytes per entry, the default `nf_conntrack_max` of 262144 ≈ 80 MB of kernel memory. Tuning this on busy nodes is essential — see §23.

### 11.5 The View From `conntrack -L`

```
$ conntrack -L -p tcp --src 10.244.1.50
tcp 6 86399 ESTABLISHED \
  src=10.244.1.50 dst=10.96.42.10 sport=54321 dport=80 \
  src=10.244.2.11 dst=10.244.1.50 sport=8080 dport=54321 \
  [ASSURED] mark=0 use=1
```

Reading this:
- First line: original direction — what the packet *looked like* on the way out.
- Second line: reply direction — what the kernel expects on the way back.

The asymmetry — `dst=10.96.42.10` outgoing but `src=10.244.2.11` incoming — is the conntrack-mediated DNAT magic. The kernel will rewrite source IPs on every reply packet so the application sees responses from the VIP.

---

## 12. Session Affinity

`spec.sessionAffinity: ClientIP` requests that all connections from a given client IP go to the same backend pod, with a timeout (default 10800s = 3 hours):

```yaml
spec:
  sessionAffinity: ClientIP
  sessionAffinityConfig:
    clientIP:
      timeoutSeconds: 3600
```

Implementation by mode:

### 12.1 iptables Mode

Uses the kernel `recent` module to remember `(srcIP, dst)` → endpoint mapping in a hash table:

```
-A KUBE-SVC-XPTPB ... -m recent --rcheck --seconds 3600 --reap \
                          --name KUBE-SEP-AAAA --mask 255.255.255.255 \
                          --rsource -j KUBE-SEP-AAAA
-A KUBE-SVC-XPTPB ... -m recent --rcheck --seconds 3600 --reap \
                          --name KUBE-SEP-BBBB ... -j KUBE-SEP-BBBB
... (one rule per endpoint, then normal random-probability fallthrough)
```

When a new connection comes in, the chain checks each `KUBE-SEP-XXXX` recent list for the source IP; if found and not expired, jump to that endpoint. Otherwise fall through to random selection and add the source IP to the chosen endpoint's recent list.

### 12.2 IPVS Mode

Switches the scheduler to `sh` (source hashing): the source IP is hashed and modulo'd into the real-server list. Same client → same RS, as long as the RS list doesn't change. Cheap, deterministic, but rebalances when endpoints are added/removed (unless you use Maglev hashing `mh`, which is consistent).

### 12.3 The SNAT Problem

The single biggest gotcha with sessionAffinity: **it doesn't work after SNAT.**

In `externalTrafficPolicy: Cluster` mode (see §13), or when traffic comes through a cloud LB without proxy-protocol, all incoming external traffic to a given node arrives with the *node's* (or the LB's) IP as the source, not the original client. After SNAT, every external client looks like the same source IP to kube-proxy, and they *all* hash to the same backend.

This effectively defeats sessionAffinity for external traffic on Cluster-policy services. The fix is `externalTrafficPolicy: Local` (preserves source IP at the cost of failover) or moving session affinity to L7 (Ingress with cookie-based sticky sessions).

For purely internal pod-to-Service traffic, sessionAffinity works as expected because the source IP is the originating pod's IP, which is preserved through DNAT.

### 12.4 When To Use It

Sessionful legacy apps that hold per-IP state (e.g., shopping cart in memory, no Redis), where you cannot put the state in a shared store. Rare in modern apps. Most apps that *think* they need sessionAffinity actually need either L7 cookie affinity (Ingress / service mesh) or to externalize their session state.

---

## 13. externalTrafficPolicy

For NodePort and LoadBalancer Services, `externalTrafficPolicy` controls what happens when traffic arrives at a node from outside the cluster.

```yaml
spec:
  type: NodePort
  externalTrafficPolicy: Cluster   # or Local
```

### 13.1 Cluster (default)

```
┌──────────────────────────────────────────────────────────────────────┐
│  externalTrafficPolicy: Cluster                                       │
│                                                                       │
│  External client (203.0.113.5)                                       │
│       │                                                               │
│       ▼ packet to NodeIP:30080 (or LB → any node:30080)              │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  node-A (no local pod for this Service)                      │    │
│  │  PREROUTING: KUBE-NODEPORTS matches dport=30080              │    │
│  │  → KUBE-SVC-XPTPB → KUBE-SEP-BBBB                            │    │
│  │  DNAT dst → 10.244.2.11:8080 (pod on node-B)                 │    │
│  │  KUBE-MARK-MASQ set (cross-node hop needs SNAT)              │    │
│  │  POSTROUTING: KUBE-POSTROUTING MASQUERADE: src→node-A IP     │    │
│  └─────────────────────────────────────────────────────────────┘    │
│       │                                                               │
│       ▼ src=node-A_IP  dst=10.244.2.11                               │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  node-B                                                       │    │
│  │  Routed to pod-B → pod sees src=node-A_IP (NOT 203.0.113.5)  │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  PROPERTY: traffic always finds a pod (any node forwards).            │
│            But srcIP is lost (SNAT). Pod sees node-A_IP.              │
└──────────────────────────────────────────────────────────────────────┘
```

In Cluster mode, *any* node will accept the NodePort and forward to *any* backend pod, regardless of whether the pod is local. This makes the cluster's LB problem trivial — load balance to any node, it works — but at the cost of an extra hop *and* SNAT (because if you didn't SNAT, the return packet from the pod would go back to node-B's routing decision, miss the original conntrack entry, and break).

**Trade**: even distribution + guaranteed routability for **loss of source IP**.

### 13.2 Local

```
┌──────────────────────────────────────────────────────────────────────┐
│  externalTrafficPolicy: Local                                         │
│                                                                       │
│  External client (203.0.113.5)                                       │
│       │                                                               │
│       ▼ packet to node-A:30080                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  node-A (has local pod 10.244.1.10)                          │    │
│  │  KUBE-XLB-XPTPB (the external-only chain):                   │    │
│  │    only includes LOCAL endpoints, no MARK-MASQ               │    │
│  │  DNAT dst → 10.244.1.10:8080  (LOCAL pod)                    │    │
│  │  No SNAT                                                      │    │
│  └─────────────────────────────────────────────────────────────┘    │
│       │                                                               │
│       ▼ src=203.0.113.5  dst=10.244.1.10                             │
│  pod-A sees the real client IP. Replies via conntrack-undone DNAT.   │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  node-X (no local pod for this Service)                      │    │
│  │  KUBE-XLB-XPTPB has zero endpoints                           │    │
│  │  Packet is DROPPED (or refused) — fails closed.              │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  PROPERTY: srcIP preserved. But nodes without pods don't accept.     │
│            That doubles as a healthcheck for the cloud LB.           │
└──────────────────────────────────────────────────────────────────────┘
```

In Local mode, kube-proxy installs endpoints into a separate chain (`KUBE-XLB-XXXX` in older versions, `KUBE-EXT-XXXX` in newer) that only contains *node-local* endpoints. If there are no local endpoints, the NodePort is effectively unreachable on that node.

This last property is critical: **the cloud LB's health check should hit the NodePort**, and a node with no local pod will fail the health check, taking it out of the LB's rotation. This is the documented intended behavior — Local mode pushes routing decisions out to the cloud LB.

**Trade**: source IP preserved + no SNAT + no extra hop, at the cost of **uneven traffic distribution** (proportional to local pod count, not endpoint count) and **slower failover** (LB has to detect the node has no pods).

Use Local when:
- You need the real client IP (audit logs, rate limiting, geo).
- You're OK with the cloud LB doing the load distribution.
- You have enough pods that uneven distribution doesn't bottleneck you.

Use Cluster (default) when:
- You don't care about source IP.
- You want simple, guaranteed routability.
- You have few replicas and want them all to receive traffic.

### 13.3 Local With a Single Pod Per Node

If you have one pod per node (e.g., a DaemonSet exposed via LoadBalancer), Local mode is essentially "send to this node's local pod." The failover behavior is then dominated by the LB health-check interval; a pod failure means the node is unhealthy in the LB for up to a few health-check intervals (typically 10–30 seconds), causing dropped connections during that window.

In contrast, Cluster mode would fail over to a pod on a *different* node almost instantly (kube-proxy's sync is sub-second), but at the cost of SNAT.

---

## 14. internalTrafficPolicy

Added in 1.22 (beta) and GA in 1.26, `internalTrafficPolicy` is the analog of `externalTrafficPolicy` for *internal* (ClusterIP) traffic.

```yaml
spec:
  internalTrafficPolicy: Local   # or Cluster (default)
```

`Cluster` (the default) preserves the historical behavior: ClusterIP traffic from anywhere in the cluster goes to any endpoint.

`Local` makes ClusterIP traffic *originating on a node* only route to endpoints *on the same node*. If there is no local endpoint, the connection fails.

### 14.1 The DaemonSet-As-Service Pattern

The primary use case is DaemonSets that want to be addressed by a Service name but where the natural intent is "talk to the local instance." Examples:

- **Node-local log collector** (Fluentd, Vector). Pods on a node write logs to `logs.kube-system.svc.cluster.local` and you want each pod to talk to its node's collector, not some random collector on another node.
- **Node-local cache** (Envoy/sidecar caches).
- **Per-node Prometheus exporters** that need to be discoverable by a single Service name.
- **kube-proxy itself** is reachable as a Service-like construct, but it's not actually a Service.

Before `internalTrafficPolicy: Local`, this pattern required clients to look up the local node IP at startup and construct a URL — clunky, error-prone, and required runtime context the app might not have.

```yaml
# Node-local collector exposed only to same-node pods
apiVersion: v1
kind: Service
metadata: { name: log-agent, namespace: logging }
spec:
  selector: { app: log-agent }
  internalTrafficPolicy: Local
  ports: [ { port: 24224, targetPort: 24224 } ]
```

Now any pod can `dial log-agent.logging.svc.cluster.local:24224` and the kernel will route it to the agent pod *on its own node*. No node-IP lookup required.

### 14.2 The Failure Mode

If a node temporarily has no local endpoint (pod restarting, draining, evicted), connections from other pods on that node fail. Apps using Local internal policy must be tolerant of these brief outages, or you must guarantee a local pod via DaemonSet + PodDisruptionBudget.

---

## 15. Topology-Aware Routing

Cross-AZ traffic costs money in clouds (roughly $0.01–0.02/GB inter-zone). At terabytes/day, that's serious money. Topology-aware routing was introduced (as "topology-aware hints" in 1.23, renamed to "topology-aware routing" with the trafficDistribution field in 1.27, GA in 1.31) to let kube-proxy prefer endpoints in the same zone as the client.

### 15.1 The Mechanism

The EndpointSlice controller computes, for each endpoint, a list of zones where that endpoint should be preferred:

```yaml
# EndpointSlice with hints populated
endpoints:
  - addresses: [ "10.244.1.10" ]
    zone: us-east-1a
    hints:
      forZones: [ { name: us-east-1a } ]
  - addresses: [ "10.244.2.11" ]
    zone: us-east-1b
    hints:
      forZones: [ { name: us-east-1b } ]
  - addresses: [ "10.244.3.12" ]
    zone: us-east-1c
    hints:
      forZones: [ { name: us-east-1c } ]
```

kube-proxy reads the hints; for traffic originating on a node in zone X, it only considers endpoints whose `hints.forZones` includes X. Endpoint selection within that zone is normal (random/rr).

### 15.2 The Heuristic

The controller decides hints by computing the CPU-capacity-weighted demand per zone. The simplified algorithm:

1. For each zone, compute the fraction of total cluster CPU that lives in that zone.
2. For each zone, compute the fraction of total endpoints that live in that zone.
3. If demand (CPU fraction) and supply (endpoint fraction) match well, assign each endpoint to its own zone.
4. If they don't match — e.g., zone A has 50% of CPU but only 20% of endpoints — fall back to "any zone" hints to prevent zone A from overloading its few local endpoints.

This is why **topology-aware routing requires at least 3 endpoints per zone**: with too few endpoints per zone, the heuristic decides the topology is unsafe and disables hints, falling back to global routing.

### 15.3 The trafficDistribution Field (1.31+)

The newer API replaces the implicit hints-based mechanism with an explicit field:

```yaml
spec:
  trafficDistribution: PreferClose
```

`PreferClose` means "prefer endpoints close to the consumer" — currently same-zone, but the API leaves room for future "same-node", "same-region", etc. policies. The implementation is still hints-based, but the user contract is now declarative rather than a side effect of EndpointSlice content.

### 15.4 Gotchas

- **Hot-spotting on small clusters.** If zone A has 1 endpoint and zone B has 100, routing all zone-A traffic to that single endpoint will likely overload it. The 3-endpoints-per-zone rule mitigates this but isn't a guarantee. Monitor per-endpoint load.
- **Doesn't work with `externalTrafficPolicy: Local`** (because Local already routes to node-local pods, hint is overridden).
- **Asymmetric autoscaling.** If HPA scales pods across zones unevenly (e.g., based on zone-localized load), hints can amplify the imbalance.

---

## 16. Dual-Stack Services (IPv4 + IPv6)

A dual-stack cluster has both IPv4 and IPv6 pod and Service CIDRs (`--service-cluster-ip-range=10.96.0.0/16,fd00::/112`). Services can be configured with `spec.ipFamilyPolicy` and `spec.ipFamilies`:

```yaml
spec:
  ipFamilyPolicy: PreferDualStack    # SingleStack | PreferDualStack | RequireDualStack
  ipFamilies: [ IPv4, IPv6 ]         # order matters (first is primary)
```

- **SingleStack**: one VIP, one family (default = the cluster's primary family).
- **PreferDualStack**: try both, fall back to single if only one family is available.
- **RequireDualStack**: must allocate both; fail if not possible.

```
$ kubectl get svc web -o jsonpath='{.spec.clusterIPs}'
["10.96.42.10","fd00::42:a"]
```

EndpointSlices are partitioned by `addressType`: an IPv4 slice contains pod IPv4 addresses, an IPv6 slice contains pod IPv6 addresses. The same Pod can appear in both (it has both IPs).

kube-proxy programs rules for each family separately (iptables + ip6tables, or `inet` tables in nftables).

DNS returns both A and AAAA records for dual-stack Services, and the pod's resolver picks based on its own preferences (per glibc / musl resolver order).

---

## 17. Multi-Port Services

A Service can expose multiple ports:

```yaml
spec:
  selector: { app: web }
  ports:
    - { name: http,    port: 80,   targetPort: web }
    - { name: metrics, port: 9090, targetPort: metrics }
```

When `len(ports) > 1`, the `name` field is **mandatory** on each port (uniqueness check at admission).

Note `targetPort: web` is referencing a named port on the *pod*:

```yaml
# In the Pod / Deployment spec:
ports:
  - { name: web,     containerPort: 8080 }
  - { name: metrics, containerPort: 9091 }
```

Named ports let you change pod port numbers without touching the Service. Useful when a sidecar's metrics port is configurable.

EndpointSlices for multi-port services list each port:

```yaml
ports:
  - { name: http,    port: 8080, protocol: TCP }
  - { name: metrics, port: 9091, protocol: TCP }
```

kube-proxy programs separate rule sets per (Service, port). A client connecting to `web.default.svc.cluster.local:80` hits the http endpoint set; `:9090` hits the metrics endpoint set. Same backend pods, just different ports.

**Pitfall**: if pods don't all expose the named port (e.g., during a rolling update where some pods have the new port and some don't), the EndpointSlice for that port may be empty for some pods. Pod readiness should gate this.

---

## 18. The kubernetes.default Service

Every cluster has a special Service: `kubernetes` in the `default` namespace, of type ClusterIP, that points at the kube-apiserver. This is how in-cluster clients (controllers, kubelets, pods using a ServiceAccount token) reach the apiserver.

```
$ kubectl get svc kubernetes
NAME         TYPE        CLUSTER-IP   EXTERNAL-IP   PORT(S)   AGE
kubernetes   ClusterIP   10.96.0.1    <none>        443/TCP   30d

$ kubectl get endpoints kubernetes
NAME         ENDPOINTS                            AGE
kubernetes   192.168.1.10:6443,192.168.1.11:6443  30d
```

### 18.1 The Bootstrapping Problem

There's a chicken-and-egg issue here. The endpoints-controller normally watches Services and Pods to compute Endpoints. But the kube-apiserver itself doesn't run as a Pod (or, if it does, it's a static pod outside the normal endpoints flow). Where does the `kubernetes` Service's Endpoints object come from?

Answer: the apiserver itself maintains it. There is a special bootstrap path in `pkg/registry/core/rest/storage_core.go` (`EndpointReconciler`) where each kube-apiserver instance:

1. On startup, registers itself in the `kubernetes` Service's Endpoints / EndpointSlice as its own advertised IP:port.
2. Periodically (every 10s) re-heartbeats its presence and removes any stale apiserver entries.

So the `kubernetes` Service is a self-maintaining, special-cased Service whose endpoints are the apiservers themselves. If you have 3 apiservers, you see 3 endpoints. If one goes down, after the heartbeat TTL its entry is removed.

This is why even a brand-new cluster has working in-cluster connectivity to the apiserver — the controller-manager isn't even running yet at first apiserver startup, and the endpoints-controller couldn't possibly resolve "where is the apiserver?" Without the special case, the cluster couldn't bootstrap.

### 18.2 Why You Care

If you ever see `kubectl get endpoints kubernetes` showing the wrong addresses, the cause is usually one apiserver instance failing to write its heartbeat (network issue, certificate issue, etc.) — and the result is in-cluster clients getting load-balanced to a dead apiserver. Restart symptoms: kubelets can't talk to control plane until they reconnect to a working endpoint.

---

## 19. NodePort: Range and Reservation

The `--service-node-port-range` flag on kube-apiserver controls the NodePort range (default `30000-32767`). The reservation logic:

- When you create a NodePort/LoadBalancer Service without specifying `spec.ports[*].nodePort`, the apiserver allocates a random unused port from the range.
- You can explicitly set `nodePort: 30080`, which the apiserver will accept if the port is free in the range.
- Two Services cannot share a NodePort (the apiserver enforces uniqueness via an in-memory allocator backed by an etcd-stored bitmap).

### 19.1 Allocation Strategy

Source: `pkg/registry/core/service/portallocator/`. Two-tier:
1. **Reserved band** (top 16% of the range, by default 32269–32767): used for services that don't request a specific port. Random selection here reduces collisions with `nodePort: <fixed>` requests in the lower band.
2. **Dynamic band** (the rest): used for explicit nodePort requests.

You can change the range via `--service-node-port-range=20000-40000` if 30000–32767 is too small (some clusters with many LoadBalancer Services run out). Be careful changing this on a live cluster — existing Services with allocated ports outside the new range cause restart errors.

### 19.2 Picking Ports Yourself

For Services that need a stable, well-known NodePort (e.g., legacy clients pointing at a hardcoded port):

```yaml
spec:
  type: NodePort
  ports:
    - port: 80
      targetPort: 8080
      nodePort: 30080      # explicit
```

Pin only when you have to. Hardcoding NodePorts couples your manifests to the cluster's port-range configuration.

---

## 20. LoadBalancer: Cloud Integration

A LoadBalancer Service is a NodePort Service plus a contract with a cloud-controller-manager (CCM) to provision an external load balancer.

### 20.1 The Provisioning Flow

```
┌──────────────────────────────────────────────────────────────────────┐
│  User creates Service { type: LoadBalancer, ports: [...] }            │
│       │                                                               │
│       ▼                                                               │
│  kube-apiserver                                                       │
│   - allocates ClusterIP                                               │
│   - allocates NodePort                                                │
│       │                                                               │
│       ▼  Service watch event                                         │
│  cloud-controller-manager → service-controller                       │
│   - calls cloud SDK: CreateLoadBalancer(name, listeners, targets)    │
│   - listeners = Service ports                                         │
│   - targets = all node IPs at the NodePort                            │
│       │                                                               │
│       ▼  cloud returns LB address                                    │
│  service-controller updates Service.status.loadBalancer.ingress[]    │
│   - { ip: "1.2.3.4" } or { hostname: "a1b2c3.elb.amazonaws.com" }    │
│       │                                                               │
│       ▼                                                               │
│  External client uses status.loadBalancer.ingress → reaches LB →     │
│  → NodePort on a node → DNAT → pod                                   │
└──────────────────────────────────────────────────────────────────────┘
```

The service-controller (in CCM) is the bridge between Kubernetes Service objects and the cloud's LB API. It reconciles:

- Service creation → CreateLoadBalancer + populate status.
- Service spec change → UpdateLoadBalancer (e.g., port changes).
- Node membership change → UpdateLoadBalancer's target list (add new nodes, remove decommissioned ones).
- Service deletion → DeleteLoadBalancer.

### 20.2 LoadBalancerClass

Multiple LB controllers can coexist (`spec.loadBalancerClass`). For example, AWS has both the in-tree `service.beta.kubernetes.io/aws-load-balancer-*` controller and the newer AWS Load Balancer Controller; you can pick which one handles each Service by setting the class. MetalLB uses a class to claim bare-metal LB Services.

### 20.3 No Cloud Controller → Stuck Pending

If you create a `type: LoadBalancer` Service on a cluster with no LB controller (e.g., kind, minikube, bare-metal without MetalLB), the Service sits forever in `Pending`:

```
$ kubectl get svc web
NAME   TYPE           CLUSTER-IP    EXTERNAL-IP   PORT(S)        AGE
web    LoadBalancer   10.96.42.10   <pending>     80:30080/TCP   5m
```

No one is reconciling it. The ClusterIP and NodePort still work — only the external address is missing.

Solutions:
- **MetalLB**: bare-metal LB controller that announces VIPs via ARP/BGP.
- **kube-vip**: lighter-weight option for small clusters.
- **Patch the status manually** for testing: not recommended in prod but works in dev.

See [ch 37 (cloud controllers)](37-cloud-controllers.md) for the deep dive on CCMs.

---

## 21. The End-to-End External Traffic Picture

Putting NodePort, LoadBalancer, externalTrafficPolicy, and Cluster vs Local together:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         External Traffic, All Paths                          │
│                                                                              │
│   External client (203.0.113.5)                                             │
│        │                                                                     │
│        │ DNS resolves web.example.com to LB IP                              │
│        ▼                                                                     │
│   ┌─────────────────────────────────┐                                       │
│   │  Cloud Load Balancer            │                                       │
│   │  (ELB/NLB/Azure-LB/MetalLB)     │                                       │
│   │  health-checks NodePort         │                                       │
│   │  forwards to nodes:NodePort     │                                       │
│   └────────────┬────────────────────┘                                       │
│                │                                                             │
│       ┌────────┼────────┬────────┐                                          │
│       ▼        ▼        ▼        ▼                                          │
│   ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐                                  │
│   │node-A │ │node-B │ │node-C │ │node-D │                                  │
│   │ pod-1 │ │ pod-2 │ │       │ │ pod-3 │                                  │
│   └───────┘ └───────┘ └───────┘ └───────┘                                  │
│                                                                              │
│  Cluster mode:                                                              │
│    Any node accepts NodePort. node-C (no local pod) forwards to             │
│    pod-1/2/3 on another node. SNAT applied (src IP lost). Pod sees node-C. │
│                                                                              │
│  Local mode:                                                                │
│    Only node-A, node-B, node-D accept (have local pods). node-C fails LB    │
│    health check, removed from LB rotation. No SNAT. Pod sees client IP.    │
└─────────────────────────────────────────────────────────────────────────────┘
```

The choice is a tradeoff matrix:

| Mode    | Client IP preserved? | Even distribution?  | Hop count             | Use when                          |
| ------- | -------------------- | ------------------- | --------------------- | --------------------------------- |
| Cluster | No (SNATed)          | Yes (across all)    | LB → node → ?node → pod | Don't care about source IP        |
| Local   | Yes                  | No (per-node pods)  | LB → node → pod        | Need source IP / minimal latency  |

---

## 22. Hairpin: Pod → Service → Self

A subtle case: a pod connects to a Service, and the chosen backend turns out to be *the same pod*. This is the "hairpin" scenario.

```
┌─────────────────────────────────────────────────────────────────────┐
│  pod-A (10.244.1.10) connects to web (VIP 10.96.42.10:80)            │
│  pod-A IS one of web's backends (selected by DNAT)                  │
│                                                                      │
│  Step 1: src=10.244.1.10:54321 dst=10.96.42.10:80                   │
│  Step 2: PREROUTING → KUBE-SVC → KUBE-SEP (pod-A's own)              │
│          DNAT: dst → 10.244.1.10:8080                                │
│          KUBE-MARK-MASQ: mark for SNAT (src == dst case)             │
│  Step 3: POSTROUTING → MASQUERADE: src → cni0 IP (10.244.1.1)        │
│  Step 4: packet has src=10.244.1.1, dst=10.244.1.10                  │
│          routed back to pod-A's veth                                 │
│  Step 5: pod-A receives src=10.244.1.1 dst=10.244.1.10               │
│          — different src, so no infinite loop                        │
└─────────────────────────────────────────────────────────────────────┘
```

Two pieces have to be set up for hairpin to work:

### 22.1 The SNAT Mark

The kube-proxy iptables rule that catches `src == dst` is:

```
-A KUBE-SEP-AAAAAAAAAAAAAAAA -s 10.244.1.10/32 -j KUBE-MARK-MASQ
-A KUBE-SEP-AAAAAAAAAAAAAAAA -p tcp -m tcp -j DNAT --to-destination 10.244.1.10:8080
```

`KUBE-MARK-MASQ` sets a packet mark; then `KUBE-POSTROUTING` masquerades any marked packet. Without this, the packet would arrive at pod-A with `src=10.244.1.10, dst=10.244.1.10` — and the kernel would (depending on settings) loop or drop.

### 22.2 The Bridge Hairpin Mode

The Linux bridge by default refuses to forward a packet back out the port it came in on. So a packet from pod-A arriving at cni0 destined for pod-A (same port) would be dropped.

The CNI must enable hairpin mode on the veth:

```
$ bridge link show
3: vethaaaa@if4: ... master cni0 ... hairpin off
```

A CNI that supports Services (essentially all of them) flips this on:

```
ip link set vethaaaa hairpin on
```

If you see "pod can reach Service X but only when the chosen backend is on another node, never when it's itself", check hairpin mode on the veth. Most CNIs handle this automatically; some old CNI configurations or manual setups miss it.

### 22.3 `promiscuous-bridge` Alternative

Older Kubernetes versions had a kubelet flag `--hairpin-mode=promiscuous-bridge` that put cni0 in promiscuous mode globally, which has the same effect more bluntly. Modern setups prefer per-veth hairpin.

---

## 23. conntrack and Service Traffic

Every connection that traverses iptables/IPVS DNAT consumes one conntrack entry on the node *originating* the connection. conntrack is the kernel's connection-tracking subsystem, and it has finite memory.

```
$ cat /proc/sys/net/netfilter/nf_conntrack_max
262144
$ cat /proc/sys/net/netfilter/nf_conntrack_count
148732
```

### 23.1 Sizing the Table

Each entry is ~300 bytes. The default 262144 ≈ 80 MB. On a node serving 100k concurrent connections, that's near the limit; bursts can push you over.

When the table is full, `nf_conntrack: table full, dropping packet` appears in dmesg, and *new* connection-establishment packets are silently dropped (existing connections continue to work). This presents to the application as random connect() failures or timeouts, often without clear correlation to traffic patterns.

Tuning:

```
# /etc/sysctl.d/99-conntrack.conf
net.netfilter.nf_conntrack_max = 1048576
net.nf_conntrack_max = 1048576
net.netfilter.nf_conntrack_tcp_timeout_established = 86400
net.netfilter.nf_conntrack_tcp_timeout_close_wait = 60
net.netfilter.nf_conntrack_tcp_timeout_time_wait = 60
```

Increase `nf_conntrack_max` proportionally to expected concurrent flows. Shorten timeouts for closed/wait states so dead entries clear faster.

### 23.2 Why eBPF Wins Here

As discussed in §10, Cilium's socket-level LB does the redirection at `connect(2)` time, never creates a packet with `dst=VIP`, and therefore never creates a conntrack entry for Service traffic. On a node doing 100k+ Service connections, this is the difference between 80 MB+ of conntrack memory and essentially zero.

For iptables/IPVS/nftables modes, this is just a tax you pay.

### 23.3 Inspecting

```
$ conntrack -L | head
tcp 6 86399 ESTABLISHED src=10.244.1.50 dst=10.96.42.10 sport=43210 dport=80 \
    src=10.244.2.11 dst=10.244.1.50 sport=8080 dport=43210 [ASSURED] mark=0 use=1
tcp 6 119 TIME_WAIT src=10.244.1.50 dst=10.96.42.10 sport=43180 dport=80 \
    src=10.244.1.10 dst=10.244.1.50 sport=8080 dport=43180 [ASSURED] mark=0 use=1
...

$ conntrack -S
cpu=0   found=12345 invalid=0 ignore=87654 insert=98765 ...
cpu=1   ...
```

Watch the `drop` and `early_drop` counters from `conntrack -S`; non-zero means you're hitting the limit.

---

## 24. Debugging Services in Practice

A toolkit for "my Service doesn't work":

### 24.1 Is the Service Object Sane?

```
$ kubectl describe svc web -n default
Name:              web
Selector:          app=web
Type:              ClusterIP
IP:                10.96.42.10
Port:              http  80/TCP
TargetPort:        8080/TCP
Endpoints:         10.244.1.10:8080,10.244.2.11:8080,10.244.3.12:8080
Session Affinity:  None
```

`Endpoints:` empty means the selector matches no Ready pods. Common causes:
- Selector typo (`app: web` vs `app: webapp`).
- Pods exist but aren't Ready (readiness probe failing).
- Pods exist in a different namespace (selectors are namespace-scoped).
- `targetPort` doesn't match any container port.

### 24.2 Check EndpointSlices

```
$ kubectl get endpointslices -n default -l kubernetes.io/service-name=web
NAME         ADDRESSTYPE   PORTS   ENDPOINTS                                AGE
web-x7k2p    IPv4          8080    10.244.1.10,10.244.2.11,10.244.3.12     10m
```

```
$ kubectl get endpointslices web-x7k2p -o yaml | grep -A 3 conditions
    conditions:
      ready: true
      serving: true
      terminating: false
```

Verify the conditions — `ready: false` means the pod isn't being routed to.

### 24.3 Verify Kernel Rules

iptables mode:

```
$ iptables-save -t nat | grep -E 'KUBE-SVC|10.96.42.10'
:KUBE-SVC-XPTPB7777ATXXXXX - [0:0]
-A KUBE-SERVICES -d 10.96.42.10/32 -p tcp -m tcp --dport 80 -j KUBE-SVC-XPTPB7777ATXXXXX
-A KUBE-SVC-XPTPB7777ATXXXXX -m statistic --mode random --probability 0.33333333349 -j KUBE-SEP-AAAA
...
```

If no `KUBE-SVC-` chain exists for your VIP, kube-proxy hasn't programmed it — check kube-proxy logs.

IPVS mode:

```
$ ipvsadm -ln | grep -A 3 10.96.42.10
TCP  10.96.42.10:80 rr
  -> 10.244.1.10:8080             Masq    1      0          0
  -> 10.244.2.11:8080             Masq    1      0          0
  -> 10.244.3.12:8080             Masq    1      0          0
```

nftables mode:

```
$ nft list table ip kube-proxy | grep -A 3 service-XPTPB
```

### 24.4 Run a Debug Pod

```
$ kubectl run debug --image=nicolaka/netshoot --rm -it -- bash
# curl 10.96.42.10:80           # test VIP
# curl 10.244.1.10:8080         # test direct pod
# dig web.default.svc.cluster.local
# tcpdump -i any -n host 10.96.42.10
```

### 24.5 Check conntrack

```
$ conntrack -L --dst 10.96.42.10
tcp 6 86399 ESTABLISHED src=... dst=10.96.42.10 ...
```

No entries while you should have active connections → traffic isn't going through netfilter, possibly because Cilium kube-proxy-free is active (then check `cilium service list`).

### 24.6 Check kube-proxy Health

```
$ kubectl -n kube-system get pods -l k8s-app=kube-proxy
$ kubectl -n kube-system logs kube-proxy-xxxx | tail -50
$ curl http://<node-IP>:10249/metrics | grep sync_proxy
```

`sync_proxy_rules_duration_seconds` p99 climbing means rule programming is slow — usually big endpoint churn or too many services for iptables mode.

### 24.7 Check the Listener

```
$ ss -nlpt
LISTEN 0 4096 *:8080 ...
```

On a backend pod (or via `kubectl exec`), confirm the targetPort is actually open. Container *thinks* it's listening but is bound to 127.0.0.1? That's a Service-doesn't-work in disguise.

---

## 25. The Replacement Path: iptables → nftables → eBPF

Where each mode wins, in 2025:

```
┌──────────────────────────────────────────────────────────────────────┐
│                       Mode Selection Matrix                           │
│                                                                       │
│  Cluster size       iptables   IPVS     nftables   eBPF (Cilium)     │
│  ─────────────────  ────────   ──────   ────────   ────────────       │
│  < 1k services      OK         OK       OK         OK                │
│  1k–5k services     OK*        OK       OK         OK                │
│  5k–10k services    Tight      OK       OK         OK                │
│  > 10k services     NO         OK       OK         OK                │
│                                                                       │
│  Need source-IP?   Local pol  Local pol  Local pol  native           │
│  Need topology?    Yes (1.27)  Limited   Yes        Yes              │
│  Need debug ease?   Best       Hard      Good       Hardest          │
│  Conntrack cost?    Full       Full      Full       None (cgroup)    │
│  Setup ease?        Default    Stable    Modern     Replaces proxy   │
│                                                                       │
│  * iptables incremental sync (1.26+) mitigates but doesn't fix       │
└──────────────────────────────────────────────────────────────────────┘
```

Practical recommendations:

- **Greenfield, 2025+, large cluster**: skip kube-proxy entirely. Cilium with `kubeProxyReplacement: strict`. Best performance, lowest kernel memory, modern observability.
- **Existing cluster on iptables, growing**: switch to nftables mode (1.31+ beta, 1.33 GA). Lowest-risk, biggest immediate scaling win. Same operator mental model as iptables.
- **Existing cluster, stable, < 5k services**: leave it on iptables until you actually hit a scaling problem. The cliff is real but you might never reach it.
- **Multi-tenant with strict network policy + observability needs**: Cilium (regardless of size), because its policy engine and Hubble observability are part of the same data plane.
- **Bare-metal, no eBPF expertise**: nftables mode + MetalLB for LB Services. Solid combination.

The iptables-mode kube-proxy will eventually become a legacy mode preserved for old kernels and small clusters. nftables is the spiritual successor; eBPF is the leap forward.

---

## 26. Observability and Alerts

What to scrape and what to alert on:

### 26.1 kube-proxy Metrics

Exposed at `:10249/metrics`:

- `kubeproxy_sync_proxy_rules_duration_seconds` (histogram) — how long each reconcile takes. **Alert: p99 > 1s sustained for 5m.**
- `kubeproxy_sync_proxy_rules_last_timestamp_seconds` — last successful sync. **Alert: now() - last > 60s.**
- `kubeproxy_network_programming_duration_seconds` — end-to-end (endpointslice change → kernel rule update).
- `kubeproxy_proxy_healthz_total` — health endpoint requests served.

### 26.2 Service / Endpoint Metrics (from kube-state-metrics)

- `kube_service_info{namespace, service, cluster_ip, type}` — inventory of Services.
- `kube_endpoint_info` — legacy Endpoints presence.
- `kube_endpoint_address_available` — count of available endpoints per Service.
- **Alert**: `kube_endpoint_address_available == 0` for a Service that should have endpoints (likely a selector typo or all pods unready).

### 26.3 conntrack Metrics

Via `node_exporter` (`textfile_collector` or built-in `netfilter`):

- `node_nf_conntrack_entries` / `node_nf_conntrack_entries_limit` — usage / max.
- **Alert**: ratio > 0.8 for 5m. Means you're approaching table-full territory.
- `node_netstat_Tcp_PassiveOpens`, `node_netstat_Tcp_ActiveOpens` — connection rates.

### 26.4 Cluster-Wide

- Service count, EndpointSlice count, total endpoint count. Trend these — sudden growth often presages a kube-proxy scaling event.
- Per-node iptables rule count (`iptables-save | wc -l`).

---

## 27. Pitfalls

A field guide to the bugs that bite staff engineers.

### 27.1 Assuming Source IP Is Preserved on NodePort/LB

The single most common error. By default (`externalTrafficPolicy: Cluster`), SNAT replaces the client IP with the receiving node's IP. Audit logs, rate limits, geo lookups — all see the wrong source. Set `externalTrafficPolicy: Local` if you need it, and understand the failover tradeoff.

### 27.2 externalTrafficPolicy: Local With Single-Pod-Per-Node Deployments

Local mode failover is bounded by the cloud LB's health-check interval (typically 10–30s). Single-pod-per-node + a pod restart → that node is down for the LB until next health-check pass. For high-availability you need at least 2 replicas per node, or use Cluster mode and accept SNAT.

### 27.3 SessionAffinity Doesn't Help After SNAT

For external traffic on Cluster-policy services, every connection's source IP is some node's IP. They all hash to the same backend. Affinity is silently useless. Use Local mode, or move affinity to L7 (Ingress cookies).

### 27.4 iptables Mode at 10k+ Services

If your `sync_proxy_rules_duration_seconds` p99 is climbing past 1s and you're at >5k services, you're on the cliff. Switch to nftables mode (lowest-risk migration) or replace kube-proxy with eBPF.

### 27.5 conntrack Table Overrun

Silent connection drops on busy nodes. The error is in dmesg (`nf_conntrack: table full`), not in your apps' logs. Tune `nf_conntrack_max` proactively, monitor utilization.

### 27.6 NodePort Outside the 30000–32767 Range

You can't allocate `nodePort: 8080` by default. Either change `--service-node-port-range` (cluster-wide) or use an Ingress on port 80/443.

### 27.7 Service Selector Matches No Pods

The Service has no endpoints. No error, no warning. Just silent traffic blackhole.

```
$ kubectl get endpoints web
NAME   ENDPOINTS   AGE
web    <none>      5m
```

Always sanity-check `kubectl get endpoints` after creating a Service. Or use admission policies that validate Services have non-empty selectors and a matching Pod exists.

### 27.8 One Service Per Pod for a StatefulSet

The newbie pattern: create a separate ClusterIP Service per pod of a StatefulSet (`web-0`, `web-1`, `web-2`), so you can address them individually. This wastes ClusterIPs, creates N times the iptables rules, and is the *wrong* idiom.

The right idiom: a **headless Service** (`clusterIP: None`) for the StatefulSet. CoreDNS serves per-pod records (`web-0.web.default.svc.cluster.local`) automatically. Zero VIPs, zero kube-proxy rules.

### 27.9 LoadBalancer in a Cluster Without a Cloud Controller

`status.loadBalancer.ingress` stays empty forever. Service is "Pending" until the heat death of the universe. Install MetalLB, kube-vip, or use a different Service type.

### 27.10 readinessProbe That Doesn't Match Service Port

If `targetPort: 8080` but the readinessProbe is `/healthz` on port `9090`, you can have all pods Ready (probe passes) but the actual app port not listening. Service endpoints are populated, traffic flows, all connections fail. Pin your probe to the same port as your target.

### 27.11 Two Services With the Same ClusterIP

Can't happen by default (allocator enforces uniqueness), but if you `kubectl edit` to force `spec.clusterIP` to an in-use IP, the apiserver will reject. Watch out for `spec.clusterIP: 1.2.3.4` in manifests that previously used a different IP — you may break Service identity across re-apply.

### 27.12 Headless Service With No Selector

You can create a headless Service that has no selector and manually manage Endpoints / EndpointSlices (this is how some operators expose external resources via a Service name). Easy to forget: there's no garbage collection. If you delete the Service, you keep the EndpointSlice (since it's owned by you, not by the controller). Cleanup yourself.

### 27.13 Cross-Namespace Services

Selectors are namespace-scoped. You cannot create a Service in `ns-A` that selects pods in `ns-B`. To bridge namespaces, use an `ExternalName` Service pointing at the FQDN of the other Service.

### 27.14 ExternalName With a CNAME Chain

CoreDNS handles `ExternalName` by emitting a CNAME response. If the target itself has a CNAME chain (CDNs do this), the client follows the chain. Each hop is a DNS lookup; latency adds up. Watch out for chains in front of latency-sensitive paths.

### 27.15 The "Ports All Forward to One Pod" Mystery

Multi-port Service, two pods, but only one ever receives traffic? Often a port-name mismatch: the second pod doesn't have a container port named the same as `targetPort: web`. EndpointSlice silently excludes the pod from that port. `kubectl get endpointslice -o yaml` and check the addresses per port.

### 27.16 NetworkPolicy Blocking Service Traffic

Service VIPs are not in any pod-network CIDR; NetworkPolicy egress rules that match on IP block don't see VIPs as anything special. But the *real* destination after DNAT is a pod IP — so an egress rule that allows `to.podSelector` matching the backend should work. If it doesn't, check whether NetworkPolicy is enforced on the source or destination (CNI-specific) and whether the DNAT happens before or after the NetworkPolicy hook. See [ch 20 (NetworkPolicy)](20-network-policy.md).

### 27.17 Multiple kube-proxy Modes Running Simultaneously

A botched migration leaves both iptables-mode kube-proxy *and* IPVS-mode kube-proxy installing rules. The conflict produces inconsistent routing — some packets DNAT correctly, some don't, depending on which rule fires first. Always confirm there is exactly one `--proxy-mode` configured cluster-wide before flipping the switch.

### 27.18 Stale Rules After kube-proxy Crash + Container Move

If kube-proxy crashes mid-sync (e.g., OOMKilled) and the next reconcile finds different state, it can leave stale chains. Usually self-heals on next full sync. If it doesn't, `iptables-save` and look for orphan `KUBE-SEP-*` chains; restart kube-proxy with `--cleanup` for a one-shot wipe.

### 27.19 Topology-Aware Routing With <3 Endpoints Per Zone

The heuristic disables hints and falls back to global routing. Your `trafficDistribution: PreferClose` is silently no-op. Confirm with `kubectl get endpointslice -o yaml | grep -A 1 hints` — if `hints` is empty, the heuristic disabled it.

### 27.20 Service of an apiserver IP

Don't create a Service named `kubernetes` in `default`; it conflicts with the bootstrapped one. Don't create a Service whose ClusterIP matches an apiserver advertise address.

---

## 28. TL;DR

- A **Service** is a stable virtual IP and DNS name that load-balances to a label-selected set of pod endpoints. Clients talk to the Service; kube-proxy programs the kernel so the VIP routes to a real pod.
- Five flavors: **ClusterIP** (internal VIP), **NodePort** (port on every node + ClusterIP), **LoadBalancer** (cloud LB + NodePort), **ExternalName** (DNS CNAME, no proxy), **headless** (`clusterIP: None`, DNS-only, one A record per pod).
- The endpoints-controller and endpointslice-controller watch Services + Pods, write **EndpointSlice** objects (sharded, default 100 endpoints per slice). Legacy single-Endpoints object hit a 1 MB / ~5k endpoint wall; EndpointSlice scales by sharding and incremental updates.
- Endpoint conditions: **ready** (fully Ready), **serving** (accepting new traffic), **terminating** (DeletionTimestamp set). Default routing is ready=true. KEP-1669 added serving-but-terminating fallback for graceful drains.
- **kube-proxy** is a control-plane agent: watches Services + EndpointSlices, programs kernel rules, **not in the data path**. If it crashes, existing traffic keeps flowing.
- **iptables mode** (legacy default): KUBE-SERVICES → KUBE-SVC-* → KUBE-SEP-* chains; per-Service random-probability rules pick a backend; per-endpoint DNAT. O(N) per-packet match cost, O(N) rule update cost. Breaks down at ~5–10k services.
- **IPVS mode**: kernel hash table for VIPs, O(1) lookup. Schedulers: rr (default), sh (for sessionAffinity), mh (Maglev consistent hashing), lc, sed, nq. Uses ipsets + a few iptables rules for masquerading.
- **nftables mode** (beta 1.31, GA 1.33): verdict-map dispatch, O(1) lookup, incremental updates, dual-stack in one table. The future default.
- **eBPF / kube-proxy replacement** (Cilium, Calico): socket-level LB at `connect(2)` via cgroup hooks. No netfilter rules, no conntrack for Service traffic. Hardest to debug, fastest performance.
- **Packet path** (iptables mode): pod → cni0 → PREROUTING:KUBE-SERVICES → KUBE-SVC → KUBE-SEP DNAT → routing to backend pod (possibly cross-node) → conntrack entry → reply traverses conntrack and undoes DNAT, application sees responses from VIP.
- **sessionAffinity: ClientIP** binds a client IP to a backend, with timeoutSeconds. Defeated by SNAT — useless for external traffic on Cluster-policy services.
- **externalTrafficPolicy: Cluster** (default) accepts on every node, DNATs to any backend, SNATs (loses srcIP). **Local** only accepts on nodes with local pods, no SNAT (preserves srcIP), uses LB health checks for failover.
- **internalTrafficPolicy: Local** (1.22+) routes ClusterIP traffic only to same-node endpoints. Use for DaemonSet-as-Service (node-local log agents, sidecars).
- **Topology-aware routing** (trafficDistribution: PreferClose, 1.31 GA) prefers same-zone endpoints to reduce cross-AZ cost. Requires ≥3 endpoints/zone or falls back to global routing.
- **Dual-stack**: `spec.ipFamilyPolicy` + `spec.ipFamilies`; per-family VIPs; EndpointSlices partitioned by `addressType`. DNS returns both A and AAAA.
- **Multi-port**: required `name` per port; pods can use named targetPorts.
- **The `kubernetes` Service**: self-maintained by each apiserver via the EndpointReconciler. Solves the bootstrap chicken-and-egg.
- **NodePort range** default 30000–32767; explicit nodePort allowed; allocator splits into reserved and dynamic bands.
- **LoadBalancer**: service-controller in CCM provisions via cloud SDK, populates `status.loadBalancer.ingress`. Stuck-Pending without a controller (install MetalLB on bare metal).
- **Hairpin** (pod → Service → self): needs KUBE-MARK-MASQ in the SEP chain + hairpin mode on the veth (CNIs do this automatically).
- **conntrack** is the silent killer: every DNAT'd Service flow consumes an entry; default 262k entries (~80 MB) fills under load and drops new connections without app-visible errors. Tune `nf_conntrack_max`; or use eBPF and skip the table.
- **Debug toolkit**: `kubectl describe svc`, `kubectl get endpointslices`, `iptables-save | grep <vip>`, `ipvsadm -ln`, `nft list table ip kube-proxy`, `conntrack -L`, `ss -nlpt`, run a netshoot debug pod.
- **kube-proxy lifecycle**: DaemonSet, `syncProxyRules()` throttled by `--min-sync-period` (1s), full reconcile every `--sync-period` (30s) as a safety net.
- **Mode selection 2025**: greenfield → eBPF (Cilium). Existing iptables hitting scale → migrate to nftables. Small stable clusters → leave on iptables. eBPF for observability/policy. Avoid IPVS for new builds; it's stable but increasingly legacy.
- **Observability**: alert on `sync_proxy_rules_duration_seconds` p99 > 1s, last-sync staleness > 60s, conntrack utilization > 80%, `kube_endpoint_address_available == 0` for Services that should have endpoints.
- **Top pitfalls**: client IP loss under Cluster policy, sessionAffinity post-SNAT being useless, iptables at 10k services, conntrack overrun, NodePort range surprises, selector typo → no endpoints, headless-vs-per-pod-Service confusion for StatefulSets, LoadBalancer with no cloud controller.
- **The decoupling that makes everything work**: clients name Services, never Pods. Pods churn freely behind the VIP. The kernel handles every endpoint change in milliseconds. Get that mental model right and Service debugging becomes mechanical.
