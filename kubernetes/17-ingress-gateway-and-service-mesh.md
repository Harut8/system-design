# Ingress, Gateway API, and Service Mesh

Kubernetes Services (chapter 14) are L4. They give every Pod a stable virtual IP and load-balance TCP/UDP across endpoints. That is enough to keep `psql` reaching Postgres, enough to keep a queue worker reaching Redis, enough for any application that only cares about *destination address + port*. It is **not** enough for the modern HTTP/HTTPS internet. Real applications need host-based routing (`api.example.com` and `app.example.com` go to different backends), path-based routing (`/v1/users` to one service, `/v1/orders` to another), header-based routing (`X-Canary: true` to v2), TLS termination, request mirroring, retries with budgets, timeouts, per-route rate limits, request authentication, OAuth flows, mTLS between services, traffic splitting for canary releases, fault injection for chaos testing, locality-aware load balancing, and twenty other things that all live at **L7**. None of that is a Service.

This chapter is the L7 control plane story. It begins with **Ingress** — the original, annotation-heavy API that every cloud provider extended differently and that the community finally admitted was a portability disaster. It moves to the **Gateway API** — a multi-resource, role-oriented redesign that became GA in Kubernetes 1.32 and is now the official future of north-south L7. Then it dives into **service meshes** — Istio (sidecar and ambient), Linkerd, Cilium's mesh mode — and the universal data plane underneath most of them: **Envoy**, configured by the **xDS** protocol. Along the way we cover mTLS, SPIFFE identity, canary traffic-splitting, locality LB, multi-cluster mesh, rate limiting, certificate management, observability, WebAssembly extensions, and the GAMMA initiative that is unifying north-south and east-west L7 under one API.

The chapter sits between [ch 14 (Services and kube-proxy)](14-services-and-kube-proxy.md), which gives you L4 reachability, and [ch 20 (NetworkPolicy and segmentation)](20-network-policy-and-segmentation.md), which gives you L3/L4/L7 deny semantics. Sibling chapter [ch 16 (Cilium / sidecarless mesh)](16-cilium-and-ebpf-deep-dive.md) covers the eBPF data path that increasingly replaces sidecar Envoy at the transport layer. Above sits [ch 18 (CoreDNS)](18-dns-and-coredns.md), which is how clients find the names you route. Below sits [ch 15 (CNI)](15-cni-and-pod-networking.md), because every byte the mesh moves still rides on a CNI-provided veth pair.

If you only remember one sentence: **Ingress is dead, Gateway API is the inheritance, Envoy is the engine, xDS is the wire format, and a service mesh is whatever subset of the above you point at east-west traffic instead of north-south.**

---

## Table of Contents

1. [The Problem L7 Routing Solves](#1-the-problem-l7-routing-solves)
2. [Ingress: The Legacy API](#2-ingress-the-legacy-api)
3. [Ingress Controllers: A Comparison](#3-ingress-controllers-a-comparison)
4. [The Gateway API: A Role-Oriented Redesign](#4-the-gateway-api-a-role-oriented-redesign)
5. [HTTPRoute Semantics: Matching, Filters, BackendRefs](#5-httproute-semantics-matching-filters-backendrefs)
6. [Gateway API Filters and Extensions](#6-gateway-api-filters-and-extensions)
7. [Migrating from Ingress to Gateway API](#7-migrating-from-ingress-to-gateway-api)
8. [Envoy: The Universal Data Plane](#8-envoy-the-universal-data-plane)
9. [xDS: How the Control Plane Streams Config](#9-xds-how-the-control-plane-streams-config)
10. [Istio Architecture: Sidecar Mode](#10-istio-architecture-sidecar-mode)
11. [Istio Ambient Mesh: ztunnel + Waypoints](#11-istio-ambient-mesh-ztunnel--waypoints)
12. [mTLS in Istio: SPIFFE, Citadel, PeerAuthentication](#12-mtls-in-istio-spiffe-citadel-peerauthentication)
13. [Traffic Management in Istio](#13-traffic-management-in-istio)
14. [Linkerd: The Rust Alternative](#14-linkerd-the-rust-alternative)
15. [Istio vs Linkerd vs Cilium Mesh](#15-istio-vs-linkerd-vs-cilium-mesh)
16. [Ingress vs Gateway vs Mesh: A Decision Tree](#16-ingress-vs-gateway-vs-mesh-a-decision-tree)
17. [TLS Termination Patterns](#17-tls-termination-patterns)
18. [Certificate Management: cert-manager and Citadel](#18-certificate-management-cert-manager-and-citadel)
19. [Rate Limiting: Local and Global](#19-rate-limiting-local-and-global)
20. [Locality-Aware Load Balancing](#20-locality-aware-load-balancing)
21. [Multi-Cluster Mesh](#21-multi-cluster-mesh)
22. [Observability in the Mesh](#22-observability-in-the-mesh)
23. [WebAssembly Extensions](#23-webassembly-extensions)
24. [GAMMA: Gateway API for East-West](#24-gamma-gateway-api-for-east-west)
25. [The Cost of a Mesh](#25-the-cost-of-a-mesh)
26. [Pitfalls](#26-pitfalls)
27. [TL;DR](#27-tldr)

---

## 1. The Problem L7 Routing Solves

A Kubernetes Service is the minimum viable load balancer: it takes a list of `Endpoints` (chapter 14), turns it into iptables / IPVS / nftables / eBPF rules on every node, and DNATs an incoming packet to one backend Pod. The decision is made on **destination IP and destination port**. The kernel never looks at the payload. From the kernel's perspective the byte stream over that TCP connection is opaque, and that opacity is by design — kube-proxy is a level-3 / level-4 NAT engine, not an HTTP parser.

This produces three categories of problem that no Service can solve.

### 1.1 One VIP, one backend pool

A Service has exactly one `selector`, which produces exactly one EndpointSlice set, which feeds exactly one VIP. If you want two URL paths — `/api/v1/users` and `/api/v1/orders` — to land on two different backend pools, you need two Services. Fine. But now the client needs to know two different DNS names, or two different ports, or two different IPs. The application's logical contract (`api.example.com`) cannot be honored by a flat L4 mapping.

The fix is L7: a router that terminates the TCP connection, parses the HTTP request, looks at `Host`, `:path`, and headers, and then re-emits the request toward the correct backend Service. That is what an **ingress** does.

### 1.2 The information needed for the decision is in the request body

Modern routing decisions are not destination-only. Examples:

- **Canary by header**: send `X-Canary: true` to v2; everyone else to v1.
- **Tenant routing**: `X-Tenant: acme` to acme's dedicated pool, `X-Tenant: globex` to globex's pool, default elsewhere.
- **Path-based versioning**: `/v1/*` to legacy, `/v2/*` to new.
- **Method routing**: write-heavy `POST`/`PUT` to primary region, `GET` to nearest replica.
- **A/B testing**: cookie-based bucket assignment with sticky behavior.
- **Per-route auth**: `/internal/*` requires JWT; `/public/*` doesn't.

All of those require the router to be reading HTTP headers, the URL path, and sometimes cookies — meaning the router must terminate TLS (or be doing TLS passthrough with SNI-only routing), parse HTTP/1.1 or HTTP/2 framing, and have an opinion about each request as a whole, not the packets in isolation.

### 1.3 Things that simply don't fit at L4

- **TLS termination** with SNI-based routing of multiple hostnames behind one IP.
- **Retries** that need to know whether the response was 5xx (retryable) vs 4xx (not).
- **Timeouts** measured at the request level, not the connection level (a long-lived gRPC stream may carry many short request-response RPCs).
- **Request mirroring**: send a copy of the request to a second backend for shadow testing without affecting the client.
- **Header rewriting** (URL canonicalization, version stripping, request-id injection).
- **Rate limits** that need to know which API key is being used.
- **Per-route observability**: histograms keyed by `route_name`, not `service_name`.

The Kubernetes answer to all of this is two complementary APIs: **Ingress** (the original) and **Gateway API** (the replacement). Both compile down to an HTTP reverse-proxy data plane (NGINX, Envoy, HAProxy, Traefik, AWS ALB, GCP HTTP(S) Load Balancer). The difference is in the *API*, the *role separation*, and the *expressiveness*.

```
        north-south traffic (clients → cluster)
        ────────────────────────────────────
                       │
                       ▼
            ┌──────────────────────────┐
            │  L7 reverse proxy        │  ← Ingress / Gateway controller
            │  (Envoy / NGINX / etc.)  │     terminates TLS, parses HTTP,
            │                          │     applies filters, picks backend
            └──────────────┬───────────┘
                           │
                           ▼  HTTP → Service VIP
            ┌──────────────────────────┐
            │   ClusterIP Service      │  ← kube-proxy / eBPF (L4)
            └──────────────┬───────────┘
                           │
                           ▼  DNAT → Pod IP
            ┌──────────────────────────┐
            │   Pod (app process)      │
            └──────────────────────────┘
```

Inside the cluster, **east-west** traffic between Pods historically went straight through kube-proxy at L4. A *service mesh* is the same picture but applied to east-west: every Pod gets a sidecar (or, in ambient mode, a node-level proxy), and that proxy is an L7 reverse proxy too. The mesh is "everything an ingress does, but for service-to-service traffic, and with mTLS by default."

The rest of this chapter is the field guide for everything that fits in those two boxes.

---

## 2. Ingress: The Legacy API

Ingress is the original Kubernetes L7 API, introduced in 1.1 (2015) and stable since 1.19 (2020). It is a single resource, `networking.k8s.io/v1/Ingress`, with a deliberately minimal schema. The minimalism is what made it succeed early and what eventually killed it.

### 2.1 The Ingress resource

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
  namespace: shop
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /$2
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/proxy-body-size: "16m"
spec:
  ingressClassName: nginx
  tls:
  - hosts: ["shop.example.com"]
    secretName: shop-tls
  rules:
  - host: shop.example.com
    http:
      paths:
      - path: /api(/|$)(.*)
        pathType: ImplementationSpecific
        backend:
          service:
            name: api
            port:
              number: 80
      - path: /
        pathType: Prefix
        backend:
          service:
            name: storefront
            port:
              number: 80
```

The structural fields are tiny:

- `spec.ingressClassName` — names the controller (matched against the cluster-wide `IngressClass` resource).
- `spec.tls[]` — host → `Secret` map for TLS termination. The Secret must be of type `kubernetes.io/tls` and contain `tls.crt` and `tls.key`.
- `spec.rules[]` — each rule has an optional `host` and a list of HTTP paths, each path pointing at a `Service` by name and port.
- `spec.defaultBackend` — fallback when no rule matches.

Path matching has three modes:

- `Exact` — string equality.
- `Prefix` — element-wise prefix on URL path segments (`/foo` matches `/foo` and `/foo/bar`, **not** `/foobar`).
- `ImplementationSpecific` — anything the controller wants (regex, glob, custom DSL). This is the controller's escape hatch and was the original sin — every controller invented its own dialect here.

### 2.2 The IngressClass resource

`IngressClass` decouples the Ingress object from the controller binary. A cluster can have multiple Ingress controllers running side-by-side; each one watches Ingress objects whose `ingressClassName` references an `IngressClass` whose `spec.controller` field matches its own identity.

```yaml
apiVersion: networking.k8s.io/v1
kind: IngressClass
metadata:
  name: nginx
  annotations:
    ingressclass.kubernetes.io/is-default-class: "true"
spec:
  controller: k8s.io/ingress-nginx
```

`spec.controller` is a free-form string that the controller deployment matches in its own startup config (`--controller-class=k8s.io/ingress-nginx`). The `is-default-class` annotation marks one IngressClass to be assumed when an Ingress object omits `ingressClassName`. Having two default IngressClasses is a misconfiguration the apiserver does **not** detect.

### 2.3 The annotation explosion

The Ingress schema cannot express most of what real applications need: timeouts, retries, header manipulation, rate limits, websocket upgrades, custom error pages, OAuth2, redirect rules, body size limits, gRPC support, session affinity tuning, sticky-cookie names, regex routing, canary weights, sticky upstream selection. The community's response, predictably, was to overload `metadata.annotations`.

Every controller invented its own annotation namespace:

- ingress-nginx: `nginx.ingress.kubernetes.io/*` (over 100 distinct keys).
- AWS Load Balancer Controller: `alb.ingress.kubernetes.io/*`.
- GCE Ingress: `kubernetes.io/ingress.global-static-ip-name`, etc.
- Azure Application Gateway: `appgw.ingress.kubernetes.io/*`.
- Traefik: `traefik.ingress.kubernetes.io/*`.
- HAProxy Ingress: `haproxy.org/*` and `haproxy-ingress.github.io/*`.
- Contour: a separate CRD (`HTTPProxy`) because annotations weren't expressive enough.
- Emissary/Ambassador: another separate CRD (`Mapping`).

The result: moving a workload from cloud A's Ingress to cloud B's Ingress required rewriting every annotation, and silent semantic differences (e.g., NGINX's `rewrite-target` vs ALB's path conditions) caused production incidents.

```
INGRESS PORTABILITY (REALITY)

   Ingress object  ──►  controller A   ──► behaves one way
                                            │
   same Ingress    ──►  controller B   ──► silently different
                                            │
   same Ingress    ──►  controller C   ──► some annotations ignored
                                            │
   "portable"      ──►  in practice no annotations carry over
```

### 2.4 Why Ingress is being replaced

Five structural problems, not fixable in a backwards-compatible way:

1. **Annotation explosion + non-portability.** Each controller's behavior is defined by its annotation dictionary, which is opaque to the apiserver, untyped, and inconsistent across implementations.
2. **No first-class traffic splitting.** Canary releases (90/10 weighted routing) became annotation hacks (`nginx.ingress.kubernetes.io/canary-weight: "10"`), with semantics that didn't generalize.
3. **No clear ownership separation.** A cluster admin owns the controller and the LB; an application team owns the Services. But the Ingress object mixes both concerns: it references infrastructure (which TLS secret? which IngressClass?) and application routing (which path? which backend?) in one resource. There is no clean way for app teams to express "I want routes" without also having permission to bind to TLS.
4. **No cross-namespace anything.** An Ingress can only reference Services in its own namespace. Multi-tenant or shared-gateway patterns don't fit.
5. **Protocol limitations.** Ingress is HTTP-shaped. gRPC works only by abusing the HTTP path; TCP/UDP routing doesn't fit; mutual TLS, request mirroring, header-based routing all have to live in annotations.

These are not bugs. They are the consequences of a 2015 API that prioritized minimalism. Gateway API (section 4) is the explicit replacement.

---

## 3. Ingress Controllers: A Comparison

Every Ingress controller implements the same API but a different data plane, deployment model, and feature surface. The choice influences blast radius, performance, and which annotations you'll be writing for the next five years.

### 3.1 ingress-nginx (the community default)

Source: `kubernetes/ingress-nginx`. Data plane: OpenResty (NGINX + LuaJIT). Deployment: usually a `Deployment` behind a `LoadBalancer` Service (cloud) or `hostNetwork` DaemonSet (bare metal). Config generation: the controller renders an `nginx.conf` template per change and asks NGINX to reload, with Lua serving as a fast path for dynamic endpoint changes (no reload needed for backend list changes).

Strengths: huge feature surface via annotations, battle-tested, runs anywhere. Weaknesses: NGINX reload is expensive at scale; many annotations have subtle interactions; Lua introduces a second runtime to debug.

### 3.2 HAProxy Ingress

Source: `haproxytech/kubernetes-ingress` and `jcmoraisjr/haproxy-ingress`. Data plane: HAProxy. Configured via dataplane API (no reload for many changes). Strong at TCP load balancing, sticky sessions, and connection-level metrics. Annotations under `haproxy.org/*`.

### 3.3 Traefik

Source: `traefik/traefik`. Data plane: Traefik's own Go-based proxy. Deployment-friendly UX, auto-discovery, ACME integration built-in (so cert-manager is optional). Tends to be popular at small/medium scale; less common at the very large end where Envoy dominates.

### 3.4 Envoy-based: Contour, Emissary, Gloo, Istio Gateway

These don't use the bare Ingress API as the primary surface; they expose CRDs (Contour's `HTTPProxy`, Emissary's `Mapping`, Gloo's `VirtualService`, Istio's `Gateway` + `VirtualService`). They *also* implement Ingress for compatibility. Envoy is the data plane; the controller is the xDS server. This is the architecture that became the basis for Gateway API.

### 3.5 Cloud-native: AWS / GCP / Azure

These don't run an in-cluster data plane. The controller watches Ingress objects and translates them into cloud LB configuration:

- **AWS Load Balancer Controller** (formerly ALB Ingress Controller) — provisions an ALB (Application Load Balancer) per Ingress, configures target groups, security groups, listener rules. Pods are reached either via IP target mode (`eks.amazonaws.com/role-arn` for the controller, ALB → pod IP via VPC CNI) or instance mode (ALB → NodePort → kube-proxy → pod).
- **GCE Ingress** — provisions a Google Cloud HTTP(S) Load Balancer (the global one with Anycast IPs).
- **Azure Application Gateway Ingress Controller (AGIC)** — programs an Azure Application Gateway.

These have zero in-cluster data-plane footprint (the LB runs in the cloud provider's infrastructure) but they cost more in cloud bills, have provider-specific feature gaps, and are bound to the cloud's release cadence.

### 3.6 The annotation matrix (sketch)

```
Feature                  | ingress-nginx           | ALB                       | GCE                       | Traefik
─────────────────────────┼─────────────────────────┼───────────────────────────┼───────────────────────────┼────────────
TLS cert source          | spec.tls + Secret       | alb./ssl-redirect+ACM     | networking.gke.io/         | spec.tls + Secret
                         |                         | alb./certificate-arn      | managed-certificates       |
Path rewrite             | nginx./rewrite-target   | alb./actions.<name>       | NOT SUPPORTED              | traefik./router.middlewares
Body size limit          | nginx./proxy-body-size  | NOT NATIVE (use WAF)      | NOT NATIVE                 | traefik./router.middlewares
gRPC                     | nginx./backend-protocol | alb./backend-protocol-vers| networking.gke.io/v1beta1  | traefik./service.servers
Canary weight            | nginx./canary-weight    | alb./actions.<weight>     | NOT NATIVE                 | weighted services CRD
Sticky sessions          | nginx./affinity         | alb./target-group-attrs   | networking.gke.io/cookies  | traefik./service.sticky
WebSocket                | works out of box        | works                     | works                     | works
```

The takeaway is not "pick one" — the takeaway is that *the same Ingress object means six different things depending on the controller*, which is exactly why Gateway API exists.

### 3.7 Deployment models

```
   DEPLOYMENT vs DAEMONSET FOR THE CONTROLLER

   ┌─────────────────────────────────────────────┐
   │ Deployment + LoadBalancer Service           │
   │ ───────────────────────────────────────────  │
   │ • N replicas of the proxy                    │
   │ • cloud LB in front (ELB/NLB/ALB/GLB)        │
   │ • TLS terminated either at the cloud LB      │
   │   (passthrough to proxy) or at the proxy    │
   │ • most common in managed K8s                 │
   └─────────────────────────────────────────────┘

   ┌─────────────────────────────────────────────┐
   │ DaemonSet + hostNetwork                     │
   │ ───────────────────────────────────────────  │
   │ • one proxy per node, bound to host's :80/  │
   │   :443                                       │
   │ • external LB (HW LB, BGP, MetalLB) sends    │
   │   traffic to any node IP                     │
   │ • lowest possible latency (one less hop)     │
   │ • bare-metal / on-prem standard              │
   └─────────────────────────────────────────────┘
```

---

## 4. The Gateway API: A Role-Oriented Redesign

The **Gateway API** (`gateway.networking.k8s.io`) is the official successor to Ingress, GA in Kubernetes 1.32. It is maintained out-of-tree in `kubernetes-sigs/gateway-api` and shipped as a set of CRDs that any conformant controller can implement. Unlike Ingress, it is **not** a single resource; it is a graph of resources designed to separate concerns by **role**.

### 4.1 The three roles

Gateway API explicitly recognizes that running L7 in a cluster involves three different humans, often on three different teams:

```
   ┌──────────────────────────────────────────────────────────────┐
   │  Role 1 — INFRASTRUCTURE PROVIDER                            │
   │                                                              │
   │   Ships:                                                     │
   │     • A controller binary (Envoy-based, etc.)               │
   │     • A GatewayClass naming itself                           │
   │   Owns:                                                      │
   │     • The data plane image, performance, feature set         │
   │   Analogy: the people who sell you the load balancer        │
   └──────────────────────────────────────────────────────────────┘

   ┌──────────────────────────────────────────────────────────────┐
   │  Role 2 — CLUSTER OPERATOR                                   │
   │                                                              │
   │   Creates:                                                   │
   │     • Gateway objects (the "running LB instance")            │
   │     • TLS certificates (or hooks cert-manager up)            │
   │     • ReferenceGrants for cross-namespace access             │
   │   Owns:                                                      │
   │     • Hostnames, listener ports, TLS material, IP pool       │
   │   Analogy: the people who own the public DNS + edge LBs     │
   └──────────────────────────────────────────────────────────────┘

   ┌──────────────────────────────────────────────────────────────┐
   │  Role 3 — APPLICATION DEVELOPER                              │
   │                                                              │
   │   Creates:                                                   │
   │     • HTTPRoute / GRPCRoute / TCPRoute / TLSRoute / UDPRoute │
   │     • Filters (header rewrites, redirects, etc.)             │
   │     • backendRefs pointing at their Services                 │
   │   Owns:                                                      │
   │     • The routing rules for their app                        │
   │   Analogy: the people who deploy the actual workload         │
   └──────────────────────────────────────────────────────────────┘
```

This is the killer feature. Under Ingress, *every* Ingress object combined "I'm an admin and I bind to port 443 with this TLS secret" with "I'm an app dev and I want `/api/v2/users` to route here". With Gateway API, the cluster admin creates a Gateway once; many app teams attach HTTPRoutes to it. RBAC neatly separates them.

### 4.2 GatewayClass

`GatewayClass` is the K8s-shaped declaration of a controller, analogous to `StorageClass` (chapter 19) or `IngressClass`.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: envoy-gateway
spec:
  controllerName: gateway.envoyproxy.io/gatewayclass-controller
  description: "Envoy Gateway controller for production traffic"
```

`spec.controllerName` is the magic string that pairs the GatewayClass with a running controller. The controller binary is configured (via its own deployment) with this same string; it ignores GatewayClasses with any other `controllerName`.

This lets one cluster host multiple controllers (e.g., `envoy-gateway`, `istio`, `nginx-gateway-fabric`, `cilium-gateway`) side by side, each owning its own GatewayClasses. App teams pick by referencing a GatewayClass name through the chain.

### 4.3 Gateway

A Gateway is a *running instance* of a class.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: edge
  namespace: gateway-system
spec:
  gatewayClassName: envoy-gateway
  listeners:
  - name: http
    port: 80
    protocol: HTTP
    allowedRoutes:
      namespaces:
        from: All

  - name: https
    port: 443
    protocol: HTTPS
    hostname: "*.example.com"
    tls:
      mode: Terminate
      certificateRefs:
      - kind: Secret
        name: example-com-wildcard

  - name: grpc
    port: 8443
    protocol: HTTPS
    hostname: "grpc.example.com"
    tls:
      mode: Terminate
      certificateRefs:
      - kind: Secret
        name: grpc-tls

  - name: mtls-passthrough
    port: 8444
    protocol: TLS
    hostname: "vault.example.com"
    tls:
      mode: Passthrough
    allowedRoutes:
      kinds:
      - kind: TLSRoute
```

Each Gateway has one or more **listeners**, each pinning a (port, protocol, optional hostname, optional TLS) tuple. Multiple listeners on different ports can coexist. `allowedRoutes` controls which namespaces (and which Route kinds) are allowed to attach.

`tls.mode`:
- `Terminate` — the Gateway decrypts and re-encrypts (or sends cleartext upstream).
- `Passthrough` — the Gateway routes by SNI only; the encrypted bytes go through untouched. Used for end-to-end TLS, mTLS authentication at the backend, or protocols the Gateway can't parse.

The Gateway controller takes this resource and produces real-world infrastructure: a cloud LB, a Deployment+Service in the cluster, an externally configured LB, etc.

### 4.4 HTTPRoute

The application-developer resource. Each HTTPRoute attaches itself to one or more Gateways (or, with GAMMA, to Services) and specifies routing rules.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: storefront
  namespace: shop
spec:
  parentRefs:
  - name: edge
    namespace: gateway-system
    sectionName: https

  hostnames:
  - shop.example.com

  rules:
  - matches:
    - path:
        type: PathPrefix
        value: /api/v2
      headers:
      - name: X-Canary
        type: Exact
        value: "true"
    backendRefs:
    - name: api-v2
      port: 80
      weight: 100

  - matches:
    - path:
        type: PathPrefix
        value: /api/v2
    backendRefs:
    - name: api-v1
      port: 80
      weight: 90
    - name: api-v2
      port: 80
      weight: 10

  - matches:
    - path:
        type: PathPrefix
        value: /
    filters:
    - type: RequestHeaderModifier
      requestHeaderModifier:
        add:
        - name: X-Forwarded-For-Real
          value: "true"
    backendRefs:
    - name: storefront
      port: 80
```

`parentRefs` declares which Gateway (and which listener `sectionName`) this route attaches to. The route only takes effect if the Gateway's `allowedRoutes` permits it (by namespace and kind). The semantics are *both sides agree* — neither alone is enough.

### 4.5 ReferenceGrant

Gateway API enforces strict namespace boundaries. By default, a Gateway in namespace `gateway-system` cannot reference a TLS Secret in namespace `shop`, and an HTTPRoute in `shop` cannot reference a backend Service in `payments`. To unlock cross-namespace references, the **owner** of the target namespace must opt in by creating a `ReferenceGrant`:

```yaml
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  name: allow-storefront-to-payments
  namespace: payments
spec:
  from:
  - group: gateway.networking.k8s.io
    kind: HTTPRoute
    namespace: shop
  to:
  - group: ""
    kind: Service
```

Now HTTPRoutes in `shop` may set `backendRefs[*].namespace: payments`. This is the proper inverse of an IngressClass annotation: the *target* grants access, not the *source* claiming it. RBAC alignment is automatic — only an admin of the payments namespace can grant the access.

### 4.6 The resource graph

```
                ┌─────────────────────┐
                │   GatewayClass      │  ← infra provider creates
                │   (controllerName)  │
                └──────────▲──────────┘
                           │
                           │ gatewayClassName
                           │
                ┌──────────┴──────────┐
                │     Gateway         │  ← cluster operator creates
                │  (listeners, TLS)   │
                └──────▲────▲─────────┘
                       │    │
                       │    │ parentRefs (attach)
                       │    │
        ┌──────────────┘    └──────────────┐
        │                                  │
   ┌────┴─────┐                       ┌────┴─────┐
   │HTTPRoute │                       │TCPRoute  │  ← app dev creates
   │ /v1, /v2 │                       │ TLS pass │
   └────┬─────┘                       └────┬─────┘
        │ backendRefs                      │ backendRefs
        │                                  │
        ▼                                  ▼
   ┌─────────────┐                   ┌─────────────┐
   │  Service    │                   │  Service    │
   │  api-v1     │                   │  postgres   │
   └─────────────┘                   └─────────────┘
```

This is the picture every staff engineer should be able to draw. Three layers of objects, three roles, with HTTPRoute attaching to Gateway via `parentRefs` and Gateway attaching to a controller via `gatewayClassName`.

### 4.7 The route kinds

| Kind | Protocol | Use case |
|---|---|---|
| `HTTPRoute` | HTTP/1.1, HTTP/2 | Standard web traffic |
| `GRPCRoute` | gRPC over HTTP/2 | RPC services (with method-level routing) |
| `TLSRoute` | TLS passthrough | End-to-end TLS where Gateway only sees SNI |
| `TCPRoute` | Raw TCP | Non-HTTP TCP services (databases, redis, etc.) |
| `UDPRoute` | Raw UDP | DNS, QUIC fronting, telemetry, etc. |

GRPCRoute is particularly nice — it lets you route by `service.method` instead of forcing you to encode RPC structure into URL paths.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GRPCRoute
metadata:
  name: payments-grpc
  namespace: payments
spec:
  parentRefs:
  - name: edge
    namespace: gateway-system
    sectionName: grpc
  hostnames: ["grpc.example.com"]
  rules:
  - matches:
    - method:
        service: payments.v1.PaymentService
        method: Charge
    backendRefs:
    - name: payments
      port: 9000
```

---

## 5. HTTPRoute Semantics: Matching, Filters, BackendRefs

The hard parts of any L7 spec are (a) *match precedence* — what wins when two rules could match the same request, and (b) *backend selection* — what happens with multiple weighted backendRefs.

### 5.1 The match types

Inside `rules[*].matches[*]` you can specify any combination of:

- **path** — `Exact`, `PathPrefix`, or `RegularExpression` (implementation-specific).
- **headers** — list of `{name, value, type}` with type `Exact` or `RegularExpression`.
- **queryParams** — list of `{name, value, type}`.
- **method** — `GET`, `POST`, etc.

All match conditions inside a single `matches[*]` element must match (AND). Multiple `matches[*]` elements within a rule are OR'd (any one matching counts).

### 5.2 The precedence rules (the part that bites)

When multiple HTTPRoutes / rules / matches could apply, Gateway API specifies a *deterministic* total ordering (this is a major improvement over Ingress, where precedence was implementation-defined). The order, highest precedence first:

1. **`Exact` path match** wins over `PathPrefix`.
2. **Longer `PathPrefix`** wins over shorter (longest-match).
3. **More headers matched** wins (matching three headers beats matching two).
4. **More query params matched** wins (same logic).
5. **Method match** wins over no method.
6. Ties broken by creation timestamp of the route (older wins), then namespace+name lexicographic order.

The intuition: more specific matches win. A staff engineer reviewing a route diagram should be able to predict, for any incoming request, *exactly* which rule fires. If they can't, the routes are ambiguous and should be refactored.

### 5.3 Weighted backendRefs

A single rule can route to multiple backends with weights — this is the in-spec primitive for canary releases.

```yaml
rules:
- matches: [{ path: { type: PathPrefix, value: / } }]
  backendRefs:
  - name: api-v1
    port: 80
    weight: 95
  - name: api-v2
    port: 80
    weight: 5
```

Weights are integers; the controller normalizes to a probability distribution per request. If `weight: 0`, the backend is registered but receives no traffic (useful for draining or pre-warming connections). Setting all weights to zero is a black hole and produces 500s.

### 5.4 Multiple HTTPRoutes attached to the same listener

When several HTTPRoutes attach to the same Gateway listener, their **rules are merged** (think: concatenated rule lists from all routes), and the precedence ordering above is applied across the union. The merge is per-hostname: routes with different hostnames don't interfere.

```
      attach
HTTPRoute A (hostnames=[shop.example.com],   rules=[/api/v1])
HTTPRoute B (hostnames=[shop.example.com],   rules=[/api/v2])
HTTPRoute C (hostnames=[admin.example.com], rules=[/])

     Gateway listener "https" port 443
     ────────────────────────────────────
     For shop.example.com  → rules from A+B merged by precedence
     For admin.example.com → rules from C
     Otherwise              → 404
```

This is what makes the API multi-tenant-friendly: many teams can attach routes to one shared Gateway without coordinating with each other, as long as their hostnames don't collide.

### 5.5 The status side: what's actually accepted

A controller writes status back to both the Gateway and each HTTPRoute. The two key conditions:

```yaml
status:
  parents:
  - parentRef:
      name: edge
      namespace: gateway-system
    controllerName: gateway.envoyproxy.io/gatewayclass-controller
    conditions:
    - type: Accepted
      status: "True"
      reason: Accepted
    - type: ResolvedRefs
      status: "True"
      reason: ResolvedRefs
```

`Accepted=False` means the route was rejected (bad parentRef, no ReferenceGrant, attached to wrong namespace, etc.). `ResolvedRefs=False` means at least one `backendRef` couldn't be resolved (Service doesn't exist, ReferenceGrant missing). A staff engineer debugging a Gateway should look at these conditions *first* before debugging the controller logs.

---

## 6. Gateway API Filters and Extensions

Filters are the part of the spec where the request can be modified before reaching the backend, or where the request is short-circuited (e.g., a 301 redirect). The spec defines a small set of **core** filters that every conformant controller must support, plus an extension mechanism for controller-specific behavior.

### 6.1 Core filters

```yaml
rules:
- matches: [{ path: { type: PathPrefix, value: /old } }]
  filters:
  - type: RequestRedirect
    requestRedirect:
      scheme: https
      statusCode: 301
      hostname: new.example.com
      path:
        type: ReplacePrefixMatch
        replacePrefixMatch: /new
```

The filters defined as **core**:

- **RequestHeaderModifier** — add, set, or remove request headers.
- **ResponseHeaderModifier** — same, on the response.
- **URLRewrite** — change host or path before forwarding (e.g., `/api/v1/users` → `/users` to the backend).
- **RequestRedirect** — 30x redirect back to the client (no backend involved).
- **RequestMirror** — send a copy of the request to a second backend; the response from the mirror is dropped. Used for shadow testing.

Example combining several:

```yaml
rules:
- matches: [{ path: { type: PathPrefix, value: /v1/orders } }]
  filters:
  - type: RequestHeaderModifier
    requestHeaderModifier:
      add:
      - name: X-Request-ID
        value: "{generated}"
      remove:
      - X-Internal-Debug
  - type: URLRewrite
    urlRewrite:
      path:
        type: ReplacePrefixMatch
        replacePrefixMatch: /orders
  - type: RequestMirror
    requestMirror:
      backendRef:
        name: orders-shadow
        port: 80
  backendRefs:
  - name: orders
    port: 80
```

That route: matches `/v1/orders/*`, adds an `X-Request-ID` header (controllers may interpret `{generated}` as a directive — this is implementation-specific), removes a debug header, rewrites the URL to drop the `/v1` prefix, mirrors a copy of the request to a shadow Service, and finally forwards to the real `orders` backend.

### 6.2 ExtensionRef: the controller-specific escape hatch

Core filters can't express everything. Gateway API leaves the door open through `ExtensionRef`:

```yaml
filters:
- type: ExtensionRef
  extensionRef:
    group: gateway.envoyproxy.io
    kind: HTTPRouteFilter
    name: rate-limit-per-tenant
```

`group/kind` identifies a CRD that the controller understands. The named object is interpreted by that specific controller. The spec deliberately defines this as opaque to enable innovation without forcing every feature through the core spec.

Examples in the wild:

- Envoy Gateway: `BackendTrafficPolicy`, `ClientTrafficPolicy`, `SecurityPolicy` (for OIDC, JWT, CORS).
- Istio (when used as a Gateway controller): `EnvoyFilter`, `WasmPlugin`.
- Cilium Gateway: `CiliumNetworkPolicy`-style filters.

This solves the annotation problem from Ingress: instead of stuffing controller-specific config into stringly-typed annotations, controllers ship their own typed CRDs that integrate via `ExtensionRef`.

### 6.3 BackendTLSPolicy: upstream TLS

By default, Gateway API specifies how *client → Gateway* TLS works (via listeners), but not how *Gateway → backend* TLS works. `BackendTLSPolicy` (currently `gateway.networking.k8s.io/v1alpha3`) fills that gap:

```yaml
apiVersion: gateway.networking.k8s.io/v1alpha3
kind: BackendTLSPolicy
metadata:
  name: api-tls
  namespace: shop
spec:
  targetRefs:
  - group: ""
    kind: Service
    name: api
    sectionName: https
  validation:
    caCertificateRefs:
    - kind: ConfigMap
      name: api-ca-bundle
    hostname: api.shop.svc.cluster.local
```

Now the Gateway initiates TLS to the backend Service, validating its certificate against the named CA bundle.

---

## 7. Migrating from Ingress to Gateway API

For any cluster older than 1.32, you almost certainly have Ingress. Migrating is rarely a single big-bang flip; it's a controller-by-controller, route-by-route translation. The reasons to move are concrete:

- **Portability**: routes written against Gateway API run on Envoy Gateway, Istio, Cilium Gateway, NGINX Gateway Fabric, AWS Gateway API Controller, GKE Gateway, etc., without rewriting annotations.
- **Native traffic splitting**: no more `nginx.ingress.kubernetes.io/canary-weight`; weights are first-class.
- **Mesh integration**: GAMMA (section 24) extends Gateway API to east-west, unifying north-south and mesh routing under one spec.
- **Ownership separation**: cluster admins own Gateways, app teams own Routes.
- **Future of L7 in Kubernetes**: SIG-Network's stated direction. Ingress will stay supported but receive no new features.

### 7.1 The mechanical mapping

| Ingress concept | Gateway API equivalent |
|---|---|
| `IngressClass` | `GatewayClass` |
| `Ingress.spec.tls[]` | `Gateway.spec.listeners[].tls.certificateRefs` |
| `Ingress.spec.rules[].host` | `HTTPRoute.spec.hostnames[]` |
| `Ingress.spec.rules[].http.paths[]` | `HTTPRoute.spec.rules[].matches[].path` |
| Single backend | `HTTPRoute.spec.rules[].backendRefs` (weight: 100) |
| `nginx.ingress.kubernetes.io/canary-*` | weighted `backendRefs` |
| `nginx.ingress.kubernetes.io/rewrite-target` | `URLRewrite` filter |
| `nginx.ingress.kubernetes.io/server-snippet` | `ExtensionRef` to controller CRD |

### 7.2 Tools

The `kubernetes-sigs/ingress2gateway` tool generates Gateway API manifests from existing Ingress objects:

```
$ ingress2gateway print --providers=ingress-nginx,gce,istio
```

It handles the structural mapping plus a curated subset of annotations. Anything it can't translate is dumped as a comment for manual conversion.

### 7.3 The dual-stack strategy

In practice you'll run both APIs side-by-side during migration:

```
                          ┌──────────────────────────┐
                          │   Cloud LB (or MetalLB)  │
                          └────────┬─────────┬───────┘
                                   │         │
                          ┌────────┴───┐ ┌───┴────────┐
                          │ ingress-   │ │ Envoy      │
                          │ nginx      │ │ Gateway    │
                          │ (Ingress)  │ │ (Gateway)  │
                          └─────┬──────┘ └─────┬──────┘
                                │              │
              new apps          │              │  legacy apps
                     ──────────►              ◄────────────
                  ┌──────────┐         ┌──────────────┐
                  │ HTTPRoute │         │   Ingress    │
                  └──────────┘         └──────────────┘
```

Decommission the Ingress controller only after the last Ingress object is gone.

---

## 8. Envoy: The Universal Data Plane

Almost every modern L7 proxy in Kubernetes — Istio sidecars, Istio ambient waypoints, Contour, Emissary, Gloo, Kong (Gateway mode), Tetrate, AWS App Mesh, Envoy Gateway, Cilium L7 — is built on **Envoy**. Understanding Envoy as a foundation pays off across many products.

Source: `envoyproxy/envoy`. Original author: Matt Klein at Lyft. Written in C++17 with a single non-blocking event loop per worker thread. Production-tested at the highest scales (Lyft, Google, Twitter, AWS, etc.).

### 8.1 The configuration hierarchy

Envoy's runtime config is a tree of objects. Knowing this tree is the difference between reading a 5000-line `envoy.yaml` and being lost.

```
Bootstrap (envoy.yaml at startup, points at xDS endpoints)
├── static_resources
│   ├── listeners[]            ← static, hardcoded listeners
│   └── clusters[]             ← static upstreams (usually only xDS itself)
└── dynamic_resources
    ├── lds_config             ← discover listeners
    ├── cds_config             ← discover clusters
    └── ads_config             ← aggregated discovery

At runtime, the discovered hierarchy is:

Listener (port + bind address + filter chains)
├── filter_chains[]
│   ├── filter_chain_match     ← match by SNI, source IP, ALPN, etc.
│   └── filters[]              ← network filters in order
│       └── http_connection_manager (HCM) ← turns L4 into L7
│           ├── route_config (or RDS reference)
│           │   └── virtual_hosts[]
│           │       └── routes[]
│           │           ├── match (path, headers, etc.)
│           │           └── route_action
│           │               └── cluster: "some_cluster"
│           └── http_filters[] ← in order: authn, ratelimit, ..., router (terminal)
│
Cluster (logical upstream, named)
├── load_assignment (or EDS reference)
│   └── endpoints[]
│       └── lb_endpoints[]
│           └── address, health_status
├── lb_policy (ROUND_ROBIN, LEAST_REQUEST, RING_HASH, MAGLEV, ...)
├── outlier_detection
├── circuit_breakers
└── transport_socket (TLS context, mTLS, etc.)
```

### 8.2 Listeners and filter chains

A **listener** binds to an address+port and accepts connections. The first work it does on a new connection is decide which **filter chain** to apply, based on `filter_chain_match` (SNI, source IP, ALPN, source port range). Each filter chain is a list of **network filters** (operate on byte streams at L4) executed in order.

The crucial network filter is `envoy.filters.network.http_connection_manager` (HCM). HCM is what turns a TCP byte stream into HTTP request/response semantics. Without HCM in the chain, the connection is just L4 (suitable for TCP proxying, TLS termination, etc.).

### 8.3 The HTTP Connection Manager (HCM)

HCM owns the HTTP state machine: framing (HTTP/1.1, HTTP/2, HTTP/3 / QUIC), connection management, request decoding, response encoding. It runs a per-request pipeline of **HTTP filters**:

```
   Request enters HCM
        │
        ▼
   ┌──────────────────────────┐
   │ envoy.filters.http.cors  │
   ├──────────────────────────┤
   │ envoy.filters.http.jwt   │  ← authn
   ├──────────────────────────┤
   │ envoy.filters.http.      │
   │   ext_authz              │  ← OIDC / OPA call
   ├──────────────────────────┤
   │ envoy.filters.http.      │
   │   ratelimit              │  ← global RLS call
   ├──────────────────────────┤
   │ envoy.filters.http.lua   │  ← custom logic
   ├──────────────────────────┤
   │ envoy.filters.http.wasm  │  ← WebAssembly extension
   ├──────────────────────────┤
   │ envoy.filters.http.      │
   │   router                 │  ← TERMINAL (sends upstream)
   └──────────────────────────┘
        │
        ▼ HTTP request → upstream cluster
```

The `router` filter is the terminal filter — it's the one that actually picks an upstream cluster and forwards the request. Everything before it can modify, short-circuit, or reject. The order matters; this is exactly where Istio injects its policy filters.

### 8.4 Clusters and endpoints

A **cluster** is a logical group of upstreams plus load-balancing/health policy. Each cluster has:

- **load_assignment** — the endpoints (typically discovered via EDS).
- **lb_policy** — round-robin, least-request, ring-hash (consistent hashing), maglev (also consistent hashing, deterministic), random, least-loaded.
- **health_checks** — active health checking config.
- **outlier_detection** — passive ejection (eject backends that produce too many 5xx).
- **circuit_breakers** — connection / request limits per priority.
- **transport_socket** — TLS context, including client certs for mTLS.

```yaml
clusters:
- name: payments
  connect_timeout: 0.25s
  type: EDS
  eds_cluster_config:
    eds_config:
      ads: {}
  lb_policy: LEAST_REQUEST
  health_checks:
  - timeout: 1s
    interval: 5s
    healthy_threshold: 2
    unhealthy_threshold: 3
    http_health_check: { path: /healthz }
  circuit_breakers:
    thresholds:
    - priority: DEFAULT
      max_connections: 1024
      max_pending_requests: 1024
      max_requests: 1024
      max_retries: 3
  outlier_detection:
    consecutive_5xx: 5
    interval: 10s
    base_ejection_time: 30s
    max_ejection_percent: 50
  transport_socket:
    name: envoy.transport_sockets.tls
    typed_config:
      "@type": type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext
      common_tls_context:
        tls_certificate_sds_secret_configs:
        - name: spiffe://cluster.local/ns/shop/sa/storefront
          sds_config:
            ads: {}
```

The `transport_socket` here is what makes Envoy present a SPIFFE-identity client certificate when calling this upstream — exactly the mechanism Istio uses for mTLS (section 12).

### 8.5 Routes and virtual hosts

Inside HCM, a `route_config` is a list of `virtual_hosts`, each of which has a list of `routes`. The HCM picks the virtual host by matching the `Host` header (or `:authority` in HTTP/2/3), then walks its routes top-to-bottom looking for the first match.

```yaml
route_config:
  virtual_hosts:
  - name: shop
    domains: ["shop.example.com", "shop.example.com:*"]
    routes:
    - match: { prefix: "/api/v2" }
      route:
        cluster: api_v2
        timeout: 5s
        retry_policy:
          retry_on: "5xx,reset"
          num_retries: 2
    - match: { prefix: "/" }
      route:
        cluster: storefront
```

Routes can be discovered via RDS (Route Discovery Service) — they're not always static.

### 8.6 xDS at the wire level

Envoy's runtime config is delivered by **xDS**: a family of gRPC APIs that push config changes over a bidirectional stream. The acronyms:

- **LDS** — Listener Discovery Service (top of tree).
- **RDS** — Route Discovery Service (referenced by listeners).
- **CDS** — Cluster Discovery Service.
- **EDS** — Endpoint Discovery Service (referenced by clusters).
- **SDS** — Secret Discovery Service (TLS keys/certs).
- **ADS** — Aggregated Discovery Service (all of the above on one stream).

Section 9 covers xDS in depth.

### 8.7 Hot restart and drain

Envoy supports **hot restart**: a new Envoy process can be started, take over listening sockets from the old via SCM (a Unix domain socket protocol), and drain existing connections from the old process while the new accepts fresh ones. This makes upgrades and config changes possible without dropping connections.

In Kubernetes, hot restart is less common (the Pod is usually just replaced), but the **drain** semantics still apply: a graceful shutdown waits for `drain_timeout` (typically 10 minutes) before killing in-flight requests. In Istio sidecars, the `terminationGracePeriodSeconds` of the Pod has to be at least as long as Envoy's drain timeout, or you'll cut connections mid-request.

---

## 9. xDS: How the Control Plane Streams Config

xDS is the protocol that turns Envoy into a *data plane controlled by software*. Without xDS, every Envoy config change would mean editing yaml on disk and restarting. With xDS, the control plane (Istio's `istiod`, Contour, Envoy Gateway, etc.) computes config and streams it to each Envoy in near-real time.

### 9.1 The basic shape

xDS is a bidirectional gRPC stream. The Envoy client opens a stream and sends **DiscoveryRequest** messages; the server replies with **DiscoveryResponse** messages, possibly many for one request (server-streamed).

```protobuf
message DiscoveryRequest {
  string version_info = 1;     // last accepted version
  Node node = 2;               // identifies this Envoy
  repeated string resource_names = 3;
  string type_url = 4;         // e.g. type.googleapis.com/.../Listener
  string response_nonce = 5;   // ACK/NACK correlation
  ErrorDetail error_detail = 6;// non-empty = NACK
}

message DiscoveryResponse {
  string version_info = 1;
  repeated google.protobuf.Any resources = 2;
  bool canary = 3;
  string type_url = 4;
  string nonce = 5;
  ControlPlane control_plane = 6;
}
```

The protocol is essentially:

1. Envoy sends `DiscoveryRequest{type_url=LDS, version="", nonce=""}`.
2. Server replies with `DiscoveryResponse{version="42", resources=[listener1, listener2], nonce="abc"}`.
3. Envoy applies the resources. If success, it sends `DiscoveryRequest{type_url=LDS, version="42", nonce="abc"}` — the new request acks `nonce=abc` by referencing it, and bumps its known version. If it rejects (e.g., invalid config), it sends `DiscoveryRequest{type_url=LDS, version="<previous>", nonce="abc", error_detail={...}}` — that's a NACK, telling the server to roll back or fix.
4. When the server has new resources, it sends another `DiscoveryResponse`; the cycle repeats.

This ACK/NACK model is essential: it lets Envoy refuse bad config without dying, and lets the control plane know which versions are live in the fleet.

### 9.2 SOTW vs Delta (Incremental)

Envoy supports two variants of xDS:

- **State of the World (SOTW)** — every response contains *all* resources of a given type. If you have 10000 clusters, every CDS response is a 10000-element list. Simple semantics; expensive at scale.
- **Incremental (Delta) xDS** — responses contain only added/modified resources, plus a list of resource names that were *removed*. Much cheaper at scale.

```protobuf
message DeltaDiscoveryResponse {
  string system_version_info = 1;
  repeated Resource resources = 2;       // added or updated
  string type_url = 4;
  repeated string removed_resources = 6; // deleted by name
  string nonce = 5;
}
```

Istio defaults to delta xDS since 1.20. Older Envoys still speak SOTW.

### 9.3 ADS: Aggregated Discovery Service

Without aggregation, each xDS type uses its own stream: LDS stream, RDS stream, CDS stream, EDS stream. That's four (or more) streams per Envoy. With **ADS**, they're multiplexed onto one stream, with `type_url` discriminating.

The bigger benefit of ADS is **ordering**. Without ordering, you can race: a new listener arrives referencing a cluster that hasn't been delivered yet. Envoy applies the listener, the cluster reference is dangling, traffic fails. ADS lets the control plane sequence updates:

```
   The "make-before-break" order for ADS:
   ─────────────────────────────────────

   1. CDS  ← new clusters first
        ▼
   2. EDS  ← endpoints for those clusters
        ▼
   3. LDS  ← listeners that reference clusters
        ▼
   4. RDS  ← routes inside those listeners
```

For removals, the reverse order: RDS → LDS → EDS → CDS, so a cluster isn't removed while a listener still references it.

This is the basis for Istio's eventual-consistency guarantees: the control plane intentionally sequences config updates so that intermediate states are always *consistent enough* not to produce 5xx.

### 9.4 The config-drift race

ADS ordering doesn't *eliminate* races; it only narrows them. The most common drift:

```
   Time t:    SDS pushes new TLS material to Envoy.
              SDS ACK takes ~30ms.
   Time t+5:  LDS pushes new listener that references that TLS material.
              LDS ACK arrives.
              Listener flips active.
   Time t+10: SDS ACK arrives. Now Envoy actually has the TLS material.

   Between t+5 and t+10, a connection might arrive and fail TLS
   because the listener is live but the cert isn't in place yet.
```

In Istio, this manifests as transient TLS handshake failures during config churn. Mitigations:

- Pin SDS resources by listener filter so the listener waits for them.
- Use **resource warming** — Envoy will not flip a listener until all its referenced resources are present.
- Use **drain on update** — when a listener config changes, Envoy creates a *new* listener and drains the old one rather than mutating in place.

The mental model: xDS is **eventually consistent**, but Envoy's resource warming makes most config flips appear atomic to traffic.

### 9.5 The xDS REST variant

Less common in K8s but worth knowing: xDS also has a REST variant (long-poll over HTTP), used by some older clients and by deployments where gRPC isn't available. Modern Envoy + Istio always uses gRPC.

### 9.6 The xDS test client

Envoy's `envoy-static --mode validate` and `xds-relay` projects let you snapshot what a control plane is pushing — invaluable for debugging "what config does Envoy actually have?" The matching client-side dump is `curl localhost:15000/config_dump` on the Envoy admin endpoint.

---

## 10. Istio Architecture: Sidecar Mode

Istio (`istio/istio`) is the most feature-rich service mesh in the ecosystem. Originally launched by Google, IBM, and Lyft in 2017, it has been through one major architecture overhaul (the merge of Mixer, Pilot, Citadel, and Galley into a single binary `istiod`) and is now in the middle of another (the introduction of ambient mesh, section 11). Sidecar mode is the historical default and remains the most-deployed model.

### 10.1 The components

```
   ISTIO SIDECAR ARCHITECTURE
   ──────────────────────────

   Control plane (one or a few replicas, cluster-wide)
   ┌──────────────────────────────────────────────────┐
   │  istiod  ─────────────────────────────────────   │
   │  ┌──────────┐  ┌──────────┐  ┌──────────────┐    │
   │  │  Pilot   │  │ Citadel  │  │   Galley     │    │
   │  │ xDS gen, │  │ cert     │  │ config       │    │
   │  │ pushes   │  │ issuance │  │ validation,  │    │
   │  │ to each  │  │ SPIFFE   │  │ XDS push     │    │
   │  │ proxy    │  │          │  │ ordering     │    │
   │  └──────────┘  └──────────┘  └──────────────┘    │
   │                                                  │
   │  Watches: K8s Service, Pod, Endpoint,            │
   │           VirtualService, DestinationRule, ...   │
   └──────────────────────────────────────────────────┘
                         │
                         │ gRPC ADS over mTLS
                         │
            ┌────────────┼─────────────┐
            ▼            ▼             ▼
       ┌─────────┐  ┌─────────┐  ┌─────────┐
       │ Pod A   │  │ Pod B   │  │ Pod C   │
       │ ┌─────┐ │  │ ┌─────┐ │  │ ┌─────┐ │
       │ │app  │ │  │ │app  │ │  │ │app  │ │
       │ └─────┘ │  │ └─────┘ │  │ └─────┘ │
       │ ┌─────┐ │  │ ┌─────┐ │  │ ┌─────┐ │
       │ │envoy│ │  │ │envoy│ │  │ │envoy│ │
       │ │side │ │  │ │side │ │  │ │side │ │
       │ │car  │ │  │ │car  │ │  │ │car  │ │
       │ └─────┘ │  │ └─────┘ │  │ └─────┘ │
       └─────────┘  └─────────┘  └─────────┘
```

`istiod` is the single binary today. The split labels (Pilot, Citadel, Galley) refer to internal modules:

- **Pilot** — converts K8s state + Istio CRDs into Envoy xDS, streams it to every sidecar over ADS.
- **Citadel** — the in-cluster CA. Issues per-workload SPIFFE certs (~24h TTL by default), rotates automatically.
- **Galley** — config validation + xDS aggregation (less prominent now; mostly merged into Pilot).

### 10.2 Sidecar injection

Sidecars are injected by a **mutating admission webhook** (chapter 6). When a Pod is created in a namespace labeled `istio-injection=enabled` (or with revision labels for multi-revision installs), the webhook patches the Pod spec to add:

- An **initContainer** (`istio-init`) that runs `iptables` to redirect all inbound traffic to port 15006 and all outbound traffic to port 15001 — both bound by the sidecar.
- A **container** (`istio-proxy`) running `pilot-agent` + Envoy, owning ports 15001 (outbound), 15006 (inbound), 15000 (admin), 15020 (merged Prometheus), 15090 (Envoy's own stats).

```
   POD WITH SIDECAR (one network namespace, two processes + init)
   ──────────────────────────────────────────────────────────────

   ┌──────────────────────────────────────────────────────────┐
   │  Pod netns                                               │
   │                                                          │
   │  ┌──────────┐                                            │
   │  │ initCtr  │  ran once: iptables -t nat -A PREROUTING   │
   │  │istio-init│  -p tcp -j REDIRECT --to-port 15006        │
   │  │ (exited) │  (and OUTPUT → 15001), excluding 15001/    │
   │  │          │  15006 themselves, excluding uid 1337      │
   │  └──────────┘                                            │
   │                                                          │
   │  ┌──────────┐         ┌─────────────┐                    │
   │  │ app      │ ◄─────► │ envoy       │ ◄── port 15006 in  │
   │  │ container│   loop  │ (istio-     │ ─── port 15001 out │
   │  │          │   back  │  proxy)     │                    │
   │  │ uid=1000 │         │ uid=1337    │                    │
   │  └──────────┘         └─────────────┘                    │
   │                                                          │
   │  iptables in netns:                                      │
   │    PREROUTING : tcp → REDIRECT 15006                     │
   │    OUTPUT    : tcp → REDIRECT 15001                      │
   │    (uid 1337 excluded → Envoy's own egress isn't looped) │
   └──────────────────────────────────────────────────────────┘
```

The `uid=1337` exclusion is critical: it stops Envoy's *own* outbound connections from being redirected back to itself (infinite loop). Envoy runs as uid 1337 by convention.

The lifecycle ordering matters too. With **native sidecars** (chapter 11), `istio-proxy` is declared as an `initContainer` with `restartPolicy: Always`, which guarantees it starts before the app container and survives the app container restarting. Pre-1.28 Istio used `holdApplicationUntilProxyStarts` and a `preStop` sleep to approximate the same ordering.

### 10.3 The CRD set

Istio's API is large. The core CRDs in `networking.istio.io/v1`:

- **Gateway** — opens a port on the ingress gateway (a special standalone Envoy at the cluster edge). Different from Gateway API's Gateway.
- **VirtualService** — routing rules (matches → destinations). The L7 brain.
- **DestinationRule** — per-destination policy (load balancing, connection pool, outlier detection, TLS to upstream, subsets).
- **ServiceEntry** — register external services with the mesh (so sidecars know how to call them).
- **Sidecar** — limit a workload's xDS config (don't push every cluster's config to every sidecar).
- **WorkloadEntry** / **WorkloadGroup** — onboard VMs into the mesh.

And in `security.istio.io/v1`:

- **PeerAuthentication** — mTLS policy (STRICT / PERMISSIVE / DISABLE).
- **RequestAuthentication** — JWT validation.
- **AuthorizationPolicy** — L7 RBAC (allow/deny based on source identity, headers, paths).

Example combining several:

```yaml
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: payments
  namespace: shop
spec:
  hosts: ["payments"]
  http:
  - match:
    - headers:
        x-canary:
          exact: "true"
    route:
    - destination:
        host: payments
        subset: v2
  - route:
    - destination:
        host: payments
        subset: v1
      weight: 90
    - destination:
        host: payments
        subset: v2
      weight: 10
    timeout: 3s
    retries:
      attempts: 2
      perTryTimeout: 1s
      retryOn: gateway-error,5xx,reset
---
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: payments
  namespace: shop
spec:
  host: payments
  trafficPolicy:
    connectionPool:
      tcp: { maxConnections: 100 }
      http:
        http1MaxPendingRequests: 100
        maxRequestsPerConnection: 10
    outlierDetection:
      consecutive5xxErrors: 5
      interval: 10s
      baseEjectionTime: 30s
    loadBalancer:
      simple: LEAST_REQUEST
  subsets:
  - name: v1
    labels: { version: v1 }
  - name: v2
    labels: { version: v2 }
```

The VirtualService says *how to route*; the DestinationRule says *how to talk to the destination once chosen*. Subsets are the labeled slices of pods under the same Service.

### 10.4 The Sidecar resource (the one everyone forgets)

By default, every sidecar receives xDS for every Service in every namespace, because Istio assumes any Pod might call any Service. For a 100-namespace cluster this is enormous config (tens of MB per sidecar, regenerated on every push). The `Sidecar` resource limits this:

```yaml
apiVersion: networking.istio.io/v1
kind: Sidecar
metadata:
  name: default
  namespace: shop
spec:
  egress:
  - hosts:
    - "shop/*"
    - "payments/payments.payments.svc.cluster.local"
    - "istio-system/*"
```

Now sidecars in `shop` only see config for their own namespace, the specific `payments` Service, and the control plane namespace. Cluster-wide, this can cut xDS push time by 10x and memory per sidecar by 5x. Forgetting Sidecar resources is the single most common scaling pitfall in Istio.

### 10.5 The Ingress Gateway

For north-south traffic, Istio runs a standalone Envoy in the `istio-system` namespace as a `Deployment`, exposed via `LoadBalancer` Service. This is the "Istio Ingress Gateway" — same Envoy data plane, no app container alongside, configured by `Gateway` + `VirtualService` resources.

The role of Istio's Gateway / VirtualService is largely superseded by Gateway API (HTTPRoute attaching to a Gateway in the K8s spec sense) — modern Istio supports both, and Gateway API is the recommended forward path.

---

## 11. Istio Ambient Mesh: ztunnel + Waypoints

Ambient mesh, introduced in 2022 and GA in Istio 1.24 (2024), is Istio's answer to the cost of sidecars. The two-line summary: **remove the sidecar from every Pod, replace it with a per-node L4 proxy (ztunnel) for transport security, and only spin up L7 proxies (waypoints) when you actually need L7 features**.

### 11.1 The architecture

```
   AMBIENT MESH (no sidecars, no per-pod restart for mesh upgrades)
   ───────────────────────────────────────────────────────────────

   ┌────────────────────────────────────────────────────────────┐
   │  Node                                                      │
   │                                                            │
   │  ┌──────────────────────────────────────────────────────┐  │
   │  │ ztunnel (DaemonSet, one per node)                    │  │
   │  │ • L4 only: mTLS + HBONE (HTTP/2 CONNECT tunneling)   │  │
   │  │ • Holds workload identities for pods on this node    │  │
   │  │ • Rust implementation (small footprint)              │  │
   │  └──────────────────────────────────────────────────────┘  │
   │             ▲                                              │
   │             │ traffic redirected via iptables/eBPF         │
   │             │ from pod netns                               │
   │             │                                              │
   │  ┌──────────┴──┐  ┌──────────┐  ┌──────────┐               │
   │  │  Pod A      │  │  Pod B   │  │  Pod C   │  no sidecar!  │
   │  │ (no proxy)  │  │          │  │          │               │
   │  └─────────────┘  └──────────┘  └──────────┘               │
   └────────────────────────────────────────────────────────────┘

   PLUS, when L7 is needed:

   ┌────────────────────────────────────────────────────────────┐
   │  Namespace shop                                            │
   │                                                            │
   │  ┌──────────────────────────────────────────────────────┐  │
   │  │ waypoint proxy (Envoy, Deployment in shop ns)        │  │
   │  │ • Receives traffic destined for shop's services      │  │
   │  │ • Applies L7 policies (VirtualService, AuthZ)        │  │
   │  └──────────────────────────────────────────────────────┘  │
   │     ▲                                                      │
   │     │  ztunnel from other nodes forwards traffic           │
   │     │  TO the waypoint (HBONE-tunneled) when L7 needed    │
   │     │                                                      │
   │  Pods in shop namespace (still no sidecar)                 │
   └────────────────────────────────────────────────────────────┘
```

Three layers:

- **ztunnel** (Zero-Trust Tunnel) — a Rust-based, per-node DaemonSet proxy that handles L4 only: terminating mTLS at the edge of the node, encapsulating traffic in HBONE (HTTP/2 CONNECT with mTLS), forwarding to peer ztunnels on other nodes. Source: `istio/ztunnel`. Identity-aware (holds each Pod's SPIFFE cert) but doesn't parse application protocols.
- **Waypoint proxy** — a per-service or per-namespace Envoy Deployment, opt-in. Routes L7 traffic for the workloads it's configured for. Only deployed when something actually requires L7 (a VirtualService with header routing, an AuthorizationPolicy with method matching, etc.).
- **Pods** — unchanged. No sidecar, no init container modification. Traffic is redirected by node-level iptables (or, with Istio CNI, by eBPF) to the local ztunnel.

### 11.2 HBONE

**HBONE** = HTTP-Based Overlay Network Encapsulation. It's HTTP/2 `CONNECT` over mTLS:

```
   Pod A sends a TCP request → local ztunnel.
   ztunnel opens HBONE tunnel to peer ztunnel on Pod B's node:
     HTTP/2 over mTLS, then CONNECT method targeting Pod B's IP:port.
   Peer ztunnel decrypts, forwards locally to Pod B.
```

HBONE is essentially how you get end-to-end mTLS without each Pod participating in the TLS handshake. Identity is preserved (the mTLS cert on the HBONE tunnel is the source Pod's SPIFFE identity); the tunnel multiplexes many flows.

### 11.3 Pay-as-you-go L7

The ambient design's killer property: **you only pay for L7 where you use L7**. A namespace that needs only mTLS and observability runs no waypoint at all — ztunnel handles everything. A namespace that needs header-based routing for one service deploys a single waypoint, used by that service.

In sidecar mode, every Pod paid Envoy's ~50-100 MiB overhead, whether or not the workload used any L7 feature. In ambient mode, only the workloads that need L7 incur that cost, and only at the waypoint (one or two replicas per namespace, not per-pod).

### 11.4 Operational consequences

- **No pod restart for mesh upgrades.** Upgrading ztunnel is upgrading the DaemonSet; pods don't notice. Upgrading a waypoint is upgrading its Deployment; only L7-using traffic might see a brief blip.
- **Lower baseline latency.** Per-request added latency drops from ~1-2ms (sidecar) to ~0.2-0.5ms (ztunnel L4-only).
- **No sidecar injection webhook complexity.** No init container, no iptables in the pod netns, no terminationGracePeriod tuning for sidecars.
- **Cleaner mental model for operators.** Identity at L4 is one component (ztunnel); L7 policy is a separate, opt-in component (waypoint). Easier to reason about than "every pod has an Envoy."

The tradeoff: ambient is newer; the feature surface lags sidecar mode (some advanced VirtualService features only ran in sidecar Envoy initially). The ecosystem is catching up fast.

---

## 12. mTLS in Istio: SPIFFE, Citadel, PeerAuthentication

Mutual TLS is the cornerstone security feature of any modern service mesh: every service-to-service connection is encrypted, both ends present certificates, both verify each other's identity. The mesh issues and rotates the certs automatically — that's the value-add over rolling your own TLS.

### 12.1 SPIFFE identities

Istio uses **SPIFFE** (Secure Production Identity Framework for Everyone) to name workloads. A SPIFFE ID is a URI:

```
spiffe://cluster.local/ns/<namespace>/sa/<serviceaccount>
```

Examples:
- `spiffe://cluster.local/ns/shop/sa/storefront`
- `spiffe://cluster.local/ns/payments/sa/payments-service`

This identity ties to the **Kubernetes ServiceAccount** that the Pod uses, not the Pod itself. The Pod inherits the SA's identity. This is the link that lets RBAC policies in Kubernetes correspond to authorization policies in the mesh.

The cert's SAN (Subject Alternative Name) carries the SPIFFE URI. When Envoy A connects to Envoy B and presents its cert, B reads the SAN, gets the SPIFFE ID, and (via `AuthorizationPolicy`) decides whether that identity is allowed to call this endpoint.

### 12.2 Citadel's cert issuance flow

```
   Pod startup, ambient or sidecar mode:
   ─────────────────────────────────────

   1. Pod has a projected SA token mounted (audience: istio-ca)
   2. pilot-agent (in istio-proxy) reads the token
   3. pilot-agent generates a private key in memory
   4. CSR sent to istiod over gRPC, authenticated by the SA token
   5. istiod (Citadel) verifies the token via TokenReview to apiserver
   6. Citadel issues a cert with SAN=spiffe://.../ns/<ns>/sa/<sa>,
      signed by the in-cluster CA (root cert in istio-ca-secret)
   7. pilot-agent loads the cert into Envoy via SDS (Secret Discovery)
   8. Envoy presents it on outbound, validates inbound certs against root
   9. ~12 hours later, pilot-agent rotates: new key, new CSR, new cert
```

The key never leaves the Pod — only the CSR does. The CA root is what every Envoy in the mesh trusts. If you're integrating with an external PKI (e.g., HashiCorp Vault as PKI engine), Istio supports plugging in an external CA via cert-manager's `istio-csr` or `external-istiod`.

### 12.3 PeerAuthentication: STRICT, PERMISSIVE, DISABLE

```yaml
apiVersion: security.istio.io/v1
kind: PeerAuthentication
metadata:
  name: default
  namespace: istio-system
spec:
  mtls:
    mode: STRICT
```

Mesh-wide (when placed in the root namespace `istio-system`):

- **STRICT** — only mTLS connections accepted. Cleartext is rejected.
- **PERMISSIVE** — both mTLS and cleartext accepted (sidecars detect protocol). Useful during migration: pods that already have a sidecar use mTLS, pods that don't (yet) can still talk cleartext.
- **DISABLE** — only cleartext. Effectively turns off mTLS.

PeerAuthentication can be scoped: cluster-wide (root namespace), namespace-wide (any namespace), or per-workload (via `selector`).

The migration discipline:

1. Install Istio.
2. Set mesh-wide `PeerAuthentication: PERMISSIVE`.
3. Inject sidecars (or enable ambient) into every workload.
4. Verify with telemetry that all traffic is mTLS.
5. Flip mesh-wide to `STRICT`.

Skipping step 4 and going straight to STRICT is the *single most common Istio outage* — any workload not yet injected gets locked out.

### 12.4 The handshake

```
   mTLS HANDSHAKE (between two Envoys)
   ───────────────────────────────────

   client envoy                              server envoy
   ────────────                              ────────────
   TCP SYN  ────────────────────────────────►
            ◄──────────────────────────────── TCP SYN/ACK
   TCP ACK  ────────────────────────────────►

   ClientHello (TLS 1.2/1.3, SNI=server-fqdn,
                supported ALPN, supported versions)
            ────────────────────────────────►
            ◄──────────────────────────────── ServerHello
                                              + Certificate (SPIFFE in SAN)
                                              + CertificateRequest
                                              + ServerHelloDone

   Certificate (SPIFFE in SAN)
   ClientKeyExchange
   CertificateVerify (client signs handshake hash)
   ChangeCipherSpec
   Finished
            ────────────────────────────────►
                                              VERIFY client cert chain → CA
                                              VERIFY SAN → SPIFFE ID
                                              ALLOWED? (AuthorizationPolicy)
            ◄──────────────────────────────── ChangeCipherSpec
                                              Finished

   APPLICATION DATA (HTTP/2 frames, etc.)
            ◄════════════════════════════════►
```

In TLS 1.3 this collapses to one round trip (the handshake messages above are batched). The important detail is that both sides verify identity *and* the SAN is the SPIFFE URI used by AuthorizationPolicy downstream.

### 12.5 AuthorizationPolicy

The L7-aware RBAC layer.

```yaml
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: payments-readers
  namespace: payments
spec:
  selector:
    matchLabels:
      app: payments
  action: ALLOW
  rules:
  - from:
    - source:
        principals:
        - "cluster.local/ns/shop/sa/storefront"
        - "cluster.local/ns/admin/sa/auditor"
    to:
    - operation:
        methods: ["GET"]
        paths: ["/v1/charges/*"]
  - from:
    - source:
        principals:
        - "cluster.local/ns/admin/sa/auditor"
    to:
    - operation:
        methods: ["GET", "POST"]
        paths: ["/v1/audit/*"]
```

That policy allows `storefront` to GET `/v1/charges/*`, allows `auditor` to GET and POST `/v1/audit/*`, and (because there's no explicit allow) denies everything else. Default action is `ALLOW` of all (deny becomes implicit when at least one ALLOW policy targets a workload). For zero-trust, also create a default-deny:

```yaml
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: deny-all
  namespace: payments
spec: {}  # empty spec = deny all
```

(An empty AuthorizationPolicy means "deny everything"; this is the explicit default-deny pattern.)

---

## 13. Traffic Management in Istio

Beyond mTLS, the second big value-add of Istio is traffic management: declarative routing, canary releases, fault injection, retries, timeouts, circuit breakers. Almost all of this is in `VirtualService` and `DestinationRule`.

### 13.1 Canary by weight

```yaml
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: reviews
  namespace: bookinfo
spec:
  hosts: ["reviews"]
  http:
  - route:
    - destination: { host: reviews, subset: v1 }
      weight: 95
    - destination: { host: reviews, subset: v2 }
      weight: 5
```

Weights are integer percentages, sum to 100. The pattern: deploy v2, point 5% there, monitor SLIs, gradually increase weight. Argo Rollouts can drive this automatically.

### 13.2 Canary by header (test-in-prod)

```yaml
http:
- match:
  - headers:
      x-test-version:
        exact: v2
  route:
  - destination: { host: reviews, subset: v2 }
- route:  # default
  - destination: { host: reviews, subset: v1 }
```

The first matched rule wins. This lets internal testers force traffic to v2 without exposing it to real users.

### 13.3 Fault injection

```yaml
http:
- fault:
    delay:
      percentage: { value: 10 }
      fixedDelay: 5s
    abort:
      percentage: { value: 1 }
      httpStatus: 503
  route:
  - destination: { host: reviews }
```

10% of requests get an extra 5 second delay; 1% return 503. Use for chaos testing in lower environments; production traffic should obviously not have this enabled.

### 13.4 Retries with budgets

```yaml
http:
- route:
  - destination: { host: reviews }
  retries:
    attempts: 3
    perTryTimeout: 2s
    retryOn: gateway-error,5xx,reset,connect-failure
    retryRemoteLocalities: true
```

Up to 3 retry attempts, each capped at 2 seconds. Retried on a curated set of failure modes. `retryRemoteLocalities` lets retries hit other zones if the local zone is failing.

Important: **retries multiply load**. A 3-retry policy under failure conditions sends 4x the request rate (1 original + up to 3 retries). If every hop in a chain does that, you have geometric explosion. Use **retry budgets** (cap retries as % of new requests) at the global level via Envoy's `retry_budget`. Istio surfaces this via `EnvoyFilter`.

### 13.5 Timeouts

```yaml
http:
- route:
  - destination: { host: reviews }
  timeout: 5s
```

Critical rule: **upstream timeouts must be longer than the chain's longest combined retry+timeout**, or you cascade timeouts. If a calls b with timeout=1s, and b calls c with timeout=2s, then b's call to c gets cut off by a's timeout long before c's own timeout can fire. That manifests as 504s in a's logs.

### 13.6 Circuit breakers and outlier detection

```yaml
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: payments
spec:
  host: payments
  trafficPolicy:
    connectionPool:
      tcp:
        maxConnections: 100
      http:
        http2MaxRequests: 1000
        maxRequestsPerConnection: 100
        maxRetries: 3
    outlierDetection:
      consecutive5xxErrors: 5
      interval: 10s
      baseEjectionTime: 30s
      maxEjectionPercent: 50
```

- **connectionPool** limits — circuit breaker. Once exceeded, new requests fail fast instead of queueing.
- **outlierDetection** — passive ejection. After 5 consecutive 5xxes, the endpoint is ejected from the load-balancing pool for 30s. After 30s it's re-introduced; if it fails again, ejected for 60s (exponential backoff). `maxEjectionPercent: 50` means at most half the pool can ever be ejected at once (avoids ejecting everything during a global outage).

### 13.7 Locality LB

```yaml
trafficPolicy:
  loadBalancer:
    localityLbSetting:
      enabled: true
      failoverPriority:
      - topology.istio.io/network
      - topology.kubernetes.io/region
      - topology.kubernetes.io/zone
```

Prefer same-zone backends first; on failure, same-region; finally any. Combines with topology-aware Service hints (chapter 14) for zone-affinity at both Service and mesh layers.

---

## 14. Linkerd: The Rust Alternative

Linkerd (`linkerd/linkerd2`) is the philosophical opposite of Istio: minimal feature surface, lowest possible footprint, opinionated defaults, simpler API. Maintained by Buoyant. Original author: William Morgan.

### 14.1 The architecture

```
   LINKERD ARCHITECTURE
   ────────────────────

   Control plane (in linkerd namespace)
   ┌─────────────────────────────────────────────┐
   │ identity     — issues mTLS certs            │
   │ destination  — resolves Service → endpoints │
   │ proxy-injector — sidecar mutating webhook   │
   │ policy       — authorization policies       │
   └─────────────────────────────────────────────┘

   Data plane (per pod)
   ┌─────────────────────────────────────────────┐
   │ linkerd2-proxy (Rust, written from scratch) │
   │ ───────────────────────────────────────────  │
   │ Not Envoy. Ultralight: ~10-20 MiB resident,  │
   │ <1ms p99 added latency. Built on Tokio.      │
   └─────────────────────────────────────────────┘
```

Linkerd's data plane is **not Envoy**. It's `linkerd2-proxy`, a Rust micro-proxy purpose-built for this use case. The cost: less feature surface than Envoy. The benefit: a tenth of the memory, sometimes a tenth of the latency.

### 14.2 Service Profiles

The Linkerd analog of Istio's VirtualService/DestinationRule, but declarative-per-Service:

```yaml
apiVersion: linkerd.io/v1alpha2
kind: ServiceProfile
metadata:
  name: payments.shop.svc.cluster.local
  namespace: shop
spec:
  routes:
  - name: charge
    condition:
      method: POST
      pathRegex: "/v1/charges"
    responseClasses:
    - condition:
        status:
          min: 500
          max: 599
      isFailure: true
    timeout: 5s
    isRetryable: false
  - name: get-charge
    condition:
      method: GET
      pathRegex: "/v1/charges/[^/]+"
    timeout: 2s
    isRetryable: true
  retryBudget:
    retryRatio: 0.2
    minRetriesPerSecond: 10
    ttl: 10s
```

`retryBudget` is a clean expression of "retries are at most 20% of new requests, with a floor of 10/sec, decayed over 10s." This is what Istio expects you to encode via EnvoyFilter.

### 14.3 Built-in mTLS

mTLS is on by default in Linkerd. There's no equivalent of PeerAuthentication; mTLS is just always happening between meshed pods. To opt out for a specific port, annotate the pod (rare).

### 14.4 The HTTPRoute path

Linkerd 2.14+ uses Gateway API's `HTTPRoute` natively (via GAMMA, section 24) for routing instead of inventing its own resource. This is the cleanest expression of the GAMMA vision: a mesh that consumes the same K8s-standard route objects an Ingress would.

### 14.5 What Linkerd doesn't have

- No multi-cluster federation as deep as Istio's (Linkerd's multi-cluster is via `Link` CRDs, simpler model).
- No WASM extensions (the proxy doesn't run user code).
- No fault injection in the API (you can still do chaos engineering, just not via Linkerd resources).
- No ingress-gateway-with-full-feature-parity (Linkerd encourages a separate Ingress like nginx or Emissary in front).

This is by design. Linkerd's pitch: "service meshes should be boring." Most mesh use cases are mTLS + observability + retries/timeouts, and Linkerd is laser-focused on those.

---

## 15. Istio vs Linkerd vs Cilium Mesh

The three live options, contrasted along the axes that actually matter.

### 15.1 Performance

```
   PERFORMANCE (per-request added latency, sidecar mode)
   ─────────────────────────────────────────────────────

   no mesh           : baseline
   Linkerd           : +0.2–0.5 ms p99
   Istio (Envoy)     : +0.8–1.5 ms p99
   Istio (ambient)   : +0.3–0.8 ms p99
   Cilium (sidecarless) : +0.1–0.4 ms p99
                          (eBPF in kernel, no userspace hop)

   MEMORY (idle, per pod)
   ──────────────────────
   Linkerd proxy     : 10–25 MiB
   Envoy sidecar     : 50–120 MiB
   ztunnel (per node, amortized over pods on that node)
                     : ~30 MiB total
   Cilium mesh       : ~5 MiB additional (just BPF maps)
```

Numbers are rough; the ratios are real and consistent across benchmarks (CNCF, Istio's own, Buoyant's).

### 15.2 Features

| Capability | Linkerd | Istio sidecar | Istio ambient | Cilium mesh |
|---|---|---|---|---|
| mTLS | yes | yes | yes (HBONE) | yes (WireGuard or IPsec) |
| Identity | TrustDomain | SPIFFE | SPIFFE | SPIFFE-ish (via cilium identity) |
| L7 routing | HTTPRoute | VS / HTTPRoute | HTTPRoute | HTTPRoute (Envoy waypoint) |
| Traffic split | yes | yes | yes | yes |
| Fault injection | no | yes | yes | yes (via Envoy) |
| Circuit breaking | basic | yes | yes | yes (via Envoy) |
| WASM filters | no | yes | yes | yes |
| Multi-cluster | yes (Link) | yes (multi-primary) | yes | yes (Cluster Mesh) |
| Egress gateways | basic | yes | partial | yes |
| Observability | viz extension | built-in (Prom/Grafana/Kiali/Jaeger) | same | Hubble |
| External CA | yes | yes (cert-manager-csi) | yes | yes |

### 15.3 Operational complexity

```
   OPERATIONAL COMPLEXITY (lower = simpler)
   ────────────────────────────────────────

   Linkerd            : ★☆☆☆☆  one binary, defaults that work
   Cilium mesh        : ★★☆☆☆  if you already run Cilium CNI, mostly free
   Istio ambient      : ★★★☆☆  newer, but no sidecar restarts to manage
   Istio sidecar      : ★★★★★  Sidecar resources, version skew per pod,
                                 push amplification at scale,
                                 EnvoyFilter footguns
```

### 15.4 The "do you need a mesh?" question

A mesh is justified when at least one of these is true:

1. **You need automatic mTLS** between services and the dev cost of doing it in libraries is unacceptable.
2. **You need L7 policy/observability** uniformly across many services.
3. **You need declarative traffic management** (canaries, retries, timeouts) without per-app code changes.
4. **You're on a polyglot stack** and can't standardize on a library-based RPC framework.

A mesh is *not* justified when:

1. **All your services are one language with one RPC framework.** Library-level resilience (gRPC, Finagle, etc.) gives you 90% of the value at near-zero overhead.
2. **You have fewer than ~10 services and traffic is north-south dominated.** A good Ingress / Gateway is enough.
3. **You can't afford the latency or the memory.** Latency-critical workloads (HFT, real-time bidding) often skip the mesh.

The honest position is that *most* clusters don't need Istio's full feature surface. Cilium mesh or Linkerd is enough.

---

## 16. Ingress vs Gateway vs Mesh: A Decision Tree

The three pieces are not mutually exclusive — most real clusters end up running at least two. Here's how to decide.

```
   THE DECISION TREE
   ─────────────────

   Do you need HTTP routing from outside the cluster?
   │
   ├─ YES → You need north-south L7.
   │        │
   │        ├─ New cluster, 1.32+ : use Gateway API (Envoy Gateway,
   │        │                       Istio Gateway, NGINX Gateway Fabric,
   │        │                       Cilium Gateway, cloud-provider impl)
   │        │
   │        └─ Existing cluster   : keep Ingress, plan migration to
   │                                Gateway API. Don't add new annotations.
   │
   └─ NO  → Only east-west traffic; no public ingress needed.
            (rare; usually internal-only platforms or batch systems)

   Do you need mTLS / L7 policy / canaries between services?
   │
   ├─ YES → You need a mesh.
   │        │
   │        ├─ Already on Cilium CNI : enable Cilium mesh
   │        ├─ Simple needs, focus on
   │        │  observability + mTLS  : Linkerd
   │        ├─ Full traffic mgmt,
   │        │  ambient preferred     : Istio ambient
   │        └─ Full traffic mgmt,
   │           sidecar baseline      : Istio sidecar
   │
   └─ NO  → Plain Services + Ingress / Gateway is enough.
            Reconsider when you cross ~50 services or need uniform mTLS.
```

The most common production combo for medium-to-large clusters:

```
   ┌──────────────────────────────────────────────┐
   │   CLIENTS (browsers, mobile apps, partners)  │
   └────────────────────┬─────────────────────────┘
                        │  TCP + TLS
                        ▼
   ┌──────────────────────────────────────────────┐
   │  Cloud LB (ALB / GLB / Azure App Gateway)    │
   │  or MetalLB + BGP                            │
   └────────────────────┬─────────────────────────┘
                        │
                        ▼
   ┌──────────────────────────────────────────────┐
   │  Gateway (Envoy Gateway / Istio Gateway)     │
   │  • Gateway API                               │
   │  • TLS termination (edge cert from           │
   │    cert-manager + Let's Encrypt)             │
   │  • HTTPRoute attached by app teams           │
   └────────────────────┬─────────────────────────┘
                        │  HTTPS → Service VIP
                        ▼
   ┌──────────────────────────────────────────────┐
   │  Service mesh (Istio ambient / Linkerd /     │
   │  Cilium mesh)                                │
   │  • mTLS between pods                         │
   │  • east-west L7 routing                      │
   │  • per-route policy                          │
   └────────────────────┬─────────────────────────┘
                        │  pod-to-pod
                        ▼
   ┌──────────────────────────────────────────────┐
   │   Pods                                       │
   └──────────────────────────────────────────────┘
```

Gateway for north-south, mesh for east-west, with the same HTTPRoute spec language now spanning both (via GAMMA).

---

## 17. TLS Termination Patterns

There are four canonical places to terminate TLS, and most production architectures use at least two of them.

### 17.1 At the cloud LB (passthrough to nodes)

```
   client ──TLS──► cloud LB ──TCP passthrough──► node ──...──► pod
```

The cloud LB does no TLS work; it forwards the encrypted bytes by SNI (TLS passthrough). The TLS termination happens further inside (at the Gateway, the pod, or both).

Pros: end-to-end TLS without app changes. The LB just routes.
Cons: cloud LB can't do L7 features (no host/path routing at the LB). Mostly used for raw TCP services or when end-to-end TLS is mandatory (compliance).

### 17.2 At the cloud LB (LB terminates)

```
   client ──TLS──► cloud LB (TLS terminates) ──cleartext──► node ──► pod
```

Cloud LB owns the cert (AWS ACM, GCP managed certs, Azure App Gateway). Cleartext inside the cluster. Cheapest model, lowest in-cluster CPU.

Pros: zero cert management for the cluster. Cloud handles renewals.
Cons: cleartext between LB and pod (insufficient for zero-trust). Use only inside a trusted network boundary or combine with mesh mTLS.

### 17.3 At the Ingress / Gateway

```
   client ──TLS──► cloud LB ──TCP/TLS──► Gateway (TLS terminates) ──HTTP──► pod
```

The Gateway holds the cert (typically from cert-manager + Let's Encrypt). TLS terminates at the edge of the cluster.

Pros: cluster-managed certs, L7 features at the Gateway, central management.
Cons: cleartext inside the cluster (unless you add mesh mTLS).

This is the most common configuration. Combined with mesh mTLS, it gives you:

```
   client ──TLS(edge)──► Gateway ──mTLS(mesh)──► Pod
```

Two distinct TLS sessions; the Gateway re-encrypts.

### 17.4 At the pod (or mesh sidecar / ztunnel)

```
   client ──TLS──► ... ──passthrough or re-encrypt──► sidecar / pod (TLS terminates)
```

End-to-end TLS or mTLS, all the way to the pod. The pod (or its sidecar) presents the cert. Used for high-compliance environments, or for protocols where TLS termination must happen at the application (gRPC streaming with client certs, for example).

Pros: zero cleartext anywhere in the chain. Strongest security posture.
Cons: must coordinate cert distribution to every pod (Istio Citadel solves this; without a mesh, it's painful).

### 17.5 The combination most production clusters end up at

```
   client ──edge TLS──► Gateway ──pod-to-pod mTLS──► Pod
                        (cert from           (cert from Citadel /
                         cert-manager)        cert-manager + Linkerd identity)
```

cert-manager handles the public-facing edge cert (auto-renews via ACME). The mesh CA handles internal mTLS (rotates frequently, short-lived). Two different cert lifecycles, two different operational concerns, both automated.

---

## 18. Certificate Management: cert-manager and Citadel

Manually managing TLS certs in 2026 is malpractice. cert-manager and the mesh CA between them automate everything.

### 18.1 cert-manager fundamentals

`cert-manager.io` is the de-facto Kubernetes cert-management controller. Source: `cert-manager/cert-manager`. It introduces three main CRDs:

- **Issuer** / **ClusterIssuer** — defines how to obtain certs. Backends: ACME (Let's Encrypt, ZeroSSL), CA (in-cluster CA), Vault, Venafi, self-signed, external.
- **Certificate** — requests a cert. Specifies dnsNames, secretName (where to store), issuerRef, duration, renewBefore.
- **CertificateRequest** — internal, auto-created.

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    email: ops@example.com
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: letsencrypt-prod-account
    solvers:
    - http01:
        ingress:
          class: nginx
    - dns01:
        route53:
          region: us-east-1
      selector:
        dnsZones: ["example.com"]
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: shop-example-com-wildcard
  namespace: gateway-system
spec:
  secretName: shop-example-com-wildcard
  issuerRef:
    name: letsencrypt-prod
    kind: ClusterIssuer
  dnsNames:
  - "*.shop.example.com"
  duration: 2160h     # 90 days
  renewBefore: 360h   # 15 days
```

cert-manager will request the cert via ACME (using DNS-01 for the wildcard, since HTTP-01 can't do wildcards), store it as a Secret of type `kubernetes.io/tls`, and renew 15 days before expiry.

The Gateway then references the Secret by `secretName` in its listener.

### 18.2 ACME challenges: HTTP-01 vs DNS-01

- **HTTP-01** — Let's Encrypt requests `http://<domain>/.well-known/acme-challenge/<token>` and expects a specific response. cert-manager spins up a temporary pod that serves the token. Requires port 80 reachable from the internet. **Does not support wildcards.**
- **DNS-01** — cert-manager creates a TXT record at `_acme-challenge.<domain>`. Let's Encrypt verifies the record. Supports wildcards. Requires DNS API credentials (Route53, Cloud DNS, etc.).

For multi-domain or wildcard certs, DNS-01 is mandatory. Also: if you run a strict default-deny NetworkPolicy, HTTP-01 challenges may be blocked from reaching the temporary solver pod (the policy needs an explicit allow); DNS-01 sidesteps this.

### 18.3 The Istio Citadel path

Citadel (the CA module of `istiod`) is separate from cert-manager. It issues **workload certs** to mesh sidecars / ztunnel, not edge certs.

```
   cert-manager : edge certs (public-facing, long-lived,
                  from Let's Encrypt or your enterprise CA)
                  → consumed by Gateway listener via secretName

   Citadel       : workload certs (in-cluster mTLS, short-lived ~24h)
                  → consumed by sidecar / ztunnel via SDS
```

The two systems are complementary. cert-manager handles the cert at the cluster's edge; Citadel handles the cert at every workload.

### 18.4 External CA integration

For organizations that already run a PKI (Vault, AWS Private CA, HashiCorp Boundary, an internal CA), you can have Citadel chain to that external CA rather than running its own root. The `istio-csr` project (by cert-manager) lets Citadel delegate to cert-manager, which talks to your PKI.

---

## 19. Rate Limiting: Local and Global

Rate limiting in Envoy is two distinct features with very different operational properties.

### 19.1 Local rate limiting

```yaml
http_filters:
- name: envoy.filters.http.local_ratelimit
  typed_config:
    "@type": type.googleapis.com/envoy.extensions.filters.http.local_ratelimit.v3.LocalRateLimit
    stat_prefix: http_local_rate_limiter
    token_bucket:
      max_tokens: 1000
      tokens_per_fill: 1000
      fill_interval: 1s
    filter_enabled:
      runtime_key: local_rate_limit_enabled
      default_value:
        numerator: 100
        denominator: HUNDRED
```

Per-Envoy, in-memory token bucket. Zero added latency. Cluster-wide rate is `N replicas × per-replica rate`, which is the failure mode: if you scale up, your effective rate doubles. Useful for *per-instance* limits (e.g., "this proxy will accept at most 1000 RPS regardless of cluster").

### 19.2 Global rate limiting

```yaml
http_filters:
- name: envoy.filters.http.ratelimit
  typed_config:
    "@type": type.googleapis.com/envoy.extensions.filters.http.ratelimit.v3.RateLimit
    domain: shop
    timeout: 0.05s
    rate_limit_service:
      grpc_service:
        envoy_grpc:
          cluster_name: rate_limit_cluster
      transport_api_version: V3
```

Envoy calls out to a central **rate limit service** (RLS) over gRPC. The RLS holds the actual counters (typically in Redis). Each request becomes a network round-trip. Pros: globally consistent limits regardless of replica count. Cons: per-request latency (~1-3ms), blast radius if the RLS is down (with `failure_mode_deny: false`, you fail-open; with `true`, you fail-closed and a wedged RLS becomes a cluster outage).

### 19.3 Per-route rate limiting in Gateway API

Envoy Gateway exposes rate limiting through a typed `BackendTrafficPolicy`:

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: BackendTrafficPolicy
metadata:
  name: per-tenant-rate-limit
spec:
  targetRefs:
  - group: gateway.networking.k8s.io
    kind: HTTPRoute
    name: api
  rateLimit:
    type: Global
    global:
      rules:
      - clientSelectors:
        - headers:
          - name: x-tenant
            value: free-tier
        limit:
          requests: 100
          unit: Minute
```

Per-tenant rate limiting via header, attached to an HTTPRoute via `targetRefs`. This is the ExtensionRef pattern in action.

---

## 20. Locality-Aware Load Balancing

Locality LB is the mesh-layer answer to "keep traffic in the same zone." Combine with Service topology hints (chapter 14) for a full-stack solution.

### 20.1 The locality model

Each endpoint has a locality:

```
locality:
  region: us-east-1
  zone:   us-east-1a
  sub_zone: rack-7
```

In Kubernetes, locality is automatic from node labels:

- `topology.kubernetes.io/region` → region
- `topology.kubernetes.io/zone` → zone

### 20.2 Failover priorities

Envoy's `locality_lb_config`:

```yaml
common_lb_config:
  locality_weighted_lb_config: {}
load_assignment:
  cluster_name: payments
  policy:
    overprovisioning_factor: 140
  endpoints:
  - locality: { region: us-east-1, zone: us-east-1a }
    priority: 0      # same zone, highest priority
    lb_endpoints: [ ... ]
  - locality: { region: us-east-1, zone: us-east-1b }
    priority: 1      # same region, lower
    lb_endpoints: [ ... ]
  - locality: { region: us-west-2, zone: us-west-2a }
    priority: 2      # different region, last resort
    lb_endpoints: [ ... ]
```

Envoy serves entirely from priority 0 *as long as* enough endpoints are healthy. Once healthy-endpoint count in priority 0 drops below the overprovisioning factor (140% means: 71% of endpoints must be healthy to stay fully on priority 0), traffic spills to priority 1, then priority 2.

### 20.3 Cost rationale

Cross-zone traffic in AWS / GCP is **billed**. At scale (10M req/day, average response 10KB), keeping 90% of traffic same-zone vs. uniform-random can save thousands a month. Mesh locality LB is one of the few features with a directly measurable dollar return.

### 20.4 Combined with Service topology hints

K8s 1.27+ has `service.kubernetes.io/topology-mode: Auto` (formerly `topology-aware-hints`). When set, kube-proxy / Cilium prefers endpoints in the same zone *at the L4 layer*. With mesh locality LB on top, you get zone affinity at both Service and mesh layers — defense in depth.

---

## 21. Multi-Cluster Mesh

A mesh per cluster is fine for one cluster. Across clusters, you want service-to-service mTLS, name resolution, and failover to span clusters too.

### 21.1 Istio multi-primary

Each cluster has its own `istiod`. They all share a **root CA** (or chain to the same external CA). Each cluster's istiod can also discover Services from peer clusters (via a "remote secret" — a kubeconfig granting read access to peers).

```
   CLUSTER A                              CLUSTER B
   ─────────                              ─────────

   istiod-A                               istiod-B
   trust root: shared CA                  trust root: same shared CA
       │                                      │
       ▼                                      ▼
   sidecars in A                          sidecars in B
   trust certs signed by shared CA        trust certs signed by shared CA

       traffic from A to B
       ───────────────────►
       routed via east-west gateway in B
       mTLS verified by SPIFFE identity (cluster.local/...)
```

Service `payments` in cluster B is discovered by cluster A as a `ServiceEntry` (auto-generated). DNS resolution within A points at the east-west gateway of B. mTLS uses the shared CA; the SPIFFE identity from B's pods is verifiable by A's sidecars.

### 21.2 Linkerd multi-cluster

Linkerd uses a `Link` CRD: cluster A's control plane is told "here is cluster B's kubeconfig + the name of the multi-cluster gateway service in B." Mirrored Services appear in A's namespace pointing at B's gateway.

```yaml
apiVersion: multicluster.linkerd.io/v1alpha1
kind: Link
metadata:
  name: cluster-b
  namespace: linkerd-multicluster
spec:
  targetClusterDomain: cluster.b.example.com
  targetClusterLinkerdNamespace: linkerd
  gatewayIdentity: linkerd-gateway.linkerd-multicluster.serviceaccount.identity.linkerd.cluster.local
```

### 21.3 Cilium Cluster Mesh

Cilium ClusterMesh creates a shared identity space across clusters with Pod-to-Pod direct routing (when network topology permits) or via WireGuard tunnels. ClusterMesh is covered in chapter 26 (multi-cluster) and chapter 16 (Cilium).

---

## 22. Observability in the Mesh

A service mesh is, among other things, an instrumentation system. Every request is parsed at L7, so the mesh can emit per-request metrics, logs, and trace spans for free.

### 22.1 The mesh metrics canon

All major meshes emit (some variant of):

- `request_total{source, destination, method, response_code}` — counter.
- `request_duration_milliseconds_bucket{...}` — histogram (per-request latency).
- `request_size_bytes_bucket{...}` — histogram (request payload size).
- `response_size_bytes_bucket{...}` — histogram.
- `tcp_received_bytes_total`, `tcp_sent_bytes_total` — for L4 flows.
- `tcp_connections_opened_total`, `tcp_connections_closed_total`.

These map cleanly to RED (Rate, Errors, Duration) and USE (Utilization, Saturation, Errors) — the four golden signals at the request layer.

### 22.2 Istio observability stack

Out of the box (via `istioctl install --set profile=demo` or addons):

- **Prometheus** scrapes every sidecar's `/stats/prometheus` endpoint (port 15020).
- **Grafana** dashboards: mesh-wide, per-service, per-workload.
- **Kiali** — service topology visualization, generated from telemetry. Shows graph: who talks to whom, with throughput and error rate edges.
- **Jaeger** (or Zipkin / Tempo) — distributed tracing. Sidecars inject and propagate trace context (B3 / W3C TraceContext), emit spans.

### 22.3 Linkerd's `viz`

`linkerd viz install` adds Prometheus + Grafana + a viz UI. Simpler than Istio's, fewer dashboards. The `linkerd viz` CLI gives live `top`-style views: `linkerd viz top` shows live RPS, latencies, error rates streaming from sidecars.

### 22.4 Envoy admin endpoint

Every Envoy has an admin server (typically on port 15000 in Istio):

```
GET /stats              → all stats (huge text dump)
GET /stats/prometheus   → Prometheus format
GET /config_dump        → entire runtime config (yaml)
GET /clusters           → per-cluster health, endpoint count
GET /listeners          → bound listeners
GET /server_info        → version, uptime
POST /logging?level=debug → change log level live
POST /drain_listeners   → start draining
```

The admin endpoint is invaluable when debugging — `kubectl exec` into the sidecar, `curl localhost:15000/clusters` and you see exactly what backends Envoy thinks exist.

### 22.5 Access logging

Envoy can emit one log line per request to stdout (or to a gRPC access log service):

```yaml
access_log:
- name: envoy.access_loggers.file
  typed_config:
    "@type": type.googleapis.com/envoy.extensions.access_loggers.file.v3.FileAccessLog
    path: /dev/stdout
    log_format:
      json_format:
        start_time: "%START_TIME%"
        method: "%REQ(:METHOD)%"
        path: "%REQ(X-ENVOY-ORIGINAL-PATH?:PATH)%"
        protocol: "%PROTOCOL%"
        response_code: "%RESPONSE_CODE%"
        response_flags: "%RESPONSE_FLAGS%"
        duration_ms: "%DURATION%"
        upstream_service_time: "%RESP(X-ENVOY-UPSTREAM-SERVICE-TIME)%"
        source_address: "%DOWNSTREAM_REMOTE_ADDRESS%"
        upstream_host: "%UPSTREAM_HOST%"
        request_id: "%REQ(X-REQUEST-ID)%"
```

`%RESPONSE_FLAGS%` is the high-signal field: codes like `UH` (no healthy upstream), `UC` (upstream connection failure), `DC` (downstream client disconnected), `LR` (local reset). Once you learn the codes, an access log line tells you the exact failure mode in one read.

---

## 23. WebAssembly Extensions

Modifying Envoy's behavior used to require a C++ rebuild. WebAssembly (Wasm) changed that. WASM modules can be loaded into Envoy at runtime via xDS, executing in a sandbox, with a stable ABI for hooking request/response lifecycle events.

### 23.1 Proxy-Wasm ABI

The Proxy-Wasm spec (`proxy-wasm/spec`) defines the host functions Envoy exposes and the guest functions Envoy calls. Languages with proxy-wasm SDKs: Rust, Go, C++, AssemblyScript, Zig.

```rust
use proxy_wasm::traits::*;
use proxy_wasm::types::*;

#[derive(Default)]
struct AddHeader;

impl Context for AddHeader {}
impl HttpContext for AddHeader {
    fn on_http_request_headers(&mut self, _: usize, _: bool) -> Action {
        self.add_http_request_header("x-injected", "by-wasm");
        Action::Continue
    }
}
```

That tiny Rust module, compiled to WASM, is a complete request filter. Loaded into Envoy via:

```yaml
apiVersion: extensions.istio.io/v1alpha1
kind: WasmPlugin
metadata:
  name: add-header
  namespace: istio-system
spec:
  selector:
    matchLabels:
      app: frontend
  url: oci://registry.example.com/wasm/add-header:v1
  phase: AUTHN
  pluginConfig:
    upstream_clusters:
    - api
```

### 23.2 Use cases

- Custom auth (OPA-style policy evaluation in-proxy).
- Custom rate limiting that needs business logic.
- Audit logging with custom redaction.
- Header rewriting more complex than the standard filters.
- Protocol bridging (e.g., translating one wire format to another inline).

### 23.3 Tradeoffs

- WASM is sandboxed but slower than native (C++) filters. Heavy logic in WASM adds latency.
- Each module has to be carefully size-controlled — large WASM blobs slow proxy startup.
- Debugging WASM in production is harder than native (no symbol info typically).

For most needs, the core filters and `ExtensionRef`-typed CRDs are enough. WASM is the long-tail solution.

---

## 24. GAMMA: Gateway API for East-West

GAMMA (Gateway API for Mesh Management and Administration) is the working group that extends Gateway API to **east-west traffic** — i.e., to service mesh use cases. The vision: one route spec language (HTTPRoute) for both ingress and mesh routing.

### 24.1 The mechanism

In GAMMA, an HTTPRoute can attach to a Service directly:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: payments-east-west
  namespace: payments
spec:
  parentRefs:
  - group: ""
    kind: Service
    name: payments
  rules:
  - matches:
    - headers:
        - name: x-canary
          value: "true"
    backendRefs:
    - name: payments-v2
      port: 9000
  - backendRefs:
    - name: payments-v1
      port: 9000
      weight: 90
    - name: payments-v2
      port: 9000
      weight: 10
```

The `parentRefs` now points at a Service (kind: Service in the core group). The mesh data plane (Linkerd, Istio ambient, Cilium) sees this and routes accordingly.

### 24.2 Why it matters

Pre-GAMMA, each mesh had its own routing CRD: Istio's VirtualService, Linkerd's ServiceProfile, Cilium's CiliumEnvoyConfig. Application teams had to write three different YAMLs for three different meshes — exactly the portability problem Gateway API solved for ingress. GAMMA unifies it.

- Linkerd 2.14+ implements GAMMA natively (no ServiceProfile needed for routing; profiles still exist for retries/timeouts).
- Istio implements GAMMA in ambient mode.
- Cilium implements GAMMA in mesh mode.

### 24.3 The endgame

When GAMMA matures, the picture becomes:

```
   ┌────────────────────────────────────────────────────────────┐
   │  HTTPRoute (one resource type)                             │
   │  ───────────────────────────                                │
   │   parentRef: Gateway   → north-south routing               │
   │   parentRef: Service   → east-west / mesh routing          │
   └────────────────────────────────────────────────────────────┘
```

One spec, two attachment points. The same canary, mirror, retry, header-routing semantics work in both directions. Mesh CRDs become legacy.

---

## 25. The Cost of a Mesh

A mesh isn't free. At 1000-pod scale the bill matters.

### 25.1 Sidecar baseline cost

Each Envoy sidecar (idle, default Istio config):

- Memory: 50-100 MiB resident.
- CPU: 5-15 mCPU steady state.
- Startup time: 2-5 seconds before ready.

At 1000 pods:

- Memory: 50-100 GiB across the cluster — dedicated to sidecars.
- CPU: 5-15 cores at idle.
- Aggregate xDS pushes: every cluster-wide Service change pushes config to all 1000 sidecars (unless you've scoped with `Sidecar` resources).

### 25.2 Per-request added latency

```
   Per-request added latency (one-direction, sidecar mode)
   ───────────────────────────────────────────────────────

   App → local sidecar (loopback)           : ~0.2 ms
   Sidecar processing (filters, routing)   : ~0.5–1.0 ms
   mTLS handshake (amortized, 1 in ~1000)  : ~0.1 ms avg
   Sidecar → remote sidecar (network)      : same as no mesh
   Remote sidecar processing               : ~0.5–1.0 ms
   ──────────────────────────────────────────────
   Total added (one direction)              : ~1.3–2.3 ms
```

For a request that crosses 5 services (microservices fan-out), the cumulative added latency is **5–10 ms** just from the mesh. For an internal service that previously took 3 ms total, the mesh doubles the latency.

### 25.3 Ambient mesh cost

```
   Per-request added latency (ambient mode)
   ────────────────────────────────────────

   App → local ztunnel (loopback)           : ~0.1 ms
   ztunnel HBONE + mTLS                     : ~0.2 ms
   Remote ztunnel decap + forward           : ~0.2 ms
   ──────────────────────────────────────────────
   Total added (L4-only, no waypoint)       : ~0.5 ms

   When L7 needed (via waypoint):
   ztunnel → waypoint Envoy in target ns    : +0.5–1.0 ms
   waypoint processing                      : +0.5–1.0 ms
```

So: an L4-only ambient mesh adds ~0.5 ms; L7 path through a waypoint adds another ~1-2 ms. You pay per-feature, not per-pod.

### 25.4 The cost argument for ambient

At 1000 pods:

- **Sidecar memory cost**: 1000 × 80 MiB = 80 GiB.
- **Ambient memory cost**: ~50 nodes × 30 MiB (ztunnel) + ~10 waypoints × 100 MiB = ~2.5 GiB.

That's a ~30× reduction in mesh memory overhead. Multiply by your cloud's per-GiB pricing.

CPU is similar: ambient's CPU usage scales with traffic volume (concentrated on the nodes where the proxies run), not with the number of pods. For workloads where most pods are idle, ambient is dramatically cheaper.

---

## 26. Pitfalls

The cumulative wisdom of every team that ran an L7 stack in production and learned the hard way.

### 26.1 Ingress controller installed but Service LoadBalancer pending

You install ingress-nginx via Helm, the controller starts, the `LoadBalancer` Service it creates sits in `Pending` forever, no external IP. The problem: no cloud LB integration (you're on bare metal) and no MetalLB installed.

Fix: install MetalLB (or `kube-vip`, or a hardware LB integration), give it an IP pool, the Service gets an external IP.

```yaml
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: pool
  namespace: metallb-system
spec:
  addresses:
  - 192.168.1.240-192.168.1.250
```

### 26.2 STRICT mTLS flipped before all workloads have sidecars

Mesh-wide `PeerAuthentication: STRICT` rejects all non-mTLS connections. If even one workload is missing its sidecar (e.g., a namespace without `istio-injection=enabled`), it becomes unreachable from anywhere meshed. Mass outage.

Fix: stage the flip. Start `PERMISSIVE`, audit telemetry (Istio's `istio_requests_total` has a `connection_security_policy` label), only flip `STRICT` once 100% of traffic is `mutual_tls`.

### 26.3 xDS partial update / config drift

Envoy's listener flips to a new config that references a TLS cert that hasn't been pushed yet via SDS. New connections fail TLS for a few seconds.

Fix: rely on Envoy's **resource warming** (don't manually craft xDS that bypasses ADS ordering); ensure SDS resources are pushed before LDS; use `tls_certificate_sds_secret_configs` rather than inline certs in listener config (so SDS owns the lifecycle).

### 26.4 cert-manager HTTP-01 blocked by NetworkPolicy

A default-deny NetworkPolicy blocks ingress into the cert-manager solver pod that serves the HTTP-01 challenge response. ACME challenges fail; certs never issue.

Fix: either explicitly allow ingress from the Internet to the solver pod (annoying — exposes a port even briefly), or switch to DNS-01 (recommended).

### 26.5 Default-deny NetworkPolicy blocking sidecar → app loopback

A NetworkPolicy with `default-deny` on ingress also blocks intra-pod loopback traffic in some implementations (it shouldn't, since loopback isn't really ingress, but several CNIs treat it ambiguously). Sidecar can't reach app on `127.0.0.1`.

Fix: explicitly allow `127.0.0.1/32` in the policy, or use a CNI that always permits loopback (most modern CNIs do — Cilium, Calico).

### 26.6 Retries layered on retries

Service A retries 3x on failure to B. B retries 3x on failure to C. C is the failing service. Each request from A produces up to 16 requests at C. Cascade failure.

Fix: only retry at the outermost layer. Set inner services to `retries.attempts: 0`. Or use **retry budgets** (cap retries as % of new requests) to bound the multiplicative explosion.

### 26.7 Istio Sidecar resource omitted at scale

Without a `Sidecar` resource, every sidecar receives the full mesh-wide xDS — every Service, every Cluster, every Endpoint. At 200 Services, each sidecar holds ~50 MiB of config; istiod CPU spikes on every Service change.

Fix: define a default `Sidecar` per namespace limiting egress to "this namespace + control plane + explicit dependencies":

```yaml
apiVersion: networking.istio.io/v1
kind: Sidecar
metadata:
  name: default
  namespace: shop
spec:
  egress:
  - hosts:
    - "shop/*"
    - "istio-system/*"
    - "payments/payments.payments.svc.cluster.local"
```

### 26.8 Long-lived gRPC streams + xDS rolling update

You're running streaming gRPC (e.g., a long-poll API or a chat service). The control plane pushes a new listener config; Envoy's drain timeout kicks in; existing streams are cut at the end of the drain window. Clients see 5xx spike during every config change.

Fix: increase `terminationGracePeriodSeconds` on the sidecar pod to match the longest expected stream duration. Or — better — use an L4 path for streams (TCP route, no L7 inspection), so the xDS churn doesn't restart listeners that streams depend on.

### 26.9 Downstream timeout shorter than upstream timeout

A calls B with 1s timeout. B calls C with 3s timeout. C takes 1.5s. A times out at 1s, the request fails with 504. B is still waiting on C (until 3s), then completes — wasted work. Repeated, this causes B to pile up connections and run out of file descriptors.

Fix: timeout budget rule — downstream timeout ≥ sum of upstream max latencies along the path. In practice: pick conservative timeouts at the top of the chain, more aggressive deeper in.

### 26.10 No PodDisruptionBudget on the ingress controller

A node drain takes down the ingress-nginx pod that's serving live traffic. No PDB → no protection. Brief 5xx spike.

Fix:

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: ingress-nginx
  namespace: ingress-nginx
spec:
  minAvailable: 1   # or maxUnavailable: 50%
  selector:
    matchLabels:
      app.kubernetes.io/name: ingress-nginx
      app.kubernetes.io/component: controller
```

### 26.11 Forgetting to size the ingress-nginx workers

The default ingress-nginx config can be conservative. Under load (10k+ req/s), you need to tune `worker-processes`, `worker-connections`, `keep-alive-requests`, `upstream-keepalive-connections`. Otherwise the controller throttles itself.

### 26.12 Health check on the wrong endpoint

Liveness probe on the sidecar (instead of pilot-agent's `/healthz`) restarts the sidecar when Envoy momentarily slows. Healthy app pods, restarting sidecars, traffic instability.

Fix: probe `pilot-agent`'s healthz on port 15021, path `/healthz/ready`.

### 26.13 Wrong listener port for protocols

Configuring an HTTP listener but the protocol is actually gRPC (HTTP/2 over TLS). Symptoms: clients get HTTP/1.1 responses instead of streamed HTTP/2; some clients fail. Always set the protocol explicitly on the Gateway listener (`HTTPS` with `tls`, and the route as `GRPCRoute`).

### 26.14 Cert-manager rate-limited by Let's Encrypt

A misconfigured Certificate causes cert-manager to retry repeatedly. Let's Encrypt hits its rate limit (50 certs per domain per week) and refuses to issue, even when the config is finally fixed.

Fix: use the **staging** issuer (`https://acme-staging-v02.api.letsencrypt.org/directory`) during testing; only flip to prod once the cert request works end-to-end.

### 26.15 Two ingress controllers, both default

Two `IngressClass` objects both annotated `is-default-class: "true"`. Behavior on Ingress objects without `ingressClassName` is undefined; each controller may or may not pick them up. Routes silently route to the wrong controller.

Fix: only one default class. Always set `ingressClassName` explicitly on production Ingress objects.

### 26.16 Ambient: pod missing the label

Ambient mesh activates per namespace via `istio.io/dataplane-mode=ambient` label. A pod in a namespace without the label is **not** in the mesh and has no mTLS / no policy enforcement. Easy to miss in audit.

Fix: gate all production namespaces with admission policy requiring the label (Kyverno / VAP).

### 26.17 Egress through the mesh assumed but ServiceEntry missing

A workload calls `https://api.stripe.com`. With `outboundTrafficPolicy: REGISTRY_ONLY` (a hardening default for Istio), egress to unknown destinations is blocked. No ServiceEntry for stripe.com → all calls fail.

Fix: create a ServiceEntry for each external dependency, or relax the outbound policy (`ALLOW_ANY`) at the cost of security visibility.

### 26.18 Headers stripped by URLRewrite

`URLRewrite` filter changes the URL, but you assumed the Host header would stay as the original. Some implementations rewrite `Host` to the upstream service. Backend logs show `Host: api.svc.cluster.local` instead of `api.example.com`, breaking apps that key on Host.

Fix: explicitly set `hostname` in the `URLRewrite` filter, or add a `RequestHeaderModifier` to preserve the original Host.

### 26.19 NetworkPolicy blocking Envoy ↔ istiod

Default-deny egress in a namespace; Envoy can't reach istiod (15012); xDS streams fail; config goes stale. Symptoms are subtle — old config keeps working, new changes don't take effect.

Fix: allow egress from labeled mesh workloads to `istio-system` on `15012/TCP`.

### 26.20 Mesh upgrade across multiple minor versions

Istio supports +1 minor version skew between control plane and sidecars. Skipping versions during upgrades produces undefined behavior. Sidecars can be stuck on an old istiod, refusing new xDS schemas, with no clear error.

Fix: upgrade istiod first, then incrementally roll sidecars; never skip a minor.

---

## 27. TL;DR

**Services give you L4.** One VIP, one backend pool, destination-based load balancing, no application-protocol awareness. Everything else this chapter is about exists because Services alone aren't enough.

**Ingress was the original L7 API**, deliberately minimal: rules with host + path + backend Service, an IngressClass to select the controller. The minimalism forced every controller to invent annotations for everything else, producing a portability disaster. New features should not land here.

**Ingress controllers are many.** ingress-nginx (community default, NGINX data plane), HAProxy Ingress, Traefik, Envoy-based (Contour, Emissary, Gloo, Istio Gateway), cloud-managed (AWS Load Balancer Controller, GCE Ingress, Azure AGIC). The same Ingress object means different things to each.

**Gateway API replaced Ingress.** GA since 1.32, lives in `kubernetes-sigs/gateway-api`. Three roles: infra provider (GatewayClass + controller), cluster operator (Gateway with listeners + TLS), application developer (HTTPRoute / GRPCRoute / TCPRoute / TLSRoute / UDPRoute). Cross-namespace references gated by ReferenceGrant. Core filters: RequestHeaderModifier, ResponseHeaderModifier, URLRewrite, RequestRedirect, RequestMirror. Controller-specific behavior via ExtensionRef to typed CRDs. Weighted backendRefs make canary releases first-class.

**HTTPRoute precedence is deterministic.** Exact > PathPrefix; longest prefix wins; more headers matched wins; method match wins; tied ages broken by creation timestamp. No more guessing.

**Envoy is the data plane underneath most of this.** Source `envoyproxy/envoy`. Configuration is listeners → filter chains → HCM → routes → clusters → endpoints, all delivered by xDS (LDS, RDS, CDS, EDS, SDS, ADS) over gRPC bidirectional streams. ADS orders pushes (CDS → EDS → LDS → RDS) to avoid dangling references. Eventual consistency, mitigated by resource warming.

**Istio is the most feature-rich mesh.** Sidecar mode injects Envoy + iptables-redirect init container into every pod via mutating webhook; istiod is the unified control plane (Pilot for xDS, Citadel for certs, Galley for config). VirtualService for routing, DestinationRule for upstream policy, Sidecar for xDS scoping (critical at scale), PeerAuthentication for mTLS mode (STRICT/PERMISSIVE/DISABLE), AuthorizationPolicy for L7 RBAC.

**Istio ambient mode is the cost-conscious future.** ztunnel (Rust DaemonSet) does per-node L4 mTLS via HBONE; waypoints (per-namespace Envoy Deployments) do L7 only when needed. Pay-as-you-go L7. No sidecar restarts for mesh upgrades.

**mTLS uses SPIFFE identity.** Per-workload certs from Citadel, ~24h TTL, rotated automatically. Identity URI: `spiffe://cluster.local/ns/<ns>/sa/<sa>`. The SA name is the identity; the Pod inherits it.

**Linkerd is the simpler alternative.** Rust micro-proxy `linkerd2-proxy`, not Envoy. Tiny memory footprint, sub-millisecond p99 added latency. ServiceProfile CRD for retries/timeouts; mTLS on by default; GAMMA-native (uses HTTPRoute directly).

**Cilium mesh is the eBPF answer.** Kernel-level routing, no userspace proxy for L4 mTLS (WireGuard or IPsec), Envoy waypoints for L7 when needed. Lowest baseline overhead if you're already running Cilium CNI.

**The decision tree.** North-south only → Ingress or Gateway. East-west L7 / uniform mTLS → mesh. Both → Gateway API for north-south, mesh for east-west; GAMMA unifies the spec language.

**TLS lives in many places.** Cloud LB (passthrough or terminate), Gateway (most common edge), Pod / sidecar (end-to-end mTLS). Production usually does edge TLS via cert-manager + Let's Encrypt + Gateway, plus internal mTLS via the mesh's CA — two cert systems, both automated.

**Rate limiting** is local (in-memory per-Envoy, no latency, scales with replicas) or global (RLS over gRPC, per-request network call, globally consistent). Pick by whether you need cross-replica consistency.

**Locality LB** prefers same-zone backends via Envoy's `priority` + `overprovisioning_factor`. Combine with Service topology hints for full-stack zone affinity. Saves cross-AZ data transfer dollars at scale.

**Multi-cluster mesh** via shared root CA across cluster control planes (Istio multi-primary), Link CRD (Linkerd), or ClusterMesh (Cilium). All approaches preserve SPIFFE identity across clusters.

**Observability is the side benefit of L7 parsing.** RED metrics per route, structured access logs (response_flags codes), distributed traces (B3/W3C context propagation), live admin endpoints (Envoy `/clusters`, `/config_dump`, `/stats`).

**WebAssembly extensions** load into Envoy at runtime via proxy-wasm. Custom filters in Rust/Go without rebuilding Envoy. Used for custom auth, redaction, rate limiting; tradeoff is sandbox overhead.

**GAMMA** extends Gateway API to mesh routing: HTTPRoute can attach to a Service (parentRef kind: Service) and the mesh data plane implements it. One spec, two attachment points, three mesh implementations converging.

**The cost of a mesh is real.** Sidecars: 50-100 MiB and 5-15 mCPU per pod, +1-2 ms per request per direction. Ambient: dramatic reduction (30x less memory, half the latency for L4-only). At 1000 pods this is the deciding factor.

**Pitfalls that bite.** STRICT mTLS flipped too early, missing Sidecar resource at scale, HTTP-01 ACME blocked by NetworkPolicy, retries layered on retries, downstream timeout shorter than upstream, two default IngressClasses, xDS partial-update race, mesh upgrade skipping minors. All preventable; all common.

**The single sentence.** *L7 routing is the layer where the application protocol matters; Ingress is the old API, Gateway API is the new role-oriented spec, Envoy is the engine, xDS is the wire format, and a service mesh is whatever subset of those you point at east-west traffic — usually Istio (sidecar or ambient), Linkerd, or Cilium mesh — to get uniform mTLS, observability, and traffic management without changing your application code.*
