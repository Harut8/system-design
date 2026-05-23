# kube-apiserver Internals: The Only Door to etcd

If etcd (ch 04) is the heart of Kubernetes, kube-apiserver is the door. It is the **only** process that talks to etcd. Every controller, every kubelet, every scheduler, every `kubectl`, every webhook, every metric you have ever scraped about cluster state, ultimately reads its data through this one binary. There are no side channels. There are no admin tools that "just go read etcd directly" except in disaster recovery. When kube-apiserver is slow, the cluster is slow. When kube-apiserver is wrong, the cluster is wrong. When kube-apiserver is down, the cluster is down — even if every pod keeps running on every node, no controller can make a single new decision.

This chapter is a staff-level deep dive into kube-apiserver as it actually runs. We walk the request handler chain layer by layer (every filter, in order); we go through the registry + storage layer where "resources" become "rows"; we trace internal-vs-external version conversion as a hub-and-spoke graph; we look at the watch cache as the in-memory ring buffer it really is; we cover server-side apply with the managed-fields model; we untangle the three-apiservers-in-one-binary aggregation chain (kube-aggregator + kube-apiserver + apiextensions-apiserver); we read APF (API Priority and Fairness) like a queueing system; and we end with the metrics, SLOs and pitfalls that you will be paged about. By the end you should be able to: look at `apiserver_request_duration_seconds` and tell which layer the latency lives in; reason about why a 100k-namespace cluster collapses on a single bad `LIST`; write the FlowSchema that protects the apiserver from your noisy controller; and understand exactly which line of Go code is running when a CRD with a conversion webhook is requested at a stale resourceVersion.

Prerequisites: ch 03 (the architecture map), ch 04 (etcd, especially watch + MVCC), and at least skim ch 07 (authn/authz) and ch 06 (admission) — both are layers in the handler chain that we *touch* here but cover end-to-end in their own chapters. Familiarity with Go interfaces and HTTP/2 helps. Some sections refer to source paths under the Kubernetes monorepo `staging/src/k8s.io/apiserver/...` and `staging/src/k8s.io/kube-aggregator/...`; the staging directories are the real source of truth (they get vendored into many other repos), and you can read them directly on GitHub.

---

## Table of Contents

1. [The Role: One Door, Five Jobs](#1-the-role-one-door-five-jobs)
2. [Binary Architecture: Three apiservers in One Process](#2-binary-architecture-three-apiservers-in-one-process)
3. [The Request Handler Chain](#3-the-request-handler-chain)
4. [The Registry and Storage Layer](#4-the-registry-and-storage-layer)
5. [Version Conversion: The Hub-and-Spoke Graph](#5-version-conversion-the-hub-and-spoke-graph)
6. [Storage Encoding: Protobuf, JSON, YAML](#6-storage-encoding-protobuf-json-yaml)
7. [The Watch Cache](#7-the-watch-cache)
8. [List Semantics, ResourceVersion, and Pagination](#8-list-semantics-resourceversion-and-pagination)
9. [Server-Side Apply and Managed Fields](#9-server-side-apply-and-managed-fields)
10. [OpenAPI and Discovery](#10-openapi-and-discovery)
11. [API Priority and Fairness](#11-api-priority-and-fairness)
12. [Audit](#12-audit)
13. [The Aggregation Layer](#13-the-aggregation-layer)
14. [Three-Apiserver Chaining in Detail](#14-three-apiserver-chaining-in-detail)
15. [Performance Characteristics](#15-performance-characteristics)
16. [Observability: Metrics and SLOs](#16-observability-metrics-and-slos)
17. [Pitfalls and Anti-Patterns](#17-pitfalls-and-anti-patterns)
18. [TL;DR](#18-tldr)

---

## 1. The Role: One Door, Five Jobs

kube-apiserver looks like a REST server. That undersells it by a factor of about five. It is more useful to think of it as **five distinct services co-located in one binary**, each of which is non-trivial on its own.

```
                ┌───────────────────────────────────────────────┐
                │            kube-apiserver                     │
                │                                               │
   clients ───▶ │   (1) Stateful-store-talker:                  │ ───▶ etcd
                │       only process that writes to etcd.       │
                │                                               │
                │   (2) Auth boundary:                          │
                │       authN + authZ + impersonation +         │
                │       admission. Nothing else enforces        │
                │       identity in the cluster.                │
                │                                               │
                │   (3) Schema and conversion authority:        │
                │       owns the type registry, defaulters,     │
                │       OpenAPI v2/v3, version conversion.      │
                │                                               │
                │   (4) Watch fan-out engine:                   │
                │       turns one etcd watch into N client      │
                │       watches; coalesces, paginates, bookmarks│
                │                                               │
                │   (5) Discovery server:                       │
                │       what GVRs exist, which verbs, what      │
                │       schemas, where to route them.           │
                └───────────────────────────────────────────────┘
```

### 1.1 Only Stateful-Store Talker

This is the most important property and the source of nearly every operational rule downstream. Controllers do **not** open connections to etcd. Schedulers do not. kubelets do not. They all go through the apiserver's `/api/v1/...` and `/apis/<group>/<version>/...` endpoints. Three consequences:

```
WHY "ONLY APISERVER WRITES TO ETCD" MATTERS

  ─ etcd auth is delegated. We do not need to keep tens of
    thousands of components' x509 client certs synced with etcd.
    We do it once: apiserver↔etcd uses a single mTLS identity.

  ─ Conversion and admission cannot be bypassed. If a controller
    could write directly to etcd, it could skip RBAC, skip
    defaulters, skip schema validation, write a malformed object
    and crash every other client. We never let that happen.

  ─ The "etcd schema" is private. The on-disk encoding (protobuf
    bytes, registered media types, storage version) is an apiserver
    implementation detail. Storage version migrations work because
    nothing else knows or cares.

  ─ The apiserver is the cluster's single concurrency root. Every
    write goes through one etcd transaction with optimistic
    concurrency on resourceVersion; we never have two writers
    racing to the same key.
```

In disaster recovery you may use `etcdctl get /registry/...` to inspect raw bytes. That is a glass-break tool. It bypasses everything in this chapter, and the bytes you see are the protobuf-encoded internal storage form (§6), not the JSON you read in `kubectl get -o yaml`.

### 1.2 Auth Boundary

The apiserver is the entire cluster's authentication and authorization checkpoint. Pod-to-pod traffic is enforced elsewhere (NetworkPolicy, ch 20; service mesh, ch 17). API access — "can this principal create a Pod in namespace `prod`?" — is enforced here and nowhere else. The implications:

- AuthN is **stateless and per-request**. The token, cert, or impersonation header arrives in the HTTP request; the authenticator(s) resolve it to a `user.Info{Name, UID, Groups, Extra}`. There is no session.
- AuthZ is **modular and chained**. Each authorizer (Node, RBAC, ABAC, Webhook) returns `Allow`, `Deny`, or `NoOpinion`. Order matters. The first definite `Allow` or `Deny` wins; `NoOpinion` falls through.
- Admission (mutating, then validating) runs *after* authZ on writes. We never run admission on something we have already refused.

We touch authN/authZ as request-pipeline stages here (§3.4 and §3.5); their full detail is ch 07.

### 1.3 Schema and Conversion Authority

The Go type registry inside the apiserver is the canonical home of "what is a `Pod`?". Three things flow from this:

1. **Defaulters** fill in missing required fields. A `Pod` without `spec.restartPolicy` gets `Always`; a `Service` without `spec.type` gets `ClusterIP`. Defaulters are *not* admission; they are part of the registry and run before storage every time.
2. **Version conversion** — `v1` ↔ `v1beta1` ↔ internal — happens here, both on the wire (a client may PUT v1beta1 against a v1-storage resource) and at read time (object stored in storage version, served in any served version). §5 walks the hub-and-spoke graph.
3. **OpenAPI** — both v2 and v3 — is *generated from* the same Go types. The apiserver serves the schema; `kubectl explain`, `kubectl apply --dry-run=server`, IDE tooling, and CRD admission all consume it.

### 1.4 Watch Fan-Out Engine

A 5000-node cluster has on the order of 50,000–100,000 active watches. The apiserver opens **one** watch per resource type to etcd, decodes each event once, and fans it out across all interested client watches. The watch cache (§7) is the in-memory data structure that makes this efficient: a per-resource ring buffer of recent events plus a snapshot of the current set of objects. Without it, every client watch would translate to a new etcd watch and every relist to an etcd range read; etcd would die in minutes on a real cluster.

### 1.5 Discovery Server

`/api`, `/apis`, `/apis/apps/v1`, and (since 1.27) aggregated discovery (`/apis` returning one big response) are how every client learns "what types live in this cluster?" `kubectl`, controller-runtime, client-go, the dashboard, all start by talking to discovery. Discovery is cheap to serve (it is a memoized in-process map) but it has to stay live as APIServices come and go and CRDs are installed.

---

## 2. Binary Architecture: Three apiservers in One Process

There is no single Go process called "the kube-apiserver" with one HTTP handler. The binary `kube-apiserver` actually starts three logical apiservers and chains them with `Director`-style HTTP routing. This is the single most important architectural fact about the binary, and it explains why CRDs behave subtly differently from built-ins, and why the aggregation layer exists at all.

```
   one Linux process
   ┌───────────────────────────────────────────────────────────────────┐
   │  kube-apiserver binary                                            │
   │                                                                   │
   │   ┌───────────────────────────────┐    receives EVERY request    │
   │   │ kube-aggregator               │    first; routes to one of   │
   │   │  staging/src/k8s.io/          │    the others based on URL.  │
   │   │  kube-aggregator              │                              │
   │   └─────────────┬─────────────────┘                              │
   │                 │                                                 │
   │     ┌───────────┼───────────┐                                     │
   │     │           │           │                                     │
   │     ▼           ▼           ▼                                     │
   │  ┌────────┐  ┌────────┐  ┌──────────────────┐                    │
   │  │remote  │  │ main   │  │ apiextensions-   │                    │
   │  │API     │  │ kube-  │  │  apiserver       │                    │
   │  │servers │  │ api-   │  │  (CRDs)          │                    │
   │  │via     │  │ server │  │  staging/src/    │                    │
   │  │APISvc  │  │(built- │  │  k8s.io/         │                    │
   │  │(metrics│  │  ins:  │  │  apiextensions-  │                    │
   │  │ -server│  │  Pod,  │  │  apiserver       │                    │
   │  │  etc)  │  │  Svc,..)│ │                  │                    │
   │  └────────┘  └────────┘  └──────────────────┘                    │
   │                                                                   │
   │   All three share:                                                │
   │     - the generic apiserver library (genericapiserver.Config)     │
   │     - the request handler chain (§3)                              │
   │     - admission, authN, authZ, audit                              │
   │     - the loopback client (talks to ourselves via in-process loop)│
   │                                                                   │
   └───────────────────────────────────────────────────────────────────┘
```

### 2.1 kube-aggregator: The Front Door

`kube-aggregator` (sources at `staging/src/k8s.io/kube-aggregator/`) is the outermost apiserver. Every HTTP request lands on it first. It owns two resource types of its own — `APIService` (`apiregistration.k8s.io/v1`) — and otherwise behaves as a routing front-end: it inspects the URL, matches it against the set of registered `APIService` objects, and either:

- proxies the request to a registered external apiserver (typical example: `v1beta1.metrics.k8s.io` served by metrics-server), or
- delegates the request to the next apiserver in the chain (the main kube-apiserver).

It also serves the **aggregated discovery document** (1.27+) by stitching together discovery responses from itself, the main apiserver, and every registered APIService.

### 2.2 The Main kube-apiserver: Built-in Types

The main apiserver (sources at `pkg/kubeapiserver/`, `pkg/registry/...`, with most generic code in `staging/src/k8s.io/apiserver/...`) handles every built-in resource: `Pod`, `Service`, `Deployment`, `Job`, `Node`, and so on. Roughly 30 API groups and 200+ resource kinds. Storage version is per-resource (mostly v1; a handful of newer ones at v1beta1 or v1alpha1). All storage goes through one connection pool to etcd.

### 2.3 apiextensions-apiserver: CRDs

`apiextensions-apiserver` (sources at `staging/src/k8s.io/apiextensions-apiserver/`) is the **delegate** of the main apiserver. When a request hits a URL whose group/version is *not* a built-in (e.g. `/apis/cert-manager.io/v1/certificates`), the main apiserver delegates to apiextensions, which:

- looks up the `CustomResourceDefinition` for the group/resource,
- builds (or reuses) a `Handler` for the CRD's storage (which is `Unstructured` JSON in etcd, §6),
- if the CRD has multiple versions, runs the **conversion strategy** (None or Webhook, §5.4),
- runs the per-CRD OpenAPI v3 schema validation, including CEL `x-kubernetes-validations`,
- handles the standard verbs, watch, subresources (`/status`, `/scale`).

CRDs share the registry framework with built-ins (same `genericregistry.Store` skeleton), but with a few crucial differences:

```
       BUILT-IN                              CRD
       ─────────                             ────
  Strongly typed Go struct          Unstructured map[string]any
  Generated conversion funcs        Conversion via webhook (or none)
  Generated defaulter               No defaulter (CRD defaults via schema)
  Protobuf wire+storage             JSON storage; JSON/YAML wire
  Generated OpenAPI                 Schema declared in CRD object itself
  Built-in CEL via @validations     Same CEL via x-kubernetes-validations
  Strategy in pkg/registry/...      Strategy generic, schema-driven
```

That divergence is why CRDs are often slower than built-ins for equivalent shapes: every list/get pays JSON encode/decode + reflective field walks; built-ins pay protobuf + direct struct access.

### 2.4 Chaining via the DelegationTarget

The three apiservers are stitched with the `genericapiserver.DelegationTarget` interface. Conceptually:

```go
// staging/src/k8s.io/apiserver/pkg/server/genericapiserver.go
//
// Each apiserver has a Handler chain. If it does not recognize a
// URL, it forwards to its delegate.
//
// chain:  kube-aggregator
//             ↓ delegate
//         main kube-apiserver
//             ↓ delegate
//         apiextensions-apiserver
//             ↓ delegate
//         404 "not found"
```

The order is fixed: aggregator first (so it can override anything with a registered APIService), main apiserver second (so built-ins always win over CRDs), apiextensions last (CRDs). This ordering is the reason you cannot register a CRD that shadows a built-in: the main apiserver claims the URL first.

§14 walks the chaining behavior in detail with worked examples.

---

## 3. The Request Handler Chain

Every request to the apiserver passes through a chain of HTTP filters before reaching the registry/storage code. Each filter is a `func(http.Handler) http.Handler` decorator; the chain is composed bottom-up at startup. The exact set has shifted across releases, but as of 1.30 the order below is canonical. Each layer has one job; nearly every per-request metric the apiserver exports is attributable to a specific filter.

```
                CLIENT (kubectl, controller, kubelet, scheduler)
                                │
                                │  HTTPS (TLS 1.2/1.3), HTTP/2 multiplex
                                ▼
   ┌────────────────────────────────────────────────────────────────┐
   │  HTTP/2 + TLS termination (Go net/http2)                       │
   ├────────────────────────────────────────────────────────────────┤
   │  WithPanicRecovery        catches panics in lower handlers     │
   ├────────────────────────────────────────────────────────────────┤
   │  WithRequestReceivedTimestamp   stamps t0 (for latency metrics)│
   ├────────────────────────────────────────────────────────────────┤
   │  WithCORS                       (only if --cors-allowed-origins)│
   ├────────────────────────────────────────────────────────────────┤
   │  WithTimeoutForNonLongRunning   default 60s, skipped for watch │
   ├────────────────────────────────────────────────────────────────┤
   │  WithRequestDeadline            attaches ctx.Deadline           │
   ├────────────────────────────────────────────────────────────────┤
   │  WithLogging                                                    │
   ├────────────────────────────────────────────────────────────────┤
   │  WithRequestInfo                parses URL → GVR + verb + obj   │
   ├────────────────────────────────────────────────────────────────┤
   │  WithCacheControl               sets no-cache headers          │
   ├────────────────────────────────────────────────────────────────┤
   │  WithHSTS                       (if --strict-transport-security)│
   ├────────────────────────────────────────────────────────────────┤
   │  WithAuthentication             chained authenticators (§3.4)   │
   ├────────────────────────────────────────────────────────────────┤
   │  WithAudit (begin: RequestReceived stage)                       │
   ├────────────────────────────────────────────────────────────────┤
   │  WithImpersonation              Impersonate-User: header        │
   ├────────────────────────────────────────────────────────────────┤
   │  WithAuthorization              chained authorizers (§3.5)      │
   ├────────────────────────────────────────────────────────────────┤
   │  WithPriorityAndFairness        APF: pick FlowSchema +          │
   │                                   PriorityLevel, queue or       │
   │                                   reject; emits seats (§11)     │
   ├────────────────────────────────────────────────────────────────┤
   │  WithMaxInFlightLimit           (legacy fallback if APF off)    │
   ├────────────────────────────────────────────────────────────────┤
   │  WithWaitGroup                  graceful-shutdown bookkeeping   │
   ├────────────────────────────────────────────────────────────────┤
   │  WithTraces (OpenTelemetry)     starts a span per request       │
   ├────────────────────────────────────────────────────────────────┤
   │  Generic API handler   ────────────────────────────────────┐    │
   │     ─ Dispatch by verb (GET/LIST/WATCH/CREATE/...)         │    │
   │     ─ Decode request body (JSON / YAML / protobuf)         │    │
   │     ─ Convert external version → internal version          │    │
   │     ─ Run defaulters                                       │    │
   │     ─ Mutating admission                                   │    │
   │     ─ Validate (schema + CEL)                              │    │
   │     ─ Validating admission                                 │    │
   │     ─ Strategy.Validate / PrepareForCreate (§4)            │    │
   │     ─ Storage.Create/Update (etcd or watch-cache read)     │    │
   │     ─ Convert internal → response version                  │    │
   │     ─ Encode response                                      │    │
   ├────────────────────────────────────────────────────────────┘    │
   │  WithAudit (end: ResponseStarted, ResponseComplete)             │
   └────────────────────────────────────────────────────────────────┘
                                │
                                ▼
                              CLIENT
```

You can find the precise composition at `staging/src/k8s.io/apiserver/pkg/server/config.go` in the function `DefaultBuildHandlerChain`. The function builds the chain inside-out: every `With...` wraps what came before it.

### 3.1 TLS Termination and HTTP/2

The listener is plain Go `net/http2`. kube-apiserver enforces:
- TLS 1.2 minimum (1.3 preferred), default cipher suites from the Go stdlib (`--tls-cipher-suites` to restrict).
- HTTP/2 by default. Watches multiplex on a single TCP connection; this is the reason a misbehaving slow watch can stall other watches from the same client.
- Client cert auth handshake happens at TLS time. The cert is later passed up to the authenticator chain; the chain decides whether to honor it.
- SNI routing via `--tls-sni-cert-key` to serve different certs to different client hostnames (mostly used in hosted control planes).

HTTP/2's multiplexing is critical and dangerous: one client opens one TCP connection and may run dozens of streams. Bad behavior on one stream (a giant `LIST`, a slow consumer) can backpressure or stall others on the same connection. APF (§11) is partly a response to this.

### 3.2 Panic Recovery and Timeouts

`WithPanicRecovery` catches any panic in handlers below it, logs it with a stack trace, returns 500, and increments `apiserver_request_total{code="500"}`. Without it, a single bad CRD schema or a corrupt object could crash the process.

`WithTimeoutForNonLongRunning` enforces a default 60-second timeout per request. It is bypassed for "long-running" verbs (`WATCH`, `PROXY` for exec/portforward/attach, log streaming). Without this, a single hung etcd write could pin a goroutine forever. The 60s default is set via `--request-timeout`.

`WithRequestDeadline` attaches a context deadline so any code further down can `select` on `ctx.Done()`. APF, storage, conversion, admission webhooks all respect this.

### 3.3 Request Info Parsing

`WithRequestInfo` is one of the most important filters. It parses the URL and stamps a `RequestInfo` struct onto the request context:

```go
// staging/src/k8s.io/apiserver/pkg/endpoints/request/requestinfo.go
type RequestInfo struct {
    IsResourceRequest bool
    Path              string
    Verb              string   // get, list, watch, create, update, patch, delete, deletecollection, proxy, connect
    APIPrefix         string   // "api" or "apis"
    APIGroup          string   // "" for core, else group
    APIVersion        string
    Namespace         string
    Resource          string   // plural
    Subresource       string   // "status", "scale", or ""
    Name              string
    Parts             []string
}
```

URL `/api/v1/namespaces/prod/pods/web-7df` parses to `Verb=get Resource=pods Namespace=prod Name=web-7df`. Every later filter uses this struct. Authorizers consume it; APF flow distinguishers consume it; audit consumes it; the dispatch into the registry uses it. If `IsResourceRequest=false`, we are hitting `/healthz`, `/metrics`, `/openapi/v2`, etc — those skip most of the chain.

### 3.4 Authentication

The authenticator chain is built from `--authentication-token-webhook-config-file`, `--oidc-issuer-url`, `--client-ca-file`, and the static `ServiceAccount` issuer. It runs as a single composite authenticator (`union.New(authenticators...)`), in registration order, returning the **first** successful identity. The standard order in stock kube-apiserver:

```
   1. RequestHeader (X-Remote-User from a trusted front proxy;
      used by the aggregation layer — §13)
   2. Client certificate (x509)
   3. Bootstrap tokens
   4. ServiceAccount tokens (legacy + projected/bound)
   5. OIDC token (--oidc-issuer-url)
   6. Webhook token (--authentication-token-webhook-config-file)
   7. Anonymous (if --anonymous-auth=true; default true; denied
      almost always at authZ)
```

On success the request context gets a `user.Info` (Name, UID, Groups, Extra). Failure short-circuits with 401. Detail of each authenticator is ch 07.

### 3.5 Authorization

The authorizer chain typically looks like:

```
   1. Node authorizer (only allows kubelets to read/write the
      objects pertaining to their own node — Pods bound to me,
      Secrets mounted by those pods, ConfigMaps, etc.)
   2. RBAC authorizer (evaluates ClusterRoleBinding +
      RoleBinding edges in a precomputed graph)
   3. Webhook authorizer (optional; --authorization-webhook-config-file)
```

It is a `union` of authorizers, but with different semantics from authN: each authorizer returns `(decision, reason, error)` where `decision ∈ {Allow, Deny, NoOpinion}`. The chain short-circuits on the first `Allow` or `Deny`; only `NoOpinion` falls through. `--authorization-mode=Node,RBAC` (the default in kubeadm clusters) yields:

- A Node-bound kubelet hits Node first; if it is asking about its own Pods, `Allow`. If asking about another node's, `Deny` (not NoOpinion). It is a positive decision.
- Anything else: Node says `NoOpinion`, falls through to RBAC.
- RBAC denies via `NoOpinion` (not `Deny`). So if RBAC has no matching rule, the answer is "no Allow" → final result `Deny`.

This subtlety matters when you write your own webhook authorizer: returning `Deny` overrides everything downstream; returning `NoOpinion` lets the next authorizer decide.

### 3.6 Impersonation

If the request includes `Impersonate-User`, `Impersonate-Group`, `Impersonate-Uid`, or `Impersonate-Extra-<key>` headers, the impersonation filter runs after authentication and after the *first* authorization. It:

1. Checks that the *original* identity has the `impersonate` verb on the specified user/group/uid/extra.
2. If allowed, replaces the request's `user.Info` with the impersonated one and re-runs the authorization for the actual operation.

This is what `kubectl --as` uses, and it is how operators safely run as users they do not own credentials for, for audit purposes.

### 3.7 APF and the Old Max-In-Flight

We cover APF in depth in §11. Briefly: every request is classified into a `FlowSchema`, which routes it to a `PriorityLevelConfiguration`. Each priority level has a configurable concurrency budget; if its queues are full, the request is rejected with 429. If APF is disabled (`--enable-priority-and-fairness=false`), the legacy `WithMaxInFlightLimit` filter applies a simple bucket: read-concurrency vs mutating-concurrency.

### 3.8 WaitGroup, Trace, Audit, Dispatch

`WithWaitGroup` increments a shared `sync.WaitGroup` for the lifetime of each request. On `SIGTERM`, the apiserver stops accepting new connections, waits for in-flight requests up to `--shutdown-delay-duration` + the longest request timeout, then exits. Without this, graceful shutdown would lose in-flight writes.

`WithTraces` starts an OpenTelemetry span if `--tracing-config-file` is set. The span links to per-storage-call spans for etcd, which makes "this request was slow because etcd was slow" attributable.

`WithAudit` is split: it emits a `RequestReceived` event at chain entry (before AuthN — so failed authN is still audited) and `ResponseStarted` / `ResponseComplete` / `Panic` events at chain exit. We cover the four stages and four levels in §12.

After all filters, dispatch is by URL: the generic handler looks up the GVR in its registered set, finds the matching `Storage` object (a `genericregistry.Store` for built-ins, an `apiextensions` handler for CRDs), and invokes a verb method. Decoding, conversion, defaulting, admission, and storage all happen inside that handler.

### 3.9 An Annotated `kubectl --v=9` Trace

The clearest way to see the chain is `kubectl --v=9`, which logs the full HTTP request and response. Here is `kubectl get pods -n prod web-7df` (whitespace trimmed):

```
I0523 ...   GET https://api.cluster.example/api/v1/namespaces/prod/pods/web-7df 200 OK in 7 ms
I0523 ...   Request Headers:
I0523 ...     Accept: application/json
I0523 ...     User-Agent: kubectl/v1.30.0 (linux/amd64)
I0523 ...     Authorization: Bearer eyJhbGciOiJSUzI1NiIs...
I0523 ...   Response Headers:
I0523 ...     Audit-Id: 7a4b2c... (correlation id, set by WithAudit)
I0523 ...     Content-Type: application/json
I0523 ...     X-Kubernetes-Pf-Flowschema-Uid: system-leader-election (APF)
I0523 ...     X-Kubernetes-Pf-Prioritylevel-Uid: leader-election
```

And here is a write (`kubectl apply -f svc.yaml --server-side --field-manager=me --v=9`):

```
I0523 ...   PATCH https://api.cluster.example/api/v1/namespaces/prod/services/web?
                  fieldManager=me&force=false
            Content-Type: application/apply-patch+yaml
            Body: apiVersion: v1
                  kind: Service
                  metadata: { name: web }
                  spec: { selector: {app: web}, ports: [{port: 80}] }
I0523 ...   200 OK in 23 ms
I0523 ...   X-Kubernetes-Pf-Flowschema-Uid: workload-low
I0523 ...   X-Kubernetes-Pf-Prioritylevel-Uid: workload-low
```

Note the headers `X-Kubernetes-Pf-Flowschema-Uid` and `X-Kubernetes-Pf-Prioritylevel-Uid` — APF stamps every response with which flow + priority level handled the request. Invaluable for debugging "why am I being rate-limited?".

For comparison, a watch:

```
GET .../api/v1/pods?watch=true&allowWatchBookmarks=true&resourceVersion=123456&timeoutSeconds=580
   ─ Transfer-Encoding: chunked
   ─ each event is a JSON line: {"type":"ADDED","object":{...}}
   ─ stays open ~10 minutes (timeoutSeconds), then must reconnect
```

`allowWatchBookmarks=true` is critical for well-behaved clients (§7.3). Without it, you have no way to confirm "I have seen everything up to RV X" without doing a fresh LIST.

### 3.10 An End-to-End Trace: One Pod Create

Walking a `kubectl run nginx --image=nginx` (which becomes a Pod create) through every filter, with the exact code path:

```
   T+0  TCP SYN, then TLS ClientHello to 0.0.0.0:6443
        ─ Go net/http2 picks ALPN h2
        ─ TLS handshake: server presents --tls-cert-file, optionally
          requests client cert (--client-ca-file)
   T+8ms HTTP/2 stream 1 opened:
        POST /api/v1/namespaces/default/pods?fieldManager=kubectl-create
        Content-Type: application/json
        Body: {"kind":"Pod", "metadata":{...}, "spec":{...}}

   T+9ms WithPanicRecovery wraps everything below in defer/recover.
   T+9ms WithRequestReceivedTimestamp stamps ctx with t=9ms.
   T+9ms WithTimeoutForNonLongRunning installs a 60s deadline on ctx.
   T+9ms WithLogging: structured log line at v=4.
   T+10ms WithRequestInfo:
            verb="create", group="", version="v1", resource="pods",
            namespace="default", subresource="", isResourceRequest=true
          ctx now carries *RequestInfo
   T+10ms WithCacheControl sets "Cache-Control: no-cache, private".
   T+11ms WithAuthentication (union of authenticators):
            tries x509 (no client cert seen) → NotAuthenticated
            tries SA bearer token → NotAuthenticated
            tries OIDC → matches; user.Info{Name=alice@corp,
                                           Groups=[ops, system:authenticated]}
            ctx now carries user.Info
   T+11ms WithAudit (RequestReceived): emits {Audit-ID=uuid,
            stage=RequestReceived, user=alice, verb=create,
            resource=pods, namespace=default}
   T+11ms WithImpersonation: no Impersonate-User header, no-op.
   T+12ms WithAuthorization:
            Node authorizer: user is not system:nodes, NoOpinion.
            RBAC authorizer: search graph for (alice, create, pods,
              ns=default). Find ClusterRoleBinding "edit-prod-default"
              binding RoleRef "edit" → grants pods:create. Allow.
   T+13ms WithPriorityAndFairness:
            match FlowSchemas in matchingPrecedence order:
              system-leader-election: subjects don't match. NEXT.
              ... (skip)
              global-default: matches (no specific FS for this user).
            Route to PriorityLevel "global-default".
            Pick queue via shuffle-sharding on user="alice"
              (handSize=8, totalQueues=128).
            Seats available? yes (3/19 in use). Admit immediately.
            Stamp response headers:
              X-Kubernetes-Pf-Flowschema-Uid: global-default
              X-Kubernetes-Pf-Prioritylevel-Uid: global-default
   T+13ms WithWaitGroup: wg.Add(1). Will wg.Done() on defer.
   T+13ms WithTraces: start span "create pods" with parent from
            traceparent header (if any).
   T+13ms Generic handler dispatch:
            handler.Handler() resolves the Storage for pods.v1.
            storage = restStorage["pods"] (a *podstore.REST)

   T+14ms Decode body:
            negotiated decoder for application/json + apps "" + v1.
            Output: &v1.Pod{...}

   T+14ms Convert v1.Pod → api.Pod (internal):
            Convert_v1_Pod_To_core_Pod(in, out, nil)
            Internal form is now in memory.

   T+15ms Default:
            Default_Pod(internal):
              - if spec.restartPolicy == "" → "Always"
              - if spec.dnsPolicy == "" → "ClusterFirst"
              - if every container missing imagePullPolicy:
                  if image tag is "latest" or empty → "Always"
                  else → "IfNotPresent"
              - inject default tolerations
              - generate UID if not set

   T+16ms Strategy.PrepareForCreate(ctx, internal):
            - clear status (status is read-only on create)
            - generate creationTimestamp
            - if name empty: pod.GenerateName produces "nginx-XXXXX"
            - strip readOnlyFields

   T+17ms Mutating admission (sequential, per-webhook):
            - LimitRanger plugin: apply LimitRange defaults to
              resources.requests / limits.
            - PodSecurity plugin: NO mutation; just lookup
              namespace label for enforcement.
            - Webhook "istio-sidecar-injector":
                POST https://injector.istio-system.svc:443/inject
                body: AdmissionReview{ Request: {object: internal as JSON} }
                wait ≤ 5s
                response: patch (JSONPatch) adding sidecar container,
                          init container, volumes, annotations.
                apply patch to internal.
            - Webhook "kyverno":
                no mutations for this rule.
            Each webhook contributes to apiserver_admission_webhook_
            admission_duration_seconds{name=...}.

   T+45ms Schema validation:
            run OpenAPI v3 against the *external* form (re-encode
            internal back to v1 for validation, or use the cached
            schema-aware validator). Reject malformed fields.

   T+46ms CEL validation (built-in x-kubernetes-validations):
            For Pod, mostly built-in Go validation. Newer
            policies (ValidatingAdmissionPolicy) eval CEL programs
            in-process. Reject on violation.

   T+47ms Validating admission:
            - PodSecurity: enforce per-namespace mode
              (privileged/baseline/restricted). May reject.
            - ResourceQuota: check that creating this pod won't
              exceed the namespace's quota. If quota.status.used +
              new request > hard, reject.
            - Webhook "kyverno":
                POST https://kyverno-svc:443/validate
                wait ≤ 5s
                response: allowed=true or allowed=false with message.
            Each webhook MUST be idempotent; mutating already ran.

   T+70ms Strategy.Validate(ctx, internal):
            Final structured field validation. Returns field.ErrorList.
            Errors → 422 Unprocessable Entity with detailed paths.

   T+71ms Strategy.Canonicalize(internal):
            normalize: sort tolerations, sort env (no — env is ordered),
            normalize volume order where ordering doesn't matter.

   T+71ms Storage.Create(ctx, key="/registry/pods/default/nginx-abcde",
                          obj=internal,
                          out=&api.Pod{},
                          ttl=0):
            ─ encoder: Convert_core_Pod_To_v1_Pod → v1.Pod
                      then protobuf-marshal → bytes
                      wrap with "k8s\0" magic + GVK header
            ─ etcd txn:
                Compare(Version("/registry/pods/default/nginx-abcde") == 0)
                Then  (Put(key, bytes))
                Else  (Get(key))
              ↑ "create if not exists" semantics
            ─ etcd round trip ~3ms (single-DC)
            ─ on success: etcd assigns revision 8421337
                          modRevision -> resourceVersion="8421337"

   T+76ms Watch fan-out begins:
            cacher's reflector receives the etcd watch event
            for revision 8421337. It:
              - puts event into ring buffer
              - notifies all subscribed watchers
              - each subscriber's filter (selector, namespace) is
                evaluated; matches get the event on their channel
            Subscribers include:
              - kube-scheduler (watches all unscheduled pods)
              - endpoint(slice) controller (watches all pods)
              - deployment controller (this isn't owned by one, skip)
              - kubelet on every node (watches pods bound to it;
                won't match yet — spec.nodeName=="")

   T+78ms Convert internal → v1 for response:
            Convert_core_Pod_To_v1_Pod
            then JSON-encode (the request was JSON).

   T+79ms WithTraces: end span. apiserver_request_duration_seconds
                      observes 0.070s.
                      apiserver_request_total{verb=create,
                      resource=pods, code=201} += 1.
   T+79ms WithWaitGroup: wg.Done().
   T+79ms WithAudit (ResponseStarted, then ResponseComplete):
            emits ResponseComplete event with code=201, response
            body if level=RequestResponse for this rule.
   T+80ms Response written to client:
            HTTP/2 status 201, headers:
              Audit-ID: <uuid>
              X-Kubernetes-Pf-Flowschema-Uid: global-default
              Content-Type: application/json
            body: {kind: Pod, metadata:{name:nginx-abcde,
                   resourceVersion:"8421337", uid:"..."}, ...}

   T+80ms HTTP/2 stream closes. ctx cancellation propagates.
```

This is a normal-case create. Total apiserver-side wall time was 70ms; the dominant chunk (T+17 → T+45) was the mutating admission webhook call to istio. That ~30ms is typical and is why slow webhooks are catastrophic at scale — every Pod create eats them in series.

### 3.11 Failure Modes Per Layer

```
   FAILURE                          LAYER              SYMPTOM
   ─────────────────────────────    ─────────────      ────────────
   TLS handshake failed             TLS                "x509: ..."
                                                       in apiserver log
   Cert valid, but unknown user     AuthN              401
   Known user, no rights            AuthZ              403
   Rights ok, but oversubscribed    APF                429 with retry
   Admission webhook timeout        Generic handler    500/504 with
                                    + admission        webhook detail
   Stored object cannot decode      Conversion         500, "no kind ...
                                                       is registered"
   etcd unhealthy                   Storage            504, "etcdserver:
                                                       request timeout"
   Watch dropped                    Watch cache        410 "Gone" — old
                                                       resourceVersion
```

The takeaway: the HTTP status code and the response body almost always pinpoint the layer. `apiserver_request_total{code=...}` decomposed by `verb,resource` is the single most useful triage metric.

---

## 4. The Registry and Storage Layer

Below the filter chain, every resource (built-in or CRD) is served by an instance of `genericregistry.Store` wired to a `storage.Interface` backed by etcd. This is where the layering of "REST verb → strategy → encoding → etcd call" actually lives.

```
                            ┌────────────────────────────────┐
                            │   REST endpoints (verb dispatch)│
                            │   /api/v1/...     /apis/.../    │
                            └───────────────┬────────────────┘
                                            │
                                            ▼
                            ┌────────────────────────────────┐
                            │   genericregistry.Store        │
                            │     - CreateStrategy           │
                            │     - UpdateStrategy           │
                            │     - DeleteStrategy           │
                            │     - TableConvertor           │
                            └───────────────┬────────────────┘
                                            │
                          ┌─────────────────┼─────────────────┐
                          ▼                 ▼                 ▼
                  ┌───────────────┐ ┌───────────────┐ ┌───────────────┐
                  │PrepareForCreate│ │ Validate      │ │ Canonicalize  │
                  │PrepareForUpdate│ │ ValidateUpdate│ │               │
                  │AllowCreateOn   │ │               │ │               │
                  │  Update        │ │               │ │               │
                  └───────────────┘ └───────────────┘ └───────────────┘
                                            │
                                            ▼
                            ┌────────────────────────────────┐
                            │   storage.Interface            │
                            │     Get / GetList / Create     │
                            │     Update / Delete / Watch    │
                            └───────────────┬────────────────┘
                                            │
                          ┌─────────────────┼─────────────────┐
                          ▼                 ▼                 ▼
                  ┌───────────────┐ ┌───────────────┐ ┌───────────────┐
                  │ etcd3 backend │ │ cacher.Cacher │ │ (alternates)  │
                  │ (raw store)   │ │ (watch cache) │ │  test only    │
                  └───────────────┘ └───────────────┘ └───────────────┘
```

### 4.1 The Strategy Interface

`Strategy` is the contract that says "what does it mean to write a Pod?" Every built-in resource defines its own; CRDs share a generic schema-driven one. The interface lives at `staging/src/k8s.io/apiserver/pkg/registry/rest/`.

```go
// staging/src/k8s.io/apiserver/pkg/registry/rest/create.go
type RESTCreateStrategy interface {
    runtime.ObjectTyper
    names.NameGenerator

    NamespaceScoped() bool
    // PrepareForCreate is invoked on create before validation.
    // Used to set defaults that depend on context (e.g. clear
    // status from user-provided input).
    PrepareForCreate(ctx context.Context, obj runtime.Object)

    Validate(ctx context.Context, obj runtime.Object) field.ErrorList
    WarningsOnCreate(ctx context.Context, obj runtime.Object) []string

    Canonicalize(obj runtime.Object) // last-chance normalization
}

// staging/src/k8s.io/apiserver/pkg/registry/rest/update.go
type RESTUpdateStrategy interface {
    runtime.ObjectTyper
    NamespaceScoped() bool
    // AllowCreateOnUpdate => PUT /foo/x with no existing object
    // is treated as create. true for some resources (e.g. ConfigMap
    // historically) and false for most.
    AllowCreateOnUpdate() bool
    // AllowUnconditionalUpdate => PUT without resourceVersion.
    // For Pods, false (you must do CAS).
    AllowUnconditionalUpdate() bool

    PrepareForUpdate(ctx context.Context, obj, old runtime.Object)
    ValidateUpdate(ctx context.Context, obj, old runtime.Object) field.ErrorList
    WarningsOnUpdate(ctx context.Context, obj, old runtime.Object) []string

    Canonicalize(obj runtime.Object)
}
```

The key invariants:

```
   PrepareForCreate     ─ runs AFTER decoding, BEFORE admission.
                          Strips fields that users may not set
                          (e.g. status), sets generated fields
                          (creationTimestamp, UID).
   PrepareForUpdate     ─ same but for updates; sees both new
                          and old. Strips fields that may not be
                          modified after create (e.g. nodeName
                          on Pod, except via /binding).
   Validate /           ─ run AFTER admission, before storage.
   ValidateUpdate         These are the LAST line of defense.
                          They are NOT skippable. They return
                          structured field.Errors.
   Canonicalize         ─ runs LAST. Sorts slices into a
                          canonical order, normalizes optional
                          fields. Important so that consecutive
                          writes do not produce spurious
                          resourceVersion bumps.
```

For Pod, the Pod-specific strategy lives at `pkg/registry/core/pod/strategy.go`. The `PrepareForCreate` for Pod strips `status`, clears `spec.nodeName` (unless the request is via the `/binding` subresource), sets default tolerations, etc. The `Validate` enforces a thousand small rules (volume names unique, container names DNS-label, port ranges).

For CRDs, the Strategy is generic and reads its rules from the CRD's OpenAPI v3 schema + `x-kubernetes-validations` CEL expressions (§9 of ch 06).

### 4.2 The Store

`genericregistry.Store` at `staging/src/k8s.io/apiserver/pkg/registry/generic/registry/store.go` is the glue. Each resource constructs one at apiserver startup, like:

```go
// pkg/registry/core/pod/storage/storage.go (simplified)
store := &genericregistry.Store{
    NewFunc:                  func() runtime.Object { return &api.Pod{} },
    NewListFunc:              func() runtime.Object { return &api.PodList{} },
    DefaultQualifiedResource: api.Resource("pods"),
    CreateStrategy:           pod.Strategy,
    UpdateStrategy:           pod.Strategy,
    DeleteStrategy:           pod.Strategy,
    TableConvertor:           printers.NewTableGenerator(...),
}
options := &generic.StoreOptions{
    RESTOptions: optsGetter,
    AttrFunc:    pod.GetAttrs,
    TriggerFunc: map[string]storage.IndexerFunc{
        "spec.nodeName": pod.NodeNameTriggerFunc,
    },
}
if err := store.CompleteWithOptions(options); err != nil { ... }
```

The `Store` exposes the verbs: `Create`, `Update`, `Delete`, `DeleteCollection`, `Get`, `List`, `Watch`, plus `New`/`NewList`. Each verb calls the strategy at the right points, then calls `storage.Interface`.

### 4.3 storage.Interface

`storage.Interface` (`staging/src/k8s.io/apiserver/pkg/storage/interfaces.go`) is the abstraction over etcd:

```go
type Interface interface {
    Create(ctx context.Context, key string, obj, out runtime.Object, ttl uint64) error
    Delete(ctx context.Context, key string, out runtime.Object,
           preconditions *Preconditions, validateDeletion ValidateObjectFunc,
           cachedExistingObject runtime.Object) error
    Watch(ctx context.Context, key string, opts ListOptions) (watch.Interface, error)
    Get(ctx context.Context, key string, opts GetOptions, out runtime.Object) error
    GetList(ctx context.Context, key string, opts ListOptions, listObj runtime.Object) error
    GuaranteedUpdate(ctx context.Context, key string, destination runtime.Object,
           ignoreNotFound bool, preconditions *Preconditions,
           tryUpdate UpdateFunc, cachedExistingObject runtime.Object) error
    Count(key string) (int64, error)
    Versioner() Versioner
}
```

Two real implementations:

- **etcd3**: at `staging/src/k8s.io/apiserver/pkg/storage/etcd3/`. Talks to etcd directly via the etcd v3 client. Translates GVR + namespace + name into an etcd key (`/registry/pods/prod/web-7df`), serializes the object (protobuf or JSON depending on storage media type, §6), and issues `Put` / `Get` / `Range` / `Watch` calls.
- **cacher.Cacher**: at `staging/src/k8s.io/apiserver/pkg/storage/cacher/`. Wraps the etcd3 backend. Reads (Get/List/Watch) are served from in-memory state where allowed; writes pass through to etcd3. This is the watch cache (§7).

`GuaranteedUpdate` is the optimistic-concurrency primitive: read object, apply `tryUpdate` function, write back with CAS on resourceVersion, retry on conflict (bounded). This is how every PATCH and most UPDATEs work internally.

### 4.4 The CRD Storage Path

For CRDs, the path differs:
- The generic Store is wired with an `Unstructured` `NewFunc`.
- The Strategy is the generic `apiextensions` strategy that walks the CRD's OpenAPI schema for validation.
- The `storage.Interface` is still cacher → etcd3, but the encoder/decoder serializes/deserializes JSON (not protobuf) because CRDs do not have generated Go types.
- The etcd key is `/registry/<group>/<resource>/<namespace>/<name>`, e.g. `/registry/cert-manager.io/certificates/prod/api-tls`.

This shared-but-divergent path is also why CRD validation and conversion landed late in Kubernetes' history: the generic Store had to be retrofitted to support a non-typed shape.

### 4.5 GuaranteedUpdate, Reproduced

The `GuaranteedUpdate` function is the heart of every PATCH and most UPDATEs. It is worth reading in pseudocode form because it explains 100% of "why did my update get retried" and "why is my counter racing".

```go
// staging/src/k8s.io/apiserver/pkg/storage/cacher/cacher.go
//                                        and etcd3/store.go (real impl)
//
// GuaranteedUpdate keeps trying until either tryUpdate returns
// an error, the CAS succeeds, or we hit a configured retry cap.
func GuaranteedUpdate(
    ctx context.Context,
    key string,
    destination runtime.Object,
    ignoreNotFound bool,
    preconditions *Preconditions,
    tryUpdate UpdateFunc,
    cachedExistingObject runtime.Object,
) error {

    // 1. Fetch the current object.
    obj := cachedExistingObject
    if obj == nil {
        obj, err := backend.Get(key)
        if err != nil && !(ignoreNotFound && IsNotFound(err)) {
            return err
        }
    }

    for attempt := 0; ; attempt++ {
        // 2. Apply preconditions (UID match, etc).
        if err := preconditions.Check(obj); err != nil {
            return err
        }

        // 3. Run the user's mutation.
        newObj, err := tryUpdate(obj)
        if err != nil { return err }

        // 4. If nothing changed, fast-path.
        if reflect.DeepEqual(obj, newObj) {
            *destination = obj
            return nil
        }

        // 5. Marshal new object.
        bytes := encoder.Encode(newObj)
        currentRV := newObj.ResourceVersion

        // 6. Compare-and-swap via etcd txn.
        txn := etcd.Txn().
            If(etcd.Compare(etcd.ModRevision(key), "=", currentRV)).
            Then(etcd.Put(key, bytes)).
            Else(etcd.Get(key))

        resp := txn.Commit()
        if resp.Succeeded {
            *destination = newObj
            return nil
        }

        // 7. CAS failed; refresh obj from the txn's Else branch
        //    and loop. Cap retries to avoid unbounded contention.
        obj = decode(resp.Responses[0].Get())
        if attempt > maxRetries {
            return errors.New("retry budget exhausted")
        }
    }
}
```

Three insights:

1. The retry loop is **inside the apiserver**. A client doing a PATCH does not see the retries; it sees the final result. This is essential for SSA semantics where the apply patch must succeed despite concurrent updates from other field managers.
2. The `tryUpdate` closure is invoked on every retry. If your closure has side effects (it shouldn't), they happen multiple times. The closure must be pure.
3. The cap on retries is intentional. Under extreme contention (every reconcile of every controller is fighting for the same key), `GuaranteedUpdate` returns an error and the caller backs off via the workqueue's rate limiter.

### 4.6 Subresources

A subresource is a named endpoint that operates on a portion of an object. The two most common:

```
   /api/v1/namespaces/<ns>/pods/<name>/status     subresource "status"
   /apis/apps/v1/namespaces/<ns>/deployments/<name>/scale  subresource "scale"
```

Why subresources matter:

- **RBAC scoping**: `verbs: ["update"]` on `resources: ["pods/status"]` grants the right to update Pod status (used by kubelet) without granting the right to update Pod spec. Without subresources, kubelet would need full pod update permission and could overwrite spec.
- **Different validation**: status updates skip spec-validation (a controller writing status should not have to pass user-facing validation rules for spec it isn't touching). Strategy implementations have separate `statusStrategy.PrepareForUpdate` and `statusStrategy.ValidateUpdate`.
- **Different storage path**: the storage object is the same, but the registry routes update-status to a different code path that only mutates the status subtree.

`/scale` is special: it accepts a `Scale` object (a minimal type with `spec.replicas` and `status.replicas`) regardless of the underlying resource (Deployment, ReplicaSet, StatefulSet, even CRDs that declare a scale subresource). This is how HPA scales arbitrary workloads with one code path.

CRDs declare subresources via `spec.versions[].subresources`:

```yaml
spec:
  versions:
  - name: v1
    subresources:
      status: {}
      scale:
        specReplicasPath: .spec.replicas
        statusReplicasPath: .status.replicas
        labelSelectorPath: .status.selector
```

When the CRD has `status: {}`, the apiserver enforces that spec updates do not modify `.status` and that status updates do not modify `.spec`. This is the bedrock of "controllers own status, users own spec" — not a convention, an enforcement.

### 4.7 DryRun

`?dryRun=All` runs the full request pipeline (admission, validation, conversion) but **does not write to etcd**. It is exactly the same code path up to the storage call. Two uses:

- `kubectl apply --dry-run=server` to preview a mutation, including admission webhook output.
- Internal consistency checks: a controller that wants to "would this mutation work?" without committing.

DryRun is not a magic switch: it is plumbed through every admission webhook (each webhook gets `dryRun=true` in the AdmissionReview and is expected to behave idempotently and side-effect-free). A webhook that ignores `dryRun` and writes to an external system on every call is a bug.

### 4.8 Built-in vs CRD: A Side-by-Side

```
   Step                  Built-in (Pod)            CRD (Certificate)
   ─────                 ─────────────────         ──────────────────────
   Wire decode           protobuf or JSON          JSON or YAML
                         → typed api.Pod           → Unstructured
   Strategy              pod.Strategy (Go)         CRD-derived (CEL+schema)
   Conversion            generated funcs           webhook (or none)
   Validate              hand-written Go           OpenAPI v3 + CEL
   Storage encoder       protobuf                  JSON
   etcd key              /registry/pods/...        /registry/<grp>/<res>/...
   Default storage       v1                        spec.versions[].storage
                                                   (exactly one true)
```

---

## 5. Version Conversion: The Hub-and-Spoke Graph

Kubernetes APIs are versioned. `apps/v1`, `apps/v1beta2`, `batch/v1`, `batch/v1beta1`, all coexist. At the same time, the apiserver stores each resource at exactly **one** storage version. How do we reconcile "client sends v1beta1, storage is v1, response in v1beta2"? With a hub-and-spoke graph.

```
                                  ┌───────────────────┐
                                  │   wire: v1beta1   │
                                  │     external      │
                                  └────────┬──────────┘
                                           │
                                           │ generated
                                           │ Convert_v1beta1_To_internal
                                           ▼
            ┌───────────────────┐    ┌───────────────────┐    ┌───────────────────┐
            │   wire: v1        │───▶│  __internal__     │◀───│   wire: v2        │
            │     external      │    │      HUB          │    │     external      │
            └───────────────────┘    │                   │    └───────────────────┘
                ▲                    │  NOT served       │
                │ Convert_internal_  │  NOT stored       │
                │   To_v1            │  in-memory only   │
                │                    │                   │
                │                    │  All defaulters   │
                │                    │  + validators run │
                │                    │  here.            │
                │                    └─────────┬─────────┘
                │                              │
                │                              │ storage version
                │                              │ Convert_internal_To_<storage>
                │                              ▼
                │                    ┌───────────────────┐
                │                    │  storage: v1      │
                │                    │  (encoded as      │
                │                    │   protobuf in     │
                │                    │   etcd)           │
                │                    └───────────────────┘
                │
                └─── always: response gets converted back to the
                     version the URL asked for (NOT the storage
                     version).
```

### 5.1 Internal Version: The Hub

For every group/resource that has multiple versions, the apiserver defines an **internal version** (`__internal`), which is a Go struct that is the *superset* of all external versions' fields. It is:
- never served on the wire,
- never stored in etcd,
- used as the common in-memory representation while admission, defaulting, and validation run.

For built-ins, you can find it at `pkg/apis/<group>/types.go` (e.g. `pkg/apis/apps/types.go` for Deployment). The external versions live at `staging/src/k8s.io/api/<group>/<version>/types.go` (e.g. `staging/src/k8s.io/api/apps/v1/types.go`).

### 5.2 Generated Conversion Functions

For every external version X and the internal hub, two functions exist:

```go
// staging/src/k8s.io/api/apps/v1/zz_generated.conversion.go
func Convert_v1_Deployment_To_apps_Deployment(in *v1.Deployment, out *apps.Deployment, s conversion.Scope) error
func Convert_apps_Deployment_To_v1_Deployment(in *apps.Deployment, out *v1.Deployment, s conversion.Scope) error
```

These files are generated by `k8s.io/code-generator`'s `conversion-gen` tool. The generator infers the obvious "copy field by field" transformations and you write Go for the non-obvious ones (`Convert_v1beta1_PodSpec_To_apps_PodSpec` for fields that were renamed or restructured).

### 5.3 The Conversion Flow Per Request

A `PUT /apis/apps/v1beta2/.../deployments/foo` against a v1-storage deployment goes:

```
   client wire bytes (v1beta2)
        │
        ▼ Decoder picks v1beta2 scheme
   v1beta2.Deployment in memory
        │
        ▼ Convert_v1beta2_Deployment_To_apps_Deployment
   apps.Deployment (internal)
        │
        ▼ defaulter runs on internal form
   apps.Deployment with defaults filled
        │
        ▼ admission, validation, strategy.PrepareForUpdate
   apps.Deployment ready to store
        │
        ▼ Convert_apps_Deployment_To_v1_Deployment
   v1.Deployment (storage version)
        │
        ▼ encoder writes protobuf bytes
   bytes to etcd
```

Reads run the reverse path: bytes → v1 → internal → response version. If you `GET /apis/apps/v1beta1/.../deployments/foo`, the response is v1beta1 even though storage is v1. This is the entire reason `kubectl convert` and the deprecation policy work.

The cost: every request to a multi-version resource pays at least two conversion calls. For built-ins these are generated, cheap, and almost free. For CRDs they go over the network (§5.4) and are emphatically not free.

### 5.4 CRD Conversion: Webhooks

CRDs do not have generated conversion functions because their Go shape is `Unstructured`. Instead, the CRD declares:

```yaml
spec:
  conversion:
    strategy: Webhook       # or "None"
    webhook:
      conversionReviewVersions: ["v1"]
      clientConfig:
        service:
          name: cert-manager-webhook
          namespace: cert-manager
          path: /convert
        caBundle: ...
```

With `strategy: None`, the only valid case is that every field in every version maps identity-to-identity (the schemas are byte-identical except for the version label). With `strategy: Webhook`, every conversion is an out-of-process call:

```
   client GET /apis/cert-manager.io/v1beta1/.../foo
        │
        ▼ apiserver fetches from etcd in storage version v1
   bytes (v1)
        │
        ▼ POST to webhook with:
            { request: {desiredAPIVersion: "cert-manager.io/v1beta1",
                        objects: [<v1 object>] } }
        ▼ webhook returns:
            { response: {convertedObjects: [<v1beta1 object>] } }
        ▼
   v1beta1 to client
```

Implications:

- A LIST that returns 10,000 CRD objects via a multi-version CRD with a conversion webhook is **10,000 webhook calls** (batched in a single HTTP request, but still serialized + decoded by the webhook).
- A slow conversion webhook is a slow apiserver. There is a 30-second per-call hard limit, but at 30s your LIST is already timing out.
- The webhook must be CA-pinned and TLS-served. CertManager-style chicken-and-egg ("the webhook is for CertManager which manages its own cert") is solved with bootstrap certs.

The right answer for any CRD that is going to grow past trivial scale is to **declare a single storage version and freeze the schema** before scaling. Conversion webhooks should be transient (during a v1alpha1 → v1 promotion), not permanent.

### 5.5 Default Conversion vs Round-Trip Fidelity

Two subtle points:

- **Defaulters** are bound to the *external* version, not the internal one. `staging/src/k8s.io/api/apps/v1/defaults.go` says "if v1.Deployment.spec.strategy.type is empty, set RollingUpdate". When converting v1beta1 → internal, the v1beta1 defaulters do not fire. When converting internal → v1 → wire on response, the v1 defaulters do fire on the way out.
- **Round-trip fidelity** is a unit-test invariant: convert v1 → internal → v1 must equal the original (modulo legitimate defaulting). Conversion code in Kubernetes has a comprehensive `RoundTrip` test framework specifically to catch lossy conversions. When a new external version drops or renames a field, the conversion code must store the dropped data in an annotation or fail at compile time.

---

## 6. Storage Encoding: Protobuf, JSON, YAML

Two different encoding axes:

- **Wire format**: what the client sends and what the apiserver returns over HTTP.
- **Storage format**: what the apiserver writes to etcd.

They are independent. The wire format is negotiated via HTTP `Accept` and `Content-Type`. The storage format is per-resource configuration, fixed at apiserver startup.

### 6.1 Wire Formats

Apiserver supports:

```
   application/json                  default, human-readable
   application/yaml                  same shape as JSON but YAML
   application/vnd.kubernetes.protobuf
                                     binary protobuf; what client-go
                                     uses by default for built-ins.
                                     CRDs do not support this — the
                                     server falls back to JSON.
```

Built-ins all have generated `*.pb.go` files (e.g. `staging/src/k8s.io/api/core/v1/generated.pb.go`); a Pod serializes to ~30–40% the size of JSON.

`kubectl` uses JSON for human-friendly errors; controllers use protobuf for speed. You can force JSON with `--content-type=application/json` for debugging.

### 6.2 Storage Formats

Built-ins are stored as **protobuf** in etcd. The etcd key looks like `/registry/pods/prod/web-7df`; the value is a protobuf-encoded `runtime.Unknown` wrapper containing a TypeMeta + the protobuf bytes of the object in the storage version.

CRDs are stored as **JSON** (technically: a JSON document wrapped in the same `runtime.Unknown`). Because the runtime type is `Unstructured` and there is no `*.pb.go` for it, protobuf is not an option. JSON storage costs are 2–3× larger and slower to decode than protobuf — yet another reason a 100k-object CRD performs differently from 100k built-in objects.

You can verify the storage format by inspecting etcd directly:

```
$ ETCDCTL_API=3 etcdctl --endpoints=... \
        get /registry/pods/default/nginx --print-value-only \
        | hexdump -C | head -1
00000000  6b 38 73 00 0a 0c 0a 02  76 31 12 06 50 6f 64 49 |k8s.....v1..PodI|
                                                          ^^ "k8s" magic +
                                                             gvk header
```

The "k8s\0" prefix is the magic byte sequence the apiserver uses to detect "is this stored object a typed object". Beyond it lies the protobuf payload.

### 6.3 Storage Version Migration

When a built-in is promoted (v1beta1 → v1) and you change the storage version, existing objects in etcd are still encoded against the old version. Two things keep this working:

1. **Conversion on read**: every object read from etcd is converted to internal, then to the requested response version. The "storage version" is just the version the encoder *writes*; the decoder copes with any registered version.
2. **Storage Version Migration controller** (the `StorageVersionMigrator`, alpha→beta in recent releases): periodically scans objects and re-writes them in the current storage version. Without it, an object created at v1beta1 stays encoded as v1beta1 forever, even after the cluster has moved to v1 storage. That mostly works — until you want to drop v1beta1 from the scheme entirely (in which case you must migrate first).

For CRDs, the storage version is whichever entry in `spec.versions[]` has `storage: true`. Exactly one entry may have it. Changing storage version on a live CRD requires a similar migration step.

### 6.4 Object Size Limits

- etcd's per-request value size limit (`--max-request-bytes`) is 1.5 MiB by default. The apiserver inherits this as the hard upper bound on a single object.
- Real-world cluster pain starts at ~250 KiB per object. Pods with thousands of env vars; ConfigMaps holding embedded TLS chains; Events accumulating long messages: all are common offenders.
- A 1-MiB object in a 10k-object LIST means 10 GiB of memory pressure on the apiserver. The watch cache holds these in RAM.

We say more in §15 (perf) and §17 (pitfalls).

---

## 7. The Watch Cache

The watch cache is the apiserver's in-memory layer that turns "watch the world" from a quadratic problem into a linear one. Without it, every client `watch` would open a fresh etcd watch and every `LIST` would be a fresh etcd range read; the apiserver-to-etcd connection would be the bottleneck of the cluster.

The cache lives at `staging/src/k8s.io/apiserver/pkg/storage/cacher/`. Its primary type is `Cacher`:

```
        ┌──────────────────────────────────────────────────┐
        │  Cacher  (one per resource, per apiserver process)│
        ├──────────────────────────────────────────────────┤
        │                                                  │
        │   ┌───────────────────────────────────────────┐  │
        │   │  Reflector                                │  │
        │   │   ─ opens ONE watch to etcd at boot       │  │
        │   │   ─ ListAndWatch loop                     │  │
        │   │   ─ pumps events into cacheWatcher        │  │
        │   └───────────────────┬───────────────────────┘  │
        │                       │                          │
        │                       ▼                          │
        │   ┌───────────────────────────────────────────┐  │
        │   │  storeWatcher / cacheWatcher              │  │
        │   │   ─ thread-safe store of current objects  │  │
        │   │     (indexed by namespace + name +        │  │
        │   │     custom indexers like spec.nodeName)   │  │
        │   │   ─ ring buffer of recent events          │  │
        │   │     (default 100, can grow to 10000)      │  │
        │   │   ─ bookmark generator (every ~minute)    │  │
        │   └───────────────────┬───────────────────────┘  │
        │                       │                          │
        │              ┌────────┴────────┐                 │
        │              ▼                 ▼                 │
        │  ┌─────────────────┐  ┌─────────────────┐        │
        │  │ client watch    │  │ client watch    │  ...   │
        │  │  ─ subscriber to│  │  ─ subscriber to│        │
        │  │    ring buffer  │  │    ring buffer  │        │
        │  │  ─ filters by   │  │  ─ filters by   │        │
        │  │    selector     │  │    selector     │        │
        │  └─────────────────┘  └─────────────────┘        │
        │                                                  │
        └──────────────────────────────────────────────────┘
```

### 7.1 The ListAndWatch Loop

At apiserver startup, for each resource, the cacher issues a `List` against etcd to populate its store, capturing the etcd revision `R0`. It then opens a watch from `R0+1`. From that point on, every mutation to that resource type lands in the ring buffer in revision order.

A client `watch` request, in the simplest case, attaches as a subscriber to the buffer. It receives a *snapshot* of the current state (if the client passed `resourceVersion=""` or `=0` with appropriate semantics) followed by a tail of subsequent events. The cacher tracks each subscriber's high-water-mark resource version.

### 7.2 The Ring Buffer

The ring buffer is per-resource and holds recent events in a fixed-size FIFO. Default size is 100; the apiserver dynamically grows it under load up to a configurable maximum. The buffer is what lets a client "rewind" to a known resourceVersion: as long as that RV is still in the buffer, the cacher can replay events from there. If not, the client receives `410 Gone` and must do a fresh `LIST`.

```
   recent events ring (newest on right):
   [RV=10501 (Pod ADD)] [RV=10502 (Pod MOD)] [RV=10503 (Pod DEL)] ...

   client subscribes at RV=10500:
     ─ if 10500 is in the buffer (after compaction) → replay from there
     ─ if 10500 is older than buffer head → 410 Gone, expects relist
```

This is why "informers stay healthy across short network blips" but die after long ones: the buffer can replay seconds-to-minutes of history, not hours.

### 7.3 Bookmarks

A bookmark is a watch event with `Type: BOOKMARK` and an empty object body, carrying only a resourceVersion. The cacher emits one to each subscribed watcher periodically. Bookmarks let a well-behaved client confirm "I have observed every event up to this RV" without doing a LIST. When the client reconnects (or restarts), it can pass `resourceVersion=<lastBookmark>` and resume cheaply.

Clients opt in via the query parameter `?allowWatchBookmarks=true`. client-go's reflector does this automatically. If you write your own watch client and skip this, you cannot safely reconnect from a known revision; you must re-list.

### 7.4 List from Cache vs List from etcd

Lists are where the cache shines. With `resourceVersion=0` (the client-go default for most LIST calls), the apiserver serves from the watch cache — no etcd hit, just an in-memory filter and copy. This is the entire reason a cluster can survive 5000 controllers all opening informers at the same time.

The catch: `resourceVersion=0` permits stale data. The cache may lag etcd by a handful of milliseconds (or seconds, in pathological cases). For "I just wrote object X and want to read it back" you want a stronger guarantee (§8).

### 7.5 Per-Resource Indexers

The cache supports custom indexers for a resource. For Pods, the cacher indexes by `spec.nodeName`, so a `kubelet` opening `?fieldSelector=spec.nodeName=node-2` can pull only its pods directly from the index — no scan of the full set. Without this, every kubelet's `LIST pods?fieldSelector=...` would be O(cluster pods).

```go
// pkg/registry/core/pod/strategy.go
func NodeNameIndexFunc(obj interface{}) ([]string, error) {
    pod, ok := obj.(*api.Pod)
    if !ok { return nil, fmt.Errorf("not a pod") }
    return []string{pod.Spec.NodeName}, nil
}

// wired via TriggerFunc in the StoreOptions (§4.2)
```

Indexers are not free: every event updates every indexer. They are worth it for high-cardinality, frequently-queried fields (Pod by node, Lease by holder, EndpointSlice by service).

### 7.6 Memory Footprint

The cache holds every object of its resource type in memory. For Pods, that is potentially hundreds of thousands of objects at ~10 KiB each — single-GiB scale.

On large clusters you can observe this directly with `apiserver_storage_objects{resource="pods"}` and `process_resident_memory_bytes`. A 5000-node cluster with default settings runs the apiserver at ~10–20 GiB; most of that is the watch cache for Pods, Events, Endpoints, Leases, and (often) ConfigMaps.

The `--watch-cache-sizes` flag lets you tune the ring buffer per resource. The cache size for the *store* (number of objects held) is implicit — it is the entire set of objects.

You can also turn the watch cache off per-resource (`--watch-cache=false` or `--watch-cache-sizes=resource#0`). Don't. The only reason to do so is debugging.

### 7.7 Why Per-apiserver, Not Cluster-Wide

In an HA control plane with three apiservers, each apiserver has its **own** watch cache. They are *not* synchronized with each other — each independently watches etcd and pumps the events into its cache. Two consequences:

- A client that reconnects to a different apiserver after a network blip may see the cache at a slightly different resourceVersion. The client must pass `resourceVersion` to keep the read monotonic; the new apiserver will wait until its cache catches up to that RV before serving (or return 410 if it cannot).
- Memory cost is per-apiserver. Three apiservers means three full copies of cluster state in RAM.

### 7.8 ConsistentList Beta and the Future

A subtle point: a `LIST` served from the cache with `resourceVersion=0` is not linearizable. For workloads where staleness matters, the apiserver historically had only "skip the cache, hit etcd for a quorum read", which is expensive. Recent work (`ConsistentList` feature, beta in 1.31) lets the cache serve linearizable lists by holding the response until the cache is known to be at least as fresh as the latest etcd revision. The mechanism is to track the etcd revision watermark, do an etcd `Get(/, count-only)` to learn the current revision, and stall the list until the cache catches up. Watch carefully for this in your version's release notes.

---

## 8. List Semantics, ResourceVersion, and Pagination

`LIST` is the operation that takes down apiservers. It is also the operation that controllers and `kubectl` issue most. Knowing its semantics in detail is staff-level.

### 8.1 The resourceVersion Parameter

`?resourceVersion=` on a LIST changes consistency:

```
   resourceVersion="" (unset)         linearizable. apiserver reads
                                      from etcd with a quorum read.
                                      Slowest. Used when the client
                                      MUST see the latest state.

   resourceVersion="0"                may be served from the watch
                                      cache. May be stale by a few
                                      ms. Cheap. Default for
                                      controller informers' initial
                                      LIST.

   resourceVersion="12345"            "I want at least RV 12345".
                                      Apiserver waits for the watch
                                      cache to catch up to 12345
                                      (with a short timeout) and
                                      then serves. Used by informers
                                      after reconnect.
```

### 8.2 The resourceVersionMatch Parameter

Added in 1.19, `?resourceVersionMatch=` makes the semantics explicit:

```
   resourceVersionMatch=NotOlderThan  with resourceVersion=R:
                                      serve at any revision >= R.
                                      cheap; default semantic.

   resourceVersionMatch=Exact         with resourceVersion=R:
                                      serve exactly at R. Required
                                      for pagination consistency.
                                      May fail if R is no longer
                                      available (compaction).
```

For pagination (§8.4), `Exact` is what you want for snapshot consistency.

### 8.3 LabelSelector and FieldSelector

The most expensive LIST is `LIST pods` (no selectors) on a large cluster. The cheapest is `LIST pods?fieldSelector=spec.nodeName=node-N` because of the indexer.

LabelSelector pushdown to the storage layer is partial: the apiserver applies the selector after pulling rows from the cache. The cache holds all objects in memory anyway, so this is mostly a CPU cost (allocation + copy + filter). For label selectors that match a tiny subset of a huge collection, that cost is dominated by allocating the response objects, not by filtering.

### 8.4 Pagination

A `LIST` with `?limit=500` returns at most 500 objects and a `metadata.continue` token if there are more:

```
   $ kubectl get pods --chunk-size=500 -A
        page 1: GET .../pods?limit=500
                       returns {items: [500 pods], continue: "<opaque>"}
        page 2: GET .../pods?limit=500&continue=<opaque>
                       returns {items: [500 pods], continue: "<next>"}
        ...
        last  : GET .../pods?limit=500&continue=<opaque>
                       returns {items: [...], continue: ""}
```

The continue token is opaque (base64-encoded JSON of `{resourceVersion, startKey}`). Pagination is **resourceVersion-pinned**: the apiserver pins the LIST to one revision and walks etcd keys lexicographically. This is why `resourceVersionMatch=Exact` matters: every page must be at the same revision.

Two failure modes:

- **Compaction during pagination**: if etcd compacts past the pinned RV between pages, the continue request returns 410 Gone. Client must restart.
- **Cache miss for the pinned RV**: if pagination uses `resourceVersion=""` (the default in older `kubectl`), each page is a separate etcd read; if you pass a specific RV, the cache may or may not have it.

In practice, well-behaved clients chunk LIST at 500 by default. `kubectl get pods -A --chunk-size=0` disables chunking and is a common cause of "kubectl falls over on a big cluster".

### 8.5 Exact Counts and the `--watch-list` Feature

`apiserver_storage_objects{resource="..."}` is the exact count. A `LIST` does **not** return a count separately; if you want one, you fetch the full list. (Some specialty subresources like `?fieldSelector=...&resourceVersion=0&limit=1` are used as approximations.)

The newer `WatchList` feature (beta in 1.30) is a watch-first list: instead of doing `LIST then WATCH`, the client opens `WATCH ?sendInitialEvents=true`. The apiserver streams every existing object as `ADDED` events, then a bookmark with `metadata.annotations["k8s.io/initial-events-end"]="true"`, then the live stream. This bypasses the cost of materializing one giant LIST response — events are streamed one at a time. Modern client-go uses this when the server supports it.

---

## 9. Server-Side Apply and Managed Fields

Server-Side Apply (SSA) is the mechanism by which multiple controllers and humans can co-own different fields of the same object, with conflict detection. It superseded the old client-side `kubectl apply` (which kept a last-applied annotation as a JSON blob and did a three-way merge client-side, with all its known pathologies).

### 9.1 The Field Manager Model

Every field in every object can be "owned" by a named field manager. Ownership is recorded in `metadata.managedFields`:

```yaml
metadata:
  name: web
  managedFields:
  - manager: kubectl-edit
    operation: Update
    apiVersion: apps/v1
    time: "2025-05-22T10:00:00Z"
    fieldsType: FieldsV1
    fieldsV1:
      f:spec:
        f:replicas: {}
  - manager: deploy-controller
    operation: Apply
    apiVersion: apps/v1
    time: "2025-05-22T10:05:00Z"
    fieldsType: FieldsV1
    fieldsV1:
      f:spec:
        f:template:
          f:spec:
            f:containers:
              k:{"name":"web"}:
                f:image: {}
```

`fieldsV1` is a structured representation of "this manager owns these paths". The keys are: `f:<field>` for object fields, `k:<jsonkey>` for list-element-by-key, `v:<value>` for list-element-by-value, `i:<index>` for list-element-by-index. The intent is that a list of containers (where each has a unique `name`) is an associative list, not an ordered one; SSA merges by name.

```
        ┌─────────────────────────────────────────────────────┐
        │  Object state                                       │
        │    spec.replicas=5                                  │
        │    spec.template.spec.containers[name=web].image=v2 │
        └─────────────────────────────────────────────────────┘
                                │
                                │
       ┌────────────────────────┼─────────────────────────┐
       ▼                        ▼                         ▼
   kubectl-edit          deploy-controller            HPA controller
   (owns: replicas)      (owns: image)                (owns: replicas)
       │                        │                         │
       │                        │                         │
       └────────────────────────┴─────────────────────────┘

   Conflict: BOTH kubectl-edit AND HPA claim spec.replicas.
   SSA detects this on the second writer and either:
       - rejects with 409 Conflict, OR
       - if request includes ?force=true, transfers ownership.
```

### 9.2 The Apply Operation

A Server-Side Apply request is a special PATCH:

```
   PATCH .../deployments/web
   Content-Type: application/apply-patch+yaml
   ?fieldManager=deploy-controller
   ?force=false

   body: a partial object containing only the fields THIS manager
         wants to assert.
```

The apiserver:

1. Decodes the partial object.
2. Builds a "fieldset" describing which paths the request asserts.
3. For each path:
   - If no other manager owns it → take ownership, set value.
   - If this manager already owns it → set value.
   - If another manager owns it AND the new value differs → conflict.
     - if `force=false`: respond `409 Conflict` with a list of conflicting paths.
     - if `force=true`: take ownership from the other manager, set value.
4. For paths previously owned by this manager but NOT in the new request → release ownership; if no one else owns the path, **delete the value** (this is how SSA removes fields).
5. Updates `managedFields` accordingly.

The "release means delete" rule is the killer feature. With client-side apply, removing a field from your YAML did nothing — the old value stayed. With SSA, removing the field releases ownership, and if nobody else owns it, the field is unset on the next apply.

### 9.3 The Three-Way Merge

The merge is, conceptually:

```
   prev_owned_paths = managedFields[manager].paths
   new_owned_paths  = paths_in_request
   existing_object  = stored

   for path in new_owned_paths:
       set existing_object[path] = request[path]
       if path was owned by other manager AND values differ:
           conflict unless force=true

   for path in prev_owned_paths - new_owned_paths:
       drop ownership of path
       if no manager owns path:
           unset existing_object[path]

   managedFields[manager] = new_owned_paths
```

The result is then written back as a normal update (so admission and validation run as usual).

### 9.4 Why SSA Exists

Three problems with the legacy `kubectl apply`:

1. **Last-applied annotation drift**: the annotation could be wrong (someone did `kubectl edit` in between), making the three-way merge wrong.
2. **No conflict detection**: if HPA changed `spec.replicas` to 10, then you applied a YAML with `replicas: 3`, you would silently overwrite HPA.
3. **Field removal was magic**: removing a field from YAML did not necessarily remove it from the object.

SSA makes ownership explicit. Controllers declare themselves field managers (e.g. `manager: my-operator`); humans use `kubectl apply --server-side --field-manager=alice`. Conflicts are surfaced; removal is principled.

Most modern operators built on controller-runtime use SSA exclusively. The `Patch(ctx, obj, client.Apply, ...)` call writes the operator's view as an SSA patch.

### 9.5 The `--field-validation` Story

A related but separate feature: `--field-validation=Strict|Warn|Ignore` causes the apiserver to reject (or warn about) requests with unknown fields. This catches typos like `spec.repplicas` that would silently be discarded. As of 1.27, `Warn` is the default.

---

## 10. OpenAPI and Discovery

The apiserver exposes the entire API surface as machine-readable schemas. Three endpoints matter:

```
   /openapi/v2                 single-document OpenAPI 2.0 (Swagger).
                               Legacy; large. ~12 MiB for a stock cluster.
   /openapi/v3                 OpenAPI 3.0, split per-GroupVersion.
                               /openapi/v3 returns an index;
                               /openapi/v3/apis/apps/v1 returns the
                               schemas for that GV only.
                               Much smaller per request.
   /api  /apis  /apis/<g>/<v>  Discovery: what GVRs exist, what verbs,
                               what subresources. The hot path for
                               every kubectl invocation.
   /apis  (aggregated, 1.27+)  Aggregated discovery: one response
                               containing every group's info, cacheable
                               with ETag/Last-Modified.
```

### 10.1 OpenAPI v2 and v3

OpenAPI v2 was the original schema. It is monolithic; `kubectl` downloads it once and caches it. Every cluster startup, every `kubectl apply` from a fresh shell, pays that download cost (typically ~12 MiB compressed).

OpenAPI v3 is the modern replacement, split per GroupVersion. The index at `/openapi/v3` lists each GV with an ETag-friendly URL:

```
   GET /openapi/v3
   {
     "paths": {
       "apis/apps/v1": {
         "serverRelativeURL": "/openapi/v3/apis/apps/v1?hash=8f3b2a1c..."
       },
       "api/v1": {
         "serverRelativeURL": "/openapi/v3/api/v1?hash=2e7c1d49..."
       }
     }
   }
```

The `?hash=...` query parameter means the URL is immutable; once a client fetches it, it can cache forever. New schemas get new hashes.

`kubectl explain --recursive deployment.spec.template` uses OpenAPI v3 to render structured field documentation. `kubectl apply --dry-run=server` uses it to validate without going to the apiserver.

### 10.2 Discovery

`/api/v1` returns the list of resources in the core group at v1:

```
GET /api/v1
{
  "kind": "APIResourceList",
  "groupVersion": "v1",
  "resources": [
    {"name":"pods","namespaced":true,"kind":"Pod","verbs":["create","delete","deletecollection","get","list","patch","update","watch"]},
    {"name":"pods/status","namespaced":true,"kind":"Pod","verbs":["get","patch","update"]},
    {"name":"pods/log","namespaced":true,"kind":"Pod","verbs":["get"]},
    ...
  ]
}
```

`kubectl` calls discovery on startup, builds a map `Kind → GVR`, and uses it for every URL it constructs. A misbehaving discovery endpoint makes `kubectl` slow for everyone.

### 10.3 Aggregated Discovery

Introduced beta in 1.27, GA in 1.30. `/apis` returns a single response with every group, version, and resource. With ETag support, a 304 Not Modified is common; the response can be cached aggressively. This collapses what used to be ~50 separate discovery calls into one.

```
GET /apis
If-None-Match: "<previous etag>"

→ 304 Not Modified (cached)
   OR
→ 200 OK
   {
     "kind": "APIGroupDiscoveryList",
     "items": [
       {"metadata":{"name":"apps"}, "versions":[{
         "version":"v1",
         "resources":[{"resource":"deployments", ...}, ...]
       }]},
       ...
     ]
   }
```

For very large CRD-heavy clusters (1000s of CRDs), aggregated discovery is the difference between `kubectl get` taking 200ms vs 5s.

---

## 11. API Priority and Fairness

APF replaced the old `--max-requests-inflight` / `--max-mutating-requests-inflight` flags with a structured, multi-tenant fairness system. Two new objects:

- `FlowSchema` (flowcontrol.apiserver.k8s.io/v1): matches incoming requests by (user, group, verb, resource, namespace) and assigns them to a PriorityLevel. Has a `distinguisherMethod` (e.g., per-user, per-namespace) for sharding.
- `PriorityLevelConfiguration` (same group): defines a concurrency budget and queueing behavior.

```
                   request
                      │
                      ▼
           ┌─────────────────────┐
           │  match FlowSchemas  │  in order of matchingPrecedence (lowest first).
           │  by RequestInfo +   │  Each FS has a set of subjects (users/groups/SAs)
           │  user.Info          │  and rules (resources/verbs).
           └──────────┬──────────┘
                      │
                      ▼  (yields PriorityLevelConfiguration name)
           ┌─────────────────────┐
           │ PriorityLevel       │  has:
           │   ─ assuredConcurr  │   - assuredConcurrencyShares (how many seats this PL gets)
           │     encyShares      │   - limited.limitResponse: Queue or Reject
           │   ─ queueing config │   - if Queue: queues, queueLengthLimit, handSize
           └──────────┬──────────┘
                      │
                      ▼
           ┌─────────────────────┐
           │ Shuffle-sharding    │  distinguisher = ByUser, ByNamespace, etc.
           │ to a specific queue │  same distinguisher → same queue. Different
           │ within the PL       │  distinguishers spread across queues (handSize).
           └──────────┬──────────┘
                      │
                      ▼
           ┌─────────────────────┐
           │ Execute when a seat │  request consumes seat(s) for its duration.
           │ is available, else  │  LIST/WATCH consume MORE seats based on width
           │ wait in queue, else │  (object count estimate). Mutations consume 1.
           │ 429 if queue full   │  When done, seats are released.
           └─────────────────────┘
```

### 11.1 Built-in FlowSchemas

A stock cluster ships with a set of system FlowSchemas (priority: lower = checked first):

```
   FlowSchema                   PriorityLevel              Subjects
   ────────────────────────     ─────────────────────      ──────────────────────
   exempt                       exempt                     system:masters
   system-leader-election       leader-election            renews on Lease objects
   workload-leader-election     leader-election            user-defined leases
   system-node-high             node-high                  kube-apiserver internal
   system-nodes                 system                     system:nodes (kubelets)
   kube-controller-manager      workload-high              kube-system SAs
   kube-scheduler               workload-high              kube-scheduler SA
   global-default               global-default             everything else (humans)
   catch-all                    catch-all                  fallback
```

Tracking which FlowSchema matched is critical for triage. The response header `X-Kubernetes-PF-FlowSchema-UID` and the metric `apiserver_flowcontrol_dispatched_requests_total{flow_schema, priority_level}` both expose it.

### 11.2 PriorityLevel Concurrency

PriorityLevels do not have an absolute concurrency cap; they have **shares**. The apiserver computes total available concurrency from `--max-requests-inflight` + `--max-mutating-requests-inflight` (yes, those flags still exist as the global budget). Each PL gets a slice of the budget proportional to its `nominalConcurrencyShares`. Within a PL, requests queue up to `queueLengthLimit`; beyond that, 429.

```
   global budget        = 400 (default: 200 read + 400 mutating fold into ~400)
   leader-election      = 10 shares  → 100/410 * 400 ≈ 9 seats
   system               = 30 shares  → ~ 29 seats
   workload-high        = 40 shares  → ~ 38 seats
   workload-low         = 100 shares → ~ 97 seats
   global-default       = 20 shares  → ~ 19 seats
   catch-all            =  5 shares  → ~  4 seats
   exempt               = unlimited
```

Tuning shares is a real knob: a runaway controller in `workload-low` cannot starve `leader-election`, because leader-election has its own slice. Tightening `catch-all` is how you protect against unauthenticated/anonymous spam.

### 11.3 Seats: LIST vs WATCH

A request consumes **seats** for its duration. For most verbs, 1 seat. For `LIST`, the apiserver estimates the cost based on expected object count and consumes proportionally more seats. For `WATCH`, the request is long-running and consumes a single seat **for its entire duration**.

This is why a 10k-object LIST may "feel like" 5 requests under APF: it ties up 5 seats for the duration of the read. It also means a misbehaving controller that opens 100 watches consumes 100 seats and can lock out an entire PL.

### 11.4 Shuffle-Sharding

Within a PriorityLevel that has queueing, the apiserver shuffles requests across multiple queues using a deterministic hash of the request's distinguisher (e.g. the user name). The goal is to give "well-behaved" senders a high probability that *some* of their requests survive even when a noisy neighbor in the same PL is flooding.

Picture three queues in a PL, each able to admit 10 in-flight. A flood from user A would, without sharding, fill the single queue and reject everyone. With three queues and `handSize=2`, user A's requests land in two of three queues; user B's requests, with high probability, are in a queue A is not in; user B survives.

`handSize` is the number of queues a single distinguisher's requests are spread across. `queueLengthLimit` caps each queue's length. The math is `birthday-paradox-like`: the probability that any two distinguishers collide on all `handSize` queues is roughly `(handSize/totalQueues)^handSize`.

### 11.5 Tuning Knobs

Most clusters never touch APF defaults. When you must:

```
   ─ Add a FlowSchema with a low matchingPrecedence (high priority,
     low number) for your critical controller. Wire it to its own
     PriorityLevel with reserved shares.

   ─ Bump global concurrency:
        --max-requests-inflight=800
        --max-mutating-requests-inflight=400
     This costs memory + etcd connections; only do it on big nodes.

   ─ Reduce catch-all to defend against anonymous abuse.

   ─ Mark a CronJob's SA exempt or high-priority if it absolutely
     must run. Be careful — exempt has no fairness.
```

The metrics to watch:

```
   apiserver_flowcontrol_rejected_requests_total{reason}
       reason="queue-full" → request was rejected, queue full
       reason="time-out"   → request waited past its deadline

   apiserver_flowcontrol_current_inqueue_requests{priority_level}
       depth of queue

   apiserver_flowcontrol_request_wait_duration_seconds{priority_level}
       p99 wait. > 1s = you have a problem.

   apiserver_flowcontrol_request_concurrency_in_use{priority_level}
       seats currently consumed
```

---

## 12. Audit

The audit pipeline records every request at one of four levels and one of four stages. It is the single best forensic tool for a cluster.

### 12.1 Stages

```
   RequestReceived      after WithAudit (begin), BEFORE authN/authZ.
                        Records that a request arrived.
   ResponseStarted      response headers have been written, before
                        the body (used for long-running like WATCH).
   ResponseComplete     full response sent. Most events use this.
   Panic                a panic occurred in the handler chain.
```

Each event has an `Audit-ID` (UUID) so multiple stages of the same request can be correlated. The response header `Audit-ID: ...` is also echoed to the client.

### 12.2 Levels

```
   None              skip; do not audit this rule.
   Metadata          who, when, what (URL, verb), Audit-ID. NO request
                     or response body. Cheap, ~500 bytes per event.
   Request           Metadata + request body. Heavier, ~few KB per event.
   RequestResponse   Metadata + request body + response body. Heaviest,
                     can be 100s of KB for a LIST.
```

### 12.3 Policy and Rules

`--audit-policy-file` specifies an `audit.k8s.io/v1.Policy`:

```yaml
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
  # don't audit kubelet heartbeats
  - level: None
    users: ["system:kube-controller-manager","system:kube-scheduler"]
    verbs: ["watch"]

  # don't audit reads of public-ish things
  - level: None
    verbs: ["get","list","watch"]
    resources:
      - group: ""
        resources: ["configmaps","endpoints"]

  # audit Secret writes at Request level
  - level: Request
    verbs: ["create","update","patch","delete"]
    resources:
      - group: ""
        resources: ["secrets"]

  # everything else at Metadata
  - level: Metadata
```

Rules are evaluated in order; first match wins. There is a heavy tax for getting this wrong — a `RequestResponse` rule on `pods` in a 10k-pod cluster produces gigabytes of audit per minute.

### 12.4 Backends

- **Log file** (`--audit-log-path`, `--audit-log-format=json`): default. Rotated by `--audit-log-maxage`, `--audit-log-maxbackup`, `--audit-log-maxsize`.
- **Webhook** (`--audit-webhook-config-file`): POSTs events in batches to a remote endpoint. Used to send to Loki / SIEM. Throttling matters; a misconfigured webhook can stall the apiserver.
- **Dynamic backend** (legacy, removed).

### 12.5 Storage Volume Reality

A stock cluster running with `Metadata` everywhere on a 5000-node setup produces ~50–200 GB/day of audit. With `Request` on writes, multiply by 2–3. With `RequestResponse` on LISTs, multiply by 50. The most common mistake is enabling `RequestResponse` on read verbs "just for visibility" and saturating the audit-log disk in hours.

Recipe for sane audit:

```
   ─ Metadata everywhere by default
   ─ Request level on writes to security-sensitive resources
     (Secrets, ClusterRoleBindings, ValidatingWebhookConfigurations,
      CertificateSigningRequests)
   ─ None for high-volume read traffic (kubelet watches, kcm leader
     election renewals)
   ─ Webhook backend with batch size 100, queue size 10000, mode
     "blocking-strict" only if you cannot lose any event
```

---

## 13. The Aggregation Layer

The aggregation layer is how the apiserver delegates entire GroupVersions to external HTTP services. The canonical example is `metrics-server`: when you `kubectl top pods`, the URL is `/apis/metrics.k8s.io/v1beta1/pods`, but the data is not in etcd. It comes from a separate pod that the apiserver proxies to.

### 13.1 APIService Objects

```yaml
apiVersion: apiregistration.k8s.io/v1
kind: APIService
metadata:
  name: v1beta1.metrics.k8s.io
spec:
  service:
    name: metrics-server
    namespace: kube-system
    port: 443
  group: metrics.k8s.io
  version: v1beta1
  groupPriorityMinimum: 100
  versionPriority: 100
  caBundle: <base64 CA>
status:
  conditions:
  - type: Available
    status: "True"
```

When this object exists, kube-aggregator routes every request matching `/apis/metrics.k8s.io/v1beta1/...` to the metrics-server service.

### 13.2 RequestHeader Authentication

The downstream apiserver needs to know who the caller is. The aggregation layer uses **RequestHeader auth**: the front (main) apiserver authenticates the user, then proxies the request to the backend with HTTP headers identifying the user:

```
   X-Remote-User: alice
   X-Remote-Group: ops
   X-Remote-Group: developers
   X-Remote-Extra-Authentication.kubernetes.io/pod-name: kubectl-1234
```

Plus a client TLS certificate signed by a CA the backend trusts. The backend verifies the cert (proving the front proxy is who we expect), then trusts the X-Remote-* headers for authentication.

The configuration on the backend (an apiserver-library-go-based service) reads the `extension-apiserver-authentication` ConfigMap from kube-system to get the trusted CA, allowed names, and header prefixes.

### 13.3 The Proxied Flow

```
   client  →  kube-aggregator
                  │ (own AuthN, RBAC for /apis/metrics.k8s.io/...)
                  │
                  │ proxy with X-Remote-* headers + signed cert
                  ▼
           metrics-server
                  │ (verifies front-proxy cert,
                  │  reads X-Remote-User as identity)
                  ▼
              own AuthZ via SubjectAccessReview RPC back
              to kube-apiserver (delegated authorization)
                  │
                  ▼
              serves the response
```

The backend may make `SubjectAccessReview` RPCs back to the main apiserver to delegate authZ decisions, so RBAC works consistently across the aggregation boundary.

We cover building an aggregated apiserver end-to-end in ch 24.

---

## 14. Three-Apiserver Chaining in Detail

Let us walk three concrete requests and see exactly which apiserver handles them.

### 14.1 Request to a Built-in: `GET /api/v1/pods/foo`

```
   1. Client → kube-aggregator
         "do any APIService objects claim /api/v1/?"
         No APIService can claim core/v1 (it is reserved).
         Pass to delegate.

   2. → main kube-apiserver
         GVR pods.v1 found in built-in scheme.
         Run the request handler chain (§3).
         Look up storage in registry/core/pod/storage.
         Storage = cacher → etcd3.
         Cache hit? Serve. Cache miss / RV too new? etcd quorum read.
         Return.
```

### 14.2 Request to a CRD: `GET /apis/cert-manager.io/v1/certificates/foo`

```
   1. Client → kube-aggregator
         "do any APIService objects claim /apis/cert-manager.io/v1?"
         APIServices for CRD-served groups are auto-created by the
         apiextensions-apiserver: there is a synthetic APIService
         like "v1.cert-manager.io". It points to the local
         apiextensions service (in-process).
         So routing here may go through the apiservice loop, but
         the destination is in-process apiextensions-apiserver.

   2. → main kube-apiserver
         Does the main apiserver have cert-manager.io/v1 in scheme?
         No. Delegate.

   3. → apiextensions-apiserver
         Lookup CRD "certificates.cert-manager.io".
         Found. Run the handler chain (same filter chain).
         Generic CRD handler:
            - decode body as Unstructured
            - schema-validate (OpenAPI v3 + CEL)
            - if conversion: call webhook
            - call storage (cacher → etcd3) with the CRD key
              /registry/cert-manager.io/certificates/...
         Return.
```

### 14.3 Request to an Aggregated apiserver: `GET /apis/metrics.k8s.io/v1beta1/nodes`

```
   1. Client → kube-aggregator
         APIService "v1beta1.metrics.k8s.io" → metrics-server svc.
         Run the handler chain UP TO AuthZ (authN happens here so we
         can RBAC-check before proxying).
         Then proxy to metrics-server with X-Remote-User headers.

   2. → metrics-server pod (separate process, different binary)
         Verifies front-proxy cert.
         Trusts X-Remote-User headers.
         Performs its own authZ (SubjectAccessReview back to main
         apiserver, or local cache).
         Serves the response from its in-memory metric store.

   3. Response flows back through aggregator to client.
```

### 14.4 The Discovery View

`/apis` returned by kube-aggregator is the union of:
- aggregator's own resources (`apiregistration.k8s.io/v1.APIService`)
- main apiserver's resources (all built-ins)
- apiextensions-apiserver's resources (all CRDs)
- every registered APIService's reported discovery (queried periodically and cached)

With aggregated discovery (§10.3), this whole thing is one HTTP response with caching headers.

---

## 15. Performance Characteristics

This section is the operational core: what is expensive, what is cheap, and what the well-known knobs do.

### 15.1 The Cost Model

```
   Operation                      Dominated by                       Scaling
   ──────────────────────────     ──────────────────────────         ─────────────
   GET object                     etcd quorum read (if RV not in     O(1)
                                  cache) or in-memory copy
   GET object (RV=0)              in-memory copy                     O(1)
   LIST (RV unset)                etcd range read +                  O(N) bytes
                                  protobuf decode every object       transferred
   LIST (RV=0)                    walk in-memory store,              O(N) memory
                                  allocate response                  + CPU
   LIST with fieldSelector        if indexed: O(matching set)        cheap
                                  if not indexed: full scan then     expensive
                                  filter
   LIST with labelSelector        full walk + filter                 O(N)
   WATCH (open)                   register subscriber, send          O(K) where K
                                  initial state                      = matching objs
   WATCH (steady)                 per-event delivery cost            O(events/sec)
   CREATE / UPDATE                etcd put + watch fan-out           O(1) + O(watchers)
                                  + admission webhooks               + webhook latency
   DELETECOLLECTION               LIST + per-object DELETE           O(N)
   PATCH (SSA)                    decode + merge + admission +       O(size of fieldset)
                                  CAS update
```

The dominant variable on a stable cluster is the LIST. A controller that lists 100k pods every reconcile fries the apiserver; the same controller running an informer pays the cost once at startup and then handles deltas.

### 15.2 Watch Cache Memory Model

```
   For each resource:
       store           = map[namespace+name]*Object         ─ all objects
       ring buffer     = []event                            ─ recent events
       indexers        = map[indexerName]map[indexValue]set ─ extra lookups

   Memory per object ≈ size(decoded object) + indexer overhead

   Dominant resources by memory:
       Pods            ~10 KiB × N pods
       Events          ~2 KiB × M events (TTL'd, but bursts)
       Endpoints       deprecated; large per-service
       EndpointSlices  ~few KB × num-services
       Leases          tiny but very high write rate
       Secrets         variable; can be 1 MiB each
       ConfigMaps      variable; some big binaries land here
```

The single most common "apiserver OOM" cause is a controller that creates millions of small objects (typically Events from a flapping operator or huge CRDs with no quota).

### 15.3 Well-Behaved Client vs Thrashing

The shape of a well-behaved client:
- Uses a SharedInformer (one LIST + WATCH per resource, cached in-process).
- Reads from the local cache, not the apiserver.
- Uses bookmarks for resumable watches.
- Uses fieldSelector / labelSelector to subscribe to only what it cares about.
- Uses Server-Side Apply for writes.
- Backs off on conflict (`StatusConflict`) and on 429.

The shape of a thrashing client:
- Polls. `for { kubectl get pods }` is the canonical bad pattern.
- Calls `apiserver.Get(...)` inside Reconcile, never the informer cache.
- Opens new watches per reconcile and never cancels them.
- LISTs the world (no selector) every cycle.
- Retries immediately on errors with no backoff.

A single thrashing controller can take a 10k-node cluster's apiserver from 5% CPU to 95%. APF can contain the damage; it cannot prevent it.

### 15.4 Tuning Knobs

```
   --max-requests-inflight         global non-mutating concurrency
   --max-mutating-requests-inflight global mutating concurrency
                                    (apf shares carve up the union)

   --watch-cache=true               (default; do not turn off)
   --watch-cache-sizes              per-resource ring buffer cap
   --default-watch-cache-size       default ring buffer cap

   --request-timeout                default per-request timeout (60s)
   --min-request-timeout            min for watches (1800s typically)

   --etcd-servers-overrides         shard certain resources to a different etcd
                                    (events to their own etcd is a common move)
   --storage-media-type             default storage encoding (don't change for
                                    built-ins; protobuf is correct)

   --tracing-config-file            OTel tracing for slow requests

   --enable-priority-and-fairness   on by default; off only in dev

   --feature-gates                  e.g. WatchList, ConsistentList,
                                    APIServerIdentity, etc.
```

### 15.5 Profiling

`/debug/pprof/profile`, `/debug/pprof/heap`, `/debug/pprof/goroutine` are exposed (gated by `--profiling=true`, default true). The two most useful in practice:

```
   go tool pprof -http=:7070 'https://api/.../debug/pprof/heap'
   go tool pprof -http=:7070 'https://api/.../debug/pprof/profile?seconds=30'
```

Memory profiles almost always point at the watch cache or the protobuf decoder. CPU profiles often point at JSON encoding (the largest fraction for human-facing requests) or at admission/conversion webhooks (which show up as HTTP client time).

---

## 16. Observability: Metrics and SLOs

### 16.1 The Indispensable Dozen

```
   # request latency
   apiserver_request_duration_seconds{verb, resource, group, code}
       histogram; the source of nearly every alert.

   # request volume
   apiserver_request_total{verb, resource, group, code}
       counter; combined with rate() for QPS.

   # in-flight
   apiserver_current_inflight_requests{request_kind}
       gauge; mutating vs read.

   # APF
   apiserver_flowcontrol_dispatched_requests_total{flow_schema, priority_level}
   apiserver_flowcontrol_rejected_requests_total{flow_schema, priority_level, reason}
   apiserver_flowcontrol_request_wait_duration_seconds{flow_schema, priority_level}
   apiserver_flowcontrol_request_concurrency_in_use{priority_level}
   apiserver_flowcontrol_current_inqueue_requests{priority_level}

   # storage
   etcd_request_duration_seconds{type, operation}
       (exposed by apiserver, talking to etcd)
   apiserver_storage_objects{resource}
       gauge; per-resource object count.
   apiserver_storage_db_total_size_in_bytes
       gauge; etcd's reported db size.

   # watch cache
   apiserver_watch_cache_events_received_total{resource}
   apiserver_watch_cache_events_dispatched_total{resource}
   apiserver_storage_list_total{resource}
       indicator of watch-cache freshness

   # admission
   apiserver_admission_webhook_admission_duration_seconds{name, type, operation}
       per-webhook p99 latency
   apiserver_admission_webhook_rejection_count{name, type, operation, error_type}

   # audit
   apiserver_audit_event_total
   apiserver_audit_error_total
   apiserver_audit_requests_rejected_total

   # process
   process_resident_memory_bytes
   process_cpu_seconds_total
```

### 16.2 SLOs

Kubernetes scalability SIG defines reference SLOs you can adopt verbatim:

```
   1. 99th percentile request latency (per verb)
      ─ mutating verbs, non-namespaced or single-namespace:
          p99 ≤ 1s
      ─ non-mutating verbs:
          p99 ≤ 1s for non-LIST
          p99 ≤ 30s for LIST (yes, really; LIST is allowed to be slow
                  for large collections, capped at request-timeout)

   2. Pod startup latency
      ─ p99(time from Pod create → first container running) ≤ 5s for stateless
      ─ separate measurement and SLO (ch 09, 10)

   3. Watch latency
      ─ p99(watch event delivery) ≤ 1s for system-critical resources
```

### 16.3 A Working Alert Set

```
   # apiserver latency SLO burn
   ALERT KubeAPIServerLatency
     IF histogram_quantile(0.99, sum by (le, verb, resource) (
            rate(apiserver_request_duration_seconds_bucket{verb!="WATCH"}[5m])
         )) > 1
     FOR 10m

   # APF rejection rate
   ALERT KubeAPFRejecting
     IF sum by (priority_level) (
            rate(apiserver_flowcontrol_rejected_requests_total[5m])
         ) > 0.5  # > 0.5/s sustained
     FOR 5m

   # etcd slowness leaking through to apiserver
   ALERT KubeAPIServerEtcdSlow
     IF histogram_quantile(0.99,
            rate(etcd_request_duration_seconds_bucket[5m])
         ) > 1
     FOR 10m

   # webhook latency
   ALERT KubeAdmissionWebhookSlow
     IF histogram_quantile(0.99,
            rate(apiserver_admission_webhook_admission_duration_seconds_bucket[5m])
         ) > 1
     FOR 10m

   # watch cache vs etcd lag (proxy for "watches are far behind")
   ALERT KubeWatchCacheBehind
     IF (apiserver_watch_cache_resource_version
          - on(resource) etcd_object_counts) > 100
     FOR 5m
```

### 16.4 Tracing

If `--tracing-config-file` is configured, every request gets an OpenTelemetry span. The span tree typically looks like:

```
   apiserver: PATCH /apis/apps/v1/namespaces/.../deployments/...
   ├── authentication
   ├── authorization
   ├── apiserver: admission
   │   ├── webhook: my-webhook
   │   └── webhook: kyverno
   ├── apiserver: validate
   ├── etcd: txn
   └── watch fan-out
```

That tree is the fastest way to determine "why was this request 800ms?" — almost always you will see the time concentrated in one of: admission webhook, etcd txn, conversion webhook.

---

## 17. Pitfalls and Anti-Patterns

The list of mistakes you will see (and probably make) operating kube-apiserver.

### 17.1 LIST Without Selectors

`kubectl get pods -A` on a 100k-pod cluster pulls every pod into kubectl, serializes 100k objects, transfers ~1 GiB over the wire, and consumes seats in APF for tens of seconds. The cluster survives, but other clients pay the price.

Defenses:

```
   ─ Always pass --field-selector or --selector if you know
     what you want.
   ─ Use --chunk-size=500 (default in modern kubectl).
   ─ Forbid anonymous LIST via APF + RBAC.
   ─ Add ResourceQuotas to cap object counts per namespace.
   ─ Use kubectl explain | kubectl get -w instead of repeated LIST.
```

### 17.2 List-and-Poll Instead of List-and-Watch

A controller written by someone who does not know about informers:

```python
   while True:
       pods = client.list_pods()
       reconcile(pods)
       time.sleep(5)
```

Multiply that by 50 controllers and you have a 10 LIST/s apiserver baseline doing no useful work. Every Kubernetes client library has informers; use them. The informer does `LIST` once, then `WATCH` forever; you reconcile off the local cache.

### 17.3 Abandoning a Watch Without Closing

A controller that opens a watch, forgets to call `Stop()`, and leaks the connection. The apiserver keeps the goroutine, keeps the seat in APF, keeps the watch cache subscriber. Over hours, the apiserver leaks goroutines; over days, it OOMs.

Always:

```go
   w, err := client.Watch(ctx, opts)
   if err != nil { return err }
   defer w.Stop()
   for ev := range w.ResultChan() { ... }
```

`ctx` cancellation should propagate stop; the `defer` is belt-and-braces.

### 17.4 Per-Namespace Queries on 100k-Namespace Clusters

Running `kubectl get pods -n <ns>` is fine. Running it in a loop across 100k namespaces is not — even with informers, you end up with 100k watches, each holding a seat. The right architecture for fleet-wide queries on multi-tenant clusters is a single informer at cluster-scope with appropriate authZ (a `ClusterRole`), filtered locally, not 100k namespaced ones.

### 17.5 Conversion Webhook Latency

A CRD with a webhook conversion and >10k objects is fragile. Every list pays N webhook calls. Symptoms:

- LIST p99 > 30s.
- `apiserver_admission_webhook_admission_duration_seconds` (or its conversion analog) climbs.
- Eventually 504s on the apiserver.

The fix is to converge on a single storage version and drop the webhook. If you must keep the webhook (e.g. you support both v1 and v2 long-term), shard work: serve conversion from a horizontally-scaled deployment, not one pod; pin storage to the *most-used* version so the webhook is only called for less-used versions.

### 17.6 Very Large Objects

A 500-KiB Secret containing an entire PKI chain. A 1-MiB ConfigMap containing a Lua script. A Pod spec with 4000 env vars. All technically legal; all destroy the watch cache.

```
   apiserver_storage_objects{resource="secrets"} * average size
       = memory the apiserver pays for that resource

   bytes per LIST response = N matching objects × average size
       (multiply by ~1.5–2× for JSON over protobuf)
```

The right answer for "I have lots of data per object" is to push the data out of etcd: store it in S3, in a Secret-of-secrets-of-references, in a CRD subresource that excludes the heavy field from watch caching, or via a custom aggregated apiserver.

### 17.7 Audit at RequestResponse on a Chatty Cluster

Already covered (§12.5). The mistake is enabling it cluster-wide instead of for the specific high-value resources.

### 17.8 Disabling AuthN/AuthZ for Convenience

`--anonymous-auth=true` + `--authorization-mode=AlwaysAllow` is convenient for dev. It is also CVE-equivalent in production. Always run with at least `Node,RBAC`. Always require an authenticated identity for every verb that mutates state. Never expose the apiserver to the public internet without a network ACL.

### 17.9 Forgetting Storage Version Migration

You drop `apps/v1beta1` from the apiserver's served versions. Half your Deployments were created back when `v1beta1` was the storage version. They are still in etcd encoded as `v1beta1`. The apiserver can still decode them (v1beta1 is in the registered scheme), but if you also drop the *registered* v1beta1 type, decoding fails with "no kind 'Deployment' is registered for version 'apps/v1beta1'" and your Deployments become unreadable.

Always run StorageVersionMigrator before dropping a version.

### 17.10 Bypassing the Informer Cache in Reconcile

```go
   // BAD
   func (r *Reconciler) Reconcile(ctx context.Context, req Request) (Result, error) {
       pod := &corev1.Pod{}
       err := r.Client.Get(ctx, req.NamespacedName, pod, &GetOptions{Raw: true})
       // ^ goes straight to apiserver, skips cache
   }

   // GOOD
   func (r *Reconciler) Reconcile(ctx context.Context, req Request) (Result, error) {
       pod := &corev1.Pod{}
       err := r.Client.Get(ctx, req.NamespacedName, pod)
       // ^ controller-runtime client by default goes through cache
   }
```

The cached client is 1000× cheaper. If your reconcile is racing with a fresh write, use `client.Patch` with `client.Apply` and let SSA's conflict detection do the right thing.

### 17.11 Watches That Never Bookmark

Already covered (§7.3). The pattern to avoid: a custom client that opens a watch, drops it after a network blip, and falls back to a full LIST. On a big cluster this is the difference between "blip" and "outage".

### 17.12 ConfigMaps as a Database

A trap many teams fall into: store application config / state / a tiny database in ConfigMaps. Every update is a write to etcd that fans out to every watcher. Every reader's watch cache holds it. A 100-KB ConfigMap updated every minute by 50 controllers can wedge the cluster.

Use a Secret for credentials. Use a CRD with `status` subresource for app state owned by one controller. Use external storage (S3, etcd-of-the-app) for actual databases.

---

## 18. TL;DR

kube-apiserver is **the only writer to etcd** and **the only auth boundary in the cluster**. Three apiservers chain inside one binary: kube-aggregator (routes), main apiserver (built-ins), apiextensions-apiserver (CRDs).

Every request runs the **filter chain**: TLS → panic recovery → timeout → RequestInfo parse → AuthN → audit-begin → impersonation → AuthZ → APF → waitgroup → trace → dispatch into the registry. **Audit ends** after the response. APF is the cluster's queueing system; FlowSchemas route requests to PriorityLevels with concurrency shares and shuffle-sharded queues.

Inside the registry, every resource has a **Strategy** (PrepareForCreate/Update, Validate, Canonicalize) and a `genericregistry.Store` over a `storage.Interface`. The storage interface has two implementations: `etcd3` (raw) and `cacher.Cacher` (the watch cache wrapping etcd3). Built-ins go protobuf-encoded; CRDs go JSON-encoded as Unstructured.

**Version conversion** is hub-and-spoke: every external version converts to an internal hub, runs defaulters/admission/validation in the hub, then converts to the storage version for write or the response version for read. CRDs replace generated conversion with **webhook conversion**, which is the single most common CRD performance trap.

The **watch cache** is per-apiserver, per-resource. It holds all objects in memory plus a ring buffer of recent events; one etcd watch fans out to thousands of client watches. **Bookmarks** let well-behaved clients resume cheaply after disconnects. **LIST with resourceVersion=0** serves from cache; without RV does a quorum etcd read. **Pagination** pins a resourceVersion across pages.

**Server-Side Apply** tracks per-field ownership in `metadata.managedFields`. Each apply asserts ownership of paths; conflicts return 409 unless `force=true`. Releasing a field unsets it. SSA is the principled solution to "multiple controllers writing the same object".

**OpenAPI v3 + aggregated discovery** are how clients learn the API surface efficiently. **Audit** records every request at one of four stages and four levels; getting the level wrong fills your audit disk in hours.

**The aggregation layer** delegates entire GroupVersions to external HTTP services via APIService + RequestHeader auth. Metrics-server is the canonical example.

**Performance**: LIST is the killer. Watch cache memory dominates apiserver RAM. Well-behaved clients use informers (LIST once + WATCH forever); thrashing clients poll. APF protects the apiserver from itself.

**Observability**: `apiserver_request_duration_seconds`, `apiserver_flowcontrol_*`, `etcd_request_duration_seconds`, `apiserver_admission_webhook_admission_duration_seconds`, and `apiserver_storage_objects` are the metrics you must alert on. SLO targets: p99 ≤ 1s for non-LIST, ≤ 30s for LIST, ≤ 1s for watch event delivery on critical resources.

**The single sentence to remember**: every controller in Kubernetes is a client of a watch-cache subscriber backed by a registry-mediated etcd write — change any one piece and the whole abstraction stops working, so the apiserver is engineered to make all three rock-solid at once.

Read next: ch 06 (admission, the layer we kept glossing over), ch 07 (authN/authZ in detail), ch 08 (client-go informers — what the apiserver is being talked to by), and revisit ch 04 (etcd) once you have ch 08 in hand for the full read-path picture.
