# API Aggregation and Extension API Servers: A Staff-Level Deep Dive

A staff-engineer reference for the *other* extension path. Chapter 23 taught you CRDs — declare a schema, the apiserver stores your objects in etcd, generic handlers serve them. This chapter teaches you the aggregation layer — register a *second* apiserver behind the first, route all traffic for some `GroupVersion` to it, and own everything: storage backend, validation, conversion, subresources, watch implementation. CRDs are easy and 95% of extension goes through them. Aggregation is hard and is reserved for the 5% where easy is impossible.

The canonical exhibit lives one process away from every cluster you have ever used: `metrics-server`. It serves `metrics.k8s.io/v1beta1` with two resources — `NodeMetrics` and `PodMetrics` — backed by an in-memory scrape of every kubelet's `/metrics/resource` endpoint. There is no etcd. There is no schema declaration. There is a deployment, a service, a single `APIService` object that tells the main apiserver "for `metrics.k8s.io/v1beta1`, ask *that* service", and a small mountain of Go code implementing the apiserver framework. HPA on CPU and memory calls `metrics.k8s.io` directly. Take metrics-server out and the autoscaler that runs every production cluster on earth goes blind.

This chapter sits on chapter 05 (the main apiserver, where the kube-aggregator handler lives), chapter 07 (RequestHeader authentication and the auth-delegation pattern), and chapter 23 (the comparison point — CRDs as the path of least resistance). It feeds chapter 30 (metrics-server is the most-deployed aggregated apiserver) and chapter 34 (custom schedulers sometimes pair with aggregated APIs for batch metrics). If chapter 23 made you say *"I can extend Kubernetes!"*, this chapter is the one that makes you say *"I can extend Kubernetes — and I really, really should have used a CRD."*

---

## Table of Contents

1. [The Problem Aggregation Solves](#1-the-problem-aggregation-solves)
2. [CRD vs Aggregated API Server: The Two Paths](#2-crd-vs-aggregated-api-server-the-two-paths)
3. [The `APIService` Resource](#3-the-apiservice-resource)
4. [The kube-aggregator Proxy: Request Routing](#4-the-kube-aggregator-proxy-request-routing)
5. [Identity Propagation: RequestHeader Authentication](#5-identity-propagation-requestheader-authentication)
6. [Why the Middleman Pattern Is Secure](#6-why-the-middleman-pattern-is-secure)
7. [Auth Delegation: TokenReview and SubjectAccessReview](#7-auth-delegation-tokenreview-and-subjectaccessreview)
8. [Building an Extension Apiserver: The `k8s.io/apiserver` Library](#8-building-an-extension-apiserver-the-k8sioapiserver-library)
9. [`RecommendedConfig`: The Generic Apiserver Skeleton](#9-recommendedconfig-the-generic-apiserver-skeleton)
10. [Registry and Strategy for Custom Resources](#10-registry-and-strategy-for-custom-resources)
11. [Storage Backend Options](#11-storage-backend-options)
12. [metrics-server: The Canonical Aggregated API](#12-metrics-server-the-canonical-aggregated-api)
13. [custom-metrics-apiserver and external-metrics-apiserver](#13-custom-metrics-apiserver-and-external-metrics-apiserver)
14. [The Service Backing: Deployment, Service, HA](#14-the-service-backing-deployment-service-ha)
15. [CA Bundle Management](#15-ca-bundle-management)
16. [The Downsides: What CRDs Hide From You](#16-the-downsides-what-crds-hide-from-you)
17. [Aggregator Request Flow: End-to-End Trace](#17-aggregator-request-flow-end-to-end-trace)
18. [The `Available` Condition and `FailedDiscoveryCheck`](#18-the-available-condition-and-faileddiscoverycheck)
19. [Versions and Priorities: GroupPriorityMinimum, VersionPriority](#19-versions-and-priorities-grouppriorityminimum-versionpriority)
20. [apiserver-runtime: The Kubebuilder of Aggregated APIs](#20-apiserver-runtime-the-kubebuilder-of-aggregated-apis)
21. [Real-World Aggregated APIs](#21-real-world-aggregated-apis)
22. [When to Choose Aggregation](#22-when-to-choose-aggregation)
23. [When to Choose CRD](#23-when-to-choose-crd)
24. [Observability of the Aggregation Layer](#24-observability-of-the-aggregation-layer)
25. [Pitfalls: The Long List](#25-pitfalls-the-long-list)
26. [TL;DR](#26-tldr)

---

## 1. The Problem Aggregation Solves

CRDs (chapter 23) feel like magic the first time you write one. Twenty lines of YAML, a `kubectl apply`, and suddenly the apiserver speaks your API. Watch works. RBAC works. `kubectl explain` works. `kubectl edit` works. OpenAPI discovery works. The aggregator gives you none of that for free, and you must reimplement a substantial fraction of the generic apiserver yourself. So the only honest motivation for aggregation is a list of things CRDs *cannot* do — and that list is short, sharp, and specific.

### 1.1 The wall CRDs hit

There are exactly five categories where CRDs run out of runway.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    WHERE CRDs FALL OFF THE CLIFF                          │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  1. Large objects                                                        │
│     etcd has a per-object size limit (default 1.5 MiB; --max-request-    │
│     bytes can raise it to ~10 MiB but you should not). Some workloads    │
│     produce naturally large objects:                                     │
│       - ML model metadata with embedded hyperparameter sweeps            │
│       - workflow definitions (Argo, Tekton) with large step graphs       │
│       - cost/billing reports with line-item arrays                       │
│     A CRD forces you to either chunk into many objects (synchronization │
│     nightmare) or accept that some writes will fail at the etcd layer.   │
│                                                                          │
│  2. Non-etcd storage                                                     │
│     The data already lives somewhere — a Postgres database, a            │
│     time-series store, a remote service, an external CMDB. CRDs force    │
│     you to copy it into etcd or write a controller that bidirectionally  │
│     syncs (the "two databases, one truth" problem). Aggregation lets     │
│     you serve the API directly from the source of truth.                 │
│                                                                          │
│  3. Dynamic / runtime-determined schemas                                 │
│     metrics.k8s.io's PodMetrics has a `containers` array whose entries   │
│     are computed at request time from a live scrape. There is no         │
│     "stored object" to validate against a fixed schema. The response is  │
│     synthesized per request.                                             │
│                                                                          │
│  4. Subresources with custom semantics                                   │
│     CRDs support `status` and `scale` subresources only. Built-ins have  │
│     `exec`, `attach`, `portforward`, `proxy`, `log`, `binding`. If you   │
│     need a streaming long-lived subresource (e.g. a `console` endpoint   │
│     that opens a websocket to a remote system), CRDs cannot express it.  │
│                                                                          │
│  5. Custom validation beyond CEL                                         │
│     CEL (since 1.25) is powerful but bounded: no arbitrary external      │
│     calls, no cross-object lookups (besides authorizer), no long-running │
│     computation, cost-bounded. If your validation needs to call out to   │
│     an external policy engine, do graph traversal, or run a model, you   │
│     are reaching for either an admission webhook or a full apiserver.   │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

There is a sixth case — *replacing* a built-in type's storage — which is the only legitimate use of `GroupPriorityMinimum` to *hide* an upstream API. This is dangerous, almost always a mistake, and is mostly the province of distributions like OpenShift that override `oapi` group resources. We will discuss it in §19 because it exists, not because most people should do it.

### 1.2 Why "build a controller that syncs" is usually not enough

The standard rebuttal to *"CRDs can't store my external data"* is *"write a controller that mirrors the external store into a CRD"*. Sometimes this works. Often it does not.

- **Liveness.** The CRD lags the source of truth by the controller's resync interval. For metrics that change every 15 seconds, you cannot resync into etcd at 15 seconds without crushing etcd. Metrics-server's in-memory scrape is the only viable design.
- **Cardinality.** If the external store has 10 million records and your CRD would have to mirror all of them, etcd dies. The watch cache dies. The informer cache in every controller dies. You need a paginated API backed by something that already paginates.
- **Authority.** If the source of truth is canonical (your billing database is the source of truth for cost data), mirroring creates two writable surfaces. Users edit the CRD, the controller overwrites, users edit again — the classic "spec vs reality" tug-of-war that GitOps engines amplify (ch 31). Aggregation makes the API a read-through (or write-through) facade.
- **Subresource semantics.** A controller cannot make a CRD answer a `/proxy` request with streamed bytes from a remote endpoint. Only an apiserver can.

### 1.3 The thing aggregation actually gives you

The aggregation layer is a *URL routing rule* with *credential propagation*. The main apiserver receives a request, looks up its `GroupVersion`, finds an `APIService` registered to handle it, and forwards the HTTP request — with the original user's identity stamped into trusted headers — to a Service inside the cluster. That Service points to *your* apiserver, which speaks the same wire format (HTTP, JSON or protobuf, watch via chunked-transfer), implements its own storage, and is otherwise a peer of `kube-apiserver` from the client's point of view.

```
                        ┌─────────────────────────────────┐
                        │   CLIENTS (kubectl, controllers)│
                        └────────────────┬────────────────┘
                                         │ TLS
                                         ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  kube-apiserver  (the main one, sometimes called the "core" apiserver)   │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  kube-aggregator handler chain                                     │  │
│  │  AuthN → AuthZ → (route by GroupVersion) → handler                 │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                              │                                            │
│                              ▼                                            │
│         ┌────────────────────┴────────────────────────┐                  │
│         │                                              │                  │
│   "is this group/version a local one?"        "is it registered as       │
│   (built-ins, CRDs)                            an APIService?"            │
│         │                                              │                  │
│         ▼                                              ▼                  │
│   ┌──────────────┐                          ┌────────────────────┐       │
│   │ local REST   │                          │ proxy to backing   │       │
│   │ storage      │                          │ Service (kube-     │       │
│   │ (etcd)       │                          │  aggregator stamps │       │
│   └──────────────┘                          │  X-Remote-* hdrs)  │       │
│                                              └─────────┬──────────┘       │
└────────────────────────────────────────────────────────┼──────────────────┘
                                                         │
                                                         ▼
                                     ┌──────────────────────────────────┐
                                     │ Service (ClusterIP)              │
                                     │ namespace/name from APIService   │
                                     └─────────────┬────────────────────┘
                                                   │
                                                   ▼
                                     ┌──────────────────────────────────┐
                                     │ EXTENSION APISERVER (your code)  │
                                     │                                  │
                                     │ ┌──────────────────────────────┐ │
                                     │ │ Trust X-Remote-User iff      │ │
                                     │ │   client cert signed by      │ │
                                     │ │   the configured CA          │ │
                                     │ │   (RequestHeader pattern)    │ │
                                     │ └──────────────────────────────┘ │
                                     │                                  │
                                     │ ┌──────────────────────────────┐ │
                                     │ │ Delegate AuthZ via           │ │
                                     │ │   SubjectAccessReview        │ │
                                     │ │   on the main apiserver      │ │
                                     │ └──────────────────────────────┘ │
                                     │                                  │
                                     │ ┌──────────────────────────────┐ │
                                     │ │ Storage = whatever you want  │ │
                                     │ │   (etcd, Postgres, memory,   │ │
                                     │ │   remote service, …)         │ │
                                     │ └──────────────────────────────┘ │
                                     └──────────────────────────────────┘
```

Every later section drills into one box of this picture.

---

## 2. CRD vs Aggregated API Server: The Two Paths

The decision is binary and rarely subtle once you know the questions. The diagram makes it explicit; the table makes it exhaustive; the prose explains the corners.

### 2.1 The decision tree

```
┌──────────────────────────────────────────────────────────────────────────┐
│                  CRD or AGGREGATED APISERVER?                             │
└──────────────────────────────────────────────────────────────────────────┘

  Is the data going to live in etcd anyway?
  ├── YES ──► Are the objects under ~1 MiB and bounded in count?
  │           ├── YES ──► Are status/scale the only subresources you need?
  │           │           ├── YES ──► Is CEL+webhook validation sufficient?
  │           │           │           ├── YES ──► **CRD.** Done.
  │           │           │           └── NO ───► CRD + admission webhook
  │           │           │                       (still simpler than agg.)
  │           │           └── NO ───► You need exec/attach/proxy-style
  │           │                       subresources → AGGREGATION
  │           └── NO ───► Objects >1 MiB or count >100k → AGGREGATION
  │                       (or rethink the API — usually too coarse)
  └── NO ───► Source of truth lives elsewhere
              (Postgres, time-series, remote service, billing system)
              └─────────► AGGREGATION (façade pattern)
                          metrics-server is the textbook case
```

### 2.2 Side-by-side

| Dimension | CRD | Aggregated APIServer |
|---|---|---|
| **Definition** | YAML object (`CustomResourceDefinition`) | Go program implementing the apiserver framework |
| **Storage** | etcd (the cluster's etcd, fixed) | Anything (etcd, SQL, in-memory, remote) |
| **Per-object size cap** | etcd's `--max-request-bytes` (default 1.5 MiB) | Whatever your backend allows |
| **Schema validation** | OpenAPI v3 + CEL `x-kubernetes-validations` | Your code (Validate, ValidateUpdate) |
| **Conversion** | Webhook (out-of-tree HTTPS endpoint) | In-process (Scheme + conversion funcs) |
| **Subresources** | `status`, `scale` only | Arbitrary (exec, proxy, log, binding…) |
| **Watch implementation** | Free — apiextensions reuses the watch cache | You implement it (storage.Interface) |
| **List pagination** | Free | You implement it (continue tokens) |
| **OpenAPI discovery** | Generated from CRD schema | You generate it (k8s.io/kube-openapi) |
| **`kubectl explain`** | Works automatically | Works only if you publish OpenAPI properly |
| **Server-side apply** | Works (since 1.18 if `x-kubernetes-list-type` is set) | You implement merge logic (or use library helpers) |
| **Admission chain** | Full chain runs (mutating, validating, CEL VAP) | Your chain (library provides hooks) |
| **Audit logging** | Main apiserver audits | Your apiserver audits (library hook) |
| **RBAC** | Free (Kubernetes RBAC) | Free *if* you delegate AuthZ |
| **HA** | Free (apiserver is HA) | Your problem (Deployment with replicas) |
| **Uptime** | Tied to main apiserver | Tied to your Deployment's uptime |
| **Rolling upgrade** | None needed | Rolling your Deployment temporarily fails requests |
| **Operational cost** | Low (one CRD object) | High (Deployment + Service + APIService + RBAC + CA) |
| **Boilerplate Go code** | Zero | ~2000–5000 LoC for a real apiserver |
| **Effort to start** | Hours | Days to weeks |
| **Right answer 95% of the time** | Yes | No |

### 2.3 The corners

There are three places the decision is not as clean as the table suggests.

**Hybrid.** Some projects use both: a CRD for the user-visible spec and an aggregated apiserver for read-only views/metrics on top. KEDA does this — its `ScaledObject` is a CRD, but its metrics adapter is an aggregated apiserver exposing `external.metrics.k8s.io` for HPA to consume. The CRD is the *configuration*; the aggregated apiserver is the *query surface*.

**Aggregator-served CRDs.** Not really a corner, but a confusion: every CRD is *served by the main apiserver*, not by an aggregated apiserver. The "aggregation layer" routes a `GroupVersion` to a *registered service*. CRDs live in the main apiserver's local handler, which is *also* served via the aggregator chain (registered as a special local APIService for `apiextensions.k8s.io`). So technically every request goes through the aggregator; only some of them get forwarded.

**Replacing a built-in.** OpenShift's `oapi.openshift.io` is *not* this — it is just a new group. *Actually* shadowing `apps/v1` Deployment is theoretically possible by registering an APIService with higher `GroupPriorityMinimum` than the built-in, but the main apiserver special-cases its own groups and refuses. We will not see this in production.

---

## 3. The `APIService` Resource

The `APIService` is the only on-cluster object the aggregation layer needs. It belongs to the `apiregistration.k8s.io/v1` group and is named `<version>.<group>` (e.g. `v1beta1.metrics.k8s.io`). One per `GroupVersion`. There is no "APIGroup" object — each version is its own APIService.

### 3.1 The full object

```yaml
apiVersion: apiregistration.k8s.io/v1
kind: APIService
metadata:
  name: v1beta1.metrics.k8s.io      # MUST be <version>.<group>
  labels:
    kube-aggregator.kubernetes.io/automanaged: "false"
  annotations:
    # cert-manager will inject ca.crt into spec.caBundle when this annotation is set
    cert-manager.io/inject-ca-from: kube-system/metrics-server-tls
spec:
  group: metrics.k8s.io             # the API group this APIService serves
  version: v1beta1                  # the version within that group
  groupPriorityMinimum: 100         # ordering across groups (higher wins)
  versionPriority: 100              # ordering within a group's versions
  service:
    namespace: kube-system          # where the backing Service lives
    name: metrics-server            # ClusterIP Service name
    port: 443                       # Service port (defaults to 443)
  caBundle: LS0tLS1CRUdJTi...       # PEM CA used to verify the Service's TLS cert
  insecureSkipTLSVerify: false      # NEVER true in production
status:
  conditions:
  - type: Available
    status: "True"
    reason: Passed
    message: "all checks passed"
    lastTransitionTime: "2026-05-23T08:15:42Z"
```

### 3.2 Field semantics, one by one

**`metadata.name`** — must be exactly `<spec.version>.<spec.group>`. The aggregator uses the name as a fast index from `GroupVersion` to `APIService` and rejects malformed names. Hyphens in the version part are fine (`v1alpha1`), dots only as separator.

**`spec.group`** — the API group. Empty string is the core group, but registering an APIService for the core group is forbidden; the main apiserver owns it.

**`spec.version`** — single version. If you want `v1` and `v1alpha1` both served by the same backend, you register *two* APIServices (with `versionPriority` controlling preference).

**`spec.groupPriorityMinimum`** — integer, higher = preferred. Determines order in discovery responses (`kubectl api-resources`), which matters because some tools use the *first* version listed. Conventional values:
- Built-in groups: `17000`–`18000` range (apps=17800, batch=17600, etc.)
- Stable third-party: `1000`–`9000`
- Beta/alpha experimental: `100`–`200`
- metrics-server: `100`

**`spec.versionPriority`** — integer, higher = preferred among versions of the same group. If `v1` has higher `versionPriority` than `v1beta1` of the same group, `v1` is the "storage version" client tools default to.

**`spec.service.namespace` / `name` / `port`** — pointer to a Service. Note: this is a Service *object* reference, not a Service IP. The aggregator resolves it through the apiserver's own cache, then dials the Service's `ClusterIP:port`. The Service must be of type `ClusterIP` (or `LoadBalancer` exposing internally, but `ClusterIP` is the norm). `port` defaults to 443. The Service's `targetPort` can be anything (typically 4443 to avoid root binding).

**`spec.caBundle`** — PEM-encoded CA certificate(s) the aggregator will trust when verifying the Service's TLS cert. Base64-encoded in YAML because it's a `[]byte` field. If empty, the aggregator falls back to the system root pool (almost never correct inside a cluster). cert-manager and the kube-controller-manager's `RootCACertConfigMap` controller manage this for you in modern clusters — more in §15.

**`spec.insecureSkipTLSVerify`** — skips cert verification of the backing Service. **Never set this to `true` in production.** It exists for the bootstrap edge case where the CA is not yet known. Even kind clusters and minikube ship with `false`.

**`status.conditions[Available]`** — driven by a controller in `kube-aggregator` that probes the backend periodically. Two reasons can flip it `False`:
- `FailedDiscoveryCheck` — the aggregator tried to fetch `/apis/<group>/<version>` from the backend and got an error (network, TLS, 5xx, slow).
- `ServiceNotFound` / `EndpointsNotFound` — no Service or no Endpoints behind it.

If `Available=False`, clients hitting that `GroupVersion` get `503 Service Unavailable` from the aggregator.

### 3.3 Local APIServices

There are a handful of *local* APIServices that have no `spec.service`:

```yaml
apiVersion: apiregistration.k8s.io/v1
kind: APIService
metadata:
  name: v1.apps
spec:
  group: apps
  version: v1
  groupPriorityMinimum: 17800
  versionPriority: 15
```

These are auto-created by the apiserver itself for every built-in API group and version. They tell the aggregator "this `GroupVersion` is local — handle it with the in-process registry, do not forward". You will see them in `kubectl get apiservice` output; do not touch them.

CRDs register *one* local APIService per group/version when the CRD is created: `kubectl get apiservice v1.cert-manager.io` shows up as soon as a Certificate CRD lands. It is local (no `spec.service`), so the aggregator routes to the apiextensions handler instead of forwarding.

---

## 4. The kube-aggregator Proxy: Request Routing

The aggregator is *inside* `kube-apiserver`. There is no separate process. The name "aggregation layer" is shorthand for a chain of handlers in `staging/src/k8s.io/kube-aggregator/pkg/apiserver/` that wrap the rest of the apiserver.

### 4.1 The handler chain

```
incoming HTTPS request
        │
        ▼
┌──────────────────────────────────┐
│ generic-apiserver outer wrappers │
│   timeout, panic recovery,       │
│   audit, max-in-flight (APF)     │
└────────────┬─────────────────────┘
             │
             ▼
┌──────────────────────────────────┐
│ AuthN  (chain: cert, OIDC,       │
│         bootstrap, SA token,     │
│         webhook)                 │
└────────────┬─────────────────────┘
             │
             ▼
┌──────────────────────────────────┐
│ AuthZ  (RBAC, Node, ABAC,        │
│         webhook)                 │
│                                  │
│ NB: at this point, the request   │
│   is *authorized for the main    │
│   apiserver's view of RBAC*. The │
│   extension apiserver will       │
│   reauthorize via SAR (see §7).  │
└────────────┬─────────────────────┘
             │
             ▼
┌──────────────────────────────────┐
│ Mutating admission (only for     │
│   write verbs; the aggregator    │
│   may skip these for proxied     │
│   requests — depends on whether  │
│   the GV is local or remote)     │
└────────────┬─────────────────────┘
             │
             ▼
┌──────────────────────────────────┐
│ AGGREGATOR ROUTER                │
│                                  │
│  match request URL to GVR:       │
│   /apis/<group>/<version>/...    │
│                                  │
│  Lookup APIService for GV.       │
│   - If local: send to local      │
│     handler (built-ins, CRDs).   │
│   - If remote: send to proxy.    │
│   - If no APIService: 404.       │
└──────┬──────────────────┬────────┘
       │                  │
       ▼                  ▼
  local handler      proxy handler
       │                  │
       │                  ▼
       │           ┌─────────────────┐
       │           │ stamp X-Remote-*│
       │           │ headers         │
       │           ├─────────────────┤
       │           │ dial backing    │
       │           │ Service over    │
       │           │ TLS (verifying  │
       │           │ caBundle)       │
       │           ├─────────────────┤
       │           │ stream request  │
       │           │ body & response │
       │           │ (proxies WATCH  │
       │           │ via chunked)    │
       │           └─────────────────┘
       │                  │
       ▼                  ▼
   etcd / CRD store   extension apiserver
```

### 4.2 The router code

The actual dispatch lives in `staging/src/k8s.io/kube-aggregator/pkg/apiserver/apiserver.go` and `handler_proxy.go`. The decision is approximately:

```go
// pseudo-code distilled from kube-aggregator/pkg/apiserver/handler_proxy.go
func (r *proxyHandler) ServeHTTP(w http.ResponseWriter, req *http.Request) {
    // 1. Extract user from context (set by upstream AuthN).
    user, ok := genericapirequest.UserFrom(req.Context())
    if !ok {
        responsewriters.InternalError(w, req, errors.New("missing user"))
        return
    }

    // 2. Load the current proxy state (cached APIService + endpoint).
    value := r.handlingInfo.Load()
    if value == nil {
        proxyError(w, req, "", http.StatusNotFound)
        return
    }
    handlingInfo := value.(proxyHandlingInfo)

    // 3. Service resolution — turn (namespace, name, port) into an IP.
    location, transport, err := r.serviceResolver.ResolveEndpoint(
        handlingInfo.serviceNamespace,
        handlingInfo.serviceName,
        handlingInfo.servicePort,
    )
    if err != nil {
        proxyError(w, req, err.Error(), http.StatusServiceUnavailable)
        return
    }

    // 4. Rewrite the request URL to point at the backend.
    newReq := req.Clone(req.Context())
    newReq.URL.Scheme = "https"
    newReq.URL.Host  = location.Host
    newReq.Host       = location.Host

    // 5. Stamp the RequestHeader auth headers (this is the trust handoff).
    proxyRoundTripper := transport.NewAuthProxyRoundTripper(
        user.GetName(),     // X-Remote-User
        user.GetUID(),      // X-Remote-Uid (since 1.30)
        user.GetGroups(),   // X-Remote-Group (one per group)
        user.GetExtra(),    // X-Remote-Extra-<key>
        handlingInfo.proxyRoundTripper,
    )

    // 6. Stream the request and the response (handles watch, upgrade, etc.).
    handler := proxy.NewUpgradeAwareHandler(
        &location, proxyRoundTripper, /*wrapTransport=*/true,
        /*upgradeRequired=*/false, &responder{w: w},
    )
    handler.ServeHTTP(w, newReq)
}
```

The five things to notice:

1. **The user object is the contract.** Whatever AuthN decided is forwarded; the proxy does not re-authenticate.
2. **The Service resolver is pluggable.** Default is "ClusterIP from cache"; in some setups (kind, deployment-without-kube-proxy) it resolves through `EndpointSlices` directly.
3. **`AuthProxyRoundTripper` does the header injection.** It wraps the underlying transport, sets `X-Remote-*` on every request, and uses *the aggregator's own client cert* — signed by `--requestheader-client-ca-file` — when dialing the backend.
4. **`NewUpgradeAwareHandler`** handles WebSocket / SPDY upgrades (for `exec`-style subresources) and bidirectional streaming.
5. **No body re-encoding.** The aggregator does not parse JSON/protobuf bodies; it streams bytes. This is critical for `watch` (chunked transfer-encoded never-ending stream).

### 4.3 Service resolution

`ServiceResolver` is an interface in `staging/src/k8s.io/apiserver/pkg/util/webhook/serviceresolver.go`:

```go
type ServiceResolver interface {
    ResolveEndpoint(namespace, name string, port int32) (*url.URL, error)
}
```

Two implementations:

- **`aggregatorRouting`**: looks up the Service in the apiserver's informer cache, returns `https://<clusterIP>:<port>`. The default for in-cluster operation.
- **`endpointsRouting`**: looks up `EndpointSlice` directly and picks a backend pod IP. Used in tests and unusual setups; bypasses kube-proxy.

Modern clusters use `aggregatorRouting` and rely on kube-proxy (or the eBPF service implementation) for actual load balancing across the extension apiserver's pod replicas.

---

## 5. Identity Propagation: RequestHeader Authentication

This is the part where most aggregated apiservers go wrong, and the part that, once you understand, makes the security model fall into place. The problem: the *main* apiserver authenticates the user. The *extension* apiserver needs to know who the user is so it can authorize and audit them. How does the user identity cross the trust boundary?

Three things could happen, and only one is safe.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  THREE WAYS USER IDENTITY COULD REACH AN EXTENSION APISERVER              │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Option A: Pass through the original bearer token / cert.                │
│    The aggregator forwards the user's Authorization header verbatim.     │
│    Problem: Now the extension apiserver must know how to validate every  │
│    token type the main apiserver supports (OIDC issuers, SA token        │
│    signing keys, webhook AuthN endpoints, x509 CAs). It also gets the    │
│    user's raw credential, which is a privilege escalation surface.       │
│                                                                          │
│  Option B: Re-authenticate against the main apiserver.                   │
│    The extension apiserver calls TokenReview on the main apiserver       │
│    with the token. This works (and we'll see it for SA tokens in §7),   │
│    but it requires an extra RPC per request and doesn't work for         │
│    x509 client certs at all (the main apiserver can't "review" them).   │
│                                                                          │
│  Option C: RequestHeader pattern  ◄── this is what Kubernetes does       │
│    The main apiserver authenticates the user, then injects headers      │
│    naming the user, groups, and extras. The extension apiserver trusts  │
│    those headers ONLY when the connection is mutually authenticated      │
│    with a client cert signed by a specific CA. No credentials cross      │
│    the boundary; only the *identity claim*.                              │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 5.1 The five headers

The aggregator injects (default names, configurable):

| Header | Source | Semantics |
|---|---|---|
| `X-Remote-User` | `user.Info.GetName()` | Username string |
| `X-Remote-Uid` | `user.Info.GetUID()` (since 1.30) | Stable user identifier |
| `X-Remote-Group` | `user.Info.GetGroups()` | One header per group |
| `X-Remote-Extra-<key>` | `user.Info.GetExtra()` | Provider-supplied attributes |
| `Authorization` | *stripped* | The original credential is removed |

Critical: the aggregator **strips the original `Authorization` header**. The extension apiserver never sees the user's raw token. This is the property that makes the model safe.

### 5.2 The CA configuration

The main apiserver runs with three flags:

```
--requestheader-client-ca-file=/etc/kubernetes/pki/front-proxy-ca.crt
--requestheader-allowed-names=front-proxy-client
--requestheader-username-headers=X-Remote-User
--requestheader-group-headers=X-Remote-Group
--requestheader-extra-headers-prefix=X-Remote-Extra-
--proxy-client-cert-file=/etc/kubernetes/pki/front-proxy-client.crt
--proxy-client-key-file=/etc/kubernetes/pki/front-proxy-client.key
```

`--proxy-client-cert-file` / `--proxy-client-key-file` is the cert the main apiserver presents when dialing the extension apiserver (the round-tripper from §4.2 uses these).

`--requestheader-*` flags are what the main apiserver writes into a well-known ConfigMap, `kube-system/extension-apiserver-authentication`, so that *extension* apiservers can read the policy. They contain:

- The CA bundle the extension apiserver should use to verify *incoming* client certs (`requestheader-client-ca-file`).
- The list of common names allowed in those certs (`requestheader-allowed-names`).
- The exact header names the extension apiserver should look for.

### 5.3 The ConfigMap

This is the configuration handoff from main → extension:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: extension-apiserver-authentication
  namespace: kube-system
data:
  client-ca-file: |
    -----BEGIN CERTIFICATE-----
    MIIDBzCCAe+gAwIBAgIIM/jq...
    -----END CERTIFICATE-----
  requestheader-allowed-names: '["front-proxy-client"]'
  requestheader-client-ca-file: |
    -----BEGIN CERTIFICATE-----
    MIIDBzCCAe+gAwIBAgIIK7zPq...
    -----END CERTIFICATE-----
  requestheader-extra-headers-prefix: '["X-Remote-Extra-"]'
  requestheader-group-headers: '["X-Remote-Group"]'
  requestheader-username-headers: '["X-Remote-User"]'
```

The extension apiserver's RBAC must allow it to read this ConfigMap:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: my-extension-apiserver-auth-reader
  namespace: kube-system
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: extension-apiserver-authentication-reader
subjects:
- kind: ServiceAccount
  name: my-extension-apiserver
  namespace: my-extension-namespace
```

The Role `extension-apiserver-authentication-reader` is pre-created by the main apiserver in `kube-system`; you only need the RoleBinding.

### 5.4 The trust chain

```
        ┌────────────────────────────────────────────────────────────┐
        │ STEP 1. Main apiserver starts.                              │
        │  - Generates / loads a front-proxy CA.                      │
        │  - Generates / loads a front-proxy client cert signed by it.│
        │  - Publishes CA bundle + allowed names into the ConfigMap.  │
        └────────────────────────────────────────────────────────────┘
                            │
                            ▼
        ┌────────────────────────────────────────────────────────────┐
        │ STEP 2. Extension apiserver starts.                         │
        │  - Reads kube-system/extension-apiserver-authentication.    │
        │  - Trusts X-Remote-* iff the TLS peer cert is signed by     │
        │    the CA in that ConfigMap AND its CN is in allowed-names. │
        └────────────────────────────────────────────────────────────┘
                            │
                            ▼
        ┌────────────────────────────────────────────────────────────┐
        │ STEP 3. User makes a request.                               │
        │  - Main apiserver authenticates (e.g. OIDC → user "alice"). │
        │  - Main apiserver authorizes (RBAC: alice can read pods).   │
        │  - Aggregator routes to ext apiserver via APIService.       │
        │  - Aggregator dials with front-proxy-client cert.           │
        │  - Aggregator stamps X-Remote-User=alice etc.               │
        └────────────────────────────────────────────────────────────┘
                            │
                            ▼
        ┌────────────────────────────────────────────────────────────┐
        │ STEP 4. Extension apiserver receives request.               │
        │  - Verifies TLS client cert against the front-proxy CA.     │
        │  - Verifies CN ∈ allowed-names. If not → IGNORE headers,    │
        │    fall back to anonymous (or fail).                        │
        │  - Reads X-Remote-User → user.Info{Name:"alice", ...}.      │
        │  - Hands off to AuthZ (SAR back to main apiserver — §7).    │
        └────────────────────────────────────────────────────────────┘
```

If you remember one diagram from this chapter, this is the one. Every aggregated apiserver security incident is some piece of this chain breaking or being misconfigured.

---

## 6. Why the Middleman Pattern Is Secure

The intuition that takes new operators a beat to internalize: *the extension apiserver does not have to trust the user*. It only has to trust the main apiserver. The main apiserver is the only entity whose client cert is signed by the front-proxy CA. Therefore, the only entity that can put arbitrary text in `X-Remote-User` and have the extension apiserver believe it is the main apiserver itself.

### 6.1 What forges look like and why they fail

Consider an attacker who has a pod with network access to the extension apiserver. They could:

```bash
# Attempt 1: send a request directly with a forged header
curl -k https://my-extension-apiserver.my-namespace.svc:443/apis/example.com/v1/foos \
     -H "X-Remote-User: cluster-admin" \
     -H "X-Remote-Group: system:masters"

# Result: TLS handshake succeeds (no client cert presented, server allows anonymous),
# but the apiserver sees no client cert in the chain signed by the front-proxy CA,
# so it strips the X-Remote-* headers and treats this as the anonymous user.
# AuthZ then rejects unless the API allows anonymous access (which you should never do).
```

```bash
# Attempt 2: present a self-signed client cert with CN=front-proxy-client
curl -k --cert evil.crt --key evil.key \
     https://my-extension-apiserver.my-namespace.svc:443/apis/example.com/v1/foos \
     -H "X-Remote-User: cluster-admin"

# Result: TLS handshake — the apiserver tries to verify the client cert against
# the front-proxy CA from the ConfigMap. The cert was self-signed (or signed by
# some other CA), so verification fails. The connection is terminated OR the cert
# is treated as untrusted (depends on tls-cert-allow-untrusted). Either way the
# X-Remote-* headers are stripped.
```

The security property is: **the front-proxy CA is the gating credential**. As long as the front-proxy CA's private key is not compromised, no attacker can forge identity headers.

### 6.2 What "trust the headers" means in code

The actual check lives in `staging/src/k8s.io/apiserver/pkg/authentication/request/headerrequest/requestheader.go`:

```go
// pseudo-code
func (a *requestHeaderAuthRequestHandler) AuthenticateRequest(req *http.Request) (*Response, bool, error) {
    // Step 1: was the connection made by a trusted client (verified TLS)?
    peerCert := req.TLS.PeerCertificates[0]
    if !a.verifier.VerifyClientCert(peerCert) {
        return nil, false, nil  // Not a trusted source — IGNORE headers.
    }
    if !sliceContains(a.allowedNames, peerCert.Subject.CommonName) {
        return nil, false, nil  // Wrong CN — IGNORE headers.
    }

    // Step 2: the connection is trusted; read headers.
    name := req.Header.Get(a.usernameHeader)
    if name == "" {
        return nil, false, nil
    }
    groups := req.Header.Values(a.groupHeaders)
    extra := extractExtra(req.Header, a.extraHeaderPrefixes)

    // Step 3: scrub headers so downstream handlers can't see them again.
    req.Header.Del(a.usernameHeader)
    for _, h := range a.groupHeaders {
        req.Header.Del(h)
    }

    return &Response{User: &user.DefaultInfo{Name: name, Groups: groups, Extra: extra}}, true, nil
}
```

Three properties:

- The check is *unconditional* — there is no escape hatch.
- The CA bundle and allowed names come from the `kube-system` ConfigMap at startup *and on refresh* (the library watches the ConfigMap so the extension apiserver picks up CA rotations without restart).
- After verification, the headers are deleted from the request so they cannot influence later handlers.

### 6.3 Failure modes worth naming

- **Misconfigured `--requestheader-allowed-names`.** If the empty list, the apiserver accepts *any* client cert signed by the front-proxy CA, which means a compromised aggregator → extension apiserver path. Always set this to an explicit list (`["front-proxy-client"]`).
- **Front-proxy CA shared with the cluster CA.** Some homegrown setups reuse the cluster CA as the front-proxy CA. Now *every* node cert is a front-proxy-client identity, and any node can forge identity. Always a separate CA.
- **Network exposure of the extension apiserver Service.** The Service should be `ClusterIP`, accepting traffic from the main apiserver only. If you expose it as `LoadBalancer` or `NodePort`, you have moved the trust boundary to the network firewall.
- **`insecureSkipTLSVerify: true` in the APIService.** The main apiserver will not verify the extension apiserver's serving cert, but the extension apiserver still verifies the main apiserver's client cert. The middleman pattern still works for *identity*, but a malicious pod with the Service's ClusterIP can present any serving cert and read user identity headers from the proxy (which it does not have, because it cannot sign with the front-proxy-client cert — so the actual confidentiality risk is data tampering of responses). Still: never set this.

---

## 7. Auth Delegation: TokenReview and SubjectAccessReview

The RequestHeader pattern only handles *identity*. AuthN of the user is done; what about AuthZ? The extension apiserver could implement its own RBAC, but then permissions would diverge from the cluster's permissions and users would be confused. The right answer is to **delegate authorization back to the main apiserver**.

### 7.1 The two delegated calls

```
┌──────────────────────────────────────────────────────────────────────────┐
│  WHAT DELEGATION LOOKS LIKE                                              │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  When a request arrives at the extension apiserver:                      │
│                                                                          │
│  Path A — request came via the aggregator with RequestHeader auth:       │
│     AuthN already done. Skip TokenReview.                                │
│                                                                          │
│  Path B — request came directly (not via aggregator, e.g. bypassing      │
│           the proxy with a token):                                       │
│     Extension apiserver calls TokenReview on the main apiserver to       │
│     validate the token.                                                  │
│                                                                          │
│  Then, for ALL paths:                                                    │
│     Extension apiserver calls SubjectAccessReview on the main apiserver  │
│     with (user, verb, resource) to ask "can this user do this?".         │
│     The main apiserver evaluates RBAC and returns yes/no.                │
└──────────────────────────────────────────────────────────────────────────┘
```

The library calls are:

```go
// authentication/v1.TokenReview
tr := authenticationv1.TokenReview{
    Spec: authenticationv1.TokenReviewSpec{
        Token: bearerToken,
        Audiences: []string{"my-extension-apiserver"},
    },
}
result, err := client.AuthenticationV1().TokenReviews().Create(ctx, &tr, metav1.CreateOptions{})
// result.Status.Authenticated bool, result.Status.User user.Info

// authorization/v1.SubjectAccessReview
sar := authorizationv1.SubjectAccessReview{
    Spec: authorizationv1.SubjectAccessReviewSpec{
        User:   "alice",
        Groups: []string{"ops"},
        ResourceAttributes: &authorizationv1.ResourceAttributes{
            Namespace: "default",
            Verb:      "get",
            Group:     "example.com",
            Version:   "v1",
            Resource:  "foos",
            Name:      "myfoo",
        },
    },
}
result, err := client.AuthorizationV1().SubjectAccessReviews().Create(ctx, &sar, metav1.CreateOptions{})
// result.Status.Allowed bool, result.Status.Reason string
```

### 7.2 The library does this for you

The `k8s.io/apiserver` library exposes both as plug-in authenticators/authorizers. From `staging/src/k8s.io/apiserver/pkg/server/options/authentication.go`:

```go
opts := genericoptions.NewDelegatingAuthenticationOptions()
opts.RemoteKubeConfigFileOptional = true  // for in-cluster
// applies the RequestHeader authenticator AND a TokenReview-backed authenticator
opts.ApplyTo(&recommendedConfig.Authentication, secureServing, openapiConfig)

authzOpts := genericoptions.NewDelegatingAuthorizationOptions()
authzOpts.RemoteKubeConfigFileOptional = true
// applies a SubjectAccessReview-backed authorizer with a small cache
authzOpts.ApplyTo(&recommendedConfig.Authorization)
```

That is the entire integration. You do not write the TokenReview or SAR clients yourself; you wire two options structs and the library calls them.

### 7.3 The cache

Per-request SAR calls would crush the main apiserver. The library caches results in two LRU caches:

```go
// authorization/cache/cached_authorizer.go (paraphrased)
type cachingAuthorizer struct {
    authorizer authorizer.Authorizer
    successCache *lru.Cache  // default size 1024, TTL 10s
    failureCache *lru.Cache  // default size 1024, TTL 10s
}
```

Cache key is the canonicalized `(user, verb, resource attributes)`. TTLs are short on purpose — RBAC changes need to take effect quickly. In a hot extension apiserver this cuts SAR calls by 95%+; in a cold one it costs ~10ms per request. Tunable via flags `--authorization-webhook-cache-authorized-ttl` and `--authorization-webhook-cache-unauthorized-ttl`.

### 7.4 The ServiceAccount the extension apiserver runs as

To call TokenReview and SAR, the extension apiserver needs a ServiceAccount with appropriate ClusterRoleBinding. The conventional name is `system:auth-delegator`:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: my-extension-apiserver:auth-delegator
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:auth-delegator
subjects:
- kind: ServiceAccount
  name: my-extension-apiserver
  namespace: my-extension-namespace
```

`system:auth-delegator` is a pre-existing ClusterRole that grants `create` on `tokenreviews.authentication.k8s.io` and `subjectaccessreviews.authorization.k8s.io`.

### 7.5 Audit logs

Two consequences of the delegation:

- The main apiserver's audit log shows the original user's `apis/metrics.k8s.io/v1beta1/pods/...` request — but does not contain the request *body* (because the aggregator streams). You see who, what GVR, but not what details.
- The extension apiserver should produce its *own* audit log for the per-resource details. The library provides this via `genericoptions.AuditOptions`.

In practice, most operators just look at the main apiserver's audit and ignore the extension apiserver's. This is fine for metrics-server (responses are derived data) but wrong for, e.g., openshift-apiserver (which has its own auditable mutation history).

---

## 8. Building an Extension Apiserver: The `k8s.io/apiserver` Library

`k8s.io/apiserver` (lives at `staging/src/k8s.io/apiserver/` in the kubernetes repo, also published as a standalone Go module) is **the generic Kubernetes apiserver framework**. It is what the main apiserver itself is built on: rip out kube-controller-manager, scheduler, kubelet, and all the built-in API groups, and what is left is essentially `k8s.io/apiserver` plus etcd. You use the same library to build your own.

### 8.1 What the library gives you

```
┌──────────────────────────────────────────────────────────────────────────┐
│                       k8s.io/apiserver MODULES                            │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  pkg/server                                                              │
│    - GenericAPIServer: the HTTP/REST machinery                           │
│    - RecommendedConfig: pre-wired AuthN/AuthZ/admission/audit            │
│    - Run(): start the HTTPS listener, gracefully shut down               │
│                                                                          │
│  pkg/server/options                                                      │
│    - SecureServingOptions: TLS, ports, cert reload                       │
│    - DelegatingAuthenticationOptions: RequestHeader + TokenReview        │
│    - DelegatingAuthorizationOptions: SAR with cache                      │
│    - AuditOptions, FeatureOptions, etcd options                          │
│                                                                          │
│  pkg/registry/generic                                                    │
│    - Store: the generic REST storage (CRUD on objects)                   │
│    - REST: HTTP verb handlers backed by a Store                          │
│                                                                          │
│  pkg/storage                                                             │
│    - Interface: the storage backend contract (Get, List, Watch, …)       │
│    - etcd3.New: etcd-backed implementation                               │
│    - Cacher: the watch cache wrapper                                     │
│                                                                          │
│  pkg/endpoints                                                           │
│    - APIInstaller: registers resource handlers under a path              │
│    - Discovery: serves /apis discovery                                   │
│                                                                          │
│  pkg/admission                                                           │
│    - PluginInitializer, the admission chain                              │
│                                                                          │
│  pkg/authentication                                                      │
│    - request/headerrequest: RequestHeader authenticator                  │
│    - request/anonymous: anonymous authenticator                          │
│                                                                          │
│  pkg/authorization                                                       │
│    - authorizer.Authorizer interface                                     │
│    - delegated: SAR-based authorizer                                     │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 8.2 The reference: sample-apiserver

`kubernetes/staging/src/k8s.io/sample-apiserver/` is the canonical "hello world" extension apiserver. It serves `wardle.example.com/v1alpha1` with two resources: `Flunder` and `Fischer`. It demonstrates every piece: scheme registration, openapi generation, etcd-backed storage, conversion between two versions, server config, command-line flags, and the Deployment/Service/RBAC manifests.

Tree (abbreviated):

```
sample-apiserver/
├── artifacts/example/             # Deployment, Service, APIService, RBAC YAMLs
├── main.go                        # entry point: NewWardleServerCommand + Execute
├── pkg/
│   ├── apis/
│   │   ├── wardle/                # internal types (unversioned)
│   │   │   ├── types.go
│   │   │   ├── register.go
│   │   │   └── v1alpha1/          # external version
│   │   │       ├── types.go
│   │   │       ├── conversion.go
│   │   │       └── zz_generated_*.go
│   │   └── ...
│   ├── apiserver/
│   │   ├── apiserver.go           # WardleServer struct, Config, New
│   │   └── scheme/                # runtime.Scheme with all versions
│   ├── cmd/server/
│   │   ├── start.go               # cobra command + RecommendedOptions
│   │   └── server.go              # Run loop
│   ├── registry/                  # one subpackage per resource
│   │   └── wardle/flunder/
│   │       ├── etcd.go            # generic.Store wiring
│   │       ├── strategy.go        # Strategy: Validate, Default, …
│   │       └── status.go          # /status subresource
│   └── generated/                 # openapi.go, listers, informers, clientset
└── ...
```

We'll walk through each piece. Below, I quote real code from the upstream repo (slightly elided for brevity).

### 8.3 main.go

```go
// staging/src/k8s.io/sample-apiserver/main.go
func main() {
    ctx := genericapiserver.SetupSignalContext()
    options := server.NewWardleServerOptions(os.Stdout, os.Stderr)
    cmd := server.NewCommandStartWardleServer(ctx, options)
    code := cli.Run(cmd)
    os.Exit(code)
}
```

Three lines. The work is in `NewCommandStartWardleServer`.

### 8.4 start.go

```go
// staging/src/k8s.io/sample-apiserver/pkg/cmd/server/start.go
type WardleServerOptions struct {
    RecommendedOptions *genericoptions.RecommendedOptions
    SharedInformerFactory informers.SharedInformerFactory
    StdOut io.Writer
    StdErr io.Writer
}

func NewWardleServerOptions(out, errOut io.Writer) *WardleServerOptions {
    o := &WardleServerOptions{
        RecommendedOptions: genericoptions.NewRecommendedOptions(
            "/registry/wardle.example.com",
            apiserver.Codecs.LegacyCodec(v1alpha1.SchemeGroupVersion),
        ),
    }
    o.RecommendedOptions.Etcd.StorageConfig.EncodeVersioner =
        runtime.NewMultiGroupVersioner(v1alpha1.SchemeGroupVersion, schema.GroupKind{Group: v1alpha1.GroupName})
    return o
}

func NewCommandStartWardleServer(ctx context.Context, opts *WardleServerOptions) *cobra.Command {
    cmd := &cobra.Command{
        Short: "Launch a wardle API server",
        RunE: func(c *cobra.Command, args []string) error {
            if err := opts.Complete(); err != nil { return err }
            if err := opts.Validate(args); err != nil { return err }
            return opts.RunWardleServer(ctx)
        },
    }
    flags := cmd.Flags()
    opts.RecommendedOptions.AddFlags(flags)
    utilfeature.DefaultMutableFeatureGate.AddFlag(flags)
    return cmd
}
```

`RecommendedOptions` wraps every common piece: etcd, secure serving, authentication, authorization, audit, admission, and core API options. You add flags from it to cobra; flags come for free.

### 8.5 server.go: assembling the server

```go
// staging/src/k8s.io/sample-apiserver/pkg/cmd/server/server.go
func (o *WardleServerOptions) Config() (*apiserver.Config, error) {
    serverConfig := genericapiserver.NewRecommendedConfig(apiserver.Codecs)
    serverConfig.OpenAPIConfig = genericapiserver.DefaultOpenAPIConfig(
        sampleopenapi.GetOpenAPIDefinitions, openapi.NewDefinitionNamer(apiserver.Scheme))
    serverConfig.OpenAPIConfig.Info.Title  = "Wardle"
    serverConfig.OpenAPIConfig.Info.Version = "0.1"

    if err := o.RecommendedOptions.ApplyTo(serverConfig); err != nil {
        return nil, err
    }

    return &apiserver.Config{
        GenericConfig: serverConfig,
        ExtraConfig:   apiserver.ExtraConfig{},
    }, nil
}

func (o *WardleServerOptions) RunWardleServer(ctx context.Context) error {
    cfg, err := o.Config()
    if err != nil { return err }
    server, err := cfg.Complete().New()
    if err != nil { return err }
    server.GenericAPIServer.AddPostStartHookOrDie("start-sample-server-informers", func(ctx genericapiserver.PostStartHookContext) error {
        cfg.GenericConfig.SharedInformerFactory.Start(ctx.Done())
        o.SharedInformerFactory.Start(ctx.Done())
        return nil
    })
    return server.GenericAPIServer.PrepareRun().RunWithContext(ctx)
}
```

`RecommendedOptions.ApplyTo` applies *every* options group at once. Etcd connection? Done. Authentication? Done. Audit? Done. Then you call `cfg.Complete().New()` to materialize a `*WardleServer` and `PrepareRun().RunWithContext(ctx)` to serve.

### 8.6 apiserver.go: the WardleServer struct

```go
// staging/src/k8s.io/sample-apiserver/pkg/apiserver/apiserver.go
type WardleServer struct {
    GenericAPIServer *genericapiserver.GenericAPIServer
}

func (c CompletedConfig) New() (*WardleServer, error) {
    genericServer, err := c.GenericConfig.New("sample-apiserver", genericapiserver.NewEmptyDelegate())
    if err != nil { return nil, err }

    s := &WardleServer{GenericAPIServer: genericServer}

    apiGroupInfo := genericapiserver.NewDefaultAPIGroupInfo(wardle.GroupName, Scheme, metav1.ParameterCodec, Codecs)

    v1alpha1storage := map[string]rest.Storage{}
    v1alpha1storage["flunders"] = wardleregistry.RESTInPeace(flunderstorage.NewREST(Scheme, c.GenericConfig.RESTOptionsGetter))
    v1alpha1storage["fischers"] = wardleregistry.RESTInPeace(fischerstorage.NewREST(Scheme, c.GenericConfig.RESTOptionsGetter))
    apiGroupInfo.VersionedResourcesStorageMap["v1alpha1"] = v1alpha1storage

    if err := s.GenericAPIServer.InstallAPIGroup(&apiGroupInfo); err != nil {
        return nil, err
    }
    return s, nil
}
```

Two things this shows:

- The server is a thin wrapper around `genericapiserver.GenericAPIServer`. All the HTTP, AuthN, AuthZ, watch cache, admission, audit — that's all in `GenericAPIServer`.
- "Installing" an API group means handing over a map from version name to map of resource name to REST storage. The library wires `/apis/wardle.example.com/v1alpha1/flunders` to the storage you provided.

### 8.7 The five pieces, summarized

You provide:

1. **Scheme registration.** A `runtime.Scheme` knowing all your types (internal + each external version) and their conversion functions.
2. **OpenAPI generation.** A function `GetOpenAPIDefinitions` produced by `openapi-gen`. Without this, `kubectl explain` and discovery clients don't work.
3. **Registry/Storage.** A `*generic.Store` (or your own `rest.Storage` impl) per resource. This is where storage backend choice plays in (§11).
4. **Strategy.** Per-resource: how to validate, default, prepare for create/update, what to do on delete.
5. **Server chaining.** Pass `genericapiserver.NewEmptyDelegate()` (no chain) or another `*GenericAPIServer` as delegate. Used to combine multiple groups in one binary (e.g. metrics-server has both stable and beta).

---

## 9. `RecommendedConfig`: The Generic Apiserver Skeleton

`RecommendedConfig` is the most under-appreciated type in `k8s.io/apiserver`. It is what makes building an apiserver in ~200 LoC of your own code possible. Knowing what it bundles is knowing what your apiserver "automatically" does.

### 9.1 The struct

```go
// staging/src/k8s.io/apiserver/pkg/server/config.go
type RecommendedConfig struct {
    Config

    SharedInformerFactory informers.SharedInformerFactory
    ClientConfig          *restclient.Config
}

type Config struct {
    SecureServing            *SecureServingInfo
    Authentication            AuthenticationInfo
    Authorization             AuthorizationInfo
    LoopbackClientConfig     *restclient.Config
    EgressSelector            *egressselector.EgressSelector
    EquivalentResourceRegistry runtime.EquivalentResourceRegistry
    Serializer                runtime.NegotiatedSerializer
    OpenAPIConfig             *openapicommon.Config
    OpenAPIV3Config           *openapicommon.OpenAPIV3Config

    AuditBackend audit.Backend
    AuditPolicyRuleEvaluator audit.PolicyRuleEvaluator

    EnableIndex     bool
    EnableProfiling bool

    RequestTimeout            time.Duration
    MinRequestTimeout         int
    LivezGracePeriod          time.Duration
    ShutdownDelayDuration     time.Duration
    JSONPatchMaxCopyBytes     int64
    MaxRequestBodyBytes       int64

    APIServerID                  string
    StorageObjectCountTracker    flowcontrolrequest.StorageObjectCountTracker

    BuildHandlerChainFunc func(apiHandler http.Handler, c *Config) (secure http.Handler)

    AdmissionControl admission.Interface

    // ...
}
```

### 9.2 What each piece is for

| Field | What it does |
|---|---|
| `SecureServing` | HTTPS listener: cert file, key file, port, SNI, http/2 |
| `Authentication.Authenticator` | The chain (RequestHeader + TokenReview + anonymous + …) |
| `Authentication.APIAudiences` | Audiences accepted on bearer tokens (TokenReview uses this) |
| `Authorization.Authorizer` | The chain (delegated SAR + system:masters bypass for loopback) |
| `LoopbackClientConfig` | A client config the apiserver uses to call *itself* (for post-start hooks) |
| `Serializer` | JSON + protobuf encoders/decoders |
| `OpenAPIConfig` | Where to publish OpenAPI v2 |
| `OpenAPIV3Config` | Where to publish OpenAPI v3 (since 1.24) |
| `AuditBackend` / `AuditPolicyRuleEvaluator` | Audit log writer + policy |
| `BuildHandlerChainFunc` | The function that wraps the inner mux with all the middleware |
| `AdmissionControl` | The mutating + validating admission plugins for *your* writes |
| `SharedInformerFactory` | Informers for your apiserver's *own* state (rarely needed unless you have controllers running alongside) |
| `ClientConfig` | A client config for the main apiserver (for TokenReview/SAR) |

### 9.3 BuildHandlerChainFunc

The default chain (from `defaults.go`) wraps in this order, outermost first:

```
DefaultBuildHandlerChain:
  WithPanicRecovery
  WithAuditAnnotations
  WithPreshutdownHooksWaiting
  WithTimeoutForNonLongRunningRequests
  WithRequestDeadline
  WithWaitGroup
  WithRequestInfo
  WithWarningRecorder
  WithCacheControl
  WithHSTS
  WithRequestReceivedTimestamp
  WithMuxAndDiscoveryComplete
  WithMaxInFlightLimit / WithPriorityAndFairness   // APF (only for main apiserver)
  WithImpersonation
  WithAuthorization                                 // AuthZ runs here
  WithAudit
  WithFailedAuthenticationAudit
  WithAuthentication                                // AuthN runs here
  WithCORS
```

So a request enters at the top, gets authenticated, authorized, audited, timed, and finally arrives at your registered resource handler. You can wrap the chain with extra middleware by replacing `BuildHandlerChainFunc`, but you should almost never do this. The whole reason this library exists is to make this chain match `kube-apiserver`'s behavior exactly.

### 9.4 Default value of "recommended"

`genericoptions.NewRecommendedOptions(prefix, codec)` returns a struct that, when `ApplyTo`'d to a config, yields an apiserver with:

- HTTPS on port 443 (configurable), cert auto-loaded from `--tls-cert-file`/`--tls-private-key-file`, or self-signed if not provided
- RequestHeader auth from the kube-system ConfigMap (`--authentication-kubeconfig` for out-of-cluster fallback)
- Delegating authorizer with 10-second cache (`--authorization-kubeconfig` for out-of-cluster fallback)
- Audit logging to stdout if `--audit-log-path=-`
- etcd at `--etcd-servers` with prefix from the codec constructor (e.g. `/registry/wardle.example.com`)
- OpenAPI v2 + v3 published
- `/healthz`, `/livez`, `/readyz` endpoints
- `/metrics` (Prometheus)
- `/debug/pprof/*` if `--profiling=true`
- Admission chain with `NamespaceLifecycle,MutatingAdmissionWebhook,ValidatingAdmissionWebhook` enabled by default

The single most useful sentence in this entire chapter: **`RecommendedOptions` gives you a Kubernetes apiserver that behaves like `kube-apiserver` for ~250 lines of glue code**. Everything else is your business logic.

---

## 10. Registry and Strategy for Custom Resources

The "registry" is the layer between the REST handler and storage. The "strategy" is the per-resource policy object that defines validation, defaulting, status splitting, and a few other hooks. Together they are `~300` lines of per-resource code in a typical aggregated apiserver.

### 10.1 The Strategy interface

```go
// staging/src/k8s.io/apiserver/pkg/registry/rest/create.go
type RESTCreateStrategy interface {
    runtime.ObjectTyper
    names.NameGenerator

    NamespaceScoped() bool
    PrepareForCreate(ctx context.Context, obj runtime.Object)
    Validate(ctx context.Context, obj runtime.Object) field.ErrorList
    WarningsOnCreate(ctx context.Context, obj runtime.Object) []string
    Canonicalize(obj runtime.Object)
}

type RESTUpdateStrategy interface {
    // ...
    AllowCreateOnUpdate() bool
    PrepareForUpdate(ctx context.Context, obj, old runtime.Object)
    ValidateUpdate(ctx context.Context, obj, old runtime.Object) field.ErrorList
    WarningsOnUpdate(ctx context.Context, obj, old runtime.Object) []string
    AllowUnconditionalUpdate() bool
}

type RESTDeleteStrategy interface {
    runtime.ObjectTyper
    // (Validate is implicit; finalizers handled by the store)
}
```

### 10.2 sample-apiserver's Flunder strategy

```go
// staging/src/k8s.io/sample-apiserver/pkg/registry/wardle/flunder/strategy.go
type flunderStrategy struct {
    runtime.ObjectTyper
    names.NameGenerator
}

func NewStrategy(typer runtime.ObjectTyper) flunderStrategy {
    return flunderStrategy{typer, names.SimpleNameGenerator}
}

func (flunderStrategy) NamespaceScoped() bool { return true }

func (flunderStrategy) PrepareForCreate(_ context.Context, obj runtime.Object) {
    f := obj.(*wardle.Flunder)
    f.Status = wardle.FlunderStatus{}        // strip status on create
}

func (flunderStrategy) PrepareForUpdate(_ context.Context, obj, old runtime.Object) {
    newF := obj.(*wardle.Flunder)
    oldF := old.(*wardle.Flunder)
    newF.Status = oldF.Status                // spec-only update: preserve status
}

func (flunderStrategy) Validate(_ context.Context, obj runtime.Object) field.ErrorList {
    f := obj.(*wardle.Flunder)
    return validation.ValidateFlunder(f)
}

func (flunderStrategy) ValidateUpdate(_ context.Context, obj, old runtime.Object) field.ErrorList {
    return validation.ValidateFlunderUpdate(obj.(*wardle.Flunder), old.(*wardle.Flunder))
}

func (flunderStrategy) AllowCreateOnUpdate() bool        { return false }
func (flunderStrategy) AllowUnconditionalUpdate() bool   { return false }
func (flunderStrategy) Canonicalize(_ runtime.Object)    {}
func (flunderStrategy) WarningsOnCreate(_ context.Context, _ runtime.Object) []string { return nil }
func (flunderStrategy) WarningsOnUpdate(_ context.Context, _, _ runtime.Object) []string { return nil }

// /status subresource: separate strategy that allows only status updates
type flunderStatusStrategy struct{ flunderStrategy }

func (flunderStatusStrategy) PrepareForUpdate(_ context.Context, obj, old runtime.Object) {
    newF := obj.(*wardle.Flunder)
    oldF := old.(*wardle.Flunder)
    newF.Spec = oldF.Spec        // spec is immutable on /status PUT
}

func (flunderStatusStrategy) ValidateUpdate(_ context.Context, obj, old runtime.Object) field.ErrorList {
    return validation.ValidateFlunderStatusUpdate(obj.(*wardle.Flunder), old.(*wardle.Flunder))
}
```

This is the **same pattern as built-in resources in the main apiserver**. `pkg/registry/core/pod/strategy.go` looks identical in structure, just with vastly more validation. The hooks let you implement the spec/status separation that gives controllers a single object to write `spec` and another to write `status`, with their respective RBAC verbs.

### 10.3 The Store

```go
// staging/src/k8s.io/sample-apiserver/pkg/registry/wardle/flunder/etcd.go
func NewREST(scheme *runtime.Scheme, optsGetter generic.RESTOptionsGetter) (*registry.REST, error) {
    strategy := NewStrategy(scheme)

    store := &genericregistry.Store{
        NewFunc:                   func() runtime.Object { return &wardle.Flunder{} },
        NewListFunc:               func() runtime.Object { return &wardle.FlunderList{} },
        PredicateFunc:             MatchFlunder,
        DefaultQualifiedResource:  wardle.Resource("flunders"),
        SingularQualifiedResource: wardle.Resource("flunder"),
        CreateStrategy: strategy,
        UpdateStrategy: strategy,
        DeleteStrategy: strategy,
        TableConvertor: rest.NewDefaultTableConvertor(wardle.Resource("flunders")),
    }
    options := &generic.StoreOptions{
        RESTOptions: optsGetter,
        AttrFunc:    GetAttrs,
    }
    if err := store.CompleteWithOptions(options); err != nil {
        return nil, err
    }
    return &registry.REST{Store: store}, nil
}
```

`generic.Store` is the **same Store implementation the built-in apiserver uses**. It implements:

- `Get` — read by name
- `List` — paginated, filtered by label selector and field selector
- `Watch` — chunked HTTP stream of events
- `Create` — runs strategy + admission + store
- `Update` / `Patch` — same
- `Delete` — graceful + immediate, finalizer handling
- `DeleteCollection` — bulk delete

This means as soon as you wire a `Store`, you get watch, list pagination, server-side apply, optimistic concurrency, finalizer behavior, deletion timestamps, table-conversion (`kubectl get` columns), and resource version semantics — for free.

If you want a non-etcd backend, you implement `storage.Interface` (§11) and pass it via `optsGetter`. The Store's HTTP-facing behavior does not change.

---

## 11. Storage Backend Options

This is the single most important decision in building an aggregated apiserver. Four choices.

### 11.1 etcd (the default)

If you use `genericregistry.Store` and configure `RESTOptions.StorageConfig.Transport.ServerList` to point at etcd, you get an apiserver that stores data in etcd just like the main apiserver. Same prefix conventions (`/registry/<group>/<resource>/<namespace>/<name>`), same MVCC semantics, same watch behavior.

Two flavors:

- **Same etcd as the main apiserver.** Use a *different prefix* (e.g. `/registry/my-extension.example.com`). This avoids touching built-in keys and lets etcd's compaction policy apply to both. Use a dedicated etcd user with RBAC limited to your prefix to avoid catastrophe.
- **Dedicated etcd cluster.** For when you have very different storage requirements (huge objects, different compaction needs, isolation). Now you have two etcd clusters to operate; mostly not worth it.

The big advantage of etcd is that the watch cache, the storage interface, pagination, and the entire stack come from the library and *just work*. The disadvantage is the per-object limit (~1.5 MiB) and the total cluster size limit (~8 GB in practice).

### 11.2 Any database (Postgres, MySQL, DynamoDB, ...)

You implement `storage.Interface`:

```go
// staging/src/k8s.io/apiserver/pkg/storage/interfaces.go
type Interface interface {
    Versioner() Versioner
    Create(ctx context.Context, key string, obj, out runtime.Object, ttl uint64) error
    Delete(ctx context.Context, key string, out runtime.Object, preconditions *Preconditions,
           validateDeletion ValidateObjectFunc, cachedExistingObject runtime.Object) error
    Watch(ctx context.Context, key string, opts ListOptions) (watch.Interface, error)
    Get(ctx context.Context, key string, opts GetOptions, objPtr runtime.Object) error
    GetList(ctx context.Context, key string, opts ListOptions, listObj runtime.Object) error
    GuaranteedUpdate(ctx context.Context, key string, destination runtime.Object,
                     ignoreNotFound bool, preconditions *Preconditions,
                     tryUpdate UpdateFunc, cachedExistingObject runtime.Object) error
    Count(key string) (int64, error)
}
```

That looks short. It is not. The hard parts:

- **Watch.** You need to deliver an *ordered* stream of changes since some resourceVersion. Postgres LISTEN/NOTIFY can do it; DynamoDB streams can; relational triggers can. You also need bookkeeping for missed events, compaction, and the dreaded "watch gone too far in the past" error (HTTP 410 Gone).
- **GuaranteedUpdate.** Optimistic concurrency: the client sends a resourceVersion; you must compare-and-swap. SQL transactions can do this; DynamoDB conditional writes can; but the failure mode is subtle (returning the *current* object on conflict so the client can retry).
- **Resource versions.** Must be a monotonic integer across all objects (or you fake it with a sequence). Watch streams emit it; LIST returns it; clients persist it.

Real implementations:

- **Aurora-backed apiserver (internal projects at AWS, Stripe).** Postgres + `xmin` system column for resourceVersion, `LISTEN/NOTIFY` for watch.
- **k0s "konnectivity-server"** uses an SQLite store for the lightweight control plane.
- **k3s** famously uses *kine* — an etcd-shim that translates etcd's gRPC API into SQLite/MySQL/Postgres calls. This is technically an etcd replacement, not a custom apiserver, but the principle is the same.

### 11.3 Remote service (no storage)

This is the metrics-server pattern: there is no "storage" — every Get/List/Watch fetches the answer from somewhere else. You still implement `rest.Storage` but you don't wire a `genericregistry.Store`; you write a custom REST handler.

```go
// pseudo-code from metrics-server
type podMetricsREST struct {
    metricsGetter MetricsGetter   // your in-memory store of scraped metrics
}

func (r *podMetricsREST) New() runtime.Object { return &metrics.PodMetrics{} }
func (r *podMetricsREST) NewList() runtime.Object { return &metrics.PodMetricsList{} }

func (r *podMetricsREST) Get(ctx context.Context, name string, opts *metav1.GetOptions) (runtime.Object, error) {
    namespace := request.NamespaceValue(ctx)
    return r.metricsGetter.GetPodMetrics(namespace, name)
}

func (r *podMetricsREST) List(ctx context.Context, opts *metainternalversion.ListOptions) (runtime.Object, error) {
    namespace := request.NamespaceValue(ctx)
    labelSelector := labels.Everything()
    if opts != nil && opts.LabelSelector != nil {
        labelSelector = opts.LabelSelector
    }
    return r.metricsGetter.ListPodMetrics(namespace, labelSelector)
}

// No Watch implementation — metrics-server returns 405 Method Not Allowed for watch.
```

Notice metrics-server **does not implement watch**. That's a legitimate choice for a read-only ephemeral API, and clients have to know not to call it. If they do, the apiserver returns `405`. We will see in §16 that this *is* a downside — some clients break.

### 11.4 In-memory (for short-lived data)

A degenerate case of "remote service" where the "remote" is your own RAM. Useful for caches, ephemeral state, anything that should not survive a pod restart.

You implement `rest.Storage` and hold a `map[string]Object` behind a mutex (or use `sync.Map`). Same watch caveat applies: you can implement watch in-process easily, but you have no resource version persistence — clients with a long-lived watch will get 410 Gone on every restart.

### 11.5 The decision table

| Use case | Backend |
|---|---|
| Schema-validated CRD-like API, you want kubectl edit, audit, etc. | **etcd** (or just use a CRD; see §22) |
| Wrapping an existing canonical database | **Custom storage.Interface to that DB** |
| Synthesized read-only data (metrics, costs, status of external resources) | **Remote service (no storage)** |
| Federation / proxy to remote clusters | **Remote service** |
| Ephemeral cache, large objects | **In-memory** |
| Replacing a built-in's storage backend | **Whatever you replaced it with**, and rethink your life |

---

## 12. metrics-server: The Canonical Aggregated API

If you only ever look at one extension apiserver in detail, look at this one. It is small (~5000 LoC of Go in `kubernetes-sigs/metrics-server`), it is in every production cluster, and it touches every part of the model. Almost every concept above maps onto it.

### 12.1 What it serves

```
$ kubectl get --raw /apis/metrics.k8s.io/v1beta1
{
  "kind": "APIResourceList",
  "apiVersion": "v1",
  "groupVersion": "metrics.k8s.io/v1beta1",
  "resources": [
    {"name": "nodes", "singularName": "", "namespaced": false, "kind": "NodeMetrics", "verbs": ["get","list"]},
    {"name": "pods",  "singularName": "", "namespaced": true,  "kind": "PodMetrics",  "verbs": ["get","list"]}
  ]
}
```

Two resources, two verbs each (`get` and `list`). No watch. No create/update/delete. The simplest possible aggregated API.

### 12.2 The APIService

```yaml
apiVersion: apiregistration.k8s.io/v1
kind: APIService
metadata:
  name: v1beta1.metrics.k8s.io
spec:
  group: metrics.k8s.io
  version: v1beta1
  groupPriorityMinimum: 100
  versionPriority: 100
  service:
    namespace: kube-system
    name: metrics-server
    port: 443
  insecureSkipTLSVerify: false
  caBundle: <auto-injected by cert-manager OR set by the operator that installed metrics-server>
```

### 12.3 Architecture

```
                    ┌────────────────────────────────────────────────┐
                    │             kube-apiserver                       │
                    │   (handles /apis/metrics.k8s.io via aggregator) │
                    └────────────────────────┬───────────────────────┘
                                             │ proxy
                                             ▼
                    ┌────────────────────────────────────────────────┐
                    │   metrics-server  (Deployment, 2+ replicas)    │
                    │                                                 │
                    │   ┌──────────────────────────────────────────┐  │
                    │   │ Scraper goroutine (every 60s by default) │  │
                    │   │  - for each Node in informer cache:      │  │
                    │   │      GET https://<node>:10250/metrics/   │  │
                    │   │          resource                         │  │
                    │   │  - parse Prom text format                 │  │
                    │   │  - decode container_cpu_usage_seconds_   │  │
                    │   │    total, container_memory_working_set   │  │
                    │   │    _bytes, node_cpu/memory                │  │
                    │   │  - compute deltas (rate) against prev    │  │
                    │   │    sample                                 │  │
                    │   │  - write into in-memory store            │  │
                    │   └──────────────────────────────────────────┘  │
                    │                                                 │
                    │   ┌──────────────────────────────────────────┐  │
                    │   │ REST handler                              │  │
                    │   │  - on Get/List, read from in-memory      │  │
                    │   │  - cross-reference with Pod informer to  │  │
                    │   │    return only currently-existing pods   │  │
                    │   │  - synthesize PodMetrics / NodeMetrics   │  │
                    │   │    objects                                │  │
                    │   └──────────────────────────────────────────┘  │
                    └─────────────────────────┬──────────────────────┘
                                              │ HTTPS
                                              ▼
                    ┌────────────────────────────────────────────────┐
                    │ kubelet on every node (port 10250)             │
                    │   /metrics/resource serves:                    │
                    │     node_cpu_usage_seconds_total                │
                    │     node_memory_working_set_bytes               │
                    │     container_cpu_usage_seconds_total           │
                    │     container_memory_working_set_bytes          │
                    │     pod_cpu_usage_seconds_total                 │
                    │     pod_memory_working_set_bytes                │
                    └────────────────────────────────────────────────┘
```

### 12.4 The /metrics/resource endpoint

The kubelet's `/metrics/resource` endpoint replaced the older `/stats/summary` JSON endpoint in modern versions; it is Prometheus text format, scraped over HTTPS with the metrics-server's ServiceAccount token. The kubelet validates that token via TokenReview against the main apiserver (chapter 10 covers this). Authorization is via the `nodes/metrics` resource:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: system:metrics-server
rules:
- apiGroups: [""]
  resources: ["nodes/metrics", "nodes/stats", "nodes/proxy"]
  verbs: ["get"]
- apiGroups: [""]
  resources: ["pods", "nodes", "namespaces", "configmaps"]
  verbs: ["get","list","watch"]
```

### 12.5 The scrape loop

```go
// kubernetes-sigs/metrics-server/pkg/scraper/scraper.go (simplified)
func (s *scraper) Scrape(ctx context.Context, baseCtx context.Context, node *corev1.Node) (*storage.MetricsBatch, error) {
    addr, err := s.addrResolver.NodeAddress(node)
    if err != nil { return nil, err }
    url := fmt.Sprintf("https://%s:%d/metrics/resource", addr, s.kubeletPort)

    req, _ := http.NewRequestWithContext(ctx, "GET", url, nil)
    resp, err := s.kubeletClient.Do(req)
    if err != nil { return nil, err }
    defer resp.Body.Close()

    batch, err := decodeBatch(resp.Body, node.Name)
    return batch, err
}

func (m *manager) Collect(ctx context.Context) {
    nodes, _ := m.nodeLister.List(labels.Everything())
    results := make(chan *storage.MetricsBatch, len(nodes))
    var wg sync.WaitGroup
    for _, n := range nodes {
        wg.Add(1)
        go func(n *corev1.Node) {
            defer wg.Done()
            b, err := m.scraper.Scrape(ctx, ctx, n)
            if err != nil { /* metric/log; continue */ return }
            results <- b
        }(n)
    }
    wg.Wait()
    close(results)
    m.store.Store(collectBatches(results))
}
```

Three points:

- Parallel scrape (one goroutine per node), bounded by a context timeout.
- The result is dumped into a `storage.Store` that holds two timestamped samples per pod and per node (current + previous, for delta computation).
- The store is read by REST handlers — no etcd, no persistence, no resourceVersion semantics. Restart a metrics-server pod and your data is gone until the next scrape interval.

### 12.6 HA and the consequence of no persistence

Metrics-server runs as a Deployment with `replicas: 2` (or 3) and `topologySpreadConstraints` to spread across zones. Each replica scrapes *every* node independently. Behind the Service, kube-proxy load-balances reads across them. There is no leader election. There is no shared state.

Consequence: each replica has slightly different samples (one might have scraped node X 5 seconds ago, another 35 seconds ago). For HPA this is fine — the rates are accurate within a percent. For finer-grained tooling it can be jarring; you typically pin HPA-style consumers to live with it.

### 12.7 HPA's path

```
HPA controller (in kube-controller-manager)
   │
   ▼ every 15s (configurable)
LIST /apis/metrics.k8s.io/v1beta1/namespaces/<ns>/pods?labelSelector=<deployment-selector>
   │
   ▼ (through aggregator)
metrics-server
   │
   ▼
in-memory store → PodMetricsList synthesized → HTTP response
   │
   ▼
HPA: compute avg CPU%, compute desired replicas
   │
   ▼
PATCH deployment/scale subresource
```

If metrics-server is down (`APIService Available=False`), HPA gets `503` on every list and refuses to scale (it does not falsely scale to zero — it stays at current `replicas`). This is the right failure mode but it does mean a wedged metrics-server takes out autoscaling cluster-wide.

---

## 13. custom-metrics-apiserver and external-metrics-apiserver

HPA v2 supports more than just CPU and memory. It supports three metric source types:

- **Resource** — CPU/memory, served by `metrics.k8s.io` (metrics-server).
- **Pods** / **Object** — custom metrics scoped to specific objects, served by `custom.metrics.k8s.io`.
- **External** — metrics from outside the cluster, served by `external.metrics.k8s.io`.

The last two are also aggregated APIs, and they have a generic implementation library: `github.com/kubernetes-sigs/custom-metrics-apiserver`. It is *the* skeleton for building a metrics adapter — you implement `provider.MetricsProvider` and the library does the apiserver framework wrapping.

### 13.1 The interface

```go
// kubernetes-sigs/custom-metrics-apiserver/pkg/provider/interfaces.go
type CustomMetricsProvider interface {
    GetMetricByName(ctx context.Context, name types.NamespacedName,
                    info CustomMetricInfo, metricSelector labels.Selector) (*custom_metrics.MetricValue, error)
    GetMetricBySelector(ctx context.Context, namespace string, selector labels.Selector,
                        info CustomMetricInfo, metricSelector labels.Selector) (*custom_metrics.MetricValueList, error)
    ListAllMetrics() []CustomMetricInfo
}

type ExternalMetricsProvider interface {
    GetExternalMetric(ctx context.Context, namespace string,
                      metricSelector labels.Selector, info ExternalMetricInfo) (*external_metrics.ExternalMetricValueList, error)
    ListAllExternalMetrics() []ExternalMetricInfo
}
```

### 13.2 The adapter zoo

| Adapter | Backend | Notes |
|---|---|---|
| `prometheus-adapter` | Prometheus | The default; configurable rules translate Prometheus metrics to k8s metric names |
| `keda` | Multiple (Kafka, RabbitMQ, AWS SQS, NATS, …) | KEDA's metrics adapter pushes both custom and external |
| `datadog-cluster-agent` | Datadog | Cloud APM metrics |
| `newrelic-k8s-metrics-adapter` | New Relic | Same idea, different vendor |
| `metrics-adapter` (Azure) | Azure Monitor | For Azure-resident metrics |
| `gcp-custom-metrics-adapter` | Stackdriver / Cloud Monitoring | |
| `openstack-cloud-controller-manager` | OpenStack telemetry | Self-hosted clouds |

All of these are **aggregated apiservers** wrapping the custom-metrics-apiserver library, registered with their own `APIService` (`v1beta1.custom.metrics.k8s.io` or `v1beta1.external.metrics.k8s.io`).

### 13.3 The HPA-side picture

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: queue-worker
  namespace: prod
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: queue-worker
  minReplicas: 2
  maxReplicas: 50
  metrics:
  - type: External                                # ← talks to external.metrics.k8s.io
    external:
      metric:
        name: kafka_consumergroup_lag
        selector:
          matchLabels:
            consumergroup: queue-worker
            topic: orders
      target:
        type: AverageValue
        averageValue: "1000"
  - type: Pods                                   # ← talks to custom.metrics.k8s.io
    pods:
      metric:
        name: http_requests_per_second
      target:
        type: AverageValue
        averageValue: "100"
```

HPA controller resolves each metric by calling the respective aggregated API. The adapter consults Prometheus (or whatever backend), returns the value, HPA does the math.

### 13.4 Why this is a great fit for aggregation

- **Data lives in Prometheus.** No reason to copy into etcd.
- **High cardinality.** Custom metrics can have thousands of dimension combinations; etcd would die.
- **Read-only.** No need for create/update; the metrics are scraped by Prometheus and queried by the adapter.
- **No watch needed.** HPA polls every 15s; it does not maintain a long watch on metrics.

All four of those are CRDs' weaknesses. The pattern is so good that you should mentally tag every "I want HPA to scale on X" question as "do I have, or want to build, an aggregated metrics adapter?".

---

## 14. The Service Backing: Deployment, Service, HA

The extension apiserver runs as ordinary Pods. Three Kubernetes objects glue it together: a Deployment, a Service, and the APIService that points the Service at the aggregator. Plus a ServiceAccount and the RBAC objects from §7.

### 14.1 Complete manifests for a sample-apiserver-style extension

```yaml
# namespace
apiVersion: v1
kind: Namespace
metadata:
  name: wardle
---
# ServiceAccount
apiVersion: v1
kind: ServiceAccount
metadata:
  name: wardle-apiserver
  namespace: wardle
---
# Auth-delegator binding (for TokenReview + SAR)
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: wardle:auth-delegator
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:auth-delegator
subjects:
- kind: ServiceAccount
  name: wardle-apiserver
  namespace: wardle
---
# Read the front-proxy CA config map
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: wardle:extension-apiserver-authentication-reader
  namespace: kube-system
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: extension-apiserver-authentication-reader
subjects:
- kind: ServiceAccount
  name: wardle-apiserver
  namespace: wardle
---
# Deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: wardle-apiserver
  namespace: wardle
spec:
  replicas: 2
  selector:
    matchLabels: { app: wardle-apiserver }
  template:
    metadata:
      labels: { app: wardle-apiserver }
    spec:
      serviceAccountName: wardle-apiserver
      topologySpreadConstraints:
      - maxSkew: 1
        topologyKey: topology.kubernetes.io/zone
        whenUnsatisfiable: ScheduleAnyway
        labelSelector:
          matchLabels: { app: wardle-apiserver }
      priorityClassName: system-cluster-critical
      containers:
      - name: apiserver
        image: registry.example.com/wardle-apiserver:v0.1.2
        imagePullPolicy: IfNotPresent
        args:
        - --etcd-servers=https://etcd.wardle.svc:2379
        - --etcd-cafile=/etc/etcd/ca.crt
        - --etcd-certfile=/etc/etcd/client.crt
        - --etcd-keyfile=/etc/etcd/client.key
        - --secure-port=4443
        - --tls-cert-file=/etc/serving/tls.crt
        - --tls-private-key-file=/etc/serving/tls.key
        - --audit-log-path=-
        - --audit-log-maxage=0
        - --audit-log-maxbackup=0
        - --feature-gates=APIPriorityAndFairness=true
        - --v=2
        ports:
        - name: https
          containerPort: 4443
        readinessProbe:
          httpGet:
            scheme: HTTPS
            path: /readyz
            port: 4443
          periodSeconds: 5
          failureThreshold: 3
        livenessProbe:
          httpGet:
            scheme: HTTPS
            path: /livez
            port: 4443
          periodSeconds: 10
        resources:
          requests:
            cpu: 100m
            memory: 200Mi
          limits:
            memory: 500Mi
        volumeMounts:
        - { name: serving, mountPath: /etc/serving, readOnly: true }
        - { name: etcd-certs, mountPath: /etc/etcd, readOnly: true }
      volumes:
      - name: serving
        secret:
          secretName: wardle-apiserver-serving
      - name: etcd-certs
        secret:
          secretName: wardle-etcd-client
---
# Service
apiVersion: v1
kind: Service
metadata:
  name: wardle-apiserver
  namespace: wardle
spec:
  type: ClusterIP
  selector:
    app: wardle-apiserver
  ports:
  - name: https
    port: 443
    targetPort: 4443
---
# APIService — the registration with the aggregator
apiVersion: apiregistration.k8s.io/v1
kind: APIService
metadata:
  name: v1alpha1.wardle.example.com
  annotations:
    cert-manager.io/inject-ca-from: wardle/wardle-apiserver-serving
spec:
  group: wardle.example.com
  version: v1alpha1
  groupPriorityMinimum: 1000
  versionPriority: 15
  service:
    namespace: wardle
    name: wardle-apiserver
    port: 443
```

### 14.2 HA properties to insist on

- **`replicas: 2` minimum, `3` for serious deployments.** A single replica means rolling restarts take the API down.
- **`topologySpreadConstraints` or `podAntiAffinity`** to put replicas in different zones / nodes.
- **`PodDisruptionBudget` with `minAvailable: 1`** (or `maxUnavailable: 1` for replicas=2):

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: wardle-apiserver
  namespace: wardle
spec:
  minAvailable: 1
  selector:
    matchLabels: { app: wardle-apiserver }
```

- **`priorityClassName: system-cluster-critical`** so the scheduler does not evict you to make room.
- **`readinessProbe` on `/readyz`** — this is *the* signal that drives the `Available` condition. If your readiness probe is too tight (1-second period, 1-attempt failure), you flap.
- **Graceful shutdown.** The library wires `--shutdown-delay-duration` (default 0); set it to `30s` so a SIGTERM keeps the apiserver serving until kube-proxy / the aggregator stops sending it traffic.

### 14.3 Rolling restart behavior

When a Deployment rolls (image bump, config change), one pod terminates, a new one starts. During the gap:

- The Service routes 100% of traffic to the surviving replica(s).
- The aggregator's connection pool may briefly hold dead connections; expect a few 503s.
- HPA / kubectl users see retryable transient errors.

With 2 replicas, a `maxSurge=25%`/`maxUnavailable=25%` Deployment policy keeps at least 1 replica serving at all times. With 1 replica, you take a downtime every rollout. Always run with ≥2.

---

## 15. CA Bundle Management

`APIService.spec.caBundle` is a base64-encoded PEM CA bundle. It must match the CA that signed the extension apiserver's serving cert. If they mismatch, the aggregator's TLS verification fails and the APIService goes `Available=False` with `FailedDiscoveryCheck`.

Three approaches, in order of operational maturity.

### 15.1 Manual

You generate a self-signed CA, sign a serving cert, mount the cert+key as a Secret on the Deployment, and base64-encode the CA into the APIService. Fine for demos. Painful when the cert expires.

### 15.2 cert-manager injection

The widely-used pattern. You annotate the APIService:

```yaml
metadata:
  annotations:
    cert-manager.io/inject-ca-from: wardle/wardle-apiserver-serving
```

cert-manager's `cainjector` watches for this annotation, finds the named `Certificate` resource, reads the CA from its issued Secret, and patches `spec.caBundle` whenever the CA rotates. The `Certificate` itself looks like:

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: wardle-apiserver-serving
  namespace: wardle
spec:
  secretName: wardle-apiserver-serving       # → mounted by the Deployment
  duration: 2160h                            # 90 days
  renewBefore: 360h                          # 15 days
  dnsNames:
  - wardle-apiserver.wardle.svc
  - wardle-apiserver.wardle.svc.cluster.local
  issuerRef:
    name: wardle-apiserver-ca-issuer
    kind: Issuer
```

When cert-manager rotates the cert, the new Secret is mounted (kubelet refreshes mounted Secret contents on a schedule, or you can use a rolling restart trigger like `secretReloader`), and the CA in the APIService is updated atomically. The aggregator picks up the new CA from its informer cache without restart.

### 15.3 kube-aggregator-managed injection

Less common: a controller in the cluster watches a known ConfigMap and stamps its content into matching APIServices. Used by some operators that manage their own apiserver — they bundle the CA controller with the operator deployment.

### 15.4 The failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `FailedDiscoveryCheck` immediately after install | `caBundle` empty or stale | Restart cainjector; manually update |
| `FailedDiscoveryCheck` after weeks | Cert expired | Rotate; check `renewBefore` |
| Random TLS errors in aggregator logs | CA bundle has multiple CAs but extension cert is signed by an old one | Force re-issue from the current CA |
| Works locally, fails in HA | `dnsNames` missing the Service FQDN | Add `*.wardle.svc.cluster.local` |

---

## 16. The Downsides: What CRDs Hide From You

You now know what aggregation *can* do. Here is what CRDs were doing for you that you have to redo.

### 16.1 Watch

CRDs get watch from the apiextensions handler, which proxies into the same watch cache the main apiserver uses. Aggregated apiservers must implement watch themselves: a long-lived chunked-transfer HTTP response that emits one JSON-encoded `WatchEvent` per change.

The `generic.Store` + etcd combination implements this for free. A custom `storage.Interface` does not — you have to:

- Track resourceVersion as a monotonic integer.
- Buffer recent events for replay (so a client reconnecting at RV=N can receive events RV=N+1 onward).
- Emit `Bookmark` events periodically so clients can advance their RV without seeing a change.
- Handle the "too old" case with HTTP 410 Gone (client must re-list).

If you skip watch (like metrics-server does), clients that depend on it break. The good news: most clients gracefully fall back to polling if watch returns 405. The bad news: many controller-runtime-based controllers will log loud errors every time they try.

### 16.2 List pagination

`LIST` with `?limit=500&continue=<token>` is the standard pagination contract. CRDs implement it via the watch cache's `RemainingItemCount`. Aggregated apiservers implementing custom storage must:

- Generate a `continue` token from the last key seen + the snapshot RV.
- On the next request, validate the token and resume from the correct position.
- Handle the case where the snapshot has been GC'd (RV too old → 410).

Many home-grown aggregated apiservers don't implement pagination. Their `LIST` returns everything in one giant payload. This works for small collections (≤1k items) and breaks for large ones — slow, RAM-heavy on both client and server, and at some point exceeds the apiserver's body limit (`--max-request-body-bytes`).

### 16.3 Server-side apply

SSA's three-way merge requires the apiserver to track per-field manager ownership in `metadata.managedFields`. CRDs get this for free since 1.18 (provided their schema declares `x-kubernetes-list-type` correctly). Aggregated apiservers must implement managed-fields logic themselves — the `genericregistry.Store` does this if you back it with etcd, but custom storage backends typically don't.

### 16.4 Conversion

A CRD with multiple versions either uses `strategy: None` (the versions are identical) or wires a `conversionReviewVersions` webhook. Aggregated apiservers implement conversion in-process via the `runtime.Scheme` and conversion functions (`zz_generated_conversion.go` files). This is *better* than webhook conversion — no round trip — but you have to write the conversion functions for every field that changes shape.

### 16.5 OpenAPI

CRDs publish their OpenAPI schema automatically from the CRD's `openAPIV3Schema`. Aggregated apiservers must publish OpenAPI documents themselves, via `openapi-gen` (a code generator that walks your Go types and emits an `openapi.go` file with definitions).

Without OpenAPI:
- `kubectl explain` returns "no description".
- `kubectl get -o jsonpath=...` may work but `kubectl edit` won't validate.
- The `kubectl explain` discovery from the aggregator returns 404.

### 16.6 kubectl explain

Even with OpenAPI properly generated, `kubectl explain wardle.example.com/v1alpha1.flunder.spec` is sometimes finicky for aggregated types. The discovery client caches per-group, and aggregator-served groups have different cache TTLs than CRDs. Users may see stale schemas; `kubectl --cache-dir=/dev/null explain ...` is a common workaround.

### 16.7 kustomize / helm tooling

Tools like `kubectl diff`, `kubectl apply --server-side`, and `kustomize build` use OpenAPI to know merge strategies. Aggregated types occasionally trip them up — e.g. `kustomize`'s `patchesStrategicMerge` may not recognize an aggregated type and fall back to JSON merge (which doesn't preserve list semantics).

### 16.8 Audit log granularity

The main apiserver audits the *aggregator's* view of the request: who, what, when. The *body* of the request is opaque to the aggregator. So if you want a structured audit log of "alice updated foo.spec.bar from X to Y", you need your own audit pipeline. The `k8s.io/apiserver` library has audit plugins; wire them.

### 16.9 Uptime is on you

A CRD's uptime is the main apiserver's uptime. An aggregated apiserver's uptime is whatever your Deployment achieves. With 2 replicas + a PDB + a sensible deployment strategy, you can get three nines. To get four or five nines (cluster-class SLO), you are running an *apiserver* with all the same maturity demands as the main one: monitoring, alerting, debugging tools, rollback procedures, dependency tracking, etc.

---

## 17. Aggregator Request Flow: End-to-End Trace

Here is a single GET request from `kubectl top pods` and the journey it takes. Times are illustrative for a healthy cluster.

```
T+0ms     kubectl top pods --namespace=prod
           kubectl: discovery → finds metrics.k8s.io/v1beta1.PodMetrics
T+5ms     kubectl: build request
           GET https://api.example.com/apis/metrics.k8s.io/v1beta1/namespaces/prod/pods
T+10ms    TCP + TLS handshake to api.example.com (kube-apiserver)
T+15ms    [kube-apiserver]
           AuthN:  client cert → user="alice", groups=["ops"]
           AuthZ:  RBAC check → alice can "list pods.metrics.k8s.io" in "prod"
           Admission: skipped for GET
           APF:     classified into "system-leader-election" or similar
T+18ms    [kube-aggregator handler]
           Route lookup: APIService v1beta1.metrics.k8s.io → service kube-system/metrics-server:443
           Pull cached caBundle, prepare TLS config
T+19ms    [serviceResolver]
           Resolve "metrics-server.kube-system.svc" → ClusterIP 10.96.0.220
           (kube-proxy will DNAT to a pod IP — that part is invisible to the aggregator)
T+20ms    [AuthProxyRoundTripper]
           Strip Authorization header
           Set X-Remote-User: alice
           Set X-Remote-Group: ops
           Set X-Remote-Uid: <alice's UID>
T+22ms    Dial 10.96.0.220:443 over HTTPS, presenting the front-proxy-client cert
           kube-proxy DNATs → pod IP 10.244.7.12 (metrics-server replica B)
T+25ms    [metrics-server pod]
           TLS handshake; verify client cert against front-proxy CA from
              kube-system/extension-apiserver-authentication ConfigMap.
           CN check: client cert CN == "front-proxy-client" → match.
T+27ms    [metrics-server: requestheader authenticator]
           Read X-Remote-User="alice", X-Remote-Group=["ops"]
           user.Info{Name:"alice", Groups:["ops"]}
T+28ms    [metrics-server: delegated authorizer]
           Cache lookup for (alice, list, pods.metrics.k8s.io, prod) → MISS
           Issue SubjectAccessReview to kube-apiserver
T+33ms      kube-apiserver: AuthN (metrics-server's SA token), then evaluate RBAC for alice
           SAR response: allowed=true
           Cache it for 10 seconds
T+34ms    [metrics-server: REST handler]
           List PodMetrics in namespace "prod"
           Pod informer: list of pods currently in "prod" → [web-1, web-2, web-3, …]
           In-memory metrics store: for each pod, fetch latest sample
           Compute rates: cpu = (curr - prev) / (currTime - prevTime)
           Build PodMetricsList object
T+38ms    Serialize as JSON
T+39ms    HTTP 200 + body → response stream
T+40ms    [kube-aggregator] streams response through to client
T+45ms    [kubectl] decodes JSON, formats table, prints to stdout
T+50ms    Done
```

The 50ms budget is mostly network + TLS. The actual work in metrics-server is ~5ms (informer cache hit + in-memory map lookup).

Failure points and what they look like:

| Stage | Failure | Symptom |
|---|---|---|
| T+18ms | APIService not found | `kubectl: error: the server doesn't have a resource type ...` |
| T+19ms | Service has no endpoints | `503` from aggregator, `EndpointsNotFound` in APIService status |
| T+25ms | TLS verify fails | `503`, aggregator log: `x509: certificate signed by unknown authority` |
| T+27ms | Headers not trusted | metrics-server treats request as anonymous, 403 Forbidden |
| T+33ms | SAR returns "no" | metrics-server returns 403 to aggregator → 403 to kubectl |
| T+34ms | No samples for any pod | Empty list (not an error) |
| Anywhere | metrics-server pod restarted | Connection RST mid-stream; kubectl retries automatically |

---

## 18. The `Available` Condition and `FailedDiscoveryCheck`

The `APIService.status.conditions[Available]` field is driven by a controller in `kube-aggregator`. It probes the extension apiserver's discovery endpoint (`/apis/<group>/<version>`) over the same Service the aggregator uses for proxying. If the probe succeeds, `Available=True`; otherwise `False` with a `reason` indicating why.

### 18.1 The controller

`staging/src/k8s.io/kube-aggregator/pkg/controllers/status/available_controller.go`:

```go
// pseudo-code
func (c *AvailableConditionController) sync(key string) error {
    apiService, err := c.apiServiceLister.Get(key)
    if errors.IsNotFound(err) { return nil }
    if err != nil { return err }

    // Local APIServices (no spec.service) are always Available.
    if apiService.Spec.Service == nil {
        return c.markAvailable(apiService)
    }

    service, err := c.serviceLister.Services(apiService.Spec.Service.Namespace).
                                   Get(apiService.Spec.Service.Name)
    if errors.IsNotFound(err) {
        return c.markUnavailable(apiService, "ServiceNotFound",
                                  fmt.Sprintf("service %s/%s not found", ...))
    }

    endpoints, err := c.endpointsLister.Endpoints(...).Get(...)
    if errors.IsNotFound(err) || len(endpoints.Subsets) == 0 {
        return c.markUnavailable(apiService, "EndpointsNotFound", ...)
    }

    // Probe /apis/<group>/<version> over the service.
    discoveryURL := fmt.Sprintf("https://%s.%s.svc:%d/apis/%s/%s",
        apiService.Spec.Service.Name, apiService.Spec.Service.Namespace,
        *apiService.Spec.Service.Port, apiService.Spec.Group, apiService.Spec.Version)

    resp, err := c.discoveryClient.Get(discoveryURL)
    if err != nil || resp.StatusCode != 200 {
        return c.markUnavailable(apiService, "FailedDiscoveryCheck",
                                  fmt.Sprintf("failing or missing response from %s: %v", discoveryURL, err))
    }

    return c.markAvailable(apiService)
}
```

Probe interval: ~60s; quicker on transitions.

### 18.2 Common failure modes

```
$ kubectl get apiservice v1beta1.metrics.k8s.io -o yaml
status:
  conditions:
  - type: Available
    status: "False"
    reason: FailedDiscoveryCheck
    message: 'failing or missing response from https://10.96.0.220:443/apis/metrics.k8s.io/v1beta1:
      Get "https://10.96.0.220:443/apis/metrics.k8s.io/v1beta1": x509: certificate signed
      by unknown authority'
```

Debug checklist:

1. **Get the APIService.** `kubectl get apiservice <name> -o yaml` and read `status.conditions`.
2. **Check the Service.** `kubectl get svc -n kube-system metrics-server`. Must exist, type ClusterIP, port matching the APIService.
3. **Check Endpoints.** `kubectl get endpoints -n kube-system metrics-server`. Must have at least one ready address.
4. **Check the Pod's readiness probe.** If the probe fails, endpoints get removed.
5. **Reproduce the discovery call.** From inside the cluster, `curl -k https://metrics-server.kube-system.svc:443/apis/metrics.k8s.io/v1beta1` (or with `--cacert`). The response should be a JSON `APIResourceList`.
6. **Check the CA bundle.** `kubectl get apiservice <name> -o jsonpath='{.spec.caBundle}' | base64 -d | openssl x509 -noout -subject -issuer -dates`. Confirm it matches the cert the extension apiserver presents (`openssl s_client -connect 10.96.0.220:443 -showcerts`).
7. **Check the extension apiserver logs.** TLS errors there will say "tls: bad certificate" or similar.

### 18.3 OpenAPIAggregation

Beyond `Available`, the aggregator also fetches OpenAPI documents from each extension apiserver and merges them into the cluster-wide schema. The fetch happens via the same Service path and the same TLS. Failures here show up as `kubectl explain` not finding your types and (since 1.27) as `NonStructuralSchema`-style warnings in the aggregator logs.

---

## 19. Versions and Priorities: GroupPriorityMinimum, VersionPriority

Two integers on every APIService control the order in which `GroupVersion`s appear to clients and, in rare cases, which one *wins* when there is a collision.

### 19.1 GroupPriorityMinimum

Higher values win. Used to order groups in discovery responses and in the aggregator's routing table when multiple groups share the same path prefix (which they don't, really, except for the special `oapi`/`apis` historical paths).

Conventional values:

| Group class | GroupPriorityMinimum |
|---|---|
| Built-in core (`""`) | 18000 |
| Built-in stable (`apps`, `batch`, `apiextensions.k8s.io`) | 17500–17800 |
| Built-in beta (`autoscaling/v2beta2` historically) | 17000 |
| Third-party stable | 1000–9000 |
| Third-party beta | 100–999 |
| Third-party alpha | <100 |

You should pick a value that **reflects how stable your API is**, not how "important" you think it is. metrics-server uses 100. cert-manager uses 1000. Istio uses 2000.

### 19.2 VersionPriority

Within a group, higher values win. Used when a group has multiple registered versions and a client doesn't specify which one (`kubectl get foos` without `-v`). Convention:

- `v1` → 100
- `v1beta1` → 15
- `v1alpha1` → 9

The "preferred version" reported in discovery is the highest-priority version.

### 19.3 The collision case (and why you should not abuse it)

If two APIServices register the same `GroupVersion`, the aggregator picks the one with the higher `GroupPriorityMinimum` (then `VersionPriority`). The other is shadowed — its requests will never be received. This is how you would *theoretically* shadow a built-in by registering a higher-priority APIService. Don't:

- It is allowed by the aggregator but the main apiserver special-cases its own groups and refuses to install the shadowed one's local APIService in the first place (in modern versions).
- Even if it worked, clients would still see *one* GroupVersion but with different behavior, which is the worst kind of debugging puzzle.
- OpenShift famously did this for some `*.openshift.io` groups (replacing in-built oapi resources with their own apiserver); they own both sides of the trust boundary, so it works, but it required significant changes to bootstrap and disaster recovery.

If you find yourself wanting to "replace" a built-in, the correct path is usually: file a KEP and contribute the change upstream. The aggregation layer is not a license to overwrite the cluster's built-in semantics for your tenant.

### 19.4 Priority cookbook

| Situation | GroupPriorityMinimum | VersionPriority |
|---|---|---|
| New v1alpha1 API, experimental | 50 | 9 |
| Promotion to v1beta1 (still alpha2 unreleased) | 1000 | 15 |
| GA v1 alongside deprecated v1beta1 | 1000 | 100 (v1), 15 (v1beta1) |
| Multi-version with stable + beta | 1000 | 100, 90, 80 |
| Replacing a built-in (don't) | >18000 | irrelevant |

---

## 20. apiserver-runtime: The Kubebuilder of Aggregated APIs

`kubebuilder` is what made CRD-based operator development sane: scaffolding, a Manager, a Reconciler, controller-runtime under the hood. The analog for aggregated apiservers is `sigs.k8s.io/apiserver-runtime` (formerly `kubernetes-sigs/apiserver-builder-alpha`). It is less mature, has a smaller user base, and the project's GitHub README still says "experimental" — but it does produce a working aggregated apiserver from a few annotated Go structs.

### 20.1 The model

```go
// pkg/apis/wardle/v1alpha1/flunder_types.go
package v1alpha1

import (
    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
    "sigs.k8s.io/apiserver-runtime/pkg/builder/resource"
)

// +k8s:openapi-gen=true
// +k8s:deepcopy-gen=true
// +genclient
type Flunder struct {
    metav1.TypeMeta   `json:",inline"`
    metav1.ObjectMeta `json:"metadata,omitempty"`
    Spec   FlunderSpec   `json:"spec,omitempty"`
    Status FlunderStatus `json:"status,omitempty"`
}

type FlunderSpec struct {
    Color string `json:"color"`
    Size  int32  `json:"size"`
}

type FlunderStatus struct {
    Phase string `json:"phase,omitempty"`
}

// Provide a default storage strategy.
var _ resource.Object = &Flunder{}
func (f *Flunder) GetObjectMeta() *metav1.ObjectMeta { return &f.ObjectMeta }
func (f *Flunder) NamespaceScoped() bool             { return true }
func (f *Flunder) New() runtime.Object               { return &Flunder{} }
func (f *Flunder) NewList() runtime.Object           { return &FlunderList{} }
func (f *Flunder) GetGroupVersionResource() schema.GroupVersionResource {
    return schema.GroupVersionResource{Group: "wardle.example.com", Version: "v1alpha1", Resource: "flunders"}
}
func (f *Flunder) IsStorageVersion() bool { return true }
```

And the main:

```go
// main.go
import (
    "sigs.k8s.io/apiserver-runtime/pkg/builder"
    v1alpha1 "example.com/wardle/pkg/apis/wardle/v1alpha1"
)

func main() {
    err := builder.APIServer.
        WithResourceAndHandler(&v1alpha1.Flunder{}, FlunderStorageProvider).
        WithLocalDebugExtension().
        Execute()
    if err != nil { log.Fatal(err) }
}
```

`builder.APIServer` is a fluent builder that under the hood wires `RecommendedOptions`, `Scheme`, `genericregistry.Store`, and your `StorageProvider` (which can be etcd, a custom backend, or in-memory).

### 20.2 Why it's less mature

- The library was renamed and re-homed multiple times; older docs reference paths that no longer exist.
- Many community examples still target `kubernetes-sigs/apiserver-builder-alpha`, which is essentially abandoned.
- The CRD path is overwhelmingly dominant; investment in aggregation tooling is small.

For a real production aggregated apiserver, **copy sample-apiserver** rather than reach for apiserver-runtime. Sample-apiserver is part of the kubernetes/kubernetes repo, maintained by SIG API Machinery, and is the closest thing to "the canonical template".

---

## 21. Real-World Aggregated APIs

A non-exhaustive map of who builds aggregated apiservers and why.

| Project | Group(s) served | Storage | Why aggregation |
|---|---|---|---|
| `metrics-server` | `metrics.k8s.io/v1beta1` | In-memory | Synthesized data, no persistence needed |
| `prometheus-adapter` | `custom.metrics.k8s.io/v1beta1`, `external.metrics.k8s.io/v1beta1` | Prometheus | High-cardinality, source-of-truth elsewhere |
| `keda` (metrics adapter) | `external.metrics.k8s.io/v1beta1` | KEDA Scalers | Plurality of backends |
| `openshift-apiserver` | `*.openshift.io` (apps, project, route, image, build, etc.) | etcd (separate prefix) | Legacy ozipped semantics; subresources for image streams |
| `kcp` | `tenancy.kcp.io`, `workload.kcp.io`, `apis.kcp.io` | Custom (workspace-scoped) | Multi-tenant control planes; aggregation per workspace |
| `virtual-kubelet` API extensions | various | Remote (VK provider) | Facade over external scheduler |
| `gardener` aggregated APIs | `core.gardener.cloud`, `seedmanagement.gardener.cloud` | etcd (separate apiserver per shoot) | Hierarchical cluster management |
| `cluster-api-operator` | `operator.cluster.x-k8s.io` | Mostly CRDs, some aggregated for managed-cluster proxy | Multi-cluster facade |
| `tekton-results-api` | `results.tekton.dev` | Postgres | Large objects (build logs, results) — exceeds etcd limit |
| `cost-analyzer-apiserver` (various vendors) | `cost.example.com` | Time-series DB | High cardinality, source-of-truth elsewhere |
| `kueue-visibility` | `visibility.kueue.x-k8s.io` | In-memory | Queue inspection synthesized from CRDs |

### 21.1 Patterns to notice

- **Metrics + autoscaling** dominates. Five of the top ten aggregated apiservers exist because HPA needs an aggregated API surface.
- **Cluster-as-a-service** (Gardener, kcp, OpenShift, vCluster) tends to require aggregation because each tenant gets its own apiserver-like surface, and stuffing those into a shared etcd-as-CRD doesn't scale.
- **Build/CI systems** (Tekton results, internal CI integrations) need aggregation because results are large and many.
- **Cost / observability** integrations need aggregation because the data lives in Prometheus, BigQuery, Snowflake, or whatever.

### 21.2 What you *don't* see in this list

- Application APIs (ScaledObject, Certificate, IngressRoute, …). All CRDs.
- Operator APIs (Postgres, Redis, Kafka, MongoDB cluster CRDs). All CRDs.
- Networking (NetworkPolicy variants, EnvoyFilter, VirtualService). All CRDs.
- Storage (VolumeSnapshot, StorageClass, …). Built-in or CRDs.

If 95% of the extension surface is CRDs, that is the strongest empirical argument for "default to CRD" you can have.

---

## 22. When to Choose Aggregation

A staff engineer's checklist. If three or more of these are true, aggregation is on the table. If only one or two, fix the data model and use a CRD.

```
┌────────────────────────────────────────────────────────────────────────┐
│  REASONS TO CHOOSE AGGREGATION                                          │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  □ Storage isn't etcd                                                   │
│    The data already lives in Postgres, a time-series DB, or a remote   │
│    service, and copying it into etcd doesn't make sense.               │
│                                                                        │
│  □ Object sizes will exceed CRD/etcd limits (1 MiB)                    │
│    Build logs, ML model metadata, line-item billing reports.           │
│                                                                        │
│  □ Cardinality will exceed CRD count limits (~100k)                    │
│    Per-event records, per-request audit, per-pod-per-15s metrics.     │
│                                                                        │
│  □ You need streaming/long-lived subresources                          │
│    `exec`, `attach`, `console`, `proxy`, custom websocket endpoints.   │
│                                                                        │
│  □ Validation can't be expressed in CEL                                │
│    Calls to external policy engines, multi-step graph traversals,      │
│    asynchronous validation against authoritative external systems.    │
│                                                                        │
│  □ You need custom optimistic-concurrency semantics                    │
│    e.g. "update succeeds iff field X has been unchanged for >5s",      │
│    or "atomic compare-and-set on a sub-object that CRDs would store    │
│    as JSON".                                                            │
│                                                                        │
│  □ Read responses are synthesized per request                          │
│    Metrics, current state of an external system, computed views.       │
│                                                                        │
│  □ You're replacing a built-in's storage (rare, dangerous)             │
│    Only for distribution-level overrides like OpenShift.              │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

### 22.1 The two-question filter

If you can answer **YES** to either of the following, you don't need aggregation:

1. "Could I solve this by making the CRD smaller (split into multiple objects, store details in a separate object referenced by name)?"
2. "Could I solve this with a CRD plus a controller that reads the external system and mirrors a summary into the CR's status?"

These two together cover ~70% of cases where a junior team reaches for aggregation. The CRD path is almost always shorter.

### 22.2 The cost calibration

A working aggregated apiserver, in practice:

- 2–3 weeks for a small one (metrics-server-like).
- 3–6 months for a real one with multiple resources, conversion, custom storage.
- 1–2 dedicated engineers on call thereafter (responding to flakes, CA rotations, library upgrades, etc.).

A CRD, in practice:

- 1 day to scaffold with kubebuilder, defaults included.
- 1–2 weeks to ship a real reconciler.
- 0 dedicated engineers — operate alongside other operators on the platform team.

That's a 10–50x cost differential. The aggregation path needs to *clear* that bar in operational benefit before it is the right call.

---

## 23. When to Choose CRD

Everything else.

More usefully, CRDs are the right choice when *any* of these are true (and aggregation isn't forced by §22):

```
┌────────────────────────────────────────────────────────────────────────┐
│  REASONS TO CHOOSE CRDs                                                 │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  □ Standard kubectl, audit, RBAC, server-side apply all need to work   │
│    without extra effort.                                                │
│                                                                        │
│  □ The data fits in etcd (small objects, bounded count).              │
│                                                                        │
│  □ You want zero operational overhead beyond the cluster itself.      │
│                                                                        │
│  □ Schema can be expressed in OpenAPI + CEL.                          │
│                                                                        │
│  □ Subresources you need are `status` and `scale` only.               │
│                                                                        │
│  □ You can model behavior with a controller + reconcile loop.         │
│                                                                        │
│  □ Your audience is "platform users" who expect kubectl to "just      │
│    work" (kubectl edit, kubectl get -o, kubectl explain, etc.).        │
│                                                                        │
│  □ Multi-cluster + GitOps + operator lifecycle (Argo, Flux, OLM)      │
│    all assume CRDs and have first-class support for them.             │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

Note that *both* paths can be combined: a CRD-based configuration surface and an aggregated API for read-only views. KEDA does this; we'll see more of it as platforms mature.

---

## 24. Observability of the Aggregation Layer

Two sets of metrics matter: the *main apiserver's* view of aggregated calls, and the *extension apiserver's* own metrics.

### 24.1 On the main apiserver

```
# Histograms by group/version. Aggregated GVs show up here too.
apiserver_request_duration_seconds{
    verb="LIST",
    group="metrics.k8s.io",
    version="v1beta1",
    resource="pods",
    scope="namespace",
    code="200"
}

apiserver_request_total{group="metrics.k8s.io",version="v1beta1",code="200"}

# How many requests the aggregator proxied (vs handled locally).
aggregator_unavailable_apiservice_total{name="v1beta1.metrics.k8s.io",reason="FailedDiscoveryCheck"}
aggregator_unavailable_apiservice{name="v1beta1.metrics.k8s.io"}  # gauge: currently unavailable

# Discovery probe outcomes.
aggregator_discovery_aggregation_count_total
```

Useful SLO queries:

```promql
# Aggregated API error rate
sum by (group, version) (
  rate(apiserver_request_total{group="metrics.k8s.io",code=~"5.."}[5m])
) /
sum by (group, version) (
  rate(apiserver_request_total{group="metrics.k8s.io"}[5m])
)

# p99 latency for an aggregated GV
histogram_quantile(0.99,
  sum by (le, group, version) (
    rate(apiserver_request_duration_seconds_bucket{group="metrics.k8s.io"}[5m])
  )
)

# Unavailable APIServices right now
sum by (name) (aggregator_unavailable_apiservice)
```

### 24.2 On the extension apiserver

Each extension apiserver (since it builds on `k8s.io/apiserver`) exposes the *same* metric names:

```
apiserver_request_total{...}        # but emitted by metrics-server's process
apiserver_request_duration_seconds{...}
authentication_attempts             # AuthN outcomes
authorization_attempts_total        # AuthZ delegated outcomes
```

You can scrape these directly with Prometheus by adding an annotation or a ServiceMonitor. The label cardinality is similar to the main apiserver's.

### 24.3 Practical SLOs

For a production aggregated apiserver:

| SLO | Target |
|---|---|
| Availability of the APIService | 99.9% (three nines) |
| p99 GET latency | <100ms |
| p99 LIST latency | <300ms |
| Discovery probe success rate | >99.9% |
| CA bundle freshness | renewBefore ≥ 30 days |

For a metrics-style apiserver (read-heavy, synthesized):

| SLO | Target |
|---|---|
| Scrape success rate per node | >99% over 1h |
| Sample freshness | <90s old at p99 |
| LIST latency | <500ms p99 for 1000-pod namespaces |
| Memory per scraped node | <5 MiB |

---

## 25. Pitfalls: The Long List

In rough order of how often I've seen each one.

### 25.1 APIService stuck `FailedDiscoveryCheck`

Almost always one of:

- **CA bundle wrong.** Renewed cert, didn't update bundle. Or cainjector failed silently.
- **Service has no Endpoints.** The Deployment's pods are not Ready (readiness probe failing or pod CrashLooping).
- **TLS SNI / handshake mismatch.** The serving cert's DNS names don't include the in-cluster Service FQDN.
- **wrong port.** APIService says 443, Service says 443, but `targetPort` doesn't reach a listener.

Fix sequence: `kubectl get apiservice <name> -o yaml`, then `kubectl get svc,ep -n <ns> <name>`, then `kubectl logs <pod>`, then reproduce with a `curl` from a debug pod inside the cluster.

### 25.2 RequestHeader credentials misconfigured

The extension apiserver accepts forged headers from any caller. Symptoms: unauthorized users can read/write your resources. Causes:

- `--requestheader-allowed-names` is empty (treats *any* client cert signed by the front-proxy CA as valid).
- The extension apiserver is configured to skip RequestHeader auth (`--authentication-skip-lookup` or similar misuse).
- The wrong CA was loaded — e.g. someone copied the cluster CA into `client-ca-file` instead of the front-proxy CA.

Mitigation: never set those flags by hand. Use `genericoptions.DelegatingAuthenticationOptions` and let the library read the kube-system ConfigMap.

### 25.3 Not delegating AuthN/AuthZ

The extension apiserver re-implements RBAC (or worse, doesn't implement it at all). Now permissions diverge: a user can `kubectl get` on the main apiserver's `pods` but can't `kubectl get` on `pods.metrics.k8s.io` — or vice versa.

Symptoms: confused users, inconsistent permission errors, security holes where someone gets access through one API but not the other.

Fix: use `genericoptions.DelegatingAuthorizationOptions` and create the `system:auth-delegator` ClusterRoleBinding.

### 25.4 Using aggregation when a CRD would do

You spent six months building an aggregated apiserver for an API that has 1000 objects, all under 100 KiB, with simple validation. You could have shipped this in two weeks as a CRD. Now you have a Deployment to operate, a CA to rotate, an APIService to monitor, and a custom storage implementation to debug.

Fix: pre-mortem on the cost vs benefit. Default to CRD; switch to aggregation only when §22 forces your hand.

### 25.5 Single-replica extension apiserver

When the pod restarts (rollout, eviction, OOM, node drain), the entire `GroupVersion` becomes unavailable. HPA on metrics-server stops scaling. Custom dashboards stop loading. Users see 503.

Fix: always run with `replicas: 2` minimum, a PDB with `minAvailable: 1`, and `priorityClassName: system-cluster-critical`.

### 25.6 Not implementing watch

You skipped watch because "it's hard". Now every controller-runtime-based client that touches your API spams the apiserver with errors. They also fall back to polling, which is much more expensive than watch — your apiserver gets hammered.

Fix: implement watch. If you really can't (synthesized data with no event source), document the limitation prominently and configure your clients to use list-only polling with reasonable intervals.

### 25.7 Not implementing pagination

`LIST` returns everything. Works for 100 objects, dies for 10,000. Both client and server allocate huge amounts of memory; the apiserver's `--max-request-body-bytes` limit gets hit; clients OOM.

Fix: support `?limit=` and `?continue=` properly. The `genericregistry.Store` does this for you; a hand-rolled REST handler does not.

### 25.8 etcd prefix collision with main apiserver

You set `--etcd-prefix=/registry` (the default for the main apiserver), and now your writes are colliding with the main apiserver's etcd keys, corrupting the cluster.

Fix: always set a unique `--etcd-prefix=/registry/<your-group>`.

### 25.9 caBundle not rotated

The serving cert was rotated by cert-manager, but `caBundle` in the APIService was managed manually and not updated. APIService flips to `FailedDiscoveryCheck`.

Fix: use cert-manager's cainjector with the `cert-manager.io/inject-ca-from` annotation. Or write a controller that watches the cert and updates the APIService.

### 25.10 Aggregator timeouts too tight for slow backends

Your extension apiserver does a remote call to a backend that occasionally takes 5 seconds. The default request timeout on the main apiserver is 60s, but if you set `--request-timeout=10s` (some hardened setups do), the slow backend's calls time out at the aggregator before your apiserver responds.

Fix: tune `--request-timeout` on the main apiserver (or, better, make your backend fast enough that it doesn't matter).

### 25.11 Watch streams hold connections open across pod replacements

When a metrics-server replica is replaced during a rollout, existing watch streams hang for up to `--terminationGracePeriodSeconds`. Clients eventually see EOF, reconnect, and the new replica answers. Annoying but correct.

Pitfall: setting `terminationGracePeriodSeconds=0` to "make rollouts faster" causes watch reconnect storms. Don't.

### 25.12 Wrong audit policy

The main apiserver's audit policy applies to the aggregator's *outer* view of the request: who, what GVR, response code. The *body* of the request is not audited at the main apiserver level for proxied requests. You need an audit policy *inside* the extension apiserver too.

Fix: configure `--audit-policy-file` and `--audit-log-path` on the extension apiserver, with a policy that captures the resources you care about.

### 25.13 APIService versioning surprises

You ship `v1beta1`, then `v1`. You set `versionPriority` on `v1` higher than `v1beta1`. But you forgot to set the extension apiserver's *internal* storage version to `v1`. Now writes via `v1` are stored as `v1beta1` internally and converted back on read — fine, but conversion bugs cause data corruption.

Fix: storage version is set in the `runtime.Scheme` via `metav1.AddToGroupVersion` and per-resource registration. Test conversion round-trips exhaustively.

### 25.14 Forgetting to expose `/metrics` and pprof

You can't debug what you can't see. The library's `--profiling=true` and the default `/metrics` endpoint give you pprof and Prometheus metrics for free; turn them on. Restrict access via RBAC (the library exposes them under `/metrics` and `/debug/pprof/*`).

### 25.15 Running the extension apiserver on the master nodes

In some custom k8s distributions you can colocate extension apiservers on control plane nodes. Tempting because of "low latency to kube-apiserver". Risky because:

- A misbehaving extension apiserver can starve `kube-apiserver` of CPU/memory.
- Control plane upgrades are now coupled to your extension apiserver's release cycle.
- Tainted control plane nodes need explicit toleration in your Deployment.

Fix: run extension apiservers on worker nodes by default. Use `priorityClassName: system-cluster-critical` for HA.

### 25.16 Forgetting that aggregation adds a hop

The main apiserver → extension apiserver hop adds ~5–20ms to every request. For chatty clients (HPA, autoscalers, kubectl with --watch), this matters. The aggregator does not pool connections aggressively; each in-flight request opens a new HTTP/2 stream.

For latency-critical paths (HPA scaling decisions on tight intervals), expect your aggregated metrics API to be the bottleneck before kube-apiserver is.

### 25.17 Treating "available" as "correct"

`APIService Available=True` only means the discovery endpoint returned 200. It does *not* mean the underlying backend (Prometheus, Postgres, the remote service) is healthy. A metrics adapter can be `Available=True` while returning empty lists because Prometheus is down.

Fix: expose readiness based on backend health, not just on the apiserver being up.

### 25.18 Calling the main apiserver inside hot request paths

Tempting: in your REST handler, call `kubeclient.CoreV1().Pods(ns).List(...)` to look up something. For request rates above ~100 qps, this fights with everyone else for APF budget on the main apiserver.

Fix: run informers and read from caches, like a controller. The `k8s.io/apiserver` library wires `SharedInformerFactory` into your config; use it.

### 25.19 Conversion functions that allocate

Per-request conversion (internal ↔ external version) runs on every read and write. If your conversion functions allocate large slices or do expensive computation, the apiserver's CPU usage scales with QPS in a way you don't expect.

Fix: profile. Use `pprof` to find conversion hot spots. Where possible, make external and internal types share underlying memory.

### 25.20 Versioning fights with downstream tools

You bump `v1alpha1` → `v1beta1`, deprecate `v1alpha1` (set `served: false`), and suddenly ArgoCD breaks because some Application still references `v1alpha1`. The aggregator now returns 404 for `v1alpha1` requests.

Fix: never remove a served version without a deprecation cycle: announce → mark deprecated → wait two minor releases → remove. Same lifecycle as CRDs and built-in APIs.

---

## 26. TL;DR

**Aggregation is the second extension path** (CRDs are the first). You register an `APIService` pointing at a Service that fronts your own apiserver. The main apiserver routes by `GroupVersion`, authenticates the user, strips their credential, and stamps trusted identity headers (`X-Remote-User`, `X-Remote-Group`, `X-Remote-Extra-*`) on the forwarded request. The extension apiserver trusts those headers only when the TLS client cert is signed by `--requestheader-client-ca-file` and the CN is in `--requestheader-allowed-names`. Authorization is then delegated back to the main apiserver via `SubjectAccessReview`. The result: a federated apiserver topology with a single coherent permission model and one user identity propagating across the boundary.

**You build extension apiservers on `k8s.io/apiserver`.** `RecommendedConfig` plus `RecommendedOptions` give you a Kubernetes-grade apiserver in ~250 lines of glue, with AuthN/AuthZ delegation, OpenAPI, admission, audit, watch cache, server-side apply, and graceful shutdown. The five pieces you write are scheme registration, OpenAPI generation, registry/strategy per resource, server chaining, and storage backend. Sample-apiserver (`staging/src/k8s.io/sample-apiserver`) is the reference template; copy it.

**Storage is the load-bearing choice.** Four options: etcd (shared with main apiserver, separate prefix), any database via `storage.Interface`, remote service (no storage — metrics-server pattern), or in-memory. The choice determines what features you get for free (etcd: everything; remote: nothing — implement watch, list, pagination, conversion yourself).

**metrics-server is the canonical aggregated apiserver.** Serves `metrics.k8s.io/v1beta1`, scrapes every kubelet's `/metrics/resource` endpoint, holds samples in memory, synthesizes `NodeMetrics`/`PodMetrics` responses per request. No persistence, no watch, no resource versions. HPA on CPU/memory depends on it. The custom-metrics-apiserver library is the same pattern for HPA's `Pods`/`Object`/`External` metric types — prometheus-adapter, KEDA, Datadog, and a half-dozen cloud adapters all build on it.

**Use aggregation when CRDs hit a wall:** non-etcd storage, objects >1 MiB, cardinality >100k, custom subresources (exec/attach/proxy), validation beyond CEL, synthesized read-only data, or custom optimistic-concurrency. Use CRDs everywhere else, and that "everywhere else" is 95%+ of cases. A working aggregated apiserver costs 10–50x more to build and operate than a CRD-based equivalent; only spend that budget if the §22 forcing functions are real.

**The middleman pattern is what makes this secure.** Raw user credentials never reach the extension apiserver. The only thing crossing the trust boundary is *the assertion that the main apiserver authenticated the user*, signed by the front-proxy-client cert. As long as the front-proxy CA is not compromised and the extension apiserver enforces `--requestheader-allowed-names`, no forge is possible.

**The pitfall hierarchy: CA misconfiguration > single-replica deployments > skipping watch/pagination > using aggregation when a CRD would do > forgotten audit/observability.** The first three are operational; the fourth is a planning miscalculation; the fifth is a maturity gap. Every aggregated apiserver in production should have ≥2 replicas, a PDB, a cert-manager-managed CA, delegated AuthN/AuthZ, watch + pagination support, and its own `/metrics` exposed.

**Aggregation, like CRDs, is *not* a license to invent a new Kubernetes.** It is a *bounded* extension surface: same wire protocol, same auth model, same admission/audit/RBAC story. The reason Kubernetes' extension layer is so widely adopted is that aggregated APIs feel like first-class citizens — `kubectl explain`, `kubectl get`, RBAC, watch, server-side apply all work uniformly. Every shortcut you take (no watch, no pagination, no OpenAPI, no audit) makes your API feel less Kubernetes-native and shifts cognitive load to your users. The library exists to *prevent* those shortcuts; use it.
